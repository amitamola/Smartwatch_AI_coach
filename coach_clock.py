"""Athlete-local wall time shared by ingestion, planning and reminders."""

import os
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_zone_name = None


def configure(name):
    global _zone_name
    if name:
        ZoneInfo(name)  # Invalid configured zones must fail visibly, not silently change dates.
        _zone_name = name
        root = os.environ.get("AGBOT_DATA_DIR")
        if root:
            path = Path(root) / "state" / "athlete_timezone.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.read_text(encoding="utf-8").strip() == name:
                return
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             delete=False) as handle:
                handle.write(name)
            try:
                os.replace(handle.name, path)
            finally:
                if os.path.exists(handle.name):
                    os.unlink(handle.name)


def zone():
    name = os.environ.get("AGBOT_TIMEZONE") or _zone_name
    if not name and os.environ.get("AGBOT_DATA_DIR"):
        path = Path(os.environ["AGBOT_DATA_DIR"]) / "state" / "athlete_timezone.txt"
        if path.exists():
            name = path.read_text(encoding="utf-8").strip()
    return ZoneInfo(name) if name else datetime.now().astimezone().tzinfo


def now():
    return datetime.now(zone())


def today():
    return now().date()
