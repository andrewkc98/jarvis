from datetime import datetime, timezone
from concurrent.futures import Future
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from jarvis.providers import schedule_provider
from jarvis.providers.schedule_provider import get_upcoming_events
from jarvis.providers.vault_provider import (
    get_daily_note,
    get_daily_note_and_task_count,
    get_open_task_count,
)


def setup_function():
    schedule_provider._reset_for_tests()


def _event(
    title: str, start: datetime, end: datetime, all_day: bool = False
) -> SimpleNamespace:
    return SimpleNamespace(
        title=lambda: title,
        startDate=lambda: start,
        endDate=lambda: end,
        isAllDay=lambda: all_day,
    )


def _store(events):
    store = MagicMock()
    store.calendarsForEntityType_.return_value = ["calendar"]
    store.eventsMatchingPredicate_.return_value = events
    return store


def test_get_upcoming_events_returns_sorted_events_and_respects_limit():
    late = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)
    early = datetime(2026, 9, 2, 9, tzinfo=timezone.utc)
    store = _store([_event("Later", late, late), _event("Earlier", early, early)])

    with patch("jarvis.providers.schedule_provider.EventKit.EKEventStore") as cls:
        cls.authorizationStatusForEntityType_.return_value = 3
        cls.alloc.return_value.init.return_value = store
        result = get_upcoming_events(limit=1)

    assert result == [
        {
            "summary": "Earlier",
            "start": "2026-09-02T09:00:00+00:00",
            "end": "2026-09-02T09:00:00+00:00",
            "all_day": False,
        }
    ]
    store.requestAccessToEntityType_completion_.assert_not_called()


@pytest.mark.parametrize("all_day", [False, True])
def test_get_upcoming_events_serializes_authoritative_all_day_boolean(all_day):
    start = datetime(2026, 9, 2, 9, tzinfo=timezone.utc)
    store = _store([_event("Meeting", start, start, all_day=all_day)])

    with patch("jarvis.providers.schedule_provider.EventKit.EKEventStore") as cls:
        cls.authorizationStatusForEntityType_.return_value = 3
        cls.alloc.return_value.init.return_value = store
        result = get_upcoming_events()

    assert result == [
        {
            "summary": "Meeting",
            "start": "2026-09-02T09:00:00+00:00",
            "end": "2026-09-02T09:00:00+00:00",
            "all_day": all_day,
        }
    ]


def test_get_upcoming_events_requests_access_when_not_authorized():
    start = datetime(2026, 9, 2, 9, tzinfo=timezone.utc)
    store = _store([_event("Meeting", start, start)])

    def request(entity_type, completion):
        completion(True, None)

    with patch("jarvis.providers.schedule_provider.EventKit.EKEventStore") as cls:
        cls.authorizationStatusForEntityType_.side_effect = [0, 3]
        del store.requestFullAccessToEventsWithCompletion_
        store.requestAccessToEntityType_completion_.side_effect = request
        cls.alloc.return_value.init.return_value = store
        result = get_upcoming_events()

    assert result[0]["summary"] == "Meeting"
    store.requestAccessToEntityType_completion_.assert_called_once()


def test_get_upcoming_events_normalizes_non_exception_access_error():
    store = _store([])

    def request(entity_type, completion):
        completion(False, "Calendar permission request failed")

    with patch("jarvis.providers.schedule_provider.EventKit.EKEventStore") as cls:
        cls.authorizationStatusForEntityType_.return_value = 0
        del store.requestFullAccessToEventsWithCompletion_
        store.requestAccessToEntityType_completion_.side_effect = request
        cls.alloc.return_value.init.return_value = store
        try:
            get_upcoming_events()
        except RuntimeError as error:
            assert str(error) == "Calendar access is temporarily unavailable"
        else:
            raise AssertionError("expected Calendar access RuntimeError")


@pytest.mark.parametrize("authorization_status", [1, 2, 4, 99, None])
def test_non_readable_authorization_status_fails_without_store_or_prompt(
    authorization_status,
):
    with patch("jarvis.providers.schedule_provider.EventKit.EKEventStore") as cls:
        cls.authorizationStatusForEntityType_.return_value = authorization_status
        with pytest.raises(PermissionError, match="^Calendar access is not authorized$"):
            get_upcoming_events()
    cls.alloc.assert_not_called()


def test_authorization_status_query_failure_is_generic_and_does_not_prompt():
    with patch("jarvis.providers.schedule_provider.EventKit.EKEventStore") as cls:
        cls.authorizationStatusForEntityType_.side_effect = RuntimeError(
            "native secret"
        )
        with pytest.raises(
            RuntimeError, match="^Calendar access is temporarily unavailable$"
        ):
            get_upcoming_events()
    cls.alloc.assert_not_called()


