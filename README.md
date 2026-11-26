# Custom Voice Agent

A real-time voice assistant. You sign in, create an agent, give it a job and a document, and talk to it in the browser. Harbor Desk answers a shop FAQ. City Hospital Front Desk is one agent that can book a visit: it checks open times, waits for a name and a 10-digit phone number, and saves the slot so that hospital cannot book the same department, date, and time twice.

The browser streams audio over WebRTC (LiveKit). Deepgram turns speech into text and text back into speech. Groq writes the reply. A FastAPI service stores accounts, agents, and visits. A Python worker runs the live call.

## Latency

The design target is a turn under **500ms**, from the moment speech ends to the first audio of the reply. That budget is a plan. The table under it is one real turn, not that plan.

| Stage | Design budget | What it covers |
| :--- | ---: | :--- |
| WebRTC uplink | ~25ms | Opus frames from the browser to the LiveKit SFU |
| Speech-to-text | ~110ms | Deepgram Nova-3, including end-of-speech detection |
| Input check | <1ms | Empty, blocked, and prompt-injection filter |
| Language model | ~185ms | Groq time to the first token (`gpt-oss-20b`, then `gpt-oss-120b`) |
| Text-to-speech | ~85ms | Deepgram Aura-2 time to the first audio byte |
| Playback | ~30ms | Downlink and the browser starting the buffer |
| **End to end** | **~435ms** | **Target: under 500ms** |

One live turn was measured by publishing the spoken question “What is the capital of France?” into a LiveKit room. The worker transcribed it and began “Paris is the capital of.” Clocks are the worker’s own, from when it decided the utterance had ended:

| Stage | Measured |
| :--- | ---: |
| Speech-to-text | 0ms reported |
| Language model, first token | 548ms |
| Text-to-speech, first audio | 378ms |
| Speech end to first audio | 933ms |

Speech-to-text shows 0ms because the transcript arrived before voice detection marked the end of the clip, and a negative gap is clamped to zero. The browser end-to-end number stayed empty on this run because the probe was not the web page, so it never sent a playback acknowledgement. One turn is not an average. This one missed the 500ms target. The language model and the speech synthesizer were the slow stages.

Each turn still records speech end, the final transcript, the first model token, the first speech-audio frame, and a browser playback acknowledgement. The UI shows those values for the session in front of you.

Two measurement bugs had to be fixed before those numbers meant anything. If speech-to-text finished before voice-activity detection reported silence, latency came out negative. If detection missed the utterance and the transcript arrived late, the same gap looked like multiple seconds. Both cases are clamped so a clock-order glitch cannot show up as a fake latency.

## What a call can do

Harbor Desk uses the pasted document, cut at 12,000 characters, and may search the web. City Hospital Front Desk does not search the web. Its hours live in code: Monday–Saturday, 09:00–16:30, every 30 minutes, closed Sunday. Today’s slots must start at least 15 minutes ahead. Open times are that grid minus rows already saved for that hospital.

A booking stores a confirmation id, the hospital account, the hospital name, the patient name, the phone, the department, the date, and the time. The phone is exactly 10 digits and is encrypted at rest. Lookup and cancel on a call, and the public confirmation form, require that same phone. The signed-in hospital calendar shows Booked, Passed, or Open. It does not show patient names.

One voice worker holds one live call (`MAX_ACTIVE_CALLS`, default 1). A second token request gets HTTP 409 until that room is released or the hold goes stale (90 seconds).

## Engineering problems this had to solve

**Barge-in left ghost audio.** If the caller interrupted the assistant, audio already in flight kept playing over the new reply. Every response carries a generation id. When speech starts again, the worker cancels that generation, closes the in-flight model stream, and the UI drops text and audio that belong to the old id.

**A slow TTS provider stalled the turn.** A 2.5s timeout cancels a hung synthesis stream and surfaces a backpressure warning. The transcript still appears, so a stuck provider does not freeze the session.

**Data-channel messages arrived twice or out of order.** Control messages carry a monotonic sequence. The UI drops anything at or below the last sequence it accepted, including duplicate token deltas.

**Long chats blew the context window and slowed the first token.** The safety prompt stays pinned. Older turns are truncated to the latest 10 messages (about 5 dialog turns) so Groq is not rereading the whole conversation on every reply.

