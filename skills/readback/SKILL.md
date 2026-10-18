---
name: readback
description: Use when the user asks what changed, what a branch/PR/commit does in product terms, who needs to review it, or wants a picture of a change for a PM, finance, support or design reader. Draws a Readback board from the repo map and git diff.
---

# Readback

Readback turns a git diff into a product picture: tiles are features or parts from `.readback/map.json`;
a solid outline means touched, fill is the answer for the chosen lens, a ring means it was in the plan,
and ① ② ③ point at the sentence above the board.

Run `python "${CLAUDE_PLUGIN_ROOT}/bin/rb.py" show [range] [level] [lens] [--as role] [--format ...]`.

- Pick the level and lens from the question: "what will users notice" → `features change`;
  "is this safe" → `parts risk`; "does it touch pricing" → `features money`; a named reader → `--as <role>`.
- Paste the terminal board unchanged inside a code block. For a PR description use `--format md`;
  for a shareable page use `--format html --out .readback/board.html`.
- If there is no map, offer `/readback init`. If many files are unmapped, offer to add tiles for them.
- When you write a PR description, end it with `Readback-Plan: Tile, Tile` (the tiles the PR sets out to change).
- If a hook asks you to map a plan to tiles, run the `rb.py plan --tiles` command it gives before editing.
- Never invent tiles or numbers that the board did not print.