def test_full_access_skips_access_request():
    store = _store([])
    with patch("jarvis.providers.schedule_provider.EventKit.EKEventStore") as cls:
        cls.authorizationStatusForEntityType_.return_value = 3
        cls.alloc.return_value.init.return_value = store
        get_upcoming_events()

    store.requestFullAccessToEventsWithCompletion_.assert_not_called()
    store.requestAccessToEntityType_completion_.assert_not_called()


def test_eventkit_store_is_reused_on_one_owner_thread_and_results_are_copied():
    store = _store([_event("Meeting", datetime(2026, 9, 2, 9, tzinfo=timezone.utc), datetime(2026, 9, 2, 9, tzinfo=timezone.utc))])
    threads = []
    with patch("jarvis.providers.schedule_provider.EventKit.EKEventStore") as cls:
        cls.authorizationStatusForEntityType_.return_value = 3
        cls.alloc.return_value.init.return_value = store
        store.calendarsForEntityType_.side_effect = lambda *args: threads.append(threading.get_ident()) or ["calendar"]
        first = get_upcoming_events(limit=1)
        second = get_upcoming_events(limit=1)

    assert cls.alloc.call_count == 1
    assert len(set(threads)) == 1
    assert threads[0] != threading.get_ident()
    assert first == second
    first[0]["summary"] = "changed"
    assert second[0]["summary"] == "Meeting"


def test_limit_zero_does_not_submit_owner_job():
    with patch.object(schedule_provider._executor, "submit") as submit:
        assert get_upcoming_events(limit=0) == []
    submit.assert_not_called()


def test_completed_owner_future_does_not_deadlock_or_hold_inflight_slot(monkeypatch):
    snapshot = [
        {
            "summary": "Meeting",
            "start": "2026-09-02T09:00:00+00:00",
            "end": "2026-09-02T10:00:00+00:00",
            "all_day": False,
        }
    ]

    class AlreadyResolvedExecutor:
        def __init__(self):
            self.submit_calls = 0

        def submit(self, owner_job):
            self.submit_calls += 1
            future = Future()
            future.set_result([dict(event) for event in snapshot])
            return future

    executor = AlreadyResolvedExecutor()
    monkeypatch.setattr(schedule_provider, "_executor", executor)
    result_box = {}

    caller = threading.Thread(
        target=lambda: result_box.setdefault("result", get_upcoming_events())
    )
    caller.start()
    caller.join(timeout=1)

    assert not caller.is_alive()
    assert result_box["result"] == snapshot
    assert schedule_provider._inflight is None
    assert schedule_provider._caller_lock.acquire(blocking=False)
    schedule_provider._caller_lock.release()

    result_box["result"][0]["summary"] = "changed"
    assert get_upcoming_events() == snapshot
    assert executor.submit_calls == 2


def test_overlapping_callers_coalesce_one_owner_job_and_do_not_overlap(monkeypatch):
    owner_started = threading.Event()
    both_callers_waiting = threading.Event()
    release_owner = threading.Event()
    owner_threads = []
    active = 0
    max_active = 0
    active_lock = threading.Lock()
    snapshot = [
        {"summary": "One", "start": "2026-09-02T09:00:00+00:00", "end": "2026-09-02T10:00:00+00:00", "all_day": False},
        {"summary": "Two", "start": "2026-09-03T09:00:00+00:00", "end": "2026-09-03T10:00:00+00:00", "all_day": False},
        {"summary": "Three", "start": "2026-09-04T09:00:00+00:00", "end": "2026-09-04T10:00:00+00:00", "all_day": False},
    ]

    def owner_job():
        nonlocal active, max_active
        owner_threads.append(threading.get_ident())
        with active_lock:
            active += 1
            max_active = max(max_active, active)
        owner_started.set()
        assert release_owner.wait(timeout=1)
        with active_lock:
            active -= 1
        return [dict(event) for event in snapshot]

    monkeypatch.setattr(schedule_provider, "_owner_snapshot", owner_job)
    wait_calls = 0
    wait_lock = threading.Lock()
    original_wait = schedule_provider._wait_future

    def wait_for_future(future, timeout):
        nonlocal wait_calls
        with wait_lock:
            wait_calls += 1
            if wait_calls == 2:
                both_callers_waiting.set()
        return original_wait(future, timeout)

    monkeypatch.setattr(schedule_provider, "_wait_future", wait_for_future)
    results = {}
    errors = {}

    def caller(name, limit):
        try:
            results[name] = get_upcoming_events(limit=limit)
        except BaseException as error:  # pragma: no cover - diagnostic only
            errors[name] = error

    first = threading.Thread(target=caller, args=("first", 1))
    second = threading.Thread(target=caller, args=("second", 3))
    first.start()
    assert owner_started.wait(timeout=1)
    second.start()
    assert both_callers_waiting.wait(timeout=1)
    release_owner.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == {}
    assert len(owner_threads) == 1
    assert max_active == 1
    assert len(results["first"]) == 1
    assert len(results["second"]) == 3
    results["first"][0]["summary"] = "changed"
    assert results["second"][0]["summary"] == "One"


