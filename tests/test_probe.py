"""`probe` with a fake client (all values invented)."""

from __future__ import annotations

import json
from datetime import date

from fakes import FakeClient

from google_health_claude.api.client import HealthApiError
from google_health_claude.devtools.probe import probe
from google_health_claude.fetch.datatypes import LIST_SPECS, ROLLUP_SPECS

DAY = date(2026, 10, 7)


class ProbeClient(FakeClient):
    def __init__(self, identity_error: HealthApiError | None = None):
        super().__init__()
        self.identity_error = identity_error
        self.failing_rollups: dict[str, HealthApiError] = {}

    def identity(self):
        if self.identity_error:
            raise self.identity_error
        return {"healthUserId": "invented"}

    def daily_rollup(self, data_type, start, end_exclusive):
        if data_type in self.failing_rollups:
            raise self.failing_rollups[data_type]
        return super().daily_rollup(data_type, start, end_exclusive)


def test_probe_reports_each_type_and_counts_failures(capsys):
    client = ProbeClient()
    client.lists["sleep"] = [{"name": "s/1"}]
    client.failing["exercise"] = HealthApiError(403, "no scope")
    client.rollups["steps"] = [{"steps": 1}, {"steps": 2}]
    client.failing_rollups["floors"] = HealthApiError(404, "unsupported")
    client.refresh_token_expires_in = 604799

    assert probe(client, DAY) == 2

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split()[:2] == ["OK", "identity"]
    assert lines[0].endswith("account is linked to Google Health")
    assert f"Probing {DAY} ..." in lines
    by_name = {
        line.split()[1]: line for line in lines if line[:6].strip() in ("OK", "EMPTY", "FAIL")
    }
    assert by_name["sleep"].split()[:3] == ["OK", "sleep", "1"]
    assert "filter snake_case" in by_name["sleep"]
    assert by_name["exercise"].startswith("FAIL") and "HTTP 403" in by_name["exercise"]
    assert by_name["daily-vo2-max"].startswith("EMPTY")
    assert by_name["steps"].split()[:4] == ["OK", "steps", "(rollup)", "2"]
    assert by_name["floors"].startswith("FAIL") and "HTTP 404" in by_name["floors"]
    assert by_name["distance"].startswith("EMPTY")
    assert len(by_name) == 1 + len(LIST_SPECS) + len(ROLLUP_SPECS)
    assert lines[-1] == "refresh_token_expires_in reported by Google: 604799"


def test_probe_counts_an_unlinked_account_and_saves_only_non_empty_replies(tmp_path, capsys):
    client = ProbeClient(identity_error=HealthApiError(400, "ACCOUNT_NOT_LINKED"))
    client.lists["daily-resting-heart-rate"] = [{"name": "r/1"}]
    client.rollups["heart-rate"] = [{"heartRate": {}}]
    client.reject_snake_case = True

    assert probe(client, DAY, tmp_path / "samples") == 1

    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("FAIL") and "ACCOUNT_NOT_LINKED" in out
    assert "filter camelCase" in out
    saved = sorted(p.name for p in (tmp_path / "samples").iterdir())
    assert saved == ["daily-resting-heart-rate.json", "heart-rate_rollup.json"]
    body = (tmp_path / "samples" / "daily-resting-heart-rate.json").read_text(encoding="utf-8")
    assert json.loads(body) == [{"name": "r/1"}]
