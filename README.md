# Vocal-Voice: Real-Time AI Voice Assistant

This project is a high-performance, real-time voice AI assistant built for ultra-low latency conversational interactions. It utilizes WebRTC for continuous audio streaming, Deepgram for streaming STT/TTS, and Groq for blazing-fast LLM token generation.

## Features Implemented
- **Dynamic Agent Creation:** Go beyond hardcoded prompts! Users can configure new agents in the UI with custom names, objectives, system prompts, and upload PDF/TXT knowledge reference files.
- **WebRTC Streaming:** Full-duplex, low-latency audio via LiveKit, entirely avoiding the overhead of raw WebSocket audio processing.
- **Progressive UI:** Real-time interim STT transcripts and token-by-token LLM streaming.
- **Latency Telemetry:** True timestamp-based measurement of STT, LLM TTFT (Time to First Token), and TTS TTFB (Time to First Byte).
- **Ghost-Audio Prevention (Barge-In):** Immutable generation IDs and immediate cancellation broadcasts ensure that when a user interrupts the AI, stale TTS audio buffers are instantly discarded.
- **Slow-TTS Backpressure:** Custom 2.5s TTS timeout guards against pipeline stalling, gracefully degrading to a text-only UI warning if the TTS provider hangs.
- **Monotonic Sequence Tracking:** Drops duplicate/stale out-of-order WebRTC data packets.
- **Context Sliding Window:** Dynamically truncates the conversational history buffer to the most recent 5-10 turns to prevent LLM context overflow while preserving the immutable system prompt.
- **Robust Error Recovery:** Explicit UI state handling for Microphone permission denials, WebSocket disconnects, and API failures.

## Architecture & Technology
This project implements a highly optimized voice pipeline. Below is the high-level data flow:

```text
 ┌────────────────────┐
 │ Browser (React)    │
 │ Microphone & UI    │
 └─────────┬──────────┘
           │
     WebRTC (LiveKit)
           │
           ▼
 ┌────────────────────┐
 │ Voice Backend      │
 │ (Python/FastAPI)   │
 └─────────┬──────────┘
           │
 ┌─────────┼──────────┐
 ▼         ▼          ▼
 STT      LLM        TTS
 │         │          │
 └─────────┼──────────┘
           │
           ▼
     Audio Stream (Opus)
           │
           ▼
 ┌────────────────────┐
 │ Browser (React)    │
 │ Audio Playback     │
 └────────────────────┘
```

For a deeper dive into the pipeline latency waterfall, technology choices (WebRTC vs WebSockets), and our ghost-audio prevention protocol, please see the detailed [`ARCHITECTURE.md`](./ARCHITECTURE.md) document.

---

## 🚀 How to Run Locally

### 1. Environment Setup
Create a `.env` file in the root directory by copying the provided example:
```bash
cp .env.example .env
```
Fill in the `.env` file with your actual API keys (LiveKit, Groq, Deepgram, Tavily). **Do not hardcode or commit these keys.**

### 2. Start the Frontend
The frontend is built with React/Vite.
```bash
# Install dependencies
yarn install

# Start the Vite development server
yarn dev
```

### 3. Start the Backend API & Agent Worker
The backend requires Python 3.10+. Open a **second terminal**.
```bash
# Navigate to the backend directory
cd backend

# Create and activate a virtual environment
python -m venv venv
# On Windows: .\venv\Scripts\activate
# On Mac/Linux: source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Start the FastAPI server (handles tokens & dispatch)
python main.py
```

### 4. Start the Voice Agent
Open a **third terminal** (ensure your virtual environment is still activated).
```bash
cd backend
python agent_worker.py start
```

### 5. Open the Application
Navigate to `http://localhost:5173` in your browser. 
1. Click **Create Agent**.
2. Give your agent a name and write a **System Prompt / Goal** (e.g., "You are a helpful math tutor").
3. Click **Start Session** and begin speaking!
---

## Testing Logical Challenges (Reviewer Guide)
These phrases are enabled when `ENABLE_DEMO_TRIGGERS` is true (the local default). Production sets it to false. Speak them into the microphone during a session:
- Say **"Simulate delay"**: The backend will intentionally stall the TTS pipeline for 3 seconds. Watch the yellow backpressure warning banner drop down and the system gracefully recover without freezing.
- Say **"Simulate duplicate"**: The backend will intentionally broadcast a duplicated WebRTC packet with an identical sequence number. Check your browser's Developer Console to verify the UI explicitly drops the duplicate packet `(seq X <= X)`.

---

## Production deploy (Render)

`render.yaml` describes two services: `vocal-backend` (API + voice worker) and `vocal-frontend` (static build). The backend blueprint uses Render's Starter plan so the voice worker stays awake. The free plan sleeps and drops live calls.

`FRONTEND_URL` is filled from the frontend hostname, and `VITE_API_URL` is filled from the API hostname. Both are public hosts. The API adds `https://` when the value has no scheme.

Set these secrets on the backend service before the first boot:

| Variable | Purpose |
| :--- | :--- |
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | WebRTC and agent dispatch. |
| `GROQ_API_KEY`, `DEEPGRAM_API_KEY` | LLM and speech. |
| `TAVILY_API_KEY` | Optional web search. |

`VITE_API_URL` is injected at **frontend build time**. Changing the API host means rebuilding the frontend.

What the deploy already enforces:

- One API process, so agent saves are read from disk on every request instead of a stale per-process cache.
- The voice worker restarts if it exits, and `/api/health` reports whether its heartbeat is fresh.
- Uploads are limited to 5 MB PDF/TXT files. Room tokens only match the session name the UI generates.
- Create, token, and log endpoints are rate limited. Server errors do not return stack traces.
- Idle worker processes default to 1 in production so a small instance does not run out of memory.

Agent records still live in `agents.json` on the service disk. That disk is wiped on redeploy. Move `DATA_DIR` to a persistent disk, or replace the file store, before you need agents to survive deploys. The public UI has no user accounts: rate limits slow abuse, they do not replace authentication.