"""Text-to-speech adapter — pluggable, same shape as megotchi_memory.LLM.

Select via MEGOTCHI_TTS, e.g. `elevenlabs:<voice_id>` (default voice "Rachel").
Voice + model overridable via env. SDK imported lazily.

NOTE: ElevenLabs is the MVP/dogfood choice for voice warmth, but its ToS
prohibits under-13 use — it is a swap-before-launch component (see
ARCHITECTURE.md §9). Cartesia / Bedrock are the shippable alternatives.
"""
from __future__ import annotations

import os
from typing import Optional

DEFAULT_VOICE = "21m00Tcm4TlvDq8ikWAM"  # "Rachel" — override via MEGOTCHI_TTS
DEFAULT_MODEL = "eleven_flash_v2_5"     # low-latency (~75ms model time)

# Emotion → delivery. (stability, style): lower stability = more emotional
# variation (less flat/robotic); higher style = more expressive. Driven by the
# `expression` the LLM already returns, so the voice matches the face.
_DELIVERY = {
    "excited":   (0.28, 0.65),
    "happy":     (0.35, 0.55),
    "surprised": (0.30, 0.60),
    "curious":   (0.40, 0.50),
    "neutral":   (0.42, 0.45),
    "gentle":    (0.60, 0.30),
    "sad":       (0.65, 0.25),
}


def default_tts_spec() -> str:
    return os.environ.get("MEGOTCHI_TTS", f"elevenlabs:{DEFAULT_VOICE}")


class TTS:
    def __init__(self, spec: Optional[str] = None):
        spec = spec or default_tts_spec()
        self.provider, _, self.voice = spec.partition(":")
        if self.provider != "elevenlabs":
            raise ValueError(f"Unsupported TTS provider {self.provider!r} "
                             "(only 'elevenlabs' wired for the MVP)")
        self.voice = self.voice or DEFAULT_VOICE
        self.model = os.environ.get("MEGOTCHI_TTS_MODEL", DEFAULT_MODEL)
        from elevenlabs.client import ElevenLabs  # deferred

        self._client = ElevenLabs()  # reads ELEVENLABS_API_KEY
        try:
            from elevenlabs import VoiceSettings
            self._VoiceSettings = VoiceSettings
        except Exception:
            self._VoiceSettings = None

    def _voice_settings(self, expression: str):
        stab, style = _DELIVERY.get(expression, _DELIVERY["neutral"])
        kw = dict(stability=stab, similarity_boost=0.75, style=style,
                  use_speaker_boost=True)
        return self._VoiceSettings(**kw) if self._VoiceSettings else kw

    def synthesize_bytes(self, text: str, output_format: str = "mp3_44100_128",
                         expression: str = "neutral") -> bytes:
        """Render `text` to audio bytes (mp3), voiced to match `expression`."""
        audio = self._client.text_to_speech.convert(
            voice_id=self.voice, model_id=self.model, text=text,
            output_format=output_format,
            voice_settings=self._voice_settings(expression))
        return b"".join(chunk for chunk in audio if chunk)

    def synthesize(self, text: str, out_path: str,
                   output_format: str = "mp3_44100_128",
                   expression: str = "neutral") -> str:
        """Render `text` to an audio file at `out_path`; returns the path."""
        with open(out_path, "wb") as f:
            f.write(self.synthesize_bytes(text, output_format, expression))
        return out_path
