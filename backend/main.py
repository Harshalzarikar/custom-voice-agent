import os
import re
import uuid
import json
import time
import logging
import threading
from typing import List, Optional
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from livekit import api
import pypdf

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

app = FastAPI(title="Vocal-Voice API", version="2.1.0")

origins = allowed_origins()
if IS_PRODUCTION and not origins:
    logger.warning("FRONTEND_URL is empty. Browser calls will be blocked until it is set.")
elif "*" in origins and IS_PRODUCTION:
    logger.warning("FRONTEND_URL=* is open CORS. Set the real frontend origin in production.")

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins or ["http://localhost:5173"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

_db_lock = threading.Lock()
_rate_lock = threading.Lock()
_rate_hits: dict[str, list[float]] = {}

ROOM_RE = re.compile(
    r"^agent_[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}_[a-z0-9]{3,16}$"
)


def load_db():
    if not DB_FILE.exists():
        return {}
    try:
        with DB_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Agent store is unreadable: %s", exc)
        raise HTTPException(status_code=500, detail="Agent store is unavailable") from exc


def save_db(db):
    tmp = DB_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(db, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, DB_FILE)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_limit(request: Request, bucket: str, limit: int, window: float) -> None:
    key = f"{bucket}:{client_ip(request)}"
    now = time.monotonic()
    with _rate_lock:
        hits = [stamp for stamp in _rate_hits.get(key, []) if now - stamp < window]
        if len(hits) >= limit:
            raise HTTPException(status_code=429, detail="Too many requests. Try again in a minute.")
        hits.append(now)
        if len(_rate_hits) > 5000:
            _rate_hits.clear()
        _rate_hits[key] = hits


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


@app.post("/api/agents")
async def create_agent(
    request: Request,
    name: str = Form(...),
    objective: str = Form(...),
    system_prompt: str = Form(...),
    agent_id: Optional[str] = Form(None),
    reference_file: Optional[UploadFile] = File(None)
):
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

    with _db_lock:
        agents_db = load_db()
        new_id = agent_id or str(uuid.uuid4())
        agent = {
            "id": new_id,
            "name": name,
            "objective": objective,
            "system_prompt": system_prompt,
            "reference_text": ref_text or agents_db.get(new_id, {}).get("reference_text", ""),
        }
        agents_db[new_id] = agent
        save_db(agents_db)
    logger.info("[API] Agent saved: id=%s", new_id)
    return {"success": True, "agent": agent}

@app.get("/api/agents")
async def list_agents():
    with _db_lock:
        agents = list(load_db().values())
    logger.info("[API] GET /api/agents — Returning %s agents", len(agents))
    return agents

class TokenRequest(BaseModel):
    room_name: str = Field(min_length=20, max_length=120)
    participant_name: str = Field(min_length=1, max_length=64)

@app.post("/api/token")
async def get_token(req: TokenRequest, request: Request):
    rate_limit(request, "token", limit=20, window=60)
    if not ROOM_RE.match(req.room_name):
        raise HTTPException(status_code=400, detail="Invalid room name")
    participant = req.participant_name.strip()
    if not participant or any(ord(ch) < 32 for ch in participant):
        raise HTTPException(status_code=400, detail="Invalid participant name")

    livekit_url = os.getenv("LIVEKIT_URL")
    livekit_key = os.getenv("LIVEKIT_API_KEY")
    livekit_secret = os.getenv("LIVEKIT_API_SECRET")
    if not (livekit_url and livekit_key and livekit_secret):
        logger.error("[API] Token request rejected: LiveKit credentials are missing")
        raise HTTPException(status_code=503, detail="Voice service is not configured")

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
        raise
    except Exception as exc:
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
