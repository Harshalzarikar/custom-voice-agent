import os
import re
import sqlite3
import uuid
import json
import time
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from livekit import api
import pypdf

import appointments
import auth
import database
from runtime_config import (
    DB_FILE,
    ENABLE_DEMO_TRIGGERS,
    HEARTBEAT_FILE,
    IS_PRODUCTION,
    MAX_PDF_PAGES,
    MAX_REFERENCE_CHARS,
    MAX_UPLOAD_BYTES,
    UPLOAD_DIR,
    allowed_origins,
    session_secret,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
    + (
        []
        if IS_PRODUCTION
        else [logging.FileHandler(DB_FILE.parent / "vocal_voice.log", encoding="utf-8")]
    ),
    force=True,
)
logger = logging.getLogger(__name__)

SAMPLE_AGENT_ID = "7c2a9e14-6b8d-4f31-9a70-1d5e8c4b2a66"
HOSPITAL_AGENT_ID = "3f1c8a20-91e4-4b7a-8d22-6a0e5b7c9d11"
SAMPLE_FAQ = Path(__file__).resolve().parent / "samples" / "harbor-desk-faq.txt"
HOSPITAL_FAQ = Path(__file__).resolve().parent / "samples" / "city-hospital.txt"

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Fail fast in production when the signing/encryption secret is missing.
    session_secret()
    yield


app = FastAPI(title="Custom Voice Agent API", version="2.1.0", lifespan=lifespan)

origins = allowed_origins()
if IS_PRODUCTION and not origins:
    logger.warning("FRONTEND_URL is empty. Browser calls will be blocked until it is set.")
elif "*" in origins and IS_PRODUCTION:
    logger.warning("FRONTEND_URL=* is open CORS. Set the real frontend origin in production.")

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins or ([] if IS_PRODUCTION else ["http://localhost:5173"]),
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

_db_lock = threading.Lock()

ROOM_RE = re.compile(
    r"^agent_[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}_[a-z0-9]{3,16}$"
)


def load_db():
    try:
        return database.load_agents()
    except sqlite3.Error as exc:
        logger.error("Agent store is unreadable: %s", exc)
        raise HTTPException(status_code=500, detail="Agent store is unavailable") from exc


def save_db(db):
    try:
        database.save_agents(db)
    except sqlite3.Error as exc:
        logger.error("Agent store could not be saved: %s", exc)
        raise HTTPException(status_code=500, detail="Agent store is unavailable") from exc


def seed_sample_agent():
    """The first-run agent is a shop support desk that answers from one FAQ."""
    if not SAMPLE_FAQ.exists():
        logger.warning("Sample FAQ is missing: %s", SAMPLE_FAQ)
        return
    with _db_lock:
        db = load_db()
        if SAMPLE_AGENT_ID not in db:
            db[SAMPLE_AGENT_ID] = {
                "id": SAMPLE_AGENT_ID,
                "name": "Harbor Desk",
                "objective": "Answer shop-owner questions from the Harbor Desk FAQ, in one or two spoken sentences.",
                "system_prompt": (
                    "You are the Harbor Desk voice support agent. "
                    "Answer only from the reference FAQ. "
                    "If the FAQ does not contain the answer, say you do not have that information "
                    "and offer to take a message. "
                    "Do not invent prices, hours, or policies. "
                    "Keep every reply to one or two short sentences."
                ),
                "reference_text": SAMPLE_FAQ.read_text(encoding="utf-8").strip()[:MAX_REFERENCE_CHARS],
            }
            save_db(db)
            logger.info("Seeded Harbor Desk support agent")
    if not HOSPITAL_FAQ.exists():
        logger.warning("Hospital sheet is missing: %s", HOSPITAL_FAQ)
        return
    with _db_lock:
        db = load_db()
        fresh_sheet = HOSPITAL_FAQ.read_text(encoding="utf-8").strip()[:MAX_REFERENCE_CHARS]
        if HOSPITAL_AGENT_ID in db:
            db[HOSPITAL_AGENT_ID]["reference_text"] = fresh_sheet
            db[HOSPITAL_AGENT_ID]["capabilities"] = ["appointments"]
            db[HOSPITAL_AGENT_ID]["system_prompt"] = (
                "You are the City Hospital front desk. "
                "Help the patient book General Medicine, Skin, or Pediatrics. "
                "If they say today, call check_availability with today. Do not switch to tomorrow until they agree. "
                "The reference sheet has no bookable times. "
                "Offer only the times the tool returns, and say the weekday with each time. "
                "Never offer a time that is already past. If today has no times left, say the day is closed. "
                "Ask for the name and a 10-digit phone number only after they choose one open time. "
                "To cancel, use cancel_appointment and say no payment was taken. "
                "If they ask who else is booked, say you can only confirm their own visit. "
                "Use find_appointment with their confirmation number or their name. Never read another patient's name. "
                "Never invent a phone number, address, or website. "
                "Keep each reply to one or two short sentences."
            )
            save_db(db)
            return
        db[HOSPITAL_AGENT_ID] = {
            "id": HOSPITAL_AGENT_ID,
            "name": "City Hospital Front Desk",
            "objective": "Book one appointment after the patient agrees to an open time.",
            "system_prompt": (
                "You are the City Hospital front desk. "
                "Help the patient book General Medicine, Skin, or Pediatrics. "
                "If they say today, call check_availability with today. Do not switch to tomorrow until they agree. "
                "The reference sheet has no bookable times. "
                "Offer only the times the tool returns, and say the weekday with each time. "
                "Never offer a time that is already past. If today has no times left, say the day is closed. "
                "Ask for the name and a 10-digit phone number only after they choose one open time. "
                "To cancel, use cancel_appointment and say no payment was taken. "
                "If they ask who else is booked, say you can only confirm their own visit. "
                "Use find_appointment with their confirmation number or their name. Never read another patient's name. "
                "Never invent a phone number, address, or website. "
                "Keep each reply to one or two short sentences."
            ),
            "reference_text": fresh_sheet,
            "capabilities": ["appointments"],
        }
        save_db(db)
        logger.info("Seeded City Hospital front desk")