**WebSocket audio was the wrong transport.** Raw socket audio has no jitter buffer, no Opus, and no NAT traversal. LiveKit carries full-duplex audio and the control messages on one WebRTC session, so there is no second socket to keep in sync.

**Two hospitals could not share one clock time.** Visits are keyed by account, department, date, and time. The same 11:00 can exist once per hospital.

**A public calendar listed patient names.** The desk view now returns time, label, and state. Confirmation lookup returns the visit only when the phone matches, and the response omits the phone.

**Rate limits lived in process memory.** Hits are rows in `rate_hits`, so a restart does not wipe the window. Limits are per client address: auth 8/min, agent writes 10/min, confirmation lookup 8/min, token 20/min, frontend logs 30/min. `TRUST_PROXY` must be true before the last `X-Forwarded-For` hop is treated as the client.

**Groq can refuse a turn.** `gpt-oss-20b` and `gpt-oss-120b` share one token budget. When both fail, the worker tries `llama-3.1-8b-instant`. That third name has to be a model the Groq account can call. If every model fails, the worker asks the caller to say that again, and the booking tool does not run.

Deeper notes on the waterfall, the booking data, and the barge-in sequence are in [`ARCHITECTURE.md`](./ARCHITECTURE.md).

## Run it locally

Python 3.10 or newer. Copy the example env and fill in LiveKit, Groq, Deepgram, and Tavily. Do not commit `.env`.

```bash
cp .env.example .env
yarn install
yarn dev
```

In a second terminal, from `backend`:

```bash
python -m venv venv
# Windows: .\venv\Scripts\activate
# macOS/Linux: source venv/bin/activate
pip install -r requirements.txt
python main.py
```

In a third terminal, with the same virtualenv active and the same working directory:

```bash
python agent_worker.py start
```

Point `DATA_DIR` at the `backend` folder for both Python processes when it is not already that folder. The API and the worker must share one `vocal.db`. On Windows PowerShell:

```powershell
$env:DATA_DIR = "d:\Documents\vocal-voice\backend"
python main.py
```

Open `http://localhost:5173`, register a hospital, and start a session. The first account created while `ENV=development` claims agents and visits that have no owner. Production registration does not claim those rows.

`ENABLE_DEMO_TRIGGERS` defaults to on locally and off in production. While it is on, saying "simulate delay" or "simulate duplicate" exercises the timeout and the duplicate-packet drop.

`SESSION_SECRET` signs the 14-day login token and encrypts phone numbers. Locally, if it is unset, a secret is written under `DATA_DIR`. Changing it makes stored phones unreadable. Production refuses to boot without `SESSION_SECRET`.

## Deploy

`render.yaml` defines two services: `vocal-backend` (API and voice worker) and `vocal-frontend` (static build). The backend uses Render’s starter plan and a 1GB disk mounted at `/var/data`. A free instance sleeps and drops a live call. A deploy still restarts the process and drops a call that is already connected. `MAX_ACTIVE_CALLS` is 1.

`FRONTEND_URL` comes from the frontend hostname. `VITE_API_URL` comes from the API hostname and is baked in at **frontend build time**, so changing the API host means rebuilding the frontend.

Set these on the backend service before the first boot:

| Variable | Purpose |
| :--- | :--- |
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | WebRTC and agent dispatch |
| `GROQ_API_KEY`, `DEEPGRAM_API_KEY` | Language model and speech |
| `TAVILY_API_KEY` | Web search for agents that do not book visits |
| `SESSION_SECRET` | Login tokens and phone encryption. Generated by the blueprint. Production boot fails fast without it. |
| `HOSPITAL_TZ` | Timezone for "today", slot cutoffs and the spoken clock. Defaults to `Asia/Kolkata`. |
| `DATABASE_URL` | Optional Postgres URL. Empty means SQLite at `DATA_DIR/vocal.db`. |
| `DATA_DIR` | `/var/data` on the Render disk |
| `TRUST_PROXY` | `true` on Render, so the last forwarded hop is the client |
| `MAX_ACTIVE_CALLS` | `1` |

SQLite on that disk keeps accounts and visits across a redeploy of the same service. Set `DATABASE_URL` when the API and the worker should share Postgres. That path is in the code. It has not been run against a live Postgres server in this project.
