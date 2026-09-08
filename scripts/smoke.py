"""Check a fresh, isolated CI demo over verified HTTPS; creates a paused test device."""
from pathlib import Path
import ssl
import sys
import time

import httpx


def main():
    root = Path(__file__).resolve().parents[1]
    environment = dict(line.split("=", 1) for line in (root / ".env").read_text().splitlines()
                       if "=" in line and not line.startswith("#"))
    assert environment["BR_MODE"] == "demo", "Smoke check requires an isolated demo"
    context = ssl.create_default_context(cafile=sys.argv[1])
    with httpx.Client(base_url="https://localhost", verify=context, timeout=10) as client:
        for attempt in range(15):
            try:
                client.get("/healthz").raise_for_status()
                break
            except httpx.HTTPError:
                if attempt == 14:
                    raise
                time.sleep(1)
        assert client.get("/api/inventory").status_code == 401
        assert "SERTRACEN" in client.get("/login").text
        preauth = client.get("/api/auth/context").json()
        client.headers.update({"Origin": "https://localhost", "X-CSRF-Token": preauth["csrf"]})
        signed_in = client.post("/api/auth/login", json={"username": environment["BR_USERNAME"], "password": environment["BR_PASSWORD"]})
        signed_in.raise_for_status()
        assert signed_in.json()["user"]["role"] == "admin"
        client.headers["X-CSRF-Token"] = signed_in.json()["csrf"]
        assert client.get("/").status_code == 200
        assert client.get("/manage").status_code == 200
        for asset in ("session.js", "manage.js", "sertracen.png"):
            client.get("/static/" + asset).raise_for_status()
        saved = client.post("/api/inventory", json={"id": "ci-smoke", "name": "SW-CI-TEST", "site": "CI lab",
                            "model": "Dell generic test", "profile": "dell_generic", "host": "10.99.99.9", "enabled": False})
        assert saved.status_code == 201
        assert any(d["id"] == "ci-smoke" and not d["enabled"] for d in client.get("/api/inventory").json()["devices"])
        assert client.post("/api/auth/logout", json={}).status_code == 200
        assert client.get("/api/inventory").status_code == 401
    print("HTTPS, login, admin portal, inventory and logout verified in isolated demo.")


if __name__ == "__main__":
    main()
