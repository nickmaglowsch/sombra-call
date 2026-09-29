# ADR 0016: Linux (GNOME Wayland) adapters for screen, audio and approval UI

- Status: **proposed (draft)**, waiting for the measured results of spike [#16](https://github.com/nickmaglowsch/sombra-call/issues/16)
- Date: 2026-09-29
- Numbered after its issue to avoid clashes with ADRs written in parallel.
- Evidence: [`docs/spikes/16-wayland.md`](../spikes/16-wayland.md), prototypes in [`scripts/spikes/16/`](../../scripts/spikes/16/)

## Context

Phase 2 brings Sombra to Ubuntu with GNOME on Wayland. Wayland removes the tools the macOS build relies on: there is no silent screen grab, no API for the focused window's title, and no always-on-top overlay for other clients. The ports in `sombra.contracts.ports` (`ScreenSource`, `AudioSource`, `ApprovalUI`) stay as they are; this ADR decides what sits behind them on Linux. It is written from the portal and GNOME documentation and source, before the spike's human run, and must be confirmed or amended with those numbers.

## Decision

### `screen/wayland.py`: `WaylandScreenSource` (implements `ScreenSource`)

- **One ScreenCast portal session per meeting**, monitor source, `persist_mode=2`. The PipeWire stream stays open for the meeting; `grab()` returns the newest frame. This is what makes "a frame every 5–10 s" possible without a dialog per frame.
- **Restore token**: saved after every `Start()` (tokens are single-use and rotate), in the user state dir (`$XDG_STATE_HOME/sombra/screencast.token`, mode 0600). It is not an API secret, but it lets Sombra re-open screen capture without asking, so it does not belong in the meeting folder or logs. First meeting: one dialog. Later meetings: none, if the spike confirms it.
- **App identity**: call `org.freedesktop.host.portal.Registry.Register("<app id>")` first when the portal has it (xdg-desktop-portal ≥ 1.19.4) and install a matching `.desktop` file, so the permission store keys the grant to Sombra rather than to a guessed id.
- **Pipeline**: GStreamer `pipewiresrc` (`target-object=<pipewire-serial>` on ScreenCast v6, `path=<node id>` before) with `keepalive-time` so a static screen still yields frames, `videorate max-rate=1`, `videoconvert` to RGB, `appsink max-buffers=1 drop=true`. Encoding to PNG happens only in `grab()`, a few times per minute.
- **Metadata**: `Screenshot.app` and `Screenshot.window_title` are `None` (the contract already allows it). The frame marker falls back to a user-provided source label (see Follow-ups).
- **Failure handling**: `Session::Closed` (the user pressed "stop sharing" in the GNOME indicator) stops screen capture, logs it and tells the orchestrator. Audio and transcription keep running (invariant 5). Re-opening uses the saved token.
- **Fallback, chosen by config, not by code path**: if the spike shows restores re-prompting, `capture_mode = "on_trigger"` opens a session only when a trigger needs the screen (the PRD's risk-table fallback), costing one dialog per meeting at most with `persist_mode=1`.
- Bindings: `gi` (PyGObject) for GDBus and GStreamer, imported lazily inside the module so `import sombra.screen` works on macOS and in CI.

### `audio/pipewire.py`: `PipeWireAudioSource` (implements `AudioSource`)

- **EU** = default source (mic). **OUTROS** = monitor of the default sink (`stream.capture.sink=true`), which carries only what apps play, so the user's voice is not in it.
- Both captured as mono float32 at 16 kHz (`SAMPLE_RATE`), each on its own stream, and yielded as `AudioChunk`s on `Channel.ME` / `Channel.OTHERS`. Capture through GStreamer `pipewiresrc` with stream properties, or a `pw-record` subprocess writing raw PCM to a pipe; phase 2 picks one by CPU and latency.
- `list_devices()` from WirePlumber/pipewire-pulse (`pactl` JSON output) with `is_input=False` for monitors.
- Speaker echo: the mic hears the speakers, so OUTROS speech can also appear as EU. Recommend headphones in the UI; evaluate PipeWire's `echo-cancel` module in phase 2, using the spike's speaker numbers as the baseline.
- Follow the default sink when it changes (headset plugged in mid-meeting) and reconnect, as A3 requires.

### `ui/gnome.py`: `GnomeApprovalUI` (implements `ApprovalUI`)

- `notify_trigger`: a transient fdo notification "Sombra: procurando contexto…".
- `show`: replaces it (`replaces_id`) with the suggestion and three buttons, the most GNOME Shell shows: **Aprovar**, **Editar**, **Descartar**. Approve and discard map straight to `UserAction`s.
- **Editar** and a click on the body open a small GTK 4 window with the text editable, **Enviar**, **Descartar** and **Não era comigo**. The window is presented with the XDG activation token GNOME sends in `ActivationToken` just before `ActionInvoked`, so it gets focus.
- `notify_failure`: a notification with the trigger excerpt; its body opens the same window in read-only mode.
- If the spike shows GNOME's portal backend supports `im.reply-with-text`, "Editar" can be an inline reply instead of a window. That is an optimisation, not a dependency.
- No floating always-on-top overlay on Wayland; the notification plus window replace it.

### Window title

Not available to a normal client on GNOME Wayland (Introspect and Eval are allow-listed or unsafe-mode only; no portal exposes it). Sombra does not ship a Shell extension in phase 2. If the spike shows AT-SPI gives correct titles without special setup, it can be added later behind the same `ScreenSource` as best-effort metadata.

## Consequences

- Linux needs PyGObject, GStreamer, `gstreamer1.0-pipewire` and a `.desktop` file. PyGObject is LGPL-2.1-or-later, which Gate 7 asks to flag: it is used as a dynamically loaded library, which LGPL permits in commercial software, but a reviewer must confirm. Installing it from pip needs the GObject introspection headers; using the distro package (`python3-gi`) means the Linux build runs on the system Python or a venv with system site-packages. Phase 2 decides which.
- Screen capture shows GNOME's sharing indicator for the whole meeting. That is visible consent and matches the privacy stance, but the UI copy should explain it.
- Frames on Linux have no app/title, so `needs_screen` heuristics and the summary cannot use the window title there.
- Revoking the stored permission (it lives in the xdg-desktop-portal permission store) brings the dialog back; the adapter must treat that as normal.

## Follow-ups (phase-2 issues, opened after the spike run confirms this ADR)

1. `screen/wayland.py`: `WaylandScreenSource` with token store, host registration, `Session::Closed` handling and the `on_trigger` mode.
2. `audio/pipewire.py`: `PipeWireAudioSource` with device following and reconnect.
3. `ui/gnome.py`: `GnomeApprovalUI` (notification + GTK window).
4. Packaging for Linux: `.desktop` file, PyGObject sourcing, apt dependencies.
5. Config: a "shared source label" the user sets at meeting start, used in `TELA` markers when `window_title` is `None` (touches `config` and possibly contracts; a separate `contract-change` if it does).
6. Echo cancellation evaluation for speaker use.