def test_native_store_and_event_operations_run_only_on_owner_thread():
    native_threads = []
    all_day_threads = []
    caller_threads = []

    def native_call(value=None):
        native_threads.append(threading.get_ident())
        return value

    start = datetime(2026, 9, 2, 9, tzinfo=timezone.utc)
    event = SimpleNamespace(
        title=lambda: native_call("Meeting"),
        startDate=lambda: native_call(start),
        endDate=lambda: native_call(start),
        isAllDay=lambda: all_day_threads.append(threading.get_ident()) or False,
    )
    store = MagicMock()
    store.calendarsForEntityType_.side_effect = lambda *args: native_call(["calendar"])
    store.predicateForEventsWithStartDate_endDate_calendars_.side_effect = (
        lambda *args: native_call("predicate")
    )
    store.eventsMatchingPredicate_.side_effect = lambda *args: native_call([event])
    allocator = MagicMock()
    allocator.init.side_effect = lambda: native_call(store)

    def call_provider():
        caller_threads.append(threading.get_ident())
        return get_upcoming_events()

    with patch("jarvis.providers.schedule_provider.EventKit.EKEventStore") as cls:
        cls.authorizationStatusForEntityType_.side_effect = (
            lambda *args: native_call(3)
        )
        cls.alloc.side_effect = lambda: native_call(allocator)
        caller = threading.Thread(target=call_provider)
        caller.start()
        caller.join(timeout=1)

    assert not caller.is_alive()
    assert native_threads
    assert all_day_threads
    assert len(set(native_threads)) == 1
    assert len(set(all_day_threads)) == 1
    assert set(all_day_threads) == set(native_threads)
    assert all_day_threads[0] not in caller_threads
    assert native_threads[0] not in caller_threads


def test_is_all_day_failure_invalidates_store_and_next_call_recovers():
    broken_event = _event(
        "Broken", datetime(2026, 9, 2, 9, tzinfo=timezone.utc),
        datetime(2026, 9, 2, 10, tzinfo=timezone.utc),
    )
    broken_event.isAllDay = lambda: (_ for _ in ()).throw(
        RuntimeError("isAllDay failed")
    )
    recovered_event = _event(
        "Recovered", datetime(2026, 9, 2, 9, tzinfo=timezone.utc),
        datetime(2026, 9, 2, 10, tzinfo=timezone.utc), all_day=True,
    )
    first_store = _store([broken_event])
    second_store = _store([recovered_event])
    first_allocator = MagicMock()
    first_allocator.init.return_value = first_store
    second_allocator = MagicMock()
    second_allocator.init.return_value = second_store

    with patch("jarvis.providers.schedule_provider.EventKit.EKEventStore") as cls:
        cls.authorizationStatusForEntityType_.return_value = 3
        cls.alloc.side_effect = [first_allocator, second_allocator]
        with pytest.raises(RuntimeError, match="isAllDay failed"):
            get_upcoming_events()
        assert schedule_provider._owner.store is None
        assert get_upcoming_events() == [
            {
                "summary": "Recovered",
                "start": "2026-09-02T09:00:00+00:00",
                "end": "2026-09-02T10:00:00+00:00",
                "all_day": True,
            }
        ]


def test_caller_timeout_does_not_cancel_or_enqueue_another_owner_job(monkeypatch):
    owner_started = threading.Event()
    release_owner = threading.Event()
    owner_calls = []

    def owner_job():
        owner_calls.append(threading.get_ident())
        owner_started.set()
        assert release_owner.wait(timeout=1)
        return []

    monkeypatch.setattr(schedule_provider, "_owner_snapshot", owner_job)
    submit = schedule_provider._executor.submit
    with patch.object(schedule_provider._executor, "submit", wraps=submit) as submit_spy:
        first = threading.Thread(target=get_upcoming_events)
        first.start()
        assert owner_started.wait(timeout=1)
        with patch.object(
            schedule_provider,
            "_wait_future",
            side_effect=TimeoutError(),
        ) as wait:
            with pytest.raises(
                RuntimeError, match="^Calendar access is temporarily unavailable$"
            ):
                get_upcoming_events()
            with pytest.raises(
                RuntimeError, match="^Calendar access is temporarily unavailable$"
            ):
                get_upcoming_events()
            assert wait.call_count == 2
            assert all(call.args[1] == 35.0 for call in wait.call_args_list)
        assert submit_spy.call_count == 1
        future = schedule_provider._inflight
        assert future is not None
        assert not future.cancelled()
        release_owner.set()
        first.join(timeout=1)

    assert not first.is_alive()
    assert len(owner_calls) == 1


