"""Overlay state: queueing order and the client-message → UserAction mapping."""

import json

import pytest

from sombra.contracts import ActionKind, Suggestion, UserAction
from sombra.ui import CardKind, OverlayState, ProtocolError


def _suggestion(n: int, text: str = "resposta", frames: tuple[str, ...] = ()) -> Suggestion:
    return Suggestion(
        id=f"s{n}",
        trigger_id=f"t{n}",
        text=text,
        excerpt=f"Nick, pergunta {n}?",
        frames_sent=frames,
    )


def _action(sid: str, kind: str, **extra: object) -> str:
    return json.dumps({"type": "action", "suggestion_id": sid, "kind": kind, **extra})


def test_newest_card_is_on_top() -> None:
    st = OverlayState()
    st.suggestion(_suggestion(1))
    st.suggestion(_suggestion(2))
    st.trigger("t3", "Nick, e agora?")
    assert [c.id for c in st.cards()] == ["t:t3", "s:s2", "s:s1"]


def test_suggestion_replaces_its_searching_card_and_moves_to_top() -> None:
    st = OverlayState()
    st.trigger("t1", "Nick, pergunta 1?")
    st.trigger("t2", "Nick, pergunta 2?")
    st.suggestion(_suggestion(1))
    cards = st.cards()
    assert [c.id for c in cards] == ["s:s1", "t:t2"]
    assert cards[0].kind is CardKind.SUGGESTION


def test_failure_replaces_searching_card() -> None:
    st = OverlayState()
    st.trigger("t1", "Nick, pergunta 1?")
    st.failure("t1", "Nick, pergunta 1?", "timeout")
    [card] = st.cards()
    assert card.kind is CardKind.FAILURE
    assert card.reason == "timeout"
    assert card.excerpt == "Nick, pergunta 1?"


def test_snapshot_shape() -> None:
    st = OverlayState()
    st.suggestion(_suggestion(1, "a resposta", ("f0001", "f0002")))
    snap = st.snapshot()
    assert snap["type"] == "state"
    assert snap["cards"] == [
        {
            "id": "s:s1",
            "kind": "suggestion",
            "trigger_id": "t1",
            "suggestion_id": "s1",
            "excerpt": "Nick, pergunta 1?",
            "text": "a resposta",
            "frames": ["f0001", "f0002"],
            "reason": "",
        }
    ]
    json.dumps(snap)  # serialisable


@pytest.mark.parametrize(
    ("kind", "extra", "expected"),
    [
        ("approve", {}, UserAction("s1", ActionKind.APPROVE, "resposta")),
        (
            "edit",
            {"text": "  outra resposta "},
            UserAction("s1", ActionKind.EDIT, "outra resposta"),
        ),
        ("discard", {}, UserAction("s1", ActionKind.DISCARD, None)),
        ("not_for_me", {}, UserAction("s1", ActionKind.NOT_FOR_ME, None)),
    ],
)
def test_each_button_maps_to_its_action(
    kind: str, extra: dict[str, object], expected: UserAction
) -> None:
    st = OverlayState()
    st.suggestion(_suggestion(1))
    assert st.handle(_action("s1", kind, **extra)) == expected
    assert st.cards() == []


def test_edit_without_changes_counts_as_approve() -> None:
    st = OverlayState()
    st.suggestion(_suggestion(1, "resposta"))
    assert st.handle(_action("s1", "edit", text="resposta\n")) == UserAction(
        "s1", ActionKind.APPROVE, "resposta"
    )


def test_resolving_one_suggestion_leaves_the_others() -> None:
    st = OverlayState()
    st.suggestion(_suggestion(1))
    st.suggestion(_suggestion(2))
    st.handle(_action("s2", "discard"))
    assert [c.id for c in st.cards()] == ["s:s1"]


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[1, 2]",
        json.dumps({"type": "hello"}),
        _action("s1", "explode"),
        _action("nope", "approve"),
        _action("s1", "edit"),
        _action("s1", "edit", text="   "),
        json.dumps({"type": "action", "kind": "approve"}),
        json.dumps({"type": "dismiss", "card_id": "s:s1"}),
        json.dumps({"type": "dismiss", "card_id": "t:missing"}),
        json.dumps({"type": "dismiss"}),
        _action("s1", "edit", text="x" * 70_000),
    ],
)
def test_rejected_messages_change_nothing(raw: str) -> None:
    st = OverlayState()
    st.suggestion(_suggestion(1))
    before = st.snapshot()
    with pytest.raises(ProtocolError):
        st.handle(raw)
    assert st.snapshot() == before


def test_double_click_resolves_once() -> None:
    st = OverlayState()
    st.suggestion(_suggestion(1))
    st.handle(_action("s1", "approve"))
    with pytest.raises(ProtocolError, match="not pending"):
        st.handle(_action("s1", "approve"))


def test_dismiss_searching_and_failure_cards() -> None:
    st = OverlayState()
    st.trigger("t1", "a")
    st.failure("t2", "b", "erro")
    assert st.handle(json.dumps({"type": "dismiss", "card_id": "t:t1"})) is None
    assert st.handle(json.dumps({"type": "dismiss", "card_id": "t:t2"})) is None
    assert st.cards() == []
