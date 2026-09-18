"""
Test matrix: Reminders
  "Email fail, WhatsApp fail, partial success, retry, and stale
  PROCESSING recovery."

test_reminder_repository.py already covers the finalize_attempt() STATE
MACHINE in isolation. This file covers the layer above it -
scheduler_worker._process_reminder(), which decides what "sent"/"failed"/
"skip" to pass to finalize_attempt() based on the user's actual
email/phone and mocked provider responses. Stale PROCESSING recovery
itself is already covered in test_reminder_repository.py
(recover_stale_processing tests) - not repeated here.
"""
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from scheduler_worker import SchedulerWorker
from app.models.database import Reminder


def _make_reminder(db_session, user, **overrides):
    defaults = dict(
        user_id=user.id,
        content="Take your medicine",
        scheduled_at=datetime.utcnow(),
        timezone="UTC",
        status="PROCESSING",
        email_status="PENDING",
        whatsapp_status="PENDING",
        attempt_count=1,
        last_attempt_at=datetime.utcnow(),
    )
    defaults.update(overrides)
    reminder = Reminder(**defaults)
    db_session.add(reminder)
    db_session.commit()
    db_session.refresh(reminder)
    return reminder


@pytest.mark.asyncio
async def test_both_channels_succeed_marks_sent(db_session, make_user, mocker):
    user = make_user(email="user@example.com")
    user.phone_number = "+923001234567"
    db_session.commit()
    reminder = _make_reminder(db_session, user)

    mocker.patch("app.services.email_service.email_service.send_reminder", new_callable=AsyncMock, return_value=True)
    mocker.patch("app.services.whatsapp_service.whatsapp_service.send_message", new_callable=AsyncMock, return_value=True)

    await SchedulerWorker()._process_reminder(db_session, reminder)
    db_session.refresh(reminder)

    assert reminder.status == "SENT"
    assert reminder.email_status == "SENT"
    assert reminder.whatsapp_status == "SENT"
    assert reminder.sent_at is not None


@pytest.mark.asyncio
async def test_email_fails_whatsapp_succeeds_marks_partially_sent(db_session, make_user, mocker):
    user = make_user(email="user@example.com")
    user.phone_number = "+923001234567"
    db_session.commit()
    reminder = _make_reminder(db_session, user)

    mocker.patch("app.services.email_service.email_service.send_reminder", new_callable=AsyncMock, return_value=False)
    mocker.patch("app.services.whatsapp_service.whatsapp_service.send_message", new_callable=AsyncMock, return_value=True)

    await SchedulerWorker()._process_reminder(db_session, reminder)
    db_session.refresh(reminder)

    assert reminder.status == "PARTIALLY_SENT"
    assert reminder.email_status == "FAILED"
    assert reminder.whatsapp_status == "SENT"
    assert reminder.sent_at is None


@pytest.mark.asyncio
async def test_whatsapp_fails_email_succeeds_marks_partially_sent(db_session, make_user, mocker):
    user = make_user(email="user@example.com")
    user.phone_number = "+923001234567"
    db_session.commit()
    reminder = _make_reminder(db_session, user)

    mocker.patch("app.services.email_service.email_service.send_reminder", new_callable=AsyncMock, return_value=True)
    mocker.patch("app.services.whatsapp_service.whatsapp_service.send_message", new_callable=AsyncMock, return_value=False)

    await SchedulerWorker()._process_reminder(db_session, reminder)
    db_session.refresh(reminder)

    assert reminder.status == "PARTIALLY_SENT"
    assert reminder.email_status == "SENT"
    assert reminder.whatsapp_status == "FAILED"


@pytest.mark.asyncio
async def test_both_channels_fail_stays_pending_for_retry(db_session, make_user, mocker):
    user = make_user(email="user@example.com")
    user.phone_number = "+923001234567"
    db_session.commit()
    reminder = _make_reminder(db_session, user)  # attempt_count=1, well under MAX_ATTEMPTS

    mocker.patch("app.services.email_service.email_service.send_reminder", new_callable=AsyncMock, return_value=False)
    mocker.patch("app.services.whatsapp_service.whatsapp_service.send_message", new_callable=AsyncMock, return_value=False)

    await SchedulerWorker()._process_reminder(db_session, reminder)
    db_session.refresh(reminder)

    assert reminder.status == "PENDING"  # eligible for another automatic retry
    assert reminder.email_status == "FAILED"
    assert reminder.whatsapp_status == "FAILED"


