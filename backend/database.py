"""Agents, accounts, and appointments.

SQLite (vocal.db) is the local database. Set DATABASE_URL to a Postgres URL
and both the API and the voice worker use that database instead, so a redeploy
does not erase visits.
"""

from contextlib import contextmanager
import base64
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone

from runtime_config import DATA_DIR, DB_FILE, IS_PRODUCTION

DB_PATH = DATA_DIR / "vocal.db"
APPOINTMENTS_FILE = DATA_DIR / "appointments.json"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
POSTGRES = DATABASE_URL.startswith("postgres")


def connect():
    if POSTGRES:
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def session():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def _sql(statement: str) -> str:
    if POSTGRES and statement.strip().upper() == "BEGIN IMMEDIATE":
        return "BEGIN"
    if POSTGRES:
        return statement.replace("?", "%s")
    return statement


def _run(conn, statement: str, params=()):
    return conn.execute(_sql(statement), params)


def _all(conn, statement: str, params=()):
    cursor = _run(conn, statement, params)
    fetched = cursor.fetchall()
    if POSTGRES:
        return list(fetched)
    return [dict(row) for row in fetched]


def _one(conn, statement: str, params=()):
    found = _all(conn, statement, params)
    return found[0] if found else None


def _columns(conn, table: str) -> set[str]:
    if POSTGRES:
        found = _all(
            conn,
            "SELECT column_name AS name FROM information_schema.columns WHERE table_name = ?",
            (table,),
        )
        return {row["name"] for row in found}
    return {row["name"] for row in _all(conn, f"PRAGMA table_info({table})")}


def init() -> None:
    with session() as conn:
        _run(
            conn,
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                hospital_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
        )
        _run(
            conn,
            """
            CREATE TABLE IF NOT EXISTS agents (
                id TEXT PRIMARY KEY,
                owner_id TEXT,
                name TEXT NOT NULL,
                objective TEXT NOT NULL,
                system_prompt TEXT NOT NULL,
                reference_text TEXT NOT NULL DEFAULT '',
                capabilities TEXT NOT NULL DEFAULT '[]'
            )
            """,
        )
        _run(
            conn,
            """
            CREATE TABLE IF NOT EXISTS appointments (
                id TEXT PRIMARY KEY,
                owner_id TEXT,
                hospital TEXT NOT NULL,
                patient_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                department TEXT NOT NULL,
                date TEXT NOT NULL,
                time TEXT NOT NULL
            )
            """,
        )
        if "owner_id" not in _columns(conn, "agents"):
            _run(conn, "ALTER TABLE agents ADD COLUMN owner_id TEXT")
        if "owner_id" not in _columns(conn, "appointments"):
            _run(conn, "ALTER TABLE appointments ADD COLUMN owner_id TEXT")
        if not POSTGRES:
            defined = _one(conn, "SELECT sql FROM sqlite_master WHERE name = 'appointments'")
            table_sql = (defined or {}).get("sql") or ""
            if "UNIQUE (department, date, time)" in table_sql:
                _run(conn, "BEGIN IMMEDIATE")
                _run(
                    conn,
                    """
                    CREATE TABLE appointments_owned (
                        id TEXT PRIMARY KEY,
                        owner_id TEXT,
                        hospital TEXT NOT NULL,
                        patient_name TEXT NOT NULL,
                        phone TEXT NOT NULL,
                        department TEXT NOT NULL,
                        date TEXT NOT NULL,
                        time TEXT NOT NULL
                    )
                    """,
                )
                _run(
                    conn,
                    """
                    INSERT INTO appointments_owned
                        (id, owner_id, hospital, patient_name, phone, department, date, time)
                    SELECT id, owner_id, hospital, patient_name, phone, department, date, time
                    FROM appointments
                    """,
                )
                _run(conn, "DROP TABLE appointments")
                _run(conn, "ALTER TABLE appointments_owned RENAME TO appointments")
                _run(conn, "COMMIT")
        _run(
            conn,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS appointments_owner_slot
            ON appointments (owner_id, department, date, time)
            """,
        )
        _run(
            conn,
            """
            CREATE TABLE IF NOT EXISTS rate_hits (
                bucket TEXT NOT NULL,
                stamp REAL NOT NULL
            )
            """,
        )
        _run(
            conn,
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                at TEXT NOT NULL,
                action TEXT NOT NULL,
                account_id TEXT NOT NULL DEFAULT '',
                subject TEXT NOT NULL DEFAULT '',
                ok INTEGER NOT NULL
            )
            """,
        )
        _run(
            conn,
            """
            CREATE TABLE IF NOT EXISTS active_calls (
                room TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL
            )
            """,
        )
        _import_json_once(conn)
        _encrypt_plain_phones(conn)


