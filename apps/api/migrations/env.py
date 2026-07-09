from logging.config import fileConfig
from app.config import settings

from sqlalchemy import engine_from_config, pool
from alembic import context
from app.db.base import Base
# Import every mapped model via __all__ so a model added to app/models/
# but forgotten in __all__ can never silently vanish from autogenerate
# (this is how ShareLink was missed previously).
import app.models as _models  # noqa: F401

# Tables that exist in the database (via a hand-written migration) but are
# intentionally NOT mapped as an ORM model — e.g. purely write-only audit /
# log tables. Autogenerate must ignore these or it will propose dropping
# them on every `alembic revision --autogenerate` run.
_UNMAPPED_TABLES = {"webhook_delivery_logs"}


def include_object(object, name, type_, reflected, compare_to):
    if type_ == "table" and name in _UNMAPPED_TABLES:
        return False
    return True


config = context.config
config.set_main_option(
    "sqlalchemy.url",
    settings.DATABASE_URL_SYNC # now always derived from DATABASE_URL — never stale
)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

 # noqa: F401
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Build connect_args for SSL on production
    connect_args = {}
    if settings.ENVIRONMENT == "production":
        connect_args["connect_args"] = {"sslmode": "require"}

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        **connect_args,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()