"""Approval overlay and trigger notifications (PRD O1, O2).

Owns the L1/L2 surface: a small always-visible window that shows the suggested
answer, the excerpt that fired the trigger and the frames that were sent, and lets
the user approve, edit or discard it with the keyboard. It is also where the
"approved without edit" metric starts: every decision is a ``UserAction``.

The UI is a local web page (see ``docs/adr/0010-ui-local-web-overlay.md``): an
aiohttp server bound to ``127.0.0.1`` on a random port, guarded by a per-session
token, pushes state over a websocket. On macOS the page is shown in an always-on-top
pywebview window (``sombra.ui.window``, imported lazily); anywhere else it can be
opened in a browser.
"""

from sombra.ui.overlay import OverlayUI
from sombra.ui.state import Card, CardKind, OverlayState, ProtocolError

__all__ = ["Card", "CardKind", "OverlayState", "OverlayUI", "ProtocolError"]
