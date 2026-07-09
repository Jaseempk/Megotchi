"""megotchi-memory — the soul of Megotchi.

Five-layer memory architecture (see ../MEGOTCHI-STRATEGY.md §1.6):
1. Working context      -> retrieve.build_memory_context
2. Episodic store       -> store.Store (append-only, never destroyed)
3. Bi-temporal facts    -> store.Store (supersede, never erase; soft delete)
4. Nightly reflection   -> reflect.run_reflection
5. Parent-readable doc  -> reflect.write_profile (PROFILE.md)

Personality lives in this data, not in model weights — portable across
hardware generations by design.
"""
from .llm import LLM
from .store import Store
from .extract import update_memory
from .reflect import run_reflection, write_profile, render_profile
from .retrieve import build_memory_context
from .speech import clean_speech, speak, speak_expressive

__all__ = [
    "LLM",
    "Store",
    "update_memory",
    "run_reflection",
    "write_profile",
    "render_profile",
    "build_memory_context",
    "clean_speech",
    "speak",
    "speak_expressive",
]
