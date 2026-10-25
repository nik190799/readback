#!/usr/bin/env python3
"""Readback: show a code change as a product picture.

One model, many surfaces. Every command reads the repo's map
(.readback/map.json) plus a git diff, resolves boards, and renders them.

  rb.py init [--force]              draft .readback/map.json from the repo tree
  rb.py show [RANGE] [--level L] [--lens X] [--as ROLE] [--format term|md|slack|html|json|trailer|line]
                                    [--expected a,b] [--plan TEXT|-]   (PR body with a Readback-Plan: line)
  rb.py plan --tiles a,b [--maybe c] [--source plan|pr] [--card FILE|-] [--format term|widget|html|json] [--out F]
                                    Claude's mapping of a real plan to tiles (and its card JSON); draws the
                                    "About to change" card, or the board when there is no card
  rb.py plan --show [--format ...]  the stored card or board again (widget = fragment for a visual tool)
  rb.py stats [--format json]       how plans held up: answers to the plan questions, plan vs done
  rb.py check                       validate the map; list the largest shared files to assign
  rb.py own PATH FEATURE|shared [--by a,b|all]   record who owns a shared file (and who uses it)
  rb.py hook-plan | hook-prompt | hook-edit | hook-done   (called by plugin hooks; JSON on stdin)

No dependencies beyond Python 3.8 and git.
"""
import fnmatch, hashlib, html, json, os, re, subprocess, sys, time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

