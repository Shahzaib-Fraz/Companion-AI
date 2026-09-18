"""
Tests for PIIMasker (app/schemas/schemas.py) - the P1-18 fix.

Two layers:
1. Pure redaction tests - prove mask_message() actually strips PII from
   arbitrary text, not just truncates it (the exact gap that was still
   live despite the file's own comment claiming it was fixed).
2. Log-capture tests with seeded PII and secrets, using pytest's caplog
   fixture, proving specific real call sites (auth, WhatsApp, reminder
   creation) never emit the raw value into the log record - this is
   what the original review asked for by name ("Add log-capture tests
   with seeded PII and secrets").
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.schemas.schemas import PIIMasker


# --------------------------------------------------------------- mask_phone / mask_email


def test_mask_phone_hides_the_middle():
    masked = PIIMasker.mask_phone("+923037454400")
    assert masked != "+923037454400"
    assert "***" in masked


def test_mask_phone_handles_too_short_input():
    assert PIIMasker.mask_phone("123") == "[INVALID_PHONE]"


def test_mask_email_hides_the_local_part():
    masked = PIIMasker.mask_email("augustine@gmail.com")
    assert masked != "augustine@gmail.com"
    assert masked.startswith("a")
    assert masked.endswith("@gmail.com")


def test_mask_email_handles_invalid_input():
    assert PIIMasker.mask_email("not-an-email") == "[INVALID_EMAIL]"


# --------------------------------------------------------------- mask_message: the actual fix


def test_mask_message_redacts_an_email_within_free_text():
    result = PIIMasker.mask_message("please email me at ch.shahzaibfraz@gmail.com about this")
    assert "ch.shahzaibfraz@gmail.com" not in result
    assert "[REDACTED_EMAIL]" in result


def test_mask_message_redacts_a_phone_number_within_free_text():
    result = PIIMasker.mask_message("call me at +923037454400 tomorrow")
    assert "923037454400" not in result
    assert "[REDACTED_PHONE]" in result


def test_mask_message_redacts_a_short_message_containing_pii():
    """
    THE exact regression: a message under the 50-char truncation
    threshold used to pass through completely untouched, since
    mask_message() only ever truncated, never redacted.
    """
    short_message = "my email is x@y.com"  # well under 50 chars
    result = PIIMasker.mask_message(short_message)
    assert "x@y.com" not in result
    assert "[REDACTED_EMAIL]" in result


def test_mask_message_redacts_a_bearer_token():
    result = PIIMasker.mask_message("here's my token: Bearer abc123.xyz789secretvalue")
    assert "abc123.xyz789secretvalue" not in result


def test_mask_message_redacts_a_jwt_shaped_string():
    fake_jwt = "eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjo3fQ.rzIZjuCnKDvg0nW8qR2fsZDMH4ra7m1zju3FvKbNu30"
    result = PIIMasker.mask_message(f"my token is {fake_jwt}")
    assert fake_jwt not in result
    assert "[REDACTED_TOKEN]" in result


def test_mask_message_redacts_a_card_like_number():
    result = PIIMasker.mask_message("card number 4111111111111111 please")
    assert "4111111111111111" not in result


def test_mask_message_still_truncates_long_clean_text():
    long_message = "a" * 100
    result = PIIMasker.mask_message(long_message, length=50)
    assert len(result) <= 53  # 50 chars + "..."
    assert result.endswith("...")


def test_mask_message_leaves_ordinary_text_untouched():
    result = PIIMasker.mask_message("remind me to buy groceries tomorrow")
    assert result == "remind me to buy groceries tomorrow"


def test_mask_message_handles_empty_input():
    assert PIIMasker.mask_message("") == "[EMPTY_MESSAGE]"


def test_mask_message_handles_multiple_pii_items_in_one_message():
    result = PIIMasker.mask_message(
        "reach me at ch.shahzaibfraz@gmail.com or +923037454400"
    )
    assert "ch.shahzaibfraz@gmail.com" not in result
    assert "923037454400" not in result


# --------------------------------------------------------------- log-capture tests with seeded PII


SEEDED_EMAIL = "ch.shahzaibfraz@gmail.com"  # the exact address from the real production log
SEEDED_PHONE = "+923037454400"


def test_signup_log_never_contains_the_raw_email(client, caplog):
    import logging
    with caplog.at_level(logging.INFO):
        client.post("/auth/signup", json={"email": SEEDED_EMAIL, "password": "SecurePass123"})

    for record in caplog.records:
        assert SEEDED_EMAIL not in record.getMessage()


def test_login_log_never_contains_the_raw_email(client, caplog):
    import logging
    client.post("/auth/signup", json={"email": SEEDED_EMAIL, "password": "SecurePass123"})
    caplog.clear()

    with caplog.at_level(logging.INFO):
        client.post("/auth/login", json={"email": SEEDED_EMAIL, "password": "wrong"})

    for record in caplog.records:
        assert SEEDED_EMAIL not in record.getMessage()


@pytest.mark.asyncio
async def test_reminder_creation_log_never_contains_raw_content(db_session, make_user, mocker, caplog):
    import logging
    from app.api.reminder_routes import create_reminder, CreateReminderRequest
    from app.repositories.user_repository import UserRepository
    from app.core.security import create_access_token

    user = make_user()
    UserRepository.get_or_create_profile(db_session, user.id)
    profile = UserRepository.get_or_create_profile(db_session, user.id)
    profile.timezone = "UTC"
    db_session.commit()

    fake_reminder = MagicMock()
    fake_reminder.id = 1
    fake_reminder.content = f"call {SEEDED_PHONE} about the invoice"
    fake_reminder.scheduled_at = None

    mocker.patch(
        "app.api.reminder_routes.reminder_service.maybe_create_from_scheduled_at",
        new_callable=AsyncMock,
        return_value={"status": "created", "reminder": fake_reminder, "local_time": "2026-09-20 09:00"},
    )

    token = create_access_token(user.id)
    req = CreateReminderRequest(
        content=f"call {SEEDED_PHONE} about the invoice",
        scheduled_at_iso="2026-09-20T09:00:00+00:00",
    )

    with caplog.at_level(logging.INFO):
        await create_reminder(req, db=db_session, authorization=f"Bearer {token}")

    for record in caplog.records:
        assert SEEDED_PHONE not in record.getMessage()


@pytest.mark.asyncio
async def test_whatsapp_send_message_log_never_contains_raw_phone(mocker, caplog):
    import logging
    from app.services.whatsapp_service import whatsapp_service

    mock_response = MagicMock(status_code=200)
    mocker.patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response)

    with caplog.at_level(logging.INFO):
        await whatsapp_service.send_message(SEEDED_PHONE, "hello")

    for record in caplog.records:
        assert SEEDED_PHONE not in record.getMessage()


@pytest.mark.asyncio
async def test_email_service_log_never_contains_raw_email(caplog, mocker):
    """
    UPDATED: email_service.py is now confirmed as the real production
    file (it was a stub when this test was first written) and makes a
    genuine Brevo API call - mocked here since this file has no network
    access to Brevo, same pattern as the WhatsApp tests mock Meta.
    """
    import logging
    from unittest.mock import AsyncMock, MagicMock
    from app.services.email_service import email_service

    mock_response = MagicMock(status_code=201)
    mock_response.json.return_value = {"messageId": "x"}
    mocker.patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response)

    with caplog.at_level(logging.INFO):
        await email_service.send_reminder(user_email=SEEDED_EMAIL, content="take your medicine")

    for record in caplog.records:
        assert SEEDED_EMAIL not in record.getMessage()