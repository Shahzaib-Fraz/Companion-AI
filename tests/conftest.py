"""
Pytest Fixtures and Configuration
File location: tests/conftest.py
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool

from app.main import app
from app.db.database import get_db, Base
from app.repositories.user_repository import UserRepository
from app.core.security import create_access_token


# ============================================================================
# DATABASE SETUP - In-Memory SQLite for Testing
# ============================================================================

@pytest.fixture(scope="function")
def test_db() -> Session:
    """
    Create an in-memory SQLite database for each test.
    Automatically creates all tables and cleans up after test.
    """
    # Create in-memory database
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    
    # Create all tables
    Base.metadata.create_all(bind=engine)
    
    # Create session
    TestingSessionLocal = sessionmaker(
        autocommit=False, autoflush=False, bind=engine
    )
    db = TestingSessionLocal()
    
    yield db
    
    # Cleanup
    db.close()
    Base.metadata.drop_all(bind=engine)


# ============================================================================
# FASTAPI TEST CLIENT
# ============================================================================

@pytest.fixture
def client(test_db: Session) -> TestClient:
    """
    Create FastAPI TestClient with test database override.
    """
    def override_get_db():
        yield test_db
    
    app.dependency_overrides[get_db] = override_get_db
    
    yield TestClient(app)
    
    # Cleanup dependency overrides
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
    Returns: User object with access_token
    """
    user = UserRepository.create(
        test_db,
        email=test_user_data["email"],
        password=test_user_data["password"],
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
    """
    Return Authorization header with valid token.
    Usage: client.get("/endpoint", headers=auth_headers)
    """
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
    """Mock the chat_service.process_message method."""
    return mocker.patch(
        "app.api.chat_routes.chat_service",  # ✅ CORRECT FOR YOUR STRUCTURE
        return_value={
            "response": "This is a mocked response.",
            "onboarding_step": 0,
            "onboarding_completed": False,
        }
    )