LEVELS = ["features", "parts"]
LENSES = ["change", "risk", "money", "owner"]
DISC = "①②③④⑤"
# What each board is called on screen. The JSON "phase" field keeps PLAN / DONE.
PHASE_LABEL = {"PLAN": "About to change", "DONE": "What changed"}
# A plan is Claude's own mapping: after ExitPlanMode, from a PR body, or of its intent right before
# its first edit. The opening question is never a plan. Plan-mode and PR plans last for the branch;
# an intent mapping belongs to the session that made it.
REAL_PLANS = {"plan", "pr", "intent"}
KEEP_PLANS = {"plan", "pr"}
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
    # quotepath off: non-ASCII paths come back as text, not "\303\274"-quoted
    r = subprocess.run(["git", "-c", "core.quotepath=off"] + args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
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


def new_lines(p):
    """Lines in a new, untracked file (binary or unreadable files count as 1)."""
    try:
        with open(p, "rb") as f:
            data = f.read(2000000)
    except OSError:
        return 1
    return 1 if b"\0" in data[:8000] else max(data.count(b"\n"), 1)


def diff_files(rng=None):
    """Return {path: lines_changed}. rng None = branch work + uncommitted."""
    if rng:
        if ".." not in rng and not rng.startswith("#") and not git(["rev-parse", "--verify", "--quiet", rng + "^"]).strip():
            out = git(["show", "--numstat", "--format=", "-M", rng])  # the first commit has no parent
        else:
            if ".." not in rng and not rng.startswith("#"):
                rng = rng + "^.." + rng  # single commit
            out = git(["diff", "--numstat", "-M", rng])
    else:
        base = base_ref()
        out = git(["diff", "--numstat", "-M", base])
        for p in git(["ls-files", "--others", "--exclude-standard"]).splitlines():
            out += "%d\t0\t%s\n" % (new_lines(p), p)
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
    # (web/src/rooms.ts + server/app/rooms.py -> Rooms; src/page-home.js + index.html -> Home)
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


def spec(g):
    return glob_spec(g) + (1000 if "*" not in g else 0)


def owner_score(path, tiles):
    best, bs = None, -1
    for t in tiles:
        for g in t["owns"]:
            if match(path, g):
                s = spec(g)
                if s > bs:
                    best, bs = t, s
    return best, bs


def owner_of(path, tiles):
    return owner_score(path, tiles)[0]


def shared_rule(m, path):
    """The most specific shared entry for a path: (glob, user ids). "shared" lists paths the
    owner marked shared (no users); "shared_by" maps a path or glob to the features that use it
    (["*"] = every feature), e.g. the app bar every screen draws."""
    best, bs = None, -1
    for g in m.get("shared", []):
        if match(path, g) and spec(g) > bs:
            best, bs = (g, []), spec(g)
    for g, users in (m.get("shared_by") or {}).items():
        if match(path, g) and spec(g) >= bs:
            best, bs = (g, users), spec(g)
    return best, bs


def shared_users(m, path):
    rule = shared_rule(m, path)[0]
    if not rule:
        return []
    ids = [t["id"] for t in m["levels"]["features"]["tiles"]]
    return ids if "*" in rule[1] else [i for i in rule[1] if i in ids]


def tile_of(m, level, path):
    """owner_of, plus three feature-level rules: docs are never product unless the map's
    product_docs globs say so (a plugin's skills), app-wide stylesheets and theme files
    stay shared unless the map names the exact path, and a file the map marks shared
    belongs to no single feature unless a tile owns it more specifically."""
    tiles = m["levels"][level]["tiles"]
    if level == "features":
        if re.search(DOCS_RE, path) and not any(match(path, g) for g in m.get("product_docs", [])):
            return None
        if re.search(SHARED_STYLE_RE, path) and not any(path in t["owns"] for t in tiles):
            return None
        if m.get("shared") or m.get("shared_by"):
            best, bs = owner_score(path, tiles)
            rule, rs = shared_rule(m, path)
            return None if rule and rs >= bs else best
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


def couples(m, ids, skip=(), n=400, min_n=3, share=0.3):
    """Feature tiles that usually change in the same commit as a planned one, from recent
    history (sweeps of 5+ features say nothing), plus any the map lists under "couples"."""
    feats = {t["id"]: t for t in m["levels"]["features"]["tiles"]}
    ids = [i for i in ids if i in feats]
    if not ids:
        return []
    alone, together = {}, {}
    for chunk in git(["log", "-n", str(n), "--no-merges", "--name-only", "--format=%x1e"]).split("\x1e"):
        hit = [tile_of(m, "features", p.strip()) for p in chunk.splitlines() if p.strip() and not re.search(TOOLING_RE, p.strip())]
        ts = {t["id"] for t in hit if t}
        if not ts or len(ts) > 4:
            continue
        for a in ts.intersection(ids):
            alone[a] = alone.get(a, 0) + 1
            for b in ts - {a}:
                together[(a, b)] = together.get((a, b), 0) + 1
    seen, out = set(ids) | set(skip), []
    for a in ids:
        for b in feats[a].get("couples", []):
            if b in feats and b not in seen:
                seen.add(b)
                out.append({"tile": b, "name": feats[b]["name"], "with": a, "with_name": feats[a]["name"], "n": None, "of": None})
    for (a, b), c in sorted(together.items(), key=lambda kv: -kv[1]):
        if b not in seen and c >= min_n and c >= share * alone[a]:
            seen.add(b)
            out.append({"tile": b, "name": feats[b]["name"], "with": a, "with_name": feats[a]["name"], "n": c, "of": alone[a]})
    return out[:3]


def couple_text(c):
    why = " (%d of %d recent commits)" % (c["n"], c["of"]) if c["n"] else " (per the map)"
    return c["name"] + " usually changes with " + c["with_name"] + why + ". Include it?"


def resolve(m, files, level="features", lens="change", expected=None, phase="DONE", kind=None, plan_text=None, maybe=None):
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
    # Tiles Claude was less sure of: a dotted ring, never an "unplanned" or "untouched" flag
    maybe = set(maybe or []) - expected if not quiet else set()
    # A shared file (the app bar on every screen) changes each feature that uses it. That
    # confirms a planned or maybe feature; it never makes an unplanned one.
    via = {}
    if level == "features" and phase != "PLAN" and (m.get("shared") or m.get("shared_by")):
        for p in unmapped:
            for tid in shared_users(m, p):
                if tid in expected | maybe:
                    via.setdefault(tid, []).append(p)
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
            unpl = bool(expected) and t["id"] not in expected | maybe and not named.get(t["id"])
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
        board.append(dict({
            "id": tid, "name": t["name"], "sub": t.get("sub", ""),
            "touched": bool(info), "sweep": bool(info and info["sweep"]),
            "brushed": bool(info and info["brushed"]), "quiet": bool(info) and quiet and level == "features",
            "lines": info["lines"] if info else 0, "files": info["files"] if info else 0,
            "paths": (info or {}).get("paths", [])[:8],
            "fill": fill.get(tid, "none"), "ring": tid in expected or tid in maybe, "maybe": tid in maybe,
            "disc": discs.get(tid),
            "risk_lines": (info or {}).get("risk_lines", 0),
            "unplanned": bool(info) and not info["brushed"] and bool(expected) and tid not in expected | maybe and not named.get(tid) and phase != "PLAN",
            "missed": tid in expected and not (info and not info["brushed"]) and not plan_files_changed and tid not in via and phase != "PLAN",
            "risk": t.get("risk"), "money": t.get("money"), "owner": t.get("owner", ""),
            "audience": t.get("audience", "customer")}, **({"via_shared": via[tid][:8]} if tid in via else {})))
    return {"product": m.get("product", ""), "level": level, "lens": lens, "phase": phase,
            "kind": kind["kind"], "tiles": board, "unmapped": unmapped, "files": len(files),
            "sentence": sentence(board, lens, phase, kind), "acts": acts(m, board, lens, kind, phase)}


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
    mb = [b["name"] for b in board if b.get("maybe")]
    ask = (" Maybe: " + ", ".join(mb[:4]) + " (confirm before coding).") if mb and phase == "PLAN" else ""
    vs = [b for b in board if b.get("via_shared") and not (b["touched"] and not b["brushed"])]
    via = (" Changed through a shared file: " + ", ".join(b["name"] for b in vs[:3]) + " (" +
           ", ".join(sorted({os.path.basename(p) for b in vs for p in b["via_shared"]})[:2]) + ").") if vs else ""
    if not by:
        if via:
            return via.strip()
        if any(b["touched"] for b in board):
            return "Only sweeps here (many tiny edits); nothing stands out for this lens."
        return "Nothing at this level changed." if phase != "PLAN" else ("Nothing certain yet." + ask if mb else "No tiles predicted yet.")
    verb = {"change": "changed most", "risk": "carries the most risk", "money": "touches money",
            "owner": "needs its owner"}[lens]
    if phase == "PLAN":
        verb = "is expected to change"
    s = by[1]["name"] + " " + DISC[0] + " " + verb
    rest = [by[i]["name"] + " " + DISC[i - 1] for i in (2, 3) if i in by]
    if rest:
        s += "; also " + " and ".join(rest)
    s += "." + ask
    un = [b["name"] for b in board if b["unplanned"]]
    if un:
        s += " Not in the plan: " + ", ".join(un[:3]) + "."
    mi = [b["name"] for b in board if b["missed"] and not b.get("brushed")]
    if mi:
        s += " Planned but untouched: " + ", ".join(mi[:3]) + "."
    bt = [b["name"] for b in board if b["missed"] and b.get("brushed")]
    if bt:
        s += " Planned but barely touched: " + ", ".join(bt[:3]) + "."
    return s + via


def acts(m, board, lens, kind=None, phase="DONE"):
    product = (kind or {"kind": "product"})["kind"] == "product"
    out = []
    for b in board:
        if not b["touched"] or b["sweep"] or b.get("brushed"):
            continue
        if phase == "PLAN":
            # nothing is built yet: a heads-up for who will be needed, not a check to do now
            if b["risk"] == "high":
                out.append({"who": b["owner"] or "engineer", "do": "Plan a review of " + b["name"] + " (high risk)", "why": "risk", "tile": b["id"]})
            if b["money"]:
                out.append({"who": "finance", "do": b["name"] + " touches money, expect a check", "why": "money", "tile": b["id"]})
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


def atlas(m, files, expected_by_level, kind=None, plan_text=None, maybe_by_level=None):
    cells = {}
    for lv in LEVELS:
        for ln in LENSES:
            b = resolve(m, files, lv, ln, expected_by_level.get(lv), kind=kind, plan_text=plan_text, maybe=(maybe_by_level or {}).get(lv))
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
    head = "Readback · " + b["product"] + " · " + PHASE_LABEL.get(b["phase"], b["phase"]) + " · " + b["level"].title() + " · " + b["lens"].title()
    L.append(head[:width])
    L.append("─" * min(width, 78))
    for line in wrap(b["sentence"], width):
        L.append(line)
    L.append("")
    shown = sorted([t for t in b["tiles"] if shown_tile(t, b["phase"])], key=lambda t: (t["disc"] or 9, {"strong": 0, "light": 1}.get(t["fill"], 2), -t["lines"]))
    rest = [t for t in b["tiles"] if not t["touched"] and not shown_tile(t, b["phase"])]
    for t in shown:
        mark = "◌" if t.get("maybe") else ("◯" if t["ring"] else " ")
        disc = DISC[t["disc"] - 1] if t["disc"] else " "
        tag = "sweep" if t["sweep"] else ("unplanned" if t["unplanned"] else (("barely touched" if t.get("brushed") else "not touched") if t["missed"] else ("maybe" if t.get("maybe") and b["phase"] == "PLAN" else "")))
        meta = (plural(t["files"], "file") + ", " + plural(t["lines"], "line")) if t["touched"] and b["phase"] != "PLAN" else ""
        if t.get("via_shared") and not (t["touched"] and not t["brushed"]):
            vp = t["via_shared"]
            meta = "via " + os.path.basename(vp[0]) + (" +%d" % (len(vp) - 1) if len(vp) > 1 else "")
        L.append((" " + disc + " " + mark + " " + bar(t["fill"]) + " " + t["name"].ljust(22)[:22] + " " + meta.ljust(22) + " " + tag)[:width].rstrip())
    quiet = [t["name"] for t in b["tiles"] if t.get("quiet")]
    if quiet:
        note(L, "code only, no product change", quiet, width)
    brushed = [t["name"] + " (" + plural(t["lines"], "line") + ")" for t in b["tiles"] if t.get("brushed") and not t.get("quiet")]
    if brushed:
        note(L, "brushed", brushed, width)
    if rest:
        note(L, "not in the plan" if b["phase"] == "PLAN" else "untouched", [t["name"] for t in rest], width)
    if b.get("couples"):
        L.append("")
        for c in b["couples"]:
            for i, line in enumerate(wrap(couple_text(c), width - 5)):
                L.append(("   ? " if i == 0 else "     ") + line)
    if b["acts"]:
        L.append("")
        for a in b["acts"]:
            L.append(("   ● " + a["who"] + ": " + a["do"])[:width])
    if b["unmapped"]:
        if b["level"] == "features":
            L.append(("   + " + plural(len(b["unmapped"]), "shared file") + " (no single feature)")[:width])
        else:
            L.append(("   ? " + plural(len(b["unmapped"]), "file") + " not on the map - run /readback check")[:width])
    if b["phase"] == "PLAN":
        L.append("   key: ◯ will change ◌ maybe, confirm ①②③ in sentence")
    else:
        L.append("   key: █ strong ▒ light ◯ expected " + ("◌ maybe " if any(t.get("maybe") for t in b["tiles"]) else "") + "①②③ in sentence")
    return "\n".join(L)


def shown_tile(t, phase="DONE"):
    """Tiles drawn as touched: real work, not code-only (no product change) or a one-line brush,
    plus planned tiles; a 'maybe' that never happened is just untouched once the work is done."""
    return (t["touched"] and not t.get("quiet") and not t.get("brushed")) or (t["ring"] and (phase == "PLAN" or not t.get("maybe") or bool(t.get("via_shared"))))


def render_line(b):
    if b.get("kind", "product") != "product":
        return "Readback: no product change (" + b["kind"] + ")  (/readback to see the board)"
    t = [x for x in b["tiles"] if x["touched"] and not x.get("brushed")]
    un = sum(1 for x in b["tiles"] if x["unplanned"])
    s = "Readback: " + plural(len(t), b["level"][:-1]) + " touched"
    if un:
        s += " · " + str(un) + " unplanned"
    if b["acts"]:
        n = len(set(a["who"] for a in b["acts"]))
        s += " · " + ("1 person acts" if n == 1 else str(n) + " people act")
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
            if not shown_tile(t, bb["phase"]):
                continue
            f = {"strong": "🟪", "light": "🟣", "none": "⬜"}[t["fill"]] if not t["sweep"] else "▫️"
            d = DISC[t["disc"] - 1] if t["disc"] else ""
            extra = " *(unplanned)*" if t["unplanned"] else ((" *(planned, barely touched)*" if t.get("brushed") else " *(planned, untouched)*") if t["missed"] else "")
            if t.get("via_shared") and not (t["touched"] and not t["brushed"]):
                extra = " *(via shared " + ", ".join("`" + os.path.basename(p) + "`" for p in t["via_shared"][:2]) + ")*"
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
        L.append("**Heads-up**" if b["phase"] == "PLAN" else "**Before launch**")
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
        if t["ring"] and (b["phase"] == "PLAN" or t["touched"] or not t.get("maybe") or t.get("via_shared")):
            out.append('<rect class="ring%s" x="%d" y="%d" width="%d" height="%d" rx="12"/>' % (" maybe" if t.get("maybe") else "", x - 5, y - 5, W + 10, H + 10))
        out.append('<g class="%s"><rect x="%d" y="%d" width="%d" height="%d" rx="9"/>' % (cls, x, y, W, H))
        out.append('<text class="nm" x="%d" y="%d">%s</text>' % (x + 12, y + 28, html.escape(t["name"][:20])))
        sub = "sweep" if t["sweep"] else ("code only" if t.get("quiet") else ("brushed" if t.get("brushed") else None))
        sub = sub or ("unplanned" if t["unplanned"] else ("planned, untouched" if t["missed"] else (str(t["files"]) + " files" if t["touched"] and b["phase"] != "PLAN" else "")))
        if t.get("via_shared") and not (t["touched"] and not t["brushed"]) and b["phase"] != "PLAN":
            sub = "via shared file"
        if b["phase"] == "PLAN":
            sub = "maybe, confirm" if t.get("maybe") else ("will change" if t["ring"] else "")
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
.f-strong .nm,.f-strong .sb{fill:var(--sink)}.ring{fill:none;stroke:var(--mut);stroke-width:1.4}.ring.maybe{stroke-dasharray:1.5 4;stroke-linecap:round;stroke-width:1.8}
.disc circle{fill:var(--ink)}.disc text{fill:var(--bg);font-size:12px;text-anchor:middle;font-weight:700}
table{border-collapse:collapse;font-size:14px}td,th{border:1px solid var(--line);padding:6px 10px;text-align:center}
ul.acts{padding-left:18px}.key{color:var(--mut);font-size:12px}
@media (max-width:560px){.nm{font-size:17px}}
"""


def render_html(boards, atl=None):
    b = boards[0]
    parts = ['<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
             "<title>Readback board</title><style>" + CSS + "</style></head><body><main>",
             '<div class="who">Readback · ' + html.escape(b["product"]) + " · " + PHASE_LABEL.get(b["phase"], b["phase"]) + "</div>",
             "<h1>" + html.escape(b["sentence"]) + "</h1>"]
    for bb in boards:
        parts.append('<section class="card"><div class="who">' + bb["level"].title() + " · " + bb["lens"].title() + "</div>")
        parts.append('<p class="sent">' + html.escape(bb["sentence"]) + "</p>" + svg_board(bb) + "</section>")
    if atl:
        rows = "".join("<tr><th>" + lv.title() + "</th>" + "".join("<td>" + atl[lv + "|" + ln] + "</td>" for ln in LENSES) + "</tr>" for lv in LEVELS)
        parts.append('<section class="card"><div class="who">Atlas</div><table><tr><th></th>' + "".join("<th>" + l.title() + "</th>" for l in LENSES) + "</tr>" + rows + "</table></section>")
    if b["acts"]:
        parts.append('<section class="card"><div class="who">' + ("Heads-up" if b["phase"] == "PLAN" else "Before launch") + '</div><ul class="acts">' + "".join("<li><b>" + html.escape(a["who"]) + "</b>: " + html.escape(a["do"]) + "</li>" for a in b["acts"]) + "</ul></section>")
    if b["phase"] == "PLAN":
        ask = ["<li><b>" + html.escape(t["name"]) + "</b>: maybe. Will it change?</li>" for t in b["tiles"] if t.get("maybe")]
        ask += ["<li>" + html.escape(couple_text(c)) + "</li>" for c in b.get("couples", [])]
        if ask:
            parts.append('<section class="card"><div class="who">Confirm before coding</div><ul class="acts">' + "".join(ask) + "</ul></section>")
        parts.append('<p class="key">Solid ring = will change · dotted ring = maybe, confirm · dashed = not planned · numbers = cited in the sentence</p></main></body></html>')
    else:
        parts.append('<p class="key">Solid outline = touched · dashed = untouched · fill = answer for the lens · ring = expected · dotted ring = maybe · numbers = cited in the sentence</p></main></body></html>')
    return "".join(parts)


# ---------------------------------------------------------------- card (the visual "About to change")
# Claude writes the card (words only); this code checks it, picks the sketches and draws them, so the
# same plan always gets the same picture.
MARKS = ("change", "new", "removed", "maybe", "risk")
LAYER_ORDER = ("screen", "logic", "data", "config", "outside")
RISK_SOURCES = ("plan", "possible")
HEDGE_RE = r"(?i)^\W*(risk|risky|may|might|could|possible|possibly|potential|warning|careful)\b"
SLOTS = [("start", "Before start"), ("merge", "Before merge"), ("release", "Before release")]
SKETCH_LABEL = {"tree": "Where", "flow": "What happens", "layers": "How deep", "radius": "How far", "before_after": "What moves"}
KIND_LABEL = {"flow": "behaviour change", "layers": "goes down the stack", "radius": "wide reach",
              "before_after": "moves things around", "tree": "small change"}


def clip(s, n, field, warn):
    s = " ".join(str(s or "").split())
    if len(s) > n:
        warn.append('%s "%s…" is over %d characters; cut.' % (field, s[:24], n))
        s = s[:n - 1].rstrip() + "…"
    return s


def map_tiles_by_key(m):
    by = {}
    for lv in LEVELS:
        for t in m["levels"][lv]["tiles"]:
            by.setdefault(t["id"].lower(), t)
            by.setdefault(t["name"].lower(), t)
    return by


def validate_card(m, card):
    """Check the card Claude wrote: tiles on the map, known marks and layers, a headline that says
    what changes. Bad parts are fixed or dropped and named in the warnings, never guessed."""
    if not isinstance(card, dict):
        return None, ["the card is not a JSON object; ignored."]
    warn, by = [], map_tiles_by_key(m)
    out = {"headline": clip(card.get("headline"), 90, "headline", warn)}
    if not out["headline"]:
        warn.append("headline is empty: say in plain words what will change.")
    elif re.match(HEDGE_RE, out["headline"]):
        warn.append('headline opens with a risk or hedge ("%s"): lead with the change, put the risk in "risk".' % out["headline"].split()[0])

    def enum(v, allowed, default, field):
        if v in allowed:
            return v
        if v is not None:
            warn.append('%s "%s" is not one of %s; used "%s".' % (field, v, "|".join(allowed), default))
        return default

    items = []
    for it in card.get("items") or []:
        if not isinstance(it, dict):
            continue
        mark = enum(it.get("mark"), MARKS, "change", "mark")
        layer = enum(it.get("layer"), LAYER_ORDER, "screen", "layer")
        what = clip(it.get("what"), 32, "item", warn)
        if it.get("new_tile"):
            items.append({"tile": None, "name": clip(it["new_tile"], 24, "new_tile", warn), "area": clip(it.get("area"), 24, "area", warn),
                          "what": what, "mark": mark if mark in ("new", "risk") else "new", "layer": layer})
            continue
        t = by.get(str(it.get("tile", "")).lower())
        if not t:
            warn.append('item tile "%s" is not on the map; dropped. Use a tile id, or "new_tile" for something new.' % it.get("tile"))
            continue
        items.append({"tile": t["id"], "name": t["name"], "area": t.get("area", ""), "what": what, "mark": mark, "layer": layer})
    out["items"] = items

    r = card.get("risk")
    out["risk"] = None
    if isinstance(r, dict) and str(r.get("text") or "").strip():
        rt = by.get(str(r.get("tile") or "").lower())
        if r.get("tile") and not rt:
            warn.append('risk tile "%s" is not on the map; ignored.' % r.get("tile"))
        out["risk"] = {"text": clip(r["text"], 100, "risk", warn), "source": enum(r.get("source"), RISK_SOURCES, "possible", "risk source"),
                       "tile": rt["id"] if rt else None}

    out["outside"] = [{"name": clip(o.get("name"), 24, "outside", warn), "what": clip(o.get("what"), 32, "outside", warn)}
                      for o in (card.get("outside") or [])[:3] if isinstance(o, dict) and o.get("name")]

    f = card.get("flow")
    if f:
        steps = [clip(s, 20, "flow step", warn) for s in (f.get("steps") or [])[:3] if str(s).strip()]
        br = [{"text": clip(b.get("text"), 30, "flow branch", warn), "mark": enum(b.get("mark"), MARKS, "change", "mark")}
              for b in (f.get("branches") or [])[:4] if isinstance(b, dict) and b.get("text")]
        if steps and len(br) >= 2:
            out["flow"] = {"steps": steps, "branches": br}
        else:
            warn.append("flow needs at least 1 step and 2 branches; left out.")
    rc = card.get("reach")
    if rc:
        near = [clip(x, 14, "reach", warn) for x in (rc.get("near") or [])[:3]]
        people = [clip(x, 14, "reach", warn) for x in (rc.get("people") or [])[:3]]
        if rc.get("core") and (near or people):
            out["reach"] = {"core": clip(rc["core"], 10, "reach core", warn), "near": near, "people": people}
        else:
            warn.append("reach needs a core and some near or people; left out.")
    ba = card.get("before_after")
    if ba:
        def groups(gs):
            return [[clip(g[0], 20, "before/after", warn), [clip(x, 20, "before/after", warn) for x in (g[1] if len(g) > 1 else [])[:4]]]
                    for g in (gs or [])[:4] if isinstance(g, (list, tuple)) and g]
        b, a = groups(ba.get("before")), groups(ba.get("after"))
        if b and a:
            out["before_after"] = {"before": b, "after": a}
        else:
            warn.append("before_after needs both before and after; left out.")

    acts_in = card.get("acts") if isinstance(card.get("acts"), dict) else {}
    out["acts"] = {k: clip(acts_in.get(k), 80, "acts." + k, warn) for k, _ in SLOTS if acts_in.get(k)}
    # A guessed risk never stands as a claim: it becomes a question for the author before work starts
    if out["risk"] and out["risk"]["source"] == "possible" and not out["acts"].get("start"):
        out["acts"]["start"] = clip("Ask the author: " + out["risk"]["text"].rstrip(".") + "?", 80, "acts.start", [])
    return out, warn


def card_digest(card):
    return hashlib.sha1(json.dumps(card, sort_keys=True).encode()).hexdigest()[:12]


def card_tiles(run):
    """Keys of everything the card draws: planned and maybe feature tiles, item tiles, new tiles."""
    c = run.get("card") or {}
    keys = list((run.get("expected") or {}).get("features", []))
    for it in c.get("items", []):
        keys.append(it["tile"] or "new:" + it["name"])
    keys += (run.get("maybe") or {}).get("features", [])
    seen = []
    for k in keys:
        if k not in seen:
            seen.append(k)
    return seen


def pick_sketches(m, card, keys):
    """Tree always. Then sketches the card has data for (flow, before/after, reach), then ones its
    items imply (layers when it goes down the stack, a radius when it spreads). Severity sets how many."""
    by = {t["id"]: t for lv in LEVELS for t in m["levels"][lv]["tiles"]}
    layers = {i["layer"] for i in card["items"]}
    r = card.get("risk")
    if any(by.get(k, {}).get("risk") == "high" for k in keys) or (r and r["source"] == "plan" and card.get("reach", {}).get("people")):
        sev = "high"
    elif not r and len(keys) <= 1 and layers <= {"screen"} and not card.get("outside"):
        sev = "low"
    else:
        sev = "medium"
    cands = [s for s, k in (("flow", "flow"), ("before_after", "before_after"), ("radius", "reach")) if card.get(k)]
    if len(layers) >= 2 or layers & {"data", "config", "outside"} or card.get("outside"):
        cands.append("layers")
    if len(keys) >= 3 and "radius" not in cands:
        cands.append("radius")
    return sev, ["tree"] + cands[:{"low": 0, "medium": 1, "high": 2}[sev]]


def card_model(m, run):
    card = run["card"]
    keys = card_tiles(run)
    sev, sk = pick_sketches(m, card, keys)
    return dict(card, product=m.get("product", "") or "Product", keys=keys, severity=sev, sketches=sk,
                kind=KIND_LABEL[sk[1] if len(sk) > 1 else "tree"], tree=tree_nodes(m, run))


def tree_nodes(m, run):
    """The pruned tree as (depth, label, mark) rows: product > area > tile > items. Only what the plan
    touches; the rest of the map is never drawn."""
    card, by = run["card"], {t["id"]: t for lv in LEVELS for t in m["levels"][lv]["tiles"]}
    maybe = set((run.get("maybe") or {}).get("features", []))
    risk_tile = (card.get("risk") or {}).get("tile")
    groups = {}
    for k in card_tiles(run):
        t = by.get(k)
        groups[k] = {"name": t["name"] if t else k[4:], "area": (t or {}).get("area", ""), "items": [], "maybe": k in maybe}
    for it in card["items"]:
        g = groups[it["tile"] or "new:" + it["name"]]
        g["items"].append(it)
        g["area"] = g["area"] or it.get("area", "")
    areas = {}
    for k, g in groups.items():
        areas.setdefault(g["area"], []).append(k)
    rows = [(0, run.get("product") or m.get("product", "") or "Product", "root")]
    for area, ks in areas.items():
        d = 1
        if area:
            rows.append((1, area, "area"))
            d = 2
        for k in ks:
            g, risky = groups[k], k == risk_tile
            its = g["items"]
            if not its:
                rows.append((d, g["name"] + (" · maybe" if g["maybe"] else " · will change"), "maybe" if g["maybe"] else ("risk" if risky else "change")))
            elif len(its) == 1:
                it = its[0]
                rows.append((d, g["name"] + (" · " + it["what"] if it["what"] else ""), "risk" if risky and it["mark"] == "change" else it["mark"]))
            else:
                rows.append((d, g["name"], "group-risk" if risky else "group"))
                rows += [(d + 1, it["what"] or it["layer"], it["mark"]) for it in its]
    if card.get("outside"):
        rows.append((1, "Outside the repo", "area"))
        rows += [(2, o["name"] + (" · " + o["what"] if o["what"] else ""), "outside") for o in card["outside"]]
    return rows


# Colours are the host's (the Claude app's visual tool sets these variables), with light fallbacks.
def V(name, fallback):
    return "var(--%s, %s)" % (name, fallback)


INK, SEC, MUT = V("text-primary", "#1f1e1d"), V("text-secondary", "#5f5e5a"), V("text-muted", "#888780")
LINE, LINE2 = V("border", "#d3d1c7"), V("border-strong", "#b4b2a9")
# mark: (fill, stroke, text, dash, strike)
MARK_STYLE = {
    "change": ("none", LINE2, INK, "", False),
    "new": (V("bg-success", "#eaf3de"), V("border-success", "#97c459"), V("text-success", "#3b6d11"), "", False),
    "removed": ("none", "none", V("text-danger", "#a32d2d"), "", True),
    "maybe": ("none", LINE2, SEC, "2 3", False),
    "risk": (V("bg-warning", "#faeeda"), V("border-warning", "#ef9f27"), V("text-warning", "#854f0b"), "", False),
    "outside": (V("bg-accent", "#e6f1fb"), V("border-accent", "#85b7eb"), V("text-accent", "#185fa5"), "3 3", False),
}
WARN_TX = MARK_STYLE["risk"][2]


def tw(s, size=12):
    """Rough text width in px for the sans font (good enough to size boxes)."""
    return int(len(s) * size * 0.5) + 18


def sv_text(x, y, s, fill, size=12, weight=400, anchor="start", strike=False):
    return '<text x="%d" y="%d" style="fill:%s;font-size:%dpx;font-weight:%d" text-anchor="%s"%s>%s</text>' % (
        x, y, fill, size, weight, anchor, ' text-decoration="line-through"' if strike else "", html.escape(s))


def sv_box(x, y, w, s, mark, size=12):
    fill, stroke, text, dash, strike = MARK_STYLE.get(mark, MARK_STYLE["change"])
    if strike:
        return sv_text(x + 8, y + 15, s, text, size, strike=True)
    return ('<rect x="%d" y="%d" width="%d" height="22" rx="5" style="fill:%s;stroke:%s"%s/>' % (
        x, y, w, fill, stroke, ' stroke-dasharray="%s"' % dash if dash else "") + sv_text(x + 8, y + 15, s, text, size))


def sv(w, h, body, label):
    return ('<svg viewBox="0 0 %d %d" width="100%%" role="img" aria-label="%s" style="display:block;max-width:%dpx">%s</svg>'
            % (w, h, html.escape(label), w, body))


def sk_tree(c):
    rows, out, last, y = c["tree"], [], {}, 6
    for d, label, mark in rows:
        x = 6 + d * 22
        if d:
            px, py = last[d - 1]
            out.append('<path d="M%d %d V%d H%d" style="fill:none;stroke:%s"/>' % (px + 8, py + 20, y + 11, x, LINE2))
        if mark in ("root", "area", "group", "group-risk"):
            out.append(sv_text(x, y + 15, label, WARN_TX if mark == "group-risk" else INK, 14 if d == 0 else 13, 500 if d == 0 else 400))
        else:
            out.append(sv_box(x, y, tw(label), label, mark))
        last[d] = (x, y)
        y += 28
    w = max([6 + d * 22 + tw(l) for d, l, _ in rows] + [200]) + 6
    return sv(w, y + 2, "".join(out), "Where it changes")


def layer_rows(c):
    """screen shows tile names; deeper layers show what changes there."""
    rows = []
    for ly in LAYER_ORDER:
        boxes, seen = [], set()
        for it in c["items"]:
            if it["layer"] != ly:
                continue
            label = it["name"] if ly == "screen" else (it["what"] or it["name"])
            mark = "risk" if it["mark"] == "risk" or (ly == "screen" and it["tile"] == (c.get("risk") or {}).get("tile")) else it["mark"]
            if label not in seen:
                seen.add(label)
                boxes.append((label, mark))
        if ly == "outside":
            boxes += [(o["name"], "outside") for o in c.get("outside", [])]
        rows.append((ly, boxes))
    return rows


def sk_layers(c):
    out, y = [], 4
    for ly, boxes in layer_rows(c):
        out.append(sv_text(4, y + 15, ly.title(), SEC, 11))
        x = 66
        if not boxes:
            out.append(sv_text(66, y + 15, "untouched", MUT, 11))
        for label, mark in boxes[:4]:
            out.append(sv_box(x, y, tw(label), label, mark))
            x += tw(label) + 8
        y += 30
        out.append('<path d="M62 %d H340" style="stroke:%s"/>' % (y - 4, LINE))
    return sv(340, y, "".join(out), "How deep it goes")


def sk_flow(c):
    f = c["flow"]
    br = f["branches"]
    cy, x, out = len(br) * 15 - 4, 4, []
    for i, s in enumerate(f["steps"]):
        w = tw(s)
        out.append(sv_box(x, cy, w, s, "change"))
        if i < len(f["steps"]) - 1:
            out.append('<path d="M%d %d H%d" style="stroke:%s"/>' % (x + w, cy + 11, x + w + 12, SEC))
        x += w + 12
    bx = x + 14
    bw = max([tw(b["text"]) for b in br])
    for i, b in enumerate(br):
        y = 4 + i * 30
        out.append('<path d="M%d %d L%d %d" style="fill:none;stroke:%s"/>' % (x - 12, cy + 11, bx, y + 11, SEC))
        out.append(sv_box(bx, y, bw, b["text"], b["mark"]))
    return sv(bx + bw + 4, len(br) * 30 + 6, "".join(out), "What happens")


def sk_radius(c):
    rc = c.get("reach") or {}
    if not rc:  # implied by a wide change: the riskiest tile in the middle, the others around it
        names = [g for d, g, mk in c["tree"] if mk not in ("root", "area", "group", "group-risk", "outside")]
        names = [n.split(" · ")[0] for n in names]
        core = next((n.split(" · ")[0] for d, n, mk in c["tree"] if mk in ("risk", "group-risk")), names[0] if names else "Change")
        rc = {"core": core[:10], "near": [n[:14] for n in names if n != core][:3], "people": []}
    out = ['<circle cx="170" cy="92" r="76" style="fill:none;stroke:%s" stroke-dasharray="3 4"/>' % LINE if rc["people"] else "",
           '<circle cx="170" cy="92" r="56" style="fill:none;stroke:%s"/>' % LINE2,
           '<circle cx="170" cy="92" r="27" style="fill:%s;stroke:%s"/>' % MARK_STYLE["risk"][:2],
           sv_text(170, 96, rc["core"], WARN_TX, 11, anchor="middle")]
    for (x, y), s in zip([(170, 50), (228, 96), (112, 96)], rc["near"]):
        out.append(sv_text(x, y, s, INK, 11, anchor="middle"))
    for (x, y), s in zip([(170, 11), (282, 170), (58, 170)], rc["people"]):
        out.append(sv_text(x, y, s, SEC, 11, anchor="middle"))
    return sv(340, 184, "".join(out), "How far it reaches")


def sk_before_after(c):
    ba, out = c["before_after"], []
    out.append(sv_text(4, 14, "Before", SEC, 11) + sv_text(184, 14, "After", SEC, 11))
    out.append('<path d="M150 60 H170 M165 55 L170 60 L165 65" style="fill:none;stroke:%s"/>' % SEC)

    def col(groups, x0):
        y = 24
        for g, kids in groups:
            out.append(sv_text(x0, y + 15, g, INK, 13))
            y += 26
            for k in kids:
                out.append(sv_text(x0 + 16, y + 15, k, INK, 12))
                y += 24
        return y
    h = max(col(ba["before"], 4), col(ba["after"], 184))
    return sv(340, h + 4, "".join(out), "What moves")


SKETCHES = {"tree": sk_tree, "layers": sk_layers, "flow": sk_flow, "radius": sk_radius, "before_after": sk_before_after}


def render_card_widget(c):
    """A fragment for the host's visual tool: no page wrapper, host colours, transparent background."""
    r = c.get("risk")
    tag = {"plan": "from plan", "possible": "possible"}
    risk = (('<p style="margin:0 0 10px;font-size:14px;color:%s">Risk: %s <span style="font-size:11px;padding:0 7px;border-radius:6px;background:%s">%s</span></p>'
             % (WARN_TX, html.escape(r["text"]), MARK_STYLE["risk"][0], tag[r["source"]])) if r else
            '<p style="margin:0 0 10px;font-size:13px;color:%s">No risk found</p>' % MUT)
    sk = "".join('<div style="min-width:0"><p style="margin:0 0 4px;font-size:11px;color:%s">%s</p>%s</div>' % (MUT, SKETCH_LABEL[s], SKETCHES[s](c))
                 for s in c["sketches"])
    slots = "".join('<div style="display:grid;grid-template-columns:110px 1fr;gap:8px;font-size:13px;padding:5px 0;border-top:0.5px solid %s">'
                    '<span style="color:%s">%s</span><span>%s</span></div>' % (LINE, SEC, name, html.escape(c["acts"][k]))
                    for k, name in SLOTS if c["acts"].get(k))
    return ('<div class="readback-card" style="position:relative;font-family:%s;color:%s;border:0.5px solid %s;border-radius:12px;padding:16px 18px">'
            '<h2 style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);margin:0">About to change: %s</h2>'
            '<p style="margin:0 0 4px;font-size:12px;color:%s">Readback · %s · About to change · %s · %s</p>'
            '<p style="margin:0 0 4px;font-size:17px;font-weight:500;line-height:1.4">%s</p>%s'
            '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px;margin:0 0 10px">%s</div>%s</div>'
            % (V("font-sans", "system-ui, sans-serif"), INK, LINE, html.escape(c["headline"]), SEC, html.escape(c["product"]), c["severity"], c["kind"],
               html.escape(c["headline"]), risk, sk, slots))


PAGE_VARS = {
    "light": "--text-primary:#1f1e1d;--text-secondary:#5f5e5a;--text-muted:#888780;--border:#d3d1c7;--border-strong:#b4b2a9;"
             "--bg-warning:#faeeda;--border-warning:#ef9f27;--text-warning:#854f0b;--bg-success:#eaf3de;--border-success:#97c459;"
             "--text-success:#3b6d11;--bg-accent:#e6f1fb;--border-accent:#85b7eb;--text-accent:#185fa5;--text-danger:#a32d2d;--page:#faf9f6",
    "dark": "--text-primary:#efeee9;--text-secondary:#b4b2a9;--text-muted:#888780;--border:#444441;--border-strong:#5f5e5a;"
            "--bg-warning:#412402;--border-warning:#854f0b;--text-warning:#fac775;--bg-success:#173404;--border-success:#3b6d11;"
            "--text-success:#c0dd97;--bg-accent:#042c53;--border-accent:#185fa5;--text-accent:#b5d4f4;--text-danger:#f09595;--page:#171716",
}


def render_card_html(c):
    css = (":root{%s}@media (prefers-color-scheme:dark){:root:not([data-theme=light]){%s}}:root[data-theme=dark]{%s}"
           "body{margin:0;background:var(--page);font:15px/1.55 system-ui,-apple-system,Segoe UI,sans-serif}"
           "main{max-width:880px;margin:0 auto;padding:24px 16px 64px}" % (PAGE_VARS["light"], PAGE_VARS["dark"], PAGE_VARS["dark"]))
    return ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            "<title>Readback plan</title><style>" + css + "</style></head><body><main>" + render_card_widget(c) + "</main></body></html>")


