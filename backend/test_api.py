"""API tests: auth, rate limits, ownership and upload ordering."""

import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

RELOAD_MODULES = {"main", "database", "appointments", "auth", "runtime_config"}
ENV_KEYS = [
    "DATA_DIR",
    "DATABASE_URL",
    "ENV",
    "SESSION_SECRET",
    "MAX_ACTIVE_CALLS",
    "FRONTEND_URL",
    "LIVEKIT_URL",
    "LIVEKIT_API_KEY",
    "LIVEKIT_API_SECRET",
]


class ApiTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        self._saved_env = {key: os.environ.get(key) for key in ENV_KEYS}
        self._saved_modules = {
            name: sys.modules[name] for name in RELOAD_MODULES if name in sys.modules
        }
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        os.environ["DATA_DIR"] = self.tmp.name
        os.environ["DATABASE_URL"] = ""
        os.environ["ENV"] = "production"
        os.environ["SESSION_SECRET"] = "test-session-secret"
        os.environ["MAX_ACTIVE_CALLS"] = "1"
        os.environ.pop("FRONTEND_URL", None)
        for name in list(sys.modules):
            if name in RELOAD_MODULES:
                del sys.modules[name]
        import main

        self.main = main
        self.client = TestClient(main.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
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

    def _register(self, email="owner@hospital.test", hospital="City Hospital"):
        response = self.client.post(
            "/api/auth/register",
            json={"email": email, "password": "password1", "hospital_name": hospital},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _auth(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_register_and_login_and_me(self):
        created = self._register()
        self.assertIn("token", created)
        me = self.client.get("/api/auth/me", headers=self._auth(created["token"]))
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["email"], "owner@hospital.test")
        login = self.client.post(
            "/api/auth/login",
            json={"email": "owner@hospital.test", "password": "password1"},
        )
        self.assertEqual(login.status_code, 200)
        self.assertIn("token", login.json())

    def test_register_requires_a_hospital_name(self):
        response = self.client.post(
            "/api/auth/register",
            json={"email": "no@hospital.test", "password": "password1", "hospital_name": ""},
        )
        self.assertEqual(response.status_code, 400)

    def test_agents_require_authentication(self):
        self.assertEqual(self.client.get("/api/agents").status_code, 401)

    def test_upload_is_rejected_before_parsing_without_auth(self):
        uploads = Path(self.tmp.name) / "uploads"
        before = list(uploads.glob("*")) if uploads.exists() else []
        response = self.client.post(
            "/api/agents",
            data={"name": "X", "objective": "Y", "system_prompt": "Z"},
            files={"reference_file": ("ref.txt", b"hello" * 100, "text/plain")},
        )
        self.assertEqual(response.status_code, 401)
        after = list(uploads.glob("*")) if uploads.exists() else []
        self.assertEqual(before, after)

    def test_seeded_agents_are_claimed_by_the_first_account(self):
        created = self._register()
        agents = self.client.get("/api/agents", headers=self._auth(created["token"])).json()
        by_name = {agent["name"]: agent for agent in agents}
        self.assertIn("Harbor Desk", by_name)
        hospital = by_name["City Hospital Front Desk"]
        self.assertEqual(hospital["capabilities"], ["appointments"])
        self.assertEqual(hospital["owner_id"], created["account"]["id"])

    def test_second_account_does_not_see_demo_agents(self):
        self._register("first@hospital.test")
        second = self._register("second@hospital.test", "Other Hospital")
        agents = self.client.get("/api/agents", headers=self._auth(second["token"])).json()
        self.assertEqual(agents, [])

    def test_create_agent_with_capabilities(self):
        created = self._register()
        response = self.client.post(
            "/api/agents",
            data={
                "name": "Booker",
                "objective": "Book visits",
                "system_prompt": "Book visits.",
                "capabilities": "appointments",
            },
            headers=self._auth(created["token"]),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["agent"]["capabilities"], ["appointments"])
        agents = self.client.get("/api/agents", headers=self._auth(created["token"])).json()
        self.assertEqual(len([a for a in agents if a["name"] == "Booker"]), 1)

    def test_cannot_update_an_agent_you_do_not_own(self):
        first = self._register("first@hospital.test")
        created = self.client.post(
            "/api/agents",
            data={"name": "Mine", "objective": "Keep", "system_prompt": "Keep."},
            headers=self._auth(first["token"]),
        ).json()["agent"]
        second = self._register("second@hospital.test", "Other Hospital")
        response = self.client.post(
            "/api/agents",
            data={
                "agent_id": created["id"],
                "name": "Stolen",
                "objective": "Take",
                "system_prompt": "Take.",
            },
            headers=self._auth(second["token"]),
        )
        self.assertEqual(response.status_code, 404)

    def test_token_rejects_an_agent_you_do_not_own(self):
        created = self._register()
        room = f"agent_{uuid.uuid4()}_abcd1234"
        response = self.client.post(
            "/api/token",
            json={"room_name": room, "participant_name": "User"},
            headers=self._auth(created["token"]),
        )
        self.assertEqual(response.status_code, 404)

    def test_token_returns_409_when_the_desk_is_busy(self):
        created = self._register()
        agent = self.client.post(
            "/api/agents",
            data={"name": "Booker", "objective": "Book", "system_prompt": "Book."},
            headers=self._auth(created["token"]),
        ).json()["agent"]
        os.environ["LIVEKIT_URL"] = "wss://livekit.invalid"
        os.environ["LIVEKIT_API_KEY"] = "test-key"
        os.environ["LIVEKIT_API_SECRET"] = "test-secret"
        self.main.database.reserve_call(created["account"]["id"], "already-busy-room")
        room = f"agent_{agent['id']}_abcd1234"
        response = self.client.post(
            "/api/token",
            json={"room_name": room, "participant_name": "User"},
            headers=self._auth(created["token"]),
        )
        self.assertEqual(response.status_code, 409)

    def test_health_reports_version(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["version"], "2.1.0")

    def test_z_rate_limit_auth(self):
        for _ in range(8):
            response = self.client.post(
                "/api/auth/login",
                json={"email": "nobody@hospital.test", "password": "password1"},
            )
            self.assertEqual(response.status_code, 401)
        blocked = self.client.post(
            "/api/auth/login",
            json={"email": "nobody@hospital.test", "password": "password1"},
        )
        self.assertEqual(blocked.status_code, 429)


if __name__ == "__main__":
    unittest.main()
