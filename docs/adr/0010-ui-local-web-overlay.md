# ADR 0010: Approval overlay as a local web UI in an always-on-top pywebview window

- Status: accepted
- Date: 2026-09-29
- Issue: #10 (PRD O1, O2; open question "native vs. Tauri vs. local web UI")

## Context

The overlay is the L1/L2 surface: a small window that must stay visible over a full-screen
Zoom/Meet call, show a suggestion within the 6 s budget (< 100 ms after `show()`), and let
the user approve, edit or discard it with the keyboard. MVP is macOS; the same UI must work
on Ubuntu/Wayland in phase 2. The core is Python (ADR 0001), and the orchestrator talks to
the UI only through the `ApprovalUI` port.

Options considered:

| Option | For | Against |
| --- | --- | --- |
| Native (AppKit via pyobjc) | Best control of window level and Spaces | macOS only; a second UI for Wayland; untestable in Linux CI; slow to iterate |
| Tauri | Small binary, good webview, real app lifecycle | Adds Rust + a second process/toolchain to a Python core; IPC to design anyway; heavy for one small window |
| Qt (PySide6) | Cross-platform, on-top flags | LGPL packaging questions, ~100 MB dependency, Wayland does not allow always-on-top for normal clients anyway |
| **Local web UI + pywebview** | One HTML/JS UI for every OS; protocol testable headless in CI; pywebview is a thin wrapper over WKWebView / WebKitGTK; any browser works as a fallback | Needs a local server (security surface); full-screen floating on macOS needs one native tweak |

## Decision

- **Local web UI.** `sombra.ui.OverlayUI` runs an aiohttp server bound to `127.0.0.1` on a
  random free port. Every request needs a per-session random token (`secrets.token_urlsafe(32)`,
  in the URL the window opens); the `Host` and `Origin` headers must name the loopback server
  (blocks DNS rebinding and cross-site websocket hijacking). The page is served with a strict
  CSP (nonce'd inline script/style, `connect-src` limited to the server's websocket).
- **Websocket, server-pushed state.** The server pushes the full card list after every change
  (a handful of cards, so snapshots are cheaper to reason about than diffs); the client sends
  `action` / `dismiss` messages. The protocol lives in `sombra.ui.state` with no I/O, so it is
  unit-tested without a server, and end to end over a real loopback websocket in CI.
- **Window: pywebview**, as an optional extra (`uv sync --extra window`) imported lazily, so
  the package imports anywhere. On macOS it opens with `on_top=True` (NSStatusWindowLevel)
  and sets `NSWindowCollectionBehavior` CanJoinAllSpaces | Stationary | FullScreenAuxiliary so
  it can float over another app's full-screen Space. Without pywebview, `sombra ui-demo`
  falls back to a browser tab.
- **Notifications and clipboard** go through OS command-line tools (`osascript`, `pbcopy`;
  `notify-send`, `wl-copy`/`xclip`) with text passed as argv, never through a shell. Missing
  tools make them no-ops; failures never break the overlay.
- Meeting content is data: the page renders it with `textContent` only.

## Consequences

- One UI codebase for macOS and Wayland; on GNOME/Wayland the window cannot force itself on
  top, so phase 2 leans on the system notification (O2) plus a normal window.
- The server is a local attack surface; the token + Host/Origin checks + loopback bind are
  tested in CI and must stay.
- Whether the collection-behavior tweak is enough to float over a full-screen Zoom window on
  every macOS version is a manual check (see the PR for #10). If it is not, the fallback is an
  `NSPanel` with `NSWindowStyleMaskNonactivatingPanel` created via pyobjc, still hosting the
  same web page.
- pywebview (BSD-3-Clause) pulls pyobjc on macOS only; aiohttp is Apache-2.0.
