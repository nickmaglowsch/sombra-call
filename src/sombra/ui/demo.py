"""Fake triggers and suggestions that drive the overlay for manual testing (``sombra ui-demo``)."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Callable
from datetime import datetime

from sombra.contracts import Suggestion, TriggerEvent, UserAction
from sombra.ui.overlay import OverlayUI

# PT-BR, as in a real call. Each round: (question, answer, frames sent, fails?).
SCRIPT: tuple[tuple[str, str, tuple[str, ...], bool], ...] = (
    (
        "Nick, o que você acha desse gráfico?",
        "A queda em agosto bate com a migração do billing; o resto da curva está dentro do "
        "esperado. Eu olharia a coorte de julho antes de mudar a meta.",
        ("f0012", "f0013"),
        False,
    ),
    (
        "Nick, dá pra fechar a entrega na sexta?",
        "Dá, se o review do PR de pagamentos sair até quinta de manhã. Senão, segunda.",
        (),
        False,
    ),
    (
        "Nick, qual era o número de usuários ativos que você mostrou ontem?",
        "",
        (),
        True,
    ),
)


def fake_trigger(n: int, question: str) -> TriggerEvent:
    return TriggerEvent(
        id=f"demo-t{n}",
        ts=datetime.now().astimezone(),
        question=question,
        matched_alias="Nick",
        score=0.9,
        window=(),
        needs_screen=False,
    )


async def drive(
    ui: OverlayUI, *, interval: float = 4.0, think: float = 1.5, rounds: int | None = None
) -> None:
    """Loop over ``SCRIPT``: trigger → (think) → suggestion or failure → (interval)."""
    steps = itertools.cycle(SCRIPT)
    counter = itertools.count(1) if rounds is None else range(1, rounds + 1)
    for n, (question, answer, frames, fails) in zip(counter, steps, strict=False):
        trigger = fake_trigger(n, question)
        await ui.notify_trigger(trigger)
        await asyncio.sleep(think)
        if fails:
            await ui.notify_failure(trigger, "timeout do agente (demo)")
        else:
            await ui.show(
                Suggestion(
                    id=f"demo-s{n}",
                    trigger_id=trigger.id,
                    text=answer,
                    excerpt=question,
                    frames_sent=frames,
                )
            )
        await asyncio.sleep(interval)


async def report_actions(ui: OverlayUI, write: Callable[[str], object]) -> None:
    async for action in ui.actions():
        write(format_action(action))


def format_action(action: UserAction) -> str:
    text = "" if action.text is None else f" {action.text!r}"
    return f"{action.suggestion_id}: {action.kind.value}{text}\n"
