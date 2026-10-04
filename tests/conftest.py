import os
import tempfile

# Must run before any app import so tests never touch the real data/ folder.
os.environ.setdefault("ORION_DATA_DIR", tempfile.mkdtemp(prefix="orion-test-"))
os.environ["ORION_AI_PROVIDER"] = "echo"  # tests never launch a real model
os.environ["ORION_AUTO_VACUUM"] = "0"  # no background VACUUM threads during tests

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.backend.database import Base, get_db
from app.backend.main import app
from app.backend import models


@pytest.fixture(scope="session")
def test_engine(tmp_path_factory):
    database_path = tmp_path_factory.mktemp("db") / "test.db"

    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )

    Base.metadata.create_all(bind=engine)

    yield engine

    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture(scope="session")
def testing_session_local(test_engine):
    return sessionmaker(
        bind=test_engine,
        autoflush=False,
        autocommit=False,
    )


@pytest.fixture(scope="session", autouse=True)
def override_database(testing_session_local):
    def override_get_db():
        db = testing_session_local()

        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    yield

    app.dependency_overrides.clear()


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client
