# RevoText Meet — Transcription Agent

A LiveKit Agent that joins every room, subscribes to participant audio, runs it through Whisper, and publishes `TranscriptionSegment`s back into the room. The RevoText Meet frontend's **ScriptSync** panel listens for those segments via `RoomEvent.TranscriptionReceived` and renders them as a live transcript.

Pairs with: [revotext-meet-design](https://github.com/michaeljhewitt-cary/revotext-meet-design).

## How it works

```
  ┌──────────────────────┐         ┌─────────────────────────────┐
  │ revotext-meet-design │         │  revotext-meet-agent (this) │
  │  (Next.js frontend)  │         │   (Python LiveKit Agent)    │
  └──────────┬───────────┘         └──────────────┬──────────────┘
             │ joins room                         │ also joins room
             ▼                                    ▼
      ╔═════════════════════ LiveKit Cloud / Self-host ═════════════════════╗
      ║   Participants publish audio  ──────────▶  Agent subscribes        ║
      ║   Transcript segments ◀────  Agent publishes via                   ║
      ║                              local_participant.publish_transcription║
      ╚════════════════════════════════════════════════════════════════════╝
             │
             ▼ RoomEvent.TranscriptionReceived
       ScriptSync panel renders the transcript in real time
```

## Run locally

Requires Python 3.10+ and [uv](https://github.com/astral-sh/uv) (or pip).

```bash
# 1. Install
uv venv && source .venv/bin/activate
uv pip install -e .

# 2. Configure
cp .env.example .env
# fill in LIVEKIT_URL / KEY / SECRET (same values as the Next.js app)

# 3. Run
export $(cat .env | xargs)   # or use python-dotenv inside agent.py
python agent.py dev
```

The agent will register with LiveKit Cloud's job-dispatch system and automatically join any room that's created in your project. Open https://revotext-meet-design.vercel.app, start a meeting, and watch the ScriptSync panel light up.

## STT choices

| Mode | Cost | Latency | Where it runs |
|------|------|---------|---------------|
| **local** (default) — `faster-whisper` | Free | ~1-3s | On the agent host |
| **api** — OpenAI Whisper API | ~$0.006/min | ~2-4s | OpenAI servers |

Switch via `WHISPER_MODE` in `.env`. For production, consider Deepgram (separate plugin, sub-second latency, ~$0.43/hr) — drop in via `livekit-plugins-deepgram`.

### Local Whisper model sizes

| Model | RAM | Speed (CPU) | Accuracy |
|-------|-----|-------------|----------|
| `tiny.en` | ~1 GB | Very fast | OK |
| `base.en` | ~1 GB | Fast | Good (default) |
| `small.en` | ~2 GB | Moderate | Better |
| `medium.en` | ~5 GB | Slow on CPU | Very good |
| `large-v3` | ~10 GB | GPU recommended | Best |

## Deploy

Pick a host that can run a long-lived Python process:

### Fly.io (cheap, easy — config included)

A `fly.toml` is committed at the repo root. Steps:

```bash
brew install flyctl
fly auth login
fly launch --no-deploy --copy-config --name revotext-meet-agent
fly secrets set \
  LIVEKIT_URL=wss://revo-test-ui-4zvndup0.livekit.cloud \
  LIVEKIT_API_KEY=APIrhk9c38MkEPk \
  LIVEKIT_API_SECRET=3P9fQdjEvgFrt7O3cW48n1epPMpAk89aXEPmhD0UQ8fB
fly deploy
```

To use rt-meet-whisperx as the STT engine instead of local faster-whisper:

```bash
fly secrets set \
  WHISPER_MODE=whisperx \
  WHISPERX_URL=https://whisperx.your-domain.com \
  WHISPERX_API_KEY=your-key
fly deploy
```

VM is sized for `base.en` local Whisper (2 CPU / 2 GB RAM). For `medium` or `large-v3` you'll want GPU — bump to `fly machines run --vm-size performance-2x` or move to a GPU host.

### Hetzner / NixOS (where Cullin already runs revotext-meet-server)

Add a systemd service that runs `python agent.py start` with the env vars set. The agent is a worker, not an HTTP service — no port to expose.

### Docker

A minimal `Dockerfile` is included. Use it as a base for any container host.

## Using your existing `rt-meet-whisperx` fork

`rt-meet-whisperx` already runs WhisperX (Whisper + alignment + diarization). To wire it to LiveKit:

1. In `rt-meet-whisperx`, expose the transcribe function as an async generator that yields `{text, speaker, start, end, final}` records.
2. In this agent, swap `build_stt()` to a custom `STT` subclass that calls into WhisperX over RPC, gRPC, or an in-process import.
3. Map the diarized speaker labels into the participant identity in `publish_segment()` — that's how the frontend color-codes Court / Witness / Counsel.

The integration is ~50 lines of glue once both sides expose a stream.

## What happens when the agent isn't running

Nothing breaks. The frontend's ScriptSync panel shows "Awaiting transcript stream" and has a **Run Demo** button that plays a scripted deposition for showcasing the design.

## License

MIT.