TERM_MARK = {"new": " (new)", "removed": " (removed)", "risk": "  ⚠", "group-risk": "  ⚠"}


def render_card_term(c, width=80):
    r = c.get("risk")
    L = [("Readback · " + c["product"] + " · About to change · " + c["severity"] + " · " + c["kind"])[:width]]
    L += wrap(c["headline"], width)
    L += wrap("Risk: " + r["text"] + " (" + {"plan": "from plan", "possible": "possible"}[r["source"]] + ")", width) if r else ["No risk found"]
    L.append("")
    rows = c["tree"]
    for i, (d, label, mark) in enumerate(rows):
        if d == 0:
            L.append(label)
            continue
        pre = ""
        for lv in range(1, d):  # a bar where an ancestor at that depth still has siblings below
            pre += "   " if not more_at(rows, i, lv) else "│  "
        pre += "├─ " if more_at(rows, i, d) else "└─ "
        L.append((pre + label + TERM_MARK.get(mark, ""))[:width])
    lines = {
        "flow": lambda: "What happens: " + " → ".join(c["flow"]["steps"]) + " → " + " | ".join(b["text"] for b in c["flow"]["branches"]),
        "layers": lambda: "How deep: " + " · ".join(ly + " " + ", ".join(l for l, _ in bx) for ly, bx in layer_rows(c) if bx),
        "radius": lambda: "How far: " + (c["reach"]["core"] + " → " + ", ".join(c["reach"]["near"]) +
                                         (" → " + ", ".join(c["reach"]["people"]) if c["reach"]["people"] else "") if c.get("reach") else "several features at once"),
        "before_after": lambda: "What moves: " + " · ".join(g + " {" + ", ".join(k) + "}" for g, k in c["before_after"]["before"]) +
                                " → " + " · ".join(g + " {" + ", ".join(k) + "}" for g, k in c["before_after"]["after"]),
    }
    extra = [s for s in c["sketches"][1:]]
    if extra:
        L.append("")
        for s in extra:
            for j, line in enumerate(wrap(lines[s](), width - 2)):
                L.append(("" if j == 0 else "  ") + line)
    acts = [(name, c["acts"][k]) for k, name in SLOTS if c["acts"].get(k)]
    if acts:
        L.append("")
        for name, txt in acts:
            for j, line in enumerate(wrap(txt, width - 18)):
                L.append(("  " + (name if j == 0 else "").ljust(16) + line).rstrip())
    return "\n".join(L)