def _import_json_once(conn) -> None:
    if _one(conn, "SELECT COUNT(*) AS n FROM agents")["n"]:
        agents_ready = True
    else:
        agents_ready = False
    if not agents_ready and DB_FILE.exists():
        try:
            raw = json.loads(DB_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        if isinstance(raw, dict):
            _run(conn, "BEGIN IMMEDIATE")
            for agent in raw.values():
                if isinstance(agent, dict) and agent.get("id"):
                    _insert_agent(conn, agent)
            _run(conn, "COMMIT")
    if _one(conn, "SELECT COUNT(*) AS n FROM appointments")["n"]:
        return
    if not APPOINTMENTS_FILE.exists():
        return
    try:
        rows = json.loads(APPOINTMENTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        rows = []
    if isinstance(rows, list) and rows:
        _run(conn, "BEGIN IMMEDIATE")
        for row in rows:
            if isinstance(row, dict) and row.get("id"):
                _insert_visit(conn, row)
        _run(conn, "COMMIT")


def _insert_agent(conn, agent: dict) -> None:
    caps = agent.get("capabilities") or []
    _run(
        conn,
        """
        INSERT INTO agents (id, owner_id, name, objective, system_prompt, reference_text, capabilities)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            agent["id"],
            agent.get("owner_id"),
            agent.get("name") or "Agent",
            agent.get("objective") or "",
            agent.get("system_prompt") or "",
            agent.get("reference_text") or "",
            json.dumps(caps if isinstance(caps, list) else []),
        ),
    )


def _fernet():
    from cryptography.fernet import Fernet

    from runtime_config import session_secret

    key = base64.urlsafe_b64encode(hashlib.sha256(session_secret().encode("utf-8")).digest())
    return Fernet(key)


def _stored_phone(phone: str) -> str:
    raw = phone or ""
    if raw.startswith("gAAAA"):
        return raw
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return ""
    return _fernet().encrypt(digits.encode("utf-8")).decode("ascii")


def _plain_phone(stored: str) -> str:
    if not stored:
        return ""
    if stored.isdigit():
        return stored
    try:
        return _fernet().decrypt(stored.encode("ascii")).decode("utf-8")
    except Exception:
        return ""


def _encrypt_plain_phones(conn) -> None:
    for row in _all(conn, "SELECT id, phone FROM appointments"):
        phone = row.get("phone") or ""
        if phone.isdigit():
            _run(conn, "UPDATE appointments SET phone = ? WHERE id = ?", (_stored_phone(phone), row["id"]))


def _insert_visit(conn, row: dict) -> None:
    _run(
        conn,
        """
        INSERT INTO appointments
            (id, owner_id, hospital, patient_name, phone, department, date, time)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["id"],
            row.get("owner_id"),
            row.get("hospital") or "",
            row.get("patient_name") or "",
            _stored_phone(row.get("phone") or ""),
            row.get("department") or "",
            row.get("date") or "",
            row.get("time") or "",
        ),
    )


def _agent_from_row(row: dict) -> dict:
    try:
        caps = json.loads(row.get("capabilities") or "[]")
    except json.JSONDecodeError:
        caps = []
    agent = {
        "id": row["id"],
        "owner_id": row.get("owner_id"),
        "name": row["name"],
        "objective": row["objective"],
        "system_prompt": row["system_prompt"],
        "reference_text": row.get("reference_text") or "",
    }
    if isinstance(caps, list) and caps:
        agent["capabilities"] = caps
    return agent


def load_agents(owner_id: str | None = None) -> dict:
    init()
    with session() as conn:
        if owner_id:
            found = _all(conn, "SELECT * FROM agents WHERE owner_id = ?", (owner_id,))
        else:
            found = _all(conn, "SELECT * FROM agents")
    return {row["id"]: _agent_from_row(row) for row in found}


def get_agent(agent_id: str):
    return load_agents().get(agent_id)


def upsert_agent(agent: dict) -> None:
    init()
    with session() as conn:
        _run(conn, "DELETE FROM agents WHERE id = ?", (agent["id"],))
        _insert_agent(conn, agent)


def save_agents(db: dict) -> None:
    for agent in db.values():
        if isinstance(agent, dict) and agent.get("id"):
            upsert_agent(agent)


def account_count() -> int:
    init()
    with session() as conn:
        return int(_one(conn, "SELECT COUNT(*) AS n FROM accounts")["n"])


def create_account(account_id: str, email: str, password_hash: str, hospital_name: str) -> None:
    init()
    with session() as conn:
        _run(conn, "BEGIN IMMEDIATE")
        try:
            _run(
                conn,
                """
                INSERT INTO accounts (id, email, password_hash, hospital_name, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (account_id, email, password_hash, hospital_name, datetime.now(timezone.utc).isoformat()),
            )
            if not IS_PRODUCTION and _one(conn, "SELECT COUNT(*) AS n FROM accounts")["n"] == 1:
                _run(conn, "UPDATE agents SET owner_id = ? WHERE owner_id IS NULL", (account_id,))
                _run(conn, "UPDATE appointments SET owner_id = ? WHERE owner_id IS NULL", (account_id,))
            _run(conn, "COMMIT")
        except Exception:
            _run(conn, "ROLLBACK")
            raise


def get_account_by_email(email: str):
    init()
    with session() as conn:
        return _one(conn, "SELECT * FROM accounts WHERE email = ?", (email,))


def get_account(account_id: str):
    init()
    with session() as conn:
        return _one(conn, "SELECT id, email, hospital_name FROM accounts WHERE id = ?", (account_id,))


def _open_visit(row: dict) -> dict:
    item = dict(row)
    item["phone"] = _plain_phone(item.get("phone") or "")
    return item


def list_visits(owner_id: str | None = None) -> list:
    init()
    with session() as conn:
        if owner_id:
            rows = _all(
                conn,
                """
                SELECT id, owner_id, hospital, patient_name, phone, department, date, time
                FROM appointments WHERE owner_id = ?
                """,
                (owner_id,),
            )
        else:
            rows = _all(
                conn,
                "SELECT id, owner_id, hospital, patient_name, phone, department, date, time FROM appointments",
            )
    return [_open_visit(row) for row in rows]


def allow_request(key: str, limit: int, window: float) -> bool:
    """Shared request counter. Survives a process restart."""
    now = time.time()
    init()
    with session() as conn:
        _run(conn, "BEGIN IMMEDIATE")
        try:
            _run(conn, "DELETE FROM rate_hits WHERE stamp < ?", (now - window,))
            count = int(_one(conn, "SELECT COUNT(*) AS n FROM rate_hits WHERE bucket = ?", (key,))["n"])
            if count >= limit:
                _run(conn, "COMMIT")
                return False
            _run(conn, "INSERT INTO rate_hits (bucket, stamp) VALUES (?, ?)", (key, now))
            _run(conn, "COMMIT")
            return True
        except Exception:
            _run(conn, "ROLLBACK")
            raise


def audit(action: str, account_id: str = "", subject: str = "", ok: bool = True) -> None:
    init()
    with session() as conn:
        _run(
            conn,
            """
            INSERT INTO audit_log (at, action, account_id, subject, ok)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                action[:40],
                (account_id or "")[:80],
                (subject or "")[:80],
                1 if ok else 0,
            ),
        )