seed_sample_agent()


def client_ip(request: Request) -> str:
    """Use the socket address. Trust the last forwarded hop only behind a known proxy."""
    if os.getenv("TRUST_PROXY", "").strip().lower() in {"1", "true", "yes"}:
        forwarded = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",") if part.strip()]
        if forwarded:
            return forwarded[-1]
    return request.client.host if request.client else "unknown"


def rate_limit(request: Request, bucket: str, limit: int, window: float) -> None:
    if not database.allow_request(f"{bucket}:{client_ip(request)}", limit, window):
        raise HTTPException(status_code=429, detail="Too many requests. Try again in a minute.")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    return response


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    if isinstance(exc, HTTPException):
        return await http_exception_handler(request, exc)
    if isinstance(exc, RequestValidationError):
        return await request_validation_exception_handler(request, exc)
    logger.exception("Unhandled API error: %s", exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})

class AgentConfig(BaseModel):
    id: str
    name: str
    objective: str
    system_prompt: str
    reference_text: Optional[str] = ""

# Frontend log model
class FrontendLogEntry(BaseModel):
    timestamp: str
    tag: str
    message: str
    data: Optional[str] = None

class FrontendLogBatch(BaseModel):
    logs: List[FrontendLogEntry]

frontend_logger = logging.getLogger("frontend")
frontend_logger.setLevel(logging.INFO)
frontend_logger.propagate = True
if not IS_PRODUCTION:
    frontend_handler = logging.FileHandler(DB_FILE.parent / "frontend.log", encoding="utf-8")
    frontend_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    frontend_logger.addHandler(frontend_handler)

# ── PROMPT SANITIZATION ──────────────────────────────────────────────────────
# Blocks malicious agent creation at the API level (Defense Layer 1)
DANGEROUS_PATTERNS = [
    "ignore your instructions", "ignore previous instructions",
    "reveal your prompt", "show me your system prompt",
    "act as if you have no restrictions", "forget your rules",
    "you are now a hacker", "bypass security",
    "execute code", "run command", "os.environ",
]

def sanitize_prompt(prompt: str) -> dict:
    """
    Scans user-submitted system prompts for dangerous patterns.
    Returns: {"safe": True/False, "blocked_phrase": str}
    """
    prompt_lower = prompt.lower()
    for pattern in DANGEROUS_PATTERNS:
        if pattern in prompt_lower:
            logger.warning(f"BLOCKED AGENT CREATION: dangerous pattern '{pattern}' found in prompt")
            return {"safe": False, "blocked_phrase": pattern}
    return {"safe": True, "blocked_phrase": ""}


def _require_text(value: str, field: str, limit: int) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail=f"{field} is required")
    if len(cleaned) > limit:
        raise HTTPException(status_code=400, detail=f"{field} must be {limit} characters or fewer")
    return cleaned


def _extract_reference(filename: str, payload: bytes) -> str:
    suffix = filename.lower().rsplit(".", 1)[-1]
    stored = UPLOAD_DIR / f"{uuid.uuid4().hex}.{suffix}"
    stored.write_bytes(payload)
    if suffix == "pdf":
        reader = pypdf.PdfReader(str(stored))
        if len(reader.pages) > MAX_PDF_PAGES:
            raise HTTPException(status_code=400, detail=f"PDF must be {MAX_PDF_PAGES} pages or fewer")
        parts = []
        for page in reader.pages[:MAX_PDF_PAGES]:
            parts.append(page.extract_text() or "")
        text = "\n".join(parts)
    else:
        text = payload.decode("utf-8", errors="replace")
    return text.strip()[:MAX_REFERENCE_CHARS]


