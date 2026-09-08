import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.control import Control, ControlError, InventoryInput, password_hash, password_valid
from app.main import create_app
from test_app import PASSWORD, login, settings


def device(**changes):
    return {"id": "dell-pilot", "name": "SW-DELL-PILOT", "site": "Laboratorio", "model": "Dell pendiente",
            "host": "10.2.3.4", "profile": "dell_generic", "enabled": False, **changes}


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = create_app(settings(self.directory.name), start_workers=False)
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def signed_in(self):
        response = login(self.client)
        self.assertEqual(response.status_code, 200)
        return response

    def test_login_requires_origin_and_matching_preauth_token(self):
        csrf = self.client.get("/api/auth/context").json()["csrf"]
        body = {"username":"tester", "password":PASSWORD}
        self.assertEqual(self.client.post("/api/auth/login", json=body).status_code, 403)
        self.assertEqual(self.client.post("/api/auth/login", json=body, headers={"Origin":"https://evil.example", "X-CSRF-Token":csrf}).status_code, 403)
        self.signed_in()

    def test_mutations_require_csrf_and_body_errors_do_not_echo_secrets(self):
        self.signed_in()
        self.assertEqual(self.client.post("/api/inventory",json=device(),headers={"X-CSRF-Token":"wrong"}).status_code,403)
        secret = "private-value-must-not-echo"
        response = self.client.post("/api/inventory",json=device(auth_password=secret,unexpected=secret))
        self.assertEqual(response.status_code,422)
        self.assertNotIn(secret,response.text)
        self.assertIn("Content-Security-Policy",response.headers)

    def test_expired_idle_and_absolute_sessions_cannot_access_api(self):
        self.signed_in()
        with patch("app.control.time.time",return_value=time.time()+1801):
            self.assertEqual(self.client.get("/api/overview").status_code,401)
        self.signed_in()
        with self.app.state.control.lock:
            self.app.state.control.db.execute("UPDATE sessions SET created=?",(time.time()-28801,))
            self.app.state.control.db.commit()
        self.assertEqual(self.client.get("/api/overview").status_code,401)

    def test_background_polling_does_not_extend_idle_session(self):
        self.signed_in()
        before = self.app.state.control.db.execute("SELECT seen FROM sessions").fetchone()[0]
        with patch("app.control.time.time",return_value=time.time()+100):
            self.client.get("/api/overview")
            self.assertEqual(self.app.state.control.db.execute("SELECT seen FROM sessions").fetchone()[0],before)
            self.assertEqual(self.client.post("/api/auth/keepalive",json={}).status_code,200)
            self.assertGreater(self.app.state.control.db.execute("SELECT seen FROM sessions").fetchone()[0],before)

    def test_logout_revokes_token_on_server(self):
        self.signed_in()
        stolen = self.client.cookies.get("dev-bloodraven")
        self.assertEqual(self.client.post("/api/auth/logout",json={}).status_code,200)
        self.client.cookies.set("dev-bloodraven",stolen)
        self.assertEqual(self.client.get("/api/overview").status_code,401)

    def test_five_failures_temporarily_block_existing_and_unknown_users(self):
        for username in ("tester","not-existing"):
            for _ in range(5):
                self.assertEqual(login(self.client,username,"invalid-password").status_code,401)
            self.assertEqual(login(self.client,username,PASSWORD).status_code,429)

    def test_viewer_is_denied_all_management_mutations(self):
        self.signed_in()
        response = self.client.post("/api/admin/users",json={"username":"reader","name":"Consulta","role":"viewer","password":PASSWORD})
        self.assertEqual(response.status_code,201)
        self.assertEqual(login(self.client,"reader").status_code,200)
        self.assertEqual(self.client.get("/api/overview").status_code,200)
        for path in ("/api/inventory","/api/admin/users","/api/admin/audit","/manage"):
            self.assertEqual(self.client.get(path).status_code,403)
        self.assertEqual(self.client.post("/api/inventory",json=device()).status_code,403)
        self.assertEqual(self.client.post("/api/inventory/albrook/archive",json={"revision":1}).status_code,403)

    def test_encrypted_inventory_persists_and_never_returns_passwords(self):
        self.signed_in()
        secret = "special_SNMP_secret_9342"
        response = self.client.post("/api/inventory",json=device(snmp_username="monitor",auth_password=secret,privacy_password=secret,enabled=True))
        self.assertEqual(response.status_code,201,response.text)
        inventory = self.client.get("/api/inventory")
        self.assertNotIn(secret,inventory.text)
        data = next(d for d in inventory.json()["devices"] if d["id"]=="dell-pilot")
        self.assertTrue(data["credentials_configured"])
        raw = self.app.state.control.db.execute("SELECT payload,credentials FROM inventory WHERE id='dell-pilot'").fetchone()
        self.assertNotIn(secret,str(tuple(raw)))
        config = next(d for d in self.app.state.settings.devices if d["id"]=="dell-pilot")
        self.assertEqual(self.app.state.control.poll_config(config)["_credentials"][1],secret)
        other = Control(settings(self.directory.name))
        try:
            self.assertEqual(len(other.inventory()),2)
            self.assertTrue(next(d for d in other.inventory() if d["id"]=="dell-pilot")["credentials_configured"])
        finally:
            other.close()

    def test_update_checks_revision_duplicate_host_and_credential_preservation(self):
        self.signed_in()
        secret = "test-SNMP-credentials"
        self.assertEqual(self.client.post("/api/inventory",json=device(snmp_username="monitor",auth_password=secret,privacy_password=secret)).status_code,201)
        self.assertEqual(self.client.post("/api/inventory",json=device(id="other-device")).status_code,409)
        self.assertEqual(self.client.put("/api/inventory/dell-pilot",json=device(revision=99)).status_code,409)
        self.assertEqual(self.client.put("/api/inventory/dell-pilot",json=device(revision=1,name="Renamed switch")).status_code,200)
        data=next(d for d in self.client.get("/api/inventory").json()["devices"] if d["id"]=="dell-pilot")
        self.assertTrue(data["credentials_configured"])
        self.assertEqual(data["revision"],2)
        self.assertEqual(next(d for d in self.client.get("/api/overview").json()["devices"] if d["id"]=="dell-pilot")["name"],"Renamed switch")

    def test_archiving_removes_device_from_polling_and_records_audit(self):
        self.signed_in()
        self.client.post("/api/inventory",json=device())
        self.assertEqual(self.client.post("/api/inventory/dell-pilot/archive",json={"revision":1}).status_code,200)
        self.assertNotIn("dell-pilot",[d["id"] for d in self.app.state.settings.devices])
        self.assertEqual(self.client.post("/api/inventory/dell-pilot/archive",json={"revision":1}).status_code,409)
        actions = [event["action"] for event in self.client.get("/api/admin/audit").json()["events"]]
        self.assertIn("device.created",actions)
        self.assertIn("device.archived",actions)

    def test_self_deactivation_blocked_and_password_change_revokes_all_sessions(self):
        user=self.signed_in().json()["user"]
        self.assertEqual(self.client.put("/api/admin/users/"+user["id"],json={"username":"tester","name":"Test","role":"admin","active":False}).status_code,400)
        self.assertEqual(self.client.post("/api/auth/password",json={"current":"wrong-password","new":"new-password-long-enough"}).status_code,403)
        self.assertEqual(self.client.post("/api/auth/password",json={"current":PASSWORD,"new":"new-password-long-enough"}).status_code,200)
        self.assertEqual(self.client.get("/api/overview").status_code,401)
        self.assertEqual(login(self.client,password="new-password-long-enough").status_code,200)

    def test_request_size_limit(self):
        self.signed_in()
        self.assertEqual(self.client.post("/api/inventory",content=json.dumps({"notes":"X"*40000}),headers={"Content-Type":"application/json"}).status_code,413)

    def test_admin_password_is_hashed_and_session_token_is_not_stored(self):
        self.signed_in()
        row=self.app.state.control.db.execute("SELECT password FROM users WHERE username='tester'").fetchone()
        self.assertNotIn(PASSWORD,row[0])
        self.assertTrue(password_valid(PASSWORD,row[0]))
        token=self.client.cookies.get("dev-bloodraven")
        stored=self.app.state.control.db.execute("SELECT token FROM sessions").fetchone()[0]
        self.assertNotEqual(token,stored)

    def test_credentials_and_host_required_before_enabling(self):
        self.signed_in()
        for payload in (device(enabled=True,host=None),device(profile="cisco_sg350",model="SG350-52MP",enabled=True),device(profile="dell_generic",model="SG350-52MP"),device(host="http://127.0.0.1"),device(host="127.0.0.1")):
            self.assertEqual(self.client.post("/api/inventory",json=payload).status_code,422)
        self.assertEqual(self.client.post("/api/inventory",json=device(enabled=True)).status_code,400)

    def test_secure_cookie_attributes_in_https_mode(self):
        self.app.state.settings.secure_cookies=True
        self.app.state.settings.public_origin="https://testserver"
        self.client.base_url="https://testserver"
        csrf=self.client.get("/api/auth/context").json()["csrf"]
        response=self.client.post("/api/auth/login",json={"username":"tester","password":PASSWORD},headers={"Origin":"https://testserver","X-CSRF-Token":csrf})
        self.assertEqual(response.status_code,200)
        cookie=response.headers.get("set-cookie")
        for attribute in ("__Host-bloodraven", "Secure", "HttpOnly", "SameSite=strict", "Path=/"):
            self.assertIn(attribute,cookie)
        self.assertIn("Strict-Transport-Security",response.headers)


if __name__ == "__main__":
    unittest.main()
