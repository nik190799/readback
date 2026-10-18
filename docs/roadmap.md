# Readback — plan

Saved 2026-10-17. Readback is a Claude Code plugin that shows what Claude is about to change as a picture of the **product**, not of the code. Anyone in a company can look at a change from their own level, with the lens they care about.

## Published pages

| Page | What it shows |
|---|---|
| [Design brief](https://claude.ai/artifact/BsC9LdRwiEUsDANKRCavj8) | The original plugin design (v3) |
| [Readback for The Atelier](https://claude.ai/artifact/Px7jo4SqkRNJqrko2cQPC2) | Six ways to picture one task (views A–F). A, the product map, is the default. |
| [Map gallery](https://claude.ai/artifact/UDsgMCzF12dATUnyMBJWzC) | The same board for six product types, three tasks each |
| [Levels and lenses](https://claude.ai/artifact/Gj3XGsug4UBuLMVDAvMC9m) | One task at 5 levels × 5 lenses, plus who sees what |

## 1. The board (fixed rules)

- **Three bands:** What users see / What runs behind it / What the team uses.
- **Four states:** new (solid), changed (amber), used but not changed (dashed), not touched (dim).
- **About 16 tiles per screen.** If there are more, related tiles are grouped, and tapping a group opens it.
- **Tiles never move.** A new part takes a free slot and keeps it.
- **Seven common parts always sit in the same slot:** Getting in, Notifications, Settings, Billing, Admin, Monitoring, Release. If a product lacks one, the slot stays empty and is labeled, e.g. "(no billing)".
- **Summary line:** parts changed, parts new, whether users will notice, and a ⚠ risk flag.
- **Views built from the board:**
  - The risk toggle recolors it.
  - The one-line form is for a commit message or the terminal.
  - The text form is for the PR description.

## 2. Levels (how far you zoom)

| # | Level | Tiles are | Usually read by |
|---|---|---|---|
| 1 | Experience | Journeys a customer goes through | Founders, design, support |
| 2 | Features | What the product can do | PMs, finance |
| 3 | Parts | The product map (named parts) | Eng leads, PMs |
| 4 | Code | Frontend / backend / database modules, tests | Engineers, security |
| 5 | Infra | Services, data stores, vendors, pipelines | Platform, SRE, CTO |
| 0 | Portfolio (large companies) | Products and teams | Leadership |

- The level names are the same in every product. What fills them differs per product.
- Links between levels are many-to-many.

## 3. Lenses (what the tiles are colored by)

- **Core (always on):** Change, Risk, Owner.
- **Plug-in lenses, one file each in `.readback/lenses/*.yaml`:**
  - Business: Finance (money in / out / shown), Usage, Customers (plans, segments), Rollout (flags).
  - Engineering: Tech stack (read automatically from lockfiles and infrastructure config), Tests, Reliability, Fragility, Performance.
  - Trust: Security, Privacy, Compliance.
  - People: Docs, Support, Accessibility and translation.
- **Time axis:** the same board for last week or last release.
- **The test for a new lens:** it answers one question, has a source (read automatically or tagged once by an owner), uses at most 4 states, and someone acts on it.
- **Display limit:** one lens colors the board at a time. A second can show as a badge, but never more than two.
- **Finance shows a number only when it can be computed** from the change, like "24× runs" or "1 AI call per tap". Otherwise it shows only the direction money moves.

## 4. Roles (default level + lens)

Founder: Experience · Change. Designer: Experience · Change. Support: Experience · Risk. PM: Features · Change. Finance: Features · Finance. Eng lead: Parts · Risk. Eng manager: Parts · Owner. Engineer: Code · Change. Security: Code · Tech stack. SRE: Infra · Risk. CTO: Infra · Tech stack. Every other combination is one tap away.

## 5. How it stays true

- **Facts live only at the bottom.** Files and infrastructure resources come from the diff. Every level above is worked out through the links.
- **Rollup:** a tile takes the strongest state of the tiles below it (new > changed > used > not touched).
- **Predict down, confirm up:**
  1. A request comes in at any level ("add discount codes" is a feature).
  2. Claude predicts which tiles each lower level will touch.
  3. After the work, the real diff is rolled back up, and any mismatch is shown ("planned Pricing, also touched Shipping").
- **"Unmapped" tiles at every level** show where the map has a gap.
- **Map files live in `.readback/`, one per level:**
  - Claude drafts each one on the first run.
  - Each owner approves their own: design → experience, product → features, eng → parts and paths, platform → infra.
  - Owner tags come from CODEOWNERS.

```yaml
# .readback/features.yaml   (owned by product)
discount_codes: { name: Discount codes, parts: [cart, checkout, pricing, admin] }
# .readback/parts.yaml      (owned by eng)
pricing: { name: Pricing, band: behind, paths: [api/pricing/**], infra: [postgres], money: in, owner: checkout-team }
# .readback/experience.yaml (owned by design)
checkout_journey: { name: Checkout, features: [cart, discount_codes, payments] }
```

## 6. What already exists vs. what's new

- **Already exists:**
  - Zoom levels for architecture: the C4 model, Structurizr, IcePanel.
  - A catalog of parts with owners: Backstage.
  - Diagrams of each PR: CodeBoarding, PR Lens, CodeSee, Bito, Miro's code-review skill.
  - File-level change impact: the impact-analysis and project-pilot plugins.
- **New, as far as searched (not proven):**
  - Levels above code (journeys, features) connected down to the code.
  - Lenses for people outside engineering.
  - The picture drawn from Claude's plan *before* the work, then checked against the diff.
  - One fixed board for every kind of product.
- **The hard part is keeping the maps true over time, not drawing the picture.**

## 7. Next steps

1. **P0 spike:** generate the Parts-level map for one real repo (e.g. The Atelier) and show it before and after a single task.
2. Add Features and Code, with rollup and the "planned vs. touched" check.
3. Add the core lenses (Risk, Owner), then Finance and Tech stack.
4. Role presets and the one-line form for the terminal and commits.
5. A lens plug-in format, so teams can add their own lenses.

---

## v2 (2026-10-18): after trying to break v1

Page: https://claude.ai/artifact/5ACjPbKQMi33e92g6syFUJ (private). We found 58 breaks in v1, and these are the fixes.

**One picture grammar, the same in every lens**
- **Outline = what the code did.** It looks the same in every lens: new (thick line plus a NEW tab), changed, used (dashed, with the cause named), swept (thin line), not touched (hairline), removed (struck through).
- **Colour = the answer.** One neutral indigo ramp with three steps: none, light, strong. No traffic lights. More colour means a stronger answer.
- **Ring = expected.** At most 3 on a DONE board. In PLAN, every lit tile gets a ring.
- **①②③ = what the sentence talks about.** One set per screen.
- **Tint budget:** at most half the tiles get light colour. The footer says how many tiles were left untinted, so a big change never paints the whole board.

**Truth**
- Each level owns files by path globs. The most specific glob wins, so each file lights exactly one tile per level.
- "Used" needs a field the diff writes. It never rises above "used".
- Links nobody has approved show a '?' tab and a ring.

**Matrix (Atlas)**
- 5 levels × 6 lenses gives 22 cells that apply.
- Marks: ● someone acts, ◐ an answer, ○ nothing here, Δ where the surprise shows first.
- Every ● comes from an action row and maps 1:1 to a line in the Before-launch strip.

**Roles and lifecycle**
- Each role is ACT, KNOW (it watches certain cells) or NONE.
- Lifecycle runs PLAN → DONE → MERGED → LIVE. PLAN draws Claude's expectations as rings.

**Surfaces and checks**
- The PR comment, Slack card, 80-column terminal view and 72-column commit trailer are all generated from the same data.
- 17 build checks, all passing.

---

## Plan board with confidence (2026-10-18)

The trial predicted tiles from intent alone, with no diff, on 40 boards (`trial/results/intent-prediction.md`, not committed). Opus working from a written plan reached 91.7% precision and 85% exact boards. Diff labels reach 95.7% and 90%. Picks Claude marked high-confidence were 98–100% exact; medium and low ones were often wrong. The most common miss was a second tile that changes along with the main one.

What we built from that:
- `rb.py plan --tiles a,b --maybe c` takes the tiles Claude is sure of and the ones it is not.
- Sure tiles get a solid ring and maybe tiles a dotted ring.
- Maybe tiles never raise an "unplanned" or "not touched" flag. Once the work is done, a maybe tile nobody touched just counts as untouched.
- "Usually changes with" questions come from recent git history: a tile that changed in the same commit as a sure tile at least 3 times, and in 30% or more of that tile's commits. Commits touching 5+ features are left out. The map can also list `"couples": [ids]` per feature.
- `--format html --out FILE` draws the PLAN picture as a page with a "Confirm before coding" card. `plan --show` redraws the stored plan.
- After plan mode, Claude shows the PLAN board and asks the user once about any maybes or couples. It then records the final list before editing.
