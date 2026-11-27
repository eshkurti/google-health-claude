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


def _read_json(path: str | Path) -> object:
    parsed: object = json.loads(Path(path).read_text(encoding="utf-8"))
    return parsed


def read_client_secret_file(path: str | Path) -> tuple[str, str]:
    """Return (client_id, client_secret) from a Google client JSON download."""
    data = _read_json(path)
    section = (data.get("installed") or data.get("web")) if isinstance(data, dict) else None
    if not isinstance(section, dict):
        raise CredentialsError(
            "Unrecognised client secret file: expected an 'installed' or 'web' section."
        )
    client_id, client_secret = section.get("client_id"), section.get("client_secret")
    if not (isinstance(client_id, str) and isinstance(client_secret, str)):
        raise CredentialsError(
            "Unrecognised client secret file: client_id and client_secret must be strings."
        )
    return client_id, client_secret


def read_refresh_token(path: Path) -> str | None:
    """The refresh token saved by `auth`, or None if the file has none."""
    data = _read_json(path)
    token = data.get("refresh_token") if isinstance(data, dict) else None
    if token is not None and not isinstance(token, str):
        raise CredentialsError(f"Unrecognised token file {path}: refresh_token is not a string.")
    return token


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
            refresh_token = read_refresh_token(token_file)

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
