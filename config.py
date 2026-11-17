"""Loads config.env (KEY=value lines) from the project folder, with defaults."""
import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
APP_SUPPORT = Path.home() / "Library" / "Application Support" / "FolkToAnki"
LOG_FILE = Path.home() / "Library" / "Logs" / "folk-to-anki.log"

DEFAULTS = {
    "FOLK_API_KEY": "",
    "FOLK_GROUPS": "US Govt,Partner orgs,Press,Corporate,Donor,Other govts",
    "FOLK_DC_GROUP": "US Govt",
    "MY_NAME": "Eli",
    "CALENDAR_ICS_URLS": "",
    "MY_EMAILS": "",
    "IGNORE_EMAIL_DOMAINS": "",
    "MAX_ATTENDEES": "10",
    "UPCOMING_DAYS": "90",
    "PAST_DAYS": "0",
    "ANKICONNECT_URL": "http://localhost:8765",
    "DECK_DC": "Daily review::*-DC networking",
    "DECK_NETWORKING": "Daily review::*-Networking",
    "SENATORS_TOTAL": "50",
    "SENATORS_PER_DAY": "2",
    "REPS_TOTAL": "25",
    "REPS_PER_DAY": "2",
    "ROLES_AGENT": "on",
    "ROLES_CHECK_DAYS": "2",
    "ROLES_AGENT_BUDGET_USD": "5.0",
    "PHOTOS_DIR": str(APP_SUPPORT / "photos"),
    "PHOTO_SEARCH": "on",
    "PHOTO_SEARCH_BUDGET_USD": "5.0",
    "PHOTO_SEARCH_RETRY_DAYS": "60",
}


def load(path=None):
    cfg = dict(DEFAULTS)
    path = Path(path) if path else PROJECT_DIR / "config.env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k in cfg:                                   # environment wins, for one-off overrides
        if k in os.environ:
            cfg[k] = os.environ[k]
    cfg["PHOTOS_DIR"] = os.path.expanduser(cfg["PHOTOS_DIR"])
    return cfg


def csv(value):
    return [v.strip() for v in value.replace("\n", ",").split(",") if v.strip()]
