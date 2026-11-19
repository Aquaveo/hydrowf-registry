"""Where legos are stored: `lego_ids` (who owns each id, and its kind) and `legos` (one row per version).

    engine = engine_from_env()       # DATABASE_URL; SQLite in the working directory without it

SQLite for local development, Postgres on a cluster (DATABASE_URL=postgresql+psycopg://...). The
schema is created and changed by Alembic (migrations/, `alembic upgrade head`); `create_schema` is for
tests only. Rows are never updated: a lego version is immutable, and so is who owns an id.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import JSON, Column, DateTime, Engine, ForeignKey, MetaData, String, Table, create_engine

DEFAULT_URL = "sqlite:///registry.db"
MAX_VERSION = 32                         # characters: core's SemVer max_length, which the models enforce


def now() -> datetime:
    return datetime.now(timezone.utc)


metadata = MetaData()

# An id belongs to whoever published its first version: that user alone, or the members of owner_group
# when it is set. The first version also fixes its kind (and data_kind) for good. The primary key makes
# the database decide who claimed an id first.
lego_ids = Table(
    "lego_ids", metadata,
    Column("id", String(63), primary_key=True),          # LegoId: at most 63 characters
    Column("kind", String(16), nullable=False),          # tool, data, pipeline
    Column("data_kind", String(16)),                     # external, managed, output (data only)
    Column("owner", String(255), nullable=False),        # the Keycloak sub that published the first version
    Column("owner_group", String(255)),                  # a Keycloak group whose members publish new versions
    Column("created_at", DateTime(timezone=True), nullable=False, default=now),
)

legos = Table(
    "legos", metadata,
    Column("id", String(63), ForeignKey("lego_ids.id"), primary_key=True),
    Column("version", String(MAX_VERSION), primary_key=True),
    Column("kind", String(16), nullable=False),
    Column("owner", String(255), nullable=False),        # the Keycloak sub that published this version
    Column("visibility", String(16), nullable=False),    # owner, group, public
    Column("created_at", DateTime(timezone=True), nullable=False, default=now),
    Column("body", JSON, nullable=False),                # the lego, as hydrowf_core.models dumps it
)


def engine_from_env(environ: dict | None = None) -> Engine:
    environ = os.environ if environ is None else environ
    return create_engine(environ.get("DATABASE_URL") or DEFAULT_URL)


def create_schema(engine: Engine) -> None:
    """The tables, for tests. Real databases get them from Alembic (`alembic upgrade head`)."""
    metadata.create_all(engine)