@pytest.mark.asyncio
async def test_retry_does_not_resend_already_succeeded_channel(db_session, make_user, mocker):
    """A reminder that's PARTIALLY_SENT (email already delivered) shouldn't re-email on retry - only WhatsApp is attempted."""
    user = make_user(email="user@example.com")
    user.phone_number = "+923001234567"
    db_session.commit()
    reminder = _make_reminder(
        db_session, user, status="PARTIALLY_SENT", email_status="SENT", whatsapp_status="FAILED",
    )

    mock_email = mocker.patch("app.services.email_service.email_service.send_reminder", new_callable=AsyncMock, return_value=True)
    mock_whatsapp = mocker.patch("app.services.whatsapp_service.whatsapp_service.send_message", new_callable=AsyncMock, return_value=True)

    await SchedulerWorker()._process_reminder(db_session, reminder)
    db_session.refresh(reminder)

    mock_email.assert_not_called()  # already SENT - must not re-send
    mock_whatsapp.assert_called_once()
    assert reminder.status == "SENT"


@pytest.mark.asyncio
async def test_user_with_no_phone_skips_whatsapp_and_can_still_fully_send(db_session, make_user, mocker):
    user = make_user(email="user@example.com")  # no phone_number set at all
    reminder = _make_reminder(db_session, user)

    mocker.patch("app.services.email_service.email_service.send_reminder", new_callable=AsyncMock, return_value=True)
    mock_whatsapp = mocker.patch("app.services.whatsapp_service.whatsapp_service.send_message", new_callable=AsyncMock)

    await SchedulerWorker()._process_reminder(db_session, reminder)
    db_session.refresh(reminder)

    mock_whatsapp.assert_not_called()
    assert reminder.whatsapp_status == "SKIPPED"
    assert reminder.status == "SENT"  # email alone is sufficient - whatsapp was never applicable


@pytest.mark.asyncio
async def test_missing_user_marks_reminder_failed(db_session, make_user, mocker):
    user = make_user()
    reminder = _make_reminder(db_session, user)
    # simulate the user having been deleted between claim and processing
    from app.models.database import User
    db_session.query(User).filter(User.id == user.id).delete()
    db_session.commit()

    await SchedulerWorker()._process_reminder(db_session, reminder)
    db_session.refresh(reminder)

    assert reminder.status in ("PENDING", "FAILED")  # finalize_attempt's own attempt-count logic decides which
    assert reminder.last_failure_reason == "User not found"


# ------------------------------------------------------------------ #
# Regression test for a real production bug: email_service.send_reminder()
# in the user's actual deployed environment turned out to be a plain
# `def`, not `async def` as the originally-shown stub was - `await`ing
# its bool return crashed AFTER the real send already happened, so the
# email was correctly delivered but recorded as failed, causing the next
# retry to send it again. _maybe_await() fixes this by only awaiting a
# result that's actually awaitable.
# ------------------------------------------------------------------ #

from unittest.mock import MagicMock


@pytest.mark.asyncio
async def test_process_reminder_handles_a_synchronous_email_service(db_session, make_user, mocker):
    """
    email_service.send_reminder returning a plain bool (not a coroutine)
    must not crash, and must not be misrecorded as failed when it
    actually succeeded.
    """
    user = make_user(email="user@example.com")
    reminder = _make_reminder(db_session, user, whatsapp_status="SKIPPED")

    # A plain MagicMock (not AsyncMock) called directly returns its
    # return_value immediately - exactly what a sync `def` does, and
    # exactly what broke in production.
    mocker.patch(
        "app.services.email_service.email_service.send_reminder",
        MagicMock(return_value=True),
    )

    await SchedulerWorker()._process_reminder(db_session, reminder)
    db_session.refresh(reminder)

    assert reminder.email_status == "SENT"  # not "FAILED" - this is the actual regression
    assert reminder.status == "SENT"


@pytest.mark.asyncio
async def test_process_reminder_still_handles_an_async_email_service(db_session, make_user, mocker):
    """The other half of the fix: a genuinely async implementation must keep working exactly as before."""
    user = make_user(email="user@example.com")
    reminder = _make_reminder(db_session, user, whatsapp_status="SKIPPED")

    mocker.patch(
        "app.services.email_service.email_service.send_reminder",
        new_callable=AsyncMock, return_value=True,
    )

    await SchedulerWorker()._process_reminder(db_session, reminder)
    db_session.refresh(reminder)

    assert reminder.email_status == "SENT"
    assert reminder.status == "SENT"


@pytest.mark.asyncio
async def test_maybe_await_passes_through_a_plain_value():
    assert await SchedulerWorker._maybe_await(True) is True
    assert await SchedulerWorker._maybe_await(False) is False


@pytest.mark.asyncio
async def test_maybe_await_awaits_a_real_coroutine():
    async def _coro():
        return "awaited"
    assert await SchedulerWorker._maybe_await(_coro()) == "awaited"