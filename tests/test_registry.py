"""hydrowf-registry: the store, the HTTP rules, the seed and the migration, with stub keys.

The database is SQLite in a temporary directory, or the one TEST_DATABASE_URL names (CI's Postgres job;
its tables are dropped and migrated again for every test).
"""
import json
import os
import time
from pathlib import Path

import jwt
import pytest
import yaml
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from sqlalchemy import create_engine, insert, text

from hydrowf_core.auth import BearerAuth
from hydrowf_core.models import LEGOS

from hydrowf_registry import app as registry_app
from hydrowf_registry.db import lego_ids, legos, metadata
from hydrowf_registry.seed import PLATFORM, migrate, seed
from hydrowf_registry.store import Store

LEGOS_FILE = Path(__file__).parent / "fixtures" / "legos.yaml"      # hydrowf-ogc's registry/legos.yaml
SHIPPED = LEGOS.validate_python(yaml.safe_load(LEGOS_FILE.read_text()))

ISSUER = "https://keycloak.test/realms/hydrowf"
AUDIENCE = "hydrowf-api"
ALICE, BOB, CAROL = "a1cc0604-alice", "434e88e1-bob", "77aa0d1e-carol"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SIGNING_KEY = jwt.PyJWK({**RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True), "alg": "RS256", "kid": "test"})
AUTHOR = ["/tool-authors"]                     # Keycloak's groups claim, "Full group path" on
USU = ["/usu-hydrology"]


class StubKeys:
    def get_signing_key_from_jwt(self, token):
        return SIGNING_KEY


def bearer(sub=ALICE, groups=None):
    now = int(time.time())
    body = {"iss": ISSUER, "aud": AUDIENCE, "sub": sub, "iat": now, "exp": now + 300}
    if groups is not None:
        body["groups"] = groups
    return {"Authorization": f"Bearer {jwt.encode(body, KEY, algorithm='RS256', headers={'kid': 'test'})}"}


@pytest.fixture
def engine(tmp_path):
    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'registry.db'}"
    engine = create_engine(url)
    with engine.begin() as conn:                  # a clean database (matters only on a shared Postgres)
        for table in ("legos", "lego_ids", "alembic_version"):      # children first
            conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
    migrate(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def store(engine):
    store = Store(engine)
    seed(store, SHIPPED)
    return store


@pytest.fixture
def client(store):
    return TestClient(registry_app.create_app(store, BearerAuth(ISSUER, AUDIENCE, keys=StubKeys())))


def shipped(id):
    return next(lego for lego in SHIPPED if lego.id == id).model_dump(mode="json")


def lego_like(id, new_id=None, version="9.9.9", **changes):
    """A shipped lego as a client would POST it: a new version, or under a new id (a new lego)."""
    return {**shipped(id), "id": new_id or id, "version": version, **changes}


EXTERNAL = "nwm-retrospective-3.0-forcing"     # data, external
MANAGED = "hydrofabric-v2.2"                   # data, managed
TOOL = "ngiab-run"
PIPELINE = "ngiab-simulation"


def post(client, lego, sub=ALICE, groups=None, **params):
    return client.post("/legos", json=lego, headers=bearer(sub, groups), params=params)


def with_tool(pipeline, tool_id, version):
    """`pipeline` with its TOOL step pinned to another tool."""
    pipeline["steps"] = [{**s, "tool": {"id": tool_id, "version": version}} if s["tool"]["id"] == TOOL else s
                         for s in pipeline["steps"]]
    return pipeline


# ---- the seed and the schema


def test_the_seed_publishes_every_shipped_lego_as_the_platforms(store, engine):
    assert len(store.visible(None)) == len(SHIPPED)
    assert {body["owner"] for body in store.visible(None)} == {PLATFORM.sub}
    with engine.connect() as conn:
        assert set(conn.scalars(text("SELECT DISTINCT owner_group FROM lego_ids")).all()) == {"tool-authors"}


def test_seeding_again_skips_what_is_there(store, engine):
    migrate(engine)                                # the next start's `alembic upgrade head`: nothing to do
    added, skipped = seed(store, SHIPPED)
    assert added == [] and len(skipped) == len(SHIPPED)


def test_seeding_a_changed_lego_under_the_same_version_fails(store):
    changed = [lego.model_copy(update={"title": "changed"}) if lego.id == TOOL else lego for lego in SHIPPED]
    with pytest.raises(ValueError, match="bump its version"):
        seed(store, changed)


def test_the_migration_matches_the_tables(engine):
    with engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), metadata) == []


# ---- reads: open, public legos plus the caller's own, as stored


