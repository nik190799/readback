# Readback

See a code change as a product picture, not a diff.

Readback reads your git diff and a small map of your product (`.readback/map.json`) and draws a
board: which **features** and **parts** changed, which of them were in the plan, and who has to act.

```
Readback · Shringar · DONE · Features · Money
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
/plugin marketplace add OWNER/readback
/plugin install readback@readback
/readback init        # drafts .readback/map.json - review the tile names, commit it
```

Needs Python 3.8+ and git. No other dependencies.

## What happens automatically

| When | What Readback does |
|---|---|
| You send the first message of a session | Keeps it as context only (an opening question is not a plan, so no flags) |
| You approve a plan (plan mode) | Asks Claude to map the plan to tiles (`rb.py plan --tiles ...`); those become the expected tiles |
| Claude stops and the diff changed | Shows the board once (silent if nothing new changed) |

## On demand

```
/readback                         this branch, features, change lens
/readback parts risk              riskiest parts
/readback HEAD~3..HEAD --as finance
/readback --format md             paste into a PR description
/readback --format html           shareable page in .readback/board.html
/readback check                   coverage, then asks who owns the largest shared files
```

## Reading the board

Solid outline = touched · dashed = untouched · fill = the lens's answer (strong / light) ·
ring ◯ = expected from the plan · ① ② ③ = cited in the sentence · "unplanned" = touched but not
in the plan · "not touched" = planned but not done (both only against a real plan: plan mode or a
PR body with a `Readback-Plan:` line) · "code only, no product change" = a refactor, revert, CI, docs
or tooling change (from commit messages and the file mix), shown without money or plan flags ·
"brushed" = a tiny incidental touch. The headline ranks customer-facing tiles first; mark internal
tiles with `"audience": "internal"` in the map.

## Pull requests and Slack

Copy `ci/readback.yml` to `.github/workflows/`. Put a line `Readback-Plan: Tile, Tile` in the PR
body (Claude adds it when it writes the PR) to get unplanned / not-touched flags; without it the
board shows what changed and no flags. It keeps one comment updated per PR, and pings Slack
(`READBACK_SLACK_WEBHOOK` secret) only when someone has to act.

## The map

`init` drafts tiles from your folder structure. Rename them to words your PM would use, merge
small ones, and add file globs for shared code that belongs to a feature. The most specific glob wins.
Flags: `"risk": "high"`, `"money": true`, `"owner": "@handle"`.
