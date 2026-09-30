"""The TELA marker title for each kept frame: ``window_title`` → app → ``""`` (#65).

``TimelineStore.append_frame`` writes the index record only (#41); ``Session``
appends exactly one ``FrameMarker`` per kept frame. Wayland often has no window
title (and sometimes no app), so the fallback chain must never leak ``None``.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from orchestrator_harness import BASE, make_rig, until

from fakes import FakeScreenSource
from sombra.contracts import FrameMarker, Screenshot, parse_line

Meta = tuple[str | None, str | None]  # (app, window_title)


def screen_of(metas: list[Meta]) -> FakeScreenSource:
    """One distinct shot per ``metas`` entry, then the last one again (deduped)."""

    def shot(n: int) -> Screenshot:
        i = min(n, len(metas) - 1)
        app, title = metas[i]
        return Screenshot(
            ts=BASE + timedelta(seconds=i), image=f"shot-{i}".encode(), app=app, window_title=title
        )

    return FakeScreenSource(shot)


async def markers_for(tmp_path: Path, metas: list[Meta]) -> tuple[list[FrameMarker], list[str]]:
    rig = await make_rig(tmp_path, screen=screen_of(metas)).start()
    await until(lambda: rig.screen.grabs > len(metas) + 2, what="all shots grabbed")
    await rig.stop()
    assert len(rig.store.frames) == len(metas)
    markers = [e for e in rig.store.entries if isinstance(e, FrameMarker)]
    return markers, [line for line in rig.store.transcript() if " TELA " in line]


@pytest.mark.parametrize(
    ("app", "window_title", "expected"),
    [
        pytest.param("Zoom", "Zoom - Roadmap Q4", "Zoom - Roadmap Q4", id="window-title"),
        pytest.param(None, "Planilha de churn", "Planilha de churn", id="title-without-app"),
        pytest.param("Google Chrome", None, "Google Chrome", id="app-when-no-title"),
        pytest.param("Google Chrome", "", "Google Chrome", id="app-when-empty-title"),
        pytest.param(None, None, "", id="neither"),
        pytest.param("", "", "", id="both-empty"),
    ],
)
async def test_marker_title_falls_back_from_window_title_to_app_to_empty(
    tmp_path: Path, app: str | None, window_title: str | None, expected: str
) -> None:
    markers, lines = await markers_for(tmp_path, [(app, window_title)])

    assert [(m.frame_id, m.window_title) for m in markers] == [("f0001", expected)]
    assert lines == [f'[14:00:00] TELA f0001 "{expected}"']
    assert "None" not in lines[0]
    back = parse_line(lines[0], day=BASE)
    assert isinstance(back, FrameMarker) and back.window_title == expected


async def test_one_marker_per_frame_in_order_with_mixed_metadata(tmp_path: Path) -> None:
    metas: list[Meta] = [
        ("Zoom", "Zoom - Vendas Q3"),
        ("Slack", None),
        (None, None),
        (None, "Figma - Onboarding"),
    ]
    markers, lines = await markers_for(tmp_path, metas)

    assert [(m.frame_id, m.window_title) for m in markers] == [
        ("f0001", "Zoom - Vendas Q3"),
        ("f0002", "Slack"),
        ("f0003", ""),
        ("f0004", "Figma - Onboarding"),
    ]
    for frame_id in ("f0001", "f0002", "f0003", "f0004"):
        assert sum(f" TELA {frame_id} " in line for line in lines) == 1
    assert len(lines) == len(metas)
    assert not any("None" in line for line in lines)
