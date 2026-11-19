"""Load a legos.yaml (e.g. hydrowf-platform's registry/legos.yaml) into the registry, as the platform's.

    python -m hydrowf_registry.seed legos.yaml

Brings the database to the latest schema first (Alembic), so seeding a fresh database is safe before the
service ever starts. Every lego is published public, by the platform (owner `hydrowf`), with its id owned
by the tool-authors group: tool authors publish the next versions through the API.

Data legos go first, then tools, then pipelines, since each is checked against what is already stored.
Seeding again is safe: a version stored with the same content is skipped; one stored with different
content is an error (versions are immutable: bump the version in the file instead).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine

from hydrowf_core.models import LEGOS, DataLego, Lego, ToolLego
from hydrowf_core.models.lego import Visibility

from hydrowf_registry.db import engine_from_env
from hydrowf_registry.store import LEGO, Duplicate, NotAllowed, Publisher, Store

PLATFORM = Publisher("hydrowf", frozenset({"tool-authors"}))    # not a Keycloak user: the platform itself
PLATFORM_GROUP = "tool-authors"


def migrate(engine: Engine) -> None:
    """`alembic upgrade head` on `engine`, without needing alembic.ini in the working directory."""
    config = Config()
    config.set_main_option("script_location", "hydrowf_registry:migrations")
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def order(lego: Lego) -> int:
    return 0 if isinstance(lego, DataLego) else 1 if isinstance(lego, ToolLego) else 2


def same(stored: dict | None, lego: Lego) -> bool:
    """Whether a stored lego is `lego`, as the current core reads both: a field a newer core added with a
    default doesn't make an unchanged lego look changed."""
    if stored is None:
        return False
    try:
        return LEGO.validate_python(stored) == lego
    except ValueError:                     # stored by a core whose models no longer read it
        return False


def seed(store: Store, legos: list[Lego]) -> tuple[list[str], list[str]]:
    """(added, skipped) as id@version; ValueError for a stored version whose content differs."""
    added, skipped = [], []
    for lego in sorted(legos, key=order):
        public = lego.model_copy(update={"visibility": Visibility.PUBLIC, "owner": PLATFORM.sub})
        key = f"{lego.id}@{lego.version}"
        try:
            store.add(public, PLATFORM, owner_group=PLATFORM_GROUP)
            added.append(key)
        except NotAllowed:
            raise ValueError(f"{lego.id} is taken by another owner; the platform can't publish it") from None
        except Duplicate:
            if not same(store.get(lego.id, lego.version, PLATFORM.sub), public):
                raise ValueError(f"{key} is stored with different content; bump its version") from None
            skipped.append(key)
    return added, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("file", type=Path)
    args = parser.parse_args(argv)
    engine = engine_from_env()
    migrate(engine)
    added, skipped = seed(Store(engine), LEGOS.validate_python(yaml.safe_load(args.file.read_text())))
    print(f"added {len(added)}: {' '.join(added) or '-'}")
    print(f"already there {len(skipped)}: {' '.join(skipped) or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
