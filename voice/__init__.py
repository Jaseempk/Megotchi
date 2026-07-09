"""Voice MVP — the cascade that turns the memory engine into a talking toy.

    audio in ─▶ STT ─▶ memory-injected LLM (speak) ─▶ TTS ─▶ audio out
                         │
                         └▶ (after playback) update_memory / nightly reflect

STT/TTS are provider-agnostic adapters (same pattern as megotchi_memory.LLM).
The transport (Mac mic today, ESP32 WebSocket next) plugs into VoicePipeline.
"""
from .stt import STT
from .tts import TTS
from .pipeline import VoicePipeline

__all__ = ["STT", "TTS", "VoicePipeline"]
