# Megotchi voice server — a lightweight, GPU-less orchestrator
# (STT/LLM/TTS are external API calls; this just routes + holds memory).
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MEGOTCHI_DATA=/data

COPY requirements-server.txt .
RUN pip install -r requirements-server.txt

# Only what the server needs: the memory engine + the voice adapters/server.
COPY megotchi_memory ./megotchi_memory
COPY voice ./voice

RUN mkdir -p /data
EXPOSE 8000

# $PORT is provided by Railway; default 8000 for local docker runs.
CMD ["sh", "-c", "uvicorn voice.server:app --host 0.0.0.0 --port ${PORT:-8000}"]