def max_active_calls() -> int:
    try:
        return max(1, int(os.getenv("MAX_ACTIVE_CALLS", "1")))
    except ValueError:
        return 1


def reserve_call(owner_id: str, room: str, ttl: float = 90) -> bool:
    """Hold a live call. A crashed worker releases the hold after ttl seconds."""
    now = time.time()
    init()
    with session() as conn:
        _run(conn, "BEGIN IMMEDIATE")
        try:
            _run(conn, "DELETE FROM active_calls WHERE updated_at < ?", (now - ttl,))
            rooms = {row["room"] for row in _all(conn, "SELECT room FROM active_calls")}
            if room not in rooms and len(rooms) >= max_active_calls():
                _run(conn, "COMMIT")
                return False
            _run(
                conn,
                """
                INSERT INTO active_calls (room, owner_id, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(room) DO UPDATE SET owner_id = excluded.owner_id, updated_at = excluded.updated_at
                """,
                (room, owner_id or "", now),
            )
            _run(conn, "COMMIT")
            return True
        except Exception:
            _run(conn, "ROLLBACK")
            raise


def touch_call(owner_id: str, room: str) -> None:
    reserve_call(owner_id, room)


def release_call(room: str) -> None:
    init()
    with session() as conn:
        _run(conn, "DELETE FROM active_calls WHERE room = ?", (room,))


def insert_visit(record: dict) -> None:
    init()
    with session() as conn:
        _run(conn, "BEGIN IMMEDIATE")
        try:
            _insert_visit(conn, record)
            _run(conn, "COMMIT")
        except Exception as exc:
            _run(conn, "ROLLBACK")
            if "unique" in str(exc).lower() or isinstance(exc, sqlite3.IntegrityError):
                raise sqlite3.IntegrityError(str(exc)) from exc
            raise


def delete_visit(visit_id: str, owner_id: str) -> None:
    init()
    with session() as conn:
        _run(conn, "DELETE FROM appointments WHERE id = ? AND owner_id = ?", (visit_id, owner_id))


init()
