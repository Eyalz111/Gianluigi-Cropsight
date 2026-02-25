"""
Google Calendar API integration.

This module handles reading from Google Calendar:
- Fetching upcoming events for meeting prep
- Checking event participants
- Reading event color for CropSight filtering

Note: v0.1 is read-only. Write access (creating events) is a v0.3 feature.

Usage:
    from services.google_calendar import calendar_service

    # Get upcoming events
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

    Read-only access for v0.1. Used for:
    - Meeting prep generation
    - CropSight meeting filtering
    """

    def __init__(self):
        """
        Initialize the Google Calendar service with credentials.
        """
        self._service = None
        self._credentials: Credentials | None = None

    @property
    def service(self):
        """
        Lazy initialization of Calendar API service.

        Uses OAuth2 credentials from settings.
        """
        if self._service is None:
            self._service = self._build_service()
        return self._service

    def _build_service(self):
        """Build the Google Calendar API service with OAuth2 credentials."""
        if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
            raise RuntimeError("Google OAuth credentials not configured")

        if not settings.GOOGLE_REFRESH_TOKEN:
            raise RuntimeError(
                "Google refresh token not configured. "
                "Run the OAuth flow to obtain a refresh token."
            )

        # Create credentials from refresh token
        self._credentials = Credentials(
            token=None,
            refresh_token=settings.GOOGLE_REFRESH_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.GOOGLE_CLIENT_ID,
            client_secret=settings.GOOGLE_CLIENT_SECRET,
            scopes=[
                "https://www.googleapis.com/auth/calendar.readonly",
            ],
        )

        # Refresh the token if needed
        if self._credentials.expired or not self._credentials.token:
            self._credentials.refresh(Request())

        return build("calendar", "v3", credentials=self._credentials)

    async def authenticate(self) -> bool:
        """
        Authenticate with Google Calendar API using OAuth2.

        Returns:
            True if authentication successful, False otherwise.
        """
        try:
            # Force service initialization to verify auth
            _ = self.service
            logger.info("Google Calendar API authentication successful")
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
        """
        Get upcoming calendar events.

        Args:
            days: Number of days to look ahead.
            max_results: Maximum number of events to return.

        Returns:
            List of event dicts with id, title, start, end, attendees, color_id.
        """
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

            # Parse each event into clean format
            parsed_events = [self._parse_event(e) for e in events]

            logger.info(f"Retrieved {len(parsed_events)} upcoming events")
            return parsed_events

        except Exception as e:
            logger.error(f"Error getting upcoming events: {e}")
            return []

    async def get_event(self, event_id: str) -> dict | None:
        """
        Get details for a specific calendar event.

        Args:
            event_id: Google Calendar event ID.

        Returns:
            Event dict with full details, or None if not found.
        """
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
        """
        Get all events for a specific date.

        Args:
            date: The date to query.

        Returns:
            List of events on that date.
        """
        try:
            # Set time to start of day
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
            parsed_events = [self._parse_event(e) for e in events]

            logger.info(f"Found {len(parsed_events)} events for {date.date()}")
            return parsed_events

        except Exception as e:
            logger.error(f"Error getting events for date {date}: {e}")
            return []

    async def get_todays_events(self) -> list[dict]:
        """
        Get all events for today.

        Returns:
            List of today's events.
        """
        return await self.get_events_for_date(datetime.now(timezone.utc))

    async def get_tomorrows_events(self) -> list[dict]:
        """
        Get all events for tomorrow.

        Returns:
            List of tomorrow's events.
        """
        tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
        return await self.get_events_for_date(tomorrow)

    async def get_events_needing_prep(
        self,
        hours_ahead: int = 24
    ) -> list[dict]:
        """
        Get events within the next N hours that need prep documents.

        Filters for CropSight meetings that don't have prep docs yet.

        Args:
            hours_ahead: How many hours ahead to check.

        Returns:
            List of events needing prep.
        """
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
            parsed_events = [self._parse_event(e) for e in events]

            # Filter to only CropSight meetings
            # (The scheduler will handle the full filter chain)
            return parsed_events

        except Exception as e:
            logger.error(f"Error getting events needing prep: {e}")
            return []

    # =========================================================================
    # Event Details
    # =========================================================================

    async def get_event_attendees(self, event_id: str) -> list[dict]:
        """
        Get attendee list for an event.

        Args:
            event_id: Google Calendar event ID.

        Returns:
            List of attendee dicts with email, displayName, responseStatus.
        """
        event = await self.get_event(event_id)
        if not event:
            return []
        return event.get("attendees", [])

    async def get_event_color(self, event_id: str) -> str | None:
        """
        Get the color ID for an event.

        Used for CropSight meeting filtering (purple = CropSight).

        Args:
            event_id: Google Calendar event ID.

        Returns:
            Color ID string, or None if no color set.
        """
        event = await self.get_event(event_id)
        if not event:
            return None
        return event.get("color_id")

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _parse_event(self, event: dict) -> dict:
        """
        Parse a raw Google Calendar event into a cleaner format.

        Args:
            event: Raw event dict from the API.

        Returns:
            Cleaned event dict with consistent field names.
        """
        # Handle all-day events vs timed events
        start = event.get("start", {})
        end = event.get("end", {})

        start_time = start.get("dateTime") or start.get("date")
        end_time = end.get("dateTime") or end.get("date")

        # Parse attendees
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
        """
        Format a datetime for the Google Calendar API.

        Args:
            dt: Datetime to format.

        Returns:
            ISO format string with timezone.
        """
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()


# Singleton instance
calendar_service = GoogleCalendarService()
