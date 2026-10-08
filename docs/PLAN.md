# creature — build plan

Living document for everyone building tonight. If code and this plan disagree, fix one of them in the same commit.

## 1. Vision

> *"I see what people do with AI on social media and I have no time to find out whether it works."*

The creature does it for me. It tries on my input what a reel shows, compares the result with the original,
and keeps what works as a skill. Its capabilities grow; only I add authority.
A skill is not "I can read a video" but "I can do what the video showed".

**The task, always the same:** "Try on my input what this reel shows."

**Tonight's domain: motion design.** Text and graphic animation: pop-in titles, typewriter, title cards,
lower thirds, transitions, counters. Reels are tutorials in which the author names the effect.
3D, particles, paid tools and generated video are a gap or an ASK.

**Capabilities may grow. Authority may not.**

## 2. What the organizers require (track Frankenstein)

Definition of done, quoted from the brief:

1. A **task** exposes a missing capability.
2. The agent **creates, tests and registers** it, then **completes the task**.
3. It **builds or extends the tooling for discovering and managing** its capabilities.
4. **In a fresh session, a different task combines previously generated capabilities**, without rebuilding or manual wiring.

Hard rules: generated code runs in a sandbox, never on a host holding credentials · no install without passing tests, test run visible in the log · the gap comes from a task, not from a hardcoded "build X" · self-iterations and spend per run capped in code.

Out of bounds: tools pre-written by the team · routing over a fixed tool library · toy capabilities (adding numbers, reversing strings) · rewriting its own core loop without tests · demoing only a harness's built-in features.

Judging: value 35 % · originality 25 % · working end to end 20 % · technical execution 10 % · honest limitations 10 %. Kick-off: cost per use in dollars at list price matters, and so do guardrails.

Deliverables: public GitHub repo + unlisted YouTube video of at most 90 s, "show it working, not slides". Freeze 07:14, snapshot of the latest commit.

## 3. What we verified tonight

| Fact | Measured |
|---|---|
| Docker sandbox: `--network none --read-only --tmpfs /tmp --memory 256m --cpus 1 --pids-limit 64 --cap-drop ALL --user 65534` | network and writes outside `/tmp` blocked, start + run **0.2 s** |
| Isolated `claude -p` (own system prompt, no tools, no MCP, no user settings) | ~630 input tokens instead of ~183k; Haiku 4.5 **$0.0018** per small call |
| `--json-schema` + `--output-format json` | clean object in `structured_output`, cost in `total_cost_usd` (list-price equivalent) |
| `--max-budget-usd`, `--fallback-model`, `--json-schema` | same in 2.1.289 and 2.1.294. `--max-budget-usd` is checked **after** the turn: a call over budget still costs, then ends `error_max_budget_usd` with no output. Our own pre-call refusal is the real cap |
| Start overhead | `npx …@2.1.294` adds **0.8 s** per call; the cached binary it resolves to starts in 0.01 s, so `llm.py` resolves it once per run |
| Haiku 5.5 | needs CLI ≥ 2.1.294 (2.1.289 returns `unrecognized_model`); pinned via `npx @anthropic-ai/claude-code@2.1.294`, `haiku` alias resolves to `claude-haiku-5-5`. Same structured call: 5.5 **$0.00042 / 3.2 s**, 4.5 $0.0030 / 4.4 s (n=1) |
| Apify `apify~instagram-reel-scraper`, sync call | one reel in ~20 s; accepts direct reel URLs; caption, `videoUrl`, `audioUrl` |
| Apify prices (free tier) | reel $0.0026, start $0.001, transcript add-on $0.048, video add-on $0.02 |
| Direct video download + ffmpeg + local Whisper | 1.6 s + 2 s, $0; music-only reels produce hallucinated lyrics → filter |
| ElevenLabs | key works, paid tier (commercial use allowed) |
| Images in the isolated model call | base64 image blocks over `--input-format stream-json`, no tools: read text and colors correctly, $0.0005, 3 s |
| Workshop image (Python 3.12, ffmpeg, Pillow, numpy, DejaVu + Inter fonts) | builds in 45 s, 988 MB; a 5 s 1080×1920 30 fps clip renders in 1.2 s, 1.5–1.8 s with container start, no network, read-only root |
| Judge model on frame strips (reel vs output, 3 criteria) | Haiku 5.5 gave the same verdicts and per-criterion marks as Sonnet 5.5 on a match, a near miss and a wrong technique; $0.001 vs $0.012 per judgement (synthetic clips, re-check on a real reel) |

