"""
User Routes Tests
File location: tests/test_user_routes.py

Closes a real gap: validate_timezone() itself was extensively tested
(test_timezone_validation.py), and user_routes.py's P0-11 fix calling
it was verified by direct code inspection every time this file was
touched - but nothing actually drove a request through
PUT /users/profile via TestClient to prove the HTTP-level behavior the
Production Approval Checklist item ("Stored timezone is validated and
authoritative for all reminder flows") actually asks for.
"""
import pytest
from fastapi.testclient import TestClient


class TestGetProfile:
    def test_get_profile_requires_no_auth_but_reports_unauthenticated(self, client: TestClient):
        response = client.get("/users/profile")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "unauthenticated"

    def test_get_profile_returns_401_for_invalid_token(self, client: TestClient, invalid_auth_headers):
        response = client.get("/users/profile", headers=invalid_auth_headers)
        assert response.status_code == 401

    def test_get_profile_success(self, client: TestClient, auth_headers):
        response = client.get("/users/profile", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert "timezone" in data["profile"]


class TestUpdateProfileTimezone:
    """The specific behavior the approval checklist item is asking about."""

    def test_update_profile_rejects_invalid_timezone(self, client: TestClient, auth_headers):
        response = client.put(
            "/users/profile",
            json={"timezone": "Not/A/Real/Zone"},
            headers=auth_headers,
        )
        assert response.status_code == 400

    def test_update_profile_rejects_garbage_string_as_timezone(self, client: TestClient, auth_headers):
        response = client.put(
            "/users/profile",
            json={"timezone": "banana"},
            headers=auth_headers,
        )
        assert response.status_code == 400

    def test_update_profile_accepts_a_valid_iana_timezone(self, client: TestClient, auth_headers):
        response = client.put(
            "/users/profile",
            json={"timezone": "Asia/Karachi"},
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["profile"]["timezone"] == "Asia/Karachi"

    def test_update_profile_persists_the_validated_timezone(self, client: TestClient, auth_headers):
        client.put("/users/profile", json={"timezone": "America/New_York"}, headers=auth_headers)

        response = client.get("/users/profile", headers=auth_headers)

        assert response.json()["profile"]["timezone"] == "America/New_York"

    def test_update_profile_requires_auth(self, client: TestClient):
        response = client.put("/users/profile", json={"timezone": "UTC"})
        assert response.status_code == 401

    def test_update_profile_rejects_unknown_fields(self, client: TestClient, auth_headers):
        response = client.put(
            "/users/profile",
            json={"not_a_real_field": "x"},
            headers=auth_headers,
        )
        assert response.status_code == 400

    def test_update_profile_rejects_empty_body(self, client: TestClient, auth_headers):
        response = client.put("/users/profile", json={}, headers=auth_headers)
        assert response.status_code == 400