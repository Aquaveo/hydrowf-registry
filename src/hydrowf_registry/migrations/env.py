"""Alembic's environment: the database is DATABASE_URL's (hydrowf_registry.db), the schema db.metadata."""
from alembic import context

from hydrowf_registry.db import engine_from_env, metadata

if context.config.config_file_name:
    from logging.config import fileConfig
    fileConfig(context.config.config_file_name, disable_existing_loggers=False)   # keep the app's loggers

connectable = context.config.attributes.get("connection") or engine_from_env()


def run(connection) -> None:
    context.configure(connection=connection, target_metadata=metadata)
    with context.begin_transaction():
        context.run_migrations()


if hasattr(connectable, "connect"):          # an Engine
    with connectable.connect() as connection:
        run(connection)
else:                                        # a Connection a caller (the tests) passed in
    run(connectable)