def test_cooldown_fails_fast_expires_and_full_access_bypasses_it(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(schedule_provider, "_monotonic", lambda: clock[0])
    store = _store([])

    with patch("jarvis.providers.schedule_provider.EventKit.EKEventStore") as cls:
        cls.authorizationStatusForEntityType_.return_value = 0
        schedule_provider._owner.cooldown("access")
        with pytest.raises(
            RuntimeError, match="^Calendar access is temporarily unavailable$"
        ):
            schedule_provider._ensure_authorized_store()
        cls.alloc.assert_not_called()

        clock[0] = 130.0
        cls.authorizationStatusForEntityType_.side_effect = [0, 3]
        cls.alloc.return_value.init.return_value = store
        store.requestFullAccessToEventsWithCompletion_.side_effect = (
            lambda completion: completion(True, None)
        )
        assert schedule_provider._ensure_authorized_store() is store
        assert schedule_provider._owner.cooldown_category is None

        schedule_provider._owner.cooldown("access")
        cls.authorizationStatusForEntityType_.side_effect = None
        cls.authorizationStatusForEntityType_.return_value = 3
        assert schedule_provider._ensure_authorized_store() is store
        assert schedule_provider._owner.cooldown_category is None


def test_callback_failure_expires_then_uses_a_fresh_store(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(schedule_provider, "_monotonic", lambda: clock[0])
    first_store = _store([])
    second_store = _store([])
    first_store.requestFullAccessToEventsWithCompletion_.side_effect = (
        lambda completion: completion(True, RuntimeError("native secret"))
    )
    second_store.requestFullAccessToEventsWithCompletion_.side_effect = (
        lambda completion: completion(True, None)
    )
    first_allocator = MagicMock()
    first_allocator.init.return_value = first_store
    second_allocator = MagicMock()
    second_allocator.init.return_value = second_store

    with patch("jarvis.providers.schedule_provider.EventKit.EKEventStore") as cls:
        cls.authorizationStatusForEntityType_.side_effect = [0, 0, 3]
        cls.alloc.side_effect = [first_allocator, second_allocator]
        with pytest.raises(RuntimeError):
            schedule_provider._ensure_authorized_store()
        assert schedule_provider._owner.store is None
        assert schedule_provider._owner.cooldown_category == "access"

        clock[0] = 131.0
        assert schedule_provider._ensure_authorized_store() is second_store
        assert cls.alloc.call_count == 2


def test_status_failure_expires_then_constructs_a_fresh_store(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(schedule_provider, "_monotonic", lambda: clock[0])
    store = _store([])
    allocator = MagicMock()
    allocator.init.return_value = store

    with patch("jarvis.providers.schedule_provider.EventKit.EKEventStore") as cls:
        cls.authorizationStatusForEntityType_.side_effect = [
            RuntimeError("native secret"),
            3,
        ]
        cls.alloc.return_value = allocator
        with pytest.raises(RuntimeError):
            schedule_provider._ensure_authorized_store()
        assert schedule_provider._owner.store is None
        assert schedule_provider._owner.cooldown_category == "status"

        clock[0] = 131.0
        assert schedule_provider._ensure_authorized_store() is store
        assert cls.alloc.call_count == 1


def test_timeout_failure_expires_then_constructs_a_fresh_store(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(schedule_provider, "_monotonic", lambda: clock[0])
    first_store = _store([])
    second_store = _store([])
    first_allocator = MagicMock()
    first_allocator.init.return_value = first_store
    second_allocator = MagicMock()
    second_allocator.init.return_value = second_store

    class NeverCompletes:
        def set(self):
            pass

        def wait(self, timeout):
            return False

    real_event = schedule_provider.Event
    first_store.requestFullAccessToEventsWithCompletion_.side_effect = lambda completion: None
    second_store.requestFullAccessToEventsWithCompletion_.side_effect = (
        lambda completion: completion(True, None)
    )
    with patch("jarvis.providers.schedule_provider.EventKit.EKEventStore") as cls:
        cls.authorizationStatusForEntityType_.side_effect = [0, 0, 3]
        cls.alloc.side_effect = [first_allocator, second_allocator]
        monkeypatch.setattr(schedule_provider, "Event", NeverCompletes)
        with pytest.raises(RuntimeError):
            schedule_provider._ensure_authorized_store()
        assert schedule_provider._owner.store is None
        assert schedule_provider._owner.cooldown_category == "access"

        monkeypatch.setattr(schedule_provider, "Event", real_event)
        clock[0] = 131.0
        assert schedule_provider._ensure_authorized_store() is second_store
        assert cls.alloc.call_count == 2


def test_query_failure_invalidates_store_and_next_call_uses_a_fresh_store():
    first_store = _store([])
    first_store.eventsMatchingPredicate_.side_effect = RuntimeError("query failed")
    second_store = _store(
        [_event("Recovered", datetime(2026, 9, 2, 9, tzinfo=timezone.utc), datetime(2026, 9, 2, 10, tzinfo=timezone.utc))]
    )
    first_allocator = MagicMock()
    first_allocator.init.return_value = first_store
    second_allocator = MagicMock()
    second_allocator.init.return_value = second_store

    with patch("jarvis.providers.schedule_provider.EventKit.EKEventStore") as cls:
        cls.authorizationStatusForEntityType_.return_value = 3
        cls.alloc.side_effect = [first_allocator, second_allocator]
        with pytest.raises(RuntimeError, match="query failed"):
            get_upcoming_events()
        assert schedule_provider._owner.store is None
        assert get_upcoming_events()[0]["summary"] == "Recovered"
        assert cls.alloc.call_count == 2


def test_conversion_failure_invalidates_store_and_next_call_uses_a_fresh_store():
    def broken_start():
        raise ValueError("conversion failed")

    broken = SimpleNamespace(
        title=lambda: "Broken",
        startDate=broken_start,
        endDate=lambda: datetime(2026, 9, 2, 10, tzinfo=timezone.utc),
    )
    first_store = _store([broken])
    second_store = _store(
        [_event("Recovered", datetime(2026, 9, 2, 9, tzinfo=timezone.utc), datetime(2026, 9, 2, 10, tzinfo=timezone.utc))]
    )
    first_allocator = MagicMock()
    first_allocator.init.return_value = first_store
    second_allocator = MagicMock()
    second_allocator.init.return_value = second_store

    with patch("jarvis.providers.schedule_provider.EventKit.EKEventStore") as cls:
        cls.authorizationStatusForEntityType_.return_value = 3
        cls.alloc.side_effect = [first_allocator, second_allocator]
        with pytest.raises(ValueError, match="conversion failed"):
            get_upcoming_events()
        assert schedule_provider._owner.store is None
        assert get_upcoming_events()[0]["summary"] == "Recovered"
        assert cls.alloc.call_count == 2


def test_false_grant_preserves_store_without_cooldown(monkeypatch):
    store = _store([])
    store.requestFullAccessToEventsWithCompletion_.side_effect = (
        lambda completion: completion(False, None)
    )
    with patch("jarvis.providers.schedule_provider.EventKit.EKEventStore") as cls:
        cls.authorizationStatusForEntityType_.return_value = 0
        cls.alloc.return_value.init.return_value = store
        with pytest.raises(PermissionError):
            schedule_provider._ensure_authorized_store()

    assert schedule_provider._owner.store is store
    assert schedule_provider._owner.cooldown_category is None


def test_late_callback_does_not_leak_into_a_later_access_attempt(monkeypatch):
    callbacks = []
    waits = [False, True]

    class ControlledEvent:
        def set(self):
            pass

        def wait(self, timeout):
            return waits.pop(0)

    def request(completion):
        callbacks.append(completion)
        if len(callbacks) == 2:
            completion(False, None)

    store = _store([])
    store.requestFullAccessToEventsWithCompletion_.side_effect = request
    monkeypatch.setattr(schedule_provider, "Event", ControlledEvent)

    with pytest.raises(RuntimeError):
        schedule_provider._request_access(store)
    callbacks[0](True, None)
    with pytest.raises(PermissionError):
        schedule_provider._request_access(store)
    assert len(callbacks) == 2


def test_nonreadable_canonical_grant_is_shared_without_query_or_duplicate_prompt():
    canonical_started = threading.Event()
    release_canonical = threading.Event()
    request_calls = []
    status_calls = 0
    store = _store([])

    def status(*args):
        nonlocal status_calls
        status_calls += 1
        if status_calls == 1:
            return 0
        canonical_started.set()
        assert release_canonical.wait(timeout=1)
        return 2

    def request(completion):
        request_calls.append(completion)
        completion(True, None)

    store.requestFullAccessToEventsWithCompletion_.side_effect = request
    results = {}

    def caller(name):
        try:
            get_upcoming_events()
        except BaseException as error:  # pragma: no cover - diagnostic only
            results[name] = error

    with patch("jarvis.providers.schedule_provider.EventKit.EKEventStore") as cls:
        cls.authorizationStatusForEntityType_.side_effect = status
        cls.alloc.return_value.init.return_value = store
        first = threading.Thread(target=caller, args=("first",))
        first.start()
        assert canonical_started.wait(timeout=1)
        second = threading.Thread(target=caller, args=("second",))
        second.start()
        second.join(timeout=0.1)
        assert second.is_alive()
        release_canonical.set()
        first.join(timeout=1)
        second.join(timeout=1)

    assert not first.is_alive()
    assert not second.is_alive()
    assert all(isinstance(error, PermissionError) for error in results.values())
    assert len(request_calls) == 1
    store.eventsMatchingPredicate_.assert_not_called()


def test_owner_snapshot_invalidates_store_for_query_exception():
    store = _store([])
    store.eventsMatchingPredicate_.side_effect = RuntimeError("query failed")
    schedule_provider._owner.store = store

    with patch.object(schedule_provider, "_ensure_authorized_store", return_value=store):
        with pytest.raises(RuntimeError, match="query failed"):
            schedule_provider._owner_snapshot()

    assert schedule_provider._owner.store is None


def test_owner_snapshot_does_not_intercept_process_control_exception():
    store = _store([])
    store.eventsMatchingPredicate_.side_effect = KeyboardInterrupt()
    schedule_provider._owner.store = store

    with patch.object(schedule_provider, "_ensure_authorized_store", return_value=store):
        with pytest.raises(KeyboardInterrupt):
            schedule_provider._owner_snapshot()

    assert schedule_provider._owner.store is not None


def test_full_access_request_is_preferred_and_rechecked():
    store = _store([])

    def request(completion):
        completion(True, None)

    store.requestFullAccessToEventsWithCompletion_.side_effect = request
    with patch("jarvis.providers.schedule_provider.EventKit.EKEventStore") as cls:
        cls.authorizationStatusForEntityType_.side_effect = [0, 3]
        cls.alloc.return_value.init.return_value = store
        get_upcoming_events()

    store.requestFullAccessToEventsWithCompletion_.assert_called_once()
    store.requestAccessToEntityType_completion_.assert_not_called()


def test_callback_error_is_generic_and_native_error_is_not_retained():
    store = _store([])
    native_error = RuntimeError("calendar secret")

    def request(completion):
        completion(True, native_error)

    store.requestFullAccessToEventsWithCompletion_.side_effect = request
    with patch("jarvis.providers.schedule_provider.EventKit.EKEventStore") as cls:
        cls.authorizationStatusForEntityType_.return_value = 0
        cls.alloc.return_value.init.return_value = store
        with pytest.raises(
            RuntimeError, match="^Calendar access is temporarily unavailable$"
        ) as caught:
            get_upcoming_events()

    assert "calendar secret" not in str(caught.value)
    store.eventsMatchingPredicate_.assert_not_called()


def test_false_grant_is_generic_permission_failure_without_duplicate_request():
    store = _store([])

    def request(completion):
        completion(False, None)

    store.requestFullAccessToEventsWithCompletion_.side_effect = request
    with patch("jarvis.providers.schedule_provider.EventKit.EKEventStore") as cls:
        cls.authorizationStatusForEntityType_.return_value = 0
        cls.alloc.return_value.init.return_value = store
        with pytest.raises(PermissionError, match="^Calendar access is not authorized$"):
            get_upcoming_events()

    cls.authorizationStatusForEntityType_.assert_called_once()
    store.requestFullAccessToEventsWithCompletion_.assert_called_once()
    store.eventsMatchingPredicate_.assert_not_called()


def test_access_request_timeout_is_generic(monkeypatch):
    store = _store([])

    def request(completion):
        pass

    class NeverCompletes:
        def set(self):
            pass

        def wait(self, timeout):
            return False

    store.requestFullAccessToEventsWithCompletion_.side_effect = request
    monkeypatch.setattr(
        "jarvis.providers.schedule_provider.Event", NeverCompletes
    )
    with patch("jarvis.providers.schedule_provider.EventKit.EKEventStore") as cls:
        cls.authorizationStatusForEntityType_.return_value = 0
        cls.alloc.return_value.init.return_value = store
        with pytest.raises(
            RuntimeError, match="^Calendar access is temporarily unavailable$"
        ):
            get_upcoming_events()
    store.eventsMatchingPredicate_.assert_not_called()


def test_true_grant_requires_full_access_on_canonical_recheck():
    store = _store([])

    def request(completion):
        completion(True, None)

    store.requestFullAccessToEventsWithCompletion_.side_effect = request
    with patch("jarvis.providers.schedule_provider.EventKit.EKEventStore") as cls:
        cls.authorizationStatusForEntityType_.side_effect = [0, 2]
        cls.alloc.return_value.init.return_value = store
        with pytest.raises(PermissionError, match="^Calendar access is not authorized$"):
            get_upcoming_events()

    assert cls.authorizationStatusForEntityType_.call_count == 2
    store.eventsMatchingPredicate_.assert_not_called()


def _httpx_client(response_text: str) -> tuple[MagicMock, MagicMock]:
    response = MagicMock()
    response.text = response_text
    client = MagicMock()
    client.get.return_value = response
    client.__enter__.return_value = client
    return client, response


def _response(status_code, json_body=None, text=""):
    response = MagicMock()
    response.status_code = status_code
    response.text = text
    if json_body is None:
        response.json.side_effect = ValueError("no JSON body")
    else:
        response.json.return_value = json_body
    return response


def test_get_daily_note_uses_configured_rest_url_and_token():
    client, response = _httpx_client("# Today\n- [ ] Write tests\n")
    with (
        patch("jarvis.providers.vault_provider.get_credential") as credential,
        patch("jarvis.providers.vault_provider.httpx.Client", return_value=client) as cls,
    ):
        credential.side_effect = lambda name: {
            "OBSIDIAN_REST_BASE_URL": "https://vault.example:27124",
            "OBSIDIAN_REST_TOKEN": "test-token",
        }[name]
        assert get_daily_note() == "# Today\n- [ ] Write tests\n"

    cls.assert_called_once_with(
        base_url="https://vault.example:27124",
        headers={"Authorization": "Bearer test-token"},
        verify=True,
        follow_redirects=True,
    )
    client.get.assert_called_once_with("/periodic/daily/")
    response.raise_for_status.assert_called_once_with()


def test_get_daily_note_uses_self_signed_default_url_when_unconfigured():
    client, _ = _httpx_client("daily note")
    with (
        patch("jarvis.providers.vault_provider.get_credential") as credential,
        patch("jarvis.providers.vault_provider.httpx.Client", return_value=client) as cls,
    ):
        def credential_value(name):
            if name == "OBSIDIAN_REST_BASE_URL":
                raise RuntimeError("Missing credential: OBSIDIAN_REST_BASE_URL")
            return "test-token"

        credential.side_effect = credential_value
        assert get_daily_note() == "daily note"

    cls.assert_called_once_with(
        base_url="https://localhost:27124",
        headers={"Authorization": "Bearer test-token"},
        verify=False,
        follow_redirects=True,
    )


def test_get_open_task_count_counts_only_unchecked_markdown_tasks():
    client, _ = _httpx_client(
        "- [ ] Open one\n- [x] Checked\n  - [ ] Indented open\n* [ ] Not a dash task\n"
    )
    with (
        patch("jarvis.providers.vault_provider.get_credential") as credential,
        patch("jarvis.providers.vault_provider.httpx.Client", return_value=client),
    ):
        credential.side_effect = lambda name: {
            "OBSIDIAN_REST_BASE_URL": "https://vault.example",
            "OBSIDIAN_REST_TOKEN": "test-token",
        }[name]
        assert get_open_task_count() == 1


def test_get_daily_note_returns_empty_string_for_missing_note_error_code():
    response = _response(404, {"errorCode": 40461, "message": "missing"})
    client = MagicMock()
    client.get.return_value = response
    client.__enter__.return_value = client
    with (
        patch("jarvis.providers.vault_provider.get_credential") as credential,
        patch("jarvis.providers.vault_provider.httpx.Client", return_value=client),
    ):
        credential.side_effect = lambda name: {
            "OBSIDIAN_REST_BASE_URL": "https://vault.example",
            "OBSIDIAN_REST_TOKEN": "test-token",
        }[name]
        assert get_daily_note() == ""
    response.raise_for_status.assert_not_called()


def test_get_open_task_count_returns_zero_for_missing_note_error_code():
    response = _response(404, {"errorCode": 40461, "message": "missing"})
    client = MagicMock()
    client.get.return_value = response
    client.__enter__.return_value = client
    with (
        patch("jarvis.providers.vault_provider.get_credential") as credential,
        patch("jarvis.providers.vault_provider.httpx.Client", return_value=client),
    ):
        credential.side_effect = lambda name: {
            "OBSIDIAN_REST_BASE_URL": "https://vault.example",
            "OBSIDIAN_REST_TOKEN": "test-token",
        }[name]
        assert get_open_task_count() == 0


def test_get_daily_note_propagates_404_with_different_error_code():
    response = _response(404, {"errorCode": 40400, "message": "missing"})
    response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "404", request=MagicMock(), response=response
    )
    client = MagicMock()
    client.get.return_value = response
    client.__enter__.return_value = client
    with (
        patch("jarvis.providers.vault_provider.get_credential") as credential,
        patch("jarvis.providers.vault_provider.httpx.Client", return_value=client),
    ):
        credential.side_effect = lambda name: {
            "OBSIDIAN_REST_BASE_URL": "https://vault.example",
            "OBSIDIAN_REST_TOKEN": "test-token",
        }[name]
        with pytest.raises(httpx.HTTPStatusError):
            get_daily_note()
    response.raise_for_status.assert_called_once_with()


def test_get_daily_note_propagates_404_with_malformed_json_body():
    response = _response(404)
    response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "404", request=MagicMock(), response=response
    )
    client = MagicMock()
    client.get.return_value = response
    client.__enter__.return_value = client
    with (
        patch("jarvis.providers.vault_provider.get_credential") as credential,
        patch("jarvis.providers.vault_provider.httpx.Client", return_value=client),
    ):
        credential.side_effect = lambda name: {
            "OBSIDIAN_REST_BASE_URL": "https://vault.example",
            "OBSIDIAN_REST_TOKEN": "test-token",
        }[name]
        with pytest.raises(httpx.HTTPStatusError):
            get_daily_note()


def test_get_daily_note_propagates_non_404_http_errors():
    response = _response(500)
    response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "500", request=MagicMock(), response=response
    )
    client = MagicMock()
    client.get.return_value = response
    client.__enter__.return_value = client
    with (
        patch("jarvis.providers.vault_provider.get_credential") as credential,
        patch("jarvis.providers.vault_provider.httpx.Client", return_value=client),
    ):
        credential.side_effect = lambda name: {
            "OBSIDIAN_REST_BASE_URL": "https://vault.example",
            "OBSIDIAN_REST_TOKEN": "test-token",
        }[name]
        with pytest.raises(httpx.HTTPStatusError):
            get_daily_note()


def test_get_daily_note_and_task_count_issues_single_request():
    note = "# Today\n- [ ] One open\n- [ ] Two open\n- [x] Checked one\n- Done\n"
    client, response = _httpx_client(note)
    with (
        patch("jarvis.providers.vault_provider.get_credential") as credential,
        patch("jarvis.providers.vault_provider.httpx.Client", return_value=client),
    ):
        credential.side_effect = lambda name: {
            "OBSIDIAN_REST_BASE_URL": "https://vault.example",
            "OBSIDIAN_REST_TOKEN": "test-token",
        }[name]
        content, count = get_daily_note_and_task_count()

    assert content == note
    assert count == 2  # two "- [ ]" lines; checked and non-checklist lines excluded
    client.get.assert_called_once_with("/periodic/daily/")


def test_get_daily_note_and_task_count_zero_for_missing_note():
    response = _response(404, {"errorCode": 40461, "message": "missing"})
    client = MagicMock()
    client.get.return_value = response
    client.__enter__.return_value = client
    with (
        patch("jarvis.providers.vault_provider.get_credential") as credential,
        patch("jarvis.providers.vault_provider.httpx.Client", return_value=client),
    ):
        credential.side_effect = lambda name: {
            "OBSIDIAN_REST_BASE_URL": "https://vault.example",
            "OBSIDIAN_REST_TOKEN": "test-token",
        }[name]
        assert get_daily_note_and_task_count() == ("", 0)
    response.raise_for_status.assert_not_called()
    client.get.assert_called_once_with("/periodic/daily/")


def test_get_daily_note_and_task_count_propagates_http_errors():
    response = _response(500)
    response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "500", request=MagicMock(), response=response
    )
    client = MagicMock()
    client.get.return_value = response
    client.__enter__.return_value = client
    with (
        patch("jarvis.providers.vault_provider.get_credential") as credential,
        patch("jarvis.providers.vault_provider.httpx.Client", return_value=client),
    ):
        credential.side_effect = lambda name: {
            "OBSIDIAN_REST_BASE_URL": "https://vault.example",
            "OBSIDIAN_REST_TOKEN": "test-token",
        }[name]
        with pytest.raises(httpx.HTTPStatusError):
            get_daily_note_and_task_count()
