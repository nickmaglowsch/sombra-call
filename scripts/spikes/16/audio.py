#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []  # stdlib only; needs pipewire-bin (pw-record/pw-play) or pulseaudio-utils
# ///
"""Spike #16, question 3: is the PipeWire sink monitor a clean OUTROS channel next to the mic?

Records the default mic and the default sink's monitor at the same time, at 16 kHz mono
(what whisper.cpp wants), through four phases:

* ``silence``  nothing plays, you stay quiet             -> noise floor of both channels
* ``playback`` a generated test signal plays, you stay quiet -> how much system audio leaks
               into the mic (acoustic echo on speakers, ~none on headphones)
* ``speech``   you read the sentence shown, nothing plays -> how much of you leaks into
               the monitor (should be none: the monitor carries only what apps play)
* ``both``     playback and speech together (double talk)

For each phase it logs RMS/peak in dBFS per channel and the separation in dB. Run it once
with headphones and once with laptop speakers (``--output``). WAVs stay in ``logs/`` (they
contain your voice; they are git-ignored).
"""

from __future__ import annotations

import argparse
import array
import math
import shutil
import signal
import subprocess
import sys
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Any

from common import LOG_DIR, JsonlLog, environment, run_text

RATE = 16_000
SENTENCE = "Nick, o que você acha desse gráfico de receita do terceiro trimestre?"
PHASES = ("silence", "playback", "speech", "both")


def write_test_signal(path: Path, seconds: float, rate: int = 48_000) -> None:
    """Tone bursts at 440/1000/2500 Hz plus a log sweep, -12 dBFS, mono s16."""
    amp = 0.25 * 32767
    samples = array.array("h")
    total = int(seconds * rate)
    for i in range(total):
        t = i / rate
        second = int(t) % 4
        if second < 3:  # bursts: 0.5 s on / 0.5 s off
            freq = (440.0, 1000.0, 2500.0)[second]
            value = math.sin(2 * math.pi * freq * t) if (t % 1.0) < 0.5 else 0.0
        else:  # 1 s log sweep 200 Hz -> 6 kHz
            u = t % 1.0
            k = math.log(6000 / 200)
            value = math.sin(2 * math.pi * 200 * (math.exp(k * u) - 1) / k)
        samples.append(int(amp * value))
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(samples.tobytes())


def level(path: Path, skip_s: float = 0.5) -> dict[str, Any]:
    """RMS and peak in dBFS of a mono s16 WAV, ignoring the first ``skip_s`` seconds."""
    if not path.exists() or path.stat().st_size <= 44:
        return {"error": "empty or missing"}
    with wave.open(str(path), "rb") as wav:
        rate, channels, width = wav.getframerate(), wav.getnchannels(), wav.getsampwidth()
        frames = wav.readframes(wav.getnframes())
    if width != 2:
        return {"error": f"sample width {width}"}
    data = array.array("h")
    data.frombytes(frames)
    data = data[int(skip_s * rate) * channels :]
    if not data:
        return {"error": "too short"}
    rms = math.sqrt(sum(x * x for x in data) / len(data))
    peak = max(abs(x) for x in data)
    return {
        "rate": rate,
        "channels": channels,
        "seconds": round(len(data) / rate / channels + skip_s, 2),
        "rms_dbfs": _db(rms),
        "peak_dbfs": _db(peak),
        "clipped": peak >= 32767,
    }


def _db(value: float) -> float:
    return round(20 * math.log10(value / 32768), 1) if value > 0 else -120.0


def record_cmds(backend: str, source: str, sink: str, mic: Path, mon: Path) -> list[list[str]]:
    if backend == "pw":
        common = ["--rate", str(RATE), "--channels", "1", "--format", "s16"]
        return [
            ["pw-record", *common, "--target", source, str(mic)],
            # stream.capture.sink=true makes a capture stream read a sink's monitor ports.
            [
                "pw-record",
                *common,
                "-P",
                "{ stream.capture.sink=true }",
                "--target",
                sink,
                str(mon),
            ],
        ]
    common = [f"--rate={RATE}", "--channels=1", "--format=s16le", "--file-format=wav"]
    return [
        ["parecord", *common, f"--device={source}", str(mic)],
        ["parecord", *common, f"--device={sink}.monitor", str(mon)],
    ]


