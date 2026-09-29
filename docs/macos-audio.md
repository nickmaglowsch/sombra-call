# macOS audio capture

Sombra records two channels (PRD A1): your microphone as `EU` and the computer's
output (everyone else in the call) as `OUTROS`. Because they are captured separately,
"me vs. others" needs no diarization.

## Requirements

- macOS 14.4 or later for the built-in system-audio path (Core Audio process taps).
  On older versions, or if you prefer, use [BlackHole](#fallback-blackhole).
- Apple Silicon is the MVP target; Intel should work but is not tested.

## Permissions

macOS asks the first time Sombra captures. Grant them to the app that runs `sombra`
(Terminal, iTerm, VS Code, ...), then restart that app.

| Permission | Where | Needed for |
| --- | --- | --- |
| Microphone | System Settings → Privacy & Security → Microphone | your voice (`EU`) |
| Screen & System Audio Recording (the "System Audio Recording Only" list on macOS 15) | System Settings → Privacy & Security → Screen & System Audio Recording | system audio through the process tap (`OUTROS`) |

Without the second permission, macOS may still create the process tap and then deliver
**silence** instead of an error (still to be confirmed on hardware), so `auto` cannot rely
on falling back. If `OUTROS` stays silent while the call is audible, check this permission
first. When tap creation does fail outright, `audio.system = "auto"` falls back to a
BlackHole device if one is installed, and logs which path it used.

## Choosing devices

```sh
uv run sombra devices          # table
uv run sombra devices --json   # for scripts
```

The ids it prints go in the config:

- `audio.mic`: a microphone id, for example `"MacBook Pro Microphone"`. Unset = the system default input.
  Devices with the same name get `#2`, `#3`, ... suffixes.
- `audio.system`:
  - `"auto"` (default): process tap on macOS 14.4+, otherwise the first loopback device (BlackHole).
  - `"tap:system"`: process tap only; fail instead of falling back.
  - a loopback device id such as `"BlackHole 2ch"`.

`MacAudioSource.system_path` records which path is in use (`process-tap` or
`loopback:<id>`) so it can be logged with the meeting.

## How the process tap works

Sombra creates a private, unmuted, global stereo tap (`CATapDescription`) of every
process's output and wraps it in a private aggregate device, which PortAudio then opens
like a microphone. You keep hearing the call normally. The tap and the aggregate device
are destroyed when capture stops; they are private to the Sombra process and never show
up in other apps.

## Fallback: BlackHole

1. Install BlackHole 2ch: `brew install blackhole-2ch` (or the installer from
   <https://existential.audio/blackhole/>).
2. Open **Audio MIDI Setup**, click **+** → **Create Multi-Output Device**, and tick
   your speakers/headphones **and** BlackHole 2ch. Put your speakers first and enable
   drift correction on BlackHole.
3. Make the Multi-Output Device your system output (menu bar sound icon, or
   System Settings → Sound → Output). You still hear the call; BlackHole gets a copy.
4. Check `sombra devices` lists `BlackHole 2ch` under system audio, then set
   `audio.system = "BlackHole 2ch"` (or leave `"auto"`).

Volume keys do not work on a Multi-Output Device; change the volume in the call app,
or switch the output back after the meeting.

## What the capture produces

Every chunk is 16 kHz mono float32 (`contracts.SAMPLE_RATE`), 50 ms by default
(20–100 ms configurable), with a wall-clock `start` taken from one monotonic clock for
both channels. Timestamps follow the sample count, steered slowly towards the host clock,
so a mic and an output device whose clocks drift apart stay aligned (≤ 50 ms per hour).
If the consumer stalls, the oldest audio is dropped and counted (about 10 s is buffered);
the capture callback never blocks.

## Checking it on your Mac

```sh
uv run pytest -m hardware -s tests/audio/test_audio_hardware.py
```

Play a video and speak while it runs (60 s; `SOMBRA_AUDIO_TEST_SECONDS` changes it,
`SOMBRA_MIC` / `SOMBRA_SYSTEM` pick devices). It prints the path used, per-channel
levels, drops and the process CPU share, and fails if a channel is silent or CPU is ≥ 2%.
