from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from jarvis.providers.schedule_provider import get_upcoming_events
from jarvis.providers.vault_provider import (
    get_daily_note,
    get_daily_note_and_task_count,
    get_open_task_count,
)


def _event(title: str, start: datetime, end: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        title=lambda: title,
        startDate=lambda: start,
        endDate=lambda: end,
    )


def _store(events, authorized=True):
    store = MagicMock()
    store.accessGrantedForEntityType_.return_value = authorized
    store.calendarsForEntityType_.return_value = ["calendar"]
    store.eventsMatchingPredicate_.return_value = events
    return store


def test_get_upcoming_events_returns_sorted_events_and_respects_limit():
    late = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)
    early = datetime(2026, 9, 2, 9, tzinfo=timezone.utc)
    store = _store(
        [_event("Later", late, late), _event("Earlier", early, early)]
    )

    with patch("jarvis.providers.schedule_provider.EventKit.EKEventStore") as cls:
        cls.alloc.return_value.init.return_value = store
        result = get_upcoming_events(limit=1)

    assert result == [
        {
            "summary": "Earlier",
            "start": "2026-09-02T09:00:00+00:00",
            "end": "2026-09-02T09:00:00+00:00",
        }
    ]
    store.requestAccessToEntityType_completion_.assert_not_called()


def test_get_upcoming_events_requests_access_when_not_authorized():
    start = datetime(2026, 9, 2, 9, tzinfo=timezone.utc)
    store = _store([_event("Meeting", start, start)], authorized=False)

    def request(entity_type, completion):
        completion(True, None)

    store.requestAccessToEntityType_completion_.side_effect = request
    with patch("jarvis.providers.schedule_provider.EventKit.EKEventStore") as cls:
        cls.alloc.return_value.init.return_value = store
        result = get_upcoming_events()

    assert result[0]["summary"] == "Meeting"
    store.requestAccessToEntityType_completion_.assert_called_once()


def test_get_upcoming_events_normalizes_non_exception_access_error():
    store = _store([], authorized=False)

    def request(entity_type, completion):
        completion(False, "Calendar permission request failed")

    store.requestAccessToEntityType_completion_.side_effect = request
    with patch("jarvis.providers.schedule_provider.EventKit.EKEventStore") as cls:
        cls.alloc.return_value.init.return_value = store
        try:
            get_upcoming_events()
        except RuntimeError as error:
            assert "Calendar permission request failed" in str(error)
        else:
            raise AssertionError("expected Calendar access RuntimeError")


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

