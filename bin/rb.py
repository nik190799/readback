#!/usr/bin/env python3
"""Readback: show a code change as a product picture.

One model, many surfaces. Every command reads the repo's map
(.readback/map.json) plus a git diff, resolves boards, and renders them.

  rb.py init [--force]              draft .readback/map.json from the repo tree
  rb.py show [RANGE] [--level L] [--lens X] [--as ROLE] [--format term|md|slack|html|json|trailer|line]
                                    [--expected a,b] [--plan TEXT|-]   (PR body with a Readback-Plan: line)
  rb.py plan --tiles a,b [--source plan|pr]   Claude's mapping of a real plan to tiles (PLAN)
  rb.py check                       validate the map; list the largest shared files to assign
  rb.py own PATH FEATURE|shared     record who owns a shared file
  rb.py hook-plan | hook-prompt | hook-done   (called by plugin hooks; JSON on stdin)

No dependencies beyond Python 3.8 and git.
"""
import fnmatch, hashlib, html, json, os, re, subprocess, sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

LEVELS = ["features", "parts"]
LENSES = ["change", "risk", "money", "owner"]
DISC = "①②③④⑤"
REAL_PLANS = {"plan", "pr"}  # ExitPlanMode or a PR body; an opening question is not a plan
REVERT_RE = r"(?i)^(revert\b|revert:)"
CHORE_RE = r"(?i)^(refactor|ci|chore|docs?|tests?|style|build|perf)(\([^)]*\))?!?:"
NOCHANGE_RE = r"(?i)(no|zero) (functional|behaviou?r(al)?|product|user[- ]facing) changes?|behaviou?r[- ]preserving|pure refactor"
TOOLING_RE = r"(?i)((^|/)(tests?|spec|docs?|scripts?|tools?|\.github|\.claude|\.cadence|eval|ci)/|\.md$|\.lock$|_test\.|\.test\.|\.spec\.|(^|/)\.[^/]+$)"
INTERNAL_WORDS = r"(?i)\b(admin|eval|setup|internal|tooling|dev|ops|scripts?)\b"
DOCS_RE = r"(?i)\.(md|mdx|markdown|rst)$"
SHARED_STYLE_RE = r"(?i)(^|/)(styles?|themes?|tokens|globals?|variables|base|main|app|index)\.(css|scss|sass|less)$|(^|/)(theme|tokens)\.(dart|ts|tsx|js|jsx)$"
RISK_SKIP_RE = (r"(?i)((^|/)(tests?|spec|__tests__|integration_test)/|_test\.|\.test\.|\.spec\.|(^|/)test_[^/]+$|\.(md|mdx|markdown|rst)$|\.lock$"
                r"|(^|/)(package-lock\.json|npm-shrinkwrap\.json|pnpm-lock\.yaml|yarn\.lock|go\.sum)$)")
RISK_WORDS = r"(auth|login|signin|session|security|secret|rules|permission|payment|billing|razorpay|stripe|subscription|purchase|iap|migration|schema|deploy|release|workflow|\.github|infra|firebase\.json|codemagic|fastlane|pubspec\.lock|package-lock|server|relay)"
MONEY_WORDS = r"(payment|billing|price|pricing|promo|discount|coupon|offer|credit|wallet|subscription|razorpay|stripe|checkout|cart|bag|order|invoice|iap|purchase|plan)"
SKIP_DIRS = {".git", "node_modules", "build", "dist", ".dart_tool", ".idea", ".vscode", "Pods", ".gradle", "__pycache__", ".next", "coverage", ".readback", ".claude"}
FEATURE_ROOTS = ["lib/features", "lib/screens", "lib/pages", "src/features", "src/pages", "src/app", "app", "pages", "src/routes", "src/components", "features", "web/src/pages", "web/app", "skills", "commands"]


# ---------------------------------------------------------------- git
def git(args, cwd=None, check=False):
    r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode:
        raise SystemExit("git " + " ".join(args) + ": " + r.stderr.strip())
    return r.stdout if r.returncode == 0 else ""


def repo_root():
    root = git(["rev-parse", "--show-toplevel"]).strip()
    if not root:
        raise SystemExit("readback: not inside a git repository")
    return root


def branch():
    return git(["rev-parse", "--abbrev-ref", "HEAD"]).strip() or "HEAD"


def base_ref():
    for b in ["origin/main", "main", "origin/master", "master", "origin/dev", "dev"]:
        if git(["rev-parse", "--verify", "-q", b]).strip():
            mb = git(["merge-base", "HEAD", b]).strip()
            return mb or b
    return "HEAD"


def diff_files(rng=None):
    """Return {path: lines_changed}. rng None = branch work + uncommitted."""
    if rng:
        if ".." not in rng and not rng.startswith("#"):
            rng = rng + "^.." + rng  # single commit
        out = git(["diff", "--numstat", "-M", rng])
    else:
        base = base_ref()
        out = git(["diff", "--numstat", "-M", base])
        for p in git(["ls-files", "--others", "--exclude-standard"]).splitlines():
            out += "1\t0\t" + p + "\n"
    files = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        a, d, p = parts[0], parts[1], parts[2]
        if " => " in p:  # rename
            p = re.sub(r"\{([^}]*) => ([^}]*)\}", r"\2", p)
            p = p.split(" => ")[-1]
        n = (int(a) if a.isdigit() else 1) + (int(d) if d.isdigit() else 0)
        p = p.replace("\\", "/")
        if p.startswith(".readback/"):
            continue
        files[p] = files.get(p, 0) + max(n, 1)
    return files


# ---------------------------------------------------------------- map
def map_path(root):
    return os.path.join(root, ".readback", "map.json")


def load_map(root):
    p = map_path(root)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def title(s):
    s = re.sub(r"[_\-.]+", " ", s).strip()
    return " ".join(w[:1].upper() + w[1:] for w in s.split()) or s


def tracked(root):
    return [p for p in git(["ls-files"], cwd=root).splitlines() if not any(seg in SKIP_DIRS for seg in p.split("/"))]


