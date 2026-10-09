import importlib.util
import unittest
from datetime import datetime, timedelta
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update_site_data.py"
SPEC = importlib.util.spec_from_file_location("update_site_data", SCRIPT)
update_site_data = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(update_site_data)


class SeedValidationTests(unittest.TestCase):
    def make_payload(self, updated_at, show_count=None):
        count = show_count or update_site_data.MIN_SEED_SHOW_COUNT
        return {
            "meta": {"updatedAt": updated_at.isoformat(timespec="seconds")},
            "shows": [{}] * count,
        }

    def test_accepts_recent_seed(self):
        now = datetime(2026, 10, 9, 12, 0, tzinfo=update_site_data.BEIJING_TZ)
        payload = self.make_payload(now - timedelta(hours=6))
        update_site_data.validate_seed_payload(payload, now=now)

    def test_rejects_stale_seed(self):
        now = datetime(2026, 10, 9, 12, 0, tzinfo=update_site_data.BEIJING_TZ)
        payload = self.make_payload(now - timedelta(days=4))
        with self.assertRaisesRegex(RuntimeError, "stale"):
            update_site_data.validate_seed_payload(payload, now=now)

    def test_rejects_suspiciously_small_seed(self):
        now = datetime(2026, 10, 9, 12, 0, tzinfo=update_site_data.BEIJING_TZ)
        payload = self.make_payload(now, show_count=10)
        with self.assertRaisesRegex(RuntimeError, "too small"):
            update_site_data.validate_seed_payload(payload, now=now)


if __name__ == "__main__":
    unittest.main()
