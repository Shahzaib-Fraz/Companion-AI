"""
Chat Routes Tests
File location: tests/test_chat.py

Tests for:
- POST /message (with rate limiting, validation, auth)

Fix applied vs. the original: test_send_message_whatsapp_channel checked
`mock_chat_service.call_args[0][3]` (positional argument index 3) for
the channel value. app/api/chat_routes.py calls
`chat_service.process_message(db=db, message=..., user_id=..., channel=...)`
with EVERY argument passed as a keyword, so call_args[0] (the positional
args tuple) is always empty - that assertion would raise IndexError
regardless of whether the mock itself was fixed. Now checks
call_args.kwargs instead.

The mock_chat_service fixture itself (patching chat_service.process_message
as an AsyncMock, rather than the whole chat_service object) is fixed in
conftest.py - see the comment there for why that mattered more than this one.
"""
import pytest
from fastapi.testclient import TestClient


class TestSendMessage:
    """Tests for send_message endpoint."""

    def test_send_message_success(
        self, client: TestClient, auth_headers, valid_chat_message, mock_chat_service
    ):
        """Send valid message returns AI response."""
        mock_chat_service.return_value = {
            "response": "Hello! How can I help you?",
            "reminder_set": False,
        }

        response = client.post(
            "/chat/message",
            json=valid_chat_message,
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert "response" in data
        assert data["response"] == "Hello! How can I help you?"
        assert "user_id" in data
        assert data["request_id"] == valid_chat_message["request_id"]

        mock_chat_service.assert_called_once()

    def test_send_message_whatsapp_channel(
        self, client: TestClient, auth_headers, chat_message_whatsapp, mock_chat_service
    ):
        """Send message via WhatsApp channel works."""
        mock_chat_service.return_value = {"response": "Message received on WhatsApp", "reminder_set": False}

        response = client.post(
            "/chat/message",
            json=chat_message_whatsapp,
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["response"] == "Message received on WhatsApp"

        # FIX: chat_routes.py passes every argument as a keyword
        # (channel=chat_request.channel, not a positional arg), so
        # call_args[0] (positional tuple) is always empty - check kwargs.
        call_args = mock_chat_service.call_args
        assert call_args.kwargs["channel"] == "whatsapp"

    # ============================================================================
    # AUTHENTICATION TESTS
    # ============================================================================

    def test_send_message_no_auth_token(self, client: TestClient, valid_chat_message):
        """Send message without token returns 401."""
        response = client.post(
            "/chat/message",
            json=valid_chat_message,
        )

        assert response.status_code == 401
        assert "Missing authorization token" in response.json()["detail"]

    def test_send_message_invalid_auth_token(
        self, client: TestClient, invalid_auth_headers, valid_chat_message
    ):
        """Send message with invalid token returns 401."""
        response = client.post(
            "/chat/message",
            json=valid_chat_message,
            headers=invalid_auth_headers,
        )

        assert response.status_code == 401
        assert "Invalid or expired token" in response.json()["detail"]

    def test_send_message_malformed_auth_header(
        self, client: TestClient, valid_chat_message
    ):
        """Send message with malformed auth header returns 401."""
        response = client.post(
            "/chat/message",
            json=valid_chat_message,
            headers={"Authorization": "NotABearerToken"},
        )

        assert response.status_code == 401
        assert "Missing authorization token" in response.json()["detail"]

    # ============================================================================
    # VALIDATION TESTS
    # ============================================================================

    def test_send_message_empty_message(self, client: TestClient, auth_headers):
        """Empty message returns 422 validation error."""
        response = client.post(
            "/chat/message",
            json={
                "message": "",
                "channel": "web",
                "request_id": "req-123",
            },
            headers=auth_headers,
        )

        assert response.status_code == 422

    def test_send_message_too_long(self, client: TestClient, auth_headers):
        """Message > 2000 chars returns 422 validation error."""
        response = client.post(
            "/chat/message",
            json={
                "message": "a" * 2001,
                "channel": "web",
                "request_id": "req-123",
            },
            headers=auth_headers,
        )

        assert response.status_code == 422

    def test_send_message_invalid_channel(
        self, client: TestClient, auth_headers, mock_chat_service
    ):
        """Invalid channel returns 422 validation error."""
        response = client.post(
            "/chat/message",
            json={
                "message": "Hello",
                "channel": "invalid_channel",
                "request_id": "req-123",
            },
            headers=auth_headers,
        )

        assert response.status_code == 422

    def test_send_message_missing_request_id_auto_generates_one(self, client: TestClient, auth_headers, mock_chat_service):
        """
        Corrected: the real ChatRequest.request_id uses
        default_factory=uuid4 - it's optional BY DESIGN, auto-generated
        when omitted, not a required field. The original version of this
        test assumed 422; that assumption was simply wrong against the
        real schema, not something either implementation needed to
        change to satisfy.
        """
        mock_chat_service.return_value = {"response": "OK", "reminder_set": False}

        response = client.post(
            "/chat/message",
            json={
                "message": "Hello",
                "channel": "web",
            },
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert "request_id" in data
        assert len(data["request_id"]) > 0  # a real UUID was generated, not empty/null

    def test_send_message_missing_message_field(self, client: TestClient, auth_headers):
        """Missing message field returns 422."""
        response = client.post(
            "/chat/message",
            json={
                "channel": "web",
                "request_id": "req-123",
            },
            headers=auth_headers,
        )

        assert response.status_code == 422

    def test_send_message_missing_channel(self, client: TestClient, auth_headers):
        """Missing channel returns 422."""
        response = client.post(
            "/chat/message",
            json={
                "message": "Hello",
                "request_id": "req-123",
            },
            headers=auth_headers,
        )

        assert response.status_code == 422

    # ============================================================================
    # EDGE CASES
    # ============================================================================

    def test_send_message_exact_max_length(
        self, client: TestClient, auth_headers, mock_chat_service
    ):
        """Message with exactly 2000 chars succeeds."""
        mock_chat_service.return_value = {"response": "OK", "reminder_set": False}

        response = client.post(
            "/chat/message",
            json={
                "message": "a" * 2000,
                "channel": "web",
                "request_id": "req-123",
            },
            headers=auth_headers,
        )

        assert response.status_code == 200

    def test_send_message_with_special_chars(
        self, client: TestClient, auth_headers, mock_chat_service
    ):
        """Message with special chars and emojis works."""
        mock_chat_service.return_value = {"response": "Got it!", "reminder_set": False}

        special_message = "Hello! \U0001F44B How are you? @#$%^&*()"
        response = client.post(
            "/chat/message",
            json={
                "message": special_message,
                "channel": "web",
                "request_id": "req-123",
            },
            headers=auth_headers,
        )

        assert response.status_code == 200

    def test_send_message_with_newlines(
        self, client: TestClient, auth_headers, mock_chat_service
    ):
        """Message with newlines works."""
        mock_chat_service.return_value = {"response": "OK", "reminder_set": False}

        response = client.post(
            "/chat/message",
            json={
                "message": "Line 1\nLine 2\nLine 3",
                "channel": "web",
                "request_id": "req-123",
            },
            headers=auth_headers,
        )

        assert response.status_code == 200

    # ============================================================================
    # RATE LIMITING TESTS
    # ============================================================================

    def test_send_message_rate_limit(
        self, client: TestClient, auth_headers, valid_chat_message, mock_chat_service
    ):
        """Rate limiter allows 10 requests/minute."""
        mock_chat_service.return_value = {"response": "OK", "reminder_set": False}

        for i in range(10):
            response = client.post(
                "/chat/message",
                json={
                    "message": f"Message {i}",
                    "channel": "web",
                    "request_id": f"req-{i}",
                },
                headers=auth_headers,
            )
            assert response.status_code == 200

    def test_send_message_exceeds_rate_limit(
        self, client: TestClient, auth_headers, valid_chat_message, mock_chat_service
    ):
        """11th request within a minute returns 429 Too Many Requests."""
        mock_chat_service.return_value = {"response": "OK", "reminder_set": False}

        for i in range(11):
            response = client.post(
                "/chat/message",
                json={
                    "message": f"Message {i}",
                    "channel": "web",
                    "request_id": f"req-{i}",
                },
                headers=auth_headers,
            )

            if i < 10:
                assert response.status_code == 200
            else:
                assert response.status_code == 429


class TestChatIntegration:
    """Integration tests for chat functionality."""

    def test_chat_flow_onboarding(
        self, client: TestClient, auth_headers, mock_chat_service
    ):
        """Chat flow during onboarding steps."""
        mock_chat_service.return_value = {
            "response": "Which tier? (free or premium)",
            "onboarding_step": 0,
            "onboarding_completed": False,
        }

        response = client.post(
            "/chat/message",
            json={
                "message": "free",
                "channel": "web",
                "request_id": "req-1",
            },
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert "Which tier?" in data["response"]

    def test_chat_flow_post_onboarding(
        self, client: TestClient, auth_headers, mock_chat_service
    ):
        """Chat flow after onboarding complete."""
        mock_chat_service.return_value = {
            "response": "That's interesting! Tell me more.",
            "onboarding_step": 5,
            "onboarding_completed": True,
            "reminder_set": False,
        }

        response = client.post(
            "/chat/message",
            json={
                "message": "How does machine learning work?",
                "channel": "web",
                "request_id": "req-1",
            },
            headers=auth_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert "interesting" in data["response"].lower()


class TestReminderSetField:
    """
    Regression test: ChatResponse (app/schemas/schemas.py) didn't
    declare a reminder_set field. chat_routes.py has always passed
    reminder_set=... into its constructor, but Pydantic's default
    extra='ignore' behavior silently drops unrecognized keyword
    arguments rather than raising - so the field never actually
    appeared in the JSON response sent to the client, no exception,
    no warning. Fixed by adding the field to the real ChatResponse.
    """

    def test_reminder_set_field_is_present_and_true(
        self, client: TestClient, auth_headers, valid_chat_message, mock_chat_service
    ):
        mock_chat_service.return_value = {"response": "Got it!", "reminder_set": True}

        response = client.post("/chat/message", json=valid_chat_message, headers=auth_headers)

        assert response.status_code == 200
        data = response.json()
        assert "reminder_set" in data  # would have been silently missing before the fix
        assert data["reminder_set"] is True

    def test_reminder_set_field_is_present_and_false(
        self, client: TestClient, auth_headers, valid_chat_message, mock_chat_service
    ):
        mock_chat_service.return_value = {"response": "Got it!", "reminder_set": False}

        response = client.post("/chat/message", json=valid_chat_message, headers=auth_headers)

        data = response.json()
        assert "reminder_set" in data
        assert data["reminder_set"] is False

    def test_reminder_set_defaults_to_false_when_service_omits_it(
        self, client: TestClient, auth_headers, valid_chat_message, mock_chat_service
    ):
        mock_chat_service.return_value = {"response": "Got it!"}  # no reminder_set key at all

        response = client.post("/chat/message", json=valid_chat_message, headers=auth_headers)

        data = response.json()
        assert data["reminder_set"] is False