def more_at(rows, i, depth):
    """True if a later row sits at this depth before the tree climbs above it."""
    for d, _, _ in rows[i + 1:]:
        if d < depth:
            return False
        if d == depth:
            return True
    return False


def plural(n, w):
    return str(n) + " " + w + ("" if n == 1 else "s")


def note(L, label, names, width):
    """A '· label: a, b, c' line under the board, wrapped rather than cut off."""
    for i, line in enumerate(wrap(label + ": " + ", ".join(names), width - 5)):
        L.append(("   · " if i == 0 else "     ") + line)


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


def record_plan(root, m, text, source, tiles=None, maybe=None):
    """Keep the plan text; only a real plan (ExitPlanMode, a PR body) mapped to tiles
    by Claude sets expectations. The opening prompt is context, never a plan.
    tiles = Claude is confident they change; maybe = less sure, for the user to confirm."""
    run = load_run(root)
    if source == "prompt":
        if run.get("source") in KEEP_PLANS:
            return run  # a plan-mode or PR plan beats the opening prompt
        run.update({"source": "prompt", "text": text[:4000], "expected": {}, "maybe": {}})
        run.pop("draft", None)
    else:
        if text:
            run["text"] = text[:4000]
        run["source"] = source
        run["expected"] = expected_from_tiles(m, tiles) if tiles is not None else {}
        run["maybe"] = expected_from_tiles(m, maybe) if maybe else {}
        run["mapped"] = tiles is not None
        if tiles is None:
            run.pop("draft", None)
    if tiles is None:  # a new plan or opening prompt: the old card no longer describes it
        for k in ("card", "card_shown", "card_nudged"):
            run.pop(k, None)
    save_run(root, run)
    return run