def draft_map(root):
    files = tracked(root)
    dirs = {}
    for p in files:
        segs = p.split("/")
        for i in range(1, len(segs)):
            dirs.setdefault("/".join(segs[:i]), 0)
            dirs["/".join(segs[:i])] += 1

    def tile(tid, name, globs, sub=""):
        text = " ".join(globs) + " " + name
        return {"id": tid, "name": name, "sub": sub, "owns": globs,
                "risk": "high" if re.search(RISK_WORDS, text, re.I) else "normal",
                "money": bool(re.search(MONEY_WORDS, text, re.I)),
                "owner": "", "audience": "internal" if re.search(INTERNAL_WORDS, name) else "customer", "keywords": sorted(set(w.lower() for w in re.split(r"[^A-Za-z]+", name) if len(w) > 2))}

    # Features 1: children of feature-ish folders (lib/features/closet -> Closet)
    feats, seen = [], set()
    GENERIC = {"shared", "common", "widgets", "utils", "util", "ui", "lib", "styles", "assets", "api", "core", "types", "helpers", "main", "index", "app", "config", "firebase", "site", "style", "models", "components"}
    for fr in FEATURE_ROOTS:
        kids = sorted(d for d in dirs if d.startswith(fr + "/") and d.count("/") == fr.count("/") + 1)
        for k in kids:
            leaf = k.split("/")[-1]
            if leaf.startswith(("_", "(", "[")) or leaf.lower() in seen or leaf.lower() in GENERIC:
                continue
            seen.add(leaf.lower())
            feats.append(tile(re.sub(r"\W+", "-", leaf.lower()), title(leaf), [k + "/**"]))
    # Features 2: flat modules - the same stem across layers is one feature
    # (web/src/rooms.ts + server/backroom/rooms.py -> Rooms; src/page-home.js + index.html -> Home)
    if len(feats) < 4:
        stems = {}
        for p in files:
            if not re.search(r"\.(html|js|mjs|ts|tsx|jsx|vue|svelte|py|dart|go|rb)$", p) or re.search(r"(^|/)(test|tests|spec|scripts?|tools?|spikes?|docs?)/|(\.test\.|\.spec\.|_test\.|config)", p):
                continue
            if p.count("/") > 3:
                continue
            stem = re.sub(r"^(page|screen|view)[-_]", "", os.path.splitext(os.path.basename(p))[0].lower())
            stem = {"index": "home"}.get(stem, stem)
            if stem in GENERIC or stem.startswith("_") or len(stem) < 3:
                continue
            stems.setdefault(stem, []).append(p)
        for stem, ps in sorted(stems.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            if stem in seen:
                continue
            seen.add(stem)
            feats.append(tile(re.sub(r"\W+", "-", stem), title(stem), sorted(ps)))
    feats = feats[:40]
    # Affinity: a shared file whose name carries a feature word joins that feature
    # (lib/services/closet_service.dart -> Closet, functions/referral.js -> Referral)
    claimed = {p for t in feats for p in files if any(match(p, g) for g in t["owns"])}
    for p in files:
        if p in claimed or not re.search(r"\.(dart|js|ts|py|mjs|tsx)$", p) or re.search(r"(test|spec)", p):
            continue
        toks = set(re.split(r"[^a-z]+", os.path.splitext(os.path.basename(p))[0].lower()))
        for t in sorted(feats, key=lambda t: -len(t["id"])):
            key = t["id"].replace("-", "_")
            if key in toks or (len(t["id"]) > 4 and "_".join(sorted(toks)).find(key) >= 0 and key in os.path.basename(p).lower()):
                t["owns"].append(p)
                break

    # Parts: top-level dirs (and second level under lib/src/server), most files first
    cands = []
    for d, n in dirs.items():
        depth = d.count("/")
        top = d.split("/")[0]
        if top.startswith(".") and top != ".github":
            continue
        if depth == 0 and top not in ("lib", "src", "test", "tests", "docs", ".github"):
            cands.append((n, d))
        elif depth == 1 and top in ("lib", "src", "server", "web", "apps", "packages", "services", "functions", "plugins"):
            cands.append((n, d))
    cands.sort(reverse=True)
    parts, names = [], set()
    for n, d in cands[:15]:
        leaf = d.split("/")[-1]
        nm = "Screens" if d in FEATURE_ROOTS else title(leaf)
        if nm.lower() in names or re.search(r"^(tests?|docs?)$", leaf):
            nm = title(d.replace("/", " "))
        names.add(nm.lower())
        parts.append(tile(re.sub(r"\W+", "-", d.lower()), nm, [d + "/**"]))
    # Flat src/: one part per module when there are no sub-folders worth naming
    if len(parts) < 4:
        for p in files:
            if re.match(r"^(src|lib)/[^/]+\.(js|ts|py|dart)$", p):
                stem = os.path.splitext(os.path.basename(p))[0]
                parts.append(tile("src-" + stem.lower(), title(stem), [p]))
        parts.append(tile("pages", "Pages", ["*.html"]))
    parts.append(tile("tests", "Tests", ["test/**", "tests/**", "**/*_test.*", "**/*.test.*", "**/*.spec.*", "**/test_*"]))
    parts.append(tile("docs", "Docs", ["docs/**", "*.md", "**/*.md"]))
    parts.append(tile("config", "Config and CI", [".github/**", "*.json", "*.yaml", "*.yml", "*.toml", "*.lock", "*.rules", "*.config.*", ".*"]))
    parts[-1]["risk"] = "high"
    parts = [t for t in parts if any(match(p, g) for p in files for g in t["owns"])]
    name = title(os.path.basename(root))
    # In a plugin, skill and command markdown is the product, not docs
    pdocs = [d + "/**" for d in ("skills", "commands", "agents") if d in dirs]
    return {"version": 1, "product": name, "product_docs": pdocs,
            "levels": {"features": {"tiles": feats}, "parts": {"tiles": parts}},
            "roles": {
                "engineer": {"home": ["parts", "change"], "acts_on": ["risk"]},
                "pm": {"home": ["features", "change"], "acts_on": ["unplanned"]},
                "finance": {"home": ["features", "money"], "acts_on": ["money"]},
                "support": {"home": ["features", "change"], "acts_on": []},
                "design": {"home": ["features", "change"], "acts_on": []}},
            "drafted_by": "rb.py init (heuristic) - review and edit; owners approve in one PR"}


# ---------------------------------------------------------------- resolve
def glob_spec(g):
    """Specificity = literal prefix length; most specific owner wins."""
    return len(re.split(r"[*?\[]", g)[0])


def match(path, g):
    if g.endswith("/**"):
        return path.startswith(g[:-3] + "/") or path == g[:-3]
    if g.startswith("**/"):
        return fnmatch.fnmatch(os.path.basename(path), g[3:]) or fnmatch.fnmatch(path, g[3:]) or fnmatch.fnmatch(path, g)
    if "/" not in g:
        return "/" not in path and fnmatch.fnmatch(path, g)
    return fnmatch.fnmatch(path, g)


def owner_of(path, tiles):
    best, bs = None, -1
    for t in tiles:
        for g in t["owns"]:
            if match(path, g):
                s = glob_spec(g) + (1000 if "*" not in g else 0)
                if s > bs:
                    best, bs = t, s
    return best


def tile_of(m, level, path):
    """owner_of, plus two feature-level rules: docs are never product unless the map's
    product_docs globs say so (a plugin's skills), and app-wide stylesheets and theme files
    stay shared unless the map names the exact path."""
    tiles = m["levels"][level]["tiles"]
    if level == "features":
        if re.search(DOCS_RE, path) and not any(match(path, g) for g in m.get("product_docs", [])):
            return None
        if re.search(SHARED_STYLE_RE, path) and not any(path in t["owns"] for t in tiles):
            return None
    return owner_of(path, tiles)


def named_in(text, path):
    """The plan names a file: its path, its file name, or its stem in backticks."""
    if not text:
        return False
    t, base = text.lower(), os.path.basename(path).lower()
    stem = os.path.splitext(base)[0]
    return path.lower() in t or re.search(r"(?<![\w.-])" + re.escape(base) + r"(?![\w-])", t) is not None or "`" + stem + "`" in t


def expected_from_tiles(m, names):
    """Claude maps the plan to tiles (hook or command) and hands us ids or names.
    Split them by level; unknown names are dropped, never guessed."""
    want = [w.strip().lower() for w in names if w and w.strip()]
    exp = {}
    for lv in LEVELS:
        exp[lv] = sorted(t["id"] for t in m["levels"][lv]["tiles"] if t["id"].lower() in want or t["name"].lower() in want)
    return exp


def plan_tiles_from_text(text):
    """A PR body (or plan) may carry 'Readback-Plan: Tile, Tile' written by Claude.
    Without that line there is no mapping, so no flags - no keyword guessing."""
    for line in (text or "").splitlines():
        mm = re.match(r"\s*Readback-Plan:\s*(.+)", line, re.I)
        if mm:
            return [x for x in re.split(r"[,;]", mm.group(1)) if x.strip()]
    return None


def tile_list(m):
    out = []
    for lv in LEVELS:
        out.append(lv + ": " + "; ".join(t["id"] + " (" + t["name"] + ")" for t in m["levels"][lv]["tiles"]))
    return "\n".join(out)


# ---------------------------------------------------------------- what kind of change
def commit_log(rng=None):
    """[{subject, body, lines}] for the commits behind a diff (none for uncommitted work)."""
    if rng:
        args = ["-1", rng] if ".." not in rng else [rng]
    else:
        base = base_ref()
        if base == "HEAD":
            return []
        args = [base + "..HEAD"]
    out = git(["log", "--no-merges", "--numstat", "--format=%x00%s%x01%b%x02"] + args)
    commits = []
    for chunk in out.split("\x00")[1:]:
        head, _, stat = chunk.partition("\x02")
        subj, _, body = head.partition("\x01")
        n = 0
        for line in stat.splitlines():
            p = line.split("\t")
            if len(p) >= 3:
                n += (int(p[0]) if p[0].isdigit() else 1) + (int(p[1]) if p[1].isdigit() else 0)
        commits.append({"subject": subj.strip(), "body": body.strip(), "lines": n})
    return commits


def commit_kind(c):
    s, text = c.get("subject", ""), c.get("subject", "") + "\n" + c.get("body", "")
    if re.search(REVERT_RE, s):
        return "revert"
    if re.match(r"(?i)(feat|fix)\b", s):
        return "product"  # a feature or fix says so, whatever the body adds
    if re.search(NOCHANGE_RE, text):
        return "refactor"
    mm = re.match(CHORE_RE, s)
    if mm:
        k = mm.group(1).lower()
        return {"doc": "docs", "test": "tests", "chore": "tooling", "build": "tooling", "style": "refactor", "perf": "refactor"}.get(k, k)
    return "product"


KIND_SAYS = {"revert": "it reverts earlier work back to a previous state", "refactor": "it restructures code without changing behaviour",
             "ci": "it changes CI and automation", "docs": "it changes docs only", "tests": "it changes tests only",
             "tooling": "it changes tooling, tests and docs"}


def change_kind(commits, files, m=None):
    """'product' unless the commit messages or the file mix say this is no product change.
    Files any feature owns are product even when they look like docs or tests (a plugin's skills,
    an eval harness). Audience only changes ranking, never whether work counts."""
    total = sum(c["lines"] for c in commits) or 0
    if commits and total:
        by = {}
        for c in commits:
            k = commit_kind(c)
            by[k] = by.get(k, 0) + c["lines"]
        if by.get("product", 0) / total < 0.2:
            k = max((k for k in by if k != "product"), key=lambda k: by[k])
            return {"kind": k, "says": KIND_SAYS[k]}
    lines = sum(files.values()) or 1
    owned = (lambda p: tile_of(m, "features", p)) if m else (lambda p: None)
    tool = sum(n for p, n in files.items() if re.search(TOOLING_RE, p) and not owned(p))
    # a fix that lands with a big test is still a fix: any non-test source file means product
    src = any(re.search(CODE_RE, p) and not re.search(TOOLING_RE, p) for p in files)
    if files and tool / lines >= 0.85 and not src:
        return {"kind": "tooling", "says": KIND_SAYS["tooling"]}
    return {"kind": "product", "says": ""}


def lens_score(t, info, lens, unplanned):
    lines, n = info["lines"], info["files"]
    if info["sweep"]:
        return 0
    if lens == "change":
        return lines
    if lens == "risk":
        rl = info.get("risk_lines", lines)  # tests, docs and lockfiles carry no risk of their own
        return rl * (3 if t.get("risk") == "high" else 0.4) + (rl if unplanned else 0)
    if lens == "money":
        return lines if t.get("money") else 0
    if lens == "owner":
        return lines if t.get("owner") else 0
    return lines


def resolve(m, files, level="features", lens="change", expected=None, phase="DONE", kind=None, plan_text=None):
    tiles = m["levels"][level]["tiles"]
    kind = kind or {"kind": "product", "says": ""}
    quiet = kind["kind"] != "product" and phase != "PLAN"
    touched, unmapped = {}, []
    for p, n in files.items():
        t = tile_of(m, level, p)
        if not t:
            unmapped.append(p)
            continue
        e = touched.setdefault(t["id"], {"lines": 0, "files": 0, "paths": [], "risk_lines": 0})
        e["lines"] += n
        e["files"] += 1
        e["paths"].append(p)
        e["risk_lines"] += 0 if re.search(RISK_SKIP_RE, p) else n
    for e in touched.values():
        e["sweep"] = e["files"] >= 8 and e["lines"] / e["files"] <= 4
    # A one- or two-line brush next to real work is not a change to that feature
    top = max([e["lines"] for e in touched.values() if not e["sweep"]] or [0])
    for e in touched.values():
        e["brushed"] = not e["sweep"] and len(touched) > 1 and e["lines"] <= max(3, 0.03 * top) and e["lines"] < top
    expected = set(expected or []) if not quiet else set()
    # Flags need a confidence bar: a tile whose files the plan names was planned, and a
    # planned tile is not called untouched once any file the plan names has changed
    named = {tid: any(named_in(plan_text, p) for p in e["paths"]) for tid, e in touched.items()}
    plan_files_changed = any(named_in(plan_text, p) for p in files)
    if phase == "PLAN":
        touched = {tid: {"lines": 1, "files": 0, "paths": [], "sweep": False, "brushed": False} for tid in expected}
    by_id = {t["id"]: t for t in tiles}
    scores = {}
    for t in tiles:
        if t["id"] in touched and not touched[t["id"]]["brushed"]:
            unpl = bool(expected) and t["id"] not in expected and not named.get(t["id"])
            scores[t["id"]] = lens_score(t, touched[t["id"]], lens, unpl)
    if quiet and level == "features":
        scores = {}  # no product change: nothing to headline at the feature level

    def rank_key(kv):
        # change and money answer "what changed for customers": customer-facing tiles first
        cust = by_id[kv[0]].get("audience", "customer") == "customer" if lens in ("change", "money") else True
        return (not cust, -kv[1])
    ranked = [tid for tid, s in sorted(scores.items(), key=rank_key) if s > 0]
    budget = max(1, len(tiles) // 2)
    fill = {}
    for i, tid in enumerate(ranked[:budget]):
        top = max(scores[ranked[0]], 1)
        fill[tid] = "strong" if (i == 0 or scores[tid] >= 0.5 * top) and i < 2 else "light"
    discs = {tid: i + 1 for i, tid in enumerate(ranked[:3])}
    board = []
    for t in tiles:
        tid = t["id"]
        info = touched.get(tid)
        board.append({
            "id": tid, "name": t["name"], "sub": t.get("sub", ""),
            "touched": bool(info), "sweep": bool(info and info["sweep"]),
            "brushed": bool(info and info["brushed"]), "quiet": bool(info) and quiet and level == "features",
            "lines": info["lines"] if info else 0, "files": info["files"] if info else 0,
            "paths": (info or {}).get("paths", [])[:8],
            "fill": fill.get(tid, "none"), "ring": tid in expected,
            "disc": discs.get(tid),
            "risk_lines": (info or {}).get("risk_lines", 0),
            "unplanned": bool(info) and not info["brushed"] and bool(expected) and tid not in expected and not named.get(tid) and phase != "PLAN",
            "missed": tid in expected and not (info and not info["brushed"]) and not plan_files_changed and phase != "PLAN",
            "risk": t.get("risk"), "money": t.get("money"), "owner": t.get("owner", ""),
            "audience": t.get("audience", "customer")})
    return {"product": m.get("product", ""), "level": level, "lens": lens, "phase": phase,
            "kind": kind["kind"], "tiles": board, "unmapped": unmapped, "files": len(files),
            "sentence": sentence(board, lens, phase, kind), "acts": acts(m, board, lens, kind)}


def sentence(board, lens, phase, kind=None):
    kind = kind or {"kind": "product"}
    if kind["kind"] != "product" and phase != "PLAN":
        lead = ("Revert, no new product change: " if kind["kind"] == "revert" else "No product change: ") + kind["says"] + "."
        q = [b["name"] for b in board if b.get("quiet")]
        if q:
            return lead + " Code edits in " + ", ".join(q[:4]) + ("" if len(q) <= 4 else " and %d more" % (len(q) - 4)) + "."
        by = {b["disc"]: b for b in board if b["disc"]}
        if by and lens == "risk":
            return lead + " Riskiest part: " + by[1]["name"] + " " + DISC[0] + "."
        return lead
    by = {b["disc"]: b for b in board if b["disc"]}
    if not by:
        if any(b["touched"] for b in board):
            return "Only sweeps here (many tiny edits); nothing stands out for this lens."
        return "Nothing at this level changed." if phase != "PLAN" else "No tiles predicted yet."
    verb = {"change": "changed most", "risk": "carries the most risk", "money": "touches money",
            "owner": "needs its owner"}[lens]
    if phase == "PLAN":
        verb = "is expected to change"
    s = by[1]["name"] + " " + DISC[0] + " " + verb
    rest = [by[i]["name"] + " " + DISC[i - 1] for i in (2, 3) if i in by]
    if rest:
        s += "; also " + " and ".join(rest)
    s += "."
    un = [b["name"] for b in board if b["unplanned"]]
    if un:
        s += " Not in the plan: " + ", ".join(un[:3]) + "."
    mi = [b["name"] for b in board if b["missed"]]
    if mi:
        s += " Planned but untouched: " + ", ".join(mi[:3]) + "."
    return s


def acts(m, board, lens, kind=None):
    product = (kind or {"kind": "product"})["kind"] == "product"
    out = []
    for b in board:
        if not b["touched"] or b["sweep"] or b.get("brushed"):
            continue
        if b["risk"] == "high" and b.get("risk_lines", b["lines"]) >= 10:
            out.append({"who": b["owner"] or "engineer", "do": "Review " + b["name"] + " before merge", "why": "risk", "tile": b["id"]})
        if not product:
            continue  # no product change: no money or plan questions to ask anyone
        if b["money"]:
            out.append({"who": "finance", "do": "Check money impact in " + b["name"], "why": "money", "tile": b["id"]})
        if b["unplanned"]:
            out.append({"who": "pm", "do": "Confirm the unplanned change to " + b["name"], "why": "unplanned", "tile": b["id"]})
    seen, uniq = set(), []
    for a in out:
        k = (a["who"], a["tile"], a["why"])
        if k not in seen:
            seen.add(k)
            uniq.append(a)
    return uniq[:7]


def atlas(m, files, expected_by_level, kind=None, plan_text=None):
    cells = {}
    for lv in LEVELS:
        for ln in LENSES:
            b = resolve(m, files, lv, ln, expected_by_level.get(lv), kind=kind, plan_text=plan_text)
            mark = "○"
            if any(t["fill"] != "none" for t in b["tiles"]):
                mark = "◐"
            rel = [a for a in b["acts"] if a["why"] == {"risk": "risk", "money": "money"}.get(ln, "unplanned" if ln == "change" else "-")]
            if rel:
                mark = "●"
            if ln == "change" and any(t["unplanned"] for t in b["tiles"]):
                mark = "Δ" if mark != "●" else "●"
            cells[lv + "|" + ln] = mark
    return cells


# ---------------------------------------------------------------- render
def bar(n, w=6):
    return "█" * w if n == "strong" else ("▒" * w if n == "light" else " " * w)


def render_term(b, width=80):
    L = []
    head = "Readback · " + b["product"] + " · " + b["phase"] + " · " + b["level"].title() + " · " + b["lens"].title()
    L.append(head[:width])
    L.append("─" * min(width, 78))
    for line in wrap(b["sentence"], width):
        L.append(line)
    L.append("")
    shown = sorted([t for t in b["tiles"] if shown_tile(t)], key=lambda t: (t["disc"] or 9, {"strong": 0, "light": 1}.get(t["fill"], 2), -t["lines"]))
    rest = [t for t in b["tiles"] if not (t["touched"] or t["ring"])]
    for t in shown:
        mark = "◯" if t["ring"] else " "
        disc = DISC[t["disc"] - 1] if t["disc"] else " "
        tag = "sweep" if t["sweep"] else ("unplanned" if t["unplanned"] else ("not touched" if t["missed"] else ""))
        meta = (plural(t["files"], "file") + ", " + plural(t["lines"], "line")) if t["touched"] and b["phase"] != "PLAN" else ""
        L.append((" " + disc + " " + mark + " " + bar(t["fill"]) + " " + t["name"].ljust(22)[:22] + " " + meta.ljust(22) + " " + tag)[:width].rstrip())
    quiet = [t["name"] for t in b["tiles"] if t.get("quiet")]
    if quiet:
        L.append(("   · code only, no product change: " + ", ".join(quiet))[:width])
    brushed = [t["name"] + " (" + plural(t["lines"], "line") + ")" for t in b["tiles"] if t.get("brushed") and not t.get("quiet")]
    if brushed:
        L.append(("   · brushed: " + ", ".join(brushed))[:width])
    if rest:
        L.append(("   · untouched: " + ", ".join(t["name"] for t in rest))[:width])
    if b["acts"]:
        L.append("")
        for a in b["acts"]:
            L.append(("   ● " + a["who"] + ": " + a["do"])[:width])
    if b["unmapped"]:
        if b["level"] == "features":
            L.append(("   + " + str(len(b["unmapped"])) + " shared files (no single feature)")[:width])
        else:
            L.append(("   ? " + str(len(b["unmapped"])) + " files not on the map - run /readback check")[:width])
    L.append("   key: █ strong ▒ light ◯ expected ①②③ in sentence")
    return "\n".join(L)


def shown_tile(t):
    """Tiles drawn as touched: real work, not code-only (no product change) or a one-line brush."""
    return (t["touched"] and not t.get("quiet") and not t.get("brushed")) or t["ring"]


def render_line(b):
    if b.get("kind", "product") != "product":
        return "Readback: no product change (" + b["kind"] + ")  (/readback to see the board)"
    t = [x for x in b["tiles"] if x["touched"] and not x.get("brushed")]
    un = sum(1 for x in b["tiles"] if x["unplanned"])
    s = "Readback: " + str(len(t)) + " " + b["level"] + " touched"
    if un:
        s += " · " + str(un) + " unplanned"
    if b["acts"]:
        s += " · " + str(len(set(a["who"] for a in b["acts"]))) + " people act"
    return s + "  (/readback to see the board)"


def render_trailer(b):
    t = [x["name"] for x in b["tiles"] if x["touched"] and not x["sweep"] and not x.get("brushed") and not x.get("quiet")]
    lines = ["Readback-" + b["level"].title() + ": " + (", ".join(t) or ("none (" + b["kind"] + ")" if b.get("kind", "product") != "product" else "none"))]
    un = [x["name"] for x in b["tiles"] if x["unplanned"]]
    if un:
        lines.append("Readback-Unplanned: " + ", ".join(un))
    if b["acts"]:
        lines.append("Readback-Acts: " + "; ".join(a["who"] + " " + a["do"].lower() for a in b["acts"][:3]))
    return "\n".join(l[:72] for l in lines)


def render_md(boards, atl=None):
    b = boards[0]
    L = ["### Readback · " + b["product"], "", "> " + b["sentence"], ""]
    for bb in boards:
        L.append("**" + bb["level"].title() + " · " + bb["lens"].title() + "**  ")
        cells = []
        for t in bb["tiles"]:
            if not shown_tile(t):
                continue
            f = {"strong": "🟪", "light": "🟣", "none": "⬜"}[t["fill"]] if not t["sweep"] else "▫️"
            d = DISC[t["disc"] - 1] if t["disc"] else ""
            extra = " *(unplanned)*" if t["unplanned"] else (" *(planned, untouched)*" if t["missed"] else "")
            cells.append(f + " " + d + " " + t["name"] + extra)
        L.append(" · ".join(cells) or "_nothing at this level_")
        quiet = [t["name"] for t in bb["tiles"] if t.get("quiet")]
        if quiet:
            L.append("<sub>code only, no product change: " + ", ".join(quiet) + "</sub>")
        L.append("")
    if atl:
        L.append("| | " + " | ".join(l.title() for l in LENSES) + " |")
        L.append("|---|" + "---|" * len(LENSES))
        for lv in LEVELS:
            L.append("| " + lv.title() + " | " + " | ".join(atl[lv + "|" + ln] for ln in LENSES) + " |")
        L.append("")
    if b["acts"]:
        L.append("**Before launch**")
        for a in b["acts"]:
            L.append("- [ ] **" + a["who"] + "**: " + a["do"])
        L.append("")
    if b["unmapped"]:
        L.append("<sub>" + str(len(b["unmapped"])) + " files are not on the map yet.</sub>")
    L.append("<sub>🟪 strong · 🟣 light · ⬜ touched, no answer for this lens · ① ② ③ cited above</sub>")
    return "\n".join(L)


def render_slack(b, link=""):
    who = sorted(set(a["who"] for a in b["acts"]))
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": "Readback · " + b["product"]}},
              {"type": "section", "text": {"type": "mrkdwn", "text": b["sentence"]}}]
    for a in b["acts"]:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "● *" + a["who"] + "*: " + a["do"]}})
    if link:
        blocks.append({"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "Open board"}, "url": link}]})
    return {"notify": who, "blocks": blocks}


def svg_board(b):
    tiles = b["tiles"]
    cols = 4
    W, H, G = 170, 74, 14
    rows = (len(tiles) + cols - 1) // cols
    w, h = cols * (W + G) + G, rows * (H + G) + G
    out = ['<svg class="board" viewBox="0 0 %d %d" role="img" aria-label="%s">' % (w, h, html.escape(b["sentence"]))]
    for i, t in enumerate(tiles):
        x, y = G + (i % cols) * (W + G), G + (i // cols) * (H + G)
        cls = "t " + ("touched " if t["touched"] else "un ") + ("sweep " if t["sweep"] else "") + "f-" + t["fill"]
        if t["ring"]:
            out.append('<rect class="ring" x="%d" y="%d" width="%d" height="%d" rx="12"/>' % (x - 5, y - 5, W + 10, H + 10))
        out.append('<g class="%s"><rect x="%d" y="%d" width="%d" height="%d" rx="9"/>' % (cls, x, y, W, H))
        out.append('<text class="nm" x="%d" y="%d">%s</text>' % (x + 12, y + 28, html.escape(t["name"][:20])))
        sub = "sweep" if t["sweep"] else ("code only" if t.get("quiet") else ("brushed" if t.get("brushed") else None))
        sub = sub or ("unplanned" if t["unplanned"] else ("planned, untouched" if t["missed"] else (str(t["files"]) + " files" if t["touched"] and b["phase"] != "PLAN" else "")))
        out.append('<text class="sb" x="%d" y="%d">%s</text></g>' % (x + 12, y + 50, html.escape(sub)))
        if t["disc"]:
            out.append('<g class="disc"><circle cx="%d" cy="%d" r="11"/><text x="%d" y="%d">%d</text></g>' % (x + W - 4, y + 4, x + W - 4, y + 8, t["disc"]))
    out.append("</svg>")
    return "".join(out)


CSS = """
:root{--bg:#faf9f6;--card:#fff;--ink:#1d1c1a;--mut:#6b6a66;--line:#d8d6cf;--l:#dcdaf7;--s:#534ab7;--sink:#fff;--warn:#9a5b00}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#171716;--card:#21211f;--ink:#efeee9;--mut:#a3a29c;--line:#444441;--l:#3a3570;--s:#7f77dd;--sink:#fff;--warn:#f0b35a}}
:root[data-theme=dark]{--bg:#171716;--card:#21211f;--ink:#efeee9;--mut:#a3a29c;--line:#444441;--l:#3a3570;--s:#7f77dd;--sink:#fff;--warn:#f0b35a}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:820px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}.who{color:var(--mut);font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:16px 0}
.sent{font-size:16px;margin:4px 0 8px}
svg.board{width:100%;height:auto;display:block}
.t rect{fill:var(--card);stroke:var(--ink);stroke-width:1.6}.t.un rect{fill:none;stroke:var(--line);stroke-dasharray:4 4;stroke-width:1}
.t.sweep rect{stroke-width:.7}.t.f-light rect{fill:var(--l)}.t.f-strong rect{fill:var(--s)}
.nm{font-size:15px;font-weight:600;fill:var(--ink)}.sb{font-size:12px;fill:var(--mut)}.un .nm{fill:var(--mut);font-weight:400}
.f-strong .nm,.f-strong .sb{fill:var(--sink)}.ring{fill:none;stroke:var(--mut);stroke-width:1.4}
.disc circle{fill:var(--ink)}.disc text{fill:var(--bg);font-size:12px;text-anchor:middle;font-weight:700}
table{border-collapse:collapse;font-size:14px}td,th{border:1px solid var(--line);padding:6px 10px;text-align:center}
ul.acts{padding-left:18px}.key{color:var(--mut);font-size:12px}
@media (max-width:560px){.nm{font-size:17px}}
"""


def render_html(boards, atl=None):
    b = boards[0]
    parts = ['<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
             "<title>Readback board</title><style>" + CSS + "</style></head><body><main>",
             '<div class="who">Readback · ' + html.escape(b["product"]) + " · " + b["phase"] + "</div>",
             "<h1>" + html.escape(b["sentence"]) + "</h1>"]
    for bb in boards:
        parts.append('<section class="card"><div class="who">' + bb["level"].title() + " · " + bb["lens"].title() + "</div>")
        parts.append('<p class="sent">' + html.escape(bb["sentence"]) + "</p>" + svg_board(bb) + "</section>")
    if atl:
        rows = "".join("<tr><th>" + lv.title() + "</th>" + "".join("<td>" + atl[lv + "|" + ln] + "</td>" for ln in LENSES) + "</tr>" for lv in LEVELS)
        parts.append('<section class="card"><div class="who">Atlas</div><table><tr><th></th>' + "".join("<th>" + l.title() + "</th>" for l in LENSES) + "</tr>" + rows + "</table></section>")
    if b["acts"]:
        parts.append('<section class="card"><div class="who">Before launch</div><ul class="acts">' + "".join("<li><b>" + html.escape(a["who"]) + "</b>: " + html.escape(a["do"]) + "</li>" for a in b["acts"]) + "</ul></section>")
    parts.append('<p class="key">Solid outline = touched · dashed = untouched · fill = answer for the lens · ring = expected · numbers = cited in the sentence</p></main></body></html>')
    return "".join(parts)


def plural(n, w):
    return str(n) + " " + w + ("" if n == 1 else "s")


def wrap(s, w):
    out, cur = [], ""
    for word in s.split():
        if len(cur) + len(word) + 1 > w:
            out.append(cur)
            cur = word
        else:
            cur = (cur + " " + word).strip()
    if cur:
        out.append(cur)
    return out


# ---------------------------------------------------------------- runs (PLAN state)
def run_path(root, br=None):
    br = re.sub(r"[^A-Za-z0-9._-]+", "_", br or branch())
    return os.path.join(root, ".readback", "runs", br + ".json")


def load_run(root):
    p = run_path(root)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_run(root, run):
    p = run_path(root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(run, f, indent=1)


def record_plan(root, m, text, source, tiles=None):
    """Keep the plan text; only a real plan (ExitPlanMode, a PR body) mapped to tiles
    by Claude sets expectations. The opening prompt is context, never a plan."""
    run = load_run(root)
    if source == "prompt":
        if run.get("source") in REAL_PLANS:
            return run  # a real plan beats the opening prompt
        run.update({"source": "prompt", "text": text[:4000], "expected": {}})
    else:
        if text:
            run["text"] = text[:4000]
        run["source"] = source
        run["expected"] = expected_from_tiles(m, tiles) if tiles is not None else {}
        run["mapped"] = tiles is not None
    save_run(root, run)
    return run


# ---------------------------------------------------------------- CLI
def args_get(argv, flag, default=None):
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def plan_expected(run):
    """Expectations count only when they come from a real plan."""
    run = run or {}
    return run.get("expected", {}) if run.get("source") in REAL_PLANS else {}


def boards_for(m, files, run, level, lens, phase="DONE", kind=None):
    return resolve(m, files, level, lens, plan_expected(run).get(level), phase, kind, (run or {}).get("text"))


def cmd_show(argv):
    root = repo_root()
    os.chdir(root)
    m = load_map(root)
    if not m:
        print("No map yet. Run /readback init first.")
        return
    role = args_get(argv, "--as")
    level, lens = "features", "change"
    if role and role in m.get("roles", {}):
        level, lens = m["roles"][role]["home"]
    level = args_get(argv, "--level", level)
    lens = args_get(argv, "--lens", lens)
    fmt = args_get(argv, "--format", "term")
    pos = [a for i, a in enumerate(argv) if not a.startswith("--") and (i == 0 or not argv[i - 1].startswith("--"))]
    for a in list(pos):
        if a in LEVELS:
            level = a; pos.remove(a)
        elif a in LENSES:
            lens = a; pos.remove(a)
    rng = pos[0] if pos else None
    run = {} if rng else load_run(root)
    plan = args_get(argv, "--plan")
    if plan:  # a PR body: flags only if Claude left a Readback-Plan line in it
        text = sys.stdin.read() if plan == "-" else plan
        tiles = plan_tiles_from_text(text)
        run = {"source": "pr", "text": text, "expected": expected_from_tiles(m, tiles) if tiles else {}}
    exp_arg = args_get(argv, "--expected")
    if exp_arg:
        run = {"source": "plan", "text": (run or {}).get("text"), "expected": expected_from_tiles(m, exp_arg.split(","))}
    files = diff_files(rng)
    kind = change_kind(commit_log(rng), files, m)
    b = boards_for(m, files, run, level, lens, kind=kind)
    if fmt == "json":
        print(json.dumps(b, indent=1))
    elif fmt == "line":
        print(render_line(b))
    elif fmt == "trailer":
        print(render_trailer(b))
    elif fmt in ("md", "html"):
        exp = plan_expected(run)
        boards = [b] + [resolve(m, files, lv, ln, exp.get(lv), kind=kind, plan_text=run.get("text")) for lv, ln in (("parts", "risk"), ("features", "money")) if (lv, ln) != (level, lens)]
        atl = atlas(m, files, exp, kind, run.get("text"))
        out = render_md(boards, atl) if fmt == "md" else render_html(boards, atl)
        dest = args_get(argv, "--out")
        if dest:
            with open(dest, "w", encoding="utf-8") as f:
                f.write(out)
            print(dest)
        else:
            print(out)
    elif fmt == "slack":
        print(json.dumps(render_slack(b, args_get(argv, "--link", "")), indent=1))
    else:
        print(render_term(b))


def cmd_init(argv):
    root = repo_root()
    p = map_path(root)
    if os.path.exists(p) and "--force" not in argv:
        print("Map already exists at .readback/map.json (use --force to redraft).")
        return
    m = draft_map(root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=1)
    gi = os.path.join(root, ".readback", ".gitignore")
    with open(gi, "w") as f:
        f.write("runs/\n")
    print("Drafted .readback/map.json: %d features, %d parts." % (len(m["levels"]["features"]["tiles"]), len(m["levels"]["parts"]["tiles"])))
    print(cmd_check([], quiet=True))


CODE_RE = r"\.(dart|js|mjs|cjs|ts|tsx|jsx|py|go|rb|kt|swift|java|vue|svelte|html|css)$"


def shared_files(root, m, files, n=10):
    """Largest product-code files no feature owns (not tooling, not already marked shared)."""
    known = set(m.get("shared", []))
    out = []
    for p in files:
        if p in known or tile_of(m, "features", p) or re.search(TOOLING_RE, p) or not re.search(CODE_RE, p):
            continue
        try:
            with open(os.path.join(root, p), "rb") as f:
                out.append((f.read().count(b"\n"), p))
        except OSError:
            pass
    return sorted(out, reverse=True)[:n]


def cmd_check(argv, quiet=False):
    root = repo_root()
    m = load_map(root)
    if not m:
        return "No map."
    files = tracked(root)
    msg = []
    for lv in LEVELS:
        tiles = m["levels"][lv]["tiles"]
        un = [p for p in files if not tile_of(m, lv, p)]
        empty = [t["name"] for t in tiles if not any(match(p, g) for p in files for g in t["owns"])]
        msg.append("%s: %d tiles, %d of %d files unmapped (%.0f%%)%s" % (lv, len(tiles), len(un), len(files), 100.0 * len(un) / max(1, len(files)),
                   ("; empty tiles: " + ", ".join(empty)) if empty else ""))
    big = shared_files(root, m, files)
    if big:
        msg.append("")
        msg.append("Largest shared files (no feature owns them). Which feature owns each?")
        for n, p in big:
            msg.append("  %6d  %s" % (n, p))
        msg.append("Features: " + ", ".join(t["id"] for t in m["levels"]["features"]["tiles"]))
        msg.append("Answer with: rb.py own <path> <feature-id|shared>")
    out = "\n".join(msg)
    if not quiet:
        print(out)
    return out


def cmd_own(argv):
    """Record an owner's answer from /readback check: a file joins a feature, or stays shared."""
    root = repo_root()
    m = load_map(root)
    if not m or len(argv) < 2:
        print("usage: rb.py own <path> <feature-id|shared>")
        return
    path, tid = argv[0].replace("\\", "/"), argv[1]
    if tid == "shared":
        m.setdefault("shared", [])
        if path not in m["shared"]:
            m["shared"].append(path)
    else:
        t = next((t for t in m["levels"]["features"]["tiles"] if t["id"] == tid or t["name"].lower() == tid.lower()), None)
        if not t:
            print("No feature '%s'. Features: %s" % (tid, ", ".join(x["id"] for x in m["levels"]["features"]["tiles"])))
            return
        if path not in t["owns"]:
            t["owns"].append(path)
    with open(map_path(root), "w", encoding="utf-8") as f:
        json.dump(m, f, indent=1)
    print("%s -> %s" % (path, tid))


def cmd_plan(argv):
    """rb.py plan --tiles a,b [--source plan|pr]   Claude's mapping of the plan
       rb.py plan TEXT|-                           store the text; Claude still has to map it"""
    root = repo_root()
    m = load_map(root)
    if not m:
        print("No map yet. Run /readback init first.")
        return
    src = args_get(argv, "--source", "plan")
    tl = args_get(argv, "--tiles")
    if tl is not None:
        run = record_plan(root, m, "", src, [x for x in tl.split(",") if x.strip()])
        print(render_term(resolve(m, {}, "features", "change", run["expected"].get("features"), "PLAN")))
        return
    text = sys.stdin.read() if argv == ["-"] else " ".join(argv)
    record_plan(root, m, text, src)
    print(map_request(m))


def map_request(m):
    return ("Readback: map this plan to the product map. Pick only the tiles the plan says it will change "
            "(features and parts; leave out anything it does not ask for):\n" + tile_list(m) +
            "\nThen run: python \"" + os.path.abspath(__file__) + "\" plan --tiles id1,id2,...  "
            "Do this before editing; do not mention it to the user unless asked.")


def hook_input():
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def emit(msg):
    print(json.dumps({"systemMessage": msg}))


def hook_plan():
    data = hook_input()
    root = repo_root()
    m = load_map(root)
    if not m:
        return
    plan = (data.get("tool_input") or {}).get("plan", "")
    record_plan(root, m, plan, "plan")
    # Claude, not keywords, maps the plan to tiles: ask it to, in its own context
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": map_request(m)}}))


def hook_prompt():
    data = hook_input()
    root = repo_root()
    m = load_map(root)
    if not m:
        return
    run = load_run(root)
    if run.get("session") == data.get("session_id"):
        return  # only the opening request of a session is kept, as context
    run = record_plan(root, m, data.get("prompt", ""), "prompt")
    run["session"] = data.get("session_id")
    save_run(root, run)


def hook_done():
    data = hook_input()
    if data.get("stop_hook_active"):
        return
    root = repo_root()
    os.chdir(root)
    m = load_map(root)
    if not m:
        return
    files = diff_files()
    if not files:
        return
    digest = hashlib.sha1(json.dumps(files, sort_keys=True).encode()).hexdigest()
    run = load_run(root)
    if run.get("last_shown") == digest:
        return  # same diff as last time: stay quiet
    run["last_shown"] = digest
    save_run(root, run)
    b = boards_for(m, files, run, "features", "change", kind=change_kind(commit_log(), files, m))
    emit(render_term(b))


def main(argv):
    if not argv:
        print(__doc__)
        return
    c, rest = argv[0], argv[1:]
    if c == "init":
        cmd_init(rest)
    elif c == "show":
        cmd_show(rest)
    elif c == "check":
        cmd_check(rest)
    elif c == "plan":
        cmd_plan(rest)
    elif c == "own":
        cmd_own(rest)
    elif c == "hook-plan":
        hook_plan()
    elif c == "hook-prompt":
        hook_prompt()
    elif c == "hook-done":
        hook_done()
    else:
        cmd_show(argv)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except SystemExit as e:
        if e.code and not str(e.code).isdigit():
            print(e.code)
