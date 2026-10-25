---
name: readback
description: Use when the user asks what changed, what a branch/PR/commit does in product terms, whether the work matched the plan, who needs to review it, or wants a picture of a change for a dev, senior, lead or manager reader (or a PM, finance, support or design role); when they have many ideas to sort into what to build; or when they keep asking where things are or why. Draws Readback cards and boards from the repo map and git diff.
---

# Readback

Readback turns a plan and a git diff into a product picture: tiles are features or parts from `.readback/map.json`;
a solid outline means touched, fill is the answer for the chosen lens, a ring means it was in the plan
(a dotted ring means "maybe", for the user to confirm),
and ① ② ③ point at the sentence above the board.

Run `sh "${CLAUDE_PLUGIN_ROOT}/scripts/rb" show [range] [level] [lens] [--as role] [--format ...]`.

- Pick the level and lens from the question: "what will users notice" → `features change`;
  "is this safe" → `parts risk`; "does it touch pricing" → `features money`; a named reader → `--as <role>`.
- Paste the terminal board unchanged inside a code block. For a PR description use `--format md`;
  for a shareable page use `--format html --out .readback/board.html`.
- If there is no map, offer `/readback init`. If many files are unmapped, offer to add tiles for them.
- When you write a PR description, end it with `Readback-Plan: Tile, Tile` (the tiles the PR sets out to change).
- If a hook asks you to map a plan to tiles (after plan mode, or before your first edit), run the `rb.py plan --tiles ... --maybe ... --card -` command it gives before editing, with the card JSON it describes on stdin (a heredoc). Put tiles you are sure of in `--tiles` and partly-sure ones in `--maybe`. In the card, lead the headline with the change (never a risk word), keep one risk tagged `plan` only when the plan says it, and leave out `flow`, `reach`, `before_after` or `acts` when they don't apply. If it prints card warnings, fix the card and rerun.
- Show the card where it reads best: with a visual tool (show_widget), run `plan --show --format widget` and pass its output to it unchanged; with a page or artifact tool, publish `--format html`; in an IDE, write `--format html --out .readback/plan.html` and point the user to it; otherwise paste the term card. Never redraw, restyle, trim or edit a card or board, including any `?` question. If it has maybe tiles or a "usually changes with" question, ask the user once, then rerun with the final `--tiles` (keep any `--source` the hook gave you).
- **Reader:** when the user names who will read it ("for my manager", "for the lead"), add `--for dev|senior|lead|manager`.
  Higher readers get less, but a risk or owner shown to one reader is always carried up; never add it back by hand.
- **What changed card:** when a plan card exists, the Stop hook prints the "What changed" card (the plan checked against the
  diff). To show it as a picture, run `show --card --format widget` and pass it to show_widget unchanged. For a PR description
  use `show --card --format md`. Its flags ("Says ... but ...", "Plan said leave X alone") name who acts; repeat them, don't soften them.
- **Many ideas (`/readback deck`):** one card JSON per idea with a `title`, `deck add -`, `deck prep`, then one card at a time
  (`deck next`, the user picks 1 Build / 2 Later / 3 Drop / 4 Merge, `deck decide N choice`), then `deck summary`. Follow
  `commands/readback.md`.
- **Questions people keep asking (`/readback fill-me`):** `gaps`, then `brief`, then write `.readback/context.md` in your own
  words. Transcripts stay on this machine: never paste or quote the user's earlier prompts into files, logs or the brief.
- Never invent tiles or numbers that the board did not print.