def test_anyone_reads_the_public_legos(client):
    assert {x["id"] for x in client.get("/legos", params={"kind": "pipeline"}).json()} == {
        lego.id for lego in SHIPPED if lego.kind == "pipeline"}
    lego = shipped(TOOL)
    assert client.get(f"/legos/{TOOL}/{lego['version']}").json()["image"] == lego["image"]
    assert [x["version"] for x in client.get(f"/legos/{TOOL}").json()] == [lego["version"]]


@pytest.mark.parametrize("path", ["/legos/nope", "/legos/nope/1.0.0"])
def test_a_missing_lego_is_404(client, path):
    assert client.get(path).status_code == 404


def test_a_bad_token_is_refused_even_on_reads(client):
    response = client.get("/legos", headers={"Authorization": "Bearer abc"})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == 'Bearer error="invalid_token"'


def test_versions_sort_as_numbers(client):
    for version in ["1.9.0", "1.10.0", "1.2.0"]:
        assert post(client, lego_like(EXTERNAL, "alice-forcing", version)).status_code == 201
    versions = [x["version"] for x in client.get("/legos/alice-forcing", headers=bearer()).json()]
    assert versions == ["1.2.0", "1.9.0", "1.10.0"]


def test_a_stored_lego_an_older_core_wrote_still_reads(client, engine):
    """Reads return what was stored, so a stricter core can't break them; a check that needs such a lego
    treats it as missing (422), not as a server error."""
    old = {**shipped(TOOL), "id": "old-tool", "version": "1.0.0", "visibility": "public", "removed_field": 1}
    with engine.begin() as conn:
        conn.execute(insert(lego_ids).values(id="old-tool", kind="tool", owner=ALICE))
        conn.execute(insert(legos).values(id="old-tool", version="1.0.0", kind="tool", owner=ALICE,
                                          visibility="public", body=old))
    assert client.get("/legos").status_code == 200
    assert client.get("/legos/old-tool/1.0.0").json()["removed_field"] == 1
    pipeline = with_tool(lego_like(PIPELINE, "uses-old-tool"), "old-tool", "1.0.0")
    assert post(client, pipeline).status_code == 422


# ---- publishing: which kinds a caller may publish (may_add)


@pytest.mark.parametrize("id, groups, status", [
    (TOOL, None, 403),
    (TOOL, AUTHOR, 201),
    (TOOL, ["tool-authors"], 201),             # "Full group path" off: the bare name
    (TOOL, ["/other/tool-authors"], 403),      # a subgroup of the same name is not the group
    (MANAGED, None, 403),
    (MANAGED, AUTHOR, 201),
    (EXTERNAL, None, 201),
    (PIPELINE, None, 201),
], ids=["tool", "tool-author", "tool-author-bare-name", "subgroup", "managed-data", "tool-author-managed",
        "external-data", "pipeline"])
def test_who_may_publish_which_kind(client, id, groups, status):
    response = post(client, lego_like(id, f"new-{id}"[:63]), groups=groups)
    assert response.status_code == status, response.text


def test_publishing_needs_a_token(client):
    response = client.post("/legos", json=lego_like(EXTERNAL, "anon-forcing"))
    assert response.status_code == 401 and response.headers["WWW-Authenticate"] == "Bearer"


def test_the_owner_is_the_token_not_the_body(client, store):
    response = post(client, lego_like(EXTERNAL, "alice-forcing", owner=BOB))
    assert response.status_code == 201 and response.json()["owner"] == ALICE
    assert response.headers["Location"].endswith("/legos/alice-forcing/9.9.9")
    assert store.get("alice-forcing", "9.9.9", ALICE)["owner"] == ALICE


def test_a_version_is_immutable(client):
    lego = lego_like(EXTERNAL, "alice-forcing", visibility="public")
    assert post(client, lego).status_code == 201
    assert post(client, {**lego, "title": "changed"}).status_code == 409
    assert post(client, lego, BOB).status_code == 403          # not his id: told nothing about its versions
    assert client.get("/legos/alice-forcing/9.9.9").json()["title"] == lego["title"]


def test_a_version_over_32_characters_is_422_and_reads_still_work(client):
    response = post(client, lego_like(EXTERNAL, "alice-forcing", version="1.0." + "1" * 5000))
    assert response.status_code == 422
    assert client.get("/legos").status_code == 200


# ---- ids: owned by their first publisher, or a group; the kind is fixed


def test_only_an_ids_owner_publishes_its_next_versions(client):
    assert post(client, lego_like(EXTERNAL, "alice-forcing", "1.0.0")).status_code == 201
    assert post(client, lego_like(EXTERNAL, "alice-forcing", "1.1.0")).status_code == 201
    assert post(client, lego_like(EXTERNAL, "alice-forcing", "2.0.0"), BOB).status_code == 403


