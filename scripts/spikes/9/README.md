# Spike 9 harness

Throwaway code for issue #9. The report is in `docs/spikes/9-prompt-cache.md`.

- `meeting.py`: deterministic synthetic 1 h meeting (transcript, 10 triggers, 6 frames).
- `run_spike.py`: replays it against the Messages API arms (5m, 1h, 5m + keep-alive) and the Agent SDK arm, then prints markdown tables.

```sh
# measured (~60 min, needs a key)
ANTHROPIC_API_KEY=... uv run --with pillow --with claude-agent-sdk python scripts/spikes/9/run_spike.py

# offline cache/cost model (no key, seconds)
uv run --with pillow python scripts/spikes/9/run_spike.py --dry-run

# one arm, 10x faster, to smoke-test the harness (TTL results are meaningless when compressed)
ANTHROPIC_API_KEY=... uv run --with pillow python scripts/spikes/9/run_spike.py --arms messages-1h --time-scale 0.1
```

`pillow` draws the synthetic frames and `claude-agent-sdk` is only needed for the `agent-sdk` arm. Neither is a project dependency.
