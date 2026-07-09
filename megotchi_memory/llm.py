"""LLM adapter — deliberately provider-agnostic (strategy: local Ollama for the
product path, Anthropic/OpenAI-class for development speed).

Select via env var MEGOTCHI_LLM, e.g.:
    MEGOTCHI_LLM=ollama:qwen3:4b
    MEGOTCHI_LLM=anthropic:claude-sonnet-4-6
    MEGOTCHI_LLM=openai:gpt-4o-mini
Default: openai if OPENAI_API_KEY set, else anthropic if ANTHROPIC_API_KEY set,
else ollama:qwen3:4b.

Zero hard dependencies: Ollama via stdlib urllib; Anthropic/OpenAI SDKs imported
only if that provider is chosen.
"""
from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.request
from typing import Optional

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

# Cumulative token usage across ALL calls in this process (replies, greetings,
# memory extraction, reflection). pop_usage() drains it — the voice server
# logs the deltas as spend events for cost tracking. Under concurrent requests
# a single delta may attribute a neighbour's tokens, but the SUM is always
# exact: whatever one pop over-counts, the next pop under-counts.
_usage = {"in": 0, "out": 0}
_usage_lock = threading.Lock()


def _add_usage(tokens_in, tokens_out) -> None:
    with _usage_lock:
        _usage["in"] += int(tokens_in or 0)
        _usage["out"] += int(tokens_out or 0)


def pop_usage() -> dict:
    """Tokens used since the last pop; resets the counter."""
    with _usage_lock:
        u = dict(_usage)
        _usage["in"] = _usage["out"] = 0
        return u


def default_spec() -> str:
    if os.environ.get("MEGOTCHI_LLM"):
        return os.environ["MEGOTCHI_LLM"]
    if os.environ.get("OPENAI_API_KEY"):
        return "openai:gpt-4o-mini"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic:claude-sonnet-4-6"
    return "ollama:qwen3:4b"


class LLM:
    def __init__(self, spec: Optional[str] = None):
        spec = spec or default_spec()
        self.provider, _, self.model = spec.partition(":")
        if self.provider not in ("ollama", "anthropic", "openai"):
            raise ValueError(f"Unknown LLM provider in spec {spec!r}")
        if not self.model:
            raise ValueError(f"LLM spec {spec!r} needs a model, e.g. openai:gpt-4o-mini")
        self._anthropic = None
        self._openai = None
        if self.provider == "anthropic":
            import anthropic  # deferred import; only needed for this provider

            # Generous retry budget: the SDK does exponential backoff on 429 /
            # 5xx / 529 (overloaded). A long suite run must ride through a
            # transient API blip instead of discarding hours of work.
            self._anthropic = anthropic.Anthropic(max_retries=8)
        elif self.provider == "openai":
            import openai  # deferred import; only needed for this provider

            self._openai = openai.OpenAI(max_retries=8)

    def complete(self, system: str, user: str, want_json: bool = False,
                 max_tokens: int = 1500, schema: Optional[dict] = None) -> str:
        """`schema` (a JSON Schema dict) enables grammar-constrained structured
        output on Ollama — essential for small local models, which otherwise
        emit valid-but-wrong-shape JSON. Frontier models (Anthropic/OpenAI)
        follow the prompt's shape reliably, so the exact schema is ignored there
        (OpenAI just gets JSON mode when structured output is requested)."""
        if self.provider == "ollama":
            return self._ollama(system, user, want_json, max_tokens, schema)
        if self.provider == "openai":
            return self._openai_complete(system, user, want_json, max_tokens, schema)
        return self._claude(system, user, max_tokens)

    def _openai_complete(self, system: str, user: str, want_json: bool,
                         max_tokens: int, schema: Optional[dict]) -> str:
        kwargs: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_completion_tokens": max_tokens,
        }
        if want_json or schema is not None:
            # JSON mode: reliable object output. (The prompt already specifies
            # the shape; frontier models follow it without a strict schema.)
            kwargs["response_format"] = {"type": "json_object"}
        resp = self._openai.chat.completions.create(**kwargs)
        if resp.usage:
            _add_usage(resp.usage.prompt_tokens, resp.usage.completion_tokens)
        return resp.choices[0].message.content or ""

    def _ollama(self, system: str, user: str, want_json: bool, max_tokens: int,
                schema: Optional[dict] = None) -> str:
        payload: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"num_predict": max_tokens},
        }
        structured = schema is not None or want_json
        if structured:
            # Thinking models (qwen3, deepseek-r1) spend the whole budget on
            # hidden reasoning when output is grammar-constrained and return
            # empty content — disable thinking for structured calls.
            payload["think"] = False
            payload["format"] = schema if schema is not None else "json"
        else:
            # Chat: thinking stays ON — Ollama routes it to a separate channel
            # and message.content is the clean spoken reply. Leave headroom so
            # thinking doesn't eat the whole budget before the reply starts.
            payload["options"]["num_predict"] = max(max_tokens, 2048)

        out = self._ollama_try(payload, allow_thinking_fallback=structured)
        if not structured and not out.strip():
            # The model spent everything thinking and said nothing — retry with
            # thinking off; any narration that leaks into content is then the
            # speech filter's job. NEVER return the thinking channel as speech
            # (that regressed the eval to 0/6 in run 7).
            payload["think"] = False
            out = self._ollama_try(payload, allow_thinking_fallback=False)
        return out

    def _ollama_try(self, payload: dict, allow_thinking_fallback: bool) -> str:
        try:
            return self._ollama_post(payload, allow_thinking_fallback)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            # Older Ollama / non-thinking models reject the "think" field.
            if "think" in detail.lower() and "think" in payload:
                payload.pop("think", None)
                return self._ollama_post(payload, allow_thinking_fallback)
            raise RuntimeError(f"Ollama error {e.code}: {detail[:200]}") from e

    def _ollama_post(self, payload: dict, allow_thinking_fallback: bool) -> str:
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            body = json.loads(resp.read())
        _add_usage(body.get("prompt_eval_count", 0), body.get("eval_count", 0))
        msg = body.get("message", {})
        content = msg.get("content", "")
        # Structured calls only: some Ollama versions put everything in the
        # thinking channel; the JSON we asked for may be salvageable there.
        # NEVER do this for chat — thinking is not speech.
        if allow_thinking_fallback and not content.strip() and msg.get("thinking"):
            return msg["thinking"]
        return content

    def _claude(self, system: str, user: str, max_tokens: int) -> str:
        msg = self._anthropic.messages.create(
            model=self.model,
            system=system,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": user}],
        )
        _add_usage(msg.usage.input_tokens, msg.usage.output_tokens)
        return "".join(b.text for b in msg.content if b.type == "text")


def parse_json_block(text: str) -> dict:
    """Robustly pull a JSON object out of a model reply (handles code fences
    and thinking-model preambles like qwen3's <think>…</think>)."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"```(?:json)?", "", text).strip("` \n")
    start = text.find("{")
    if start == -1:
        raise ValueError(f"No JSON object in model reply: {text[:200]!r}")
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError("Unbalanced JSON in model reply")
