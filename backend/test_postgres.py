"""Postgres smoke test. Skipped unless TEST_POSTGRES_URL points at a server."""

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

TEST_URL = os.environ.get("TEST_POSTGRES_URL", "").strip()
RELOAD_MODULES = {"database", "appointments", "auth", "runtime_config"}
ENV_KEYS = ["DATA_DIR", "DATABASE_URL", "ENV", "SESSION_SECRET", "TEST_POSTGRES_URL"]


@unittest.skipUnless(TEST_URL, "set TEST_POSTGRES_URL to run the Postgres smoke test")
class PostgresSmokeTest(unittest.TestCase):
    def setUp(self):
        import psycopg

        with psycopg.connect(TEST_URL, autocommit=True) as conn:
            conn.execute(
                "DROP TABLE IF EXISTS agents, appointments, accounts, "
                "rate_hits, audit_log, active_calls CASCADE"
            )
        self._saved_env = {key: os.environ.get(key) for key in ENV_KEYS}
        self._saved_modules = {
            name: sys.modules[name] for name in RELOAD_MODULES if name in sys.modules
        }
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["DATA_DIR"] = self.tmp.name
        os.environ["DATABASE_URL"] = TEST_URL
        os.environ["ENV"] = "development"
        os.environ["SESSION_SECRET"] = "postgres-test-secret"
        for name in list(sys.modules):
            if name in RELOAD_MODULES:
                del sys.modules[name]
        import appointments
        import auth
        import database

        appointments.now_local = lambda: datetime(2026, 1, 5, 9, 0)
        self.appointments = appointments
        self.auth = auth
        self.database = database

    def tearDown(self):
        for name in list(sys.modules):
            if name in RELOAD_MODULES:
                del sys.modules[name]
        sys.modules.update(self._saved_modules)
        for key in ENV_KEYS:
            if self._saved_env[key] is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = self._saved_env[key]
        self.tmp.cleanup()

    def test_postgres_round_trip(self):
        self.assertTrue(self.database.POSTGRES)
        self.database.upsert_agent({
            "id": "pg-agent-1",
            "owner_id": None,
            "name": "Seeded",
            "objective": "Test",
            "system_prompt": "Test",
            "reference_text": "",
            "capabilities": ["appointments"],
        })

        created = self.auth.register("pg@hospital.test", "password1", "PG Hospital")
        owner = created["account"]["id"]
        claimed = self.database.get_agent("pg-agent-1")
        self.assertEqual(claimed["owner_id"], owner)
        self.assertEqual(claimed["capabilities"], ["appointments"])

        booked = self.appointments.book(
            "John Smith", "9876543210", "general medicine", "tomorrow", "10:00",
            owner, "PG Hospital",
        )
        self.assertEqual(len(booked["id"]), 8)
        visits = self.database.list_visits(owner)
        self.assertEqual(visits[0]["phone"], "9876543210")
        with self.database.session() as conn:
            stored = self.database._one(
                conn, "SELECT phone FROM appointments WHERE id = ?", (booked["id"],)
            )["phone"]
        self.assertTrue(stored.startswith("gAAAA"))
        self.assertNotIn("9876543210", stored)

        with self.assertRaises(sqlite3.IntegrityError):
            self.database.insert_visit({
                "id": "dupe-pg",
                "owner_id": owner,
                "hospital": "PG Hospital",
                "patient_name": "Other",
                "phone": "1111111111",
                "department": "general medicine",
                "date": booked["date"],
                "time": booked["time"],
            })

        self.assertTrue(self.database.allow_request("pg:test", 1, 60))
        self.assertFalse(self.database.allow_request("pg:test", 1, 60))

        self.assertTrue(self.database.reserve_call(owner, "pg-room"))
        self.assertFalse(self.database.reserve_call(owner, "pg-other-room"))
        self.database.release_call("pg-room")
        self.assertTrue(self.database.reserve_call(owner, "pg-other-room"))


if __name__ == "__main__":
    unittest.main()