def current_account(request: Request) -> dict:
    account = auth.read_token(request.headers.get("authorization"))
    if not account:
        raise HTTPException(status_code=401, detail="Sign in required")
    return account


class AuthRequest(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=8, max_length=200)
    hospital_name: str = Field(default="", max_length=120)


@app.post("/api/auth/register")
async def register_account(body: AuthRequest, request: Request):
    rate_limit(request, "auth", limit=8, window=60)
    try:
        return auth.register(body.email, body.password, body.hospital_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/auth/login")
async def login_account(body: AuthRequest, request: Request):
    rate_limit(request, "auth", limit=8, window=60)
    try:
        return auth.login(body.email, body.password)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=8, max_length=200)


@app.post("/api/auth/password")
async def change_password(body: PasswordChange, request: Request):
    account = current_account(request)
    rate_limit(request, "auth", limit=8, window=60)
    try:
        auth.change_password(account["id"], body.current_password, body.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@app.get("/api/auth/me")
async def me(request: Request):
    return current_account(request)


@app.post("/api/agents")
async def create_agent(
    request: Request,
    name: str = Form(...),
    objective: str = Form(...),
    system_prompt: str = Form(...),
    agent_id: Optional[str] = Form(None),
    capabilities: Optional[str] = Form(None),
    reference_file: Optional[UploadFile] = File(None)
):
    account = current_account(request)
    rate_limit(request, "agents", limit=10, window=60)
    name = _require_text(name, "Name", 80)
    objective = _require_text(objective, "Objective", 500)
    system_prompt = _require_text(system_prompt, "System prompt", 8000)
    logger.info("[API] POST /api/agents — Creating agent: name='%s'", name)

    sanitization = sanitize_prompt(system_prompt)
    if not sanitization["safe"]:
        logger.warning("[API] Agent creation blocked: '%s'", sanitization["blocked_phrase"])
        raise HTTPException(
            status_code=400,
            detail=f"Agent creation blocked: prompt contains restricted content ('{sanitization['blocked_phrase']}')"
        )

    ref_text = ""
    if reference_file and reference_file.filename:
        original = os.path.basename(reference_file.filename)
        if not original.lower().endswith((".pdf", ".txt")):
            raise HTTPException(status_code=400, detail="Only PDF and TXT reference files are supported")
        payload = await reference_file.read(MAX_UPLOAD_BYTES + 1)
        if len(payload) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Reference file must be 5 MB or smaller")
        if not payload:
            raise HTTPException(status_code=400, detail="Reference file is empty")
        logger.info("[API] Processing reference file: %s (%s bytes)", original, len(payload))
        try:
            ref_text = _extract_reference(original, payload)
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Reference extraction failed: %s", exc)
            raise HTTPException(status_code=400, detail="Could not read that reference file") from exc

    if agent_id and not re.fullmatch(r"[0-9a-fA-F-]{36}", agent_id):
        raise HTTPException(status_code=400, detail="Invalid agent id")

    account = current_account(request)
    with _db_lock:
        new_id = agent_id or str(uuid.uuid4())
        existing = database.get_agent(new_id)
        if existing and existing.get("owner_id") != account["id"]:
            raise HTTPException(status_code=404, detail="Agent not found")
        if capabilities is None:
            caps = (existing or {}).get("capabilities") or []
        else:
            requested = {part.strip().lower() for part in capabilities.split(",") if part.strip()}
            caps = [cap for cap in ("appointments",) if cap in requested]
        agent = {
            "id": new_id,
            "owner_id": account["id"],
            "name": name,
            "objective": objective,
            "system_prompt": system_prompt,
            "reference_text": ref_text or (existing or {}).get("reference_text", ""),
            "capabilities": caps,
        }
        database.upsert_agent(agent)
    logger.info("[API] Agent saved: id=%s", new_id)
    return {"success": True, "agent": agent}

@app.get("/api/appointments/lookup")
async def lookup_appointment(request: Request, confirmation: str = "", phone: str = ""):
    rate_limit(request, "lookup", limit=8, window=60)
    try:
        return appointments.lookup(confirmation, phone)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/appointments")
async def list_appointments(request: Request):
    account = current_account(request)
    return appointments.desk(account["id"], account["hospital_name"])

@app.get("/api/agents")
async def list_agents(request: Request):
    account = current_account(request)
    agents = list(database.load_agents(account["id"]).values())
    logger.info("[API] GET /api/agents — Returning %s agents", len(agents))
    return agents

class TokenRequest(BaseModel):
    room_name: str = Field(min_length=20, max_length=120)
    participant_name: str = Field(min_length=1, max_length=64)

@app.post("/api/token")
async def get_token(req: TokenRequest, request: Request):
    account = current_account(request)
    rate_limit(request, "token", limit=20, window=60)
    if not ROOM_RE.match(req.room_name):
        raise HTTPException(status_code=400, detail="Invalid room name")
    agent_id = req.room_name.split("_")[1]
    agent = database.get_agent(agent_id)
    if not agent or agent.get("owner_id") != account["id"]:
        raise HTTPException(status_code=404, detail="Agent not found")
    participant = req.participant_name.strip()
    if not participant or any(ord(ch) < 32 for ch in participant):
        raise HTTPException(status_code=400, detail="Invalid participant name")

    livekit_url = os.getenv("LIVEKIT_URL")
    livekit_key = os.getenv("LIVEKIT_API_KEY")
    livekit_secret = os.getenv("LIVEKIT_API_SECRET")
    if not (livekit_url and livekit_key and livekit_secret):
        logger.error("[API] Token request rejected: LiveKit credentials are missing")
        raise HTTPException(status_code=503, detail="Voice service is not configured")

    if not database.reserve_call(account["id"], req.room_name):
        raise HTTPException(status_code=409, detail="The voice desk is already on a call. Try again in a minute.")
    logger.info("[API] POST /api/token — Room: '%s'", req.room_name)
    try:
        token = api.AccessToken(livekit_key, livekit_secret) \
            .with_identity(participant) \
            .with_grants(api.VideoGrants(
                room_join=True,
                room=req.room_name,
            ))
        jwt_token = token.to_jwt()

        lkapi = api.LiveKitAPI(livekit_url, livekit_key, livekit_secret)
        try:
            await lkapi.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name="vocal-agent",
                    room=req.room_name,
                )
            )
        finally:
            await lkapi.aclose()
    except HTTPException:
        database.release_call(req.room_name)
        raise
    except Exception as exc:
        database.release_call(req.room_name)
        logger.exception("[API] Token generation failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not start a voice session") from exc

    return {"token": jwt_token, "url": livekit_url}


