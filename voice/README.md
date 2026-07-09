# voice/ — the talking-toy cascade (Phase A)

Turns the `megotchi_memory` engine into a voice companion:

```
audio ─▶ STT (gpt-4o-mini-transcribe) ─▶ retrieve memory ─▶ speak() (OpenAI) ─▶ TTS (ElevenLabs) ─▶ audio
                                                │
                                                └▶ after playback: update_memory + nightly reflect
```

| file | role |
|---|---|
| `stt.py` | STT adapter (`MEGOTCHI_STT`, default `openai:gpt-4o-mini-transcribe`) |
| `tts.py` | TTS adapter (`MEGOTCHI_TTS`, default `elevenlabs:<voice>`) — **swap before launch** (ToS) |
| `pipeline.py` | `VoicePipeline`: transcribe → respond → speak_to_file / learn / reflect |
| `run_mac.py` | push-to-talk loop — talk to it on the Mac (stand-in for the toy) |
| `server.py` | FastAPI web/phone server — serves the avatar UI + `/api/talk` |
| `web/index.html` | the mobile avatar app — Megotchi face with expressions + lip-sync |

## Web / phone app (talk to the avatar from any phone)

```bash
pip install -r requirements-voice.txt
export OPENAI_API_KEY=sk-...        # STT + LLM
export ELEVENLABS_API_KEY=...       # TTS
uvicorn voice.server:app --host 0.0.0.0 --port 8000
```
Open `http://localhost:8000` on the Mac to test. **For a real phone, mic access
needs HTTPS** — expose it with a tunnel:
```bash
cloudflared tunnel --url http://localhost:8000     # or: ngrok http 8000
```
…then open the `https://…` link on any phone. Enter a name (that's the child /
memory key), **hold** the mic button to talk, release to send. The Megotchi face
listens, thinks, and speaks back with lip-sync + a content-driven expression.
"🌙 tuck in" runs nightly reflection. Memory is shared with the Mac loop and the
eval suite — one brain.

## Run it (from `megotchi/`)

```bash
brew install portaudio                 # sounddevice needs this on macOS
pip install -r requirements-voice.txt
export OPENAI_API_KEY=sk-...            # STT + LLM
export ELEVENLABS_API_KEY=...          # TTS
python -m voice.run_mac --child maggie
```

Press **Enter** to start talking, **Enter** again to stop. It transcribes,
answers from memory, speaks back, then learns. Commands: `/reflect`,
`/profile`, `/quit`.

## Prove the moat (the demo that matters)

1. Session 1: tell it something — "my hamster Biscuit died today."
2. `/reflect`, then `/quit`.
3. Session 2 (`python -m voice.run_mac --child maggie` again): ask "do I have
   any pets?" — it should recall Biscuit, in the past tense.

Same memory DB (`data/megotchi.db`) and `PROFILE.md` as the text REPL
(`companion/run.py`) and the eval suite — one brain, three front-ends.

## Notes
- Provider-agnostic: `--llm openai:gpt-4o` / `--stt ...` / `--tts elevenlabs:<voice_id>`.
- Memory writes run **after** playback, so the spoken reply stays snappy.
- Untested end-to-end here (needs your API keys + a mic); the wiring compiles
  and imports clean. Report back what the latency + voice feel like.
