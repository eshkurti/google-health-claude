"""Tiny fakes used by the tests (no network involved)."""

from __future__ import annotations

import json
from typing import Any

from google_health_claude.client import HealthApiError, RawReply


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")
        self.content = self.text.encode()

    def json(self) -> Any:
        return self._payload


class NotJsonResponse(FakeResponse):
    """A 200 reply whose body is not JSON (e.g. an HTML error page from a proxy)."""

    def __init__(self) -> None:
        super().__init__(200, text="<html>oops</html>")

    def json(self) -> Any:
        raise ValueError("not json")


class FakeSession:
    """Plays back queued responses (or raises queued exceptions) and records every request."""

    def __init__(self, responses: list[FakeResponse | Exception]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeClient:
    """Stands in for HealthClient; behaviour is configured per test."""

    def __init__(self):
        self.list_calls = []
        self.rollup_calls = []
        self.refresh_token_expires_in = None
        self.rollups = {}  # data_type -> list of points
        self.lists = {}  # data_type -> list of points
        self.reject_snake_case = False
        self.failing = {}  # data_type -> HealthApiError
        self.tcx = {}  # exercise id -> RawReply or HealthApiError
        self.tcx_calls = []

    def list_data_points(self, data_type, flt=None, page_size=None):
        self.list_calls.append((data_type, flt))
        if data_type in self.failing:
            raise self.failing[data_type]
        if self.reject_snake_case and "_" in flt.split(" ")[0]:
            raise HealthApiError(400, "bad filter")
        return self.lists.get(data_type, [])

    def daily_rollup(self, data_type, start, end_exclusive):
        self.rollup_calls.append((data_type, start, end_exclusive))
        return self.rollups.get(data_type, [])

    def export_exercise_tcx(self, exercise_id, partial_data=False):
        self.tcx_calls.append((exercise_id, partial_data))
        reply = self.tcx.get(exercise_id, RawReply(404, b'{"error": {"message": "not found"}}'))
        if isinstance(reply, HealthApiError):
            raise reply
        return reply
