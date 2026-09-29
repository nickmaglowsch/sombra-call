# Spike #15: whisper.cpp real-time factor and PT-BR accuracy

Status: **script ready, reference-hardware numbers pending a human run.** The agent that
wrote this had only a cloud Linux VM without GPU, and that VM cannot reach Hugging Face, so
it could not download any real model or the FLEURS clips. Every cell below marked *pending
human run* is empty on purpose: no number in this report was estimated or copied from
elsewhere.

## 1. Question and pass criterion

Which whisper.cpp model and quantisation runs in real time with good PT-BR accuracy on the
reference Mac and on Ubuntu (with and without an NVIDIA GPU)?

Pass criteria (PRD spike 2, T2):

- **Ubuntu reference hardware: real-time factor < 0.5.**
- **Mac: ~1 s from end of speech to text** (the STT slice of the 6 s p50 budget in
  `docs/ARCHITECTURE.md`).

Also answers, partly, the PRD open questions "reference hardware" (proposal in §5) and
"PT-BR only or mixed English" (mixed-clip WER in the tables).

## 2. Setup

| | Mac reference | Ubuntu CPU-only | Ubuntu + NVIDIA |
| --- | --- | --- | --- |
| Machine | pending human run | pending human run | pending human run |
| Chip / CPU | | | |
| GPU | Apple GPU (Metal) | none | |
| RAM | | | |
| OS | | | |

The script records all of this itself (CPU model, logical/physical cores, RAM, GPU from
`nvidia-smi -L`, OS, whisper.cpp tag and commit, thread count, model SHA-256) in the JSON
it writes, and prints it above the results table.

Software, pinned by the script:

- whisper.cpp **v1.9.4** (`927cfce34f`), built from source with CMake; `whisper-server`
  target. Metal is on by default on macOS; `--cuda` builds with `-DGGML_CUDA=1`.
- Models from `huggingface.co/ggerganov/whisper.cpp`: `large-v3-turbo-q5_0`,
  `large-v3-turbo-q8_0`, `medium-q5_0`, `small-q5_1`. The issue asks for "small q5":
  whisper.cpp only publishes small as `q5_1`, so that is what is measured. Any other ggml
  file can be added with `--models NAME` (downloaded) or `--model-file NAME=PATH` (local).
- Python ≥ 3.12 via `uv`; the only Python dependency is `psutil` (BSD-3-Clause), declared
  inline (PEP 723), not added to the `sombra` package.

Prerequisites on the machine: `git`, `cmake`, a C++ compiler (Xcode CLT on macOS,
`build-essential` on Ubuntu), `uv`, and the CUDA toolkit for `--cuda`. `ffmpeg` only if a
clip is not already 16 kHz mono 16-bit WAV.

## 3. Method

One command per machine (details and all flags: `--help`):

```sh
# Mac (Metal)
uv run scripts/spikes/15/bench_whisper.py --label mac-<chip>-<ram>

# Ubuntu, CPU-only
uv run scripts/spikes/15/bench_whisper.py --label ubuntu-<cpu>-cpu

# Ubuntu with NVIDIA: GPU run, then the same build forced onto the CPU
uv run scripts/spikes/15/bench_whisper.py --cuda --label ubuntu-<gpu>
uv run scripts/spikes/15/bench_whisper.py --cuda --server-arg=--no-gpu --label ubuntu-<gpu>-nogpu
```

Downloads (models ~2.5 GB total, the FLEURS stream a few hundred MB) are cached under
`~/.cache/sombra-spike15`; a re-run only re-measures. Results land in
`scripts/spikes/15/results/<label>-<timestamp>.{json,md}`; the `.md` is the table to paste
into §4.

What the script does:

1. Builds `whisper-server` at the pinned tag.
2. Fetches 30 distinct PT-BR sentences (`--clips`) from the FLEURS `pt_br` test split by
   streaming the archive and stopping once it has enough, plus 2 `en_us` sentences.
