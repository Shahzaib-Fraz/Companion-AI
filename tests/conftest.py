"""
Pytest fixtures and configuration.
File location: tests/conftest.py

This merges the original TestClient-based fixtures (test_db, client,
test_user, auth_headers, mock_chat_service, ...) with the lighter
service/repository-level fixtures (db_session, make_user) used by the
newer test_reminder_repository.py / test_context_builder.py / etc. Both
styles share the same underlying database setup - db_session is just an
alias for test_db, not a second thing to keep in sync.

Fixes applied here, and why:

1. Environment variables are seeded BEFORE `from app.main import app`.
   Without this, importing app.main (which happens at collection time,
   before any fixture runs) transitively constructs several real
   service singletons - WhatsAppService() raises ValueError immediately
   if WHATSAPP_APP_SECRET is empty (that's a deliberate P0-9 fix, not a
   bug), and Settings() / LLMService() expect GROQ_API_KEY, JWT_SECRET,
   etc. Without dummy values already in the environment, pytest can fail
   to even COLLECT tests, before a single test runs. This is the
   specific failure mode the review meant by "tests import app.main
   before overriding [state]" - it undersells it by naming only the
   database, but the same problem applies to every module-level
   singleton main.py pulls in.

2. mock_chat_service now patches `chat_service.process_message`
   specifically, as an explicit AsyncMock - not the whole `chat_service`
   object with `return_value=...` set on it. The original version
   replaced the entire chat_service object with a plain MagicMock;
   chat_routes.py calls `await chat_service.process_message(...)`, and
   awaiting a MagicMock's auto-generated child attribute raises
   `TypeError: object MagicMock can't be used in 'await' expression`.
   That would have made every test using this fixture fail with an
   unhandled 500, not the specific responses each test asserts - this
   is exactly the review's "configures the object's return_value rather
   than process_message" finding.

3. test_user now hashes the password before calling
   UserRepository.create(). The original passed `password=...` - the
   real method signature is `create(db, email, password_hash, phone=None)`,
   so this would have raised TypeError immediately (wrong keyword name),
   breaking every test that depends on test_user/auth_headers (most of
   test_chat.py, several of test_auth.py).

4. An autouse fixture resets the rate limiter's counters before every
   test. slowapi's Limiter is a process-global singleton
   (app.core.limiter.limiter) - its hit-counts are NOT reset between
   tests automatically. Without this, rate-limit tests are order- and
   suite-composition-dependent: running test_auth.py's several
   signup-triggering tests (signup is limited to 5/minute) before
   test_chat.py, or even just running test_chat.py's two rate-limit
   tests back to back, means a later test starts with an
   already-partially-spent budget from an earlier one.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("GROQ_API_KEY", "test-groq-key")
os.environ.setdefault("GROQ_MODEL", "test-model")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("WHATSAPP_APP_SECRET", "test-whatsapp-app-secret")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test-verify-token")

import pytest
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool

from app.main import app
from app.db.database import get_db, Base
from app.repositories.user_repository import UserRepository  # also registers every ORM class on Base.metadata, via its own `from app.models.database import ...`
from app.core.security import create_access_token, hash_password
from app.core.limiter import limiter


# ============================================================================
# RATE LIMITER ISOLATION
# ============================================================================

@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """
    Runs before EVERY test, not just the ones that explicitly test rate
    limiting - any test hitting /auth/signup, /auth/login, or
    /chat/message contributes to the same global counters otherwise.
    """
    try:
        limiter.reset()
    except Exception:
        # Fallback for slowapi versions that don't expose .reset() directly.
        try:
            limiter._storage.reset()
        except Exception:
            pass
    yield


# ============================================================================
# DATABASE SETUP - In-Memory SQLite for Testing
# ============================================================================

@pytest.fixture(scope="function")
def test_db() -> Session:
    """
    Create an in-memory SQLite database for each test.
    Automatically creates all tables and cleans up after test.

    KNOWN LIMITATION: SQLite does not support SELECT ... FOR UPDATE SKIP
    LOCKED at all. app/repositories/reminder_repository.py detects this
    (_supports_skip_locked) and falls back to a plain, unlocked query on
    SQLite, real locking only on Postgres. Tests here can prove the
    reminder claim/finalize STATE MACHINE is correct; they prove nothing
    about the actual cross-worker concurrency guarantee, which only
    exists on Postgres and needs a real multi-process test against a
    real instance to prove.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    Base.metadata.create_all(bind=engine)

    TestingSessionLocal = sessionmaker(
        autocommit=False, autoflush=False, bind=engine
    )
    db = TestingSessionLocal()

    yield db

    db.close()
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db_session(test_db: Session) -> Session:
    """
    Alias for test_db, for the lighter service/repository-level tests
    (test_reminder_repository.py, test_context_builder.py, etc.) that
    don't need a TestClient - same underlying database either way, not
    a second one to keep in sync.
    """
    return test_db


