# Readback

See a code change as a product picture, not a diff.

Readback reads your git diff and a small map of your product (`.readback/map.json`) and draws a
board: which **features** and **parts** changed, which of them were in the plan, and who has to act.

```
Readback · Shopfront · What changed · Features · Money
──────────────────────────────────────────────
Bag and offers ① touches money.

 ①   ██████ Bag and offers         2 files, 216 lines
            Admin                  3 files, 281 lines
            Collection             4 files, 171 lines
   · untouched: Home page, Custom orders, Brand story

   ● finance: Check money impact in Bag and offers
```

## Install

```
/plugin marketplace add nik190799/readback
/plugin install readback@readback
/readback init        # drafts .readback/map.json - review the tile names, commit it
```

Needs Python 3.8+ (found as `python3`, `python` or `py`) and git. No other dependencies.

## What happens automatically

| When | What Readback does |
|---|---|
| You send the first message of a session | Keeps it as context only (an opening question is not a plan, so no flags) |
| You approve a plan (plan mode) | Claude maps the plan to tiles (`rb.py plan --tiles sure --maybe unsure`) and shows the "About to change" board before editing. If there are maybe tiles, or a tile that usually changes with a planned one, it asks you once and then records the final list |
| Claude is about to make its first edit with no mapped plan (you skipped plan mode, or a headless `claude -p` run) | Holds that one edit and asks Claude which tiles the change will touch (`--source intent`), then shows the same "About to change" board and question. Once per session |
| A session starts and `.readback/context.md` exists | Gives Claude a short "Before you ask" brief (12 lines or fewer) for what is being worked on |
| Claude stops and the diff changed | Shows the "What changed" card when there is a plan card, else the board (once; silent if nothing new changed) |
| Claude stops with an "About to change" card it never showed you | Asks Claude once to show it before finishing (never loops) |

## The "About to change" card

When Claude maps a plan it also writes a short card as JSON (`rb.py plan --tiles … --card -`): the change in
plain words, one risk tagged *from plan* or *possible*, what happens to each touched tile, and who acts before
start, merge or release. `rb.py` checks it against the map and draws it the same way every time; Claude never
draws it by hand.

- **Only what the plan touches is drawn**, as a pruned tree (product → area → tile → what changes). A map
  with 100 features gives the same small card.
- **Extra sketches by change type, more for riskier plans** (at most 3): *What happens* (a flow with its
  branches), *How deep* (screen / logic / data / config / outside), *How far* (who it reaches), *What moves*
  (before / after). A one-tile label change is the tree alone.
- **Colours mean one thing each:** amber = risk, green = new, red strike-through = removed, dashed = maybe,
  blue dashed = outside the repo.
- **Where it shows, best first:** the Claude app's inline visual tool (`plan --show --format widget`, passed
  through unchanged), a page link (`--format html`), `.readback/plan.html` in your IDE's preview, or the
  text tree in the terminal.
- A guessed risk never blocks: it becomes a question to the plan's author before work starts.

## The "What changed" card

The same card after the work, checked against the diff (`rb.py show --card`):

- **Headline:** "Done as planned: …" when every planned tile changed and nothing else did; otherwise what changed,
  what was not in the plan, and what was not touched.
- **Tree:** each tile marked done ✓, not in the plan ⚠, not touched (dashed), barely touched, via a shared file, or
  "plan said leave alone" ⚠ (tiles in the plan card's `keep` list). Each tile opens to its files and line ranges; in
  the PR comment each one links to those lines.
- **Checks, only when they fire:** a shared file or widget changed, an API, schema or metric file changed,
  dependencies changed ("upstream not reviewed"), a thin plan, and test files added, changed or skipped.
- **Claims:** a PR body or plan that says "no money impact", "no UI change", "only touches X", "tests added",
  "no schema change" or "backward compatible" is checked against the diff: `Says "No money impact", but Prices changed`.
- **Who acts:** each flag goes to the tile's owner, or the map role whose `acts_on` matches (money → finance,
  unplanned → pm, code → engineer).

## Reader levels

`--for dev|senior|lead|manager` squeezes the same card for who reads it: dev sees everything (files and lines per
tile), senior sees the tree with layers and reach first, lead one row per tile, manager the headline, the risk and one
action. Extra rows become "+N more", never dropped silently. A risk, owner or loud flag shown to one reader is
carried up to every reader above ("Still true: …"). `--as pm` picks the role's default reader (`"for"` in the map's roles).

## Many ideas: `/readback deck`

