# Vocal-Voice

My real-time voice assistant. The browser streams audio over WebRTC (LiveKit). Deepgram turns speech into text and text back into speech. Groq writes the reply. A FastAPI service creates agents and issues room tokens. A Python worker runs the live pipeline.

## Latency

The design target is a turn under **500ms**, from the moment speech ends to the first audio of the reply. That budget is a plan. The table under it is one real turn, not that plan.

| Stage | Design budget | What it covers |
| :--- | ---: | :--- |
| WebRTC uplink | ~25ms | Opus frames from the browser to the LiveKit SFU |
| Speech-to-text | ~110ms | Deepgram Nova-3, including end-of-speech detection |
| Input check | <1ms | Empty, blocked, and prompt-injection filter |
| Language model | ~185ms | Groq time to the first token (`gpt-oss-20b`, with a larger fallback) |
| Text-to-speech | ~85ms | Deepgram Aura-2 time to the first audio byte |
| Playback | ~30ms | Downlink and the browser starting the buffer |
| **End to end** | **~435ms** | **Target: under 500ms** |

I measured one live turn by publishing the spoken question “What is the capital of France?” into a LiveKit room. The worker transcribed it and began “Paris is the capital of.” Clocks are the worker’s own, from when it decided the utterance had ended:

| Stage | Measured |
| :--- | ---: |
| Speech-to-text | 0ms reported |
| Language model, first token | 548ms |
| Text-to-speech, first audio | 378ms |
| Speech end to first audio | 933ms |

Speech-to-text shows 0ms because the transcript arrived before voice detection marked the end of the clip, and a negative gap is clamped to zero. The browser end-to-end number stayed empty on this run because the probe was not the web page, so it never sent a playback acknowledgement. One turn is not an average. This one missed the 500ms target. The language model and the speech synthesizer were the slow stages.

Each turn still records speech end, the final transcript, the first model token, the first speech-audio frame, and a browser playback acknowledgement. The UI shows those values for the session in front of you.

Two measurement bugs had to be fixed before those numbers meant anything. If speech-to-text finished before voice-activity detection reported silence, latency came out negative. If detection missed the utterance and the transcript arrived late, the same gap looked like multiple seconds. Both cases are clamped so a clock-order glitch cannot show up as a fake latency.

## Engineering problems this had to solve

**Barge-in left ghost audio.** If I interrupted the assistant, audio already in flight kept playing over the new reply. Every response now carries a generation id. When speech starts again, the worker cancels that generation, closes the in-flight model stream, and the UI drops text and audio that belong to the old id.

**A slow TTS provider stalled the turn.** A 2.5s timeout cancels a hung synthesis stream and surfaces a backpressure warning. The transcript still appears, so a stuck provider does not freeze the session.

**Data-channel messages arrived twice or out of order.** Control messages carry a monotonic sequence. The UI drops anything at or below the last sequence it accepted, including duplicate token deltas.

**Long chats blew the context window and slowed the first token.** The safety prompt stays pinned. Older turns are truncated to the latest 10 messages (about 5 dialog turns) so Groq is not rereading the whole conversation on every reply.

**WebSocket audio was the wrong transport.** Raw socket audio has no jitter buffer, no Opus, and no NAT traversal. LiveKit carries full-duplex audio and the control messages on one WebRTC session, so there is no second socket to keep in sync.

**The API and the worker disagreed about agents.** Several Gunicorn processes each cached `agents.json` in memory, so a save on one process was invisible to the others. Production runs a single API process and reads the file on every request. The worker restarts if it exits, and `/api/health` reports whether its heartbeat is fresh.

**A public token endpoint can burn provider quota.** Room names must match the id the UI generates. Uploads are limited to 5MB PDF or TXT files. Create, token, and log routes are rate limited. Failures return a short message, not a stack trace. Browser calls are limited to `FRONTEND_URL`.

Deeper notes on the waterfall, transport choice, and the barge-in sequence are in [`ARCHITECTURE.md`](./ARCHITECTURE.md).

## Run it locally

Python 3.10 or newer. Copy the example env and fill in LiveKit, Groq, Deepgram, and Tavily. Do not commit `.env`.

```bash
cp .env.example .env
yarn install
yarn dev
```

In a second terminal:

```bash
cd backend
python -m venv venv
# Windows: .\venv\Scripts\activate
# macOS/Linux: source venv/bin/activate
pip install -r requirements.txt
python main.py
```

In a third terminal, with the same virtualenv active:

```bash
cd backend
python agent_worker.py start
```

Open `http://localhost:5173`, create an agent, and start a session.

`ENABLE_DEMO_TRIGGERS` defaults to on locally and off in production. While it is on, saying "simulate delay" or "simulate duplicate" exercises the timeout and the duplicate-packet drop.

## Deploy

`render.yaml` defines two services: `vocal-backend` (API and voice worker) and `vocal-frontend` (static build). The backend uses Render's Starter plan so the worker stays awake. The free plan sleeps and drops live calls.

`FRONTEND_URL` comes from the frontend hostname. `VITE_API_URL` comes from the API hostname and is baked in at **frontend build time**, so changing the API host means rebuilding the frontend.

Set these on the backend service before the first boot:

| Variable | Purpose |
| :--- | :--- |
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | WebRTC and agent dispatch |
| `GROQ_API_KEY`, `DEEPGRAM_API_KEY` | Language model and speech |
| `TAVILY_API_KEY` | Optional web search |

Agents are stored in `agents.json` on the service disk. That disk is wiped on redeploy. Point `DATA_DIR` at a persistent disk, or replace the file store, if agents need to survive deploys. The UI has no accounts. Rate limits slow abuse. They are not a substitute for authentication.