def test_an_ids_kind_is_fixed(client):
    assert post(client, lego_like(EXTERNAL, "alice-forcing", "1.0.0")).status_code == 201
    pipeline = lego_like(PIPELINE, "alice-forcing", "2.0.0")
    response = post(client, pipeline)
    assert response.status_code == 422 and "can't change its kind" in response.json()["detail"][0]["msg"]


def test_an_ids_data_kind_is_fixed(client):
    assert post(client, lego_like(EXTERNAL, "alice-data", "1.0.0"), groups=AUTHOR).status_code == 201
    managed = lego_like(MANAGED, "alice-data", "2.0.0")
    response = post(client, managed, groups=AUTHOR)
    assert response.status_code == 422 and "can't change its kind" in response.json()["detail"][0]["msg"]


def test_a_failed_first_version_claims_nothing(client):
    pipeline = lego_like(PIPELINE, "contested-sim")
    pipeline["steps"] = [{**step, "tool": {"id": "no-such-tool", "version": "1.0.0"}} for step in pipeline["steps"]]
    assert post(client, pipeline, ALICE).status_code == 422
    assert post(client, lego_like(PIPELINE, "contested-sim"), BOB).status_code == 201   # the id is still free


def test_a_non_owner_learns_nothing_about_an_ids_versions(client):
    assert post(client, lego_like(EXTERNAL, "alice-forcing", "1.0.0", visibility="owner")).status_code == 201
    assert post(client, lego_like(EXTERNAL, "alice-forcing", "1.0.0"), BOB).status_code == 403   # not 409
    assert post(client, lego_like(EXTERNAL, "alice-forcing", "1.0.0"), BOB,
                owner_group="x").status_code == 403                                              # not 422


def test_reseeding_survives_a_core_default_the_stored_rows_lack(store, engine):
    """A row stored without a field the current core fills in with a default is still the same lego."""
    with engine.begin() as conn:
        body = conn.execute(text("SELECT body FROM legos WHERE id = :id"), {"id": EXTERNAL}).scalar_one()
        body = json.loads(body) if isinstance(body, str) else body
        body.pop("description", None)            # LegoBase.description defaults to ""
        conn.execute(legos.update().where(legos.c.id == EXTERNAL).values(body=body))
    added, skipped = seed(store, SHIPPED)
    assert added == [] and f"{EXTERNAL}@1.0.0" in skipped


def test_a_platform_id_takes_new_versions_from_tool_authors_only(client):
    hijack = lego_like(EXTERNAL, MANAGED, visibility="public")   # someone's own URL under the platform's id
    assert post(client, hijack, BOB).status_code == 403
    assert post(client, lego_like(EXTERNAL, PIPELINE), BOB).status_code == 403   # an id isn't up for grabs
    assert post(client, lego_like(MANAGED), BOB, AUTHOR).status_code == 201


def test_a_group_owned_id_takes_new_versions_from_its_members(client):
    first = lego_like(EXTERNAL, "usu-gages", "1.0.0")
    assert post(client, first, ALICE, USU, owner_group="usu-hydrology").status_code == 201
    assert post(client, lego_like(EXTERNAL, "usu-gages", "1.1.0"), BOB, USU).status_code == 201
    assert post(client, lego_like(EXTERNAL, "usu-gages", "1.2.0"), CAROL).status_code == 403
    assert post(client, lego_like(EXTERNAL, "usu-gages", "1.3.0"), ALICE).status_code == 403   # left the group


def test_an_id_can_only_be_given_to_ones_own_group(client):
    response = post(client, lego_like(EXTERNAL, "usu-gages"), ALICE, owner_group="usu-hydrology")
    assert response.status_code == 403
    assert post(client, lego_like(EXTERNAL, "usu-gages"), BOB).status_code == 201    # nothing was claimed


def test_an_ids_owner_group_is_set_once(client):
    first = post(client, lego_like(EXTERNAL, "usu-gages", "1.0.0"), ALICE, USU, owner_group="usu-hydrology")
    assert first.status_code == 201
    response = post(client, lego_like(EXTERNAL, "usu-gages", "1.1.0"), ALICE, USU + AUTHOR,
                    owner_group="tool-authors")
    assert response.status_code == 422


# ---- checks: against the legos referenced, which a public lego must find among public ones


def test_a_lego_that_does_not_fit_is_422_with_every_problem(client):
    pipeline = lego_like(PIPELINE, "alice-sim")
    pipeline["steps"] = [{**step, "tool": {"id": "no-such-tool", "version": "1.0.0"}} for step in pipeline["steps"]]
    response = post(client, pipeline)
    assert response.status_code == 422
    problems = response.json()["detail"]
    assert len(problems) == len(pipeline["steps"])
    assert all({"loc", "msg", "type"} <= set(p) and "no-such-tool" in p["msg"] for p in problems)