Give Claude a list of ideas (a file, a doc, a chat list). It writes one "About to change" card per idea, and Readback
sorts them by area, marks near-duplicates ("Similar to 2: merge?") and overlaps ("Overlaps idea 3"), and sets aside
ideas too vague to place. Then you go one card at a time: **1 Build, 2 Later, 3 Drop, 4 Merge** (buttons in the Claude
app, a question elsewhere). The summary puts every Build on one tree: tiles two builds both change are amber, and
ideas that share nothing are listed as "can be built in parallel". `deck plan N` makes an idea the current plan.
Choices live in `.readback/deck.json` (gitignored). A plan card also says "Also planned on branch X" when another
branch's plan expects the same tiles.

## The questions you keep asking: `/readback fill-me`

`rb.py gaps` reads this repo's Claude Code sessions on your machine (`~/.claude/projects/<this repo>/`), picks the
questions you typed (not requests, not "shall we?"), sorts them (orientation, impact, recall, where, next) and pins
each to the tiles Claude then had to read to answer it. `rb.py brief` asks Claude to write `.readback/context.md` for
the most-asked tiles (purpose, flow, decisions, where config lives, open items); you review it like the map. After
that, each session starts with a short brief, the "What changed" card shows each tile's purpose, and `/readback stats`
tracks questions per session. Transcripts never leave your machine: the log keeps only kinds, tile ids and counts,
and `context.md` holds Claude's summaries, never your prompts.

## On demand

```
/readback                         this branch, features, change lens
/readback parts risk              riskiest parts
/readback HEAD~3..HEAD --as finance
/readback --format md             paste into a PR description
/readback --format html           shareable page in .readback/board.html
/readback check                   coverage, then asks who owns shared files and widgets
/readback plan                    map the current plan to tiles and draw the "About to change" board
/readback --card                  the "What changed" card: the plan checked against the diff
/readback --card --for manager    the same card, for a manager
/readback deck                    many ideas, one card each: build, later, drop or merge
/readback fill-me                 find the questions you keep asking, and write the answers down
/readback stats                   how plans held up: your answers to the plan questions, plan vs done
```

## Reading the board

Solid outline = touched · dashed = untouched · fill = the lens's answer (strong / light) ·
ring ◯ = expected from the plan · dotted ring ◌ = maybe (Claude was not sure, so you are asked to
confirm it, and it never raises a flag) · ① ② ③ = cited in the sentence · "unplanned" = touched but not
in the plan · "not touched" = planned but not done (both only against a real plan: plan mode or a
PR body with a `Readback-Plan:` line) · "code only, no product change" = a refactor, revert, CI, docs
or tooling change (from commit messages and the file mix), shown without money or plan flags ·
"brushed" = a tiny incidental touch · "via shared file" = a planned feature changed only
through a file it shares with others (an app bar on every screen). The headline ranks customer-facing tiles first; mark internal
tiles with `"audience": "internal"` in the map.

## How well the plan held up

Each mapped plan, your answers to its questions, and the latest "What changed" board for it go to
`.readback/log.jsonl`. The file holds tile ids and counts only (no code, no plan text), stays on
your machine and is gitignored. `/readback stats` reads it: how many maybe tiles you kept, how
many "usually changes with" tiles you took, how often you corrected a plan (dropped a sure tile or
added a missing one), and how many planned tiles really changed.

## Pull requests and Slack

Copy `ci/readback.yml` to `.github/workflows/`. It posts the "What changed" card, with each file linked to
its changed lines and the PR body's claims checked against the diff. Put a line `Readback-Plan: Tile, Tile` in the PR
body (Claude adds it when it writes the PR) to get unplanned / not-touched flags; without it the
card shows what changed and no plan flags. It keeps one comment updated per PR, and pings Slack
(`READBACK_SLACK_WEBHOOK` secret) only when someone has to act.

## The map

`init` drafts tiles from your folder structure. Rename them to words your PM would use, merge
small ones, and add file globs for shared code that belongs to a feature. The most specific glob wins.
A file several features draw (an app bar, a shared card) can be marked shared instead of owned:
`rb.py own lib/widgets/app_bar.dart shared --by closet,outfit-ideas` writes
`"shared_by": {"lib/widgets/app_bar.dart": ["closet", "outfit-ideas"]}` (`--by all` = every
feature). When it changes, planned features on that list count as changed, "via shared file";
it never makes a feature unplanned. `check` suggests these from the imports.
Flags: `"risk": "high"`, `"money": true`, `"owner": "@handle"`. Roles can say what they act on and who they read
as: `"roles": {"finance": {"home": ["features", "money"], "acts_on": ["money"], "for": "manager"}}`.
