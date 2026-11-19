"""Reading and adding legos, with the rules every caller gets.

    store = Store(engine)
    store.visible(caller_sub)                         # the legos (as stored JSON) a caller may see
    store.get("ngiab-run", "2.1.0", caller_sub)       # one, or None
    store.add(lego, Publisher(sub, groups), owner_group=None)   # the stored lego, or an error below

Visibility: a caller sees public legos and their own. `group` counts as `owner` until groups can share.
Someone else's private lego reads as missing, the same as one that doesn't exist. Reads return the
JSON stored at publish time without validating it again, so a later, stricter hydrowf-core can't make
old rows unreadable.

Ids: the first version of an id claims it (lego_ids) for its publisher, or for `owner_group` when given
(the publisher must be in it), and fixes its kind. New versions come from that publisher, or from any
member of the owner group; the kind can't change. NotAllowed otherwise.

Versions are immutable: an existing (id, version) is Duplicate, told only to the id's owners (anyone else
gets NotAllowed first, so private versions can't be probed).

Checks: the lego is validated against the legos it references (a tool's data ids, a pipeline's tools
and data), looked up among those its publisher can see; a public lego only among public legos, so a
public lego never depends on a private one. Only the problems the new lego brings are Invalid.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Connection, Engine, insert, or_, select, tuple_
from sqlalchemy.exc import DataError, IntegrityError

from hydrowf_core.models import DataLego, DataSource, Lego, Pipeline, ToolLego
from hydrowf_core.models.lego import Visibility
from hydrowf_core.registry import check

from hydrowf_registry.db import lego_ids, legos

LOGGER = logging.getLogger(__name__)
LEGO = TypeAdapter(Lego)


@dataclass(frozen=True)
class Publisher:
    sub: str                              # the Keycloak user id
    groups: frozenset[str] = frozenset()


class Duplicate(Exception):
    """This (id, version) exists: versions are immutable, a change is a new version (409)."""


class NotAllowed(Exception):
    """The id belongs to someone else, or the owner group isn't the publisher's (403)."""


class Invalid(Exception):
    """The lego doesn't fit: its problems, each a sentence (422)."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def version_key(version: str) -> tuple:
    """SemVer order (0.10.0 after 0.9.0); anything unexpected sorts last instead of failing the read."""
    try:
        return (0, tuple(int(part) for part in version.split(".")))
    except ValueError:
        return (1, version)


def references(lego: Lego) -> tuple[set[tuple[str, str]], set[str]]:
    """The (id, version) keys and the data ids `lego` points at: what check() needs besides the lego."""
    keys: set[tuple[str, str]] = set()
    data_ids: set[str] = set()
    if isinstance(lego, ToolLego):
        data_ids = {port.data for port in lego.inputs + lego.outputs if port.data}
    elif isinstance(lego, Pipeline):
        for step in lego.steps:
            keys.add(step.tool.key())
            keys |= {src.data.key() for src in step.inputs.values() if isinstance(src, DataSource)}
    return keys, data_ids


