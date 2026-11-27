"""`auth`: one-time browser sign-in that produces a refresh token.

Run this on your own computer, not in CI. It opens your browser, you sign in
with the Google account linked to Google Health, and the refresh token is saved
to a local file outside any repository. You then store that token as the
GOOGLE_REFRESH_TOKEN secret in the private data repo.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .client import HealthApiError, HealthClient
from .config import READ_SCOPES
from .credentials import DEFAULT_TOKEN_FILE, Credentials


def run_auth(client_secret_path: Path, token_path: Path | None = None) -> int:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print('Missing dependency. Install it with:  pip install "google-auth-oauthlib>=1.2"')
        return 1

    token_path = Path(token_path) if token_path else DEFAULT_TOKEN_FILE
    flow = InstalledAppFlow.from_client_secrets_file(
        str(client_secret_path), scopes=list(READ_SCOPES)
    )
    creds = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
        authorization_prompt_message="Opening your browser to sign in to Google ...",
        success_message="Signed in. You can close this tab and return to the terminal.",
    )
    raw_token = dict(flow.oauth2session.token or {})
    if not creds.refresh_token:
        print(
            "Google did not return a refresh token. Revoke the app at "
            "https://myaccount.google.com/permissions and run `auth` again."
        )
        return 1

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(
        json.dumps(
            {
                "refresh_token": creds.refresh_token,
                "refresh_token_expires_in": raw_token.get("refresh_token_expires_in"),
                "scopes": sorted(creds.scopes or []),
                "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    try:
        os.chmod(token_path, 0o600)
    except OSError:
        pass

    print(f"\nSigned in. Refresh token saved to:\n  {token_path}\n")
    print("Scopes granted:")
    for scope in sorted(creds.scopes or []):
        print("  ", scope.rsplit("/", 1)[-1])
    print(
        "\nrefresh_token_expires_in reported by Google:", raw_token.get("refresh_token_expires_in")
    )

    client = HealthClient(
        Credentials(
            flow.client_config["client_id"],
            flow.client_config["client_secret"],
            creds.refresh_token,
        )
    )
    try:
        client.identity()
        print("\nAccount check: OK, this Google account is linked to Google Health.")
    except HealthApiError as err:
        print(f"\nAccount check failed: {err}")
        if "ACCOUNT_NOT_LINKED" in str(err):
            print(
                "Open the Google Health app on your phone and sign in with this Google account first."
            )
        return 1
    return 0
