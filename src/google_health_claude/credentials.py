"""Loading OAuth credentials from environment variables or local files."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TOKEN_FILE = Path.home() / ".config" / "google-health-claude" / "token.json"


class CredentialsError(RuntimeError):
    """Credentials are missing or unreadable."""


@dataclass(frozen=True)
class Credentials:
    client_id: str
    client_secret: str
    refresh_token: str


def read_client_secret_file(path: str | Path) -> tuple[str, str]:
    """Return (client_id, client_secret) from a Google client JSON download."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    section = data.get("installed") or data.get("web")
    if not section:
        raise CredentialsError(
            "Unrecognised client secret file: expected an 'installed' or 'web' section."
        )
    return section["client_id"], section["client_secret"]


def load_credentials(
    client_secret_path: str | Path | None = None,
    token_path: str | Path | None = None,
) -> Credentials:
    """Environment variables win; otherwise fall back to local files.

    Env: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
    (this is how GitHub Actions supplies them).
    """
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN")

    if not (client_id and client_secret) and client_secret_path:
        client_id, client_secret = read_client_secret_file(client_secret_path)

    if not refresh_token:
        token_file = Path(token_path) if token_path else DEFAULT_TOKEN_FILE
        if token_file.exists():
            refresh_token = json.loads(token_file.read_text(encoding="utf-8")).get("refresh_token")

    missing = [
        name
        for name, value in (
            (
                "client id/secret (use --client-secret or GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET)",
                client_id and client_secret,
            ),
            ("refresh token (run `auth` first or set GOOGLE_REFRESH_TOKEN)", refresh_token),
        )
        if not value
    ]
    if missing or not (client_id and client_secret and refresh_token):
        raise CredentialsError("Missing: " + "; ".join(missing))
    return Credentials(client_id, client_secret, refresh_token)
