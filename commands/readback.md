---
description: Show the Readback board for this branch, a commit, or a range
argument-hint: "[init | check | plan | stats | <range>] [features|parts] [change|risk|money|owner] [--as pm|finance|support|design] [--format term|md|html|slack|trailer]"
allowed-tools: Bash(python:*)
---

Run Readback with the user's arguments and show the result.

- If `$ARGUMENTS` starts with `init`, run `python "${CLAUDE_PLUGIN_ROOT}/bin/rb.py" init`, then read `.readback/map.json`, fix tiles whose names are not product words (rename them the way a PM would say it), and tell the user in two lines what to check.
- If it starts with `stats`, run `python "${CLAUDE_PLUGIN_ROOT}/bin/rb.py" stats` and paste it in a code block. Then add one sentence on the gate that is furthest off (plans corrected under 20%, planned tiles that changed 90%+), or say there is not enough data yet.
- If it starts with `check`, run `python "${CLAUDE_PLUGIN_ROOT}/bin/rb.py" check` and summarise the coverage in one line. If it lists "Largest shared files", ask the user which feature owns each one (AskUserQuestion if you have it, one question per file, the likeliest feature first and "shared" as an option; otherwise ask in plain text). For each answer run `python "${CLAUDE_PLUGIN_ROOT}/bin/rb.py" own <path> <feature-id|shared>`.
- If it starts with `plan`, map the current plan to tiles yourself (only the tiles the plan says it will change). Put the ones you are sure of in `--tiles` and the ones you are only partly sure of in `--maybe`, and run `python "${CLAUDE_PLUGIN_ROOT}/bin/rb.py" plan --tiles id1,id2 --maybe id3`. Paste the PLAN board it prints. If it shows maybe tiles or a "usually changes with" question, ask the user once, then rerun with `--tiles` set to the final list. For a page, add `--format html --out .readback/plan.html`. Readback never guesses the plan from keywords, and the opening request is not a plan: without a mapped plan (plan mode or a PR body) it shows no "unplanned" or "not touched" flags.
- When you write a pull request description, end it with a line `Readback-Plan: Tile name, Tile name` naming the tiles the PR sets out to change; CI uses it to flag unplanned and missing work.
- If `--format html` is asked, add `--out .readback/board.html` and tell the user the path.
- Otherwise run `python "${CLAUDE_PLUGIN_ROOT}/bin/rb.py" show $ARGUMENTS` and paste its output inside a code block, unchanged. Then add at most one sentence: anything surprising (an unplanned tile, a planned tile left untouched, "No product change", or unmapped files).

Do not re-explain the key; the board carries it.
