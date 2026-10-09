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

**From a script** (`creature make --script lines.txt`): one line per shot. One cheap model call picks a
learned skill for each line from a catalog where every skill carries a style card (its background,
main colours, length, how much of the frame the text takes, how much moves), measured by fixed code
from two renders of the skill. Each shot plays its skill's whole learned length with its line; one judge
call with fixed criteria (every line readable, nothing over it, the shots feel like one piece) decides,
and a failed result is composed again at most twice. No builder call: nothing new is learned there.

Every run and every installed skill gets a `POSTUP.md`, written by fixed code from the run's ledger:
source, what it saw and how it meant to make it, the rounds, the result and the cost. Hidden criteria
never appear in it. For now the creature only writes it; it does not read it.

## Who wrote what

| The creature, at run time | The spine, written by us during the event |
|---|---|
| the drawing code of every skill | the loop, the container, the fixed checks |
| scripts that measure the reel | the judge's instructions, the registry, the caps |
| the choice of what to reuse and what to learn | the cut that joins screens into a montage |
| version 2 of a skill that fails on new input | the sound layer |
| a tool it wished for after a failure (`loop-seam-check`) | |
| which skill plays which line of a script (`make`) | the style cards' measuring code, `POSTUP.md` |

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
uv run python -m creature make --script lines.txt --style style.json      # one cheap call + one judge
uv run python -m creature.ui ~/creature-homes      # a local page: a home's skills, make, replay
```

The results below used `examples/authority-deep-assets.json` (Opus builder, up to 30 rounds, $4 per run,
the asset library mounted).

## The night in numbers

All from the run ledgers of every home of the night (30 homes, the early test homes included):
174 runs, 152 of them finished; 116 skills installed by finished runs (113 distinct). Evidence:
`scripts/outcomes.py` and `scripts/night.py` over every ledger, output in `docs/outcomes.json` and
`docs/night.json`. The live board counts differently (it also reads the registries), so it shows more.

| Result | Runs | Example |
|---|---|---|
| BUILT: learned, tested, installed | 34 | `11-illdari`: a new skill `elastic-bounce-overshoot`, $0.27 |
| HAVE: done with a skill it already had | 8 | `13-grafos.anima`: a skill learned on another reel, $0.006 |
| DONE: a design replay, `make`, practice, cards | 10 | `make` on a 7-line script, $0.016 |
| ASK: needs something it may not take | 6 | a 3D low-poly highway explainer: asks for 3D |
| SKIP: nothing to learn | 4 | `02-io_klik`: a tool's promo, $0.06 |
| STOPPED: no end in its ledger, stopped by hand (10 at 03:25–03:31, 12 while building the evening before) | 22 | `12-motion_mirror` in the ok batch |
| FAILED, infrastructure: timeout under load (9), container cut off (7), our bugs (11) | 27 | `09-yuk.aji`: a screen's render killed by the timeout while 13 runs shared the machine |
| FAILED, a real failure: the judge, a fixed check, the budget, its own code | 63 | `03-artbyjay`: a colour covering 17 % of the clip is not one of the reel's |

Our bugs among the 27: screens rendered past their learned length (9, fixed in `32b2d11`), a preview
image over the model's 5 MB cap (1, fixed), the montage code (1). The ok batch of 12 reels: 1 BUILT,
1 HAVE, 2 SKIP, 1 STOPPED, 3 real failures, 5 failures from the infrastructure.

- Learning a simple effect: about $0.54 (median of 11)
- Learning a reel of several screens: $0.32 to $3.82 (40 runs)
- The same reel again, screens taken from the registry, no builder call: $0.08 to $0.12 (24 runs;
  example: $3.26 → $0.10)
- Replaying a saved design on new text or a new style: $0.000, no model call
- A 7-line script with `make` on 14 learned screens: $0.016, passed on the second composition. It
  chose one screen for all seven lines: of the 14, five do not draw the user's text at all (their card
  says the text takes 0 % of the frame)
- Practice, once, $4 cap: it went back to 3 of its failures and none passed the second time ($3.81).
  A return without new information does not help.
- Costs count model calls only; the Apify download of a reel is not in them
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
- Asset library use by the creature: 26 of 116 installed skills declare library files: 23 use its
  fonts, 3 its glows, 2 its photos, 2 its textures.
- Reacts to its input: rendered on two different texts, 94 of 112 installed skills change and 18
  keep their text fixed (one more failed to render on the long text). `scripts/reacts.py`,
  `docs/reacts.json`.
- A montage copies the reel's rhythm; with fast cuts between light and dark screens it can flash more
  than 3 times a second.
- A screen is learned whole, text and layout together. Some screens keep text from the reel they were
  learned on, and a screen cannot lend just its title or its motion to another.
- Video only. Outside motion design nothing can be verified today.
- There is no static filter on generated code. The boundary is the container.

## Supplied by the author

An asset library mounted read-only (339 items, own and open-licensed), a style file,
and some reels that are the author's own videos. Third-party reels are credited, not redistributed.

The asset library is not part of this repository; the list of its items, sources and licences is in
`docs/assets/` (`manifest.json`, `LICENSES.md`).

Tooling: Claude Code, Docker, Apify, local Whisper.

## License

MIT
