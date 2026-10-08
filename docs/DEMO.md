# Demo path (recording at about 05:15)

The story in one line: a reel goes in, the creature writes criteria before any attempt, forges in
rounds while looking at its own frames next to the reel's, installs a sealed skill, replays a learned
design on new text at $0, names what it lacks, waits for a human on an ASK, and the board shows every
run of the night with its cost. Authority stays the same: every run prints `authority unchanged: True`.

Each step says whether it costs money and how it was verified (QA, 9 Oct, ~02:40). Nothing below was
run in a live home: the $0 steps were verified on a temporary copy of `creature-ukazka`'s registry.

## 0. Before the camera

- Docker running, workshop image built (`docker image inspect creature-workshop:v1`).
- The board loop up (one terminal, keep it running):
  `uv run python -m creature.board --every=10 ~/Desktop/creature-homes/board.html ~/Desktop/creature-homes/*/`
  then open `~/Desktop/creature-homes/board.html` in a browser (it refreshes itself every 10 s).
- The library closed (no more files added to `~/Desktop/creature-assets`): a change mid-run refuses the
  install (fingerprint part "asset library"), which is correct and not what you want on camera.
- Keys: `CREATURE_SECRETS` points at the file with `APIFY_TOKEN=…` (Instagram reels only; a local `.mp4`
  needs none).

## 1. A clean home — the registry is empty on screen ($0, verified)

```bash
export CREATURE_HOME=~/Desktop/creature-homes/zaznam        # a new folder, never used before
mkdir -p $CREATURE_HOME/workshop
cp examples/authority-deep-knihovna.json $CREATURE_HOME/authority.json   # Martin's caps + the library
cp workshop/Dockerfile $CREATURE_HOME/workshop/
uv run python -m creature list      # prints nothing: no skills, no designs
uv run python -m creature queue     # prints nothing: nothing waits for a human
```

## 2. The reel goes in — criteria, rounds, install (PAID, live)

```bash
uv run python -m creature try <reel URL or .mp4> --text "Capabilities may grow. Authority may not."
```

- What happens, in order: perceive (24 labelled frames, cuts, palette) → criteria written before any
  attempt (Opus, ≤ $0.20; one or two are held out from the forge) → rounds (Opus, ≤ $0.25 each; the
  creature sees pairs of frames, reel left, its own right, and edits its files) → the whole clip through
  the fixed checks and the judge → the same clip 2 s shorter → install, sealed → `BUILT` and
  `authority unchanged: True`.
- Money and time (from last night's ledgers): rounds ran $0.09–0.13 each; a surface took 2–10 rounds;
  one deep run stays under its $4.00 budget by construction (the budget ends the rounds).
- While it runs, the board shows the run first, `RUNNING`, with its latest pairs sheet (reel | output)
  and the last steps (`kolo N: …`, scores, `nainstalováno …`).
- Pick the reel from the batch that behaved: `lilium-s-knihovnou` or `mikro-s-knihovnou` have their
  ledgers; `creature-test` after the batch ends. Do not reuse their homes for the recording: the story
  needs an empty registry at the start.

## 3. What it learned ($0, verified)

```bash
uv run python -m creature list                 # skill, version, what it cost to learn, what it draws
uv run python -m creature show <slug>          # SKILL.md, capability.json (assets it reads), sealed tests
open $CREATURE_HOME/runs/<run>/page.html       # reel frames | clip frames, criteria and verdict, cost
```

The run folder also holds `rounds/round-NN/pairs.png` (what the creature saw each round) and
`rounds/round-NN/files.json` (its files after that round).

## 4. Replay a learned design on new text — $0, no model call (verified, 7 s)

```bash
uv run python -m creature design <design> --text "A different line."
# prints:  DONE  design:<design>  /  spent: $0.0000  authority unchanged: True  /  clip: …
```

Verified on a copy of `creature-ukazka`: `design intro --text "Capabilities may grow. Authority may not."`
chained two learned skills (typewriter-caret, word-pop-color-flip) into one joined clip in 7 s, zero
model calls in the ledger. A design is saved for every learned skill under its own name, and
`creature compose <name> <part> <part>` makes a new one from existing ones ($0).

## 5. Martin's style file — only on a montage design (depends on a finished montage run)

```bash
uv run python -m creature design <timeline design> --text "…" --style examples/style-martin.json [--mark logo.png]
```

- The style (colours, fonts) and the mark apply to a **timeline** design (a montage). A plain step
  design ignores them; as of 02:40 no home has a saved timeline yet (the montage runs in
  `lilium-s-knihovnou` and `mikro-s-knihovnou` were still composing). If one finishes, `creature list`
  shows it as `design: <name>` and this step is $0. If none finishes, skip the style on camera or say
  it is for montages.
- QA pin pending: `--style` on a step design should print a note instead of silently doing nothing.

## 6. What it lacks (PAID, one small call, ~$0.03)

```bash
uv run python -m creature wishes
```

Reads every run of this home that ended without a skill (and every skipped surface and gap), groups
them into tool / effect / input / authority wishes, sorted by how many reels each would unlock; writes
`wishes.json` and a page. Best on a home with a few failures: run it on `lilium-s-knihovnou` or
`creature-test` rather than the fresh recording home (one BUILT run gives an empty list).

## 7. The ASK queue (depends on a run that asked)

```bash
uv run python -m creature queue      # run · status · effect · what it needs
```

An ASK appears when the criteria model says a reel needs something the creature may not do or does not
have (photos, 3D, footage, network). As of 02:40 no home had one waiting. Before recording, run `queue`
over the homes (`for h in ~/Desktop/creature-homes/*/; do echo $h; uv run python -m creature --home $h
queue; done`) and point the camera at the one that has it; or feed `try` a reel that is plainly a photo
montage and let it ask (PAID, outcome not guaranteed).

## 8. The board ($0, verified)

`~/Desktop/creature-homes/board.html`: every home and run of the night, `RUNNING` runs first, then
newest; per run the input text and reel frames, the output clip or the latest pairs sheet, time, dollars,
what was learned, the last steps and the gap. Totals on top: runs, running, learned, spent. Numbers come
from the ledgers only (QA-tested: spent = Σ cost_usd, learned = installed events, RUNNING = no run_end
and something new within 15 min).

## 9. Pages, rebuilt from the ledger ($0, verified)

```bash
uv run python -m creature page <run id>        # one run's side-by-side page again
uv run python -m creature overview             # every run of this home
```

## Order on camera

1 clean home → 2 `try` (talk over the rounds; the board on the second screen) → 3 `list`, `show`, the
run page → 4 `design` replay at $0 → 5 style (if a timeline exists) → 6 `wishes` → 7 `queue` → 8 board
totals. Keep the three live steps (2, 6, 7) to the homes named above; everything else is $0 and
repeatable.
