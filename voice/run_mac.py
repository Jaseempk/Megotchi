"""Talk to Megotchi on your Mac — push-to-talk stand-in for the toy.

The Mac's mic/speaker replace the ESP32's until firmware exists; the pipeline
and memory engine are exactly what the device will use.

Setup:
    pip install -r requirements-voice.txt
    export OPENAI_API_KEY=sk-...        # STT + LLM
    export ELEVENLABS_API_KEY=...       # TTS

Run (from the megotchi/ directory):
    python -m voice.run_mac --child maggie

In the loop: press Enter to start talking, Enter again to stop. It transcribes,
answers from memory, and speaks back. Commands: /reflect (nightly consolidation
+ PROFILE.md), /profile (show it), /quit.
"""
from __future__ import annotations

import argparse
import os
import queue
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import sounddevice as sd
import soundfile as sf

from voice.pipeline import VoicePipeline

SR = 16000
DATA_DIR = Path(os.environ.get(
    "MEGOTCHI_DATA", Path(__file__).resolve().parents[1] / "data"))


def record_until_enter():
    q: queue.Queue = queue.Queue()

    def cb(indata, frames, time_info, status):
        q.put(indata.copy())

    print("  🎙  recording… press Enter to stop")
    with sd.InputStream(samplerate=SR, channels=1, dtype="int16", callback=cb):
        input()
    frames = []
    while not q.empty():
        frames.append(q.get())
    if not frames:
        return None
    return np.concatenate(frames, axis=0)


def main() -> None:
    ap = argparse.ArgumentParser(description="Megotchi voice — Mac push-to-talk")
    ap.add_argument("--child", required=True, help="child name, e.g. maggie")
    ap.add_argument("--llm", default=None, help="e.g. openai:gpt-4o-mini")
    ap.add_argument("--stt", default=None, help="e.g. openai:gpt-4o-mini-transcribe")
    ap.add_argument("--tts", default=None, help="e.g. elevenlabs:<voice_id>")
    args = ap.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    pipe = VoicePipeline(args.child, DATA_DIR / "megotchi.db",
                         llm_spec=args.llm, stt_spec=args.stt, tts_spec=args.tts,
                         data_dir=DATA_DIR)
    print(f"Megotchi voice · child={args.child} · "
          f"llm={pipe.llm.provider}:{pipe.llm.model} · "
          f"stt={pipe.stt.provider}:{pipe.stt.model} · tts={pipe.tts.provider}")
    print("Enter = push-to-talk · /reflect · /profile · /quit\n")

    while True:
        try:
            cmd = input(f"{args.child}> [Enter to talk] ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if cmd == "/quit":
            break
        if cmd == "/reflect":
            pipe.reflect()
            print("  · reflected — PROFILE.md updated\n")
            continue
        if cmd == "/profile":
            p = DATA_DIR / "memory" / args.child / "PROFILE.md"
            print(p.read_text() if p.exists() else "  (no profile yet — /reflect first)")
            continue

        audio = record_until_enter()
        if audio is None or len(audio) < SR // 3:
            print("  (nothing heard)\n")
            continue

        wav_path = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
        sf.write(wav_path, audio, SR)
        heard = pipe.transcribe(wav_path)
        print(f"  heard   : {heard}")
        reply = pipe.respond(heard)
        print(f"  megotchi: {reply}")

        mp3 = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False).name
        pipe.speak_to_file(reply, mp3)
        subprocess.run(["afplay", mp3])

        # memory write AFTER playback — keeps the spoken reply snappy
        for line in pipe.learn(heard, reply):
            print(f"  · memory: {line}")
        print()
        for f in (wav_path, mp3):
            try:
                os.unlink(f)
            except OSError:
                pass

    pipe.close()


if __name__ == "__main__":
    main()
