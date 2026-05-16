"""
Custom LiveKit STT adapter that delegates to a self-hosted rt-meet-whisperx
service over HTTP.

rt-meet-whisperx exposes POST /v1/audio/transcriptions with a multipart audio
file body and returns a transcription. It's a batch endpoint (not streaming),
so this adapter buffers VAD-detected utterances and submits one request per
utterance.

Usage in agent.py:

    from whisperx_stt import WhisperXSTT
    stt = WhisperXSTT(
        endpoint=os.environ["WHISPERX_URL"],
        api_key=os.environ.get("WHISPERX_API_KEY"),
        language="en",
    )

The agent's existing VAD (silero) handles utterance segmentation. This adapter
slots into the `agents.stt.StreamAdapter(stt=..., vad=vad)` wrapper used in
agent.py — same shape as the default Whisper plugin.
"""

from __future__ import annotations

import asyncio
import io
import logging
import wave
from typing import AsyncIterable, Optional

import aiohttp
from livekit import agents, rtc

logger = logging.getLogger("revotext-agent.whisperx")


class WhisperXSTT(agents.stt.STT):
    """LiveKit STT that calls rt-meet-whisperx /v1/audio/transcriptions."""

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        language: str = "en",
    ) -> None:
        super().__init__(
            capabilities=agents.stt.STTCapabilities(
                streaming=False,  # batch only — StreamAdapter+VAD wraps for streaming feel
                interim_results=False,
            )
        )
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._language = language
        self._session: Optional[aiohttp.ClientSession] = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    async def _recognize_impl(
        self,
        buffer: rtc.AudioFrame | AsyncIterable[rtc.AudioFrame],
        *,
        language: Optional[str] = None,
        conn_options: Optional[agents.types.APIConnectOptions] = None,
    ) -> agents.stt.SpeechEvent:
        frames: list[rtc.AudioFrame] = []
        if isinstance(buffer, rtc.AudioFrame):
            frames = [buffer]
        else:
            async for f in buffer:
                frames.append(f)

        if not frames:
            return _empty_final_event(language or self._language)

        wav_bytes = _frames_to_wav(frames)

        text = await self._post_transcribe(wav_bytes, language or self._language)

        return agents.stt.SpeechEvent(
            type=agents.stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[
                agents.stt.SpeechData(
                    language=language or self._language,
                    text=text or "",
                    confidence=1.0,
                )
            ],
        )

    async def _post_transcribe(self, wav_bytes: bytes, language: str) -> str:
        session = await self._ensure_session()
        url = f"{self._endpoint}/v1/audio/transcriptions"

        form = aiohttp.FormData()
        form.add_field(
            "file",
            wav_bytes,
            filename="utterance.wav",
            content_type="audio/wav",
        )
        form.add_field("language", language)
        if self._model:
            form.add_field("model", self._model)

        try:
            async with session.post(url, data=form, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                resp.raise_for_status()
                payload = await resp.json()
        except Exception:
            logger.exception("whisperx request failed")
            return ""

        # whisperx returns AudioTranscription | AudioTranscriptionVerbose;
        # both shapes have a `text` field at the top level.
        text = payload.get("text", "") if isinstance(payload, dict) else ""
        return text.strip()

    async def aclose(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


def _frames_to_wav(frames: list[rtc.AudioFrame]) -> bytes:
    sample_rate = frames[0].sample_rate
    channels = frames[0].num_channels
    sample_width = 2  # int16

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        for f in frames:
            wf.writeframes(f.data.tobytes())
    return buf.getvalue()


def _empty_final_event(language: str) -> agents.stt.SpeechEvent:
    return agents.stt.SpeechEvent(
        type=agents.stt.SpeechEventType.FINAL_TRANSCRIPT,
        alternatives=[agents.stt.SpeechData(language=language, text="", confidence=0.0)],
    )
