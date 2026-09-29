# /// script
# requires-python = ">=3.12"
# dependencies = ["psutil>=5.9"]
# ///
"""Spike #15: whisper.cpp real-time factor and PT-BR accuracy on reference hardware.

One command, on the machine being measured:

    uv run scripts/spikes/15/bench_whisper.py --label mac-m2-16gb

What it does, all cached under ``--work-dir`` (default ``~/.cache/sombra-spike15``):

1. Clones whisper.cpp at a pinned tag and builds ``whisper-server`` (Metal on macOS by
   default, CUDA with ``--cuda``).
2. Downloads the ggml models under test from Hugging Face (``ggerganov/whisper.cpp``).
3. Fetches PT-BR clips from FLEURS (CC BY 4.0) by streaming only the start of the test
   archive, and builds 2 PT→EN mixed clips from FLEURS ``pt_br`` + ``en_us``.
4. For each model: starts ``whisper-server`` once (model load excluded from timings),
   transcribes every full clip (RTF, WER, proper-name error), then 3/5/7/10 s segments
   (per-utterance latency), while sampling peak RSS, CPU and GPU utilisation.
5. Writes ``results/<label>-<timestamp>.json`` and a Markdown table ready to paste into
   ``docs/spikes/15-whisper-rtf.md``.

Throwaway spike code: exempt from coverage, but must pass lint. See the report for the
method, the metric definitions and the licenses of the audio.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import statistics
import subprocess
import sys
import tarfile
import threading
import time
import unicodedata
import urllib.request
import uuid
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import psutil

WHISPER_CPP_REPO = "https://github.com/ggml-org/whisper.cpp"
WHISPER_CPP_TAG = "v1.9.4"
HF_MODELS = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"
FLEURS = "https://huggingface.co/datasets/google/fleurs/resolve/main/data"

# The issue asks for large-v3-turbo q5_0/q8_0, medium q5 and small q5. whisper.cpp
# publishes small only as q5_1, so that is the "small q5" measured here.
DEFAULT_MODELS = ["large-v3-turbo-q5_0", "large-v3-turbo-q8_0", "medium-q5_0", "small-q5_1"]
SEGMENT_SECONDS = [3.0, 5.0, 7.0, 10.0]
SAMPLE_RATE = 16_000


# --------------------------------------------------------------------------- data


@dataclass
class Clip:
    """One reference utterance: a 16 kHz mono 16-bit WAV plus its reference text."""

    clip_id: str
    path: Path
    text: str
    kind: str  # "pt" or "mixed"
    source: str
    license: str
    names: list[str] = field(default_factory=list)
    duration_s: float = 0.0


@dataclass
class ClipResult:
    clip_id: str
    kind: str
    language: str
    duration_s: float
    compute_s: float
    rtf: float
    wer: float
    ref_words: int
    word_errors: int
    names_total: int
    names_missed: int
    hypothesis: str


@dataclass
class ModelResult:
    model: str
    model_sha256: str
    model_mb: float
    load_s: float
    clips: list[ClipResult] = field(default_factory=list)
    segments: dict[str, list[float]] = field(default_factory=dict)
    peak_rss_mb: float = 0.0
    cpu_percent_of_machine: float = 0.0
    cpu_cores_busy: float = 0.0
    gpu_util_mean: float | None = None
    gpu_util_max: float | None = None
    gpu_mem_peak_mb: float | None = None
    error: str | None = None


# ------------------------------------------------------------------ text + WER


def normalize(text: str) -> list[str]:
    """Lowercase, NFC, drop punctuation, keep accents and digits. Returns tokens."""
    text = unicodedata.normalize("NFC", text).lower()
    text = re.sub(r"[^\w\s]|_", " ", text)
    return text.split()


def edit_distance(ref: list[str], hyp: list[str]) -> int:
    """Word-level Levenshtein distance (substitutions + deletions + insertions)."""
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return prev[-1]


def proper_names(raw: str) -> list[str]:
    """Capitalised words that do not start a sentence: a cheap proper-name proxy.

    FLEURS sentences come from Wikipedia, so they carry many names (people, places,
    organisations). Sentence-initial words are skipped because they are capitalised anyway.
    """
    names: list[str] = []
    for sentence in re.split(r"[.!?]\s+", raw):
        words = sentence.split()
        for word in words[1:]:
            clean = re.sub(r"[^\w-]", "", word)
            if clean[:1].isupper() and not clean.isdigit():
                names.extend(normalize(clean))
    return names


def names_missed(names: list[str], hyp_tokens: list[str]) -> int:
    pool = list(hyp_tokens)
    missed = 0
    for name in names:
        if name in pool:
            pool.remove(name)
        else:
            missed += 1
    return missed


# ------------------------------------------------------------------- shell/net


def log(msg: str) -> None:
    print(f"[bench] {msg}", file=sys.stderr, flush=True)


def run(cmd: list[str], cwd: Path | None = None) -> str:
    log("$ " + " ".join(cmd))
    # Fixed argument lists built by this script, never shell strings.
    out = subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)  # noqa: S603
    return out.stdout


def download(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    log(f"downloading {url}")
    # URLs are the fixed https constants above.
    with urllib.request.urlopen(url, timeout=60) as resp, tmp.open("wb") as fh:  # noqa: S310
        shutil.copyfileobj(resp, fh, length=1 << 20)
    tmp.replace(dest)
    return dest


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------- build/models


def build_whisper(work: Path, cuda: bool, tag: str) -> tuple[Path, str]:
    src = work / f"whisper.cpp-{tag}"
    build = src / ("build-cuda" if cuda else "build")
    server = build / "bin" / "whisper-server"
    if not src.exists():
        run(["git", "clone", "--depth", "1", "--branch", tag, WHISPER_CPP_REPO, str(src)])
    commit = run(["git", "rev-parse", "HEAD"], cwd=src).strip()
    if not server.exists():
        cfg = ["cmake", "-S", str(src), "-B", str(build), "-DCMAKE_BUILD_TYPE=Release"]
        if cuda:
            cfg.append("-DGGML_CUDA=1")
        run(cfg)
        jobs = str(os.cpu_count() or 4)
        run(["cmake", "--build", str(build), "-j", jobs, "--target", "whisper-server"])
    return server, commit


def fetch_model(work: Path, name: str, local: dict[str, Path]) -> Path:
    if name in local:
        return local[name]
    return download(f"{HF_MODELS}/ggml-{name}.bin", work / "models" / f"ggml-{name}.bin")


# ----------------------------------------------------------------------- audio


def read_wav(path: Path) -> bytes:
    """Return raw 16 kHz mono s16le frames, converting with ffmpeg if needed."""
    with contextlib.suppress(wave.Error, EOFError), wave.open(str(path)) as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (SAMPLE_RATE, 1, 2):
            return w.readframes(w.getnframes())
    if not shutil.which("ffmpeg"):
        raise SystemExit(f"{path} is not 16 kHz mono s16 and ffmpeg is not installed")
    conv = path.with_suffix(".16k.wav")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-ar", "16000", "-ac", "1",
         "-sample_fmt", "s16", str(conv)])  # fmt: skip
    with wave.open(str(conv)) as w:
        return w.readframes(w.getnframes())


def write_wav(path: Path, frames: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(frames)


def seconds(frames: bytes) -> float:
    return len(frames) / 2 / SAMPLE_RATE


def fleurs_subset(work: Path, lang: str, count: int) -> list[Clip]:
    """Stream the FLEURS test archive and keep the first ``count`` distinct sentences.

    Only the start of the ~GB archive is downloaded: the stream stops once enough
    clips are found. Clips are re-encoded to 16 kHz mono s16 WAV under ``work``.
    """
    out_dir = work / "clips" / f"fleurs-{lang}"
    manifest = out_dir / f"manifest-{count}.json"
    if manifest.exists():
        rows = json.loads(manifest.read_text(encoding="utf-8"))
        return [Clip(**{**r, "path": Path(r["path"])}) for r in rows]
    tsv = download(f"{FLEURS}/{lang}/test.tsv", out_dir / "test.tsv")
    refs: dict[str, str] = {}
    for line in tsv.read_text(encoding="utf-8").splitlines():
        cols = line.split("\t")
        if len(cols) >= 3:
            refs[cols[1]] = cols[2]  # file_name -> raw transcription (keeps casing)
    clips: list[Clip] = []
    seen_text: set[str] = set()
    url = f"{FLEURS}/{lang}/audio/test.tar.gz"
    log(f"streaming {url} (stops after {count} clips)")
    with (
        urllib.request.urlopen(url, timeout=60) as resp,  # noqa: S310 - fixed https constant
        tarfile.open(fileobj=resp, mode="r|gz") as tar,
    ):
        for member in tar:
            name = Path(member.name).name
            if not member.isfile() or name not in refs or refs[name] in seen_text:
                continue
            raw_path = out_dir / "raw" / name
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            fh = tar.extractfile(member)
            if fh is None:
                continue
            raw_path.write_bytes(fh.read())
            frames = read_wav(raw_path)
            wav = out_dir / f"{Path(name).stem}.wav"
            write_wav(wav, frames)
            seen_text.add(refs[name])
            clips.append(
                Clip(
                    clip_id=f"fleurs-{lang}-{Path(name).stem}",
                    path=wav,
                    text=refs[name],
                    kind="pt" if lang.startswith("pt") else "en",
                    source=f"google/fleurs {lang} test/{name}",
                    license="CC BY 4.0",
                    names=proper_names(refs[name]),
                    duration_s=seconds(frames),
                )
            )
            if len(clips) >= count:
                break
    manifest.write_text(
        json.dumps([{**asdict(c), "path": str(c.path)} for c in clips], ensure_ascii=False),
        encoding="utf-8",
    )
    return clips


def mixed_clips(work: Path, pt: list[Clip], en: list[Clip]) -> list[Clip]:
    """PT sentence, 0.4 s silence, EN sentence: language switch between sentences.

    This is not intra-sentence code-switching ("vamos fazer o deploy na sprint"); that
    needs a consented human recording, see ``--local-manifest``.
    """
    silence = b"\x00\x00" * int(0.4 * SAMPLE_RATE)
    clips = []
    for i, (p, e) in enumerate(zip(pt[-2:], en[:2], strict=False)):
        frames = read_wav(p.path) + silence + read_wav(e.path)
        path = work / "clips" / "mixed" / f"mixed-{i}.wav"
        write_wav(path, frames)
        clips.append(
            Clip(
                clip_id=f"mixed-{i}",
                path=path,
                text=f"{p.text} {e.text}",
                kind="mixed",
                source=f"{p.source} + {e.source}",
                license="CC BY 4.0",
                names=p.names + e.names,
                duration_s=seconds(frames),
            )
        )
    return clips


def local_clips(manifest: Path) -> list[Clip]:
    """Consented recordings supplied by a human. See the report for the format."""
    rows = json.loads(manifest.read_text(encoding="utf-8"))
    clips = []
    for row in rows:
        path = (manifest.parent / row["path"]).resolve()
        frames = read_wav(path)
        wav = path.with_suffix(".bench16k.wav")
        write_wav(wav, frames)
        clips.append(
            Clip(
                clip_id=row.get("id", path.stem),
                path=wav,
                text=row["text"],
                kind=row.get("kind", "pt"),
                source=str(path),
                license=row.get("license", "consented recording, not redistributed"),
                names=[t for n in row.get("names", []) for t in normalize(n)]
                or proper_names(row["text"]),
                duration_s=seconds(frames),
            )
        )
    return clips


def segments(clips: list[Clip], work: Path) -> dict[float, list[Path]]:
    """Cut 3/5/7/10 s utterances from the start of every clip long enough for them."""
    out: dict[float, list[Path]] = {s: [] for s in SEGMENT_SECONDS}
    for clip in clips:
        frames = read_wav(clip.path)
        for s in SEGMENT_SECONDS:
            n = int(s * SAMPLE_RATE) * 2
            if len(frames) >= n:
                path = work / "clips" / "segments" / f"{clip.clip_id}-{int(s)}s.wav"
                if not path.exists():
                    write_wav(path, frames[:n])
                out[s].append(path)
    return out


# ------------------------------------------------------------------ monitoring


class Monitor:
    """Samples the server's RSS and, when available, GPU utilisation every 100 ms."""

    def __init__(self, proc: psutil.Process) -> None:
        self.proc = proc
        self.peak_rss = 0
        self.gpu_util: list[float] = []
        self.gpu_mem: list[float] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._gpu = gpu_probe()

    def __enter__(self) -> Monitor:
        self.cpu0 = self.proc.cpu_times()
        self.t0 = time.perf_counter()
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join()
        cpu1 = self.proc.cpu_times()
        wall = time.perf_counter() - self.t0
        busy = (cpu1.user - self.cpu0.user) + (cpu1.system - self.cpu0.system)
        self.cores_busy = busy / wall if wall else 0.0
        self.cpu_percent = 100 * self.cores_busy / (psutil.cpu_count() or 1)

    def _loop(self) -> None:
        tick = 0
        while not self._stop.wait(0.1):
            with contextlib.suppress(psutil.Error):
                self.peak_rss = max(self.peak_rss, self.proc.memory_info().rss)
            tick += 1
            if self._gpu and tick % 5 == 0:  # GPU tools are slower: every 0.5 s
                sample = self._gpu()
                if sample:
                    self.gpu_util.append(sample[0])
                    if sample[1] is not None:
                        self.gpu_mem.append(sample[1])