# ---------------------------------------------------------------- log (how plans held up)
def log_path(root):
    return os.path.join(root, ".readback", "log.jsonl")


def log_event(root, ev):
    """Local only (.readback/log.jsonl, gitignored): tile ids and counts, no code or plan text."""
    try:
        gi = os.path.join(root, ".readback", ".gitignore")
        old = open(gi, encoding="utf-8").read() if os.path.exists(gi) else ""
        if "log.jsonl" not in old.split():
            with open(gi, "a", encoding="utf-8") as f:
                f.write(("\n" if old and not old.endswith("\n") else "") + "log.jsonl\n")
        with open(log_path(root), "a", encoding="utf-8") as f:
            f.write(json.dumps(dict(ev, t=int(time.time()), branch=branch())) + "\n")
    except OSError:
        pass  # the log is a nice-to-have; never break a board over it


def note_plan(root, prev, run, cpl):
    """Log each mapped plan. A rerun with no --maybe right after a draft that asked questions
    is the user's answer: which maybes they kept, which couples they took, what they changed."""
    sure = run.get("expected", {}).get("features", [])
    maybe = run.get("maybe", {}).get("features", [])
    draft = prev.get("draft")
    if draft and not maybe:
        final, asked_m, asked_c, was = set(sure), set(draft["maybe"]), set(draft["couples"]), set(draft["tiles"])
        log_event(root, {"ev": "confirm", "plan": draft["id"], "kept": sorted(asked_m & final), "dropped": sorted(asked_m - final),
                         "couple_yes": sorted(asked_c & final), "couple_no": sorted(asked_c - final),
                         "sure_dropped": sorted(was - final), "added": sorted(final - was - asked_m - asked_c)})
        run["plan_id"] = draft["id"]
        run.pop("draft", None)
        return
    pid = hashlib.sha1((branch() + repr(time.time())).encode()).hexdigest()[:8]
    asked_c = [c["tile"] for c in cpl]
    log_event(root, {"ev": "plan", "plan": pid, "source": run.get("source"), "tiles": sure, "maybe": maybe, "couples": asked_c})
    run["plan_id"] = pid
    if maybe or asked_c:
        run["draft"] = {"id": pid, "tiles": sure, "maybe": maybe, "couples": asked_c}
    else:
        run.pop("draft", None)


