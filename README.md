# creature

An agent that grows its own capabilities from short videos, while its authority stays fixed.

> Built from scratch at **From Dusk Till Dawn #01** (Agents 0.0.7, Prague, 8–9 Oct 2026), track **Frankenstein**.
> Work in progress: this README grows with every feature.

## Status

| Part | State |
|---|---|
| Repository skeleton | done |
| Workshop: container with Python, ffmpeg, Pillow, numpy and fonts, one work folder, limits from `authority.json` | done |
| Run ledger (hash chain) and authority fingerprint | done |
| Model call: isolated, budget-capped, logged, reads images (Haiku 5.5, fallback 4.5) | done |
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

**Known limits**

- The run ledger is a hash chain: editing, removing or reordering a line is detected. Cutting lines off
  the end leaves a valid shorter chain; only the `run_end` event shows that a run finished.
  The chain proves the file was not altered, not who wrote it: it has no secret key. Generated code never
  touches `runs/`, so for our threat model that is enough.
- A run can overshoot its model budget by at most one call. The spine refuses a call when the budget
  left is below a reserve, but the CLI checks `--max-budget-usd` only after a turn, so one call cannot be
  cut short; a call over its cap still costs and returns nothing.
- There is no static filter on generated code. The boundary is the workshop container: no network,
  a read-only image, no keys or host environment, one work folder of fixed size, an unprivileged user,
  no capabilities, and limits on time, memory, processes and output. Generated code never runs on the
  host. A filter in front of it would only reject code the container already makes harmless.

## Tooling used to build it

Claude Code (with the author's own skills), Docker, Apify, ElevenLabs, local Whisper.
The creature itself is written from scratch during the event.

## License

MIT
