"""FUNF Relay multi-tenant control service."""

import asyncio
from contextlib import contextmanager
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from cryptography.fernet import Fernet, InvalidToken

from protocol import (
    build_control_frame,
    build_stop_frames,
    frames_to_hex,
    get_config,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("funf-relay")

API_TOKEN = os.environ.get("FUNF_API_TOKEN", "")
DEVICE_TOKEN = os.environ.get("FUNF_DEVICE_TOKEN", API_TOKEN)
ADMIN_USERNAME = os.environ.get("FUNF_ADMIN_USERNAME", "admin").strip().lower()
ADMIN_PASSWORD_HASH = os.environ.get("FUNF_ADMIN_PASSWORD_HASH", "")
DB_PATH = os.environ.get("FUNF_DB_PATH", "/var/lib/funf-relay/funf.db")
PUBLIC_ORIGIN = os.environ.get("FUNF_PUBLIC_ORIGIN", "http://localhost:3870").rstrip("/")
VAULT_KEY = os.environ.get("FUNF_VAULT_KEY", "")
SESSION_COOKIE = "funf_session"
SESSION_TTL = 12 * 60 * 60
HEARTBEAT_TIMEOUT = 35
CONTROL_LOCK_TIMEOUT = 60
SERVER_PING_INTERVAL = 20
PASSWORD_ITERATIONS = 310_000
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")
FEEDBACK_CATEGORIES = {"connection", "device", "account", "feature", "other"}
FEEDBACK_STATUSES = {"open", "in_progress", "resolved"}
FEEDBACK_SUBMISSIONS: Dict[str, list[float]] = {}


app = FastAPI(title="FUNF Relay", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[PUBLIC_ORIGIN],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
)


@app.middleware("http")
async def prevent_stale_dashboard(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self' ws: wss:; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    )
    if request.url.path in ("/", "/index.html"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


DB_LOCK = threading.RLock()
SESSIONS: Dict[str, Dict[str, Any]] = {}
LOGIN_FAILURES: Dict[str, list[float]] = {}
VAULT_FAILURES: Dict[str, list[float]] = {}
HUBS: Dict[str, "Hub"] = {}
VAULT = Fernet(VAULT_KEY.encode()) if VAULT_KEY else None


def now() -> float:
    return time.time()


@contextmanager
def db_connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    if VAULT is None:
        raise RuntimeError("FUNF_VAULT_KEY is required")
    os.makedirs(os.path.dirname(DB_PATH), mode=0o750, exist_ok=True)
    with DB_LOCK, db_connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                password_ciphertext TEXT,
                role TEXT NOT NULL CHECK(role IN ('admin', 'user')),
                enabled INTEGER NOT NULL DEFAULT 1,
                must_change_password INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                last_login REAL
            );
            CREATE TABLE IF NOT EXISTS device_keys (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL REFERENCES users(id),
                label TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                token_ciphertext TEXT,
                token_suffix TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                last_seen REAL
            );
            CREATE INDEX IF NOT EXISTS idx_device_owner ON device_keys(owner_id);
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at REAL NOT NULL,
                actor_id TEXT,
                action TEXT NOT NULL,
                target TEXT,
                details TEXT
            );
            CREATE TABLE IF NOT EXISTS feedback_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT REFERENCES users(id),
                reporter_name TEXT,
                category TEXT NOT NULL,
                device_id TEXT,
                device_label TEXT,
                description TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                admin_reply TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_feedback_user ON feedback_reports(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_feedback_status ON feedback_reports(status, created_at DESC);
            """
        )
        user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        if "password_ciphertext" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN password_ciphertext TEXT")
        device_columns = {row[1] for row in conn.execute("PRAGMA table_info(device_keys)")}
        if "token_ciphertext" not in device_columns:
            conn.execute("ALTER TABLE device_keys ADD COLUMN token_ciphertext TEXT")
        feedback_info = list(conn.execute("PRAGMA table_info(feedback_reports)"))
        if any(row[1] == "user_id" and row[3] for row in feedback_info):
            conn.executescript(
                """
                CREATE TABLE feedback_reports_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT REFERENCES users(id),
                    reporter_name TEXT,
                    category TEXT NOT NULL,
                    device_id TEXT,
                    device_label TEXT,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    admin_reply TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                INSERT INTO feedback_reports_new(
                    id, user_id, category, device_id, device_label, description,
                    status, admin_reply, created_at, updated_at
                )
                SELECT id, user_id, category, device_id, device_label, description,
                    status, admin_reply, created_at, updated_at
                FROM feedback_reports;
                DROP TABLE feedback_reports;
                ALTER TABLE feedback_reports_new RENAME TO feedback_reports;
                CREATE INDEX idx_feedback_user ON feedback_reports(user_id, created_at DESC);
                CREATE INDEX idx_feedback_status ON feedback_reports(status, created_at DESC);
                """
            )
            feedback_info = list(conn.execute("PRAGMA table_info(feedback_reports)"))
        feedback_columns = {row[1] for row in feedback_info}
        if "reporter_name" not in feedback_columns:
            conn.execute("ALTER TABLE feedback_reports ADD COLUMN reporter_name TEXT")
        admin = conn.execute("SELECT id FROM users WHERE role = 'admin' ORDER BY created_at LIMIT 1").fetchone()
        if admin is None:
            if not ADMIN_PASSWORD_HASH:
                raise RuntimeError("FUNF_ADMIN_PASSWORD_HASH is required for first startup")
            conn.execute(
                "INSERT INTO users(id, username, password_hash, role, enabled, created_at) "
                "VALUES(?, ?, ?, 'admin', 1, ?)",
                ("admin", ADMIN_USERNAME, ADMIN_PASSWORD_HASH, now()),
            )
        else:
            conn.execute(
                "UPDATE users SET username = ?, role = 'admin', enabled = 1 WHERE id = ?",
                (ADMIN_USERNAME, admin["id"]),
            )


def password_hash(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(actual, bytes.fromhex(digest_hex))
    except (TypeError, ValueError):
        return False


def device_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def vault_encrypt(value: str) -> str:
    if VAULT is None:
        raise RuntimeError("credential vault is unavailable")
    return VAULT.encrypt(value.encode()).decode()


def vault_decrypt(value: Optional[str]) -> Optional[str]:
    if not value or VAULT is None:
        return None
    try:
        return VAULT.decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        log.error("credential vault entry could not be decrypted")
        return None


def generate_password() -> str:
    return secrets.token_urlsafe(15)


def generate_device_token() -> str:
    return "funf_" + secrets.token_urlsafe(32)


def normalize_username(value: Any) -> str:
    username = str(value or "").strip().lower()
    if not USERNAME_RE.fullmatch(username):
        raise HTTPException(400, "用户名需为 3-32 位字母、数字、点、下划线或短横线")
    return username


def clean_label(value: Any) -> str:
    label = str(value or "").strip()
    if not 1 <= len(label) <= 64:
        raise HTTPException(400, "设备名称需为 1-64 个字符")
    return label


def clean_feedback(body: Dict[str, Any]) -> tuple[str, str, str]:
    category = str(body.get("category", "")).strip().lower()
    if category not in FEEDBACK_CATEGORIES:
        raise HTTPException(400, "请选择有效的问题类型")
    description = str(body.get("description", "")).strip()
    if not 10 <= len(description) <= 2000:
        raise HTTPException(400, "详细描述需为 10-2000 个字符")
    reporter_name = str(body.get("reporter_name", "")).strip()
    if len(reporter_name) > 80:
        raise HTTPException(400, "称呼或联系方式不能超过 80 个字符")
    return category, description, reporter_name


def get_user(user_id: str) -> Optional[Dict[str, Any]]:
    with DB_LOCK, db_connect() as conn:
        row = conn.execute(
            "SELECT id, username, role, enabled, must_change_password, created_at, last_login "
            "FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def get_user_with_password(username: str) -> Optional[Dict[str, Any]]:
    with DB_LOCK, db_connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()
    return dict(row) if row else None


def audit(actor_id: Optional[str], action: str, target: str = "", details: str = "") -> None:
    with DB_LOCK, db_connect() as conn:
        conn.execute(
            "INSERT INTO audit_events(at, actor_id, action, target, details) VALUES(?, ?, ?, ?, ?)",
            (now(), actor_id, action, target[:128], details[:500]),
        )


def new_session(user: Dict[str, Any]) -> str:
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {
        "user_id": user["id"],
        "expires": now() + SESSION_TTL,
        "device_grants": {},
    }
    return token


def session_user_from_token(token: str) -> Optional[Dict[str, Any]]:
    session = SESSIONS.get(token)
    if not session or session["expires"] <= now():
        SESSIONS.pop(token, None)
        return None
    user = get_user(session["user_id"])
    if not user or not user["enabled"]:
        SESSIONS.pop(token, None)
        return None
    session["expires"] = now() + SESSION_TTL
    return user


def require_user(request: Request, allow_password_change: bool = False) -> Dict[str, Any]:
    user = session_user_from_token(request.cookies.get(SESSION_COOKIE, ""))
    if not user:
        raise HTTPException(401, "请先登录")
    if user["must_change_password"] and not allow_password_change:
        raise HTTPException(403, "必须先修改初始密码")
    return user


def require_admin(request: Request) -> Dict[str, Any]:
    user = require_user(request)
    if user["role"] != "admin":
        raise HTTPException(403, "需要管理员权限")
    return user


def revoke_user_sessions(user_id: str, except_token: str = "") -> None:
    for token, session in list(SESSIONS.items()):
        if session["user_id"] == user_id and token != except_token:
            SESSIONS.pop(token, None)


def get_device(device_id: str) -> Optional[Dict[str, Any]]:
    if device_id == "legacy" and DEVICE_TOKEN:
        admin = get_user("admin")
        return {
            "id": "legacy",
            "owner_id": "admin",
            "owner_username": admin["username"] if admin else ADMIN_USERNAME,
            "label": "旧设备",
            "token_suffix": "legacy",
            "enabled": 1,
            "created_at": 0,
            "last_seen": None,
            "legacy": True,
        }
    with DB_LOCK, db_connect() as conn:
        row = conn.execute(
            "SELECT d.*, u.username AS owner_username FROM device_keys d "
            "JOIN users u ON u.id = d.owner_id WHERE d.id = ?",
            (device_id,),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["legacy"] = False
    return result


def device_current_token_hash(device: Dict[str, Any]) -> str:
    if device.get("legacy"):
        return device_token_hash(DEVICE_TOKEN) if DEVICE_TOKEN else ""
    return str(device.get("token_hash", ""))


def can_manage_device(user: Dict[str, Any], device: Dict[str, Any]) -> bool:
    return user["role"] == "admin" or device["owner_id"] == user["id"]


def can_access_device(user: Dict[str, Any], device: Dict[str, Any], session_token: str = "") -> bool:
    if can_manage_device(user, device):
        return True
    session = SESSIONS.get(session_token)
    if not session or session.get("user_id") != user["id"]:
        return False
    granted_hash = session.get("device_grants", {}).get(device["id"], "")
    current_hash = device_current_token_hash(device)
    return bool(granted_hash and current_hash and hmac.compare_digest(granted_hash, current_hash))


def require_device_access(user: Dict[str, Any], device_id: str, session_token: str = "") -> Dict[str, Any]:
    device = get_device(device_id)
    if not device or not device["enabled"]:
        raise HTTPException(404, "设备不存在或密钥已吊销")
    if not can_access_device(user, device, session_token):
        raise HTTPException(403, "无权访问此设备")
    return device


def require_device_manager(user: Dict[str, Any], device_id: str, allow_disabled: bool = False) -> Dict[str, Any]:
    device = get_device(device_id)
    if not device or (not allow_disabled and not device["enabled"]):
        raise HTTPException(404, "设备不存在或密钥已吊销")
    if not can_manage_device(user, device):
        raise HTTPException(403, "只有设备所有者或管理员可以管理此设备")
    return device


def authenticate_device_token(token: str) -> Optional[Dict[str, Any]]:
    if DEVICE_TOKEN and hmac.compare_digest(token, DEVICE_TOKEN):
        return get_device("legacy")
    if not token:
        return None
    digest = device_token_hash(token)
    with DB_LOCK, db_connect() as conn:
        row = conn.execute(
            "SELECT d.*, u.username AS owner_username FROM device_keys d "
            "JOIN users u ON u.id = d.owner_id "
            "WHERE d.token_hash = ? AND d.enabled = 1 AND u.enabled = 1",
            (digest,),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["legacy"] = False
    return result


def touch_device(device_id: str) -> None:
    if device_id == "legacy":
        return
    with DB_LOCK, db_connect() as conn:
        conn.execute("UPDATE device_keys SET last_seen = ? WHERE id = ?", (now(), device_id))


class Peer:
    def __init__(self, ws: WebSocket, role: str, device_id: str, user_id: str = ""):
        self.ws = ws
        self.role = role
        self.device_id = device_id
        self.user_id = user_id
        self.alive = True
        self.connected_at = now()
        self.last_seen = now()
        self.last_persisted_seen = 0.0
        self.replaced = False
        self.disconnect_reason = ""

    async def send(self, message: Dict[str, Any]) -> None:
        if not self.alive:
            return
        try:
            await self.ws.send_json(message)
        except Exception as exc:
            log.warning("send to %s/%s failed: %s", self.device_id, self.role, exc)
            if not self.disconnect_reason:
                self.disconnect_reason = "send_failed"
            self.alive = False


class Hub:
    def __init__(self, device_id: str):
        self.device_id = device_id
        self.controller: Optional[Peer] = None
        self.device: Optional[Peer] = None
        self.lock_holder: Optional[str] = None
        self.lock_at = 0.0
        self.device_state: Dict[str, Any] = {
            "ble_connected": False,
            "ble_connecting": False,
            "suction": 0,
            "vibration": 0,
            "electric": 0,
            "insertion": 0,
            "suction_mode": 1,
            "vibration_mode": 1,
            "electric_mode": 1,
            "insertion_mode": 1,
            "last_command_at": 0,
            "last_command_result": None,
        }
        self.command_log: list[Dict[str, Any]] = []

    def log_command(self, entry: Dict[str, Any]) -> None:
        self.command_log.append(entry)
        if len(self.command_log) > 50:
            self.command_log.pop(0)

    def acquire_lock(self, role: str) -> bool:
        if self.lock_holder is None or now() - self.lock_at > CONTROL_LOCK_TIMEOUT:
            self.lock_holder = role
            self.lock_at = now()
            return True
        return self.lock_holder == role

    def release_lock(self) -> None:
        self.lock_holder = None
        self.lock_at = 0

    async def notify_peer(self, role: str, message: Dict[str, Any]) -> None:
        peer = self.device if role == "controller" else self.controller
        if peer and peer.alive:
            await peer.send(message)

    async def close_all(self, reason: str) -> None:
        for peer in (self.controller, self.device):
            if peer and peer.alive:
                peer.disconnect_reason = reason
                peer.alive = False
                try:
                    await peer.ws.close(code=1008, reason=reason)
                except Exception:
                    pass
        self.controller = None
        self.device = None
        self.release_lock()


def get_hub(device_id: str) -> Hub:
    if device_id not in HUBS:
        HUBS[device_id] = Hub(device_id)
    return HUBS[device_id]


def device_json(device: Dict[str, Any]) -> Dict[str, Any]:
    hub = HUBS.get(device["id"])
    return {
        "id": device["id"],
        "owner_id": device["owner_id"],
        "owner_username": device["owner_username"],
        "label": device["label"],
        "token_suffix": device["token_suffix"],
        "enabled": bool(device["enabled"]),
        "legacy": bool(device.get("legacy")),
        "created_at": device["created_at"],
        "last_seen": device["last_seen"],
        "device_online": bool(hub and hub.device and hub.device.alive),
        "controller_online": bool(hub and hub.controller and hub.controller.alive),
        "ble_connected": bool(hub and hub.device_state["ble_connected"]),
    }


def list_devices(user: Dict[str, Any], session_token: str = "") -> list[Dict[str, Any]]:
    with DB_LOCK, db_connect() as conn:
        rows = conn.execute(
            "SELECT d.*, u.username AS owner_username FROM device_keys d "
            "JOIN users u ON u.id = d.owner_id ORDER BY d.enabled DESC, d.created_at DESC",
        ).fetchall()
    visible = []
    for row in rows:
        device = {**dict(row), "legacy": False}
        if can_access_device(user, device, session_token) and (device["enabled"] or can_manage_device(user, device)):
            item = device_json(device)
            item["can_manage"] = can_manage_device(user, device)
            item["access_type"] = "owned" if device["owner_id"] == user["id"] else ("admin" if user["role"] == "admin" else "shared")
            visible.append(item)
    if DEVICE_TOKEN:
        legacy = get_device("legacy")
        if legacy and can_access_device(user, legacy, session_token):
            item = device_json(legacy)
            item["can_manage"] = user["role"] == "admin"
            item["access_type"] = "admin" if user["role"] == "admin" else "shared"
            visible.insert(0, item)
    return visible


def build_dispatch_from_state(state: Dict[str, Any]) -> list[str]:
    frames = []
    for function in ("suction", "vibration", "electric", "insertion"):
        frame = build_control_frame(function, state.get(function, 0), state.get(f"{function}_mode", 1))
        if frame is not None:
            frames.append(frame.hex())
    return frames


async def dispatch_state(hub: Hub, state: Dict[str, Any], source: str = "controller") -> Dict[str, Any]:
    if not hub.device or not hub.device.alive:
        raise HTTPException(503, "设备离线")
    if not hub.acquire_lock("controller"):
        raise HTTPException(423, "控制锁由另一端持有")
    frames = build_dispatch_from_state(state)
    timestamp = now()
    hub.log_command({"at": timestamp, "source": source, "state": state, "frames": frames})
    await hub.device.send({"type": "command", "state": state, "frames": frames, "ts": timestamp})
    for key in ("suction", "vibration", "electric", "insertion"):
        hub.device_state[key] = state.get(key, 0)
        if f"{key}_mode" in state:
            hub.device_state[f"{key}_mode"] = state[f"{key}_mode"]
    hub.device_state["last_command_at"] = timestamp
    return {"frames_sent": len(frames), "state": {key: state.get(key, 0) for key in ("suction", "vibration", "electric", "insertion")}}


@app.on_event("startup")
async def startup() -> None:
    init_db()


@app.get("/healthz")
async def healthz() -> Dict[str, Any]:
    return {
        "ok": True,
        "devices_online": sum(bool(h.device and h.device.alive) for h in HUBS.values()),
        "controllers_online": sum(bool(h.controller and h.controller.alive) for h in HUBS.values()),
    }


@app.post("/api/login")
async def login(request: Request) -> JSONResponse:
    body = await request.json()
    username = str(body.get("username", "")).strip().lower()
    password = str(body.get("password", ""))
    client = request.client.host if request.client else "unknown"
    key = f"{client}|{username}"
    cutoff = now() - 15 * 60
    failures = [timestamp for timestamp in LOGIN_FAILURES.get(key, []) if timestamp > cutoff]
    if len(failures) >= 8:
        raise HTTPException(429, "登录失败次数过多，请稍后再试")
    user = get_user_with_password(username)
    if not user or not user["enabled"] or not password_matches(password, user["password_hash"]):
        failures.append(now())
        LOGIN_FAILURES[key] = failures
        audit(user["id"] if user else None, "login_failed", username)
        await asyncio.sleep(0.35)
        raise HTTPException(401, "用户名或密码错误")
    LOGIN_FAILURES.pop(key, None)
    with DB_LOCK, db_connect() as conn:
        conn.execute(
            "UPDATE users SET last_login = ?, password_ciphertext = ? WHERE id = ?",
            (now(), vault_encrypt(password), user["id"]),
        )
    token = new_session(user)
    audit(user["id"], "login", user["username"])
    response = JSONResponse({"ok": True, "user": {key: user[key] for key in ("id", "username", "role", "must_change_password")}})
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_TTL,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    return response


@app.post("/api/logout")
async def logout(request: Request) -> JSONResponse:
    token = request.cookies.get(SESSION_COOKIE, "")
    user = session_user_from_token(token)
    SESSIONS.pop(token, None)
    if user:
        audit(user["id"], "logout", user["username"])
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, samesite="strict")
    return response


@app.get("/api/session")
async def session_info(request: Request) -> Dict[str, Any]:
    user = require_user(request, allow_password_change=True)
    return {"user": user}


@app.post("/api/account/password")
async def change_password(request: Request) -> Dict[str, Any]:
    user = require_user(request, allow_password_change=True)
    body = await request.json()
    current = str(body.get("current_password", ""))
    new_password = str(body.get("new_password", ""))
    stored = get_user_with_password(user["username"])
    if not stored or not password_matches(current, stored["password_hash"]):
        raise HTTPException(400, "当前密码错误")
    if len(new_password) < 10:
        raise HTTPException(400, "新密码至少需要 10 个字符")
    with DB_LOCK, db_connect() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ?, password_ciphertext = ?, must_change_password = 0 WHERE id = ?",
            (password_hash(new_password), vault_encrypt(new_password), user["id"]),
        )
    current_token = request.cookies.get(SESSION_COOKIE, "")
    revoke_user_sessions(user["id"], except_token=current_token)
    audit(user["id"], "password_changed", user["username"])
    return {"ok": True}


@app.get("/api/users")
async def users_list(request: Request) -> Dict[str, Any]:
    require_admin(request)
    with DB_LOCK, db_connect() as conn:
        rows = conn.execute(
            "SELECT u.id, u.username, u.role, u.enabled, u.must_change_password, u.created_at, u.last_login, "
            "COUNT(d.id) AS device_count FROM users u LEFT JOIN device_keys d ON d.owner_id = u.id "
            "GROUP BY u.id ORDER BY u.role, u.created_at"
        ).fetchall()
    return {"users": [dict(row) for row in rows]}


@app.post("/api/users")
async def create_user(request: Request) -> Dict[str, Any]:
    actor = require_admin(request)
    body = await request.json()
    username = normalize_username(body.get("username"))
    initial_password = generate_password()
    user_id = secrets.token_hex(12)
    try:
        with DB_LOCK, db_connect() as conn:
            conn.execute(
                "INSERT INTO users(id, username, password_hash, password_ciphertext, role, enabled, must_change_password, created_at) "
                "VALUES(?, ?, ?, ?, 'user', 1, 1, ?)",
                (user_id, username, password_hash(initial_password), vault_encrypt(initial_password), now()),
            )
    except sqlite3.IntegrityError:
        raise HTTPException(409, "用户名已存在")
    audit(actor["id"], "user_created", user_id, username)
    return {"user": {"id": user_id, "username": username, "role": "user", "enabled": True}, "initial_password": initial_password}


@app.patch("/api/users/{user_id}")
async def update_user(user_id: str, request: Request) -> Dict[str, Any]:
    actor = require_admin(request)
    if user_id == actor["id"]:
        raise HTTPException(400, "不能停用当前管理员")
    target = get_user(user_id)
    if not target or target["role"] == "admin":
        raise HTTPException(404, "用户不存在")
    body = await request.json()
    if "enabled" not in body:
        raise HTTPException(400, "缺少 enabled")
    enabled = 1 if bool(body["enabled"]) else 0
    with DB_LOCK, db_connect() as conn:
        conn.execute("UPDATE users SET enabled = ? WHERE id = ?", (enabled, user_id))
    if not enabled:
        revoke_user_sessions(user_id)
        for device in list_devices(actor):
            if device["owner_id"] == user_id and device["id"] in HUBS:
                await HUBS[device["id"]].close_all("user disabled")
    audit(actor["id"], "user_enabled" if enabled else "user_disabled", user_id, target["username"])
    return {"ok": True, "enabled": bool(enabled)}


@app.post("/api/users/{user_id}/reset-password")
async def reset_user_password(user_id: str, request: Request) -> Dict[str, Any]:
    actor = require_admin(request)
    target = get_user(user_id)
    if not target or target["role"] == "admin":
        raise HTTPException(404, "用户不存在")
    temporary = generate_password()
    with DB_LOCK, db_connect() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ?, password_ciphertext = ?, must_change_password = 1 WHERE id = ?",
            (password_hash(temporary), vault_encrypt(temporary), user_id),
        )
    revoke_user_sessions(user_id)
    audit(actor["id"], "user_password_reset", user_id, target["username"])
    return {"ok": True, "temporary_password": temporary}


@app.get("/api/devices")
async def devices_list(request: Request) -> Dict[str, Any]:
    user = require_user(request)
    session_token = request.cookies.get(SESSION_COOKIE, "")
    return {"devices": list_devices(user, session_token)}


@app.post("/api/devices")
async def create_device(request: Request) -> Dict[str, Any]:
    user = require_user(request)
    body = await request.json()
    label = clean_label(body.get("label"))
    owner_id = user["id"]
    if user["role"] == "admin" and body.get("owner_id"):
        owner_id = str(body["owner_id"])
        owner = get_user(owner_id)
        if not owner or not owner["enabled"]:
            raise HTTPException(400, "设备所属用户不存在或已停用")
    raw_token = generate_device_token()
    device_id = secrets.token_hex(12)
    with DB_LOCK, db_connect() as conn:
        conn.execute(
            "INSERT INTO device_keys(id, owner_id, label, token_hash, token_ciphertext, token_suffix, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (device_id, owner_id, label, device_token_hash(raw_token), vault_encrypt(raw_token), raw_token[-6:], now()),
        )
    audit(user["id"], "device_created", device_id, f"owner={owner_id};label={label}")
    return {"device": device_json(get_device(device_id)), "device_key": raw_token}


@app.post("/api/devices/connect")
async def connect_device_with_key(request: Request) -> Dict[str, Any]:
    user = require_user(request)
    session_token = request.cookies.get(SESSION_COOKIE, "")
    session = SESSIONS.get(session_token)
    if not session or session.get("user_id") != user["id"]:
        raise HTTPException(401, "登录会话已失效")
    body = await request.json()
    raw_token = str(body.get("device_key", "")).strip()
    device = authenticate_device_token(raw_token)
    if not device:
        audit(user["id"], "device_access_failed", "device_key")
        raise HTTPException(401, "设备密钥无效或已吊销")
    session.setdefault("device_grants", {})[device["id"]] = device_token_hash(raw_token)
    audit(user["id"], "device_access_granted", device["id"], f"owner={device['owner_id']}")
    item = device_json(device)
    item["can_manage"] = can_manage_device(user, device)
    item["access_type"] = "owned" if device["owner_id"] == user["id"] else ("admin" if user["role"] == "admin" else "shared")
    return {"ok": True, "device": item}


@app.patch("/api/devices/{device_id}")
async def rename_device(device_id: str, request: Request) -> Dict[str, Any]:
    user = require_user(request)
    device = require_device_manager(user, device_id)
    if device.get("legacy"):
        raise HTTPException(400, "旧设备不能重命名")
    body = await request.json()
    label = clean_label(body.get("label"))
    with DB_LOCK, db_connect() as conn:
        conn.execute("UPDATE device_keys SET label = ? WHERE id = ?", (label, device_id))
    audit(user["id"], "device_renamed", device_id, label)
    return {"ok": True, "label": label}


@app.post("/api/devices/{device_id}/rotate")
async def rotate_device_key(device_id: str, request: Request) -> Dict[str, Any]:
    user = require_user(request)
    device = require_device_manager(user, device_id)
    if device.get("legacy"):
        raise HTTPException(400, "旧设备密钥由服务器配置管理")
    raw_token = generate_device_token()
    with DB_LOCK, db_connect() as conn:
        conn.execute(
            "UPDATE device_keys SET token_hash = ?, token_ciphertext = ?, token_suffix = ?, last_seen = NULL WHERE id = ?",
            (device_token_hash(raw_token), vault_encrypt(raw_token), raw_token[-6:], device_id),
        )
    if device_id in HUBS:
        await HUBS[device_id].close_all("device key rotated")
        HUBS.pop(device_id, None)
    audit(user["id"], "device_key_rotated", device_id)
    return {"ok": True, "device_key": raw_token}


@app.delete("/api/devices/{device_id}")
async def revoke_device(device_id: str, request: Request) -> Dict[str, Any]:
    user = require_user(request)
    device = require_device_manager(user, device_id)
    if device.get("legacy"):
        raise HTTPException(400, "旧设备只能通过服务器配置停用")
    with DB_LOCK, db_connect() as conn:
        conn.execute("UPDATE device_keys SET enabled = 0 WHERE id = ?", (device_id,))
    if device_id in HUBS:
        await HUBS[device_id].close_all("device key revoked")
        HUBS.pop(device_id, None)
    audit(user["id"], "device_revoked", device_id, device["label"])
    return {"ok": True}


@app.delete("/api/devices/{device_id}/record")
async def delete_device_record(device_id: str, request: Request) -> Dict[str, Any]:
    user = require_user(request)
    device = require_device_manager(user, device_id, allow_disabled=True)
    if device.get("legacy"):
        raise HTTPException(400, "旧设备不能从这里删除")
    if device["enabled"]:
        raise HTTPException(409, "请先吊销设备，再永久删除")
    with DB_LOCK, db_connect() as conn:
        conn.execute("DELETE FROM device_keys WHERE id = ?", (device_id,))
    for session in SESSIONS.values():
        session.get("device_grants", {}).pop(device_id, None)
    HUBS.pop(device_id, None)
    audit(user["id"], "device_deleted", device_id, device["label"])
    return {"ok": True}


@app.get("/api/devices/{device_id}/status")
async def device_status(device_id: str, request: Request) -> Dict[str, Any]:
    user = require_user(request)
    device = require_device_access(user, device_id, request.cookies.get(SESSION_COOKIE, ""))
    hub = get_hub(device_id)
    return {
        "device": device_json(device),
        "device_state": hub.device_state,
        "lock_holder": hub.lock_holder,
        "recent_commands": hub.command_log[-10:],
    }


@app.post("/api/feedback")
async def create_feedback(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "请求格式无效") from exc
    if not isinstance(body, dict):
        raise HTTPException(400, "请求格式无效")
    category, description, reporter_name = clean_feedback(body)
    session_token = request.cookies.get(SESSION_COOKIE, "")
    user = session_user_from_token(session_token)
    device_id = str(body.get("device_id", "")).strip()
    device_label = None

    if user:
        reporter_name = user["username"]
        if device_id:
            device = require_device_access(user, device_id, session_token)
            device_label = device["label"]
    elif device_id:
        raise HTTPException(400, "访客反馈不能关联设备，请登录后提交")
    else:
        client = request.client.host if request.client else "unknown"
        cutoff = now() - 60 * 60
        recent = [timestamp for timestamp in FEEDBACK_SUBMISSIONS.get(client, []) if timestamp > cutoff]
        if len(recent) >= 5:
            raise HTTPException(429, "提交过于频繁，请稍后再试")
        recent.append(now())
        FEEDBACK_SUBMISSIONS[client] = recent
        reporter_name = reporter_name or "访客"

    created_at = now()
    with DB_LOCK, db_connect() as conn:
        cursor = conn.execute(
            "INSERT INTO feedback_reports(user_id, reporter_name, category, device_id, device_label, "
            "description, status, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, 'open', ?, ?)",
            (
                user["id"] if user else None,
                reporter_name,
                category,
                device_id or None,
                device_label,
                description,
                created_at,
                created_at,
            ),
        )
        feedback_id = int(cursor.lastrowid)
    audit(
        user["id"] if user else None,
        "feedback_created",
        str(feedback_id),
        f"category={category};device={device_id or '-'}",
    )
    return {"ok": True, "feedback": {"id": feedback_id, "status": "open", "created_at": created_at}}


@app.get("/api/feedback")
async def feedback_list(request: Request) -> Dict[str, Any]:
    user = require_user(request)
    query = (
        "SELECT f.id, f.user_id, COALESCE(u.username, f.reporter_name, '访客') AS reporter_name, "
        "f.category, f.device_id, f.device_label, f.description, f.status, f.admin_reply, "
        "f.created_at, f.updated_at FROM feedback_reports f LEFT JOIN users u ON u.id = f.user_id "
    )
    parameters: tuple[Any, ...] = ()
    if user["role"] != "admin":
        query += "WHERE f.user_id = ? "
        parameters = (user["id"],)
    query += "ORDER BY f.created_at DESC LIMIT 100"
    with DB_LOCK, db_connect() as conn:
        rows = conn.execute(query, parameters).fetchall()
    return {"feedback": [dict(row) for row in rows]}


@app.patch("/api/admin/feedback/{feedback_id}")
async def update_feedback(feedback_id: int, request: Request) -> Dict[str, Any]:
    actor = require_admin(request)
    body = await request.json()
    status = str(body.get("status", "")).strip().lower()
    if status not in FEEDBACK_STATUSES:
        raise HTTPException(400, "请选择有效的处理状态")
    admin_reply = str(body.get("admin_reply", "")).strip()
    if len(admin_reply) > 2000:
        raise HTTPException(400, "管理员回复不能超过 2000 个字符")
    updated_at = now()
    with DB_LOCK, db_connect() as conn:
        existing = conn.execute("SELECT id FROM feedback_reports WHERE id = ?", (feedback_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "反馈记录不存在")
        conn.execute(
            "UPDATE feedback_reports SET status = ?, admin_reply = ?, updated_at = ? WHERE id = ?",
            (status, admin_reply or None, updated_at, feedback_id),
        )
    audit(actor["id"], "feedback_updated", str(feedback_id), f"status={status}")
    return {"ok": True, "status": status, "admin_reply": admin_reply, "updated_at": updated_at}


@app.get("/api/audit")
async def audit_list(request: Request) -> Dict[str, Any]:
    require_admin(request)
    with DB_LOCK, db_connect() as conn:
        rows = conn.execute(
            "SELECT a.id, a.at, a.action, a.target, a.details, u.username AS actor_username "
            "FROM audit_events a LEFT JOIN users u ON u.id = a.actor_id ORDER BY a.id DESC LIMIT 100"
        ).fetchall()
    return {"events": [dict(row) for row in rows]}


@app.post("/api/admin/vault")
async def admin_vault(request: Request) -> JSONResponse:
    actor = require_admin(request)
    body = await request.json()
    password = str(body.get("password", ""))
    client = request.client.host if request.client else "unknown"
    failure_key = f"{client}|{actor['id']}"
    cutoff = now() - 15 * 60
    failures = [timestamp for timestamp in VAULT_FAILURES.get(failure_key, []) if timestamp > cutoff]
    if len(failures) >= 5:
        raise HTTPException(429, "保险库解锁失败次数过多，请稍后再试")
    stored = get_user_with_password(actor["username"])
    if not stored or not password_matches(password, stored["password_hash"]):
        failures.append(now())
        VAULT_FAILURES[failure_key] = failures
        audit(actor["id"], "vault_unlock_failed", actor["username"])
        await asyncio.sleep(0.35)
        raise HTTPException(401, "管理员密码错误")
    VAULT_FAILURES.pop(failure_key, None)

    with DB_LOCK, db_connect() as conn:
        encrypted_admin_password = vault_encrypt(password)
        conn.execute(
            "UPDATE users SET password_ciphertext = ? WHERE id = ?",
            (encrypted_admin_password, actor["id"]),
        )
        user_rows = conn.execute(
            "SELECT id, username, role, enabled, must_change_password, password_ciphertext "
            "FROM users ORDER BY role, created_at"
        ).fetchall()
        device_rows = conn.execute(
            "SELECT d.id, d.label, d.enabled, d.token_ciphertext, d.token_suffix, u.username AS owner_username "
            "FROM device_keys d JOIN users u ON u.id = d.owner_id ORDER BY d.enabled DESC, d.created_at DESC"
        ).fetchall()

    users = []
    for row in user_rows:
        users.append(
            {
                "id": row["id"],
                "username": row["username"],
                "role": row["role"],
                "enabled": bool(row["enabled"]),
                "must_change_password": bool(row["must_change_password"]),
                "password": vault_decrypt(row["password_ciphertext"]),
            }
        )
    devices = []
    for row in device_rows:
        devices.append(
            {
                "id": row["id"],
                "label": row["label"],
                "owner_username": row["owner_username"],
                "enabled": bool(row["enabled"]),
                "token_suffix": row["token_suffix"],
                "device_key": vault_decrypt(row["token_ciphertext"]),
                "legacy": False,
            }
        )
    if DEVICE_TOKEN:
        devices.insert(
            0,
            {
                "id": "legacy",
                "label": "旧设备",
                "owner_username": actor["username"],
                "enabled": True,
                "token_suffix": "legacy",
                "device_key": DEVICE_TOKEN,
                "legacy": True,
            },
        )

    audit(actor["id"], "vault_viewed", "all_credentials", f"users={len(users)};devices={len(devices)}")
    response = JSONResponse(
        {
            "users": users,
            "devices": devices,
            "server_keys": [
                {"name": "Controller API Token", "value": API_TOKEN},
                {"name": "Legacy Device Token", "value": DEVICE_TOKEN},
            ],
        }
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.get("/status")
async def legacy_status(request: Request) -> Dict[str, Any]:
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if API_TOKEN and not hmac.compare_digest(token, API_TOKEN):
        raise HTTPException(401, "unauthorized")
    hub = get_hub("legacy")
    return {
        "controller_online": bool(hub.controller and hub.controller.alive),
        "device_online": bool(hub.device and hub.device.alive),
        "device_state": hub.device_state,
        "lock_holder": hub.lock_holder,
        "recent_commands": hub.command_log[-10:],
    }


@app.get("/mcp")
async def mcp_stub() -> Dict[str, Any]:
    return {
        "ok": True,
        "msg": "MCP endpoint reserved",
        "config_functions": list(get_config()["functions"].keys()),
        "presets": [key for key in get_config().get("presets", {}) if not key.startswith("_")],
    }


@app.post("/api/preset/{name}")
async def legacy_apply_preset(name: str, request: Request) -> Dict[str, Any]:
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if API_TOKEN and not hmac.compare_digest(token, API_TOKEN):
        raise HTTPException(401, "unauthorized")
    presets = {key: value for key, value in get_config().get("presets", {}).items() if not key.startswith("_")}
    if name not in presets:
        raise HTTPException(404, "preset not found")
    preset = presets[name]
    state = {
        "suction": preset.get("suction", 0),
        "vibration": preset.get("vibration", 0),
        "electric": preset.get("electric", 0),
        "insertion": preset.get("insertion", 0),
        "suction_mode": 1,
        "vibration_mode": 1,
        "electric_mode": 1,
        "insertion_mode": 1,
    }
    return {"ok": True, **await dispatch_state(get_hub("legacy"), state, source="preset:" + name)}


async def heartbeat_watchdog(peer: Peer) -> None:
    while peer.alive:
        await asyncio.sleep(5)
        if peer.alive and now() - peer.last_seen > HEARTBEAT_TIMEOUT:
            log.warning("%s/%s heartbeat timeout", peer.device_id, peer.role)
            peer.disconnect_reason = "heartbeat_timeout"
            try:
                await peer.ws.close(code=1001, reason="heartbeat timeout")
            except Exception:
                pass
            peer.alive = False
            return


async def server_keepalive(peer: Peer) -> None:
    while peer.alive:
        await asyncio.sleep(SERVER_PING_INTERVAL)
        if peer.alive:
            await peer.send({"type": "ping", "t": now()})


async def websocket_loop(ws: WebSocket, role: str) -> None:
    await ws.accept()
    device_id = ""
    user_id = ""
    controller_session_token = ""
    controller_api_auth = False
    controller_access = ""
    if role == "device":
        header_token = ws.headers.get("authorization", "").removeprefix("Bearer ").strip()
        device = authenticate_device_token(header_token or ws.query_params.get("token", ""))
        if not device:
            await ws.close(code=1008, reason="bad device key")
            return
        device_id = device["id"]
        touch_device(device_id)
    else:
        controller_session_token = ws.cookies.get(SESSION_COOKIE, "")
        user = session_user_from_token(controller_session_token)
        if not user and API_TOKEN and hmac.compare_digest(ws.query_params.get("token", ""), API_TOKEN):
            user = get_user("admin")
            device_id = "legacy"
            controller_api_auth = True
        if not user or user["must_change_password"]:
            await ws.close(code=1008, reason="login required")
            return
        origin = ws.headers.get("origin")
        if origin and origin != PUBLIC_ORIGIN:
            await ws.close(code=1008, reason="bad origin")
            return
        device_id = device_id or ws.query_params.get("device", "")
        try:
            device = require_device_access(user, device_id, controller_session_token)
        except HTTPException:
            await ws.close(code=1008, reason="device access denied")
            return
        user_id = user["id"]
        if controller_api_auth:
            controller_access = "legacy_api"
        elif device["owner_id"] == user["id"]:
            controller_access = "owned"
        elif user["role"] == "admin":
            controller_access = "admin"
        else:
            controller_access = "shared"

    hub = get_hub(device_id)
    peer = Peer(ws, role, device_id, user_id)
    old = getattr(hub, role)
    if old and old.alive:
        idle = now() - old.last_seen
        if idle < 15:
            await ws.close(code=1008, reason="already connected")
            return
        old.replaced = True
        old.disconnect_reason = "replaced"
        old.alive = False
        try:
            await old.ws.close(code=1000, reason="replaced")
        except Exception:
            pass
    setattr(hub, role, peer)
    log.info("%s/%s connected", device_id, role)
    if role == "controller":
        audit(user_id or None, "controller_connected", device_id, f"access={controller_access or 'unknown'}")
    asyncio.create_task(heartbeat_watchdog(peer))
    asyncio.create_task(server_keepalive(peer))
    await hub.notify_peer(role, {"type": "peer_connected", "role": role})
    await peer.send(
        {
            "type": "hello",
            "role": role,
            "device_id": device_id,
            "peer_online": bool(hub.device and hub.device.alive) if role == "controller" else bool(hub.controller and hub.controller.alive),
            "device_state": hub.device_state,
            "lock_holder": hub.lock_holder,
        }
    )

    try:
        while peer.alive:
            raw = await ws.receive_text()
            peer.last_seen = now()
            if role == "device" and peer.last_seen - peer.last_persisted_seen > 30:
                touch_device(device_id)
                peer.last_persisted_seen = peer.last_seen
            try:
                message = json.loads(raw)
            except Exception:
                continue
            message_type = message.get("type")
            if role == "controller" and not controller_api_auth:
                active_user = session_user_from_token(controller_session_token)
                try:
                    if not active_user or active_user["id"] != user_id:
                        raise HTTPException(401, "登录会话已失效")
                    require_device_access(active_user, device_id, controller_session_token)
                except HTTPException:
                    peer.disconnect_reason = "access_expired"
                    await ws.close(code=1008, reason="device access expired")
                    peer.alive = False
                    break
            if message_type == "heartbeat":
                await peer.send({"type": "heartbeat_ack", "t": now()})
                continue
            if message_type == "pong":
                continue

            if role == "controller":
                if message_type == "command":
                    try:
                        result = await dispatch_state(hub, message.get("state", {}))
                        await peer.send({"type": "command_ack", "ok": True, **result})
                    except HTTPException as exc:
                        await peer.send({"type": "command_ack", "ok": False, "detail": exc.detail, "status": exc.status_code})
                    except Exception as exc:
                        log.exception("dispatch error")
                        await peer.send({"type": "command_ack", "ok": False, "detail": str(exc)})
                elif message_type == "stop":
                    frames = frames_to_hex(build_stop_frames())
                    timestamp = now()
                    hub.log_command({"at": timestamp, "source": "stop", "frames": frames})
                    if hub.device and hub.device.alive:
                        await hub.device.send({"type": "stop", "frames": frames, "ts": timestamp})
                    for key in ("suction", "vibration", "electric", "insertion"):
                        hub.device_state[key] = 0
                    hub.device_state["last_command_at"] = timestamp
                    await peer.send({"type": "stop_ack", "ok": True})
                elif message_type == "release_lock":
                    hub.release_lock()
                    await peer.send({"type": "lock_released", "by": "controller"})
                    if hub.device:
                        await hub.device.send({"type": "lock_released", "by": "controller"})
                elif message_type == "apply_preset":
                    name = message.get("name")
                    preset = get_config().get("presets", {}).get(name)
                    if preset and not str(name).startswith("_"):
                        state = {
                            **{key: 0 for key in ("suction", "vibration", "electric", "insertion")},
                            **preset,
                            **{f"{key}_mode": 1 for key in ("suction", "vibration", "electric", "insertion")},
                        }
                        result = await dispatch_state(hub, state, source="preset:" + str(name))
                        await peer.send({"type": "command_ack", "ok": True, **result})
                    else:
                        await peer.send({"type": "command_ack", "ok": False, "detail": "预设不存在"})
            else:
                if message_type == "ble_status":
                    hub.device_state["ble_connected"] = bool(message.get("ble_connected"))
                    hub.device_state["ble_connecting"] = bool(message.get("ble_connecting"))
                    if message.get("error"):
                        hub.device_state["last_command_result"] = message["error"]
                    if hub.controller:
                        await hub.controller.send(
                            {
                                "type": "ble_status",
                                "ble_connected": hub.device_state["ble_connected"],
                                "ble_connecting": hub.device_state["ble_connecting"],
                                "error": message.get("error"),
                            }
                        )
                elif message_type == "command_result":
                    hub.device_state["last_command_result"] = message.get("result")
                    if hub.controller:
                        await hub.controller.send({"type": "command_result", **message})
                elif message_type == "emergency_stop":
                    for key in ("suction", "vibration", "electric", "insertion"):
                        hub.device_state[key] = 0
                    hub.release_lock()
                    if hub.controller:
                        await hub.controller.send({"type": "emergency_stop", "by": "device"})
                elif message_type == "defer":
                    hub.release_lock()
                    if hub.controller:
                        await hub.controller.send({"type": "lock_released", "by": "device"})
    except WebSocketDisconnect:
        if not peer.disconnect_reason:
            peer.disconnect_reason = "client_disconnected"
    except Exception as exc:
        if not peer.disconnect_reason:
            peer.disconnect_reason = "server_error"
        log.exception("%s/%s websocket error: %s", device_id, role, exc)
    finally:
        peer.alive = False
        if getattr(hub, role) is peer:
            setattr(hub, role, None)
        if not peer.replaced:
            if hub.lock_holder == role:
                hub.release_lock()
            await hub.notify_peer(role, {"type": "peer_disconnected", "role": role})
        if role == "controller":
            duration_seconds = max(0, int(now() - peer.connected_at))
            audit(
                user_id or None,
                "controller_disconnected",
                device_id,
                f"duration_seconds={duration_seconds};reason={peer.disconnect_reason or 'disconnected'}",
            )
        log.info("%s/%s disconnected", device_id, role)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    role = ws.query_params.get("role", "controller")
    if role not in ("controller", "device"):
        await ws.accept()
        await ws.close(code=1008, reason="bad role")
        return
    await websocket_loop(ws, role)


WEB_DIR = os.path.join(os.path.dirname(__file__), "..", "web")
if os.path.isdir(WEB_DIR):
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("FUNF_HOST", "127.0.0.1"),
        port=int(os.environ.get("FUNF_PORT", "3870")),
        log_level="info",
    )
