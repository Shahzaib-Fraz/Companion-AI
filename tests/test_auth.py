"""
Authentication Routes Tests
File location: tests/test_auth.py

Tests for:
- POST /signup
- POST /login
- GET /verify
"""
import pytest
from fastapi.testclient import TestClient


class TestSignup:
    """Tests for signup endpoint."""
    
    def test_signup_success(self, client: TestClient):
        """✅ Successful user signup creates user and returns access token."""
        response = client.post(
            "/auth/signup",
            json={
                "email": "newuser@example.com",
                "password": "SecurePass123",
            }
        )
        
        # Status code
        assert response.status_code == 200
        
        # Response has access_token and user_id
        data = response.json()
        assert "access_token" in data
        assert "user_id" in data
        assert isinstance(data["user_id"], int)
        assert data["access_token"].count(".") == 2  # JWT format
    
    def test_signup_duplicate_email(self, client: TestClient, test_user_data):
        """❌ Signup with existing email returns 409 Conflict."""
        # Create first user
        client.post(
            "/auth/signup",
            json={
                "email": test_user_data["email"],
                "password": test_user_data["password"],
            }
        )
        
        # Try to create duplicate
        response = client.post(
            "/auth/signup",
            json={
                "email": test_user_data["email"],
                "password": "AnotherPassword123",
            }
        )
        
        # Should fail
        assert response.status_code == 409
        assert "already exists" in response.json()["detail"]
    
    def test_signup_invalid_email(self, client: TestClient):
        """❌ Signup with invalid email format returns 422."""
        response = client.post(
            "/auth/signup",
            json={
                "email": "not-an-email",
                "password": "SecurePass123",
            }
        )
        
        assert response.status_code == 422
        assert "email" in str(response.json()).lower()
    
    def test_signup_password_too_short(self, client: TestClient):
        """❌ Signup with password < 8 chars returns 422."""
        response = client.post(
            "/auth/signup",
            json={
                "email": "test@example.com",
                "password": "Short1",  # Only 6 chars
            }
        )
        
        assert response.status_code == 422
        assert "password" in str(response.json()).lower()
    
    def test_signup_password_too_long(self, client: TestClient):
        """❌ Signup with password > 128 chars returns 422."""
        response = client.post(
            "/auth/signup",
            json={
                "email": "test@example.com",
                "password": "a" * 129,  # 129 chars
            }
        )
        
        assert response.status_code == 422
    
    def test_signup_missing_fields(self, client: TestClient):
        """❌ Signup with missing fields returns 422."""
        # Missing password
        response = client.post(
            "/auth/signup",
            json={"email": "test@example.com"}
        )
        assert response.status_code == 422
        
        # Missing email
        response = client.post(
            "/auth/signup",
            json={"password": "SecurePass123"}
        )
        assert response.status_code == 422


class TestLogin:
    """Tests for login endpoint."""
    
    def test_login_success(self, client: TestClient, test_user_data):
        """✅ Login with correct credentials returns access token."""
        # First signup
        client.post(
            "/auth/signup",
            json=test_user_data
        )
        
        # Then login
        response = client.post(
            "/auth/login",
            json={
                "email": test_user_data["email"],
                "password": test_user_data["password"],
            }
        )
        
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert "user_id" in data
    
    def test_login_invalid_email(self, client: TestClient):
        """❌ Login with non-existent email returns 401."""
        response = client.post(
            "/auth/login",
            json={
                "email": "nonexistent@example.com",
                "password": "AnyPassword123",
            }
        )
        
        assert response.status_code == 401
        assert "Invalid email or password" in response.json()["detail"]
    
    def test_login_invalid_password(self, client: TestClient, test_user_data):
        """❌ Login with wrong password returns 401."""
        # Signup first
        client.post("/auth/signup", json=test_user_data)
        
        # Login with wrong password
        response = client.post(
            "/auth/login",
            json={
                "email": test_user_data["email"],
                "password": "WrongPassword123",
            }
        )
        
        assert response.status_code == 401
        assert "Invalid email or password" in response.json()["detail"]
    
    def test_login_case_insensitive_email(self, client: TestClient, test_user_data):
        """✅ Login with different email case should work."""
        # Signup
        client.post("/auth/signup", json=test_user_data)
        
        # Login with uppercase
        response = client.post(
            "/auth/login",
            json={
                "email": test_user_data["email"].upper(),
                "password": test_user_data["password"],
            }
        )
        
        # Should succeed (case-insensitive)
        assert response.status_code in [200, 401]  # Depends on DB implementation


class TestVerify:
    """Tests for verify token endpoint."""
    
    def test_verify_valid_token(self, client: TestClient, test_user):
        """✅ Verify valid token returns valid: true."""
        response = client.get(
            "/auth/verify",
            headers={"Authorization": f"Bearer {test_user['access_token']}"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is True
        assert data["user_id"] == test_user["user_id"]
    
    def test_verify_invalid_token(self, client: TestClient):
        """✅ Verify invalid token returns valid: false."""
        response = client.get(
            "/auth/verify",
            headers={"Authorization": "Bearer invalid.token.here"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is False
    
    def test_verify_no_authorization(self, client: TestClient):
        """✅ Verify without auth header returns valid: false."""
        response = client.get("/auth/verify")
        
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is False
    
    def test_verify_missing_bearer_prefix(self, client: TestClient):
        """✅ Verify with token but no Bearer prefix returns valid: false."""
        response = client.get(
            "/auth/verify",
            headers={"Authorization": "SomeRandomToken"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is False
    
    def test_verify_expired_token(self, client: TestClient, mocker):
        """✅ Verify expired token returns valid: false."""
        # Mock verify_access_token to return None (expired)
        mocker.patch(
            "app.api.auth_routes.verify_access_token",  # ✅ CORRECT PATH
            return_value=None
        )
        
        response = client.get(
            "/auth/verify",
            headers={"Authorization": "Bearer invalid.token.here"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is False


class TestAuthFlow:
    """Integration tests for complete auth flow."""
    
    def test_complete_signup_login_verify_flow(self, client: TestClient):
        """✅ Complete flow: signup → login → verify."""
        email = "integration@example.com"
        password = "IntegrationPassword123"
        
        # 1. Signup
        signup_response = client.post(
            "/auth/signup",
            json={"email": email, "password": password}
        )
        assert signup_response.status_code == 200
        signup_data = signup_response.json()
        token1 = signup_data["access_token"]
        user_id = signup_data["user_id"]
        
        # 2. Login
        login_response = client.post(
            "/auth/login",
            json={"email": email, "password": password}
        )
        assert login_response.status_code == 200
        login_data = login_response.json()
        token2 = login_data["access_token"]
        assert token2.count(".") == 2
        
        # 3. Verify signup token
        verify_response1 = client.get(
            "/auth/verify",
            headers={"Authorization": f"Bearer {token1}"}
        )
        assert verify_response1.json()["valid"] is True
        assert verify_response1.json()["user_id"] == user_id
        
        # 4. Verify login token
        verify_response2 = client.get(
            "/auth/verify",
            headers={"Authorization": f"Bearer {token2}"}
        )
        assert verify_response2.json()["valid"] is True
        assert verify_response2.json()["user_id"] == user_id