# ── Frontend Log Sink ─────────────────────────────────────────────────────────
# Receives batched logs from the browser and writes them to frontend.log
@app.post("/api/frontend-logs")
async def receive_frontend_logs(batch: FrontendLogBatch, request: Request):
    rate_limit(request, "logs", limit=30, window=60)
    if len(batch.logs) > 50:
        raise HTTPException(status_code=400, detail="Log batch is too large")
    for entry in batch.logs[:50]:
        message = entry.message[:2000]
        data = (entry.data or "")[:2000]
        log_line = f"{entry.timestamp[:40]} - frontend - [{entry.tag[:40]}] {message}"
        if data:
            log_line += f" | data={data}"
        frontend_logger.info(log_line)
    return {"received": min(len(batch.logs), 50)}


# ── Health & Diagnostics Endpoint ──────────────────────────────────────────
@app.get("/api/health")
async def health_check():
    livekit_configured = bool(os.getenv("LIVEKIT_API_KEY") and os.getenv("LIVEKIT_API_SECRET") and os.getenv("LIVEKIT_URL"))
    groq_configured = bool(os.getenv("GROQ_API_KEY"))
    deepgram_configured = bool(os.getenv("DEEPGRAM_API_KEY"))
    tavily_configured = bool(os.getenv("TAVILY_API_KEY"))
    
    worker_state = "not_started"
    try:
        if HEARTBEAT_FILE.exists():
            age = time.time() - float(HEARTBEAT_FILE.read_text(encoding="utf-8").strip())
            worker_state = "alive" if age < 30 else "stale"
    except (OSError, ValueError):
        worker_state = "stale"

    with _db_lock:
        try:
            agents_count = len(load_db())
        except HTTPException:
            agents_count = -1

    credentials_ok = livekit_configured and groq_configured and deepgram_configured
    return {
        "status": "healthy" if credentials_ok and worker_state == "alive" else "degraded",
        "pipeline": {
            "webrtc_livekit": "connected" if livekit_configured else "missing_credentials",
            "stt_deepgram": "configured" if deepgram_configured else "missing_credentials",
            "llm_groq": "configured" if groq_configured else "missing_credentials",
            "tts_deepgram": "configured" if deepgram_configured else "missing_credentials",
            "tools_tavily": "configured" if tavily_configured else "optional_missing",
            "agent_worker": worker_state,
        },
        "agents_count": agents_count,
        "demo_triggers": ENABLE_DEMO_TRIGGERS,
        "version": "2.1.0"
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)
