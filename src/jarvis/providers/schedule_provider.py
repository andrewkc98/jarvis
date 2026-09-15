"""Read upcoming events from macOS Calendar through EventKit."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from datetime import datetime, timezone
from threading import Event, Lock
import time

import EventKit
from Foundation import NSDate


_NOT_DETERMINED = 0
_FULL_ACCESS = 3
_CALLER_TIMEOUT_SECONDS = 35.0
_COOLDOWN_SECONDS = 30.0

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-eventkit")
_caller_lock = Lock()
_inflight: Future[list[dict]] | None = None
_monotonic = time.monotonic


class _OwnerState:
    def __init__(self) -> None:
        self.store: object | None = None
        self.cooldown_category: str | None = None
        self.cooldown_until = 0.0

    def invalidate(self) -> None:
        self.store = None

    def cooldown(self, category: str) -> None:
        self.cooldown_category = category
        self.cooldown_until = _monotonic() + _COOLDOWN_SECONDS

    def clear_cooldown(self) -> None:
        self.cooldown_category = None
        self.cooldown_until = 0.0


_owner = _OwnerState()


def _authorization_status() -> int:
    """Read canonical EventKit event authorization without touching an instance."""
    try:
        status_method = getattr(
            EventKit.EKEventStore, "authorizationStatusForEntityType_"
        )
        status = status_method(EventKit.EKEntityTypeEvent)
    except Exception as exc:
        raise RuntimeError("Calendar access is temporarily unavailable") from exc
    if isinstance(status, bool) or not isinstance(status, int):
        raise PermissionError("Calendar access is not authorized")
    return status


def _authorized() -> bool:
    return _authorization_status() == _FULL_ACCESS


def _request_access(store: object) -> None:
    completed = Event()
    result = {"granted": False, "had_error": False}

    def completion(granted: bool, error: object) -> None:
        result["granted"] = bool(granted)
        result["had_error"] = error is not None
        completed.set()

    request = getattr(store, "requestFullAccessToEventsWithCompletion_", None)
    if not callable(request):
        request = getattr(store, "requestAccessToEntityType_completion_", None)
        if not callable(request):
            raise RuntimeError("Calendar access is temporarily unavailable")
        try:
            request(EventKit.EKEntityTypeEvent, completion)
        except Exception as exc:
            raise RuntimeError("Calendar access is temporarily unavailable") from exc
    else:
        try:
            request(completion)
        except Exception as exc:
            raise RuntimeError("Calendar access is temporarily unavailable") from exc
    if not completed.wait(timeout=30):
        raise RuntimeError("Calendar access is temporarily unavailable")
    if result["had_error"]:
        raise RuntimeError("Calendar access is temporarily unavailable")
    if not result["granted"]:
        raise PermissionError("Calendar access is not authorized")


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


def _new_store() -> object:
    try:
        return EventKit.EKEventStore.alloc().init()
    except Exception as exc:
        _owner.invalidate()
        _owner.cooldown("store")
        raise RuntimeError("Calendar access is temporarily unavailable") from exc


def _ensure_authorized_store() -> object:
    try:
        status = _authorization_status()
    except RuntimeError:
        _owner.invalidate()
        _owner.cooldown("status")
        raise

    if status == _FULL_ACCESS:
        _owner.clear_cooldown()
        if _owner.store is None:
            _owner.store = _new_store()
        return _owner.store

    if status != _NOT_DETERMINED:
        raise PermissionError("Calendar access is not authorized")

    now = _monotonic()
    if _owner.cooldown_category is not None and now < _owner.cooldown_until:
        raise RuntimeError("Calendar access is temporarily unavailable")
    if _owner.cooldown_category is not None:
        _owner.clear_cooldown()

    # A fresh store is required after an abandoned request/native failure.
    _owner.invalidate()
    store = _new_store()
    _owner.store = store
    try:
        _request_access(store)
    except PermissionError:
        # A user denial is a normal permission result: retain the store and do not
        # apply the native-error cooldown.
        raise
    except RuntimeError:
        _owner.invalidate()
        _owner.cooldown("access")
        raise

    try:
        canonical = _authorization_status()
    except RuntimeError:
        _owner.invalidate()
        _owner.cooldown("status")
        raise
    if canonical != _FULL_ACCESS:
        # A true callback grant without canonical full access is not trusted.
        raise PermissionError("Calendar access is not authorized")
    _owner.clear_cooldown()
    return store


def _owner_snapshot() -> list[dict]:
    store = _ensure_authorized_store()
    try:
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
                "all_day": bool(event.isAllDay()),
            }
            for event in events
        ]
    except Exception:
        _owner.invalidate()
        raise


def _clear_inflight(completed: Future[list[dict]]) -> None:
    global _inflight
    with _caller_lock:
        if _inflight is completed:
            _inflight = None


def _wait_future(future: Future[list[dict]], timeout: float) -> list[dict]:
    return future.result(timeout=timeout)


def get_upcoming_events(limit: int = 5) -> list[dict]:
    """Return up to ``limit`` events occurring in the next 14 days."""
    if limit <= 0:
        return []

    global _inflight
    with _caller_lock:
        if _inflight is None:
            future = _executor.submit(_owner_snapshot)
            _inflight = future
            register_callback = True
        else:
            future = _inflight
            register_callback = False

    # A completed Future invokes callbacks synchronously. Register only after
    # releasing _caller_lock so that _clear_inflight cannot deadlock, while the
    # initiating caller keeps its local future even if the callback clears the slot.
    if register_callback:
        future.add_done_callback(_clear_inflight)

    try:
        snapshot = _wait_future(future, _CALLER_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        raise RuntimeError("Calendar access is temporarily unavailable") from exc

    return [dict(event) for event in snapshot[:limit]]


def _reset_for_tests() -> None:
    """Reset owner-thread state for deterministic tests; never used in production."""
    reset = _executor.submit(_reset_owner)
    reset.result(timeout=_CALLER_TIMEOUT_SECONDS)
    with _caller_lock:
        global _inflight
        if _inflight is not None and _inflight.done():
            _inflight = None


def _reset_owner() -> None:
    _owner.invalidate()
    _owner.clear_cooldown()
