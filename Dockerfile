FROM python:3.11-slim

WORKDIR /app

# System deps for faster-whisper (ffmpeg + CTranslate2 runtime)
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
      libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first for layer caching
COPY pyproject.toml .
RUN pip install --no-cache-dir \
      "livekit-agents>=0.10.0" \
      "livekit-plugins-openai>=0.10.0" \
      "livekit-plugins-silero>=0.7.0" \
      "livekit-plugins-faster-whisper>=0.1.0"

COPY agent.py .

# Download the Whisper model at build time so first room join isn't slow
ENV WHISPER_MODEL=base.en
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL}', compute_type='int8')"

CMD ["python", "agent.py", "start"]
