import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import crud, database, rate_limit
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models import CustomerTier, UserRole


@pytest.fixture(autouse=True)
def _disable_rate_limiting(monkeypatch):
    """Phase 7 rate limiting is process-global state; keep it off for the bulk
    of the suite so tests aren't coupled to its counters. The dedicated
    rate-limit tests re-enable it explicitly and reset the store."""
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    rate_limit.reset()
    yield
    rate_limit.reset()


@pytest.fixture()
def db_session(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    # the ticket-creation background task opens its own session via
    # database.SessionLocal() (the request's session is already closed by
    # the time background tasks run) — point it at the same in-memory
    # engine so it can see data committed within a test.
    monkeypatch.setattr(database, "SessionLocal", TestingSessionLocal)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def client(db_session):
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _auth_header(client, email, password):
    resp = client.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def admin_user(db_session):
    return crud.create_user(
        db_session,
        email="admin@example.com",
        password="adminpass123",
        full_name="Ada Min",
        role=UserRole.ADMIN,
    )


@pytest.fixture()
def agent_user(db_session):
    return crud.create_user(
        db_session,
        email="agent@example.com",
        password="agentpass123",
        full_name="Alex Agent",
        role=UserRole.AGENT,
    )


@pytest.fixture()
def customer_user(db_session):
    return crud.create_user(
        db_session,
        email="customer@example.com",
        password="customerpass123",
        full_name="Cara Customer",
        role=UserRole.CUSTOMER,
        tier=CustomerTier.STANDARD,
    )


@pytest.fixture()
def other_customer_user(db_session):
    return crud.create_user(
        db_session,
        email="other@example.com",
        password="otherpass123",
        full_name="Otto Other",
        role=UserRole.CUSTOMER,
        tier=CustomerTier.FREE,
    )


@pytest.fixture()
def admin_headers(client, admin_user):
    return _auth_header(client, "admin@example.com", "adminpass123")


@pytest.fixture()
def agent_headers(client, agent_user):
    return _auth_header(client, "agent@example.com", "agentpass123")


@pytest.fixture()
def customer_headers(client, customer_user):
    return _auth_header(client, "customer@example.com", "customerpass123")


@pytest.fixture()
def other_customer_headers(client, other_customer_user):
    return _auth_header(client, "other@example.com", "otherpass123")
