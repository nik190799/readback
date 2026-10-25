# Readback backlog

Improvements collected for the next release; we plan the release from this list. Items marked **Built (0.5)** are on
branch `v0.5-plan-card`, not yet merged.

## 1. Draw the board visually when the app supports it

**Built (0.5) for the "About to change" card:** `plan --card -` and `--format widget|html|term`, the host order
in the skill, the Stop hook asking once, and fragment tests in `tests/test_card.py`. **Built (0.5) for "What changed" too:**
`show --card --format widget|html|md|term`; the Stop hook prints its term version (no block).

- **Where:** the plugin itself (`skills/readback/SKILL.md`, maybe `commands/readback.md`), not per-project rules.
- **What:** tell Claude to render the PLAN board visually (e.g. an inline widget/visual) when the host app supports it, and fall back to the current text board otherwise.
- **Why:** every project using Readback gets the better board without adding its own rule.
- **Today (checked 2026-10-24):** nothing in `SKILL.md`, `commands/readback.md` or the hooks mentions a visual tool.
  Boards reach the user as text, or as a separate page via `--format html`.
- **Host order, best first.** The skill tells Claude which to use, and `rb.py` always draws the picture itself:
  1. *Inline visual tool* (the Claude desktop app's `show_widget`, after its one-time `read_me`): pass the
     `--format widget` output unchanged. Fragment only: no doctype, host CSS variables, transparent background,
     no `position: fixed`, works in light and dark.
  2. *Artifact or page tool* (claude.ai, desktop): publish `--format html` and give the link.
  3. *IDE with a preview pane* (VS Code, Cursor): write `.readback/board.html` and ask the user to open it in
     the editor's preview or browser. The skill can't make it appear inline in the IDE.
  4. *Terminal only:* the indented-outline tree (sketch E) plus the headline and risk line.
- **Hooks can't call tools.** `hook-plan` and `hook-done` only print, so they print the text board plus one line
  telling Claude to render the widget if a visual tool exists. The skill carries the rule ("never redraw or trim
  it; pass it through as is").
- **Making sure it shows:** a hook can't call the visual tool itself, but the Stop hook can return "block" with a
  reason ("the board is ready, show it before you finish"). Claude then can't end the turn until it has passed the
  board on. Use this only when a visual tool is present, and at most once per diff, so it never loops.
- **Stages:** ideas (`/readback deck`, new), plan approved (`hook-plan`), coding (`hook-edit`: one line when the
  work drifts from the plan, no picture), Claude stops (`hook-done`), PR (CI page and Slack).
- **Test:** a snapshot test checks that the widget output follows the fragment rules above.

## 2. `/readback fill-me`: answer the questions you keep having to ask

**Built (0.5):** `rb.py gaps` (this repo only, deduped across resumed sessions, discussion and requests skipped,
`--unsure` / `--label`), `rb.py brief` and `brief --show`, the `SessionStart` hook, "↳ purpose" rows on the done card,
questions per session in `stats`. Tests in `tests/test_gaps.py`. On this repo's own 11 sessions: 41 questions, 19 the
rules could not sort (mostly design talk), so the labels matter. Still open: the replay trial below.

- **Where:** new `rb.py gaps` (mining) + `rb.py brief` (filling) in `bin/rb.py`, a `fill-me` line in
  `commands/readback.md`, a step in `skills/readback/SKILL.md`, optional `SessionStart` hook.
- **Evidence:** a scan of local Claude Code transcripts (`~/.claude/projects/*/*.jsonl`, 2026-10-20)
  found ~700 of ~2,200 user prompts were questions. Many are context gaps, not new work:
  "Do you understand this project?", "What does this website do?", "so tell me what all things the
  PR will bring?", "but didn't we already set the key?", "where do I have to set the api key?",
  "What to do next?", "how long do you think it's remaining?".
- **What, part A - find the gaps (`rb.py gaps`):** read this repo's transcripts, pick the user
  prompts that only ask to understand (heuristic first, Claude labels the unsure ones), and sort them:
  - *orientation* - what is this / what's the flow
  - *impact* - what will this change or PR bring
  - *recall* - "didn't we already...", a decision or setting from an earlier session
  - *where* - where does X live / where do I set Y
  - *next* - what's left, what to do next
  Map each question to tiles from the files Claude read or edited right after it. Output: which tiles
  carry the most "context debt", and of which kind.
- **What, part B - fill them (`rb.py brief`, `/readback fill-me`):** for the top gap tiles, Claude writes
  `.readback/context.md`: per tile, a one-line purpose, the flow (A → B → C), decisions made and where
  (with session date), where its config/keys live, open items. The user reviews it like the map before
  committing. Then:
  - session start: a short "Before you ask" brief for the tiles the last sessions touched
  - "What changed" board: one line per touched tile answering the gap kind it usually gets
    (impact for PR work, recall for config)
- **Privacy:** transcripts never leave the machine; `log.jsonl` keeps only gap kind + tile ids + counts
  (same rule as today); `context.md` holds Claude-written summaries, never quoted prompts.
- **How we know it works:** replay held-out recent sessions (like `trial/results/intent-prediction.md`):
  what share of their understanding questions would the brief have already answered? Track in
  `/readback stats` as "questions per session" going down.
- **Open questions:** does Cursor/other hosts' history count, or Claude Code only? Mine all projects or
  only the current repo? One `context.md` or a `"why"` field per tile in `map.json`?

## 3. Read back across layers: catch what each summary drops

**Built (0.5):** evidence links (tile → files → line ranges, linked in the PR comment), unsupported-claim flags
(money, UI, "only touches", tests, schema, compatible), `keep` tiles ("Plan said leave X alone"), and approver routing
from tile owners and role `acts_on`. Tests in `tests/test_done.py`.

- **Source:** an idea from the Backroom ideation session (2026-10-22). It is a reasoned framework, not research.
- **Idea:** work moves between layers. Dev (task, HOW) → lead (PR, WHAT/WHETHER) → manager (delivery,
  HOW MUCH/WHEN) → director (portfolio, WHY). Facts go up as summaries (diff → PR → status →
  narrative), and intent comes down as constraints. Most of what gets lost, gets lost where one layer
  hands off to the next: summaries of summaries drift (the "watermelon report", green outside and red
  inside), intent arrives vague, and agents act above their operator's authority.
- **Readback angles:**
  - *Evidence links:* every line Readback writes (board sentence, PR comment, Slack ping, item 2's brief)
    keeps a link one layer down: narrative → tile → files → diff hunk. "Reading back" means you can always click down.
  - *Unsupported claims:* flag a summary line that no lower-layer evidence backs (e.g. a PR body says
    "no money impact" while a `money: true` tile changed).
  - *Intent survives:* check that the plan's constraints (the `Readback-Plan` tiles, "don't touch
    Payments") still hold at the diff. This is today's unplanned / not-touched check, made general.
  - *Altitude-matched approver:* send each flag to the person whose layer owns the claim (the `--as`
    roles and `owner` already point this way).
- **Link to item 2:** the fill-me gap kinds line up with the layers. *impact* is facts going up, and
  *recall* / *next* are intent coming down, so fill-me measures where the handoffs leak in your own sessions.

## 4. Reader levels: dev, senior, lead, manager

**Built (0.5)** on both cards (`--for`, role `"for"` defaults, budgets with "+N more", carry-up checked for every
fixture in `tests/test_levels.py`). The old boards without a card don't take `--for` yet. The 5-second test is still to do.

- **What:** one board, squeezed harder for each step up. Same diff, same analysis; only the drawing changes.

  | `--for` | Level / lens | Budget | Per row |
  |---|---|---|---|
  | dev | parts / change | ~12 lines | files, lines, tests touched |
  | senior | parts / risk | ~8 lines | risk, missing tests, shared reach |
  | lead | features / plan | ~5 lines | planned, unplanned, not touched, who acts |
  | manager | features / status | 1-2 lines | done vs plan, one action |

- **Flag name:** `--for`, because `--level` already means features/parts. Existing roles in `map.json` get a
  `"for"` default (engineer → senior, pm → lead, finance → manager + money lens).
- **Rules:** going up only drops or merges rows, never adds a claim. The line budget is enforced in
  code (overflow becomes "+2 more"). Tile words come from the map.
- **Scenarios to snapshot-test at all four levels:** feature + unplanned extra, big refactor (code only),
  shared file (app bar), plan drift (planned not touched + risky CI), one-line fix (same line at every level).
- **Check:** 5-second test in `trial/` - "what changed, what do you need to do?"

## 5. Change categories, and what the stress test found (2026-10-24)

- **Categories:** 18 in 6 families (product, correctness, quality, data, platform, code health), each a record
  `{family, signals, paths, question, visual, acts, severity}` shipped as defaults and overridable in `map.json`.
  Multi-label: the highest severity drives the headline and colour, the rest become chips. 7 shared drawings.
- **Stress test:** 62 real PRs from 32 open-source projects, boards at 4 levels, 5 simulated readers (intern to manager)
  plus a red team, 2 rounds (What changed; About to change v1 vs v2). The full report is private.
- **Results:** the one-sentence headline works (87-98% at every level). v1 plan boards fail on "what do I do"
  (21-35%) and "what's risky" (26-31%); leads were confidently wrong on 34/62. v2 fixes raise both to ~80% but
  "grasped in 5 s" drops 96% → 78% (intern 95% → 56%).
- **Adopt for the next design (v3, the two-line glance):** **built (0.5)** on both cards, except the dependency scope
  (dev-only / goes to users / core) in 5 and "release notes read?" in 7.
  1. Line 1 is always the change in plain words; line 2 is one risk naming its subject, tagged "from plan" or "possible".
  2. Three fixed action slots: before start / before merge / before release ("nothing" when empty).
  3. Carry-up: a medium+ risk or a named owner at any level must appear on every level above (render check).
  4. Plan quality badge only when thin ("thin plan, ask the author"); draw the one tile a title names.
  5. Dependencies: scope (dev-only / goes to users / core) + "upstream not reviewed" unless release notes were read.
  6. Show a slot only when it has content; legend only beside a map; word chips (Benefit / Downside / Needs outside step), no +/−/!.
  7. Three standing questions: touches a shared/core package? changes what a metric or API means? release notes read?
  8. Inferred risks never block merge or take the headline alone; phrase them as a question to the author.
- **Add categories:** data integrity, contract/breaking for consumers, rollout state (kill switch, ramp), access and
  governance, release/versioning, shared component (reach badge), generated/vendored code, other / needs a human label.
- **Still to prove:** readers were simulated and boards were text. No recruited testers are available, so:
  1. *Blind self-test:* a page shows a board for 5 s and then hides it, and the reader answers "what changes / what
     do I do / what's risky". It uses held-out PRs not shown in the report, with v1/v2/v3 shuffled and unlabeled.
     The owner is a real dev-level reader of other projects' PRs.
  2. *Image-only skim proxy:* render the boards as pictures and give them to a small, fast model on low effort, with
     no text version, as a "skimmer". This tests the drawing, not the wording.
  3. *Dogfood:* turn on v3 for our own work and log each time a follow-up question was needed (feeds item 2's
     "questions per session").
  The same test page can later go in the public README as "try the board", which lets strangers add answers.

## 6. Big maps: the board stays the same size at 100+ features

**Built (0.5) for the plan card** (pruned tree). The old board without a card still lists every tile.

- **Problem:** `svg_board` draws every tile, and the term board lists every untouched name. A map with 100 features
  makes a wall of boxes.
- **Fix:** draw only the tiles the plan touches: will change, maybe, has a risk, or outside the repo. Nothing is
  drawn for untouched features: no dot strip, no "+N more", no area chips (tried and rejected 2026-10-24, they added
  nothing). Each tile says what happens to it ("Removed", "New screen, hidden until the flag is on") and who it
  reaches. A small area label above the name gives the "where".
- **Rule:** the board's size depends on the plan, not on the map. Untouched tiles come back only on the "What
  changed" board, and only when the diff touched something the plan didn't.

## Release order

Everything above that says **Built (0.5)** ships together as 0.5.0: ideas → deck → plan card → coding → "What changed"
card → PR comment, at four reader levels, with fill-me. Next, from what is left: the "still to prove" tests in item 5,
the extra change categories, the fill-me replay trial, and `--for` on the old boards.
