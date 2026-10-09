# Demo path (recording at about 05:15)

The story in one line: a reel goes in, the creature writes criteria before any attempt, forges in
rounds while looking at its own frames next to the reel's, installs a sealed skill, replays a learned
design on new text at $0, names what it lacks, waits for a human on an ASK, and the board shows every
run of the night with its cost. Authority stays the same: every run prints `authority unchanged: True`.

Each step says whether it costs money and how it was verified (QA, 9 Oct, ~02:40; board and montage
notes updated ~03:50 on 87702bf, 345 tests green). Nothing below was run in a live home: the $0 steps
were verified on a temporary copy of `creature-ukazka`'s registry.

## 0. Before the camera

- Docker running, workshop image built (`docker image inspect creature-workshop:v1`).
- The board loop up (one terminal, keep it running): `scripts/board-loop.sh` (finds every home again each
  10 s, so a new home appears by itself), or by hand
  `uv run python -m creature.board --every=10 ~/Desktop/creature-homes/board.html ~/Desktop/creature-homes/*/`
  then open `~/Desktop/creature-homes/board.html` in a browser. The page never reloads: every 10 s it
  pulls `board-data.js` (written next to it) and patches itself in place, so a playing clip keeps playing
  and the tab you picked stays. Keep the two files next to each other.
- The library closed (no more files added to `~/Desktop/creature-assets`): a change mid-run refuses the
  install (fingerprint part "asset library"), which is correct and not what you want on camera.
- The tree frozen: the fingerprint also covers the enforcing modules (`authority.py`, `registry.py`,
  `verdict.py`, …). A commit touching one of them while a run is going prints `authority unchanged: False`
  with the file named (QA saw exactly that once at 02:04, a $0 replay during the builder's commit). Nobody
  edits `creature/` while the camera runs.
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
- If an installed skill already does it (`HAVE`), its clip must also match this reel's orientation and
  rhythm (and colours, when it was learned for its look); otherwise the reel is taken apart and learned.
  A reel with 6 or more cuts is matched part by part after the criteria, not as a whole. A montage is as
  long as the reel (up to 30 s) and cut where the reel cuts; its first try follows the reel's own shots.
- A usage-limit answer from the CLI ("You've hit your session limit") stops the run at once instead of
  burning attempts: `FAILED` with the limit named in the gap. Check `/usage` before recording.
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
  design prints a note that the style is for montages and replays without it (pinned by QA). As of
  03:50 no home has a saved timeline yet (`mikro-s-knihovnou` was rerunning on f95e2a0: the surfaces
  came from the registry at $0.11, the montage itself had not passed). If one finishes, `creature list`
  shows it as `design: <name>` and this step is $0. If none finishes, skip the style on camera or say
  it is for montages.
- A second montage of the same kind never overwrites the first: it is saved as `<slug>-2` (pinned).
- **Verified 02:07 on a copy of `opus-Db3DFh8Nr5y`:** `design radiant-brand-montage --text "Capabilities
  may grow. Authority may not." --style examples/style-martin.json --mark <png>` → `DONE`, 36 s, 0 model
  calls, $0.0000, all checks passed, `authority unchanged: True`. Use the saved home's own four surfaces.
- **Texts of at least two words.** `type-specimen-card` splits the text into two blocks and dies on a
  one-word text (`"Frankenstein."` → `4 of 4 render workers failed`, at any length; `"From Dusk Till
  Dawn."` passes even at 3.8 s). Replay texts on camera: two short sentences, as the learned one.

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

`~/Desktop/creature-homes/board.html`: one section per reel, the reel run most recently on top. A reel
run several times (in any home) is one section with tabs `V1..Vn`, oldest to newest, the newest shown.
Per run: the source video, the input text and reel frames, the output clip or the latest pairs sheet,
time, dollars, what was learned, the last steps and the gap; a run that died without an end says so
(`STOPPED`, no output, the last step is in the list). Totals on top: runs, running, learned, spent.
Numbers come from the ledgers only (QA-tested: spent = Σ cost_usd, learned = installed events,
RUNNING = no run_end and something new within 15 min). A broken ledger shows as a red line on the page,
the rest of the page stays.

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
