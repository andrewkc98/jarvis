"""Read upcoming events from macOS Calendar through EventKit."""

from __future__ import annotations

from datetime import datetime, timezone
from threading import Event

import EventKit
from Foundation import NSDate


def _authorized(store: object) -> bool:
    """Return whether the EventKit store has event access.

    Older EventKit bindings expose ``accessGrantedForEntityType_`` while some
    test doubles and SDKs expose the authorization-status method.
    """
    entity_type = EventKit.EKEntityTypeEvent
    access_method = getattr(store, "accessGrantedForEntityType_", None)
    if callable(access_method):
        access = access_method(entity_type)
        if isinstance(access, bool):
            return access

    status_method = getattr(store, "authorizationStatusForEntityType_", None)
    if callable(status_method):
        status = status_method(entity_type)
        return status in {
            EventKit.EKAuthorizationStatusAuthorized,
            EventKit.EKAuthorizationStatusFullAccess,
        }

    return bool(access_method(entity_type))


def _request_access(store: object) -> None:
    completed = Event()
    result = {"granted": False, "error": None}

    def completion(granted: bool, error: object) -> None:
        result["granted"] = bool(granted)
        result["error"] = error
        completed.set()

    store.requestAccessToEntityType_completion_(
        EventKit.EKEntityTypeEvent, completion
    )
    if not completed.wait(timeout=30):
        raise RuntimeError("Timed out waiting for Calendar access response")
    if result["error"] is not None:
        error = result["error"]
        if isinstance(error, BaseException):
            raise error
        description = getattr(error, "localizedDescription", None)
        if callable(description):
            description = description()
        if not description:
            description = str(error)
        raise RuntimeError(f"Calendar access request failed: {description}")
    if not result["granted"]:
        raise PermissionError("Calendar access was denied")


def _iso8601(date: object) -> str:
    if isinstance(date, datetime):
        value = date
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return datetime.fromtimestamp(
        float(date.timeIntervalSince1970()), tz=timezone.utc
    ).isoformat()


def _timestamp(date: object) -> float:
    if isinstance(date, datetime):
        value = date
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    return float(date.timeIntervalSince1970())


def get_upcoming_events(limit: int = 5) -> list[dict]:
    """Return up to ``limit`` events occurring in the next 14 days."""
    if limit <= 0:
        return []

    store = EventKit.EKEventStore.alloc().init()
    if not _authorized(store):
        _request_access(store)

    start = NSDate.date()
    end = NSDate.dateWithTimeIntervalSinceNow_(14 * 24 * 60 * 60)
    calendars = store.calendarsForEntityType_(EventKit.EKEntityTypeEvent)
    predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
        start, end, calendars
    )
    events = list(store.eventsMatchingPredicate_(predicate))
    events.sort(key=lambda event: _timestamp(event.startDate()))

    return [
        {
            "summary": str(event.title() or ""),
            "start": _iso8601(event.startDate()),
            "end": _iso8601(event.endDate()),
        }
        for event in events[:limit]
    ]
