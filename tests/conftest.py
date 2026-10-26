import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db import Base, get_db
from app.main import app
from app.models import Org

# Same server as your dev database, but a different database name
TEST_DB_URL = settings.database_url.rsplit("/", 1)[0] + "/clinicmind_test"

# Safety check: refuse to run if this isn't clearly a test database
assert TEST_DB_URL.endswith("_test"), "Tests must run on a *_test database"

engine = create_engine(TEST_DB_URL)
TestingSession = sessionmaker(bind=engine, autoflush=False)


@pytest.fixture(scope="session", autouse=True)
def create_tables():
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture(autouse=True)
def clean_db():
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE memories, webhook_events, messages, threads, leads, users, orgs "
                "RESTART IDENTITY CASCADE"
            )
        )
    with TestingSession() as session:
        session.add(Org(name="Test Clinic"))
        session.commit()
    yield


@pytest.fixture
def client():
    def override_get_db():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def db():
    with TestingSession() as session:
        yield session