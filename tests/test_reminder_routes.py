"""
Reminder Routes Tests
File location: tests/test_reminder_routes.py

Closes the second half of the same gap as test_user_routes.py: the
P0-11 fix in reminder_routes.py (never trust req.user_timezone; always
use the authenticated user's stored profile.timezone) was verified by
direct code inspection, not by an actual request through
POST /reminders/create. This is the specific behavior the Production
Approval Checklist means by "authoritative for all reminder flows" -
not just that validation exists somewhere, but that a client sending a
different timezone than what's on file cannot make it take effect.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.repositories.user_repository import UserRepository


class TestCreateReminderAuthoritativeTimezone:
    def test_ignores_client_supplied_timezone_uses_stored_profile(
        self, client: TestClient, auth_headers, test_user, test_db, mocker
    ):
        profile = UserRepository.get_or_create_profile(test_db, test_user["user_id"])
        profile.timezone = "Asia/Karachi"
        test_db.commit()

        mock_create = mocker.patch(
            "app.api.reminder_routes.reminder_service.maybe_create_from_scheduled_at",
            new_callable=AsyncMock,
            return_value={
                "status": "created",
                "reminder": MagicMock(id=1, content="call mom"),
                "local_time": "2026-12-01 09:00 PKT",
            },
        )

        response = client.post(
            "/reminders/create",
            json={
                "content": "call mom",
                "scheduled_at_iso": "2026-12-01T09:00:00+00:00",
                # Deliberately a DIFFERENT timezone than what's on file -
                # this must be ignored, not honored.
                "user_timezone": "America/New_York",
            },
            headers=auth_headers,
        )

        assert response.status_code == 200
        sent_timezone = mock_create.call_args.kwargs["user_timezone"]
        assert sent_timezone == "Asia/Karachi"
        assert sent_timezone != "America/New_York"

    def test_falls_back_to_utc_when_profile_has_no_timezone_set(
        self, client: TestClient, auth_headers, test_user, test_db, mocker
    ):
        profile = UserRepository.get_or_create_profile(test_db, test_user["user_id"])
        profile.timezone = None
        test_db.commit()

        mock_create = mocker.patch(
            "app.api.reminder_routes.reminder_service.maybe_create_from_scheduled_at",
            new_callable=AsyncMock,
            return_value={
                "status": "created",
                "reminder": MagicMock(id=1, content="call mom"),
                "local_time": "2026-12-01 09:00 UTC",
            },
        )

        client.post(
            "/reminders/create",
            json={
                "content": "call mom",
                "scheduled_at_iso": "2026-12-01T09:00:00+00:00",
                "user_timezone": "Europe/Paris",
            },
            headers=auth_headers,
        )

        assert mock_create.call_args.kwargs["user_timezone"] == "UTC"

    def test_requires_auth(self, client: TestClient):
        response = client.post(
            "/reminders/create",
            json={"content": "call mom", "scheduled_at_iso": "2026-12-01T09:00:00+00:00"},
        )
        assert response.status_code == 401

    def test_rejects_datetime_without_timezone_offset(self, client: TestClient, auth_headers):
        response = client.post(
            "/reminders/create",
            json={"content": "call mom", "scheduled_at_iso": "2026-12-01T09:00:00"},  # no offset
            headers=auth_headers,
        )
        assert response.status_code == 400