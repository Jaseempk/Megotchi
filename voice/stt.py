"""Speech-to-text adapter — pluggable, same shape as megotchi_memory.LLM.

Select via MEGOTCHI_STT, e.g. `openai:gpt-4o-mini-transcribe` (default).
SDK imported lazily so importing this module needs no dependencies.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def default_stt_spec() -> str:
    return os.environ.get("MEGOTCHI_STT", "openai:gpt-4o-mini-transcribe")


class STT:
    def __init__(self, spec: Optional[str] = None):
        spec = spec or default_stt_spec()
        self.provider, _, self.model = spec.partition(":")
        if self.provider != "openai":
            raise ValueError(f"Unsupported STT provider {self.provider!r} "
                             "(only 'openai' wired for the MVP)")
        self.model = self.model or "gpt-4o-mini-transcribe"
        import openai  # deferred

        self._client = openai.OpenAI(max_retries=6)

    def transcribe(self, wav_path: str, language: str = "en") -> str:
        with open(wav_path, "rb") as f:
            return self.transcribe_bytes(f.read(), Path(wav_path).name, language)

    def transcribe_bytes(self, data: bytes, filename: str = "audio.webm",
                         language: str = "en") -> str:
        """Transcribe raw audio bytes (e.g. a browser MediaRecorder blob). The
        filename extension tells OpenAI the format (webm/mp4/m4a/wav/mp3…)."""
        resp = self._client.audio.transcriptions.create(
            model=self.model, file=(filename, data), language=language,
            response_format="text")
        text = resp if isinstance(resp, str) else getattr(resp, "text", str(resp))
        return text.strip()
