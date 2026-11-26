"""Tiny fakes used by the tests (no network involved)."""

from __future__ import annotations

import json
from typing import Any


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
