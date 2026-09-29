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

## When a device goes away (reconnect)

Headsets get unplugged and outputs change mid-call. Capture notices and recovers by
itself (PRD A3); the transcript just shows a short gap on the affected channel.

How a loss is noticed:

- **Core Audio notifications**: Sombra listens for changes to the device list and the
  default input/output. If the microphone in use disappears, `EU` is restarted at once.
  With the process tap, a change of the default output restarts `OUTROS`.
- **Stall watchdog**: a channel that delivered audio and then nothing for 0.5 s is
  treated as lost, whatever the cause.

How the replacement is chosen for `EU`: the configured `audio.mic` id, then any device
with the same name, then the system default input, then any other microphone.
Loopback drivers and Sombra's own tap are never picked as the mic. When the configured
mic (or, with `audio.mic` unset, the new system default) is plugged back in, `EU`
switches back to it.

Retries back off 0.1, 0.2, 0.4, 0.8, 1.6, then every 2 s until the device is back or
capture stops. A restart only counts as done when the first new audio arrives.

**The other channel keeps flowing** whenever the replacement device is one PortAudio
already knows about: unplugging a headset (fall back to the built-in mic) or switching
outputs on the tap path (reopen the tap's device) touch one channel only. PortAudio can
only discover *new* devices by re-initialising, which closes every stream, so plugging a
headset back in, or recreating a process tap that stopped working, reopens both
channels. The status events report the other channel's gap too.

### Status events

`MacAudioSource(on_status=callback)` calls `callback(SourceStatus)` on the reconnect
thread (keep it quick; hand off with `loop.call_soon_threadsafe`). Fields:

| Field | Meaning |
| --- | --- |
| `channel` | `EU` or `OUTROS` |
| `kind` | `lost`, `retrying` (an attempt failed, next after the backoff) or `restored` (see below for planned switches) |
| `at` | wall-clock time, on the same clock as the chunks |
| `reason` | why it was lost or why the attempt failed |
| `device` | the device id in use after the restart |
| `attempt` | restart attempt number, from 1 |
| `gap_s` | on `restored`: seconds from the last audio before the loss to the first new audio |

Not every `lost` is a failure. A deliberate move to a better device is reported as
`lost` too, with a `reason` the orchestrator can match: `switching to microphone ...`
(the preferred mic came back), `default output changed ...` (tap path), or
`reopened with EU` / `reopened with OUTROS` (the other channel, reopened because PortAudio
had to re-enumerate). Show those as "switching" rather than "source lost".

## Checking it on your Mac

```sh
uv run pytest -m hardware -s tests/audio/test_audio_hardware.py
```

Play a video and speak while it runs (60 s; `SOMBRA_AUDIO_TEST_SECONDS` changes it,
`SOMBRA_MIC` / `SOMBRA_SYSTEM` pick devices). It prints the path used, per-channel
levels, drops and the process CPU share, and fails if a channel is silent or CPU is ≥ 2%.

Reconnect (A3):

```sh
uv run pytest -m hardware -s tests/audio/test_audio_hardware.py -k reconnect
```

During the 10 minutes (`SOMBRA_RECONNECT_TEST_SECONDS` changes it), unplug and replug a
Bluetooth or USB headset and switch outputs (menu bar sound icon) at least 5 times each.
It prints every status event with its gap and fails if a channel did not come back, or
took 2 s or more.
