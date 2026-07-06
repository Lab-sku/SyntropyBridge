import os
import tempfile
import unittest
from unittest.mock import patch


class UsageRollupsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        os.environ["DATABASE_PATH"] = self.tmp.name

        from backend import database as db

        self.db = db
        self.db.init_db()

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except Exception:
            pass

    def test_usage_windows_from_rollups(self):
        base_ts = 1_700_000_000.0

        with self.db.get_db_context() as conn:
            conn.execute(
                "INSERT INTO users (username, password_hash, api_key, quota_5h, quota_week, is_active) VALUES (?, ?, ?, ?, ?, 1)",
                ("u1", "x", "k1", 10, 100),
            )
            user_id = conn.execute("SELECT id FROM users WHERE username = ?", ("u1",)).fetchone()[0]

        with patch("backend.database.time.time", return_value=base_ts - 6 * 3600):
            self.db.add_usage_log(
                user_id=user_id,
                endpoint="e",
                model="m",
                prompt_tokens=0,
                completion_tokens=0,
                response_time_ms=1,
                status_code=200,
            )

        with patch("backend.database.time.time", return_value=base_ts):
            self.db.add_usage_log(
                user_id=user_id,
                endpoint="e",
                model="m",
                prompt_tokens=0,
                completion_tokens=0,
                response_time_ms=1,
                status_code=200,
            )
            self.db.add_usage_log(
                user_id=user_id,
                endpoint="e",
                model="m",
                prompt_tokens=0,
                completion_tokens=0,
                response_time_ms=1,
                status_code=200,
            )

        with patch("backend.database.time.time", return_value=base_ts):
            usage_5h, usage_week = self.db.get_usage_windows(user_id)

        self.assertEqual(usage_5h, 2)
        self.assertEqual(usage_week, 3)


if __name__ == "__main__":
    unittest.main()
