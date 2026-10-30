import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import coach_clock


class ClockTests(unittest.TestCase):
    def test_timezone_survives_restart_and_controls_midnight(self):
        with tempfile.TemporaryDirectory() as root, \
                patch.dict(coach_clock.os.environ, {"AGBOT_DATA_DIR": root, "AGBOT_TIMEZONE": ""}), \
                patch.object(coach_clock, "_zone_name", None):
            coach_clock.configure("Asia/Kolkata")
            coach_clock._zone_name = None
            zone = coach_clock.zone()
            instant = datetime(2026, 2, 1, 20, tzinfo=timezone.utc)
            with patch.object(coach_clock, "datetime") as clock:
                clock.now.return_value = instant.astimezone(zone)
                self.assertEqual(coach_clock.today().isoformat(), "2026-02-02")
            self.assertEqual(zone.key, "Asia/Kolkata")
            with patch.dict(coach_clock.os.environ, {"AGBOT_TIMEZONE": "UTC"}):
                self.assertEqual(coach_clock.zone().key, "UTC")


if __name__ == "__main__":
    unittest.main()