@pytest.fixture()
def make_user(db_session: Session):
    """Factory fixture: make_user() or make_user(email="x@y.com", ...)."""
    from app.models.database import User

    def _make(email: str = "user@example.com", **kwargs):
        user = User(email=email, password_hash="not-a-real-hash", is_active=True, **kwargs)
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)
        return user

    return _make


# ============================================================================
# FASTAPI TEST CLIENT
# ============================================================================

@pytest.fixture
def client(test_db: Session) -> TestClient:
    """Create FastAPI TestClient with test database override."""
    def override_get_db():
        yield test_db

    app.dependency_overrides[get_db] = override_get_db

    yield TestClient(app)

    app.dependency_overrides.clear()


# ============================================================================
# TEST USER FIXTURES
# ============================================================================

@pytest.fixture
def test_user_data():
    """Test user credentials."""
    return {
        "email": "testuser@example.com",
        "password": "SecurePassword123",
    }


@pytest.fixture
def test_user(test_db: Session, test_user_data):
    """
    Create a test user in the database.
    Returns: dict with user, token, and credentials.

    FIX: hashes the password before storing it. The real
    UserRepository.create() takes password_hash (already hashed) - it
    does not hash on your behalf, unlike the /auth/signup route, which
    calls hash_password() itself before calling this. Bypassing that
    here (as the original `password=...` kwarg attempted to) either
    raises TypeError (wrong kwarg name) or, if "fixed" by just renaming
    the kwarg without hashing, stores the raw plaintext password where a
    bcrypt hash is expected - breaking any test that tries to actually
    log in as this user via /auth/login.
    """
    user = UserRepository.create(
        test_db,
        email=test_user_data["email"],
        password_hash=hash_password(test_user_data["password"]),
    )
    UserRepository.get_or_create_profile(test_db, user.id)

    token = create_access_token(user.id)

    return {
        "user": user,
        "email": test_user_data["email"],
        "password": test_user_data["password"],
        "access_token": token,
        "user_id": user.id,
    }


@pytest.fixture
def auth_headers(test_user):
    """Return Authorization header with valid token."""
    return {"Authorization": f"Bearer {test_user['access_token']}"}


@pytest.fixture
def invalid_auth_headers():
    """Return Authorization header with invalid token."""
    return {"Authorization": "Bearer invalid.token.here"}


# ============================================================================
# CHAT MESSAGE FIXTURES
# ============================================================================

@pytest.fixture
def valid_chat_message():
    """Valid chat message."""
    return {
        "message": "Hello, how are you?",
        "channel": "web",
        "request_id": "req-123",
    }


@pytest.fixture
def chat_message_whatsapp():
    """Valid WhatsApp chat message."""
    return {
        "message": "Hi there!",
        "channel": "whatsapp",
        "request_id": "req-whatsapp-123",
    }


# ============================================================================
# UTILITY FIXTURES
# ============================================================================

@pytest.fixture
def mock_chat_service(mocker):
    """
    Mock chat_service.process_message specifically, as an AsyncMock.

    FIX: the original patched the whole `chat_service` object with
    `return_value=...`. chat_routes.py calls
    `await chat_service.process_message(...)` - patching the object
    itself (rather than the specific async method) replaces it with a
    plain MagicMock, and `chat_service.process_message` on a MagicMock
    is an auto-generated child MagicMock; calling and awaiting it raises
    TypeError, not returns your configured value. `new_callable=AsyncMock`
    is set explicitly (rather than relying on autospec-detection) so
    this works the same regardless of mock/pytest-mock version.
    """
    return mocker.patch(
        "app.api.chat_routes.chat_service.process_message",
        new_callable=AsyncMock,
        return_value={
            "response": "This is a mocked response.",
            "reminder_set": False,
            "onboarding_step": 0,
            "onboarding_completed": False,
        },
    )