def gpu_probe() -> Any:
    """Return a sampler giving (util %, mem MB | None), or None if no GPU tool exists."""
    if shutil.which("nvidia-smi"):

        def nvidia() -> tuple[float, float | None] | None:
            try:
                out = subprocess.run(
                    ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used",  # noqa: S607
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=5, check=True,
                ).stdout.splitlines()[0]  # fmt: skip
                util, mem = (float(x) for x in out.split(","))
                return util, mem
            except (OSError, subprocess.SubprocessError, ValueError, IndexError):
                return None

        return nvidia
    if sys.platform == "darwin" and shutil.which("ioreg"):

        def apple() -> tuple[float, float | None] | None:
            # IOAccelerator exposes "Device Utilization %" without sudo.
            try:
                out = subprocess.run(
                    ["ioreg", "-r", "-d", "1", "-c", "IOAccelerator"],  # noqa: S607
                    capture_output=True, text=True, timeout=5, check=True,
                ).stdout  # fmt: skip
            except (OSError, subprocess.SubprocessError):
                return None
            m = re.search(r'"Device Utilization %"=(\d+)', out)
            return (float(m.group(1)), None) if m else None

        return apple
    return None


# ---------------------------------------------------------------------- server


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def post_wav(port: int, wav: Path, language: str) -> tuple[str, float]:
    """POST one WAV to /inference. Returns (text, seconds from send to full response)."""
    boundary = uuid.uuid4().hex
    fields = {"response_format": "json", "temperature": "0.0", "language": language}
    body = b""
    for key, value in fields.items():
        body += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'
        ).encode()
    body += (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{wav.name}"\r\n'
        "Content-Type: audio/wav\r\n\r\n"
    ).encode()
    body += wav.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/inference",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=600) as resp:  # noqa: S310 - local server
        payload = json.loads(resp.read())
    return str(payload.get("text", "")).strip(), time.perf_counter() - t0


