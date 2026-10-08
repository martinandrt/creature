# creature — build plan

Living document for everyone building tonight. If code and this plan disagree, fix one of them in the same commit.

## 1. Vision

> *"I see interesting things in AI reels, but I have no time to find out whether my assistant can do them."*

The creature watches short videos about AI agents, works out what each one claims an agent can do, and checks whether it can do the same on real input.
When it cannot, it writes the missing capability as a tested, versioned skill, installs it and reuses it later, also combined with other skills in a fresh session.
Whenever a capability would need more power than it has, it stops and asks a human.

**Capabilities may grow. Authority may not.**

Who it is for: a person with a personal AI assistant who keeps seeing "agents can now do X" and wants the useful part, safely, without spending evenings testing it.

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

Background research (two reports, kept outside the repo): tests before install are what makes self-written skills help at all (SkillsBench 2026: untested self-written skills score 8–11.5 pp *below* no skills); an agent edited the signals its evaluator read (Darwin Gödel Machine) so tests and checks must sit outside its reach; 1,184 malicious skills on ClawHub (Feb 2026) show that skill text is an attack surface too; no verified system separates capability growth from authority growth by design.

## 4. Architecture

```
creature/
  authority.json        what the creature may do; edited by a human only; fingerprinted
  creature/             the spine (written tonight, by us)
    cli.py              run · list · approve · deny · rollback
    authority.py        load, validate, fingerprint (authority.json + every enforcing module)
    ledger.py           append-only run log (JSONL) with costs
    llm.py              isolated claude -p, JSON schema, cost, caps
    perceive.py         reel → caption, transcript, frames (Apify + download + Whisper)
    planner.py          task + registry → plan of existing skills, or a gap
    examiner.py         gap spec → tests as data (never sees code)
    forge.py            spec + tests (+ last error) → skill code, max 3 attempts
    gate.py             static filter: imports, dunders, banned calls, shape
    sandbox.py          runs skill code in Docker, JSON in, JSON out
    broker.py           performs declared actions for skills, holds the keys
    registry.py         install, version, activate, rollback, search
    voice.py            the creature speaks at five moments
  registry/             written only by creature runs (starts empty)
  queue/                ASK requests waiting for a human
  runs/                 one ledger per run
  docs/                 this plan
  tests/
```

**One run**

1. New process. Load `authority.json`, record its fingerprint, print the registry.
2. Perceive the reel: what does it claim an agent can do? Turn the claim into a concrete task on real input.
3. Planner: can existing skills do it? Yes → **HAVE**, execute. No → gap.
4. Gap: does the needed power fit the authority? No → **ASK** (queue) or **REFUSE** (forbidden class). Hype with nothing to replicate → **SKIP**.
5. Examiner writes tests from the spec. Forge writes the skill. Gate filters. Tests run in the sandbox, visible in the log. Error goes back to the forge; after 3 failed attempts → **FAILED**.
6. Install (version, archive the old one), then complete the task → **BUILD**.
7. End: fingerprint again (must equal step 1), summary: skills used, forge calls, dollars; voice.

**Skill format** (open Agent Skills standard + our manifest):

```
registry/<name>/<version>/
  SKILL.md          name + description ("what and when"), readable by Claude Code
  capability.json   interface (JSON Schema in/out), kind, permissions, origin (reel + task), tests, version
  skill.py          code skill: run(input) -> output   (pure, runs in the sandbox)
  tests.json        tests as data, evaluated by fixed code
registry/index.json   active version per skill
```

Kinds: `code` (pure function, $0 per use) · `prompt` (template run by the spine's model call, budget-capped) · `action` (pure `plan()` returns an action, the broker performs it, `finish()` shapes the result; stretch goal).

A proven chain of skills is saved as a **workflow** and replayed next time without the planner, at $0 model cost.

## 5. Authority v1 (draft, needs Martin's approval)

- **Spine network:** Apify API, Instagram CDN download (perception only), ElevenLabs API, the model via `claude -p`.
- **Code skills:** no network, no files, no processes, no environment, no model; allowed imports `re json math datetime collections itertools string unicodedata statistics`; time, memory and output limits; Docker sandbox only.
- **Prompt skills:** model calls through the spine only, max $0.02 per use.
- **Caps per run:** 3 forge attempts per skill, $1.00 model spend, 5 new skills.
- **Always REFUSE:** sending messages, payments, deleting, credentials, publishing in someone's name, changing authority, gate or registry code.
- **ASK:** anything else outside the above (new host, file access, more budget). A human edits `authority.json`; the new fingerprint is logged.
- No ASK is approved during the night: fingerprint at dusk = fingerprint at dawn.

## 6. Our definition of done

**For the submission (all must hold):**

- [ ] Registry empty at the start of the recorded night run, shown on screen.
- [ ] A reel-derived task exposes a gap; tests are written before the code; the test run is visible; the skill installs; the task completes.
- [ ] At least 3 skills built by the creature, none written or seeded by us.
- [ ] At least one failure handled visibly (red test → fix, or FAILED after 3 attempts).
- [ ] The creature built its own discovery tool ("what can you do?") from a task.
- [ ] Fresh process: a different task combines 2+ generated skills, no rebuild, no manual wiring.
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
- **Security tests:** escape attempts must fail in the gate *and* in the sandbox (dunder chains, `__import__`, `open`, sockets, fork bomb, huge output, endless loop).
- **Recorded fixtures:** one real Apify response and one real model response, replayed offline.
- **Fake model** for deterministic end-to-end tests of the loop; live model runs marked `slow`.
- **Skill tests are data**, evaluated by fixed code; the creature cannot edit them after the examiner writes them.
- Later, if time allows: one mutation check per skill (break the code, tests must fail).

## 10. Roadmap

| # | Feature | Target |
|---|---|---|
| F1 | authority.json + fingerprint + ledger | 21:30 |
| F2 | Docker sandbox runner + security tests | 22:00 |
| F3 | isolated model call: schema, cost, caps | 22:20 |
| F4 | registry: SKILL.md + capability.json, versions, rollback, search | 22:50 |
| F5 | gate (static filter) | 23:10 |
| F6 | examiner + forge + install loop (3 attempts, error back) | 23:50 |
| F7 | perceive: reel → claim → task (with fixture) | 00:20 |
| F8 | planner + run orchestrator + CLI | 00:50 |
| — | **Tracer: one reel end to end** | **01:00** |
| F9 | ASK / REFUSE queue, verdicts | 01:30 |
| F10 | fresh-session composition + saved workflows | 02:15 |
| F11 | voice (five moments) | 02:45 |
| F12 | run report (HTML from the ledger, for the video) | 03:15 |
| — | clean recorded night run, README, submission texts | 03:30–04:15 |
| — | fallback video uploaded, early submit | 04:30 |
| — | final video, last push | 06:50 |

## 11. Open decisions

1. Martin approves authority v1 (section 5).
2. Martin picks 5–8 reels from one domain (draft idea: content repurposing, where transcript → summary → hook → post compose naturally).
3. Public GitHub repo: waiting for OK.
4. Voice: Martin picks it later; voice id is configuration.
