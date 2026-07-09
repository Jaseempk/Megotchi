"""Speech-path output (FINDINGS F8 / F15 / F18 / F20).

The robust way to get clean speech from a small model is `speak()`: force the
reply through a JSON grammar with a spoken `reply` field and an unspoken
`recall` scratchpad. Deliberation is then STRUCTURALLY unable to reach the
child — no phrase-blocklist to maintain, no arms race (F20). This is the same
grammar-constrained mechanism that makes extraction/reflection reliable.

`clean_speech()` remains as a best-effort salvage for free-text replies (and
as speak()'s own fallback): it strips think-tags and known deliberation
markers. It is a blocklist and therefore leaks by construction — kept only for
defense in depth, not as the primary guarantee.
"""
from __future__ import annotations

import re

from .llm import parse_json_block

FALLBACK = "Hmm, my thoughts got tangled — tell me that again?"

# Grammar for the answer path: `reply` is spoken, `recall` is a private
# scratchpad the model may reason in but the child never hears.
REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "recall": {"type": "string"},
        "reply": {"type": "string"},
    },
    "required": ["reply"],
}

_JSON_HINT = (
    "\n\nReply as JSON. Put any reasoning in \"recall\" (the child NEVER sees "
    "it). Put ONLY the words you say out loud — 1-3 short sentences — in "
    "\"reply\". In \"reply\" talk TO the child using \"you\" and \"your\" "
    "(e.g. \"You used to be afraid of the dark\"); never speak as the child "
    "with \"I\" about their own life.")

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
# Orphan closer: replies sometimes arrive as "…reasoning…</think>\n\nreply"
# (the opener was consumed as a separate channel). Drop everything up to a
# lone </think> — that prefix is reasoning, never speech — then any stray tag.
_ORPHAN_CLOSE_RE = re.compile(r"^.*?</think>", re.DOTALL | re.IGNORECASE)
_ORPHAN_TAG_RE = re.compile(r"</?think>", re.IGNORECASE)
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_LABEL_RE = re.compile(r"^\s*(megotchi|companion)\s*[:>]\s*", re.IGNORECASE)
_SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+")

# Phrases that mark a sentence as deliberation/meta, not speech. Drawn from
# real eval transcripts — extend as new tells appear.
_META_MARKERS = (
    "memory note", "the memory says", "from the memory", "my memory notes",
    "the instruction", "the rubric", "the user", "the child is", "the child's",
    "i need to answer", "i need to respond", "i need to check", "i must use",
    "let me check", "let me think", "let me review", "let me list",
    "the question is", "the response should", "the answer should",
    "the reply should", "output only", "1-3 short sentences", "short sentences",
    "as megotchi", "megotchi would say", "megotchi says", "megotchi should",
    "i'll craft", "possible response", "so the answer is", "step by step",
    "first, i", "wait,", "re-think", "to be precise", "to be concise",
    "the key point", "scanning through", "looking at the memory",
    "check the memory", "based on the memory", "in the memory",
    "never invent", "so i should say", "phrase it", "so the response",
    "let me write", "we are in the role", "i'll respond", "keep the response",
)
_PREAMBLE_RE = re.compile(
    r"^(okay|ok|alright|first|hmm+|so)\s*[,—-]\s*(i|the|let)", re.IGNORECASE)


def _is_meta(sentence: str) -> bool:
    low = sentence.lower()
    if _PREAMBLE_RE.match(low.strip()):
        return True
    return any(m in low for m in _META_MARKERS)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text) if s.strip()]


def _budget(text: str, max_sentences: int, max_chars: int = 420) -> str:
    sents = _sentences(text)[:max_sentences]
    out = " ".join(sents).strip()
    if len(out) > max_chars:
        out = out[:max_chars].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return out


