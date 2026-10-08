# creature

An agent that grows its own capabilities from short videos, while its authority stays fixed.

> Built from scratch at **From Dusk Till Dawn #01** (Agents 0.0.7, Prague, 8–9 Oct 2026), track **Frankenstein**.
> Work in progress: this README grows with every feature.

## Status

| Part | State |
|---|---|
| Repository skeleton | done |
| Everything else | not started |

## Run

```bash
uv sync
uv run pytest && uv run ruff check . && uv run ruff format --check .   # the one check
uv run pytest -m slow                                                   # live model and network tests
```

Tests are offline by default: no network, no model calls (a scripted fake model stands in), no secrets,
and every test gets its own temporary registry. The run fails if the real `registry/` changes during it.
Sandbox tests are marked `docker` and are listed as skipped when the Docker daemon is not reachable.

## What is real, simulated or missing

Filled in honestly as features land.

## Tooling used to build it

Claude Code (with the author's own skills), Docker, Apify, ElevenLabs, local Whisper.
The creature itself is written from scratch during the event.

## License

MIT
