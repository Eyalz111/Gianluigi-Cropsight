"""
Google Calendar API integration.

This module handles reading from Google Calendar:
- Fetching upcoming events for meeting prep
- Checking event participants
- Reading event color for CropSight filtering

Architecture:
    Gianluigi authenticates AS EYAL (using Eyal's OAuth refresh token) to
    read his calendar. This means we see Eyal's event colors, declined
    status, and everything exactly as he sees it — which is critical for
    the purple-color CropSight filter.

    The GOOGLE_CLIENT_ID/SECRET are from Gianluigi's Google Cloud project.
    The EYAL_CALENDAR_REFRESH_TOKEN is from Eyal's Google account (obtained
    via scripts/get_calendar_token.py with calendar.readonly scope).

    If EYAL_CALENDAR_REFRESH_TOKEN is not set, falls back to Gianluigi's
    own token (GOOGLE_REFRESH_TOKEN) — but colors won't be visible on
    shared calendars.

    Future: When CropSight moves to Google Workspace, replace per-user
    OAuth tokens with a service account + domain-wide delegation.

Usage:
    from services.google_calendar import calendar_service

    # Get upcoming events (as Eyal sees them, with colors)
    events = await calendar_service.get_upcoming_events(days=7)

    # Get specific event details
    event = await calendar_service.get_event(event_id)
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from config.settings import settings

logger = logging.getLogger(__name__)


class GoogleCalendarService:
    """
    Service for Google Calendar API operations.

    Reads Eyal's calendar using his OAuth token so we see his colors.
    """

    def __init__(self):
        self._service = None
        self._credentials: Credentials | None = None
        self._using_eyal_token: bool = False

    @property
    def service(self):
        """Lazy initialization of Calendar API service."""
        if self._service is None:
            self._service = self._build_service()
        return self._service

    def _build_service(self):
        """Build the Google Calendar API service.

        Uses Eyal's refresh token if available (sees colors),
        otherwise falls back to Gianluigi's token (no colors on shared calendars).
        """
        if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
            raise RuntimeError("Google OAuth credentials not configured")

        # Prefer Eyal's token — sees his calendar colors
        refresh_token = settings.EYAL_CALENDAR_REFRESH_TOKEN
        if refresh_token:
            self._using_eyal_token = True
            logger.info("Calendar: using Eyal's OAuth token (colors visible)")
        else:
            refresh_token = settings.GOOGLE_REFRESH_TOKEN
            if not refresh_token:
                raise RuntimeError(
                    "No calendar refresh token configured. "
                    "Run: python scripts/get_calendar_token.py"
                )
            self._using_eyal_token = False
            logger.warning(
                "Calendar: using Gianluigi's token (colors NOT visible). "
                "Set EYAL_CALENDAR_REFRESH_TOKEN for full access."
            )

        self._credentials = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.GOOGLE_CLIENT_ID,
            client_secret=settings.GOOGLE_CLIENT_SECRET,
            scopes=["https://www.googleapis.com/auth/calendar.readonly"],
        )

        if self._credentials.expired or not self._credentials.token:
            self._credentials.refresh(Request())

        return build("calendar", "v3", credentials=self._credentials)

    async def authenticate(self) -> bool:
        """Verify calendar API access."""
        try:
            _ = self.service
            mode = "as Eyal" if self._using_eyal_token else "as Gianluigi (no colors)"
            logger.info(f"Google Calendar API authentication successful ({mode})")
            return True
        except Exception as e:
            logger.error(f"Google Calendar API authentication failed: {e}")
            return False

    # =========================================================================
    # Reading Events
    # =========================================================================

    async def get_upcoming_events(
        self,
        days: int = 7,
        max_results: int = 50
    ) -> list[dict]:
        """Get upcoming calendar events."""
        try:
            now = datetime.now(timezone.utc)
            time_max = now + timedelta(days=days)

            events_result = self.service.events().list(
                calendarId="primary",
                timeMin=now.isoformat(),
                timeMax=time_max.isoformat(),
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
            ).execute()

            events = events_result.get("items", [])
            events = [e for e in events if not self._is_declined_by_owner(e)]
            parsed_events = [self._parse_event(e) for e in events]

            logger.info(f"Retrieved {len(parsed_events)} upcoming events")
            return parsed_events

        except Exception as e:
            logger.error(f"Error getting upcoming events: {e}")
            return []

    async def get_event(self, event_id: str) -> dict | None:
        """Get details for a specific calendar event."""
        try:
            event = self.service.events().get(
                calendarId="primary",
                eventId=event_id
            ).execute()

            return self._parse_event(event)

        except Exception as e:
            logger.error(f"Error getting event {event_id}: {e}")
            return None

    async def get_events_for_date(self, date: datetime) -> list[dict]:
        """Get all events for a specific date."""
        try:
            start_of_day = date.replace(
                hour=0, minute=0, second=0, microsecond=0,
                tzinfo=timezone.utc
            )
            end_of_day = start_of_day + timedelta(days=1)

            events_result = self.service.events().list(
                calendarId="primary",
                timeMin=start_of_day.isoformat(),
                timeMax=end_of_day.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            ).execute()

            events = events_result.get("items", [])
            events = [e for e in events if not self._is_declined_by_owner(e)]
            parsed_events = [self._parse_event(e) for e in events]

            logger.info(f"Found {len(parsed_events)} events for {date.date()}")
            return parsed_events

        except Exception as e:
            logger.error(f"Error getting events for date {date}: {e}")
            return []

    async def get_todays_events(self) -> list[dict]:
        """Get all events for today."""
        return await self.get_events_for_date(datetime.now(timezone.utc))

    async def get_tomorrows_events(self) -> list[dict]:
        """Get all events for tomorrow."""
        tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
        return await self.get_events_for_date(tomorrow)

    async def get_events_needing_prep(
        self,
        hours_ahead: int = 24
    ) -> list[dict]:
        """Get events within the next N hours that need prep documents."""
        try:
            now = datetime.now(timezone.utc)
            cutoff = now + timedelta(hours=hours_ahead)

            events_result = self.service.events().list(
                calendarId="primary",
                timeMin=now.isoformat(),
                timeMax=cutoff.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            ).execute()

            events = events_result.get("items", [])
            events = [e for e in events if not self._is_declined_by_owner(e)]
            parsed_events = [self._parse_event(e) for e in events]

            return parsed_events

        except Exception as e:
            logger.error(f"Error getting events needing prep: {e}")
            return []

    # =========================================================================
    # Event Details
    # =========================================================================

    async def get_event_attendees(self, event_id: str) -> list[dict]:
        """Get attendee list for an event."""
        event = await self.get_event(event_id)
        if not event:
            return []
        return event.get("attendees", [])

    async def get_event_color(self, event_id: str) -> str | None:
        """Get the color ID for an event (visible when using Eyal's token)."""
        event = await self.get_event(event_id)
        if not event:
            return None
        return event.get("color_id")

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _is_declined_by_owner(self, event: dict) -> bool:
        """Check if the calendar owner declined this event."""
        for attendee in event.get("attendees", []):
            if attendee.get("self", False):
                return attendee.get("responseStatus") == "declined"
        return False

    def _parse_event(self, event: dict) -> dict:
        """Parse a raw Google Calendar event into a cleaner format."""
        start = event.get("start", {})
        end = event.get("end", {})

        start_time = start.get("dateTime") or start.get("date")
        end_time = end.get("dateTime") or end.get("date")

        attendees = []
        for attendee in event.get("attendees", []):
            attendees.append({
                "email": attendee.get("email", ""),
                "displayName": attendee.get("displayName", ""),
                "responseStatus": attendee.get("responseStatus", "needsAction"),
                "organizer": attendee.get("organizer", False),
            })

        return {
            "id": event.get("id"),
            "title": event.get("summary", "Untitled"),
            "description": event.get("description", ""),
            "start": start_time,
            "end": end_time,
            "start_timezone": start.get("timeZone"),
            "end_timezone": end.get("timeZone"),
            "is_all_day": "date" in start and "dateTime" not in start,
            "location": event.get("location", ""),
            "attendees": attendees,
            "color_id": event.get("colorId"),
            "organizer": event.get("organizer", {}).get("email", ""),
            "html_link": event.get("htmlLink", ""),
            "status": event.get("status", "confirmed"),
            "recurring_event_id": event.get("recurringEventId"),
        }

    def _format_datetime_for_api(self, dt: datetime) -> str:
        """Format a datetime for the Google Calendar API."""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()


# Singleton instance
calendar_service = GoogleCalendarService()
