from jarvis.orchestrator.router import route


def test_routes_schedule_keyword_to_schedule():
    assert route("What's on my schedule today?") == "schedule"


def test_routes_calendar_keyword_to_schedule():
    assert route("Check my calendar") == "schedule"


def test_routes_case_insensitively():
    assert route("SCHEDULE for tomorrow") == "schedule"


def test_routes_unrelated_text_to_fallback():
    assert route("What did I write about Jarvis last week?") == "fallback"


def test_does_not_match_schedule_as_a_substring_of_reschedule():
    assert route("Can you reschedule my flight?") == "fallback"


def test_does_not_match_calendar_as_a_substring_of_calendaring():
    assert route("I need to update my calendaring preferences") == "fallback"


def test_empty_text_routes_to_fallback():
    assert route("") == "fallback"