def start_server(
    server: Path, model: Path, threads: int, extra: list[str]
) -> tuple[subprocess.Popen[bytes], int, float]:
    port = free_port()
    cmd = [str(server), "-m", str(model), "-t", str(threads), "-l", "pt",
           "--host", "127.0.0.1", "--port", str(port), *extra]  # fmt: skip
    log("$ " + " ".join(cmd))
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)  # noqa: S603
    deadline = t0 + 600
    while time.perf_counter() < deadline:
        if proc.poll() is not None:
            err = proc.stderr.read().decode(errors="replace")[-2000:] if proc.stderr else ""
            raise RuntimeError(f"whisper-server exited: {err}")
        try:
            health = f"http://127.0.0.1:{port}/health"
            with urllib.request.urlopen(health, timeout=2) as resp:
                if resp.status == 200:
                    return proc, port, time.perf_counter() - t0
        except OSError:
            time.sleep(0.2)
    proc.kill()
    raise RuntimeError("whisper-server did not become healthy in 600 s")


def bench_model(
    name: str,
    model: Path,
    server: Path,
    clips: list[Clip],
    segs: dict[float, list[Path]],
    threads: int,
    extra: list[str],
) -> ModelResult:
    result = ModelResult(
        model=name,
        model_sha256=sha256(model),
        model_mb=round(model.stat().st_size / 1e6, 1),
        load_s=0.0,
    )
    try:
        proc, port, result.load_s = start_server(server, model, threads, extra)
    except RuntimeError as exc:
        result.error = str(exc)
        log(f"{name}: {exc}")
        return result
    try:
        # Stream stderr away so the pipe never fills and blocks the server.
        threading.Thread(target=lambda: proc.stderr and proc.stderr.read(), daemon=True).start()
        post_wav(port, clips[0].path, "pt")  # warm-up: first call allocates buffers
        with Monitor(psutil.Process(proc.pid)) as mon:
            for clip in clips:
                langs = ["pt", "auto"] if clip.kind == "mixed" else ["pt"]
                for lang in langs:
                    hyp, secs = post_wav(port, clip.path, lang)
                    ref_tokens, hyp_tokens = normalize(clip.text), normalize(hyp)
                    errors = edit_distance(ref_tokens, hyp_tokens)
                    result.clips.append(
                        ClipResult(
                            clip_id=clip.clip_id,
                            kind=clip.kind,
                            language=lang,
                            duration_s=round(clip.duration_s, 2),
                            compute_s=round(secs, 3),
                            rtf=round(secs / clip.duration_s, 4),
                            wer=round(errors / max(len(ref_tokens), 1), 4),
                            ref_words=len(ref_tokens),
                            word_errors=errors,
                            names_total=len(clip.names),
                            names_missed=names_missed(clip.names, hyp_tokens),
                            hypothesis=hyp,
                        )
                    )
                    log(f"{name} {clip.clip_id} [{lang}] rtf={secs / clip.duration_s:.3f}")
            for s, paths in segs.items():
                result.segments[f"{int(s)}s"] = [
                    round(post_wav(port, p, "pt")[1], 3) for p in paths
                ]
        result.peak_rss_mb = round(mon.peak_rss / 1e6, 1)
        result.cpu_cores_busy = round(mon.cores_busy, 2)
        result.cpu_percent_of_machine = round(mon.cpu_percent, 1)
        if mon.gpu_util:
            result.gpu_util_mean = round(statistics.fmean(mon.gpu_util), 1)
            result.gpu_util_max = max(mon.gpu_util)
        if mon.gpu_mem:
            result.gpu_mem_peak_mb = max(mon.gpu_mem)
    except (OSError, RuntimeError, ValueError) as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        log(f"{name}: {result.error}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
    return result


# --------------------------------------------------------------------- reports


def machine_info(threads: int, cuda: bool, extra: list[str]) -> dict[str, Any]:
    cpu = platform.processor() or platform.machine()
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        if sys.platform == "darwin":
            cpu = run(["sysctl", "-n", "machdep.cpu.brand_string"]).strip()
        else:
            info = Path("/proc/cpuinfo").read_text(encoding="utf-8")
            m = re.search(r"model name\s*:\s*(.+)", info)
            cpu = m.group(1).strip() if m else cpu
    gpu = "none detected"
    if shutil.which("nvidia-smi"):
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            gpu = run(["nvidia-smi", "-L"]).strip()
    elif sys.platform == "darwin":
        gpu = "Apple GPU (Metal)"
    return {
        "os": platform.platform(),
        "cpu": cpu,
        "logical_cpus": psutil.cpu_count(),
        "physical_cpus": psutil.cpu_count(logical=False),
        "ram_gb": round(psutil.virtual_memory().total / 2**30, 1),
        "gpu": gpu,
        "threads": threads,
        "cuda_build": cuda,
        "server_args": extra,
        "python": platform.python_version(),
    }


def pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def summarize(r: ModelResult) -> dict[str, Any]:
    pt = [c for c in r.clips if c.kind == "pt"]
    mixed_pt = [c for c in r.clips if c.kind == "mixed" and c.language == "pt"]
    mixed_auto = [c for c in r.clips if c.kind == "mixed" and c.language == "auto"]

    def wer(cs: list[ClipResult]) -> float:
        words = sum(c.ref_words for c in cs)
        return sum(c.word_errors for c in cs) / words if words else float("nan")

    names = sum(c.names_total for c in pt)
    audio = sum(c.duration_s for c in pt)
    return {
        "model": r.model,
        "error": r.error,
        "rtf": sum(c.compute_s for c in pt) / audio if audio else float("nan"),
        "rtf_p95": pct([c.rtf for c in pt], 0.95),
        "wer_pt": wer(pt),
        "name_err": sum(c.names_missed for c in pt) / names if names else float("nan"),
        "wer_mixed_pt": wer(mixed_pt),
        "wer_mixed_auto": wer(mixed_auto),
        "lat": {k: (pct(v, 0.5), pct(v, 0.95)) for k, v in r.segments.items()},
        "load_s": r.load_s,
        "peak_rss_mb": r.peak_rss_mb,
        "cpu": r.cpu_percent_of_machine,
        "cores": r.cpu_cores_busy,
        "gpu": r.gpu_util_mean,
        "gpu_mem": r.gpu_mem_peak_mb,
        "audio_s": audio,
        "clips": len(pt),
    }


def markdown(label: str, info: dict[str, Any], results: list[ModelResult]) -> str:
    def f(x: float | None, spec: str) -> str:
        return "n/a" if x is None or x != x else format(x, spec)

    def pc(x: float, spec: str) -> str:
        return "n/a" if x != x else format(x * 100, spec) + "%"

    lines = [
        f"### {label}",
        "",
        f"{info['cpu']}, {info['logical_cpus']} threads, {info['ram_gb']} GB RAM, "
        f"GPU: {info['gpu']}. {info['os']}. whisper.cpp {info.get('whisper_cpp', '?')}, "
        f"`-t {info['threads']}`{' (CUDA build)' if info['cuda_build'] else ''}"
        f"{' ' + ' '.join(info['server_args']) if info['server_args'] else ''}.",
        "",
        "| Model | RTF | RTF p95 | WER PT | Name err | WER mixed (pt / auto) "
        "| Latency 3 s | 5 s | 7 s | 10 s (p50/p95, s) | Peak RSS | CPU | GPU |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        s = summarize(r)
        if s["error"] and not r.clips:
            lines.append(f"| {r.model} | failed: {s['error'][:80]} |" + " |" * 11)
            continue
        lat = [
            f"{f(s['lat'][k][0], '.2f')}/{f(s['lat'][k][1], '.2f')}" if k in s["lat"] else "n/a"
            for k in ("3s", "5s", "7s", "10s")
        ]
        gpu = f(s["gpu"], ".0f") + "%" if s["gpu"] is not None else "n/a"
        if s["gpu_mem"] is not None:
            gpu += f", {s['gpu_mem']:.0f} MB"
        lines.append(
            f"| {r.model} | {f(s['rtf'], '.3f')} | {f(s['rtf_p95'], '.3f')} "
            f"| {pc(s['wer_pt'], '.1f')} | {pc(s['name_err'], '.1f')} "
            f"| {pc(s['wer_mixed_pt'], '.0f')} / {pc(s['wer_mixed_auto'], '.0f')} "
            f"| {' | '.join(lat)} | {r.peak_rss_mb:.0f} MB "
            f"| {s['cpu']:.0f}% ({s['cores']:.1f} cores) | {gpu} |"
        )
    if results:
        s = summarize(results[0])
        lines += ["", f"{s['clips']} PT-BR clips, {s['audio_s']:.0f} s of audio."]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------ main


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--label", default=platform.node() or "machine",
                   help="machine label for the results file, e.g. mac-m2-16gb")  # fmt: skip
    p.add_argument("--work-dir", type=Path, default=Path.home() / ".cache" / "sombra-spike15")
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="results dir (default: scripts/spikes/15/results, or <work-dir>/results with "
        "--local-manifest so private hypotheses stay out of the repo)",
    )
    p.add_argument("--models", nargs="+", default=DEFAULT_MODELS)
    p.add_argument("--model-file", action="append", default=[], metavar="NAME=PATH",
                   help="use a local ggml file instead of downloading NAME")  # fmt: skip
    p.add_argument("--clips", type=int, default=30, help="FLEURS pt_br clips (default 30)")
    p.add_argument("--no-fleurs", action="store_true", help="skip FLEURS; use --local-manifest")
    p.add_argument("--local-manifest", type=Path,
                   help="JSON list of consented local clips, see the report")  # fmt: skip
    p.add_argument("--threads", type=int, default=min(8, psutil.cpu_count(logical=False) or 4))
    p.add_argument("--cuda", action="store_true", help="build whisper.cpp with CUDA")
    p.add_argument("--whisper-tag", default=WHISPER_CPP_TAG)
    p.add_argument("--server-arg", action="append", default=[],
                   help="extra whisper-server flag, e.g. --server-arg=--no-gpu")  # fmt: skip
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    work: Path = args.work_dir.expanduser()
    work.mkdir(parents=True, exist_ok=True)
    local_models = dict(
        (k, Path(v).expanduser().resolve())
        for k, v in (item.split("=", 1) for item in args.model_file)
    )

    server, commit = build_whisper(work, args.cuda, args.whisper_tag)
    clips: list[Clip] = []
    if not args.no_fleurs:
        pt = fleurs_subset(work, "pt_br", args.clips)
        en = fleurs_subset(work, "en_us", 2)
        clips += pt + mixed_clips(work, pt, en)
    if args.local_manifest:
        clips += local_clips(args.local_manifest)
    if not clips:
        raise SystemExit("no clips: drop --no-fleurs or pass --local-manifest")
    segs = segments([c for c in clips if c.kind == "pt"], work)

    results = []
    for name in args.models:
        model = fetch_model(work, name, local_models)
        results.append(bench_model(name, model, server, clips, segs, args.threads, args.server_arg))

    info = machine_info(args.threads, args.cuda, args.server_arg)
    info["whisper_cpp"] = f"{args.whisper_tag} ({commit[:10]})"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if args.out is None:
        repo_results = Path(__file__).resolve().parent / "results"
        args.out = work / "results" if args.local_manifest else repo_results
    args.out.mkdir(parents=True, exist_ok=True)
    base = args.out / f"{args.label}-{stamp}"
    report = {
        "label": args.label,
        "machine": info,
        "clips": [{**asdict(c), "path": c.path.name} for c in clips],
        "results": [asdict(r) for r in results],
        "summary": [summarize(r) for r in results],
    }
    base.with_suffix(".json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    table = markdown(args.label, info, results)
    base.with_suffix(".md").write_text(table, encoding="utf-8")
    print(table)
    log(f"wrote {base}.json and {base}.md")
    return 0 if all(r.error is None for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
