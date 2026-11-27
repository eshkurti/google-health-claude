from datetime import date

import pytest
import requests
from fakes import FakeResponse, FakeSession, NotJsonResponse

from google_health_claude.client import AuthError, HealthApiError, HealthClient
from google_health_claude.credentials import Credentials

CREDS = Credentials("id", "secret", "refresh")
TOKEN = FakeResponse(200, {"access_token": "tok", "expires_in": 3599})


def client(responses):
    session = FakeSession([TOKEN, *responses])
    return HealthClient(CREDS, session=session, sleep=lambda _: None), session


def test_pagination_follows_next_page_token():
    c, session = client(
        [
            FakeResponse(200, {"dataPoints": [{"a": 1}], "nextPageToken": "p2"}),
            FakeResponse(200, {"dataPoints": [{"a": 2}]}),
        ]
    )
    points = c.list_data_points("sleep", 'sleep.x >= "d"')
    assert points == [{"a": 1}, {"a": 2}]
    first, second = session.calls[1], session.calls[2]
    assert first["params"] == {"filter": 'sleep.x >= "d"'}
    assert second["params"]["pageToken"] == "p2"
    assert first["headers"]["Authorization"] == "Bearer tok"


def test_token_is_refreshed_once_and_reused():
    c, session = client([FakeResponse(200, {}), FakeResponse(200, {})])
    c.list_data_points("steps")
    c.list_data_points("steps")
    token_calls = [call for call in session.calls if "oauth2" in call["url"]]
    assert len(token_calls) == 1
    assert token_calls[0]["data"]["grant_type"] == "refresh_token"


def test_retries_on_rate_limit():
    c, _ = client([FakeResponse(429, {}), FakeResponse(200, {"dataPoints": [{"ok": True}]})])
    assert c.list_data_points("steps") == [{"ok": True}]


def test_expired_access_token_is_refreshed_after_401():
    session = FakeSession(
        [TOKEN, FakeResponse(401, {}), TOKEN, FakeResponse(200, {"dataPoints": []})]
    )
    c = HealthClient(CREDS, session=session, sleep=lambda _: None)
    assert c.list_data_points("steps") == []


def test_api_error_carries_status_and_message():
    c, _ = client([FakeResponse(403, {"error": {"message": "scope missing"}})])
    with pytest.raises(HealthApiError) as err:
        c.list_data_points("sleep")
    assert err.value.status == 403
    assert "scope missing" in str(err.value)


def test_rejected_refresh_token_raises_auth_error():
    session = FakeSession([FakeResponse(400, {"error": {"message": "invalid_grant"}})])
    c = HealthClient(CREDS, session=session, sleep=lambda _: None)
    with pytest.raises(AuthError):
        c.list_data_points("sleep")


def test_refresh_reply_without_access_token_raises_auth_error():
    session = FakeSession(
        [
            FakeResponse(200, {"access_token": None, "expires_in": 3599}),
            FakeResponse(200, {"dataPoints": []}),
        ]
    )
    c = HealthClient(CREDS, session=session, sleep=lambda _: None)
    with pytest.raises(AuthError, match="no access token"):
        c.list_data_points("steps")
    assert len(session.calls) == 1  # only the token request; no API call followed
    assert all(
        call.get("headers", {}).get("Authorization") != "Bearer None" for call in session.calls
    )


def test_daily_rollup_request_body():
    c, session = client([FakeResponse(200, {"rollupDataPoints": [{"x": 1}]})])
    points = c.daily_rollup("steps", date(2026, 11, 24), date(2026, 11, 26))
    assert points == [{"x": 1}]
    call = session.calls[1]
    assert call["method"] == "POST"
    assert call["url"].endswith("/users/me/dataTypes/steps/dataPoints:dailyRollUp")
    assert call["json"] == {
        "range": {
            "start": {"date": {"year": 2026, "month": 11, "day": 24}},
            "end": {"date": {"year": 2026, "month": 11, "day": 26}},
        },
        "windowSizeDays": 1,
    }


def test_network_errors_are_retried():
    c, _ = client(
        [requests.ConnectionError("reset"), FakeResponse(200, {"dataPoints": [{"ok": 1}]})]
    )
    assert c.list_data_points("steps") == [{"ok": 1}]


def test_network_error_during_token_refresh_is_retried():
    session = FakeSession([requests.Timeout("slow"), TOKEN, FakeResponse(200, {"dataPoints": []})])
    c = HealthClient(CREDS, session=session, sleep=lambda _: None)
    assert c.list_data_points("steps") == []


def test_persistent_network_error_becomes_api_error():
    c, _ = client([requests.Timeout("slow")] * 5)
    with pytest.raises(HealthApiError) as err:
        c.list_data_points("steps")
    assert err.value.status == 0
    assert "Timeout" in str(err.value)


def test_non_json_reply_becomes_api_error():
    c, _ = client([NotJsonResponse()])
    with pytest.raises(HealthApiError, match="not valid JSON"):
        c.list_data_points("steps")


def test_unexpected_reply_shape_is_reported_by_key_name_only():
    c, _ = client([FakeResponse(200, {"data_points": [{"bpm": 55}]})])
    with pytest.raises(HealthApiError) as err:
        c.list_data_points("steps")
    assert "data_points" in str(err.value)
    assert "55" not in str(err.value)  # values (health data) never end up in errors


def test_empty_reply_is_not_a_shape_error():
    c, _ = client([FakeResponse(200, {}), FakeResponse(200, {"rollupDataPoints": []})])
    assert c.list_data_points("steps") == []
    assert c.daily_rollup("steps", date(2026, 11, 24), date(2026, 11, 25)) == []


def test_tcx_export_returns_raw_bytes_and_asks_for_media():
    xml = "<TrainingCenterDatabase/>"
    c, session = client([FakeResponse(200, text=xml)])
    reply = c.export_exercise_tcx("123")
    assert reply.status == 200
    assert reply.content == xml.encode()
    call = session.calls[1]
    assert call["url"].endswith("/users/me/dataTypes/exercise/dataPoints/123:exportExerciseTcx")
    assert call["params"] == {"alt": "media"}
    assert call["headers"]["Accept"] == "*/*"


def test_tcx_export_partial_data_flag():
    c, session = client([FakeResponse(200, text="<x/>")])
    c.export_exercise_tcx("123", partial_data=True)
    assert session.calls[1]["params"] == {"alt": "media", "partialData": "true"}


def test_tcx_export_returns_client_errors_instead_of_raising():
    c, _ = client([FakeResponse(404, {"error": {"message": "not found"}})])
    reply = c.export_exercise_tcx("123")
    assert reply.status == 404
    assert b"not found" in reply.content


def test_tcx_export_retries_server_errors_then_returns_the_last_one():
    c, session = client([FakeResponse(503, {})] * 5)
    assert c.export_exercise_tcx("123").status == 503
    assert len(session.calls) == 1 + 5  # token + initial try + 4 retries


def test_tcx_export_escapes_the_id():
    c, session = client([FakeResponse(200, text="<x/>")])
    c.export_exercise_tcx("a/b")
    assert "/dataPoints/a%2Fb:exportExerciseTcx" in session.calls[1]["url"]
