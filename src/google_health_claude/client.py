"""Minimal Google Health API client: token refresh, retries, pagination."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import quote

import requests

from .config import API_BASE, TOKEN_URL
from .credentials import Credentials


class AuthError(RuntimeError):
    """The refresh token was rejected. Run the `auth` command again."""


class HealthApiError(RuntimeError):
    def __init__(self, status: int, message: str, url: str = ""):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.url = url


@dataclass(frozen=True)
class RawReply:
    """A non-JSON reply (e.g. a TCX file): HTTP status and the body as bytes."""

    status: int
    content: bytes


def civil_date_time(day: date) -> dict[str, Any]:
    """CivilDateTime for midnight at the start of ``day`` (time defaults to 00:00)."""
    return {"date": {"year": day.year, "month": day.month, "day": day.day}}


def _short(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


def _check_shape(body: dict[str, Any], items_key: str, url: str) -> None:
    """Fail loudly if a list reply has content but not under the expected key.

    The key names are from the docs and not yet confirmed live; a silent empty
    result would hide a wrong guess. Only key names are reported, never values.
    """
    other = sorted(set(body) - {items_key, "nextPageToken"})
    if other and items_key not in body:
        raise HealthApiError(0, f"unexpected response keys {other}, expected '{items_key}'", url)


def _error_message(resp: Any) -> str:
    try:
        return _short(resp.json()["error"]["message"])
    except Exception:  # noqa: BLE001 - any parsing problem falls back to raw text
        return _short(getattr(resp, "text", "") or "")


class HealthClient:
    def __init__(
        self,
        creds: Credentials,
        session: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 4,
    ):
        self._creds = creds
        self._session = session or requests.Session()
        self._sleep = sleep
        self._max_retries = max_retries
        self._access_token: str | None = None
        self._expires_at = 0.0
        # Reported by the token endpoint; logged so we learn the real lifetime.
        self.refresh_token_expires_in: Any = None
        # HTTP status of the last API reply (for probes that report it).
        self.last_status: int | None = None

    # -- authentication -------------------------------------------------

    def _refresh(self) -> str:
        resp = self._session.request(
            "POST",
            TOKEN_URL,
            data={
                "client_id": self._creds.client_id,
                "client_secret": self._creds.client_secret,
                "refresh_token": self._creds.refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise AuthError(
                f"Token refresh failed (HTTP {resp.status_code}): {_error_message(resp)}. "
                "Run `python -m google_health_claude auth` again and update the "
                "GOOGLE_REFRESH_TOKEN secret."
            )
        body = resp.json()
        token = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            # Never send "Bearer None": stop here, without echoing the reply body.
            raise AuthError(
                "Token refresh returned HTTP 200 but no access token. "
                "Run `python -m google_health_claude auth` again if this persists."
            )
        self._access_token = token
        self._expires_at = time.time() + float(body.get("expires_in", 3600)) - 60
        self.refresh_token_expires_in = body.get("refresh_token_expires_in")
        return token

    def _token(self) -> str:
        if self._access_token and time.time() < self._expires_at:
            return self._access_token
        return self._refresh()

    # -- HTTP -----------------------------------------------------------

    def _send(self, method: str, url: str, accept: str = "application/json", **kwargs: Any) -> Any:
        """Send with auth, one refresh on 401, and backoff on 429/5xx/network errors.

        Returns the final response whatever its status; raises only when no
        response could be obtained at all.
        """
        refreshed = False
        for attempt in range(self._max_retries + 1):
            can_retry = attempt < self._max_retries
            try:
                headers = {"Authorization": f"Bearer {self._token()}", "Accept": accept}
                resp = self._session.request(method, url, headers=headers, timeout=60, **kwargs)
            except requests.RequestException as err:
                # Timeouts and dropped connections (also during token refresh).
                if can_retry:
                    self._sleep(2**attempt)
                    continue
                raise HealthApiError(0, f"network error ({type(err).__name__})", url) from err
            if resp.status_code == 401 and not refreshed:
                self._access_token = None
                refreshed = True
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and can_retry:
                self._sleep(2**attempt)
                continue
            self.last_status = resp.status_code
            return resp
        raise HealthApiError(0, "retries exhausted", url)

    def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        resp = self._send(method, url, **kwargs)
        if resp.status_code >= 400:
            raise HealthApiError(resp.status_code, _error_message(resp), url)
        if not resp.content:
            return {}
        try:
            body = resp.json()
        except ValueError as err:
            raise HealthApiError(resp.status_code, "response is not valid JSON", url) from err
        if not isinstance(body, dict):
            raise HealthApiError(resp.status_code, "response is not a JSON object", url)
        return body

    # -- API methods ----------------------------------------------------

    def identity(self) -> dict[str, Any]:
        return self._request("GET", f"{API_BASE}/users/me/identity")

    def list_page(
        self,
        data_type: str,
        flt: str | None = None,
        page_size: int | None = None,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        """One page of a list call: the reply with ``dataPoints`` and maybe ``nextPageToken``."""
        url = f"{API_BASE}/users/me/dataTypes/{data_type}/dataPoints"
        params: dict[str, Any] = {}
        if flt:
            params["filter"] = flt
        if page_size:
            params["pageSize"] = page_size
        if page_token:
            params["pageToken"] = page_token
        body = self._request("GET", url, params=params)
        _check_shape(body, "dataPoints", url)
        return body

    def list_data_points(
        self, data_type: str, flt: str | None = None, page_size: int | None = None
    ) -> list[dict[str, Any]]:
        """All data points matching ``flt``, following pagination."""
        points: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            body = self.list_page(data_type, flt, page_size, token)
            points.extend(body.get("dataPoints", []))
            token = body.get("nextPageToken")
            if not token:
                return points

    def export_exercise_tcx(self, exercise_id: str, partial_data: bool = False) -> RawReply:
        """Raw TCX export of one exercise session (unverified against the live API).

        Unlike the JSON methods this does not raise on 4xx/5xx: the caller gets
        the status and body so a probe can report what the API said.
        """
        url = f"{API_BASE}/users/me/dataTypes/exercise/dataPoints/{quote(exercise_id, safe='')}:exportExerciseTcx"
        params = {"alt": "media"}
        if partial_data:
            params["partialData"] = "true"
        resp = self._send("GET", url, accept="*/*", params=params)
        return RawReply(resp.status_code, resp.content or b"")

    def daily_rollup(
        self, data_type: str, start: date, end_exclusive: date
    ) -> list[dict[str, Any]]:
        """One aggregated point per civil day in [start, end_exclusive)."""
        url = f"{API_BASE}/users/me/dataTypes/{data_type}/dataPoints:dailyRollUp"
        body: dict[str, Any] = {
            "range": {
                "start": civil_date_time(start),
                "end": civil_date_time(end_exclusive),
            },
            "windowSizeDays": 1,
        }
        points: list[dict[str, Any]] = []
        while True:
            reply = self._request("POST", url, json=body)
            _check_shape(reply, "rollupDataPoints", url)
            points.extend(reply.get("rollupDataPoints", []))
            token = reply.get("nextPageToken")
            if not token:
                return points
            body = {**body, "pageToken": token}