Background research (two reports, kept outside the repo): tests before install are what makes self-written skills help at all (SkillsBench 2026: untested self-written skills score 8–11.5 pp *below* no skills); an agent edited the signals its evaluator read (Darwin Gödel Machine) so tests and checks must sit outside its reach; 1,184 malicious skills on ClawHub (Feb 2026) show that skill text is an attack surface too; no verified system separates capability growth from authority growth by design.

## 4. Architecture

```
creature/
  authority.json        what the creature may do; edited by a human only; fingerprinted
  workshop/Dockerfile   the workshop image: Python, ffmpeg, Pillow, numpy, fonts; pinned by digest
  creature/             the spine (written tonight, by us)
    cli.py              try · list · show · approve · deny
    authority.py        load, validate, fingerprint (authority.json + workshop image + every enforcing module)
    ledger.py           append-only run log (JSONL, hash chain) with costs
    llm.py              isolated claude -p: JSON schema, images, cost, caps
    perceive.py         reel → video, caption, transcript, frame strips
    criteria.py         reel → effect, success criteria (visible + held out), task, clip format, file checks
    planner.py          task + registry → existing skills to run or chain, or a gap
    forge.py            gap + criteria + last failure → skill code, max 3 attempts
    workshop.py         runs skill code in the workshop container: files in, clip out
    verdict.py          file checks (fixed code, ffprobe) + judge (frames and criteria, never code)
    registry.py         install, version, index, search
    page.py             side-by-side page per attempt, morning overview
    voice.py            the morning overview, spoken (only if time allows)
  registry/             written only by creature runs (starts empty)
  queue/                ASK requests waiting for a human
  runs/                 one ledger and one attempt folder per run
  docs/                 this plan
  tests/
```

**The loop (one `creature try <reel> --input …`)**

1. New process. Load `authority.json`, record the fingerprint, print the registry.
2. Perceive: download the reel, caption, transcript, frame strips (frames are extracted inside the workshop).
3. Criteria, **before any attempt**: the effect, success criteria (some held out from the forge), the task on my
   input, and the clip format (duration, resolution, fps) with file checks as data.
4. Planner: does the registry already do it? Run it. Otherwise it is a gap.
5. Gap: does it fit the authority? No → **ASK** (queue) or **REFUSE**. Yes → the forge writes a script, the workshop
   runs it (the container is the boundary; there is no static gate), the verdict judges it: file checks first, then the judge. A failure goes back
   to the forge with the gap described; after 3 attempts → **FAILED**, with the gap described for me.
6. Success → install as a skill (automatic after passing checks) and write the side-by-side page.
7. End: fingerprint again (must equal step 1), summary with dollars.

**Skill format** (open Agent Skills standard + our manifest):

```
registry/<name>/<version>/
  SKILL.md          what the effect is and when to use it, readable by Claude Code
  capability.json   effect, inputs (text, params), output clip format {width, height, fps, duration_s},
                    origin (reel, criteria), file checks, version
  skill.py          run(input, workdir) writes the clip; runs only in the workshop
  tests.json        file checks + judge criteria, as data, evaluated by fixed code
registry/index.json   active version per skill
```

Every effect returns a clip in exactly the format its manifest declares, so clips can be chained.

**Fresh session:** "Make me a short animated intro from this text" → the planner chains 2–3 installed effects
without building anything new. A proven chain is saved as a workflow (stretch). The catalog "what can you do?"
is built by the creature itself, from a task.

## 5. Authority v2

- **Spine network:** Apify API, Instagram CDN download (perception only), ElevenLabs API, the model via `claude -p`.
- **Skills** are scripts over files: input (text, parameters, files) → file. They run only in the workshop
  container: Python, ffmpeg, Pillow, numpy, fonts; one writable work folder; may start programs inside; no network,
  no keys, no host environment; limits on time, memory, processes and output size.
- **Downloading a package or a model at run time:** ASK (not built tonight).
- **Caps per run:** 3 forge attempts per gap, $1.00 model spend, 5 new skills.
- **Always REFUSE:** sending messages, payments, deleting, credentials, publishing in someone's name, changing
  authority, gate, workshop or registry code.
- **ASK:** anything else outside the above (network for skills, new tools, more budget). A human edits
  `authority.json`; the new fingerprint is logged.
- **Install** is automatic after passing file checks and the judge. In the recorded run Martin approves one install
  with the side-by-side preview on camera. New authority only ever comes from Martin.
