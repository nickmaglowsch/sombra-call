# Spike #16 prototypes: Ubuntu GNOME Wayland

Throwaway scripts for [issue #16](https://github.com/nickmaglowsch/sombra-call/issues/16).
The report, the questions and the **test protocol** are in
[`docs/spikes/16-wayland.md`](../../../docs/spikes/16-wayland.md); follow that, not this file,
when running the spike.

| Script | Question | What it does |
| --- | --- | --- |
| `capture.py` | Q1 screen capture | ScreenCast portal with `persist_mode=2` + `restore_token`; `stream`, `per-capture` and `screenshot` modes; logs every capture, every `Start()` and the dialogs you mark |
| `notify.py` | Q2 approve/edit/discard | GNOME notification with Aprovar / Editar / Descartar; GTK 4 edit window as the local-UI fallback |
| `audio.py` | Q3 system audio | records mic + default sink monitor at 16 kHz through silence / playback / speech / both phases |
| `window_title.py` | Q4 window title | asks Introspect, Eval, AT-SPI and two Shell extensions which window is focused |
| `extension/` | Q4 | minimal GNOME Shell 45+ extension exposing the focused window on D-Bus |
| `summarize.py` | all | turns `logs/*.jsonl` into the report's Markdown tables |
| `portal.py`, `common.py` | — | GDBus portal client and JSONL logging shared by the scripts |

## Install (Ubuntu 24.04 LTS or newer, GNOME on Wayland)

```sh
sudo apt install python3-gi gir1.2-glib-2.0 gir1.2-gtk-4.0 gir1.2-atspi-2.0 \
  gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 gir1.2-gdkpixbuf-2.0 \
  gstreamer1.0-pipewire gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  pipewire-bin pulseaudio-utils
```

Run the scripts with the **system** Python (`/usr/bin/python3`), because PyGObject comes from
apt. They have no pip dependencies (their PEP 723 headers are empty), so `uv run` is not
needed and would hide the system `gi` module.

```sh
cd scripts/spikes/16
python3 capture.py --dry-run --minutes 0.2 --interval 2   # checks GStreamer without a portal
```

Logs, frames, WAVs and the restore token go to `logs/` (git-ignored). WAVs contain your voice
and frames contain your screen: do not commit them; paste only `summarize.py` output into the
report.

## Lint

The scripts are linted with the repo's ruff config (`make lint`); mypy and coverage do not
cover `scripts/`.
