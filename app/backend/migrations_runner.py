from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app.backend.config import BASE_DIR
from app.backend.database import DATABASE_URL

APP_TABLES = {"users", "conversations", "messages"}


def run_migrations(database_url: str = DATABASE_URL) -> None:
    """Bring the database schema up to date. Safe to call on every startup."""
    config = Config(str(BASE_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BASE_DIR / "migrations"))
    config.attributes["database_url"] = database_url

    engine = create_engine(database_url)

    try:
        existing_tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    if "alembic_version" not in existing_tables and existing_tables & APP_TABLES:
        # Database predates Alembic tracking (created via create_all).
        # Assume it matches the current schema and start tracking from here.
        command.stamp(config, "head")
    else:
        command.upgrade(config, "head")