def note_done(root, run, b):
    """The latest DONE board for the current plan; stats keeps the last one per plan."""
    if not run.get("plan_id") or not plan_expected(run):
        return
    touched = [t["id"] for t in b["tiles"] if (t["touched"] and not t["sweep"] and not t.get("brushed") and not t.get("quiet")) or t.get("via_shared")]
    log_event(root, {"ev": "done", "plan": run["plan_id"], "kind": b["kind"], "planned": plan_expected(run).get("features", []),
                     "maybe": plan_maybe(run).get("features", []), "touched": touched})


def pct(a, n):
    return "%d of %d (%d%%)" % (a, n, round(100 * a / n)) if n else "none yet"


def cmd_stats(argv):
    root = repo_root()
    evs = []
    if os.path.exists(log_path(root)):
        with open(log_path(root), encoding="utf-8") as f:
            for line in f:
                try:
                    evs.append(json.loads(line))
                except ValueError:
                    pass
    plans = {e["plan"]: e for e in evs if e.get("ev") == "plan"}
    confirms = {e["plan"]: e for e in evs if e.get("ev") == "confirm"}
    dones = {e["plan"]: e for e in evs if e.get("ev") == "done"}  # last board per plan wins
    asked = [p for p, e in plans.items() if e["maybe"] or e["couples"]]
    cs = list(confirms.values())
    n = lambda k: sum(len(c[k]) for c in cs)
    prod = [d for d in dones.values() if d.get("kind") == "product"]
    o = {"plans": len(plans), "asked": len(asked), "answered": len(cs), "unanswered": len([p for p in asked if p not in confirms]),
         "maybe_kept": n("kept"), "maybe_asked": n("kept") + n("dropped"),
         "couple_yes": n("couple_yes"), "couple_asked": n("couple_yes") + n("couple_no"),
         "sure_dropped": n("sure_dropped"), "sure_total": sum(len(plans[c["plan"]]["tiles"]) for c in cs if c["plan"] in plans),
         "added": n("added"), "corrected": sum(1 for c in cs if c["sure_dropped"] or c["added"]),
         "done": len(prod), "planned": sum(len(d["planned"]) for d in prod),
         "planned_changed": sum(len(set(d["planned"]) & set(d["touched"])) for d in prod),
         "changed": sum(len(d["touched"]) for d in prod),
         "changed_ringed": sum(len(set(d["touched"]) & (set(d["planned"]) | set(d["maybe"]))) for d in prod),
         "exact": sum(1 for d in prod if set(d["planned"]) <= set(d["touched"]) <= set(d["planned"]) | set(d["maybe"])),
         "maybe_total": sum(len(d["maybe"]) for d in prod),
         "maybe_changed": sum(len(set(d["maybe"]) & set(d["touched"])) for d in prod)}
    if args_get(argv, "--format") == "json":
        print(json.dumps(o, indent=1))
        return
    L = ["Readback stats · " + plural(o["plans"], "plan") + " · %d with questions · %d finished with a product change" % (o["asked"], o["done"]),
         "─" * 78, "Plan questions (your answers before coding)",
         "  Maybe tiles you kept           " + pct(o["maybe_kept"], o["maybe_asked"]),
         "  'Usually changes with' taken   " + pct(o["couple_yes"], o["couple_asked"]),
         "  Sure tiles you dropped         " + pct(o["sure_dropped"], o["sure_total"]),
         "  Tiles you added                " + str(o["added"]),
         "  Plans you corrected            " + pct(o["corrected"], o["answered"]) + "   gate: under 20%"]
    if o["unanswered"]:
        L.append("  Questions never answered       %d plan(s): Claude carried on without asking" % o["unanswered"])
    L += ["", "Plan vs done (last board per plan)",
          "  Planned tiles that changed     " + pct(o["planned_changed"], o["planned"]) + "   gate: 90%+",
          "  Changed tiles that were ringed " + pct(o["changed_ringed"], o["changed"]),
          "  Maybe tiles that changed       " + pct(o["maybe_changed"], o["maybe_total"]),
          "  Boards that matched the plan   " + pct(o["exact"], o["done"]),
          "", "   log: .readback/log.jsonl (local, gitignored)"]
    print("\n".join(L))


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