class Store:
    def __init__(self, engine: Engine):
        self.engine = engine

    @staticmethod
    def _visible_to(caller: str | None):
        public = legos.c.visibility == Visibility.PUBLIC.value
        return or_(public, legos.c.owner == caller) if caller else public

    def visible(self, caller: str | None, kind: str | None = None, id: str | None = None) -> list[dict]:
        """The stored legos `caller` may see, by id then version (oldest first)."""
        query = select(legos.c.id, legos.c.version, legos.c.body).where(self._visible_to(caller))
        if kind:
            query = query.where(legos.c.kind == kind)
        if id:
            query = query.where(legos.c.id == id)
        with self.engine.connect() as conn:
            rows = conn.execute(query).all()
        return [row.body for row in sorted(rows, key=lambda row: (row.id, version_key(row.version)))]

    def get(self, id: str, version: str, caller: str | None) -> dict | None:
        query = select(legos.c.body).where(legos.c.id == id, legos.c.version == version, self._visible_to(caller))
        with self.engine.connect() as conn:
            return conn.scalar(query)

    def _referenced(self, conn: Connection, lego: Lego, sub: str) -> list[Lego]:
        """The legos `lego` references that it may use: public ones for a public lego, else also `sub`'s."""
        keys, data_ids = references(lego)
        if not keys and not data_ids:
            return []
        public_only = lego.visibility == Visibility.PUBLIC
        wanted = []
        if keys:
            wanted.append(tuple_(legos.c.id, legos.c.version).in_(list(keys)))
        if data_ids:
            wanted.append(legos.c.id.in_(list(data_ids)) & (legos.c.kind == "data"))
        query = select(legos.c.body).where(or_(*wanted), self._visible_to(None if public_only else sub))
        found = []
        for body in conn.scalars(query):
            try:
                found.append(LEGO.validate_python(body))
            except ValidationError as err:             # stored by an older core: unusable, not fatal
                LOGGER.warning("lego %s@%s no longer validates: %s", body.get("id"), body.get("version"), err)
        return found

    def _claim(self, conn: Connection, lego: Lego, by: Publisher, owner_group: str | None) -> None:
        """Claim `lego.id` for `by` (or `owner_group`) if it is new; else check `by` may add a version."""
        data_kind = lego.data_kind if isinstance(lego, DataLego) else None
        row = conn.execute(select(lego_ids).where(lego_ids.c.id == lego.id)).mappings().first()
        if row is None:
            if owner_group and owner_group not in by.groups:
                raise NotAllowed(f"you are not in the group {owner_group!r}")
            conn.execute(insert(lego_ids).values(id=lego.id, kind=lego.kind, data_kind=data_kind,
                                                 owner=by.sub, owner_group=owner_group))
            return
        allowed = row["owner_group"] in by.groups if row["owner_group"] else row["owner"] == by.sub
        if not allowed:                   # first: a non-owner learns nothing more about the id
            raise NotAllowed(f"{lego.id} belongs to someone else; only its owners publish new versions")
        if owner_group and owner_group != row["owner_group"]:
            raise Invalid([f"{lego.id}'s owner is set by its first version; owner_group can't change"])
        if (row["kind"], row["data_kind"]) != (lego.kind, data_kind):
            was = row["kind"] + (f" ({row['data_kind']})" if row["data_kind"] else "")
            raise Invalid([f"{lego.id} is a {was} lego; a new version can't change its kind"])

    def add(self, lego: Lego, by: Publisher, owner_group: str | None = None) -> Lego:
        lego = lego.model_copy(update={"owner": by.sub})
        key = f"{lego.id}@{lego.version}"
        try:
            with self.engine.begin() as conn:            # one transaction: the claim and the version together
                self._claim(conn, lego, by, owner_group)  # first: a non-owner can't probe which versions exist
                if conn.scalar(select(legos.c.id).where(legos.c.id == lego.id, legos.c.version == lego.version)):
                    raise Duplicate(f"{key} exists; publish a new version")
                referenced = self._referenced(conn, lego, by.sub)
                before = set(check(referenced))
                problems = [p for p in check(referenced + [lego]) if p not in before]
                if problems:
                    raise Invalid(problems)
                conn.execute(insert(legos).values(
                    id=lego.id, version=lego.version, kind=lego.kind, owner=by.sub,
                    visibility=lego.visibility.value, body=lego.model_dump(mode="json")))
        except IntegrityError as err:      # a concurrent publish took this id or version first
            raise Duplicate(f"{key} (or its id) was just taken; try again") from err
        except DataError as err:           # a value the database won't hold; its own words stay in the log
            LOGGER.warning("publishing %s: %s", key, err.orig)
            raise Invalid(["a value is too long for the registry's database"]) from err
        return lego
