"""Booking privacy: a visit belongs to one hospital, and lookup needs the phone number."""

import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

RELOAD_MODULES = {"database", "appointments", "auth", "runtime_config"}
ENV_KEYS = [
    "DATA_DIR",
    "DATABASE_URL",
    "ENV",
    "SESSION_SECRET",
    "MAX_ACTIVE_CALLS",
    "HOSPITAL_TZ",
    "FRONTEND_URL",
]


class BookingPrivacyTest(unittest.TestCase):
    def setUp(self):
        self._saved_env = {key: os.environ.get(key) for key in ENV_KEYS}
        self._saved_modules = {
            name: sys.modules[name] for name in RELOAD_MODULES if name in sys.modules
        }
        self._temp_dirs = []
        self.tmp = self._new_temp()
        self._configure(self.tmp.name, "development")

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
        for tmp in self._temp_dirs:
            tmp.cleanup()

    def _new_temp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._temp_dirs.append(tmp)
        return tmp

    def _configure(self, data_dir, env):
        os.environ["DATA_DIR"] = data_dir
        os.environ["DATABASE_URL"] = ""
        os.environ["ENV"] = env
        os.environ["SESSION_SECRET"] = "test-session-secret"
        os.environ["MAX_ACTIVE_CALLS"] = "1"
        for name in list(sys.modules):
            if name in RELOAD_MODULES:
                del sys.modules[name]
        import appointments
        import auth
        import database

        # Deterministic clock: Monday 5 Jan 2026, 09:00 hospital time.
        appointments.now_local = lambda: datetime(2026, 1, 5, 9, 0)
        self.appointments = appointments
        self.auth = auth
        self.database = database

    def test_hospitals_do_not_share_a_slot_and_lookup_needs_the_phone(self):
        first = self.auth.register("one@hospital.test", "password1", "North Hospital")
        second = self.auth.register("two@hospital.test", "password2", "South Hospital")
        with self.assertRaises(ValueError):
            self.appointments.book(
                "Asha Rao", "123456789", "general medicine", "tomorrow", "10:30",
                first["account"]["id"], "North Hospital",
            )
        booked = self.appointments.book(
            "Asha Rao", "9999999999", "general medicine", "tomorrow", "10:00",
            first["account"]["id"], "North Hospital",
        )
        other = self.appointments.book(
            "Other Patient", "8888888888", "general medicine", "tomorrow", "10:00",
            second["account"]["id"], "South Hospital",
        )
        self.assertNotEqual(booked["id"], other["id"])
        north = self.appointments.desk(first["account"]["id"], "North Hospital")
        self.assertEqual(north["upcoming"][0]["department"], "general medicine")
        self.assertNotIn("patient_name", north["upcoming"][0])
        with self.assertRaises(ValueError):
            self.appointments.lookup(booked["id"], "1111111111")
        found = self.appointments.lookup(booked["id"], "9999999999")
        self.assertEqual(found["patient_name"], "Asha Rao")
        stored = (Path(self.tmp.name) / "vocal.db").read_bytes()
        self.assertNotIn(b"9999999999", stored)
        with self.assertRaises(ValueError):
            self.appointments.cancel("", "Asha Rao", "1111111111", first["account"]["id"])
        self.appointments.cancel(booked["id"], "", "9999999999", first["account"]["id"])
        self.assertEqual(self.appointments.desk(first["account"]["id"], "North Hospital")["upcoming"], [])
        self.assertEqual(len(self.appointments.desk(second["account"]["id"], "South Hospital")["upcoming"]), 1)

    def test_failed_booking_is_audited(self):
        account = self.auth.register("audit@hospital.test", "password1", "City Hospital")
        with self.assertRaises(ValueError):
            self.appointments.book(
                "Asha Rao", "123456789", "general medicine", "tomorrow", "10:30",
                account["account"]["id"], "City Hospital",
            )
        with self.database.session() as conn:
            rows = self.database._all(conn, "SELECT action, ok FROM audit_log")
        self.assertTrue(any(row["action"] == "book" and row["ok"] == 0 for row in rows))
    def test_limits_password_and_who_claims_the_existing_desk(self):
        self.database.insert_visit({
            "id": "claim0001",
            "owner_id": None,
            "hospital": "City Hospital",
            "patient_name": "Existing Patient",
            "phone": "7777777777",
            "department": "general medicine",
            "date": "2026-12-01",
            "time": "11:00",
        })
        created = self.auth.register("desk@hospital.test", "password1", "City Hospital")
        self.assertEqual(self.database.list_visits(created["account"]["id"])[0]["id"], "claim0001")
        self.auth.change_password(created["account"]["id"], "password1", "password2")
        self.auth.login("desk@hospital.test", "password2")
        self.assertTrue(self.database.allow_request("lookup:test", 1, 60))
        self.assertFalse(self.database.allow_request("lookup:test", 1, 60))
        self.assertTrue(self.database.reserve_call("owner", "room-a"))
        self.assertFalse(self.database.reserve_call("owner", "room-b"))
        self.database.release_call("room-a")
        self.assertTrue(self.database.reserve_call("owner", "room-b"))

    def test_first_production_account_claims_unowned_rows(self):
        prod_tmp = self._new_temp()
        self._configure(prod_tmp.name, "production")
        self.database.insert_visit({
            "id": "open0001",
            "owner_id": None,
            "hospital": "City Hospital",
            "patient_name": "Waiting Patient",
            "phone": "6666666666",
            "department": "skin",
            "date": "2026-12-02",
            "time": "12:00",
        })
        created = self.auth.register("stranger@hospital.test", "password1", "Other Hospital")
        waiting = [row for row in self.database.list_visits() if row["id"] == "open0001"]
        self.assertEqual(waiting[0]["owner_id"], created["account"]["id"])
        self.assertNotIn(b"6666666666", (Path(prod_tmp.name) / "vocal.db").read_bytes())


if __name__ == "__main__":
    unittest.main()
