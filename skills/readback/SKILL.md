---
name: readback
description: Use when the user asks what changed, what a branch/PR/commit does in product terms, who needs to review it, or wants a picture of a change for a PM, finance, support or design reader. Draws a Readback board from the repo map and git diff.
---

# Readback

Readback turns a git diff into a product picture: tiles are features or parts from `.readback/map.json`;
a solid outline means touched, fill is the answer for the chosen lens, a ring means it was in the plan
(a dotted ring means "maybe", for the user to confirm),
and ① ② ③ point at the sentence above the board.

Run `sh "${CLAUDE_PLUGIN_ROOT}/bin/rb" show [range] [level] [lens] [--as role] [--format ...]`.

- Pick the level and lens from the question: "what will users notice" → `features change`;
  "is this safe" → `parts risk`; "does it touch pricing" → `features money`; a named reader → `--as <role>`.
- Paste the terminal board unchanged inside a code block. For a PR description use `--format md`;
  for a shareable page use `--format html --out .readback/board.html`.
- If there is no map, offer `/readback init`. If many files are unmapped, offer to add tiles for them.
- When you write a PR description, end it with `Readback-Plan: Tile, Tile` (the tiles the PR sets out to change).
- If a hook asks you to map a plan to tiles (after plan mode, or before your first edit), run the `rb.py plan --tiles ... --maybe ...` command it gives before editing. Put tiles you are sure of in `--tiles` and partly-sure ones in `--maybe`. Show the "About to change" board unchanged, every line including any `?` question; never trim or edit a board. If it has maybe tiles or a "usually changes with" question, ask the user once, then rerun with the final `--tiles` (keep any `--source` the hook gave you).
- Never invent tiles or numbers that the board did not print.