def plan_maybe(run):
    run = run or {}
    return run.get("maybe", {}) if run.get("source") in REAL_PLANS else {}


def boards_for(m, files, run, level, lens, phase="DONE", kind=None):
    return resolve(m, files, level, lens, plan_expected(run).get(level), phase, kind, (run or {}).get("text"), plan_maybe(run).get(level))


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
    if level not in LEVELS or lens not in LENSES:
        print("Readback: levels are " + ", ".join(LEVELS) + "; lenses are " + ", ".join(LENSES) + ".")
        return
    if rng and not rng.startswith("#") and any(s and not git(["rev-parse", "--verify", "--quiet", s + "^{commit}"]).strip()
                                               for s in re.split(r"\.\.\.?", rng)):
        print("Readback: no commit or range called '" + rng + "' (levels: " + ", ".join(LEVELS) + "; lenses: " + ", ".join(LENSES) + ").")
        return
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
        exp, mb = plan_expected(run), plan_maybe(run)
        boards = [b] + [resolve(m, files, lv, ln, exp.get(lv), kind=kind, plan_text=run.get("text"), maybe=mb.get(lv)) for lv, ln in (("parts", "risk"), ("features", "money")) if (lv, ln) != (level, lens)]
        atl = atlas(m, files, exp, kind, run.get("text"), mb)
        write_out(render_md(boards, atl) if fmt == "md" else render_html(boards, atl), args_get(argv, "--out"))
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
        f.write("runs/\nlog.jsonl\n")
    print("Drafted .readback/map.json: %s, %s." % (plural(len(m["levels"]["features"]["tiles"]), "feature"), plural(len(m["levels"]["parts"]["tiles"]), "part")))
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


UI_RE = (r"(?i)(^|/)(widgets?|components?|ui|views?|layouts?|partials?|shell)/"
         r"|(^|[/_-])(app_?bar|header|footer|nav|navbar|sidebar|drawer|layout|scaffold|toolbar|tab_?bar|menu)[^/]*\.[a-z]+$")
IMPORT_RE = r"(?m)^\s*(import|export|from|require|use|#include|part)\b.*$|\brequire\(.*\)|\bimport\(.*\)"


def widely_used(root, m, files, n=8, min_users=2):
    """UI files one feature owns through a glob that code in 2+ other features imports: an app
    bar every screen draws, a shared card. Read from import lines that name the file's stem."""
    feats = m["levels"]["features"]["tiles"]
    stems = {}
    for p in files:
        stems.setdefault(os.path.splitext(os.path.basename(p))[0].lower(), []).append(p)
    who = {}
    for p in files:
        t = tile_of(m, "features", p)
        if not t or not re.search(CODE_RE, p) or re.search(TOOLING_RE, p):
            continue
        try:
            with open(os.path.join(root, p), encoding="utf-8", errors="replace") as f:
                src = f.read(400000)
        except OSError:
            continue
        for mm in re.finditer(IMPORT_RE, src):
            for tok in set(re.findall(r"[A-Za-z0-9_\-]+", mm.group(0).lower())):
                who.setdefault(tok, set()).add(t["id"])
    out = []
    for stem, ps in stems.items():
        if len(ps) != 1 or len(stem) < 5:
            continue
        p = ps[0]
        if not re.search(CODE_RE, p) or not re.search(UI_RE, p) or re.search(TOOLING_RE, p) or shared_rule(m, p)[0]:
            continue
        t = tile_of(m, "features", p)
        if not t or p in t["owns"]:
            continue  # unowned files are listed above; an exact path is an owner's answer
        users = who.get(stem, set()) - {t["id"]}
        if len(users) >= min_users:
            out.append((len(users), p, t, [f for f in feats if f["id"] in users]))
    return sorted(out, key=lambda x: (-x[0], x[1]))[:n]


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
    wide = widely_used(root, m, files)
    if wide:
        msg.append("")
        msg.append("Owned by one feature, drawn by others (a shared widget?). Keep the owner, or mark it shared?")
        for _, p, t, users in wide:
            msg.append("  %s  owner %s; used by %s" % (p, t["id"], ", ".join(u["id"] for u in users)))
            msg.append("    shared: rb.py own %s shared --by %s" % (p, ",".join([t["id"]] + [u["id"] for u in users])))
        msg.append("Shared means a planned feature on the --by list counts as changed when the file changes (--by all = every feature).")
    out = "\n".join(msg)
    if not quiet:
        print(out)
    return out


def cmd_own(argv):
    """Record an owner's answer from /readback check: a file joins a feature, or stays shared."""
    root = repo_root()
    m = load_map(root)
    if not m or len(argv) < 2:
        print("usage: rb.py own <path> <feature-id|shared> [--by feature-id,...|all]")
        return
    path, tid = argv[0].replace("\\", "/"), argv[1]
    feats = m["levels"]["features"]["tiles"]
    find = lambda x: next((t for t in feats if t["id"] == x or t["name"].lower() == x.lower()), None)
    if tid == "shared":
        by = args_get(argv, "--by")
        if by is not None:
            users = ["*"] if by.strip().lower() in ("all", "*") else [find(x.strip()) for x in by.split(",") if x.strip()]
            if None in users:
                print("No feature in '%s'. Features: %s" % (by, ", ".join(x["id"] for x in feats)))
                return
            m.setdefault("shared_by", {})[path] = users if users == ["*"] else [u["id"] for u in users]
            if path in m.get("shared", []):
                m["shared"].remove(path)
        else:
            m.setdefault("shared", [])
            if path not in m["shared"]:
                m["shared"].append(path)
        for t in feats:  # a shared file is no single feature's
            if path in t["owns"]:
                t["owns"].remove(path)
    else:
        t = find(tid)
        if not t:
            print("No feature '%s'. Features: %s" % (tid, ", ".join(x["id"] for x in feats)))
            return
        if path not in t["owns"]:
            t["owns"].append(path)
        if path in m.get("shared", []):
            m["shared"].remove(path)
        (m.get("shared_by") or {}).pop(path, None)
        if m.get("shared_by") == {}:
            del m["shared_by"]
    with open(map_path(root), "w", encoding="utf-8") as f:
        json.dump(m, f, indent=1)
    print("%s -> %s" % (path, tid))


def write_out(out, dest):
    if dest:
        with open(dest, "w", encoding="utf-8") as f:
            f.write(out)
        print(dest)
    else:
        print(out)


