"""
Local LiveKit STT adapter using `faster-whisper` directly.

There's no upstream `livekit-plugins-faster-whisper` package on PyPI, so this
wraps faster-whisper in the same STT interface as the plugin packages. Used by
agent.py when WHISPER_MODE=local.

The model is loaded lazily on first transcription request.
"""

from __future__ import annotations

import io
import logging
import os
import wave
from typing import AsyncIterable, Optional

from livekit import agents, rtc

logger = logging.getLogger("revotext-agent.local-whisper")


class LocalWhisperSTT(agents.stt.STT):
    """LiveKit STT backed by faster-whisper running in-process."""

    def __init__(
        self,
        *,
        model_size: str = "base.en",
        compute_type: str = "int8",
        language: str = "en",
        device: str = "cpu",
    ) -> None:
        super().__init__(
            capabilities=agents.stt.STTCapabilities(
                streaming=False,
                interim_results=False,
            )
        )
        self._model_size = model_size
        self._compute_type = compute_type
        self._device = device
        self._language = language
        self._model = None  # lazy load

    def _get_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            logger.info(
                "loading faster-whisper model=%s compute=%s device=%s",
                self._model_size,
                self._compute_type,
                self._device,
            )
            self._model = WhisperModel(
                self._model_size,
                device=self._device,
                compute_type=self._compute_type,
            )
        return self._model

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
            return _empty_final(language or self._language)

        wav_bytes = _frames_to_wav(frames)
        text = self._transcribe_sync(wav_bytes, language or self._language)

        return agents.stt.SpeechEvent(
            type=agents.stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[
                agents.stt.SpeechData(
                    language=language or self._language,
                    text=text,
                    confidence=1.0,
                )
            ],
        )

    def _transcribe_sync(self, wav_bytes: bytes, language: str) -> str:
        model = self._get_model()

        # faster-whisper accepts a file path or BytesIO of WAV/MP3/etc.
        segments, _info = model.transcribe(
            io.BytesIO(wav_bytes),
            language=language,
            vad_filter=False,  # agent already gates with silero VAD
            beam_size=1,
        )
        return " ".join(s.text.strip() for s in segments).strip()


def _frames_to_wav(frames: list[rtc.AudioFrame]) -> bytes:
    sample_rate = frames[0].sample_rate
    channels = frames[0].num_channels
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        for f in frames:
            wf.writeframes(f.data.tobytes())
    return buf.getvalue()


def _empty_final(language: str) -> agents.stt.SpeechEvent:
    return agents.stt.SpeechEvent(
        type=agents.stt.SpeechEventType.FINAL_TRANSCRIPT,
        alternatives=[agents.stt.SpeechData(language=language, text="", confidence=0.0)],
    )
