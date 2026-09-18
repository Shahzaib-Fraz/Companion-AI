"""Timezone validation service - P0-11 FIX"""
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Optional

logger = logging.getLogger(__name__)


class TimezoneValidationError(Exception):
    """Raised when timezone validation fails"""
    pass


def validate_timezone(tz_string: str) -> str:
    if not tz_string or not isinstance(tz_string, str):
        raise TimezoneValidationError("Timezone cannot be empty")

    tz_string = tz_string.strip()

    try:
        zi = ZoneInfo(tz_string)
        return str(zi)

    except ZoneInfoNotFoundError:
        raise TimezoneValidationError(
            f"Invalid timezone: '{tz_string}'. "
            f"Please use IANA timezone names (e.g., 'Asia/Karachi', 'UTC', 'America/New_York')"
        )
    except Exception as e:
        raise TimezoneValidationError(f"Timezone validation failed: {str(e)}")


def get_user_timezone(user_profile) -> str:
    if not user_profile or not user_profile.timezone:
        return "UTC"

    try:
        return validate_timezone(user_profile.timezone)
    except TimezoneValidationError:
        logger.warning(f"Invalid timezone in profile: {user_profile.timezone}, defaulting to UTC")
        return "UTC"


def convert_local_to_utc(local_time_str: str, timezone: str):
    from datetime import datetime

    try:
        tz = ZoneInfo(validate_timezone(timezone))
        local_dt = datetime.fromisoformat(local_time_str)
        local_dt = local_dt.replace(tzinfo=tz)
        utc_dt = local_dt.astimezone(ZoneInfo("UTC"))

        logger.info(
            f"Converted local time",
            extra={
                "local": local_time_str,
                "timezone": timezone,
                "utc": utc_dt.isoformat(),
            }
        )

        return utc_dt

    except TimezoneValidationError as e:
        logger.error(f"Timezone validation error: {e}")
        raise
    except Exception as e:
        logger.error(f"Time conversion error: {e}")
        raise


def convert_utc_to_local(utc_dt, timezone: str):
    try:
        tz = ZoneInfo(validate_timezone(timezone))
        local_dt = utc_dt.astimezone(tz)
        return local_dt

    except Exception as e:
        logger.error(f"Time conversion error: {e}")
        return utc_dt


COMMON_TIMEZONES = {
    "karachi": "Asia/Karachi",
    "dubai": "Asia/Dubai",
    "london": "Europe/London",
    "newyork": "America/New_York",
    "los_angeles": "America/Los_Angeles",
    "tokyo": "Asia/Tokyo",
    "sydney": "Australia/Sydney",
    "singapore": "Asia/Singapore",
}


def resolve_timezone_shortcut(input_str: str) -> Optional[str]:
    input_lower = (input_str or "").lower().strip()

    for shortcut, iana_name in COMMON_TIMEZONES.items():
        if shortcut in input_lower or iana_name.lower() in input_lower:
            try:
                validate_timezone(iana_name)
                return iana_name
            except TimezoneValidationError:
                continue

    try:
        return validate_timezone(input_str)
    except TimezoneValidationError:
        return None