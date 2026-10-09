# creature

An agent that learns motion-design skills from short videos and reuses them later.
Its capabilities grow. Its authority does not.

Built from scratch at **From Dusk Till Dawn #01** (Prague, 8–9 Oct 2026), track **Frankenstein**.

## What it does

1. Looks at a reel: frames, cut rhythm, colours.
2. Checks its registry: can an installed skill already do this?
3. If not, writes down what success looks like (part of it hidden from the builder), then writes the
   skill as code, in rounds, in a container with no network and no keys.
4. Fixed checks, then a separate judge. Only what passes is installed, with tests and a version.
5. Next time it reuses the skill. A saved design replays on new text with no model call, for $0.

It asks (ASK) when a reel needs photos, 3D or footage, and skips when there is nothing to learn.

**From a script** (`creature make`): one line per shot. One cheap model call picks a learned skill per
line, using a style card measured for each skill by fixed code; one judge call checks the result.

Every run and skill gets a `POSTUP.md` written from its ledger (hidden criteria left out).

## Who wrote what

**The creature, at run time:** the drawing code of every skill, the scripts that measure a reel, what to
reuse and what to learn, version 2 of a failing skill, a tool it asked for (`loop-seam-check`), which
skill plays which line of a script.

**Us, during the event:** the loop, the container, the fixed checks, the judge's instructions, the
registry, the caps, the montage cut, the sound layer, the style cards, `POSTUP.md`.

## Authority

`authority.json` holds budgets, caps, container limits and the ASK/REFUSE lists, and changes only by a
human. Its fingerprint is checked at the start of a run and before install; if it changed, nothing is
installed. Every model call and its cost goes to an append-only, hash-chained ledger.

## Run

Needs Docker, uv and Node (Claude Code CLI, logged in).

```bash
uv sync
export CREATURE_HOME=~/creature-home
mkdir -p $CREATURE_HOME/workshop
cp authority.json $CREATURE_HOME/ && cp workshop/Dockerfile $CREATURE_HOME/workshop/
uv run python -m creature try reel.mp4 --text "Capabilities may grow. Authority may not."
uv run python -m creature design <name> --text "..."    # replay, no model, $0
uv run python -m creature make --script lines.txt       # one cheap call + one judge
uv run python -m creature.ui ~/creature-homes           # local operator page
```

## The night in numbers

From the ledgers of all 30 homes (`scripts/outcomes.py`, `docs/outcomes.json`): 174 runs.

| Result | Runs |
|---|---|
| BUILT: learned, tested, installed | 34 |
| HAVE: done with a skill it already had | 8 |
| DONE: replay, `make`, practice, cards | 10 |
| ASK / SKIP | 6 / 4 |
| STOPPED by hand | 22 |
| FAILED, infrastructure (timeouts under load, container cut off, our bugs) | 27 |
| FAILED, real (judge, fixed check, budget, its own code) | 63 |

- Learning a simple effect: ~$0.54. A reel of several screens: $0.32–$3.82.
- The same reel again from the registry: $0.08–$0.12 (e.g. $3.26 → $0.10).
- Replay on new text: $0. A 7-line script with `make`: $0.016.
- Human check of 23 outputs: 2 good, 6 close, 15 not met.
- Practice (going back to its own failures): 0 of 3 passed, $3.81.
- Costs are model calls only; reel downloads are not counted.

## Honest limits

- A pass does not mean it looks like the reel: the judge is a model on sampled frames.
- A screen is learned whole; 18 of 112 skills keep the reel's text fixed (`docs/reacts.json`).
- Long text overflows; mixing screens from different reels rarely passes.
- Reuse across different reels works for screens, not yet for a whole reel.
- Montages copy the reel's rhythm and can flash more than 3 times a second.
- Video only. No static filter on generated code; the boundary is the container.

## Supplied by the author

A read-only asset library (339 items, own and open-licensed; list in `docs/assets/`), a style file and
some of the author's own reels. Third-party reels are credited, not redistributed.
The library was mounted read-only during runs; 26 of 116 installed skills declare files from it, mostly fonts.
Tooling: Claude Code, Docker, Apify, local Whisper.

## License

MIT
