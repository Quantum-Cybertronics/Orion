import os
import stat
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from app.backend.config import SESSION_SECRET_FILENAME, load_session_secret
from app.backend.database import Base
from app.backend.migrations_runner import run_migrations


@pytest.fixture
def no_env_secret(monkeypatch):
    monkeypatch.delenv("ORION_SESSION_SECRET", raising=False)


def test_secret_is_generated_and_persisted(tmp_path, no_env_secret):
    first = load_session_secret(tmp_path)
    second = load_session_secret(tmp_path)

    assert len(first) >= 64
    assert first == second
    assert (tmp_path / SESSION_SECRET_FILENAME).read_text() == first


def test_secret_differs_between_installs(tmp_path, no_env_secret):
    a = load_session_secret(tmp_path / "a") if (tmp_path / "a").mkdir() is None else None
    b = load_session_secret(tmp_path / "b") if (tmp_path / "b").mkdir() is None else None

    assert a != b


def test_env_secret_overrides_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ORION_SESSION_SECRET", "from-env")

    assert load_session_secret(tmp_path) == "from-env"
    assert not (tmp_path / SESSION_SECRET_FILENAME).exists()


def test_old_hardcoded_secret_is_gone():
    source = open("app/backend/auth/session.py", encoding="utf-8").read()

    assert "development-only" not in source


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_secret_file_is_owner_only(tmp_path, no_env_secret):
    load_session_secret(tmp_path)
    mode = stat.S_IMODE(os.stat(tmp_path / SESSION_SECRET_FILENAME).st_mode)

    assert mode == 0o600


def test_migrations_create_schema_on_empty_database(tmp_path):
    url = f"sqlite:///{tmp_path / 'fresh.db'}"

    run_migrations(url)

    engine = create_engine(url)
    tables = set(inspect(engine).get_table_names())
    engine.dispose()

    assert {"users", "conversations", "messages", "alembic_version"} <= tables


def test_migrations_are_idempotent(tmp_path):
    url = f"sqlite:///{tmp_path / 'twice.db'}"

    run_migrations(url)
    run_migrations(url)


def test_legacy_database_is_stamped_not_recreated(tmp_path):
    url = f"sqlite:///{tmp_path / 'legacy.db'}"

    engine = create_engine(url)
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, username, password_hash, created_at) "
                "VALUES ('u1', 'keepme', 'x', '2026-01-01')"
            )
        )
    engine.dispose()

    run_migrations(url)

    engine = create_engine(url)
    with engine.connect() as connection:
        usernames = connection.execute(text("SELECT username FROM users")).scalars().all()
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    engine.dispose()

    assert usernames == ["keepme"]
    assert len(version) == 1