def test_a_malformed_lego_is_422(client):
    response = post(client, lego_like(TOOL, "alice-tool", image="not pinned"), groups=AUTHOR)
    assert response.status_code == 422 and {"loc", "msg", "type"} <= set(response.json()["detail"][0])


def test_a_private_lego_is_its_owners_alone(client):
    assert post(client, lego_like(EXTERNAL, "alice-forcing", visibility="owner")).status_code == 201
    path = "/legos/alice-forcing/9.9.9"
    assert client.get(path, headers=bearer(ALICE)).status_code == 200
    assert client.get(path, headers=bearer(BOB)).status_code == 404
    assert client.get(path).status_code == 404
    assert "alice-forcing" not in {x["id"] for x in client.get("/legos", headers=bearer(BOB)).json()}


def test_a_pipeline_uses_only_tools_its_publisher_can_see(client):
    assert post(client, lego_like(TOOL, "alice-run", visibility="owner"), groups=AUTHOR).status_code == 201
    pipeline = with_tool(lego_like(PIPELINE, "sim-on-alice-run", visibility="owner"), "alice-run", "9.9.9")
    assert post(client, pipeline, BOB).status_code == 422          # Bob can't see Alice's private tool
    assert post(client, pipeline, ALICE).status_code == 201        # Alice can, for her own private pipeline


def test_a_pipeline_cant_read_in_place_an_output_stored_as_one_tgz(client):
    reader = lego_like(TOOL, "alice-reader", visibility="owner")
    reader["inputs"] = [{**{k: v for k, v in port.items() if k != "path"}, "type": "dataset"}
                        if port["name"] == "ngen_run" else port for port in reader["inputs"]]
    assert post(client, reader, groups=AUTHOR).status_code == 201
    pipeline = lego_like("ngiab-rerun", "alice-reread", visibility="owner")
    pipeline["steps"] = [{**s, "tool": {"id": "alice-reader", "version": "9.9.9"}} if s["name"] == "run" else s
                         for s in pipeline["steps"]]
    response = post(client, pipeline)
    assert response.status_code == 422, response.text           # ngen-run keeps a run as one .tgz (archive: tgz)
    assert any(".tgz" in problem["msg"] for problem in response.json()["detail"])


def test_a_public_lego_never_depends_on_a_private_one(client):
    assert post(client, lego_like(TOOL, "alice-run", visibility="owner"), groups=AUTHOR).status_code == 201
    pipeline = with_tool(lego_like(PIPELINE, "public-sim", visibility="public"), "alice-run", "9.9.9")
    assert post(client, pipeline, ALICE).status_code == 422        # even its owner: others couldn't run it


# ---- request bodies: at most MAX_BODY bytes, counted


def test_a_body_over_max_body_is_413(client):
    huge = b"x" * (registry_app.MAX_BODY + 1)
    assert client.post("/legos", content=huge, headers=bearer()).status_code == 413
    chunks = (b"x" * 65536 for _ in range(registry_app.MAX_BODY // 65536 + 1))
    assert client.post("/legos", content=chunks, headers=bearer()).status_code == 413


def test_a_chunked_lego_is_read(client):
    body = json.dumps(lego_like(EXTERNAL, "chunked-forcing")).encode()
    response = client.post("/legos", content=iter([body[:20], body[20:]]),
                           headers={**bearer(), "Content-Type": "application/json"})
    assert response.status_code == 201, response.text


# ---- pydantic at the door: path parameters and owner_group are core's types; /docs shows the lego schema


@pytest.mark.parametrize("path", ["/legos/BAD_ID", "/legos/ngiab-run/not-a-version", "/legos/" + "a" * 64])
def test_a_malformed_id_or_version_is_422_before_the_database(client, path):
    assert client.get(path).status_code == 422


@pytest.mark.parametrize("group", ["", "/leading-slash", "bad!chars", "a//b"])
def test_owner_group_must_be_a_group_name(client, group):
    assert post(client, lego_like(EXTERNAL, "alice-forcing"), owner_group=group).status_code == 422


def test_the_docs_describe_the_lego_schema(client):
    get = client.get("/openapi.json").json()["paths"]["/legos/{id}/{version}"]["get"]
    assert "discriminator" in json.dumps(get["responses"]["200"])
    assert get["parameters"][0]["schema"]["maxLength"] == 63


def test_external_data_must_point_at_a_public_host(client):
    metadata = lego_like(EXTERNAL, "metadata-forcing")
    metadata["storage"] = {**metadata["storage"], "url": "https://169.254.169.254/latest/meta-data/"}
    assert post(client, metadata).status_code == 422          # hydrowf-core's public_host, at the door
