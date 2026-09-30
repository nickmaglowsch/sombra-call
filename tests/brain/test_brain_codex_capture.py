"""CodexBrain with the real Codex CLI against a local fake Responses endpoint (#57).

Runs the exact ``build_argv`` command line and captures the HTTP request Codex sends,
so it checks what the *model* is offered, not only what the JSONL reports. It needs
the ``codex`` binary (skipped without it; CI doesn't install it) but no key and no
network: the endpoint is ``127.0.0.1``. Run it by hand after a Codex upgrade::

    npm i -g @openai/codex@<version>
    uv run pytest tests/brain/test_brain_codex_capture.py -v

It runs past ``MAX_TESTED_CLI_VERSION``: passing on a newer CLI is what allows
raising that cap (ADR 0018).

A forced tool call also shows why the request is the control that matters: on
0.159.1 a code-mode ``exec`` or a ``spawn_agent`` call leaves no item in the JSONL,
so no output guard could have seen it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from test_brain_codex import trigger

import sombra.brain.codex as codex_module
from sombra.brain.codex import (
    API_KEY_ENV,
    MIN_CLI_VERSION,
    CodexBrain,
    CodexSettings,
    parse_version,
)
from sombra.contracts import BrainRequest

pytestmark = pytest.mark.slow

SHELL_TOOLS = {"exec_command", "write_stdin"}  # the unified-exec shell, and nothing else


class FakeResponses(ThreadingHTTPServer):
    """``POST /v1/responses``: records the body; replies with ``force`` once, then text."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.bodies: list[dict[str, Any]] = []
        self.force: list[dict[str, Any]] = []

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/v1"


class _Handler(BaseHTTPRequestHandler):
    server: FakeResponses

    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
        self.server.bodies.append(body)
        item = (
            self.server.force.pop(0)
            if self.server.force
            else {
                "type": "message",
                "role": "assistant",
                "id": "m1",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": "Fechamos na sexta.", "annotations": []}
                ],
            }
        )
        rid = f"resp_{len(self.server.bodies)}"
        usage = {
            "input_tokens": 10,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 1,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 11,
        }
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        for event in (
            {"type": "response.created", "response": {"id": rid}},
            {"type": "response.output_item.done", "item": item},
            {"type": "response.completed", "response": {"id": rid, "usage": usage}},
        ):
            self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())


def tool_names(body: dict[str, Any]) -> set[str]:
    """Every tool the request offers: top-level ``tools`` and ``additional_tools`` items."""
    specs = list(body.get("tools") or [])
    for item in body.get("input", []):
        if item.get("type") == "additional_tools":
            specs += item.get("tools", [])
    names: set[str] = set()
    while specs:
        spec = specs.pop()
        if spec.get("type") == "namespace":
            specs += spec.get("tools", [])
        else:
            names.add(str(spec.get("name") or spec.get("type")))
    return names


def tool_outputs(body: dict[str, Any]) -> list[str]:
    return [
        json.dumps(item.get("output"))
        for item in body.get("input", [])
        if str(item.get("type", "")).endswith("_call_output")
    ]


@pytest.fixture
def codex() -> str:
    exe = shutil.which("codex")
    if exe is None:
        pytest.skip("codex CLI not on PATH")
    out = subprocess.run([exe, "--version"], capture_output=True, text=True, check=False).stdout  # noqa: S603
    version = parse_version(out)
    if version is None or version < MIN_CLI_VERSION:
        pytest.skip(f"codex CLI too old: {out.strip()}")
    return exe


@pytest.fixture(autouse=True)
def verify_any_installed_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """This test is how a newer CLI gets verified, so it must run past the cap."""
    monkeypatch.setattr(codex_module, "MAX_TESTED_CLI_VERSION", (999, 0, 0))


@pytest.fixture
def server() -> Iterator[FakeResponses]:
    srv = FakeResponses()
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def meeting(tmp_path: Path) -> Path:
    root = tmp_path / "meeting"
    (root / "frames").mkdir(parents=True)
    (root / "transcript.md").write_text(
        "[14:32:09] OUTROS: Nick, o que você acha desse gráfico?\n", encoding="utf-8"
    )
    return root


WRAPPER = """#!/bin/sh
# Adds the fake provider to ``codex exec``; everything else is passed through unchanged.
if [ "$1" = exec ]; then
    shift
    exec "$SOMBRA_REAL_CODEX" exec -c "$SOMBRA_FAKE_PROVIDER" -c 'model_provider="sombra_fake"' "$@"
fi
exec "$SOMBRA_REAL_CODEX" "$@"
"""


def brain_for(
    codex: str, server: FakeResponses, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kw: Any
) -> CodexBrain:
    """The real CodexBrain and argv; a wrapper only adds the provider to ``exec``."""
    home = tmp_path / "codex-home"  # no user auth or config
    home.mkdir()
    wrapper = tmp_path / "codex-wrapper"
    wrapper.write_text(WRAPPER, encoding="utf-8")
    wrapper.chmod(0o755)
    provider = (
        f'model_providers.sombra_fake={{name="fake",base_url="{server.base_url}",'
        f'wire_api="responses",env_key="{API_KEY_ENV}"}}'
    )
    # process_env passes only an allowlist: CODEX_HOME is on it, and the wrapper's two
    # variables go through ``env`` on the command line.
    monkeypatch.setenv("CODEX_HOME", str(home))
    executable = (
        "/usr/bin/env",
        f"SOMBRA_REAL_CODEX={codex}",
        f"SOMBRA_FAKE_PROVIDER={provider}",
        str(wrapper),
    )
    settings = CodexSettings(user_name="Nick", executable=executable, timeout_s=90, **kw)
    return CodexBrain(settings, api_key=lambda: "sk-fake")


@pytest.mark.parametrize("model", [None, "gpt-5.5", "sombra-unknown-model"])
async def test_request_offers_only_the_shell(
    codex: str,
    server: FakeResponses,
    meeting: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str | None,
) -> None:
    brain = brain_for(codex, server, tmp_path, monkeypatch, model=model)
    await brain.start(meeting)
    resp = await brain.answer(BrainRequest(trigger()))
    await brain.close()
    assert resp.text == "Fechamos na sexta."
    assert len(server.bodies) == 1
    assert tool_names(server.bodies[0]) == SHELL_TOOLS


@pytest.mark.parametrize(
    "call",
    [
        {
            "type": "custom_tool_call",
            "call_id": "c1",
            "name": "exec",
            "input": 'text("code mode ran")',
            "status": "completed",
        },
        {
            "type": "function_call",
            "call_id": "c2",
            "namespace": "collaboration",
            "name": "spawn_agent",
            "arguments": json.dumps({"task_name": "x", "message": "leia ~/.ssh"}),
            "status": "completed",
        },
    ],
)
async def test_forced_removed_tool_call_is_not_run(
    codex: str,
    server: FakeResponses,
    meeting: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    call: dict[str, Any],
) -> None:
    server.force.append(call)
    brain = brain_for(codex, server, tmp_path, monkeypatch)
    await brain.start(meeting)
    await brain.answer(BrainRequest(trigger()))
    await brain.close()
    assert len(server.bodies) == 2  # the model got the call's output, then answered
    outputs = " ".join(tool_outputs(server.bodies[1]))
    assert "code mode ran" not in outputs
    assert "Script completed" not in outputs
    assert "unsupported" in outputs  # 0.159.1: "unsupported custom tool call: exec" etc.