def spawn(cmd: list[str]) -> subprocess.Popen[bytes]:
    exe = shutil.which(cmd[0])
    if exe is None:
        raise SystemExit(f"{cmd[0]} not found; see README.md for the apt install line")
    # S603: fixed tool names; device names come from pactl on this machine.
    return subprocess.Popen(  # noqa: S603
        [exe, *cmd[1:]], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )


def run_phase(
    phase: str, args: argparse.Namespace, out_dir: Path, test_wav: Path, log: JsonlLog
) -> None:
    mic, mon = out_dir / f"{phase}-mic.wav", out_dir / f"{phase}-monitor.wav"
    instructions = {
        "silence": "stay quiet",
        "playback": "stay quiet while the test signal plays",
        "speech": f"read aloud, at normal volume: “{SENTENCE}”",
        "both": f"read aloud while the test signal plays: “{SENTENCE}”",
    }[phase]
    input(f"\nphase {phase}: press Enter, then {instructions} ")
    recorders = [spawn(c) for c in record_cmds(args.backend, args.source, args.sink, mic, mon)]
    time.sleep(0.5)
    started = time.monotonic()
    if phase in ("playback", "both"):
        player = spawn(["pw-play" if args.backend == "pw" else "paplay", str(test_wav)])
        player.wait(timeout=args.seconds + 10)
    else:
        time.sleep(args.seconds)
    wall = time.monotonic() - started + 0.5
    errors = []
    for proc in recorders:
        proc.send_signal(signal.SIGINT)
        try:
            _, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, err = proc.communicate()
        if err.strip():
            errors.append(err.decode(errors="replace").strip()[:300])
    mic_level, mon_level = level(mic), level(mon)
    fields: dict[str, Any] = {"phase": phase, "wall_s": round(wall, 2), "mic": mic_level}
    fields["monitor"] = mon_level
    if "rms_dbfs" in mic_level and "rms_dbfs" in mon_level:
        # playback: how far below the monitor the mic sits (echo leak); speech: the reverse.
        diff = round(mon_level["rms_dbfs"] - mic_level["rms_dbfs"], 1)
        fields["separation_db"] = {"playback": diff, "speech": -diff}.get(phase)
    if errors:
        fields["recorder_stderr"] = errors
    log.event("phase", **fields)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--backend", choices=["pw", "pulse"], default="pw")
    parser.add_argument("--seconds", type=float, default=8.0, help="length of each phase")
    parser.add_argument("--output", choices=["headphones", "speakers"], required=True)
    parser.add_argument("--source", help="mic source name (default: pactl get-default-source)")
    parser.add_argument("--sink", help="output sink name (default: pactl get-default-sink)")
    parser.add_argument("--phases", default=",".join(PHASES))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.source = args.source or run_text(["pactl", "get-default-source"])
    args.sink = args.sink or run_text(["pactl", "get-default-sink"])
    log = JsonlLog(f"audio-{args.output}")
    log.event("environment", **environment())
    log.event(
        "devices",
        backend=args.backend,
        output=args.output,
        source=args.source,
        sink=args.sink,
        pactl_info=run_text(["pactl", "info"]).splitlines(),
        sources=run_text(["pactl", "list", "short", "sources"]).splitlines(),
        echo_cancel_loaded="echo-cancel" in run_text(["pactl", "list", "short", "modules"]),
    )
    out_dir = LOG_DIR / f"audio-{args.output}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    test_wav = out_dir / "test-signal.wav"
    write_test_signal(test_wav, args.seconds)
    print("set the output volume to a normal listening level before starting")
    for phase in [p.strip() for p in args.phases.split(",") if p.strip()]:
        run_phase(phase, args, out_dir, test_wav, log)
    log.event("summary", out_dir=str(out_dir), cpu_percent=log.cpu_percent())
    log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
