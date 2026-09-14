"""
Chat Routes Tests
File location: tests/test_chat.py

Tests for:
- POST /message (with rate limiting, validation, auth)
"""
import pytest
from fastapi.testclient import TestClient


class TestSendMessage:
    """Tests for send_message endpoint."""
    
    def test_send_message_success(
        self, client: TestClient, auth_headers, valid_chat_message, mock_chat_service
    ):
        """✅ Send valid message returns AI response."""
        mock_chat_service.return_value = {
            "response": "Hello! How can I help you?",
            "onboarding_step": 0,
            "onboarding_completed": False,
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
        
        # Verify chat_service was called
        mock_chat_service.assert_called_once()
    
    def test_send_message_whatsapp_channel(
        self, client: TestClient, auth_headers, chat_message_whatsapp, mock_chat_service
    ):
        """✅ Send message via WhatsApp channel works."""
        mock_chat_service.return_value = {"response": "Message received on WhatsApp"}
        
        response = client.post(
            "/chat/message",
            json=chat_message_whatsapp,
            headers=auth_headers,
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["response"] == "Message received on WhatsApp"
        
        # Verify channel was passed correctly
        call_args = mock_chat_service.call_args
        assert call_args[0][3] == "whatsapp"  # channel parameter
    
    # ============================================================================
    # AUTHENTICATION TESTS
    # ============================================================================
    
    def test_send_message_no_auth_token(self, client: TestClient, valid_chat_message):
        """❌ Send message without token returns 401."""
        response = client.post(
            "/chat/message",
            json=valid_chat_message,
            # No headers
        )
        
        assert response.status_code == 401
        assert "Missing authorization token" in response.json()["detail"]
    
    def test_send_message_invalid_auth_token(
        self, client: TestClient, invalid_auth_headers, valid_chat_message
    ):
        """❌ Send message with invalid token returns 401."""
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
        """❌ Send message with malformed auth header returns 401."""
        response = client.post(
            "/chat/message",
            json=valid_chat_message,
            headers={"Authorization": "NotABearerToken"},
        )
        
        assert response.status_code == 401
        assert "Missing authorization token" in response.json()["detail"]
    
    # ============================================================================
    # VALIDATION TESTS (Issue #20)
    # ============================================================================
    
    def test_send_message_empty_message(self, client: TestClient, auth_headers):
        """❌ Empty message returns 422 validation error."""
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
        """❌ Message > 2000 chars returns 422 validation error."""
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
        """❌ Invalid channel returns 422 validation error."""
        response = client.post(
            "/chat/message",
            json={
                "message": "Hello",
                "channel": "invalid_channel",  # Only "web" or "whatsapp" allowed
                "request_id": "req-123",
            },
            headers=auth_headers,
        )
        
        assert response.status_code == 422
    
    def test_send_message_missing_request_id(self, client: TestClient, auth_headers):
        """❌ Missing request_id returns 422."""
        response = client.post(
            "/chat/message",
            json={
                "message": "Hello",
                "channel": "web",
                # Missing request_id
            },
            headers=auth_headers,
        )
        
        assert response.status_code == 422
    
    def test_send_message_missing_message_field(self, client: TestClient, auth_headers):
        """❌ Missing message field returns 422."""
        response = client.post(
            "/chat/message",
            json={
                "channel": "web",
                "request_id": "req-123",
                # Missing message
            },
            headers=auth_headers,
        )
        
        assert response.status_code == 422
    
    def test_send_message_missing_channel(self, client: TestClient, auth_headers):
        """❌ Missing channel returns 422."""
        response = client.post(
            "/chat/message",
            json={
                "message": "Hello",
                "request_id": "req-123",
                # Missing channel
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
        """✅ Message with exactly 2000 chars succeeds."""
        mock_chat_service.return_value = {"response": "OK"}
        
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
        """✅ Message with special chars and emojis works."""
        mock_chat_service.return_value = {"response": "Got it!"}
        
        special_message = "Hello! 👋 How are you? @#$%^&*()"
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
        """✅ Message with newlines works."""
        mock_chat_service.return_value = {"response": "OK"}
        
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
    # RATE LIMITING TESTS (Issue #19)
    # ============================================================================
    
    def test_send_message_rate_limit(
        self, client: TestClient, auth_headers, valid_chat_message, mock_chat_service
    ):
        """✅ Rate limiter allows 10 requests/minute."""
        mock_chat_service.return_value = {"response": "OK"}
        
        # Send 10 requests - should all succeed
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
        """❌ 11th request within minute returns 429 Too Many Requests."""
        mock_chat_service.return_value = {"response": "OK"}
        
        # Send 11 requests - 11th should be rate limited
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
                # 11th request should be rate limited
                assert response.status_code == 429


class TestChatIntegration:
    """Integration tests for chat functionality."""
    
    def test_chat_flow_onboarding(
        self, client: TestClient, auth_headers, mock_chat_service
    ):
        """✅ Chat flow during onboarding steps."""
        # Simulate onboarding: tier selection
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
        """✅ Chat flow after onboarding complete."""
        # Simulate normal chat
        mock_chat_service.return_value = {
            "response": "That's interesting! Tell me more.",
            "onboarding_step": 5,  # Complete
            "onboarding_completed": True,
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