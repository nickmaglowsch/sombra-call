# Spike 16: Ubuntu GNOME Wayland (screen capture, notifications, system audio, window title)

- Issue: [#16](https://github.com/nickmaglowsch/sombra-call/issues/16) (PRD: "Plataformas", "Riscos específicos do Wayland", "Spikes antes de fechar o escopo do Ubuntu")
- Status: **prototypes and protocol ready; every measured result is pending a human run** on a real Ubuntu GNOME Wayland session. The agent that wrote this had no Wayland session and did not run anything against GNOME. Nothing under "Results" may be filled in except from `summarize.py` output.
- Prototypes: [`scripts/spikes/16/`](../../scripts/spikes/16/)
- Proposed adapter design: [ADR 0016 (draft)](../adr/0016-linux-wayland-adapters.md)

## Summary

| # | Question | What the docs and source say | Measured | Proposed decision |
| --- | --- | --- | --- | --- |
| 1 | ScreenCast with `restore_token`: a frame every 5 s for 30 min and across an app restart, without a new dialog? | Supported by the API since ScreenCast v4 (`persist_mode=2`, single-use rotating token). A single open session needs no further dialogs by design. Restore of a *window* source is matched by app id + title in the GNOME backend, so it can fail and re-prompt. | pending human run | **Go** with one long-lived session + token restore, if the run shows 0 dialogs after the first. Fallback: per-trigger capture with the rotating token. |
| 2 | Approve / edit / discard from a GNOME notification? | GNOME Shell implements fdo notifications with `actions` (max 3 buttons shown) and emits `ActivationToken` before `ActionInvoked`. No `inline-reply` capability in the fdo server. The portal v2 defines `im.reply-with-text`, but GNOME support is unverified. | pending human run | Approve/discard from buttons; **edit opens a local GTK window** (fallback). |
| 3 | Clean OUTROS audio from the PipeWire monitor, separate from the mic? | Sink monitors carry only what apps play; `stream.capture.sink=true` (PipeWire) or `<sink>.monitor` (pipewire-pulse) records them. The mic still picks up speaker echo acoustically. | pending human run | **Go** for the monitor as OUTROS; headphones recommended; echo on speakers is a known leak into EU. |
| 4 | App/title on GNOME Wayland? | No portal gives it. `org.gnome.Shell.Introspect.GetWindows` and `Shell.Eval` refuse callers not on an allow-list unless Shell runs in unsafe mode. AT-SPI and a Shell extension are the remaining options. | pending human run | Frames get `app=None`, `window_title=None` by default (the contract allows it). Optional: user-installed extension. Fallback: user names the source. |

## Setup

To be filled in from the `environment` block that every log starts with (`summarize.py` prints it):

| Item | Value |
| --- | --- |
| Machine (CPU, RAM, GPU) | pending human run |
| Ubuntu release | pending human run (24.04 LTS is the baseline; 26.04 LTS if available) |
| GNOME Shell | pending human run |
| xdg-desktop-portal / -gnome versions | pending human run |
| PipeWire / WirePlumber | pending human run |
| GStreamer + `gstreamer1.0-pipewire` | pending human run |
| Displays (count, resolution, scale) | pending human run |
| Meeting app used (Meet in Chrome/Firefox, Zoom, …) | pending human run |

## What the documentation and source say

Sources were read on 2026-09-29 from the upstream repositories (GitHub mirrors of the GNOME projects). File links point at `main`; the behaviour on an Ubuntu release depends on the package version recorded above.

### Q1: ScreenCast / Screenshot portals

From [`org.freedesktop.portal.ScreenCast`](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.ScreenCast.html) ([XML source](https://github.com/flatpak/xdg-desktop-portal/blob/main/data/org.freedesktop.portal.ScreenCast.xml)):

- `SelectSources` takes `persist_mode` (`u`, since v4): `0` do not persist (default), `1` persist while the app runs, `2` persist until explicitly revoked. "If the permission for the session to persist is granted, a restore token will be returned" in the `Start()` response.
- `restore_token` (`s`, since v4): "If the stored session cannot be restored, this value is ignored and the user will be prompted normally. This may happen when, for example, the session contains a monitor or a window that is not available anymore, or when the stored permissions are withdrawn."
- Tokens are single-use: "The restore token is invalidated after using it once. To restore the same session again, use the new restore token sent in response to starting this session." So every restore must save the new token, or the next restart prompts again.
- `Start()` "will typically result [in] the portal presenting a dialog". Streams come back as `a(ua{sv})` with `id` (persists across restored sessions), `size`, `source_type` and, since v6, `pipewire-serial` (preferred over the node id, which can be reused after hotplug or suspend). **No stream property carries an app name or window title.**
- `OpenPipeWireRemote()` returns an fd for `pw_context_connect_fd`; GStreamer's `pipewiresrc fd=… path=<node>` uses it. `pipewiresrc` also has `target-object` (for the v6 serial) and `keepalive-time` (re-sends the last buffer when the compositor sends no damage) ([gstpipewiresrc.c](https://github.com/PipeWire/pipewire/blob/master/src/gst/gstpipewiresrc.c)).

How the token is stored ([`xdp-session-persistence.c`](https://github.com/flatpak/xdg-desktop-portal/blob/main/desktop-portal/xdp-session-persistence.c)): mode 1 ("transient") permissions are keyed by the D-Bus sender, mode 2 permissions go to the permission store keyed by **app id**. For a host (non-Flatpak) app the app id comes from a heuristic, or, since xdg-desktop-portal **1.19.4**, from [`org.freedesktop.host.portal.Registry.Register`](https://github.com/flatpak/xdg-desktop-portal/blob/main/data/org.freedesktop.host.portal.Registry.xml), which must be called before any other portal call and needs a matching `.desktop` file. `capture.py --app-id org.sombra.Spike` tests that path.

How GNOME restores ([xdg-desktop-portal-gnome `screencast.c`](https://github.com/GNOME/xdg-desktop-portal-gnome/blob/main/src/screencast.c)): the restore data holds monitors and windows; windows are re-found with `find_best_window_by_app_id_and_title`. A meeting window whose title changes (Meet puts the meeting name in the tab title) may not be found, which re-prompts. Sharing a **monitor** avoids that.

[`org.freedesktop.portal.Screenshot`](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.Screenshot.html) (v3) has `interactive` (default false), `modal` and, since v3, `target` (screen, window, area, active window). The docs say nothing about remembering permission for non-interactive screenshots; `capture.py --mode screenshot` measures what GNOME does.

What the docs do **not** answer, so the run must: whether GNOME shows a dialog on the first restore after a reboot, whether an unregistered host app (empty or guessed app id) gets a token at all with `persist_mode=2`, and what the sharing indicator looks like during a 30-minute session.

### Q2: notifications with actions

- fdo [Desktop Notifications spec](https://specifications.freedesktop.org/notification-spec/latest/): `Notify` takes an `actions` list of key/label pairs; the server emits `ActionInvoked(id, key)`; spec 1.2 adds `ActivationToken(id, token)` for XDG activation.
- GNOME Shell's server ([`notificationDaemon.js`](https://github.com/GNOME/gnome-shell/blob/main/js/ui/notificationDaemon.js)) advertises `actions, body, body-markup, icon-static, persistence, sound`, **not** `inline-reply`, and calls `_emitActivationToken` right before `_emitActionInvoked` for buttons and for the default action. [`messageList.js`](https://github.com/GNOME/gnome-shell/blob/main/js/ui/messageList.js) sets `MAX_NOTIFICATION_BUTTONS = 3`, so Aprovar / Editar / Descartar fit exactly and "Não era comigo" (`not_for_me`) needs another place (the edit window, or the body click).
- [`org.freedesktop.portal.Notification`](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.Notification.html) v2 adds `buttons` with a `purpose`; the `im.received` category defines `im.reply-with-text` ("the user-provided text will be added to the response"). Servers list supported purposes in the `SupportedOptions` property. Whether GNOME's backend lists it is unknown until `notify.py` reads the property. The portal notification is attributed to an app id, so a host app needs `--app-id` and the `.desktop` file.
- Two GNOME behaviours that can break the flow: Do Not Disturb (`org.gnome.desktop.notifications show-banners=false`) hides banners, and focus-stealing prevention can show "… is ready" instead of focusing a new window unless it carries the activation token. `notify.py` logs both.
- No always-on-top overlay on Wayland: core `xdg-shell` gives a client no way to keep its window above others, so the macOS-style floating overlay (O1) has no direct equivalent on GNOME.

### Q3: system audio

- PipeWire: `PW_KEY_STREAM_CAPTURE_SINK` = `"stream.capture.sink"`, "Try to capture the sink output instead of source output" ([`keys.h`](https://github.com/PipeWire/pipewire/blob/master/src/pipewire/keys.h)). `pw-record -P '{ stream.capture.sink=true }' --target <sink>` records a sink monitor; `pw-record --target <source>` records the mic.
- pipewire-pulse exposes each sink's monitor as `<sink>.monitor`, recordable with `parecord --device=<sink>.monitor` (`audio.py --backend pulse`).
- The monitor contains everything played to that sink (meeting audio, but also notification sounds and music). A single app can be targeted by node instead of the sink (not prototyped).
- The mic contains the user plus whatever the speakers play back into the room. Without headphones or an echo-cancel module, OUTROS speech also lands in the EU channel. `audio.py` measures that leak per phase.

### Q4: window title

- No portal exposes the focused window or its title (see ScreenCast stream properties above; `Screenshot` `target=8` "Active Window" captures pixels, not metadata).
- `org.gnome.Shell.Introspect.GetWindows` returns `title`, `app-id`, `wm-class` and `has-focus` per window, but checks the caller with `DBusSenderChecker` against an allow-list of the GTK and GNOME portal backends only, unless Shell runs in unsafe mode ([`introspect.js`](https://github.com/GNOME/gnome-shell/blob/main/js/misc/introspect.js), [`util.js`](https://github.com/GNOME/gnome-shell/blob/main/js/misc/util.js)). Expected result: `AccessDenied`.
- `org.gnome.Shell.Eval` returns an error unless `global.context.unsafe_mode` ([`shellDBus.js`](https://github.com/GNOME/gnome-shell/blob/main/js/ui/shellDBus.js)). Expected: refused.
- AT-SPI (accessibility bus) works on Wayland and exposes frames with an `ACTIVE` state and their accessible name, which for most apps is the window title. It depends on each toolkit exporting its tree; browsers and Electron apps may only do so once accessibility is enabled, which is why the protocol runs it with `toolkit-accessibility` off and on.
- A GNOME Shell extension can read `global.display.get_focus_window().get_title()` and export it on D-Bus (`scripts/spikes/16/extension/`). It runs inside Shell, needs a log-out/in to load on Wayland, must be updated for each GNOME release, and anything on the session bus can read what it exports.

## Method: test protocol for a human (about 1.5 h)

Everything runs from `scripts/spikes/16/` with the system Python. One terminal is enough; keep it visible. Every script writes `logs/<name>-<stamp>.jsonl`. **Do not run any of this during a real meeting with other people**; use a meeting with yourself (a second device or a test call) and synthetic content.

### 0. Prepare (10 min)

1. Log in to "Ubuntu" (Wayland) on GNOME. Check: `echo $XDG_SESSION_TYPE` prints `wayland`.
2. Install the packages from [`scripts/spikes/16/README.md`](../../scripts/spikes/16/README.md#install-ubuntu-2404-lts-or-newer-gnome-on-wayland).
3. Install the throwaway app id: `cp org.sombra.Spike.desktop ~/.local/share/applications/`.
4. Install the spike extension (Q4), then **log out and back in**:
   `cp -r extension/sombra-spike@sombra.local ~/.local/share/gnome-shell/extensions/` and, after logging in again, `gnome-extensions enable sombra-spike@sombra.local`.
5. Smoke test: `python3 capture.py --dry-run --minutes 0.2 --interval 2` must end with `"exit_code": 0`.
6. Start a test call (Meet in the browser or Zoom) with a second device, so there is remote audio and a meeting window.

### 1. Q1 screen capture (45 min, runs in the background of steps 2–4)

1. First run ever (expect exactly one dialog): `rm -f logs/restore_token.txt; python3 capture.py --mode stream --minutes 30 --app-id org.sombra.Spike`. Choose **the monitor** (not a window) and, if the dialog offers "Remember this selection" / "Allow restore", tick it. Type `d` + Enter in the terminal for **every** dialog you see, including this first one. Type a note (e.g. `indicator: orange icon top right`) to describe what the sharing indicator looks like. Leave it running and do steps 2–4 meanwhile; the capture keeps going.
2. After 30 min it exits. Restart test: run `python3 capture.py --mode stream --minutes 2 --app-id org.sombra.Spike` again. Mark any dialog with `d`.
3. Reboot test (optional, +5 min): reboot, log in, run the same 2-minute command. Mark dialogs.
4. Rotation / on-trigger fallback: `python3 capture.py --mode per-capture --minutes 5 --interval 10 --app-id org.sombra.Spike` (about 30 new sessions, each restored from the previous token). Mark dialogs; note whether the sharing indicator flashes each time.
5. Without app registration (tests the heuristic app id; skip if step 1 already failed): `python3 capture.py --mode stream --minutes 2 --token-file logs/token-noid.txt` twice.
6. Window instead of monitor: `python3 capture.py --mode stream --minutes 2 --window --token-file logs/token-window.txt` twice, sharing the meeting window. In between, change the meeting's title (e.g. switch browser tab) and note it.
7. Screenshot portal: `python3 capture.py --mode screenshot --minutes 2 --interval 10`. Mark dialogs; note any flash or sound.

Pass: step 1 has `captures` ≥ 355 with max gap ≤ 10 s, and `dialogs seen` = 1 (the first); steps 2 and 4 have `dialogs seen` = 0 and every `screencast_start` shows `restore_token_sent: true`.

### 2. Q2 notifications (15 min)

1. `python3 notify.py` (fdo backend). For each trial the terminal says what to click; press Enter first, then click. Trials: approve, edit, discard, body click, approve, edit, discard, ignore. In the edit window change a word and press **Enviar**. Note whether the edit window came up focused (you could type immediately) or GNOME showed "… is ready".
2. `python3 notify.py --urgency 2 --plan approve,ignore`: note whether a critical notification stays on screen until clicked.
3. `python3 notify.py --backend portal --app-id org.sombra.Spike --plan approve,edit,discard`: portal v2 buttons; if the log's `portal_notification.supported_options` lists `im.reply-with-text`, the Editar button should offer an inline text field; type into it.
4. Turn Do Not Disturb on, run `python3 notify.py --plan approve --timeout 20`, note what you see, turn it off.

Pass: approve and discard arrive as the right action in ≥ 9/10 clicks; the edit path reaches an editable text box (inline or window) that has focus within 2 s.

### 3. Q3 system audio (15 min)

1. With **headphones**: `python3 audio.py --output headphones`. Follow the prompts (quiet, quiet during playback, read the sentence, read during playback).
2. With **laptop speakers**, at a normal volume: `python3 audio.py --output speakers`.
3. Repeat step 1 with `--backend pulse` to confirm both recording paths give the same levels.
4. During the test call, run `pw-record -P '{ stream.capture.sink=true }' --target "$(pactl get-default-sink)" --rate 16000 --channels 1 logs/call-monitor.wav` for ~20 s while the other device talks and you stay quiet, then listen to the file. Note whether the remote voice is clean and whether your own voice is absent.

Pass: speech-phase separation ≥ 40 dB (your voice absent from the monitor) and, with headphones, playback-phase separation ≥ 30 dB (system audio absent from the mic). With speakers, record the number: it sets how much OUTROS echo the EU channel will transcribe.

### 4. Q4 window title (10 min)

1. `python3 window_title.py`. For each target (Meet in the browser, Zoom, Slack/Teams, Terminal), press Enter and click that window before the countdown ends.
2. Enable accessibility (`gsettings set org.gnome.desktop.interface toolkit-accessibility true`), run it again, then set it back to `false`.
3. Remove the extension afterwards: `gnome-extensions disable sombra-spike@sombra.local && rm -r ~/.local/share/gnome-shell/extensions/sombra-spike@sombra.local`.

Pass (per method): the reported title matches the clicked window for ≥ 3 of 4 targets without unsafe mode.

### 5. Report (10 min)

`python3 summarize.py > logs/summary.md`, paste its tables into "Results" below unchanged, add the notes on what you saw, fill in "Setup", then fill in the "Measured" column and confirm or change each decision. Remove `restore_token.txt` and do not commit anything from `logs/`.

## Results log format

Each script writes JSON lines with `ts` (local ISO-8601), `t` (seconds since start) and `type`. The types `summarize.py` reads:

| Script | Event types |
| --- | --- |
| all | `environment` (OS, GNOME, package versions, CPU), `summary` (totals, `cpu_percent` of one core) |
| `capture.py` | `config`, `host_registry`, `portal_versions`, `screencast_start` (`restore_token_sent`, `restore_token_returned`, `token_changed`, `start_s`, `streams`), `capture` / `screenshot` (`n`, `fresh`, size, `grab_ms` or `open_s`/`first_frame_s`/`total_s`, saved JPEG, periodic `cpu_percent`), `dialog_seen`, `note`, `session_closed_by_portal`, `portal_error`, `pipeline_error` |
| `notify.py` | `fdo_server` (capabilities, show-banners), `portal_notification` (version, SupportedOptions), `trial` (`expected`, `action`, `action_s`, `activation_token`, `closed`, `inline_reply`, `edit_window`, `window_focused_s`, `edited_chars`) |
| `audio.py` | `devices` (source, sink, `pactl info`, echo-cancel loaded), `phase` (mic/monitor RMS and peak dBFS, `separation_db`, durations, recorder errors) |
| `window_title.py` | `settings` (accessibility, enabled extensions), `sample` (per method: app/title or error, latency) |

`start_s` includes the time a dialog was on screen, so a restore without a dialog shows up as a short `start_s` (expected well under 1 s); the tester's `d` marks are the authoritative dialog count.

## Results

All pending human run. Paste `summarize.py` output here.

### Q1 screen capture

| Run | Captures | Max gap (s) | Starts (token sent) | Dialogs seen | Start() p50 (s) | CPU % | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1. stream 30 min, first run | pending human run | | | | | | |
| 2. stream restart | pending human run | | | | | | |
| 3. after reboot | pending human run | | | | | | |
| 4. per-capture, 5 min | pending human run | | | | | | |
| 5. no app id | pending human run | | | | | | |
| 6. window source | pending human run | | | | | | |
| 7. screenshot portal | pending human run | | | | | | |

Sharing indicator (what the user sees): pending human run.

### Q2 notifications

| Backend | Approve ok | Discard ok | Edit path | Window focused after (s) | Activation token | Inline reply offered | DND behaviour |
| --- | --- | --- | --- | --- | --- | --- | --- |
| fdo | pending human run | | | | | | |
| portal | pending human run | | | | | | |

### Q3 system audio

| Output | Backend | Silence mic / monitor dBFS | Playback separation dB | Speech separation dB | Notes |
| --- | --- | --- | --- | --- | --- |
| headphones | pw | pending human run | | | |
| speakers | pw | pending human run | | | |
| headphones | pulse | pending human run | | | |

Live call check (remote voice clean, own voice absent): pending human run.

### Q4 window title

| Method | Meet (browser) | Zoom | Slack/Teams | Terminal | Notes |
| --- | --- | --- | --- | --- | --- |
| Introspect | pending human run | | | | expected AccessDenied |
| Eval | pending human run | | | | expected refused |
| AT-SPI (a11y off / on) | pending human run | | | | |
| Spike extension | pending human run | | | | |
| Window Calls (3rd party) | pending human run | | | | only if installed |

## Decision

Proposed from the documentation; each line is confirmed or changed once the results are in. The phase-2 adapter design built on these is [ADR 0016 (draft)](../adr/0016-linux-wayland-adapters.md).

1. **Screen (Q1)**: go with one ScreenCast session per meeting, monitor source, `persist_mode=2`, token saved after every `Start()` and the app registered through the host Registry where available. If restores re-prompt, fall back to the PRD risk-table option: capture only on trigger, accepting a dialog per meeting, or the Screenshot portal if it proves silent.
2. **Approval UI (Q2)**: notification with Aprovar / Editar / Descartar; Editar and a body click open a small local GTK window (with the activation token) that also offers "Não era comigo". If buttons prove unreliable, the window alone is the UI and the notification only announces it.
3. **System audio (Q3)**: go with the default sink monitor as OUTROS and the default source as EU, both at 16 kHz mono. Recommend headphones; with speakers, evaluate PipeWire's echo-cancel module in phase 2.
4. **Window title (Q4)**: record `app=None`, `window_title=None` on Wayland (the `FrameRecord` contract already allows it) and let the user name the shared source at meeting start. Offer the extension or AT-SPI only if the run shows they work without unsafe mode and the privacy cost is acceptable.

## Left for a human

1. Run the protocol above on Ubuntu GNOME Wayland and paste the `summarize.py` tables into "Results".
2. Fill in "Setup" and the "Measured" column of the summary; confirm or change each decision.
3. Move ADR 0016 from "proposed" to "accepted" (or rewrite it) and open the phase-2 issues it lists.
4. Close issue #16.
