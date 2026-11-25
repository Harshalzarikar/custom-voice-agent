"""Hospital accounts. A token is required to see that hospital's agents and calendar."""

import hashlib
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

import jwt

import database
from runtime_config import session_secret

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    salt, _, _digest = stored.partition("$")
    if not salt or not _digest:
        return False
    return hash_password(password, salt) == stored


def issue_token(account_id: str, email: str) -> str:
    payload = {
        "sub": account_id,
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(days=14),
    }
    return jwt.encode(payload, session_secret(), algorithm="HS256")


def read_token(header: str | None) -> dict | None:
    if not header or not header.lower().startswith("bearer "):
        return None
    raw = header.split(" ", 1)[1].strip()
    try:
        data = jwt.decode(raw, session_secret(), algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    account = database.get_account(data.get("sub", ""))
    return account


def register(email: str, password: str, hospital_name: str) -> dict:
    email = email.strip().lower()
    hospital_name = " ".join(hospital_name.strip().split())
    if not EMAIL_RE.match(email):
        raise ValueError("Enter a valid email")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")
    if len(hospital_name) < 2:
        raise ValueError("Hospital name is required")
    if database.get_account_by_email(email):
        raise ValueError("An account with that email already exists")
    account_id = str(uuid.uuid4())
    database.create_account(account_id, email, hash_password(password), hospital_name)
    account = database.get_account(account_id)
    database.audit("register", account_id, "", True)
    return {"token": issue_token(account_id, email), "account": account}


def login(email: str, password: str) -> dict:
    email = email.strip().lower()
    row = database.get_account_by_email(email)
    if not row or not verify_password(password, row["password_hash"]):
        database.audit("login", "", "", False)
        raise ValueError("Email or password is wrong")
    account = {"id": row["id"], "email": row["email"], "hospital_name": row["hospital_name"]}
    database.audit("login", row["id"], "", True)
    return {"token": issue_token(row["id"], row["email"]), "account": account}


def change_password(account_id: str, current_password: str, new_password: str) -> None:
    with database.session() as conn:
        row = database._one(conn, "SELECT * FROM accounts WHERE id = ?", (account_id,))
    if not row or not verify_password(current_password, row["password_hash"]):
        database.audit("password_change", account_id, "", False)
        raise ValueError("Current password is wrong")
    if len(new_password) < 8:
        raise ValueError("Password must be at least 8 characters")
    with database.session() as conn:
        database._run(
            conn,
            "UPDATE accounts SET password_hash = ? WHERE id = ?",
            (hash_password(new_password), account_id),
        )
    database.audit("password_change", account_id, "", True)
