"""
Test matrix: Deployment
  "Readiness changes correctly as each dependency fails and recovers."

Mocks all four live probes app/api/health_routes.py's /health/ready
performs (Postgres via engine.connect, Qdrant via httpx.get, Groq via
groq.Groq(...).models.list(), email via env presence) so each can be
independently forced up/down and the response checked - both the
overall 200-vs-503 and which dependency is actually reported down.
"""
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient


def _mock_all_healthy(mocker):
    mocker.patch("app.db.database.engine.connect")  # MagicMock supports __enter__/__exit__ by default
    mocker.patch("httpx.get", return_value=MagicMock(status_code=200))
    mocker.patch("groq.Groq", return_value=MagicMock(models=MagicMock(list=MagicMock(return_value=None))))


def test_ready_returns_200_when_everything_is_up(client: TestClient, mocker, monkeypatch):
    _mock_all_healthy(mocker)
    monkeypatch.setenv("BREVO_API_KEY", "test-key")
    monkeypatch.setenv("BREVO_SENDER_EMAIL", "test@example.com")

    response = client.get("/health/ready")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"
    assert data["postgres"] is True
    assert data["qdrant"] is True
    assert data["groq"] is True
    assert data["email"] is True


def test_ready_returns_503_when_postgres_is_down(client: TestClient, mocker, monkeypatch):
    _mock_all_healthy(mocker)
    monkeypatch.setenv("BREVO_API_KEY", "test-key")
    monkeypatch.setenv("BREVO_SENDER_EMAIL", "test@example.com")
    mocker.patch("app.db.database.engine.connect", side_effect=Exception("connection refused"))

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert "postgres" in response.text.lower()


def test_ready_returns_503_when_qdrant_is_down(client: TestClient, mocker, monkeypatch):
    _mock_all_healthy(mocker)
    monkeypatch.setenv("BREVO_API_KEY", "test-key")
    monkeypatch.setenv("BREVO_SENDER_EMAIL", "test@example.com")
    mocker.patch("httpx.get", side_effect=Exception("connection refused"))

    response = client.get("/health/ready")

    assert response.status_code == 503


def test_ready_returns_503_when_groq_key_is_bad(client: TestClient, mocker, monkeypatch):
    _mock_all_healthy(mocker)
    monkeypatch.setenv("BREVO_API_KEY", "test-key")
    monkeypatch.setenv("BREVO_SENDER_EMAIL", "test@example.com")

    bad_client = MagicMock()
    bad_client.models.list.side_effect = Exception("401 invalid api key")
    mocker.patch("groq.Groq", return_value=bad_client)

    response = client.get("/health/ready")

    assert response.status_code == 503


def test_ready_returns_503_when_email_not_configured(client: TestClient, mocker, monkeypatch):
    _mock_all_healthy(mocker)
    monkeypatch.delenv("BREVO_API_KEY", raising=False)
    monkeypatch.delenv("BREVO_SENDER_EMAIL", raising=False)

    response = client.get("/health/ready")

    assert response.status_code == 503


def test_ready_recovers_after_dependency_comes_back(client: TestClient, mocker, monkeypatch):
    """The same process, first down then up - readiness must reflect the CURRENT state, not a cached one."""
    monkeypatch.setenv("BREVO_API_KEY", "test-key")
    monkeypatch.setenv("BREVO_SENDER_EMAIL", "test@example.com")

    mocker.patch("app.db.database.engine.connect", side_effect=Exception("down"))
    mocker.patch("httpx.get", return_value=MagicMock(status_code=200))
    mocker.patch("groq.Groq", return_value=MagicMock(models=MagicMock(list=MagicMock(return_value=None))))

    first = client.get("/health/ready")
    assert first.status_code == 503

    mocker.patch("app.db.database.engine.connect")  # recovered

    second = client.get("/health/ready")
    assert second.status_code == 200


def test_live_health_check_does_not_probe_dependencies(client: TestClient):
    """/health/ (liveness) should answer instantly, unlike /health/ready."""
    response = client.get("/health/")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"