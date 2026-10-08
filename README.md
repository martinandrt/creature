# creature

An agent that grows its own capabilities from short videos, while its authority stays fixed.

> Built from scratch at **From Dusk Till Dawn #01** (Agents 0.0.7, Prague, 8–9 Oct 2026), track
> **Frankenstein**: *build an agent that can build itself: recognize what capability it's missing,
> create it, test it, install it, and use it again later.*

The task is always the same: **"try on my input what this reel shows"**. The domain tonight is motion
design (typewriters, kinetic type, title cards, grids, charts, brand montages). For every reel the
creature decides what success looks like *before* it tries, then tries with what it already has, and
only when that fails it writes the missing skill, tests it, installs it and uses it again later.

## The loop

| Step | Who | What happens |
|---|---|---|
| Perceive | spine (fixed code) | the reel's frames (24, half picked by cuts), measured cut rhythm and palette, transcript by local Whisper |
| Plan | model (Haiku) | does an installed skill already do this? It is then judged by **its own stored tests** (HAVE) |
| Criteria | model (Opus) | before any attempt: the effect, 3–6 criteria (about a third held out from the forge), params with the transcript sentence they come from; or ASK / REFUSE / SKIP |
| Decompose | model (Opus) | a montage reel splits into graphic surfaces (each learned and judged on its own) and gaps (photos, 3D: ASK) |
| Forge | model (Haiku) | writes `skill.py`, at most 3 attempts, sees only visible criteria and what failed |
| Workshop | spine | runs the script in a container: no network, no keys, read-only image, one work folder |
| Verdict | spine + model | fixed file checks first (format, exact frame count, not black, colours and rhythm against the reel, smooth motion), then a separate judge that sees reel frames, clip frames and criteria, never code |
| Install | spine | sealed in the registry (`SKILL.md`, `capability.json`, `skill.py`, `tests.json`, reference frames), versions kept |
| Evolve | spine | a skill that fails its own tests on new input gets v2, judged by the same tests |
| Compose | model + spine | the creature picks learned surfaces, rhythm and a mark; fixed code cuts them into a timeline (`sequence`, `layer`) |
| Replay | spine | a saved design (recipe as data) runs on new text with **no model call and $0** |
| Wishes | model (Haiku) | reads its own ledgers and names what it lacks, with how many reels each would unlock |
| Tools | model + spine | builds a wished tool that returns data; cases rendered by fixed code, answers compared exactly |

**Capabilities may grow. Authority may not.** `authority.json` holds the budget, the caps per step, the
workshop limits, the models and the ASK and REFUSE lists. Only a human edits it. A fingerprint over
`authority.json`, the workshop image and the enforcing code is taken at the start of every run and again
right before install; when it changed, nothing is installed. Every step and every dollar is written to an
append-only, hash-chained ledger per run, and every run gets a page with the reel next to the clip, the
verdict in one sentence, every model call with its cost, and the commands that replay it.

## Run

Needs Docker, [uv](https://docs.astral.sh/uv/) and Node (the Claude Code CLI is pinned and run via
`npx @anthropic-ai/claude-code@2.1.294`; it must be logged in). Local Whisper (`whisper-cli` and a ggml
model at `~/whisper-models/` or `$CREATURE_WHISPER_MODEL`) is optional.

```bash
uv sync
export CREATURE_HOME=~/creature-home           # state: authority.json, registry/, runs/, queue/
mkdir -p $CREATURE_HOME/workshop
cp authority.json $CREATURE_HOME/ && cp workshop/Dockerfile $CREATURE_HOME/workshop/   # image builds on first run

uv run python -m creature try reel.mp4 --text "Capabilities may grow. Authority may not."
uv run python -m creature try https://www.instagram.com/reel/XXXX/ --text "..."   # needs CREATURE_SECRETS (Apify)
uv run python -m creature list                 # what it can do: skills, tools, designs
uv run python -m creature design <name> --text "..." [--style style.json] [--mark logo.png]   # no model, $0
uv run python -m creature wishes               # what it lacks, from its own runs
uv run python -m creature tool                 # build the first wished tool
uv run python -m creature use <tool> clip.mp4  # use it later, no model
uv run python -m creature queue                # ASK requests waiting for a human
uv run python -m creature overview             # one page with every run
```

A local `.mp4` may have a sidecar `<name>.json` with `caption` (and `transcript`). Keys are read only from
the file named by `$CREATURE_SECRETS` and never reach the workshop or a page.

Checks: `uv run pytest && uv run ruff check . && uv run ruff format --check .` — offline by default (a
scripted fake model, no network, no secrets, a temporary registry per test); container tests are marked
`docker`.

## What is real, measured or missing

Real: every number below comes from the run ledgers of the night, every clip was rendered by code the
creature wrote, every verdict came from fixed checks plus a separate judge.

<!-- night numbers are filled in from the ledgers at the end of the night -->

**Known limits**

- **It composes clips, not capabilities.** A skill cannot call another skill; montage is fixed spine code
  over finished clips, and the creature only chooses what goes in. Next: registry mounted read-only in
  the workshop and declared dependencies in the manifest.
- **Video only.** The workshop and the judge are built for clips; outside motion design nothing can be
  verified today.
- **Looks are judged loosely.** Colours and cut rhythm are measured against the reel; layout and density
  are judged by a model from sampled frames. A clip can pass and still not look like the reel.
- **Photos, 3D renders and footage** are gaps: the creature asks for them (ASK), it does not fake them.
- **Smoothness** is measured from frame differences: it catches a dropped or held frame, not every bad ease.
- The run ledger is a hash chain: editing, removing or reordering a line is detected. Cutting lines off
  the end leaves a valid shorter chain; only the `run_end` event shows that a run finished. The chain has
  no secret key: it proves the file was not altered, not who wrote it.
- A run can overshoot its model budget by at most one call: the CLI checks `--max-budget-usd` only after
  a turn, so the spine refuses a call when less than a reserve is left; a call over its cap still costs.
- There is no static filter on generated code. The boundary is the workshop container: no network, a
  read-only image, no keys or host environment, one work folder of fixed size, an unprivileged user, no
  capabilities, limits on time, memory, processes and output. Generated code never runs on the host.

## Tooling used to build it

Claude Code, Docker, Apify, local Whisper. The creature itself was written from scratch during the event.

## License

MIT
