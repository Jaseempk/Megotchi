#!/usr/bin/env python3
"""B1 capture — listen on the watch's USB serial port, catch the hex-dumped
WAV, save + play it, and (optionally) send it through the live Megotchi brain.

Usage:
    python3 b1_capture.py [--port /dev/cu.usbmodem2101] [--send]

No pyserial needed — the CDC port behaves as a file on macOS.
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

URL = "https://megotchi-voice-production.up.railway.app/api/talk_device"
OUT = Path(__file__).parent / "captures"


def capture(port: str) -> bytes:
    print(f"listening on {port} — hold the watch screen, speak, release…")
    hex_lines: list[str] = []
    grabbing = False
    with open(port, "rb", buffering=0) as f:
        while True:
            line = f.readline().decode(errors="replace").strip()
            if not line:
                continue
            if line.startswith("[b1]"):
                print(" ", line)
            if line.startswith("---WAV-BEGIN"):
                grabbing, hex_lines = True, []
                print("  receiving…", end="", flush=True)
                continue
            if line.startswith("---WAV-END"):
                print(" done")
                return bytes.fromhex("".join(hex_lines))
            if grabbing:
                hex_lines.append(line)
                if len(hex_lines) % 500 == 0:
                    print(".", end="", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/cu.usbmodem2101")
    ap.add_argument("--send", action="store_true",
                    help="also send through the live Megotchi brain")
    ap.add_argument("--child", default="watchtest")
    args = ap.parse_args()

    wav = capture(args.port)
    OUT.mkdir(exist_ok=True)
    stamp = time.strftime("%H%M%S")
    path = OUT / f"rec_{stamp}.wav"
    path.write_bytes(wav)
    secs = max(0.0, (len(wav) - 44) / 32000)
    print(f"saved {path}  ({len(wav)} bytes, {secs:.1f}s)")

    print("playing back…")
    subprocess.run(["afplay", str(path)], check=False)

    if args.send:
        print("sending to Megotchi…")
        r = subprocess.run(
            ["curl", "-sS", "-m", "60", "-D", "-", "-o", str(OUT / "reply.pcm"),
             "-F", f"child={args.child}", "-F", f"secs={secs:.1f}",
             "-F", f"audio=@{path};filename=rec.wav", URL],
            capture_output=True, text=True)
        heard = reply = ""
        for h in r.stdout.splitlines():
            low = h.lower()
            if low.startswith("x-heard:"):
                heard = h.split(":", 1)[1].strip()
            if low.startswith("x-reply:"):
                reply = h.split(":", 1)[1].strip()
        from urllib.parse import unquote
        print(f"  heard: {unquote(heard)!r}")
        print(f"  reply: {unquote(reply)!r}")
        pcm = OUT / "reply.pcm"
        if pcm.exists() and pcm.stat().st_size > 1000:
            # raw 16k mono s16le → wav header so afplay can play it
            import struct
            data = pcm.read_bytes()
            hdr = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
                   + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
                   + b"data" + struct.pack("<I", len(data)))
            (OUT / "reply.wav").write_bytes(hdr + data)
            print("playing Megotchi's reply…")
            subprocess.run(["afplay", str(OUT / "reply.wav")], check=False)


if __name__ == "__main__":
    main()
