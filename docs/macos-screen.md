# Screen capture on macOS

Sombra takes a screenshot every 5–10 s (PRD S1) and tags it with the frontmost app and window title. Kept frames become `TELA` markers in `transcript.md`. The capture code is `src/sombra/screen/macos.py` (`MacScreenSource`).

## Permission: Screen Recording

macOS only lets an app capture the screen, or read other apps' window titles, after you grant it **Screen Recording**.

1. The first time Sombra captures, macOS shows its prompt once. If you dismiss it, macOS does not ask again.
2. Open **System Settings → Privacy & Security → Screen Recording** (on macOS 15: *Screen & System Audio Recording*).
3. Turn on the app you run `sombra` from: Terminal, iTerm, VS Code, and so on. The permission belongs to that app, not to Sombra.
4. Quit and reopen that app. macOS only applies the change after a restart.

Without the permission, every capture fails with `ScreenPermissionError` and the message above. Sombra never saves a blank frame in its place. Window titles also come back empty without it, so `sombra windows` requires the permission too.

On macOS 15, the system asks again about once a month whether the app should keep screen access. Answer *Allow*, or capture stops with the same error.

## The menu-bar indicator

While an app records the screen, macOS shows a purple screen-recording icon in the menu bar (on macOS 14, inside Control Center). Click it to see which app is capturing. Sombra takes one still frame at a time rather than streaming, so the indicator flashes each time a frame is taken, or stays on while a meeting is running, depending on the macOS version. It means Sombra can see what that capture covers: the whole display in display mode, one window in window mode. Pausing Sombra stops the captures.

## Display mode (default)

Captures one whole monitor. `display=0` is the main display; other monitors are numbered in the order macOS lists them. Each shot records the frontmost app window's app name and title, or `None` when there is no app window (only the desktop showing) or the title is unavailable.

## Window-only mode (S4)

Captures **one window and nothing else**. Use it to keep other apps, notifications, or a second monitor out of the meeting folder.

1. List the windows Sombra can capture:

   ```sh
   uv run sombra windows          # table: ID, APP, TITLE; "(hidden)" = minimised or on another Space
   uv run sombra windows --json   # one JSON object per line
   ```

2. Pick the window by **app** (exact name, case-insensitive, as in the `APP` column) and/or **title** (a case-insensitive substring of the title), for example `app="zoom.us"` or `app="Google Chrome", title="Roadmap"`.

How it behaves:

- The window is captured on its own, even when other windows cover it. Nothing from the rest of the screen gets into the frame.
- If several windows match, the frontmost visible one wins. Once chosen, Sombra follows that window while it exists, even if its title changes (for example, when you switch browser tabs).
- If the window closes, Sombra looks for a match again on the next capture. If none matches, the capture fails with `WindowNotFoundError`. It never falls back to the full display.
- A minimised window, or one on another Space, cannot be captured: while it is, each capture fails with `WindowNotFoundError` (no frame is saved) and capture resumes when the window is back.

## Backends and cost

| Backend | Used when | Cost per shot |
| --- | --- | --- |
| ScreenCaptureKit (`SCScreenshotManager`) | macOS 14+ with pyobjc (default) | target < 100 ms, loop < 3% CPU at 1 shot / 5 s (see the hardware test) |
| `screencapture -x` subprocess | ScreenCaptureKit unavailable | one process spawn plus a temp PNG per shot; roughly 100–250 ms (estimate, not yet measured) |

To measure on your Mac, run `uv run pytest -m hardware tests/screen/test_screen_macos_hardware.py -s`. It prints the p50 `grab()` latency and the CPU use of the capture loop, then checks for 5 minutes that window mode captures only a red test window while a green window covers it. Set `SOMBRA_HW_SECONDS=30` for a quicker run.
