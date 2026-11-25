"""Booking privacy: a visit belongs to one hospital, and lookup needs the phone number."""

import os
import sys
import tempfile
import unittest
from pathlib import Path


class BookingPrivacyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = self.tmp.name
        os.environ["DATABASE_URL"] = ""
        os.environ["ENV"] = "development"
        os.environ["SESSION_SECRET"] = "test-session-secret"
        os.environ["MAX_ACTIVE_CALLS"] = "1"
        for name in list(sys.modules):
            if name in {"database", "appointments", "auth", "runtime_config"}:
                del sys.modules[name]
        import appointments
        import auth
        self.appointments = appointments
        self.auth = auth

    def tearDown(self):
        self.tmp.cleanup()

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

    def test_limits_password_and_who_claims_the_existing_desk(self):
        import database
        database.insert_visit({
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
        self.assertEqual(database.list_visits(created["account"]["id"])[0]["id"], "claim0001")
        self.auth.change_password(created["account"]["id"], "password1", "password2")
        self.auth.login("desk@hospital.test", "password2")
        self.assertTrue(database.allow_request("lookup:test", 1, 60))
        self.assertFalse(database.allow_request("lookup:test", 1, 60))
        self.assertTrue(database.reserve_call("owner", "room-a"))
        self.assertFalse(database.reserve_call("owner", "room-b"))
        database.release_call("room-a")
        self.assertTrue(database.reserve_call("owner", "room-b"))

        prod_tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = prod_tmp.name
        os.environ["ENV"] = "production"
        os.environ["SESSION_SECRET"] = "production-session-secret"
        for name in list(sys.modules):
            if name in {"database", "appointments", "auth", "runtime_config"}:
                del sys.modules[name]
        import auth as prod_auth
        import database as prod_db
        prod_db.insert_visit({
            "id": "open0001",
            "owner_id": None,
            "hospital": "City Hospital",
            "patient_name": "Waiting Patient",
            "phone": "6666666666",
            "department": "skin",
            "date": "2026-12-02",
            "time": "12:00",
        })
        prod_auth.register("stranger@hospital.test", "password1", "Other Hospital")
        waiting = [row for row in prod_db.list_visits() if row["id"] == "open0001"]
        self.assertIsNone(waiting[0]["owner_id"])
        self.assertNotIn(b"6666666666", (Path(prod_tmp.name) / "vocal.db").read_bytes())
        prod_tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
