import hashlib
import os
import secrets
import sys
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


ADMIN_PASSWORD = secrets.token_urlsafe(24)


def make_password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    iterations = 310_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def cookie_from(response) -> str:
    return response.headers["set-cookie"].split(";", 1)[0]


def request_json(client: TestClient, method: str, path: str, cookie: str = "", body=None, expected: int = 200):
    headers = {"Cookie": cookie} if cookie else {}
    response = client.request(method, path, headers=headers, json=body)
    assert response.status_code == expected, (method, path, response.status_code, response.text)
    return response.json()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="funf-multitenant-") as directory:
        root = Path(directory)
        os.environ.update(
            {
                "FUNF_DB_PATH": str(root / "funf.db"),
                "FUNF_ADMIN_USERNAME": "admin",
                "FUNF_ADMIN_PASSWORD_HASH": make_password_hash(ADMIN_PASSWORD),
                "FUNF_VAULT_KEY": Fernet.generate_key().decode(),
                "FUNF_PUBLIC_ORIGIN": "https://testserver",
                "FUNF_API_TOKEN": "",
                "FUNF_DEVICE_TOKEN": "",
            }
        )
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import relay_server

        with TestClient(relay_server.app, base_url="https://testserver") as client:
            login = client.post(
                "/api/login", json={"username": "admin", "password": ADMIN_PASSWORD}
            )
            assert login.status_code == 200, login.text
            admin_cookie = cookie_from(login)

            credentials = {}
            for username in ("alice", "bob"):
                created = request_json(
                    client, "POST", "/api/users", admin_cookie, {"username": username}
                )
                credentials[username] = created["initial_password"]

            cookies = {}
            for username in ("alice", "bob"):
                response = client.post(
                    "/api/login",
                    json={"username": username, "password": credentials[username]},
                )
                cookies[username] = cookie_from(response)
                request_json(
                    client,
                    "POST",
                    "/api/account/password",
                    cookies[username],
                    {
                        "current_password": credentials[username],
                        "new_password": secrets.token_urlsafe(24),
                    },
                )

            device_ids = {}
            device_keys = {}
            for username in ("alice", "bob"):
                created = request_json(
                    client,
                    "POST",
                    "/api/devices",
                    cookies[username],
                    {"label": f"{username.title()} Device"},
                )
                device_ids[username] = created["device"]["id"]
                device_keys[username] = created["device_key"]
            for username in ("alice", "bob"):
                listed = request_json(client, "GET", "/api/devices", cookies[username])
                assert [device["id"] for device in listed["devices"]] == [device_ids[username]]
                other = "bob" if username == "alice" else "alice"
                request_json(
                    client,
                    "GET",
                    f"/api/devices/{device_ids[other]}/status",
                    cookies[username],
                    expected=403,
                )

            health = client.get("/healthz")
            assert health.headers["x-frame-options"] == "DENY"
            assert health.headers["referrer-policy"] == "no-referrer"
            assert "frame-ancestors 'none'" in health.headers["content-security-policy"]
            with client.websocket_connect(
                "/ws?role=device",
                headers={"Authorization": f"Bearer {device_keys['alice']}"},
            ) as socket:
                hello = socket.receive_json()
                assert hello["type"] == "hello" and hello["device_id"] == device_ids["alice"]
            with client.websocket_connect(
                f"/ws?role=device&token={device_keys['bob']}"
            ) as socket:
                hello = socket.receive_json()
                assert hello["type"] == "hello" and hello["device_id"] == device_ids["bob"]

            shared = request_json(
                client,
                "POST",
                "/api/devices/connect",
                cookies["alice"],
                {"device_key": device_keys["bob"]},
            )
            assert shared["device"]["id"] == device_ids["bob"]
            assert shared["device"]["access_type"] == "shared"
            request_json(client, "GET", f"/api/devices/{device_ids['bob']}/status", cookies["alice"])
            request_json(
                client,
                "PATCH",
                f"/api/devices/{device_ids['bob']}",
                cookies["alice"],
                {"label": "Not Allowed"},
                expected=403,
            )

            alice_feedback = request_json(
                client,
                "POST",
                "/api/feedback",
                cookies["alice"],
                {
                    "category": "connection",
                    "device_id": device_ids["bob"],
                    "description": "Shared connection dropped during the local isolation test.",
                },
            )["feedback"]
            bob_feedback = request_json(
                client,
                "POST",
                "/api/feedback",
                cookies["bob"],
                {
                    "category": "device",
                    "device_id": device_ids["bob"],
                    "description": "Device diagnostics requested by the owner during testing.",
                },
            )["feedback"]
            alice_reports = request_json(client, "GET", "/api/feedback", cookies["alice"])
            bob_reports = request_json(client, "GET", "/api/feedback", cookies["bob"])
            assert {item["id"] for item in alice_reports["feedback"]} == {alice_feedback["id"]}
            assert {item["id"] for item in bob_reports["feedback"]} == {bob_feedback["id"]}

            admin_reports = request_json(client, "GET", "/api/feedback", admin_cookie)
            assert {alice_feedback["id"], bob_feedback["id"]} <= {
                item["id"] for item in admin_reports["feedback"]
            }
            request_json(
                client,
                "PATCH",
                f"/api/admin/feedback/{alice_feedback['id']}",
                admin_cookie,
                {"status": "resolved", "admin_reply": "Reviewed in the isolated test."},
            )
            updated = request_json(client, "GET", "/api/feedback", cookies["alice"])
            assert updated["feedback"][0]["status"] == "resolved"

            client.cookies.clear()
            anonymous = request_json(
                client,
                "POST",
                "/api/feedback",
                body={
                    "category": "account",
                    "reporter_name": "test visitor",
                    "description": "Anonymous feedback submitted without an authenticated session.",
                },
            )
            assert anonymous["feedback"]["status"] == "open"
            request_json(client, "GET", "/api/feedback", expected=401)

            request_json(client, "DELETE", f"/api/devices/{device_ids['alice']}", cookies["alice"])
            request_json(
                client,
                "DELETE",
                f"/api/devices/{device_ids['alice']}/record",
                cookies["alice"],
            )
            request_json(
                client,
                "GET",
                f"/api/devices/{device_ids['alice']}/status",
                cookies["alice"],
                expected=404,
            )

    print("MULTITENANT_INTEGRATION_OK")


if __name__ == "__main__":
    main()