def clean_speech(raw: str, max_sentences: int = 3,
                 fallback: str = FALLBACK) -> str:
    """Return only the delivered speech from a model reply, or `fallback`."""
    text = _THINK_RE.sub("", raw or "")
    if "</think>" in text.lower():
        text = _ORPHAN_CLOSE_RE.sub("", text, count=1)
    text = _ORPHAN_TAG_RE.sub("", text)
    text = _FENCE_RE.sub("", text)
    text = _LABEL_RE.sub("", text.strip()).strip()
    if not text:
        return fallback

    sents = _sentences(text)
    if not sents:
        return fallback

    # Drop an unterminated trailing fragment (mid-thought truncation).
    if len(sents) > 1 and not re.search(r"[.!?…\"']$", sents[-1]):
        sents = sents[:-1]

    meta_flags = [_is_meta(s) for s in sents]

    # Clean reply: nothing meta — pass through with budget only.
    if not any(meta_flags):
        return _budget(" ".join(sents), max_sentences) or fallback

    # Deliberation transcript: the actual speech, if any, is usually the last
    # contiguous run of non-meta sentences, or the last quoted candidate.
    blocks: list[list[str]] = []
    cur: list[str] = []
    for s, is_meta in zip(sents, meta_flags):
        if is_meta:
            if cur:
                blocks.append(cur)
                cur = []
        else:
            cur.append(s)
    if cur:
        blocks.append(cur)

    if blocks:
        candidate = " ".join(blocks[-1]).strip().strip('"“”')
        if len(candidate) >= 4:
            return _budget(candidate, max_sentences) or fallback

    # No clean block — mine the deliberation for its drafted speech.
    # Best signal: a quote introduced by "would say / response: / reply" —
    # that IS the intended utterance, even though its host sentence is meta.
    drafted = re.findall(
        r'(?:would say|should say|say something like|says?|'
        r'response(?: should be)?|reply)\s*[:,]?\s*["“]([^"”]{4,300})["”]',
        text, re.IGNORECASE)
    drafted = [q.strip() for q in drafted if not _is_meta(q)]
    if drafted:
        return _budget(drafted[-1], max_sentences) or fallback

    # Fall back to the last quote that isn't just an echoed question.
    quotes = [q.strip() for q in re.findall(r'["“]([^"”]{4,300})["”]', text)
              if not _is_meta(q)]
    statements = [q for q in quotes if not q.rstrip().endswith("?")]
    if statements:
        return _budget(statements[-1], max_sentences) or fallback
    if quotes:
        return _budget(quotes[-1], max_sentences) or fallback
    return fallback


def speak(llm, system: str, user: str, max_sentences: int = 3,
          fallback: str = FALLBACK) -> str:
    """Get a clean spoken reply via grammar-constrained JSON (F20).

    The model may reason freely in the (unspoken) `recall` field; the grammar
    makes it impossible for that reasoning to land in `reply`, which is all the
    child hears. Falls back to clean_speech() salvage only if the model returns
    no usable reply. Works for both providers: Ollama enforces the schema as a
    grammar; Anthropic follows it from the prompt.
    """
    raw = llm.complete(system + _JSON_HINT, user, want_json=True,
                       schema=REPLY_SCHEMA, max_tokens=600)
    try:
        reply = str(parse_json_block(raw).get("reply", "")).strip()
    except (ValueError, AttributeError):
        reply = ""
    if reply:
        return _budget(reply, max_sentences) or fallback
    # No structured reply — salvage from whatever came back.
    return clean_speech(raw, max_sentences, fallback)


# Expressions the avatar can render (see voice/web/index.html).
EXPRESSIONS = ("neutral", "happy", "excited", "curious", "gentle",
               "surprised", "sad")

REPLY_EXPRESSIVE_SCHEMA = {
    "type": "object",
    "properties": {
        "recall": {"type": "string"},
        "reply": {"type": "string"},
        "expression": {"type": "string", "enum": list(EXPRESSIONS)},
    },
    "required": ["reply", "expression"],
}

_EXPR_HINT = (
    "\n\nReply as JSON. Put any reasoning in \"recall\" (the child NEVER sees "
    "it). Put ONLY the words you say out loud — 1-3 short sentences — in "
    "\"reply\"; talk TO the child using \"you\", never as them. Put the feeling "
    "your FACE should show as you say it in \"expression\": one of neutral, "
    "happy, excited, curious, gentle, surprised, sad. Match it to the moment — "
    "gentle when they're upset, excited at good news, curious when asking.")


def speak_expressive(llm, system: str, user: str, max_sentences: int = 3,
                     fallback: str = FALLBACK) -> tuple[str, str]:
    """Like speak(), but also returns an `expression` for the avatar's face.
    Returns (reply, expression). Non-breaking: speak() is unchanged."""
    raw = llm.complete(system + _EXPR_HINT, user, want_json=True,
                       schema=REPLY_EXPRESSIVE_SCHEMA, max_tokens=600)
    reply, expr = "", "neutral"
    try:
        data = parse_json_block(raw)
        reply = str(data.get("reply", "")).strip()
        expr = str(data.get("expression", "neutral")).strip().lower()
    except (ValueError, AttributeError):
        pass
    if expr not in EXPRESSIONS:
        expr = "neutral"
    reply = _budget(reply, max_sentences) if reply else clean_speech(raw, max_sentences, fallback)
    return (reply or fallback), expr
