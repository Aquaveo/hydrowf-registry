# hydrowf-registry

The hydrowf lego registry: tool, data and pipeline legos, versioned and shared over HTTP. Built on
FastAPI; the lego models and their checks come from
[hydrowf-core](https://github.com/Aquaveo/hydrowf-core) (`hydrowf_core.models`, `hydrowf_core.registry`),
and so do bearer tokens (`hydrowf_core.auth`), shared with hydrowf-ogc.

```
client ──HTTPS + Bearer JWT──▶ uvicorn (FastAPI) ──▶ Store ──▶ SQLite (local) / Postgres (cluster)
                                  │                    └─ hydrowf_core: models validate, registry.check
                                  └─ may_add(): who may add which lego
```

## Routes

| Route | Who | What |
|---|---|---|
| `GET /legos?kind=tool\|data\|pipeline` | anyone | The legos the caller may see: public ones and their own |
| `GET /legos/{id}` | anyone | That lego's versions, oldest first |
| `GET /legos/{id}/{version}` | anyone | One lego |
| `POST /legos[?owner_group=<group>]` | a bearer token | Publishes one lego version (201, `Location`) |
| `GET /docs`, `GET /openapi.json` | anyone | The API, with the lego schemas |

- **Visibility.** A caller sees public legos and their own. Someone else's private lego is 404, the
  same as a missing one. `group` visibility counts as `owner` until groups can share.
- **Which kinds a caller may publish** (`may_add`):

  | Lego | Needs |
  |---|---|
  | tool | Keycloak group `tool-authors` |
  | data, `managed` or `output` | `tool-authors` |
  | data, `external` | any signed-in user |
  | pipeline | any signed-in user |

- **Ids have owners.** The first version of an id claims it for its publisher, or, with
  `?owner_group=<group>`, for a Keycloak group the publisher is in. After that, only that publisher, or
  any member of that group, publishes new versions (403 for anyone else), and the group can't change.
  The platform's legos (the seed's) are owned by the `tool-authors` group. Teams are Keycloak groups:
  add a person to the group and they can publish every lego it owns.
- **An id's kind is fixed** by its first version: a tool stays a tool, external data stays external (422).
- **Versions are immutable.** Publishing an existing `id@version` is 409 for the id's owners; anyone else
  gets the 403 above first, so nobody can probe which versions of a private id exist. Ids are one
  namespace, so a taken id still shows as taken (403). A change is a new version, of at most 32 characters.
- **Checks.** The models validate a lego (422), then `registry.check` runs it against the legos it
  references (a tool's data ids, a pipeline's tools and data), found among those its publisher can see;
  for a **public** lego, among public legos only, so a public lego never depends on a private one. Only
  the problems the new lego brings are reported. Every 422 has FastAPI's shape (`loc`, `msg`, `type`).
- **Reads return what was stored**, without validating it again, so a stricter hydrowf-core later can't
  make old legos unreadable.
- **The owner** is the token's `sub`, whatever the body says.
- **Tokens.** Reads need none, but a bad token is 401 even on a read. 401s carry
  `WWW-Authenticate: Bearer` (`error="invalid_token"` when a token was sent).
- **Bodies** are at most 1 MiB, counted as they stream in (413), whatever `Content-Length` says.

## Run it locally

```bash
uv sync
uv run alembic upgrade head                                   # SQLite: ./registry.db
uv run python -m hydrowf_registry.seed ../hydrowf-platform/registry/legos.yaml
HYDROWF_OIDC_ISSUER=http://localhost:8080/realms/hydrowf HYDROWF_OIDC_AUDIENCE=hydrowf-api \
  uv run uvicorn --factory hydrowf_registry.app:app_from_env --port 8002
```

Or the image (each start runs the migrations; SQLite lives in the `/data` volume):

```bash
docker build -t hydrowf-registry .
docker run --rm -p 8002:8000 -v registry-data:/data --env-file registry.env hydrowf-registry
```

| Variable | What it sets |
|---|---|
| `DATABASE_URL` | The database. Default `sqlite:///registry.db` (`sqlite:////data/registry.db` in the image); a cluster: `postgresql+psycopg://user:password@host/db` |
| `HYDROWF_OIDC_ISSUER`, `HYDROWF_OIDC_AUDIENCE` | Bearer tokens, as for hydrowf-ogc. Without the issuer, reads work and every `POST` is 401 |
| `FORWARDED_ALLOW_IPS` | The ingress addresses whose `X-Forwarded-*` headers uvicorn trusts |

The seed brings the database to the latest schema first (Alembic), then publishes a `legos.yaml` as the
platform's: public, owner `hydrowf`, ids owned by the `tool-authors` group; data first, then tools, then
pipelines. Seeding again skips what is stored; a stored version whose content differs is an error.

## Keycloak

- The `tool-authors` group, and a **group membership mapper** on each calling client that writes the
  `groups` claim into access tokens, with **"Full group path" on**. With it off, Keycloak sends bare
  names, and a subgroup called `tool-authors` under any parent looks like the real group.
- The audience mapper for `hydrowf-api`, as for hydrowf-ogc.

## Schema changes

Alembic, in `src/hydrowf_registry/migrations` (`alembic upgrade head` with `DATABASE_URL` set). Change
`db.py`, then add a revision with `uv run alembic revision --autogenerate -m "<what>"`;
`test_the_migration_matches_the_tables` fails until the two agree.

## Tests

```bash
uv run pytest
```

## Known gaps

- **No deleting or deprecating** a lego version yet, and no handing an id to another owner or group.
- **Names are first come, first served.** Anyone signed in can claim a new id for a pipeline or external
  data; an admin who can reassign ids comes later.
- **No per-user quota** on how many legos one caller publishes.
- **External data URLs are checked by name only.** hydrowf-core refuses localhost, private, link-local
  and numeric hosts, but a public name can still resolve to an internal address: whatever fetches these
  URLs for users must check the address it connects to (SSRF).
- **Migrations run at every start** (Dockerfile). With several replicas on Postgres, run them once
  instead (an init Job) so two starts don't race.
- **Listings sort in Python** after one query; fine for thousands of legos.
- **hydrowf-ogc doesn't read from here yet**: its processes still come from its own config.
