"""Constants: endpoints, OAuth scopes and the timezone used for "a day"."""

from __future__ import annotations

import os

API_BASE = "https://health.googleapis.com/v4"
TOKEN_URL = "https://oauth2.googleapis.com/token"

SCOPE_PREFIX = "https://www.googleapis.com/auth/googlehealth."

# Google documents exactly these read-only scopes. (Reproductive health,
# symptoms and mindfulness only exist as write-only scopes, so they are not
# requested here.)
READ_SCOPE_NAMES = (
    "activity_and_fitness",
    "ecg",
    "health_metrics_and_measurements",
    "irn",
    "location",
    "nutrition",
    "profile",
    "settings",
    "sleep",
)
READ_SCOPES = tuple(f"{SCOPE_PREFIX}{name}.readonly" for name in READ_SCOPE_NAMES)

# A "day" means a calendar day in this timezone (override with GHC_TIMEZONE).
TIMEZONE = os.environ.get("GHC_TIMEZONE", "Europe/Berlin")
