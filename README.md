# creature

An agent that learns motion-design skills from short videos and reuses them later.
Its capabilities grow. Its authority does not.

Built from scratch at **From Dusk Till Dawn #01** (Prague, 8–9 Oct 2026), track **Frankenstein**.

## What it does

The task is always the same: *"try on my input what this reel shows"*.

1. It looks at the reel: frames, cut rhythm, colours.
2. It checks its registry: can an installed skill already do this?
3. If not, it writes down what success looks like before it tries. Part of those criteria stays hidden from the builder.
4. It writes the missing skill as code, in rounds, comparing its own output with the reel.
5. That code runs only in a container with no network and no keys.
6. Fixed checks run first, then a separate judge. Only what passes is installed, with its tests and a version.
7. Next time it reuses the skill without rebuilding it. A saved design replays on new text with no model call, for $0.

When a reel needs photos, 3D or footage, it asks for them (ASK). When there is nothing to learn, it skips.
On command, it returns to its own failures by itself.

## Who wrote what

| The creature, at run time | The spine, written by us during the event |
|---|---|
| the drawing code of every skill | the loop, the container, the fixed checks |
| scripts that measure the reel | the judge's instructions, the registry, the caps |
| the choice of what to reuse and what to learn | the cut that joins screens into a montage |
| version 2 of a skill that fails on new input | the sound layer |
| a tool it wished for after a failure (`loop-seam-check`) | |

The rules of the loop were tuned by a human during the night, from his own verdicts.
The creature does not improve its own method yet.

## Authority

`authority.json` holds the budget per run, the caps per step, the container limits and the ASK and
REFUSE lists. It changes only on a human's decision. A fingerprint of it, the container image and the
enforcing code is taken at the start of a run and again before install; if it changed, nothing is
installed. Every model call and its cost goes to an append-only, hash-chained ledger.

## Run

Needs Docker, uv and Node (Claude Code CLI, logged in).

```bash
uv sync
export CREATURE_HOME=~/creature-home
mkdir -p $CREATURE_HOME/workshop
cp authority.json $CREATURE_HOME/ && cp workshop/Dockerfile $CREATURE_HOME/workshop/
uv run python -m creature try reel.mp4 --text "Capabilities may grow. Authority may not."
uv run python -m creature list
uv run python -m creature design <name> --text "..." --style style.json   # no model, $0
```

The results below used `examples/authority-deep-assets.json` (Opus builder, up to 30 rounds, $4 per run,
the asset library mounted).

## The night in numbers

All from the run ledgers. Evidence: `scripts/night.py` over every ledger, its output in `docs/night.json`.

- Runs: 110 · skills installed: 78 (distinct: 75) · FAILED: 57 · ASK: 6 · SKIP: 3
- Learning a simple effect: about $0.57
- Learning a reel of several screens: $1.69 to $3.56
- The same reel again, screens taken from the registry, no builder call: $0.08 to $0.11 (example: $3.26 → $0.10)
- Replaying a saved design on new text or a new style: $0.000, no model call
- Human check: the author looked at 23 outputs: 2 good, 6 close, 15 not met
- The same reel against a free Claude Code session (LILIUM): the session made it at 9/10 for $9.78; the
  creature, on the closed asset library, learned 2 of 4 screens and ran out of its $4 budget one call
  before composing them ($3.77, no montage)

## Honest limits

- **A pass does not mean it looks like the reel.** The judge is a model looking at sampled frames.
  Our pixel-based audit did not agree with the human eye, so we do not report its number.
- **Reuse across different reels:** shown for screens, not yet for a whole reel. On a reel it had never
  seen (`motion-showreel`), it took 3 of 4 screens from skills learned on two other reels without a
  builder call and learned the fourth; that montage then failed its rhythm check, and a second render crashed.
- A reel is split into at most 4 screens.
- A replay on a one-word text failed: a learned screen expected at least two words.
- A skill cannot call another skill. A montage joins finished clips.
- Sound is fixed code with library sounds, not a learned skill.
- Asset library use by the creature: 16 of 88 installed skills declare library files: 15 use its fonts,
  2 its photos, 1 a texture.
- Video only. Outside motion design nothing can be verified today.
- There is no static filter on generated code. The boundary is the container.

## Supplied by the author

An asset library mounted read-only (339 items, own and open-licensed, see `LICENSES.md`), a style file,
and some reels that are the author's own videos. Third-party reels are credited, not redistributed.

Tooling: Claude Code, Docker, Apify, local Whisper.

## License

MIT