- The fingerprint covers `authority.json`, the workshop image digest and every module that enforces the rules or caps.

## 6. Our definition of done

**For the submission (all must hold):**

- [ ] Registry empty at the start of the recorded run, shown on screen.
- [ ] A reel becomes criteria before any attempt; the creature tries, the checks and the judge run visibly; the skill installs.
- [ ] At least 3 effects learned by the creature, none written or seeded by us.
- [ ] At least one failure handled visibly (a failed check → fix, or FAILED with the gap described).
- [ ] A side-by-side page per attempt: reel frames, output frames and player, criteria, verdict, cost.
- [ ] The creature built its own catalog ("what can you do?") from a task.
- [ ] Fresh process: "intro from this text" chains 2–3 learned effects, no new build, no manual wiring.
- [ ] One ASK and one REFUSE shown; authority fingerprint identical at start and end.
- [ ] Per run: learning cost, cost per use, and cost if a model did it every time, in dollars.
- [ ] Caps enforced in code and covered by tests.
- [ ] README: how to run, what is real / simulated / missing. Video ≤ 90 s. Public repo.

**For every feature:** code · unit tests incl. edge cases · `ruff check` + `ruff format` clean · QA review passed · docs updated · one commit · short report to Martin.

## 7. Rules

From `CLAUDE.md`: never write to `registry/` by hand · English everywhere, short comments only where needed · one feature at a time · no secrets in the repo · generated code only in the sandbox · the creature's model calls get no outside context · plain Python 3.12, standard library first.

From the organizers: fresh build (our tooling and open-source libraries are fine, say so; prefer few, well-known ones) · no pre-written tools · gap from a task · caps in code · never cut failures from the video · say what is simulated.

## 8. Who does what

| Who | Role |
|---|---|
| Martin | decides: authority, reels, voice, approvals; final say on scope |
| Claude Code, main session (HAC 01) | builds features, coordinates, keeps this plan and the schedule |
| Claude Code, QA session (HAC 02 / Nocturne) | tests, code quality, contracts between modules, security tests; reviews each feature |
| Claude on the web | scope and time keeper, reviews batches |

Work is assigned by Martin and the main session. QA owns `tests/` and quality tooling; code changes outside `tests/` go through the main session unless trivial (typo, lint).

## 9. Test strategy

- **Unit tests** per module, fast, no network, no model (`uv run pytest`).
- **Contract tests** between modules: JSON Schemas for capability.json, ledger events, planner output.
- **Containment tests:** network blocked, nothing writable outside the work folder, no secrets, time, process and output limits; re-run against the workshop image.
- **Recorded fixtures:** one real Apify response and one real model response, replayed offline.
- **Fake model** for deterministic end-to-end tests of the loop; live model runs marked `slow`.
- **Skill checks are data** (file checks and judge criteria), evaluated by fixed code; written before the attempt, some held out from the forge; the judge never sees code.
- Later, if time allows: one mutation check per skill (break the code, tests must fail).

## 10. Roadmap (times are approximate)

| Step | What | Around |
|---|---|---|
| done | ledger + fingerprint · Docker sandbox · isolated capped model call | 21:00 |
| 1 | model reads frames | 21:45 |
| 2 | workshop: container with tools, work folder, limits, authority v2, image in fingerprint | 22:25 |
| 3 | perceive: reel → video, caption, transcript, frame strips | 23:00 |
| 4 | criteria, task, clip format, file checks | 23:25 |
| 5 | forge: 3 attempts, failure back | 00:05 |
| 6 | verdict: file checks + judge | 00:50 |
| 7 | registry: install + index | 01:10 |
| — | **first reel end to end** (`creature try`); then Martin runs more reels himself | **01:50** |
| — | ~~gate for workshop code~~: dropped (Martin); the container is the boundary | — |
| 9 | side-by-side page + morning overview | 02:50 |
| 10 | approval with preview | 03:10 |
| — | 2–3 effects in the registry for composition | 03:15 |
| 11 | ASK / REFUSE queue | 03:35 |
| 12 | fresh session: chain 2–3 effects + catalog | 04:25 |
| — | public repo + HQ submission (Martin) | 04:30 |
| 13 | voice: morning overview, only if time allows | 04:50 |
| — | recorded run = fallback video | 05:15 |
| — | final video, last push | 06:50 |

## 11. Open decisions

1. Reels: delivered by Martin's third session.
2. Voice: Martin picks it; the voice id is configuration.