3. Builds **2 mixed clips**: a PT-BR sentence, 0.4 s of silence, an English sentence. This is
   a language switch *between* sentences. Real code-switching *inside* a sentence ("o deploy
   da feature flag ficou pra sexta") has no public PT-BR corpus I could find with a usable
   license, so the script also accepts consented local recordings (see below).
4. Per model: starts `whisper-server -l pt -t <threads>` once; model load time is recorded
   but excluded from every other number. One warm-up request, then:
   - **RTF**: each full clip is POSTed to `/inference`; RTF = total processing time / total
     audio duration over all PT-BR clips (the "RTF p95" column is the per-clip 95th
     percentile). Processing time is send → full JSON response on localhost, so it includes
     mel, encoder and decoder, which is what a VAD-segmented live path pays.
   - **Per-utterance latency**: the first 3, 5, 7 and 10 s of each clip long enough, POSTed
     one at a time; p50/p95 per length. This models the transcription module sending one
     VAD segment at end of speech, so it is the "end of speech → text" number for the Mac
     criterion (minus VAD hangover, which #4 owns).
   - **WER**: word-level Levenshtein / reference words, over all PT-BR clips. Normalisation:
     Unicode NFC, lower case, punctuation removed, **accents kept** (a missing accent is an
     error, because the trigger detector and the agent both see the text). Numbers are not
     normalised; FLEURS keeps digits as written, so "1990" vs "mil novecentos e noventa"
     counts as errors for every model alike.
   - **Name error**: FLEURS sentences come from Wikipedia and are full of names. Proper
     names are approximated as capitalised words that do not start a sentence; name error
     = share of those tokens missing from the hypothesis. For local clips the manifest can
     list the names explicitly.
   - **Mixed clips** are transcribed twice: with `language=pt` (what the live path would
     force) and `language=auto`.
   - **Peak RSS** of the server process (sampled every 100 ms), **CPU** as cores busy and as
     % of the machine during the measured window, **GPU** utilisation from `nvidia-smi` or,
     on macOS, `ioreg` IOAccelerator "Device Utilization %" (no sudo). On Apple Silicon the
     Metal buffers live in unified memory and may not all show up in RSS. The GPU memory
     figure is `nvidia-smi` `memory.used` for the whole device (desktop and other processes
     included), not whisper-server alone; read it against an idle baseline.

### Consented local clips (optional, recommended)

`--local-manifest clips.json` adds recordings made by the person running the benchmark,
e.g. the two code-switching scripts and the name-heavy question in
[`scripts/spikes/15/local-clips.example.json`](../../scripts/spikes/15/local-clips.example.json).
Record them in your own voice, any format ffmpeg reads, next to the manifest. Only record
yourself or people who agreed; do not commit the audio. With `--local-manifest` the results
go to `~/.cache/sombra-spike15/results/` (not the repo) unless `--out` is given, because
the JSON contains the hypotheses.

```sh
uv run scripts/spikes/15/bench_whisper.py --label mac-m2-16gb \
  --local-manifest ~/sombra-clips/clips.json
```

### Audio licenses

| Source | Used for | License |
| --- | --- | --- |
| [FLEURS](https://huggingface.co/datasets/google/fleurs) `pt_br` and `en_us` test splits (Conneau et al., 2022) | PT-BR clips, mixed clips | CC BY 4.0 |
| Local recordings via `--local-manifest` | code-switching, names | consented, never committed |
| whisper.cpp `samples/jfk.wav` | VM smoke test only | public-domain speech (US government) |

No audio is committed to this repository; the script downloads FLEURS into the cache.

## 4. Results

Metric columns, in every table: RTF (lower is better; < 0.5 passes on Ubuntu), WER PT,
Name err, WER mixed with `pt` / `auto`, latency p50/p95 for 3/5/7/10 s utterances, peak
RSS, CPU, GPU.

### Mac reference (Apple Silicon, Metal)

pending human run

### Ubuntu, CPU-only

pending human run

### Ubuntu + NVIDIA (CUDA) and same machine with `--no-gpu`

pending human run

### Cloud VM smoke test, not reference hardware

Only proves the pipeline runs end to end: build at the pinned tag, server start and health
check, WAV upload, WER/name scoring, RSS/CPU sampling, JSON + Markdown output. The model is
whisper.cpp's `for-tests-ggml-tiny.bin` (a 575 KB stub without real weights) and the clip is
English, because this VM cannot reach Hugging Face. **None of these numbers says anything
about any real model; they must not be used.**

```sh
uv run scripts/spikes/15/bench_whisper.py --label smoke --no-fleurs \
  --local-manifest <dir>/manifest.json \
  --models tiny-for-tests --model-file tiny-for-tests=<whisper.cpp>/models/for-tests-ggml-tiny.bin
```

`manifest.json` lists `jfk.wav` with its English reference text.

Intel Xeon @ 2.80 GHz (KVM), 4 vCPU, 15.7 GB RAM, no GPU, Linux 6.18. whisper.cpp v1.9.4
(927cfce34f), `-t 4`. Build of `whisper-server` from scratch: ~80 s.

| Model | RTF | WER | Latency 3/5/7/10 s p50 | Peak RSS | CPU |
| --- | --- | --- | --- | --- | --- |
| tiny-for-tests (stub) | 0.067 | 100% (stub emits nothing) | 0.71 / 0.69 / 0.69 / 0.68 s | 108 MB | 3.3 cores |

## 5. Decision

### How to read the results into a default

For each platform, the **default** is the most accurate model (lowest WER PT, then name
error) that meets both:

- RTF < 0.5 over the PT-BR set, and
- 10 s utterance latency p95 ≤ 1.5 s (p50 ≈ 1 s is the Mac target).

The **fallback** is the next smaller model that meets them with margin (RTF < 0.3), used
when the machine is under load or on battery. If `large-v3-turbo-q5_0` and `-q8_0` tie on
WER within 1 point, prefer q5_0 (smaller, less memory, less bandwidth).

### Recommendation (provisional until the numbers are in)

| Platform | Default | Fallback | Status |
| --- | --- | --- | --- |
| macOS, Apple Silicon (phase 1, #4) | `large-v3-turbo-q5_0` | `small-q5_1` | hypothesis, confirm with the Mac run |
| Ubuntu + NVIDIA (phase 2) | `large-v3-turbo-q5_0` | `medium-q5_0` | hypothesis, confirm with the CUDA run |
| Ubuntu CPU-only (phase 2) | decided by the CPU run | `small-q5_1` | open: the "STT does not keep up without GPU" risk is exactly this cell |

The hypothesis follows from model size only (turbo has 4 decoder layers against 32 in
large-v3, so it is the obvious first candidate on a GPU); it is not a measurement. The
transcription module (#4) should take the model name as a setting and ship with this
default, so the numbers can change it without code changes.

"PT-BR only or mixed English": decided by the mixed-clip columns. If WER with `pt` on the
mixed and code-switching clips is within a few points of `auto`, keep `-l pt` forced (it
avoids language-ID flapping on short segments); otherwise #4 needs per-segment language
detection.

### Proposal for the "reference hardware" open question

- **Mac: base M1 (or M2) with 16 GB RAM.** It is the floor of the Apple Silicon machines
  people use for work; whatever passes there passes on Pro/Max. 8 GB machines are out of
  the MVP target (whisper + a browser + a call app leaves too little headroom for the
  2 GB budget).
- **Ubuntu: CPU-only is the reference**, an 8-core x86-64 laptop/desktop from the last
  ~4 years (Ryzen 7 5800U/6800U or Core i7 12th gen class) with 16 GB RAM. An NVIDIA GPU
  (RTX 3060 class) is a second, optional tier: it should make the larger model the default
  there, but the product cannot require it.

This proposal stands or falls with the CPU-only run: if no model reaches RTF < 0.5 with
acceptable WER on that class of CPU, phase 2 needs either a GPU requirement or a smaller
model with a documented accuracy cost, and that goes in an ADR.

No ADR yet: the default is a setting of #4 and will be recorded in an ADR once the
reference numbers are in.

## Left for a human

1. On the Mac reference machine: `uv run scripts/spikes/15/bench_whisper.py --label mac-<chip>-<ram>`
   (optionally with `--local-manifest` and the three clips in the example manifest).
2. On an Ubuntu machine without GPU: `uv run scripts/spikes/15/bench_whisper.py --label ubuntu-<cpu>-cpu`.
3. If an NVIDIA machine is available: the two `--cuda` commands in §3.
4. If the FLEURS fetch fails (layout or URL changed), report it on #15 instead of switching
   to other clips, so the results stay comparable across machines.
5. Paste each `results/*.md` table into §4, commit the `results/*.json` (FLEURS runs only),
   fill §2 and confirm or change the recommendation in §5.
