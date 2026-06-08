import sys
import os
from logging.config import fileConfig

from sqlalchemy import create_engine, pool
from alembic import context

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from config import settings
from db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Use DATABASE_URL directly from settings to avoid ConfigParser interpolation
# issues with percent-encoded characters (e.g. %21 in passwords).
_DB_URL = settings.DATABASE_URL


def run_migrations_offline() -> None:
    context.configure(
        url=_DB_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(_DB_URL, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