def plan_boards(m, run):
    """The PLAN picture: rings from the mapped plan, before any code exists."""
    exp, mb = run.get("expected", {}), run.get("maybe", {})
    boards = [resolve(m, {}, lv, "change", exp.get(lv), "PLAN", maybe=mb.get(lv)) for lv in LEVELS]
    boards[0]["couples"] = [c for c in couples(m, exp.get("features", []), mb.get("features", []))
                            if c["tile"] not in run.get("answered", [])]
    return boards


def cmd_plan(argv):
    """rb.py plan --tiles a,b [--maybe c,d] [--source plan|pr] [--card FILE|-] [--format term|widget|html|json] [--out FILE]
                                                Claude's mapping of the plan; prints the 'About to change' board
       rb.py plan --show [--format ...]         the stored "About to change" board again
       rb.py plan TEXT|-                        store the text; Claude still has to map it"""
    root = repo_root()
    os.chdir(root)
    m = load_map(root)
    if not m:
        print("No map yet. Run /readback init first.")
        return
    src = args_get(argv, "--source", "plan")
    if src not in REAL_PLANS:
        src = "plan"
    tl = args_get(argv, "--tiles")
    if tl is not None or "--show" in argv:
        split = lambda s: [x for x in (s or "").split(",") if x.strip()]
        known = {k.lower() for lv in LEVELS for t in m["levels"][lv]["tiles"] for k in (t["id"], t["name"])}
        unknown = [x.strip() for x in split(tl) + split(args_get(argv, "--maybe")) if x.strip().lower() not in known]
        if unknown:
            sys.stderr.write("Readback: not on the map, ignored: " + ", ".join(unknown) + ". Use tile ids from the map.\n")
        prev = load_run(root)
        run = record_plan(root, m, "", src, split(tl), split(args_get(argv, "--maybe"))) if tl is not None else prev
        if tl is not None:
            d = prev.get("draft")
            if d and not run.get("maybe", {}).get("features"):
                run["answered"] = sorted(set(d["maybe"]) | set(d["couples"]))  # the user's answer to the draft
            else:
                run.pop("answered", None)
        boards = plan_boards(m, run)
        cf = args_get(argv, "--card")
        if cf is not None:
            try:
                raw = json.loads(sys.stdin.read() if cf == "-" else open(cf, encoding="utf-8").read())
            except (OSError, ValueError) as e:
                raw = None
                sys.stderr.write("Readback: could not read the card (%s); showing the board without it.\n" % e)
            card, warn = validate_card(m, raw) if raw is not None else (None, [])
            if warn:
                sys.stderr.write("Readback card: fix these and run again if they matter:\n" + "".join("  - " + w + "\n" for w in warn))
            if card:
                run["card"] = card
        elif tl is not None and prev.get("card"):
            run["card"] = prev["card"]  # a rerun with the final tile list keeps the card
        if tl is not None:
            note_plan(root, prev, run, boards[0].get("couples", []))
        if tl is not None or cf is not None:
            save_run(root, run)
        fmt = args_get(argv, "--format", "term")
        if run.get("card"):
            c = card_model(m, run)
            if fmt == "json":
                print(json.dumps(c, indent=1, ensure_ascii=False))
            elif fmt == "html":
                write_out(render_card_html(c), args_get(argv, "--out"))
            else:
                print(render_card_widget(c) if fmt == "widget" else render_card_term(c))
                if fmt != "widget":
                    sys.stderr.write("Readback: if you have a visual tool (show_widget), run " + rb_cmd() +
                                     " plan --show --format widget and pass its output to it unchanged.\n")
                run["card_shown"] = card_digest(run["card"])
                save_run(root, run)
            return
        if fmt == "json":
            print(json.dumps(boards[0], indent=1))
        elif fmt == "html":
            write_out(render_html(boards), args_get(argv, "--out"))
        else:
            print(render_term(boards[0]))
        return
    text = sys.stdin.read() if argv == ["-"] else " ".join(argv)
    record_plan(root, m, text, src)
    print(map_request(m))


def rb_cmd():
    return "\"" + sys.executable.replace(os.sep, "/") + "\" \"" + os.path.abspath(__file__).replace(os.sep, "/") + "\""


CARD_SCHEMA = ('{"headline": "the change in plain words, max 90 chars, no risk or hedge word first", '
               '"risk": {"text": "one risk naming its subject", "source": "plan|possible", "tile": "id"} or null, '
               '"items": [{"tile": "id" or "new_tile": "Name", "area": "for new_tile", "what": "<=32 chars", '
               '"mark": "change|new|removed|maybe|risk", "layer": "screen|logic|data|config|outside"}], '
               '"outside": [{"name": "Bank", "what": "<=32 chars"}], '
               '"flow": {"steps": ["Pay", "Bank"], "branches": [{"text": "OK: done", "mark": "change"}, {"text": "Timeout: retry", "mark": "risk"}]}, '
               '"reach": {"core": "Retry", "near": ["Orders"], "people": ["Buyers"]}, '
               '"before_after": {"before": [["Checkout", ["Cart list"]]], "after": [["Cart", ["Cart list"]]]}, '
               '"acts": {"start": "", "merge": "who checks what", "release": ""}}')


def map_request(m, source="plan"):
    what = ("map this plan to the product map before editing. Pick only the tiles the plan says it will change "
            "(features and parts; leave out anything it does not ask for)." if source == "plan" else
            "before your first edit, say which tiles the change you are about to make will touch "
            "(features and parts; only what the user asked for).")
    return ("Readback: " + what + " Put tiles you are sure of in --tiles and "
            "tiles you are only partly sure of in --maybe:\n" + tile_list(m) +
            "\nAlso write the 'About to change' card as JSON (leave out flow, reach, before_after or acts when they don't apply; "
            "risk source is 'plan' only when the plan itself says it): " + CARD_SCHEMA +
            "\nThen run, with the card on stdin (a heredoc):  " + rb_cmd() + " plan --tiles id1,id2 --maybe id3" +
            (" --source intent" if source == "intent" else "") + " --card -  "
            "If you have a visual tool (show_widget), then run " + rb_cmd() + " plan --show --format widget and pass its output to it "
            "unchanged; otherwise show the user the card it printed, unchanged (every line), in a code block. Never redraw or trim it. "
            "If it has maybe tiles or a 'usually changes with' question, ask the user once which to include, then run plan --tiles again "
            "with the final list (no --maybe" + (", same --source" if source == "intent" else "") + ") before editing. Otherwise carry on.")


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


def hook_edit():
    """Before Claude's first edit in a session with no mapped plan (no plan mode, or a headless
    run), stop that one edit and ask Claude to say which tiles it is about to touch. Once per session."""
    data = hook_input()
    root = repo_root()
    m = load_map(root)
    if not m:
        return
    path = (data.get("tool_input") or {}).get("file_path") or (data.get("tool_input") or {}).get("notebook_path") or ""
    try:
        rel = os.path.relpath(os.path.abspath(path), root).replace(os.sep, "/") if path else ""
    except ValueError:
        return  # another drive
    if not rel or rel.startswith("../") or rel.startswith(".readback/") or os.path.isabs(rel):
        return  # plan files, scratch files and Readback's own state are not product edits
    run = load_run(root)
    sid = data.get("session_id")
    if (run.get("mapped") and run.get("source") in REAL_PLANS) or run.get("intent_asked") == sid:
        return
    run["intent_asked"] = sid
    save_run(root, run)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": map_request(m, "intent") +
                                             " Then make this edit again."}}))


def hook_done():
    data = hook_input()
    if data.get("stop_hook_active"):
        return
    root = repo_root()
    os.chdir(root)
    m = load_map(root)
    if not m:
        return
    run = load_run(root)
    card = run.get("card")
    if card and card_digest(card) not in (run.get("card_shown"), run.get("card_nudged")):
        # Hooks can't call the visual tool; stopping once makes Claude show the card before it finishes
        run["card_nudged"] = card_digest(card)
        save_run(root, run)
        print(json.dumps({"decision": "block", "reason": "Readback: the 'About to change' card is ready but was not shown. If you have a "
                          "visual tool (show_widget), run " + rb_cmd() + " plan --show --format widget and pass its output to it unchanged; "
                          "otherwise run " + rb_cmd() + " plan --show and paste it unchanged in a code block. Then finish."}))
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
    note_done(root, run, b)
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
    elif c == "stats":
        cmd_stats(rest)
    elif c == "hook-plan":
        hook_plan()
    elif c == "hook-prompt":
        hook_prompt()
    elif c == "hook-edit":
        hook_edit()
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
