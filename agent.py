"""
RevoText Meet — Transcription Agent

Joins LiveKit rooms automatically, subscribes to every participant's audio
track, streams it through faster-whisper, and publishes TranscriptionSegments
back into the room. The RevoText Meet frontend's ScriptSync panel listens for
those segments and renders them.

Run locally:
    uv pip install -e .
    export LIVEKIT_URL=wss://your-project.livekit.cloud
    export LIVEKIT_API_KEY=...
    export LIVEKIT_API_SECRET=...
    python agent.py dev

Deploy:
    See README.md for Fly.io / Render / Docker instructions.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass

from livekit import agents, rtc
from livekit.agents import AutoSubscribe, JobContext, WorkerOptions, cli
from livekit.agents.stt import SpeechEvent, SpeechEventType
from livekit.plugins import openai, silero

logger = logging.getLogger("revotext-agent")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


# ---------------------------------------------------------------------------
# STT — uses Whisper locally via faster-whisper (free) or OpenAI Whisper API.
# Switch by setting WHISPER_MODE=local (default) or WHISPER_MODE=api.
# ---------------------------------------------------------------------------
def build_stt() -> agents.stt.STT:
    mode = os.getenv("WHISPER_MODE", "local").lower()

    if mode == "whisperx":
        url = os.environ.get("WHISPERX_URL")
        if not url:
            raise RuntimeError("WHISPER_MODE=whisperx needs WHISPERX_URL")
        from whisperx_stt import WhisperXSTT
        logger.info("STT: rt-meet-whisperx @ %s", url)
        return WhisperXSTT(
            endpoint=url,
            api_key=os.environ.get("WHISPERX_API_KEY"),
            model=os.environ.get("WHISPERX_MODEL"),
            language="en",
        )

    if mode == "api":
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("WHISPER_MODE=api needs OPENAI_API_KEY")
        logger.info("STT: OpenAI Whisper API")
        return openai.STT(model="whisper-1", language="en")

    # local — faster-whisper
    try:
        from livekit.plugins import faster_whisper  # type: ignore
        logger.info("STT: local faster-whisper %s", os.getenv("WHISPER_MODEL", "base.en"))
        return faster_whisper.STT(
            model_size=os.getenv("WHISPER_MODEL", "base.en"),
            compute_type=os.getenv("WHISPER_COMPUTE", "int8"),
            language="en",
        )
    except ImportError:
        logger.warning(
            "livekit-plugins-faster-whisper not installed; install with "
            "`uv pip install livekit-plugins-faster-whisper` for local STT. "
            "Falling back to OpenAI Whisper API."
        )
        return openai.STT(model="whisper-1", language="en")


# ---------------------------------------------------------------------------
# Per-participant transcription pipeline
# ---------------------------------------------------------------------------
@dataclass
class ParticipantTranscriber:
    """Holds the per-participant STT stream and a stable ID for the active utterance."""

    track: rtc.Track
    participant: rtc.RemoteParticipant
    stt_stream: agents.stt.SpeechStream
    current_segment_id: str

    @classmethod
    def create(
        cls,
        stt: agents.stt.STT,
        track: rtc.Track,
        participant: rtc.RemoteParticipant,
    ) -> "ParticipantTranscriber":
        return cls(
            track=track,
            participant=participant,
            stt_stream=stt.stream(),
            current_segment_id=str(uuid.uuid4()),
        )


async def transcribe_track(
    ctx: JobContext,
    pt: ParticipantTranscriber,
) -> None:
    """Pump audio frames from the track into the STT stream and publish results."""
    audio_stream = rtc.AudioStream(pt.track)

    async def forward_audio() -> None:
        async for ev in audio_stream:
            pt.stt_stream.push_frame(ev.frame)

    async def emit_transcripts() -> None:
        async for event in pt.stt_stream:
            await handle_stt_event(ctx, pt, event)

    await asyncio.gather(forward_audio(), emit_transcripts())


async def handle_stt_event(
    ctx: JobContext,
    pt: ParticipantTranscriber,
    event: SpeechEvent,
) -> None:
    """Map STT events → LiveKit TranscriptionSegments published to the room."""
    if event.type == SpeechEventType.INTERIM_TRANSCRIPT:
        await publish_segment(ctx, pt, event.alternatives[0].text, final=False)

    elif event.type == SpeechEventType.FINAL_TRANSCRIPT:
        await publish_segment(ctx, pt, event.alternatives[0].text, final=True)
        # Roll segment id for the next utterance.
        pt.current_segment_id = str(uuid.uuid4())


async def publish_segment(
    ctx: JobContext,
    pt: ParticipantTranscriber,
    text: str,
    final: bool,
) -> None:
    if not text.strip():
        return

    segment = rtc.TranscriptionSegment(
        id=pt.current_segment_id,
        text=text,
        start_time=0,
        end_time=0,
        language="en",
        final=final,
    )

    transcription = rtc.Transcription(
        participant_identity=pt.participant.identity,
        track_sid=pt.track.sid,
        segments=[segment],
    )

    try:
        await ctx.room.local_participant.publish_transcription(transcription)
    except Exception:
        logger.exception("publish_transcription failed")


# ---------------------------------------------------------------------------
# Entry point — runs once per dispatched job (one room)
# ---------------------------------------------------------------------------
async def entrypoint(ctx: JobContext) -> None:
    logger.info("connecting to room %s", ctx.room.name)
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    stt = build_stt()
    vad = silero.VAD.load()  # used to gate silence

    # Wrap STT with VAD so we don't pay (compute or $) for silence.
    stt = agents.stt.StreamAdapter(stt=stt, vad=vad)

    pumps: list[asyncio.Task] = []

    @ctx.room.on("track_subscribed")
    def on_track(
        track: rtc.Track,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return
        logger.info("subscribed to audio from %s", participant.identity)
        pt = ParticipantTranscriber.create(stt, track, participant)
        pumps.append(asyncio.create_task(transcribe_track(ctx, pt)))

    # Wait until the room disconnects.
    await asyncio.Future()


def main() -> None:
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            # Auto-dispatch to every room. To restrict, set agent_name and
            # call /agents/dispatch from the backend.
        )
    )


if __name__ == "__main__":
    main()
