"""
Test matrix: Auth/API
  "Service failures map to truthful HTTP results and request_id
  idempotency is either implemented or removed."

Route-level (through TestClient), closing a gap explicitly flagged
earlier: test_chat_service_errors.py proves chat_service.process_message()
sets the right error_code; these prove chat_routes.py actually maps
that error_code to the right HTTP status, end to end.
"""
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


def _payload(**overrides):
    base = {"message": "hello", "channel": "web", "request_id": "req-1"}
    base.update(overrides)
    return base


def test_invalid_message_error_code_maps_to_400(client: TestClient, auth_headers, mocker):
    mocker.patch(
        "app.api.chat_routes.chat_service.process_message",
        new_callable=AsyncMock,
        return_value={"error_code": "INVALID_MESSAGE", "error": "Empty message"},
    )

    response = client.post("/chat/message", json=_payload(), headers=auth_headers)

    assert response.status_code == 400


def test_unauthenticated_error_code_maps_to_401(client: TestClient, auth_headers, mocker):
    mocker.patch(
        "app.api.chat_routes.chat_service.process_message",
        new_callable=AsyncMock,
        return_value={"error_code": "UNAUTHENTICATED", "error": "Authentication required"},
    )

    response = client.post("/chat/message", json=_payload(), headers=auth_headers)

    assert response.status_code == 401


def test_user_not_found_error_code_maps_to_404(client: TestClient, auth_headers, mocker):
    mocker.patch(
        "app.api.chat_routes.chat_service.process_message",
        new_callable=AsyncMock,
        return_value={"error_code": "USER_NOT_FOUND", "error": "User not found"},
    )

    response = client.post("/chat/message", json=_payload(), headers=auth_headers)

    assert response.status_code == 404


def test_llm_unavailable_falls_through_as_200_not_5xx(client: TestClient, auth_headers, mocker):
    """
    LLM_UNAVAILABLE isn't a client error - chat_service already produced
    a usable (apologetic) response and a truthful reminder_set value, so
    the route returns it normally rather than a 5xx.
    """
    mocker.patch(
        "app.api.chat_routes.chat_service.process_message",
        new_callable=AsyncMock,
        return_value={
            "error_code": "LLM_UNAVAILABLE",
            "response": "I'm having trouble thinking straight for a second.",
            "reminder_set": True,
        },
    )

    response = client.post("/chat/message", json=_payload(), headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data["reminder_set"] is True


def test_unrecognized_error_code_maps_to_500(client: TestClient, auth_headers, mocker):
    mocker.patch(
        "app.api.chat_routes.chat_service.process_message",
        new_callable=AsyncMock,
        return_value={"error_code": "SOMETHING_UNEXPECTED", "error": "unexpected"},
    )

    response = client.post("/chat/message", json=_payload(), headers=auth_headers)

    assert response.status_code == 500


def test_request_id_is_not_currently_deduplicated(client: TestClient, auth_headers, mock_chat_service):
    """
    Documents a known, currently-unresolved gap rather than asserting a
    fix: sending the SAME request_id twice results in TWO separate
    process_message calls, not one. request_id is only echoed back in
    the response today - there is no idempotency store anywhere in the
    codebase keyed on it. This is a regression MARKER, not an
    endorsement: if real idempotency is added later, this test should
    be updated to assert the opposite (the second call is skipped or
    returns the first call's cached result) - a green run of this test
    after that change would mean the fix didn't actually take effect.
    """
    mock_chat_service.return_value = {"response": "ok", "reminder_set": False}
    payload = _payload(request_id="same-id-both-times")

    client.post("/chat/message", json=payload, headers=auth_headers)
    client.post("/chat/message", json=payload, headers=auth_headers)

    assert mock_chat_service.call_count == 2