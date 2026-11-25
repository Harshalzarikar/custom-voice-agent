"""Shared paths and environment loading for the API and the voice worker."""

import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent
ROOT_DIR = BACKEND_DIR.parent

# Root .env is what the README tells people to create. A backend/.env wins if both exist.
load_dotenv(ROOT_DIR / ".env")
load_dotenv(BACKEND_DIR / ".env", override=True)

ENV = os.getenv("ENV", "development").strip().lower()
IS_PRODUCTION = ENV == "production"

DATA_DIR = Path(os.getenv("DATA_DIR", str(BACKEND_DIR))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_FILE = DATA_DIR / "agents.json"
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
HEARTBEAT_FILE = DATA_DIR / "worker.heartbeat"

def session_secret() -> str:
    configured = os.getenv("SESSION_SECRET", "").strip()
    if configured:
        return configured
    if IS_PRODUCTION:
        raise RuntimeError("SESSION_SECRET is required when ENV=production")
    path = DATA_DIR / ".session_secret"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    secret = os.urandom(32).hex()
    path.write_text(secret, encoding="utf-8")
    return secret

ENABLE_DEMO_TRIGGERS = os.getenv(
    "ENABLE_DEMO_TRIGGERS",
    "false" if IS_PRODUCTION else "true",
).strip().lower() in {"1", "true", "yes"}

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_REFERENCE_CHARS = 12_000
MAX_PDF_PAGES = 20


def normalize_origin(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value or value == "*":
        return value
    if not value.startswith(("http://", "https://")):
        return f"https://{value}"
    return value


def allowed_origins() -> list[str]:
    raw = os.getenv("FRONTEND_URL", "" if IS_PRODUCTION else "*").strip()
    if not raw:
        return []
    if raw == "*":
        return ["*"]
    return [normalize_origin(part) for part in raw.split(",") if part.strip()]
