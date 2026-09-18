"""
Tests for WhatsAppService.verify_webhook_signature() - the P0-9 fix.
Previously this method existed but nothing in whatsapp_routes.py ever
called it; these tests prove the function itself is correct, which
whatsapp_routes.py now actually relies on for every incoming webhook.
"""
import hashlib
import hmac

from app.services.whatsapp_service import whatsapp_service


def _sign(body: str, secret: str) -> str:
    digest = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_accepts_correctly_signed_body():
    body = '{"entry": []}'
    signature = _sign(body, whatsapp_service.app_secret)
    assert whatsapp_service.verify_webhook_signature(body, signature) is True


def test_rejects_tampered_body():
    body = '{"entry": []}'
    signature = _sign(body, whatsapp_service.app_secret)
    tampered_body = '{"entry": [{"injected": true}]}'
    assert whatsapp_service.verify_webhook_signature(tampered_body, signature) is False


def test_rejects_signature_from_wrong_secret():
    body = '{"entry": []}'
    signature = _sign(body, "not-the-real-secret")
    assert whatsapp_service.verify_webhook_signature(body, signature) is False


def test_rejects_missing_signature():
    body = '{"entry": []}'
    assert whatsapp_service.verify_webhook_signature(body, "") is False


def test_rejects_malformed_signature_header():
    body = '{"entry": []}'
    assert whatsapp_service.verify_webhook_signature(body, "not-even-hmac-shaped") is False