#!/usr/bin/env python3
"""Readback: show a code change as a product picture.

One model, many surfaces. Every command reads the repo's map
(.readback/map.json) plus a git diff, resolves boards, and renders them.

  rb.py init [--force]              draft .readback/map.json from the repo tree
  rb.py show [RANGE] [--level L] [--lens X] [--as ROLE] [--format term|md|slack|html|json|trailer|line]
                                    [--expected a,b] [--plan TEXT|-]   (PR body with a Readback-Plan: line)
  rb.py plan --tiles a,b [--maybe c] [--source plan|pr] [--card FILE|-] [--for L] [--format term|widget|html|json] [--out F]
                                    Claude's mapping of a real plan to tiles (and its card JSON); draws the
                                    "About to change" card, or the board when there is no card
  rb.py plan --show [--format ...]  the stored card or board again (widget = fragment for a visual tool)
  rb.py show --card [RANGE] [--for dev|senior|lead|manager] [--format term|widget|html|md|json] [--link-base URL]
                                    the "What changed" card: the plan card checked against the diff
  rb.py deck add|prep|next|decide|summary|plan   many ideas, one card each (rb.py deck for help)
  rb.py gaps [--days 30] [--unsure] [--label ID KIND]   where people had to ask, from this repo's sessions
  rb.py brief [--show [--tiles a,b]]                    write / show .readback/context.md
  rb.py stats [--format json]       how plans held up: answers to the plan questions, plan vs done
  rb.py check                       validate the map; list the largest shared files to assign
  rb.py own PATH FEATURE|shared [--by a,b|all]   record who owns a shared file (and who uses it)
  rb.py hook-plan | hook-prompt | hook-edit | hook-done | hook-start   (called by plugin hooks; JSON on stdin)

--for picks the reader: dev (everything), senior, lead, manager (headline, risk, one action). A risk or
owner shown to one reader is carried up to every reader above.

No dependencies beyond Python 3.8 and git.
"""
import fnmatch, hashlib, html, json, os, re, subprocess, sys, time

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
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
                "engineer": {"home": ["parts", "change"], "acts_on": ["risk", "deps", "tests"], "for": "senior"},
                "pm": {"home": ["features", "change"], "acts_on": ["unplanned", "metric"], "for": "lead"},
                "finance": {"home": ["features", "money"], "acts_on": ["money"], "for": "manager"},
                "support": {"home": ["features", "change"], "acts_on": [], "for": "lead"},
                "design": {"home": ["features", "change"], "acts_on": [], "for": "lead"}},
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
                out.append({"who": route(m, "risk", b["owner"]), "do": "Plan a review of " + b["name"] + " (high risk)", "why": "risk", "tile": b["id"]})
            if b["money"]:
                out.append({"who": route(m, "money"), "do": b["name"] + " touches money, expect a check", "why": "money", "tile": b["id"]})
            continue
        if b["risk"] == "high" and b.get("risk_lines", b["lines"]) >= 10:
            out.append({"who": route(m, "risk", b["owner"]), "do": "Review " + b["name"] + " before merge", "why": "risk", "tile": b["id"]})
        if not product:
            continue  # no product change: no money or plan questions to ask anyone
        if b["money"]:
            out.append({"who": route(m, "money"), "do": "Check money impact in " + b["name"], "why": "money", "tile": b["id"]})
        if b["unplanned"]:
            out.append({"who": route(m, "unplanned"), "do": "Confirm the unplanned change to " + b["name"], "why": "unplanned", "tile": b["id"]})
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
            out["reach"] = {"core": clip(rc["core"], 12, "reach core", warn), "near": near, "people": people}
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

    # Tiles the plan says must stay as they are; the "What changed" card flags them if they move
    keep = []
    for k in card.get("keep") or []:
        t = by.get(str(k).lower())
        if not t:
            warn.append('keep tile "%s" is not on the map; ignored.' % k)
        elif t["id"] in {i["tile"] for i in items}:
            warn.append('keep tile "%s" is also an item; a tile can\'t both change and stay; ignored.' % k)
        elif t["id"] not in keep:
            keep.append(t["id"])
    if keep:
        out["keep"] = keep

    acts_in = card.get("acts") if isinstance(card.get("acts"), dict) else {}
    out["acts"] = {k: clip(acts_in.get(k), 80, "acts." + k, warn) for k, _ in SLOTS if acts_in.get(k)}
    # A guessed risk never stands as a claim: it becomes a question for the author before work starts
    if out["risk"] and out["risk"]["source"] == "possible" and not out["acts"].get("start"):
        out["acts"]["start"] = "Ask the author: " + out["risk"]["text"].rstrip(".…") + "?"  # whole risk, never cut
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


def card_model(m, run, level="dev"):
    card = run["card"]
    keys = card_tiles(run)
    sev, sk = pick_sketches(m, card, keys)
    kind = KIND_LABEL[sk[1] if len(sk) > 1 else "tree"]
    base = dict(card, phase="PLAN", product=m.get("product", "") or "Product", keys=keys, severity=sev, sketches=sk,
                kind=kind, tree=tree_nodes(m, run), meta=[sev, kind],
                slots=[(name, [card["acts"][k]]) for k, name in SLOTS if card["acts"].get(k)])
    return leveled(base, level)


# ---------------------------------------------------------------- reader levels
# One card, squeezed for who reads it. Anything risky or owned that a lower level shows is carried up
# to every level above it, so a manager never sees less danger than the developer did.
READERS = ("dev", "senior", "lead", "manager")
READER_FOR = {"engineer": "senior", "pm": "lead", "support": "lead", "design": "lead", "finance": "manager"}
TREE_BUDGET = {"dev": 12, "senior": 8, "lead": 5, "manager": 0}
LOUD = ("risk", "group-risk", "unplanned", "keep", "missed", "collide")
CARRY_SAYS = {"risk": "%s carries the risk", "group-risk": "%s carries the risk", "unplanned": "%s changed outside the plan",
              "keep": "%s changed though the plan said leave it alone", "missed": "%s was planned but not touched",
              "collide": "%s is changed by more than one idea"}
COUNTS_RE = r" · \d+ files?, \d+ lines?$"


def reader_level(m, argv):
    lv = args_get(argv, "--for")
    if lv in READERS:
        return lv
    if lv:
        sys.stderr.write("Readback: --for is one of " + "|".join(READERS) + "; used dev.\n")
        return "dev"
    role = args_get(argv, "--as")
    if not role:
        return "dev"
    return ((m.get("roles") or {}).get(role) or {}).get("for") or READER_FOR.get(role, "dev")


def collapse_tree(rows):
    """Lead and manager: one row per tile. A tile with several items becomes 'Name · 3 changes'."""
    out, i = [], 0
    while i < len(rows):
        d, label, mark = rows[i]
        if mark == "note":
            i += 1
            continue
        if mark in ("group", "group-risk"):
            j = i + 1
            while j < len(rows) and rows[j][0] > d:
                j += 1
            kids = rows[i + 1:j]
            loud = mark == "group-risk" or any(k[2] in LOUD for k in kids)
            out.append((d, label + " · " + plural(len(kids), "change"), "risk" if loud else "change"))
            i = j
            continue
        out.append(rows[i])
        i += 1
    return out


def squeeze_tree(rows, budget):
    """Keep the root, every loud row and its parents, then rows in order; the rest become '+N more'."""
    if len(rows) <= budget:
        return rows

    def parents(i):
        out, d = [], rows[i][0]
        for j in range(i - 1, -1, -1):
            if rows[j][0] < d:
                out.append(j)
                d = rows[j][0]
        return out
    keep = {0}
    for i, r in enumerate(rows):
        if r[2] in LOUD:
            keep.update([i] + parents(i))
    for i in range(len(rows)):
        if len(keep) >= budget - 1:
            break
        keep.update([i] + parents(i))
    dropped = len([r for i, r in enumerate(rows) if i not in keep and r[2] not in ("area", "note")])
    out = [rows[i] for i in sorted(keep)]
    return out + ([(1, "+%d more" % dropped, "more")] if dropped else [])


def for_level(c, level):
    c = dict(c, level=level, carried=list(c.get("carried", [])))
    tree = c["tree"] if level == "dev" else [(d, re.sub(COUNTS_RE, "", l), mk) for d, l, mk in c["tree"]]
    if level in ("lead", "manager"):
        tree = collapse_tree(tree)
    c["tree"] = squeeze_tree(tree, TREE_BUDGET[level]) if level != "manager" else []
    rest = [s for s in c["sketches"] if s != "tree"]
    if level == "senior":
        rest.sort(key=lambda s: s not in ("layers", "radius"))  # what it touches underneath comes first
    has_tree = "tree" in c["sketches"]
    c["sketches"] = (["tree"] if has_tree else []) + rest if level in ("dev", "senior") else (["tree"] if has_tree and level == "lead" else [])
    if level in ("lead", "manager"):
        c["evidence"], c["lines"] = [], c["lines"] if c.get("phase") == "DECK" else []
    if level == "lead":
        c["slots"], c["flags"] = [(n, t[:2]) for n, t in c["slots"]], c["flags"][:3]
    if level == "manager":
        first = next(((n, t[:1]) for n, t in c["slots"] if t), None)
        c["slots"], c["chips"] = [first] if first else [], []
        c["flags"] = [f for f in c["flags"] if f.get("loud")][:1]
    return c


def card_facts(c):
    """What a reader must not lose going up: the risk, loud tree rows, named owners, loud flags."""
    f = [c["risk"]["text"]] if c.get("risk") else []
    f += [(label.split(" · ")[0], mark) for d, label, mark in c["tree"] if mark in LOUD]
    for name, texts in c["slots"]:
        for t in texts:
            mm = re.match(r"(@?[A-Za-z][\w&/.-]*(?: [\w&/.-]+){0,2}):\s", t)
            if mm and not t.startswith("Ask the author"):
                f.append((mm.group(1), name))
    return f + [fl["text"] for fl in c["flags"] if fl.get("loud")]


def carry_text(f):
    if isinstance(f, str):
        return f
    name, what = f
    return CARRY_SAYS[what] % name if what in CARRY_SAYS else "%s acts %s" % (name, what.lower())


def carry_up(chain):
    seen = []
    for i, c in enumerate(chain):
        if i:
            text = " ".join(render_card_term(c, width=400).split())
            for f in seen:
                if (f if isinstance(f, str) else f[0]) not in text:
                    c["carried"].append(carry_text(f))
                    text += " " + carry_text(f)
        seen += [f for f in card_facts(c) if f not in seen]
    return chain


def leveled(base, level):
    base = dict({"flags": [], "lines": [], "chips": [], "evidence": [], "relation": "", "carried": [], "risk": None}, **base)
    level = level if level in READERS else "dev"
    return carry_up([for_level(base, lv) for lv in READERS[:READERS.index(level) + 1]])[-1]


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
# The "What changed" and deck marks reuse the same meanings: amber = check this, dashed = didn't happen
MARK_STYLE.update(done=MARK_STYLE["change"], unplanned=MARK_STYLE["risk"], keep=MARK_STYLE["risk"], collide=MARK_STYLE["risk"],
                  missed=MARK_STYLE["maybe"], brushed=MARK_STYLE["maybe"], via=("none", LINE2, SEC, "1 3", False))
WARN_TX = MARK_STYLE["risk"][2]
TEXT_ROWS = ("root", "area", "group", "group-risk", "note", "more")


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
        if mark in ("note", "more"):
            out.append(sv_text(x, y + 15, label, MUT, 12))
        elif mark in TEXT_ROWS:
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
        names = [g for d, g, mk in c["tree"] if mk not in TEXT_ROWS + ("outside",)]
        names = [n.split(" · ")[0] for n in names]
        core = next((n.split(" · ")[0] for d, n, mk in c["tree"] if mk in ("risk", "group-risk")), names[0] if names else "Change")
        rc = {"core": core[:12], "near": [n[:14] for n in names if n != core][:3], "people": []}
    # Near labels sit in the band between the core and the ring, longest at the top where the band is widest;
    # people sit outside the dashed ring
    out = ['<circle cx="170" cy="118" r="104" style="fill:none;stroke:%s" stroke-dasharray="3 4"/>' % LINE if rc["people"] else "",
           '<circle cx="170" cy="118" r="78" style="fill:none;stroke:%s"/>' % LINE2,
           '<circle cx="170" cy="118" r="%d" style="fill:%s;stroke:%s"/>' % ((max(26, len(rc["core"]) * 3 + 8),) + MARK_STYLE["risk"][:2]),
           sv_text(170, 122, rc["core"], WARN_TX, 11, anchor="middle")]
    for (x, y), s in zip([(170, 70), (124, 152), (216, 152)], sorted(rc["near"], key=len, reverse=True)):
        out.append(sv_text(x, y, s, INK, 11, anchor="middle"))
    for (x, y), s in zip([(170, 10), (292, 220), (48, 220)], rc["people"]):
        out.append(sv_text(x, y, s, SEC, 11, anchor="middle"))
    return sv(340, 228, "".join(out), "How far it reaches")


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


PHASE_LABEL.update(IDEA="Idea", DECK="Deck")
RISK_TAG = {"plan": "from plan", "possible": "possible"}


def card_meta(c):
    return "Readback · " + c["product"] + " · " + (c.get("phase_label") or PHASE_LABEL.get(c["phase"], "")) + "".join(" · " + x for x in c.get("meta", []) if x)


def range_text(hs, n=3):
    """[(start, len)] from the diff as 'L12–30, L41'; a pure deletion is 'deleted near L7'."""
    out = [("deleted near L%d" % s if not k else ("L%d" % s if k == 1 else "L%d–%d" % (s, s + k - 1))) for s, k in hs[:n]]
    return ", ".join(out) + (" +%d more" % (len(hs) - n) if len(hs) > n else "")


def render_card_widget(c):
    """A fragment for the host's visual tool: no page wrapper, host colours, transparent background.
    Only the deck card carries buttons (they send the choice back to chat); no card has a script."""
    esc, r = html.escape, c.get("risk")
    p = lambda s, size=13, color=SEC, gap=6, extra="": '<p style="margin:0 0 %dpx;font-size:%dpx;color:%s%s">%s</p>' % (gap, size, color, extra, s)
    out = [p(esc(card_meta(c)), 12, SEC, 4)]
    if c.get("relation"):
        out.append(p(esc(c["relation"]), 12, MUT, 4))
    out.append(p(esc(c["headline"]), 17, INK, 4, ";font-weight:500;line-height:1.4"))
    if c["phase"] in ("PLAN", "IDEA"):
        out.append(p('Risk: %s <span style="font-size:11px;padding:0 7px;border-radius:6px;background:%s">%s</span>'
                     % (esc(r["text"]), MARK_STYLE["risk"][0], RISK_TAG[r["source"]]), 14, WARN_TX, 10) if r else p("No risk found", 13, MUT, 10))
    if c["chips"]:
        out.append(p("".join('<span style="display:inline-block;padding:1px 8px;margin:0 6px 0 0;border-radius:6px;border:0.5px dashed %s">%s</span>'
                             % (MARK_STYLE["outside"][1], esc(x)) for x in c["chips"]), 12, MARK_STYLE["outside"][2], 10))
    if c["sketches"]:
        out.append('<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px;margin:0 0 10px">%s</div>'
                   % "".join('<div style="min-width:0">%s%s</div>' % (p(SKETCH_LABEL[s], 11, MUT, 4), SKETCHES[s](c)) for s in c["sketches"]))
    out += [p(esc(l)) for l in c["lines"]]
    out += [p("⚠ " + esc(f["text"]) + ' <span style="color:%s">· %s</span>' % (SEC, esc(f["who"])), 13, WARN_TX if f.get("loud") else INK, 4)
            for f in c["flags"]]
    for name, fs in c["evidence"]:
        out.append('<details style="margin:0 0 4px;font-size:12px;color:%s"><summary style="cursor:pointer">%s · %s</summary>%s</details>'
                   % (SEC, esc(name), plural(len(fs), "file"), "".join(p(esc(path) + " " + esc(range_text(hs)), 12, SEC, 2, ";padding-left:14px;font-family:" + V("font-mono", "monospace"))
                                                               for path, hs in fs)))
    out += ['<div style="display:grid;grid-template-columns:110px 1fr;gap:8px;font-size:13px;padding:5px 0;border-top:0.5px solid %s">'
            '<span style="color:%s">%s</span><span>%s</span></div>' % (LINE, SEC, name, "<br>".join(esc(t) for t in texts)) for name, texts in c["slots"]]
    if c["carried"]:
        out.append(p("Still true: " + esc(" · ".join(c["carried"])), 12, WARN_TX, 4, ";padding-top:6px"))
    if c.get("buttons"):
        out.append('<div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:12px">%s</div>' % "".join(
            '<button onclick="sendPrompt(%s)" style="font:inherit;font-size:13px;padding:6px 14px;border-radius:8px;border:0.5px solid %s;background:transparent;color:%s;cursor:pointer">%s</button>'
            % (esc(json.dumps(prompt, ensure_ascii=False)), LINE2, INK, esc(label)) for label, prompt in c["buttons"]))
    return ('<div class="readback-card" style="position:relative;font-family:%s;color:%s;border:0.5px solid %s;border-radius:12px;padding:16px 18px">'
            '<h2 style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);margin:0">%s: %s</h2>%s</div>'
            % (V("font-sans", "system-ui, sans-serif"), INK, LINE, PHASE_LABEL.get(c["phase"], ""), esc(c["headline"]), "".join(out)))


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
            "<title>Readback " + {"DONE": "changes", "DECK": "deck"}.get(c["phase"], "plan") + "</title><style>" + css +
            "</style></head><body><main>" + render_card_widget(c) + "</main></body></html>")


TERM_MARK = {"new": " (new)", "removed": " (removed)", "risk": "  ⚠", "group-risk": "  ⚠", "done": " ✓",
             "unplanned": "  ⚠", "keep": "  ⚠", "collide": "  ⚠"}


def term_tree(rows, width=80):
    L = []
    for i, (d, label, mark) in enumerate(rows):
        if d == 0:
            L.append(label)
            continue
        pre = ""
        for lv in range(1, d):  # a bar where an ancestor at that depth still has siblings below
            pre += "   " if not more_at(rows, i, lv) else "│  "
        pre += "├─ " if more_at(rows, i, d) else "└─ "
        L.append((pre + label + TERM_MARK.get(mark, ""))[:width])
    return L


def render_card_term(c, width=80):
    r = c.get("risk")
    L = [card_meta(c)[:width]]
    if c.get("relation"):
        L += wrap(c["relation"], width)
    L += wrap(c["headline"], width)
    if c["phase"] in ("PLAN", "IDEA"):
        L += wrap("Risk: " + r["text"] + " (" + RISK_TAG[r["source"]] + ")", width) if r else ["No risk found"]
    if c["chips"]:
        L += wrap(" · ".join(c["chips"]), width)
    if "tree" in c["sketches"]:
        L.append("")
        L += term_tree(c["tree"], width)
    lines = {
        "flow": lambda: "What happens: " + " → ".join(c["flow"]["steps"]) + " → " + " | ".join(b["text"] for b in c["flow"]["branches"]),
        "layers": lambda: "How deep: " + " · ".join(ly + " " + ", ".join(l for l, _ in bx) for ly, bx in layer_rows(c) if bx),
        "radius": lambda: "How far: " + (c["reach"]["core"] + " → " + ", ".join(c["reach"]["near"]) +
                                         (" → " + ", ".join(c["reach"]["people"]) if c["reach"]["people"] else "") if c.get("reach") else "several features at once"),
        "before_after": lambda: "What moves: " + " · ".join(g + " {" + ", ".join(k) + "}" for g, k in c["before_after"]["before"]) +
                                " → " + " · ".join(g + " {" + ", ".join(k) + "}" for g, k in c["before_after"]["after"]),
    }
    extra = [s for s in c["sketches"] if s != "tree"]
    if extra:
        L.append("")
        for s in extra:
            for j, line in enumerate(wrap(lines[s](), width - 2)):
                L.append(("" if j == 0 else "  ") + line)
    hang = lambda s, lead="", indent="  ": [(lead if j == 0 else indent) + line for j, line in enumerate(wrap(s, width - len(indent)))]
    if c["lines"] or c["flags"]:
        L.append("")
        for s in c["lines"]:
            L += hang(s)
        for f in c["flags"]:
            L += hang(f["text"] + " (" + f["who"] + ")", "⚠ ")
    if c["evidence"]:
        L.append("")
        for name, fs in c["evidence"]:
            L += hang(name + ": " + "; ".join(path + " " + range_text(hs) for path, hs in fs), "  ", "    ")
    if c["slots"]:
        L.append("")
        for name, texts in c["slots"]:
            for k, txt in enumerate(texts):
                for j, line in enumerate(wrap(txt, width - 18)):
                    L.append(("  " + (name if j == 0 and k == 0 else "").ljust(16) + line).rstrip())
    if c["carried"]:
        L.append("")
        L += hang("Still true: " + " · ".join(c["carried"]))
    if c.get("buttons"):
        L.append("")
        L += hang("Choose: " + " · ".join(label for label, _ in c["buttons"]))
    return "\n".join(L)


def render_card_md(c, link_base=None):
    """For a PR comment: the term tree in a code block, then checks, evidence with line links, and slots."""
    L = ["### " + card_meta(c), "", "**" + c["headline"] + "**", ""]
    if c.get("relation"):
        L += ["<sub>" + html.escape(c["relation"]) + "</sub>", ""]
    if c["chips"]:
        L += ["> " + " · ".join(c["chips"]), ""]
    if c["tree"]:
        L += ["```text"] + term_tree(c["tree"], 100) + ["```", ""]
    L += [l + "  " for l in c["lines"]]
    if c["flags"]:
        L += ["", "**Check**"] + ["- ⚠ " + f["text"] + " · **" + f["who"] + "**" for f in c["flags"]]
    if c["evidence"]:
        def link(path, hs):
            label = "`" + path + "` " + range_text(hs)
            if not link_base:
                return label
            s, k = (hs or [(1, 0)])[0]
            return "[" + label + "](" + link_base.rstrip("/") + "/" + path + ("#L%d-L%d" % (s, s + max(k, 1) - 1) if hs else "") + ")"
        L += ["", "<details><summary>Evidence</summary>", ""]
        L += ["- **" + name + "**: " + " · ".join(link(path, hs) for path, hs in fs) for name, fs in c["evidence"]]
        L += ["", "</details>"]
    for name, texts in c["slots"]:
        L += ["", "**" + name + "**"] + ["- [ ] " + t for t in texts]
    if c["carried"]:
        L += ["", "<sub>Still true: " + html.escape(" · ".join(c["carried"])) + "</sub>"]
    return "\n".join(L)


# ---------------------------------------------------------------- "What changed" card
# The same card after the work: each planned tile checked against the diff, with the lines that prove it.
TEST_RE = r"(?i)((^|/)(tests?|spec|__tests__|integration_test)/|_test\.|\.test\.|\.spec\.|(^|/)test_[^/]+$)"
SKIP_MARK_RE = r"(\.skip\(|\bxit\(|\bxdescribe\(|\.only\(|@pytest\.mark\.skip|\bt\.Skip\(|@unittest\.skip|@Disabled\b|@Ignore\b)"
API_RE = r"(?i)(openapi|swagger|\.proto$|\.graphql$|\.gql$|(^|/)api/)"
SCHEMA_RE = r"(?i)((^|/)migrations?/|schema|\.sql$|\.prisma$|(^|/)models?\.py$)"
METRIC_RE = r"(?i)((^|/)(metrics?|analytics|tracking|telemetry)(/|\.[a-z]+$)|(^|/)events?\.(json|ya?ml)$)"
DEPS_RE = (r"(?i)(^|/)(package\.json|package-lock\.json|yarn\.lock|pnpm-lock\.yaml|requirements[^/]*\.txt|pyproject\.toml|pipfile(\.lock)?|"
           r"poetry\.lock|go\.(mod|sum)|cargo\.(toml|lock)|gemfile(\.lock)?|pubspec\.(yaml|lock)|build\.gradle(\.kts)?|pom\.xml|composer\.(json|lock))$")
CHECKS = [(API_RE, "risk", "API file changed: %s (callers outside the repo may break)"),
          (SCHEMA_RE, "risk", "Schema or migration changed: %s"),
          (METRIC_RE, "metric", "Metric definition changed: %s (dashboards may shift)"),
          (DEPS_RE, "deps", "Dependencies changed: %s (upstream not reviewed)")]
# What a PR body or plan says, and what in the diff would make it untrue
CLAIMS = [("money", r"\bno (money|pricing|price|payments?|billing|revenue) (impact|changes?)\b|\b(does|do)(n't| not) (touch|affect|change) (money|pricing|prices|payments?|billing)\b"),
          ("ui", r"\bno (user[- ]facing|ui|visual|visible|behaviou?r(al)?|functional) changes?\b|\b(backend|internal)[- ]only\b"),
          ("only", r"\bonly (touches|changes|affects|modifies) ([^.;\n]+)"),
          ("tests", r"\b(adds?|added|adding|includes?|with|new) (unit |integration |e2e |regression )?tests?\b|\btests? (added|included)\b"),
          ("schema", r"\bno (schema|database|db|migrations?)( changes?)?\b"),
          ("compat", r"\bbackwards?[- ]compatible\b|\bno breaking changes?\b|\bnon[- ]breaking\b")]
DONE_SAYS = {"done": "changed", "unplanned": "not in the plan", "missed": "not touched", "brushed": "barely touched",
             "quiet": "code only", "keep": "plan said leave alone"}
DONE_MARK = {"done": "done", "unplanned": "unplanned", "missed": "missed", "brushed": "brushed", "quiet": "brushed", "keep": "keep", "via": "via"}


def route(m, why, owner=""):
    """Who acts on a flag: money and plan questions go to the role that owns them, anything about the
    code goes to the tile's owner first. Roles say what they act on in the map ("acts_on")."""
    role = next((r for r, v in (m.get("roles") or {}).items() if why in (v.get("acts_on") or [])), None)
    default = {"money": "finance", "unplanned": "pm", "metric": "pm"}.get(why, "engineer")
    return (role or default) if why in ("money", "unplanned", "metric") else (owner or role or default)


def git_diff(rng, args):
    """git diff for a range, one commit (its own parent, or the first commit) or the branch work."""
    if rng:
        if ".." not in rng and not rng.startswith("#") and not git(["rev-parse", "--verify", "--quiet", rng + "^"]).strip():
            return git(["show", "--format="] + args + [rng])
        if ".." not in rng and not rng.startswith("#"):
            rng = rng + "^.." + rng
        return git(["diff"] + args + [rng])
    return git(["diff"] + args + [base_ref()])


def diff_detail(rng=None):
    """Changed line ranges per file, A/M/D per file, and skip/only markers added to tests."""
    hunks, status, flips, cur = {}, {}, 0, None
    for line in git_diff(rng, ["-U0", "-M", "--no-color"]).splitlines():
        if line.startswith("diff --git "):
            mm = re.match(r"diff --git a/.* b/(.*)$", line)
            cur = mm.group(1) if mm else None
            if cur:
                hunks.setdefault(cur, [])
        elif line.startswith("@@") and cur:
            mm = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            if mm:
                hunks[cur].append((int(mm.group(1)), int(mm.group(2)) if mm.group(2) is not None else 1))
        elif line.startswith("+") and not line.startswith("+++") and cur and re.search(TEST_RE, cur) and re.search(SKIP_MARK_RE, line):
            flips += 1
    for line in git_diff(rng, ["--name-status", "-M"]).splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            status[parts[-1]] = parts[0][:1]
    if not rng:
        for p in git(["ls-files", "--others", "--exclude-standard"]).splitlines():
            if not p.startswith(".readback/"):
                status[p], hunks[p] = "A", [(1, new_lines(p))]
    return {"hunks": hunks, "status": {p: s for p, s in status.items() if not p.startswith(".readback/")}, "flips": flips}


def test_line(detail):
    st = [s for p, s in detail["status"].items() if re.search(TEST_RE, p)]
    a, r = st.count("A"), st.count("D")
    parts = [", ".join(w % n for n, w in ((a, "%d added"), (len(st) - a - r, "%d changed"), (r, "%d removed")) if n)]
    if detail["flips"]:
        parts.append(plural(detail["flips"], "skip/only marker") + " added")
    return "Test files: " + " · ".join(p for p in parts if p) if st or detail["flips"] else ""


def short_paths(ps, n=2):
    return ", ".join(os.path.basename(p) for p in ps[:n]) + (" +%d" % (len(ps) - n) if len(ps) > n else "")


def importers(m, p):
    """Feature tiles whose code names this file's stem (an app bar every screen draws)."""
    stem = os.path.splitext(os.path.basename(p))[0]
    if len(stem) < 5:
        return []
    ids = {t["id"] for q in git(["grep", "-l", "-F", stem]).splitlines() if q != p and re.search(CODE_RE, q)
           for t in [tile_of(m, "features", q)] if t}
    return sorted(ids)


def checks(m, b, files, detail, run):
    """Things a reviewer should look at, shown only when they fire."""
    out, feats = [], {t["id"]: t for t in m["levels"]["features"]["tiles"]}
    paths = [p for p in (detail["status"] or files) if not re.search(TEST_RE, p)]
    for p in [p for p in b["unmapped"] if shared_rule(m, p)[0]][:2]:
        users = [feats[i]["name"] for i in shared_users(m, p)]
        out.append({"text": "Shared file changed: %s%s" % (os.path.basename(p), " (used by " + ", ".join(users[:3]) + ("…" if len(users) > 3 else "") + ")" if users else ""),
                    "who": route(m, "risk")})
    for p in [p for p in paths if re.search(UI_RE, p) and re.search(CODE_RE, p)][:5]:
        t = tile_of(m, "features", p)
        users = [i for i in importers(m, p) if t and i != t["id"]] if t and p not in t["owns"] else []
        if len(users) >= 2:
            out.append({"text": "%s is drawn by %d other features (%s)" % (os.path.basename(p), len(users), ", ".join(feats[i]["name"] for i in users[:3])),
                        "who": route(m, "risk", t.get("owner", ""))})
    for rx, why, say in CHECKS:
        hit = [p for p in paths if re.search(rx, p)]
        if hit:
            t = tile_of(m, "features", hit[0])
            out.append({"text": say % short_paths(hit), "who": route(m, why, (t or {}).get("owner", ""))})
    card, text = run.get("card"), (run.get("text") or "")
    if (card and not card.get("items")) or (run.get("source") == "pr" and len(re.sub(r"(?im)^\s*Readback-Plan:.*$", "", text).split()) < 15):
        out.append({"text": "Thin plan: ask the author what done looks like", "who": route(m, "unplanned")})
    return out


def quote(text, mm, n=12):
    end = min([i for i in (text.find(c, mm.end()) for c in ".,;\n!?") if i >= 0] + [len(text)])
    return " ".join(text[mm.start():end].split()[:n]).rstrip(",:")


def claims(m, text, b, detail):
    """Claims in the PR body or plan that the diff does not back up, with the file that says otherwise."""
    text = re.sub(r"(?im)^\s*Readback-Plan:.*$", "", text or "")
    if not text.strip():
        return []
    real = [t for t in b["tiles"] if t["touched"] and not t["brushed"] and not t["sweep"] and not t.get("quiet")]
    paths = list(detail["status"])
    out = []
    for kind, rx in CLAIMS:
        mm = re.search("(?i)" + rx, text)
        if not mm:
            continue
        said, ev, who, loud = 'Says "%s"' % quote(text, mm), None, None, True
        if kind == "money":
            hit = [t for t in real if t["money"]]
            ev, who = hit and ("but %s changed (%s)" % (hit[0]["name"], hit[0]["paths"][0])), route(m, "money")
        elif kind == "ui":
            hit = [t for t in real if t.get("audience", "customer") == "customer"]
            ev, who = hit and ("but %s changed for customers (%s)" % (hit[0]["name"], hit[0]["paths"][0])), route(m, "unplanned")
        elif kind == "only":
            named = {t["id"] for lv in LEVELS for t in m["levels"][lv]["tiles"] if t["name"].lower() in mm.group(2).lower()}
            hit = [t for t in real if t["id"] not in named] if named else []
            ev, who = hit and ("but %s changed too (%s)" % (hit[0]["name"], hit[0]["paths"][0])), route(m, "unplanned")
        elif kind == "tests":
            ev, who = (not any(re.search(TEST_RE, p) for p in paths)) and "but no test file changed", route(m, "tests")
        elif kind == "schema":
            hit = [p for p in paths if re.search(SCHEMA_RE, p) and not re.search(TEST_RE, p)]
            ev, who = hit and ("but %s changed" % hit[0]), route(m, "risk")
        elif kind == "compat":
            hit = [p for p in paths if (re.search(API_RE, p) or re.search(SCHEMA_RE, p)) and not re.search(TEST_RE, p)]
            ev, who, loud = hit and ("check it: %s changed" % short_paths(hit)), route(m, "risk"), False
        if ev:
            out.append({"text": said + ", " + ev, "who": who, "loud": loud})
    return out


def and_list(names, n=3):
    names = names[:n] + (["%d more" % (len(names) - n)] if len(names) > n else [])
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def done_card_model(m, run, b, detail, level="dev", text=None, ctx=None):
    """b is the DONE board (features, change lens); run may carry the plan card it is checked against."""
    pc, feats = run.get("card") or {}, {t["id"]: t for t in m["levels"]["features"]["tiles"]}
    what = {}
    for it in pc.get("items", []):
        if it["tile"]:
            what.setdefault(it["tile"], it["what"])
    keep, areas, seen = set(pc.get("keep", [])), {}, {}
    for t in b["tiles"]:
        via = t.get("via_shared") and not (t["touched"] and not t["brushed"])
        if t["unplanned"]:
            st = "unplanned"
        elif t["missed"]:
            st = "brushed" if t["touched"] else "missed"
        elif via:
            st = "via"
        elif t.get("quiet"):
            st = "quiet"
        elif t["touched"] and not t["brushed"] and not t["sweep"]:
            st = "done"
        else:
            continue
        if st in ("done", "unplanned") and t["id"] in keep:
            st = "keep"
        say = what.get(t["id"]) if st == "done" and what.get(t["id"]) else ("via " + os.path.basename(t["via_shared"][0]) if st == "via" else DONE_SAYS[st])
        label = t["name"] + " · " + say + (" · %s, %s" % (plural(t["files"], "file"), plural(t["lines"], "line")) if t["touched"] and st != "brushed" else "")
        areas.setdefault(feats[t["id"]].get("area", ""), []).append((label, st, t))
        seen[t["id"]] = st
    for it in pc.get("items", []):
        if not it["tile"]:
            areas.setdefault(it.get("area", ""), []).append((it["name"] + " · new, not on the map yet", "brushed", None))
    rows = [(0, b["product"] or "Product", "root")]
    for area, rs in areas.items():
        d = 1
        if area:
            rows.append((1, area, "area"))
            d = 2
        for label, st, t in rs:
            rows.append((d, label, DONE_MARK[st]))
            purpose = t and ((ctx or {}).get(t["id"]) or {}).get("purpose")
            if purpose and st in ("done", "via", "keep", "unplanned"):
                rows.append((d + 1, "↳ " + purpose, "note"))
    if len(rows) == 1:
        rows.append((1, "Nothing at the feature level", "note"))

    names = lambda *sts: [feats[i]["name"] for i, s in seen.items() if s in sts]
    planned = bool(plan_expected(run).get("features"))
    off = names("unplanned", "missed", "brushed", "keep")
    if b["kind"] != "product":
        headline, status = re.sub(r"\s*[" + DISC + "]", "", b["sentence"]).split(". ")[0].rstrip(".") + ".", "no product change"
    elif not seen:
        headline, status = "Nothing at the feature level changed", "no plan" if not planned else "off plan"
    elif planned and pc and not off:
        headline, status = "Done as planned: " + pc["headline"], "as planned"
    else:
        parts = [("Changed " + and_list(names("done", "via", "quiet"))) if names("done", "via", "quiet") else "",
                 ("not in the plan: " + and_list(names("unplanned"))) if names("unplanned") else "",
                 ("not touched: " + and_list(names("missed", "brushed"))) if names("missed", "brushed") else "",
                 ("plan said leave alone: " + and_list(names("keep"))) if names("keep") else ""]
        headline = "; ".join(p for p in parts if p)
        headline, status = headline[:1].upper() + headline[1:], ("off plan" if planned else "no plan")

    flags = [{"text": "Plan said leave %s alone, but it changed" % feats[i]["name"], "who": route(m, "risk", feats[i].get("owner", "")), "loud": True}
             for i, s in seen.items() if s == "keep"]
    flags += claims(m, text, b, detail) + checks(m, b, list(detail["status"]), detail, run)
    slot = {"merge": [pc["acts"]["merge"]] if pc.get("acts", {}).get("merge") else [],
            "release": [pc["acts"]["release"]] if pc.get("acts", {}).get("release") else []}
    for a in b["acts"]:
        who = a["who"].upper() if len(a["who"]) <= 2 else a["who"][:1].upper() + a["who"][1:]  # pm → PM
        slot["release" if a["why"] == "money" else "merge"].append(who + ": " + a["do"])
    evidence = []
    for area, rs in areas.items():
        for label, st, t in rs:
            ps = (t or {}).get("paths") or ((t or {}).get("via_shared") or [])
            if ps and st != "missed":
                evidence.append((t["name"], [(p, detail["hunks"].get(p, [])) for p in ps]))
    lines = [test_line(detail)] + (["Also changed: " + plural(len(b["unmapped"]), "file") + " no single feature owns"] if b["unmapped"] else [])
    base = {"phase": "DONE", "product": b["product"] or "Product", "headline": clip(headline, 140, "headline", []), "meta": [status],
            "tree": rows, "sketches": ["tree"], "keys": list(seen), "flags": flags, "lines": [l for l in lines if l],
            "chips": ["Needs outside step: " + o["name"] for o in pc.get("outside", [])], "evidence": evidence,
            "slots": [(name, slot[k]) for k, name in SLOTS if slot.get(k)]}
    return leveled(base, level)


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


def read_log(root):
    evs = []
    if os.path.exists(log_path(root)):
        with open(log_path(root), encoding="utf-8") as f:
            for line in f:
                try:
                    evs.append(json.loads(line))
                except ValueError:
                    pass
    return evs


def cmd_stats(argv):
    root = repo_root()
    evs = read_log(root)
    plans ={e["plan"]: e for e in evs if e.get("ev") == "plan"}
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
          "  Boards that matched the plan   " + pct(o["exact"], o["done"])]
    gs = [e for e in evs if e.get("ev") == "gaps" and e.get("sessions")]
    if gs:
        L += ["", "Context gaps (rb gaps)",
              "  Questions per session          " + " → ".join("%.1f" % (e["questions"] / e["sessions"]) for e in gs[-5:]) + "   goal: going down"]
    L += ["", "   log: .readback/log.jsonl (local, gitignored)"]
    print("\n".join(L))


# ---------------------------------------------------------------- deck (many ideas, one card each)
# Ideas become plan cards. Set math on their tiles, not a model, finds duplicates and overlaps; the user
# decides one card at a time in chat, and every Build lands on one summary tree.
DECK_CHOICES = ("build", "later", "drop", "merge")
STOP_WORDS = {"the", "and", "for", "with", "from", "into", "when", "that", "this", "adds", "add", "new", "change", "changes",
              "make", "makes", "more", "less", "per", "all", "its", "their", "our", "your", "can", "will", "not", "now", "use", "uses"}


def readback_ignore(root, name):
    """Keep a local-only file out of git (.readback/.gitignore)."""
    gi = os.path.join(root, ".readback", ".gitignore")
    old = open(gi, encoding="utf-8").read() if os.path.exists(gi) else ""
    if name not in old.split():
        os.makedirs(os.path.dirname(gi), exist_ok=True)
        with open(gi, "a", encoding="utf-8") as f:
            f.write(("\n" if old and not old.endswith("\n") else "") + name + "\n")


def deck_path(root):
    return os.path.join(root, ".readback", "deck.json")


def load_deck(root):
    if os.path.exists(deck_path(root)):
        with open(deck_path(root), encoding="utf-8") as f:
            return json.load(f)
    return {"ideas": [], "order": []}


def save_deck(root, deck):
    readback_ignore(root, "deck.json")
    with open(deck_path(root), "w", encoding="utf-8") as f:
        json.dump(deck, f, indent=1, ensure_ascii=False)


def idea_tiles(m, card, extra=()):
    by, out = map_tiles_by_key(m), []
    ids = [i["tile"] for i in card["items"] if i["tile"]] + ([card["risk"]["tile"]] if (card.get("risk") or {}).get("tile") else [])
    ids += [by[str(x).lower()]["id"] for x in extra if str(x).lower() in by]
    for i in ids:
        if i not in out:
            out.append(i)
    return out


def idea_area(m, idea):
    feats = {t["id"]: t for t in m["levels"]["features"]["tiles"]}
    return next((feats[t].get("area", "") for t in idea["tiles"] if t in feats), "") or \
        next((i.get("area", "") for i in idea["card"]["items"] if not i["tile"]), "")


def idea_words(idea):
    c = idea["card"]
    text = " ".join([idea["title"], c["headline"]] + [i["what"] for i in c["items"]])
    return set(re.findall(r"[a-z]{3,}", text.lower())) - STOP_WORDS


def deck_prep(m, deck):
    feats = {t["id"] for t in m["levels"]["features"]["tiles"]}
    areas = {t.get("area", "") for t in m["levels"]["features"]["tiles"]} - {""}
    ideas = deck["ideas"]
    for i in ideas:
        i["similar"], i["overlaps"] = [], []
        i["vague"] = not i["tiles"] and not any(it.get("area") in areas for it in i["card"]["items"] if not it["tile"])
    for x, a in enumerate(ideas):
        for b in ideas[x + 1:]:
            ta, tb = set(a["tiles"]) & feats, set(b["tiles"]) & feats
            wa, wb = idea_words(a), idea_words(b)
            j = len(wa & wb) / len(wa | wb) if wa | wb else 0
            if (ta and ta == tb and j >= 0.3) or j >= 0.6:
                a["similar"].append(b["id"]), b["similar"].append(a["id"])
            elif ta & tb:
                a["overlaps"].append(b["id"]), b["overlaps"].append(a["id"])
    # by area, so cards in a row share context; the ones that land nowhere go last
    deck["order"] = [i["id"] for i in sorted(ideas, key=lambda i: (i["vague"], idea_area(m, i).lower(), i["id"]))]
    return deck


def idea_relation(idea):
    r = []
    if idea.get("similar"):
        r.append("Similar to " + ", ".join(str(x) for x in idea["similar"][:3]) + ": merge?")
    if idea.get("overlaps"):
        r.append("Overlaps idea " + ", ".join(str(x) for x in idea["overlaps"][:4]))
    if idea.get("planned_on"):
        r.append("Planned on branch " + idea["planned_on"])
    return " · ".join(r)


def idea_card(m, deck, idea, level="dev"):
    run = {"card": idea["card"], "expected": expected_from_tiles(m, idea["tiles"]), "maybe": {}}
    c = card_model(m, run, level)
    into = (idea.get("similar") or [None])[0]
    say = "Readback deck: idea %d → " % idea["id"]
    c.update(phase="IDEA", phase_label="Idea %d of %d" % (idea["id"], len(deck["ideas"])), meta=[idea_area(m, idea), c["severity"]],
             relation=idea_relation(idea),
             buttons=[("1 Build", say + "build"), ("2 Later", say + "later"), ("3 Drop", say + "drop"),
                      ("4 Merge" + (" into %d" % into if into else ""), say + "merge" + (" into %d" % into if into else ""))])
    return c


def next_idea(deck):
    by = {i["id"]: i for i in deck["ideas"]}
    return next((by[i] for i in deck["order"] if i in by and not by[i].get("decision") and not by[i].get("vague")), None)


def deck_summary(m, deck, level="dev"):
    feats = {t["id"]: t for t in m["levels"]["features"]["tiles"]}
    by = {i["id"]: i for i in deck["ideas"]}
    builds = [i for i in deck["ideas"] if i.get("decision") == "build"]
    keys = {i["id"]: [t for t in i["tiles"] if t in feats] + ["new:" + it["name"] for it in i["card"]["items"] if not it["tile"]] for i in builds}
    for i in deck["ideas"]:  # a merged idea's tiles join the idea it went into
        if i.get("decision") == "merge" and i.get("into") in keys:
            keys[i["into"]] += [t for t in i["tiles"] if t in feats and t not in keys[i["into"]]]
    who = {}
    for iid, ks in keys.items():
        for k in ks:
            who.setdefault(k, []).append(iid)
    area_of = lambda k: feats[k].get("area", "") if k in feats else next((it.get("area", "") for i in builds for it in i["card"]["items"]
                                                                            if not it["tile"] and "new:" + it["name"] == k), "")
    areas = {}
    for k in who:
        areas.setdefault(area_of(k), []).append(k)
    rows = [(0, m.get("product", "") or "Product", "root")]
    for area, ks in areas.items():
        d = 1
        if area:
            rows.append((1, area, "area"))
            d = 2
        for k in ks:
            name = feats[k]["name"] if k in feats else k[4:] + " (new)"
            rows.append((d, name + " · idea " + ", ".join(str(x) for x in who[k]), "collide" if len(who[k]) > 1 else "change"))
    # ideas that share a tile must go one after the other; the rest can run at the same time
    group = {i: i for i in keys}
    find = lambda i: i if group[i] == i else find(group[i])
    for ids in who.values():
        for x in ids[1:]:
            group[find(x)] = find(ids[0])
    groups = {}
    for i in keys:
        groups.setdefault(find(i), []).append(i)
    count = lambda d: len([i for i in deck["ideas"] if i.get("decision") == d])
    lines = []
    if len(groups) > 1:
        lines.append("Can be built in parallel: " + " | ".join(" + ".join("%d %s" % (i, by[i]["title"]) for i in g) for g in groups.values()))
    for d, say in (("later", "Later"), ("merge", "Merged")):
        ids = [i for i in deck["ideas"] if i.get("decision") == d]
        if ids:
            lines.append(say + ": " + ", ".join("%d %s" % (i["id"], i["title"]) + (" → %d" % i["into"] if d == "merge" and i.get("into") else "") for i in ids[:6]))
    vague = [i for i in deck["ideas"] if i.get("vague")]
    if vague:
        lines.append("Too vague to place: " + ", ".join("%d %s" % (i["id"], i["title"]) for i in vague[:6]))
    left = [i for i in deck["ideas"] if not i.get("decision") and not i.get("vague")]
    flags = [{"text": "Ideas %s %s change %s: build them one after the other" % (and_list([str(x) for x in ids], 4), "both" if len(ids) == 2 else "all",
                                                                              feats[k]["name"] if k in feats else k[4:]),
              "who": route(m, "unplanned")} for k, ids in who.items() if len(ids) > 1]
    headline = ("%s to build, in %s" % (plural(len(builds), "idea"), plural(len(groups), "independent group")) if builds else "No idea marked Build yet")
    base = {"phase": "DECK", "phase_label": "Deck summary", "product": m.get("product", "") or "Product", "headline": headline,
            "meta": [plural(len(deck["ideas"]), "idea")],
            "tree": rows if builds else [], "sketches": ["tree"] if builds else [], "keys": list(who), "flags": flags,
            "lines": ["Decided: " + " · ".join(n for n in ("%d build" % len(builds), "%d later" % count("later"), "%d drop" % count("drop"),
                                                          "%d merged" % count("merge"), "%d undecided" % len(left)) if not n.startswith("0 "))] + lines,
            "slots": [("Next", ["Pick one to start: deck plan ID"])] if builds else []}
    return leveled(base, level)


def print_card(c, fmt, out=None):
    if fmt == "json":
        print(json.dumps(c, indent=1, ensure_ascii=False))
    elif fmt == "html":
        write_out(render_card_html(c), out)
    else:
        print(render_card_widget(c) if fmt == "widget" else render_card_term(c))


def print_next(root, m, deck, fmt, level):
    idea = next_idea(deck)
    if not idea:
        print("Deck done: %s decided. Next: %s deck summary" % (plural(len([i for i in deck["ideas"] if i.get("decision")]), "idea"), rb_cmd()))
        return
    print_card(idea_card(m, deck, idea, level), fmt)
    sys.stderr.write("Readback: record the user's answer with " + rb_cmd() + " deck decide %d build|later|drop|merge [--into ID]"
                     " (it prints the next card).\n" % idea["id"])


def cmd_deck(argv):
    """rb.py deck add [FILE|-]        idea cards as JSON (one object or a list; each may add "title", "source", "tiles")
       rb.py deck prep                find duplicates, overlaps and ideas too vague to place
       rb.py deck next                the next undecided idea as a card (--format term|widget|json, --for L)
       rb.py deck decide ID build|later|drop|merge [--into ID]   record the answer, print the next card
       rb.py deck summary             every Build idea on one tree: collisions and what can run in parallel
       rb.py deck plan ID             make a Build idea the current plan"""
    root = repo_root()
    os.chdir(root)
    m = load_map(root)
    if not m:
        print("No map yet in %s. Run /readback init first (or run this from the project folder)." % root)
        return
    sub, rest = (argv[0], argv[1:]) if argv else ("", [])
    deck, fmt, level = load_deck(root), args_get(argv, "--format", "term"), reader_level(m, argv)
    by = {i["id"]: i for i in deck["ideas"]}
    if sub == "add":
        src = rest[0] if rest and not rest[0].startswith("--") else "-"
        try:
            raw = json.loads(sys.stdin.read() if src == "-" else open(src, encoding="utf-8").read())
        except (OSError, ValueError) as e:
            print("Readback: could not read the ideas (%s)." % e)
            return
        nid, added, warns = max(list(by) + [0]) + 1, [], []
        for r in raw if isinstance(raw, list) else [raw]:
            card, warn = validate_card(m, r)
            if not card:
                warns += warn
                continue
            deck["ideas"].append({"id": nid, "title": clip(r.get("title") or card["headline"], 60, "title", warn), "source": str(r.get("source") or "")[:80],
                                  "card": card, "tiles": idea_tiles(m, card, r.get("tiles") or []), "decision": None})
            warns += ["idea %d: %s" % (nid, w) for w in warn]
            added.append(nid)
            nid += 1
        deck_prep(m, deck)
        save_deck(root, deck)
        if warns:
            sys.stderr.write("Readback deck: fix these and add again if they matter:\n" + "".join("  - " + w + "\n" for w in warns))
        print("Added %s%s; the deck has %s. Next: %s deck prep" % (plural(len(added), "idea"), " (%d–%d)" % (added[0], added[-1]) if len(added) > 1 else
              (" (%d)" % added[0] if added else ""), plural(len(deck["ideas"]), "idea"), rb_cmd()))
    elif sub == "prep":
        deck_prep(m, deck)
        save_deck(root, deck)
        ideas = deck["ideas"]
        sim = {tuple(sorted([i["id"]] + i["similar"])) for i in ideas if i["similar"]}
        print("Readback deck · %s · %d near-duplicate set(s) · %d overlap another · %d too vague to place"
              % (plural(len(ideas), "idea"), len(sim), len([i for i in ideas if i["overlaps"]]), len([i for i in ideas if i["vague"]])))
        for s in sorted(sim):
            print("  similar: " + ", ".join("%d %s" % (x, by[x]["title"]) for x in s))
        for i in [i for i in ideas if i["vague"]]:
            print("  too vague to place: %d %s" % (i["id"], i["title"]))
        print("Next: %s deck next (one card at a time)" % rb_cmd())
    elif sub == "next":
        print_next(root, m, deck, fmt, level)
    elif sub == "decide":
        try:
            iid, choice = int(rest[0]), rest[1].lower()
        except (IndexError, ValueError):
            print("usage: rb.py deck decide ID build|later|drop|merge [--into ID]")
            return
        choice = {"1": "build", "2": "later", "3": "drop", "4": "merge"}.get(choice, choice)
        if iid not in by or choice not in DECK_CHOICES:
            print("Readback deck: no idea %d, or '%s' is not one of %s." % (iid, choice, "|".join(DECK_CHOICES)))
            return
        into = args_get(rest, "--into") or ((by[iid].get("similar") or [None])[0] if choice == "merge" else None)
        if choice == "merge" and (not str(into).isdigit() or int(into) not in by or int(into) == iid):
            print("Readback deck: merge idea %d into which idea? Add --into ID." % iid)
            return
        by[iid].update(decision=choice, into=int(into) if choice == "merge" else None)
        save_deck(root, deck)
        log_event(root, {"ev": "deck", "idea": iid, "decision": choice})
        print_next(root, m, deck, fmt, level)
    elif sub == "summary":
        print_card(deck_summary(m, deck, level), fmt, args_get(argv, "--out"))
    elif sub == "plan":
        iid = int(rest[0]) if rest and rest[0].isdigit() else None
        if iid not in by:
            print("usage: rb.py deck plan ID (an idea in the deck)")
            return
        idea, prev = by[iid], load_run(root)
        text = idea["title"] + ". " + idea["card"]["headline"] + ". " + "; ".join(i["what"] for i in idea["card"]["items"])
        run = record_plan(root, m, text, "plan", idea["tiles"], [])
        run["card"] = idea["card"]
        note_plan(root, prev, run, [])
        idea["planned_on"] = branch()
        save_deck(root, deck)
        c = card_model(m, run, level)
        c["relation"] = also_planned(root, m, c["keys"])
        print_card(c, fmt, args_get(argv, "--out"))
        if fmt in ("term", "widget"):
            run["card_shown"] = card_digest(run["card"])
        save_run(root, run)
    else:
        left = len([i for i in deck["ideas"] if not i.get("decision") and not i.get("vague")])
        print("Readback deck: %s, %d undecided.\n%s" % (plural(len(deck["ideas"]), "idea"), left, cmd_deck.__doc__.replace("rb.py", rb_cmd())))


def also_planned(root, m, keys):
    """Another branch's plan expects some of the same features: say so on the card."""
    names, out = {t["id"]: t["name"] for t in m["levels"]["features"]["tiles"]}, []
    d, cur = os.path.join(root, ".readback", "runs"), os.path.basename(run_path(root))
    for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not fn.endswith(".json") or fn == cur:
            continue
        try:
            with open(os.path.join(d, fn), encoding="utf-8") as f:
                r = json.load(f)
        except (OSError, ValueError):
            continue
        common = [names[k] for k in keys if k in names and k in plan_expected(r).get("features", [])]
        if common:
            out.append(fn[:-5] + " (" + ", ".join(common[:3]) + ")")
    return ("Also planned on branch " + "; ".join(out[:3])) if out else ""


# ---------------------------------------------------------------- fill-me (the questions people keep asking)
# Reads this repo's Claude Code sessions on this machine only. Nothing it reads is written anywhere:
# the log keeps gap kinds, tile ids and counts; context.md holds what Claude writes, never a prompt.
GAP_KINDS = ("orientation", "impact", "recall", "where", "next")
GAP_RES = [("next", r"\b(what('s| is| are)? next|next steps?|what now|what should (i|we) do|what('s| is) left|left to do|remaining|todo|"
                    r"what (all )?(\w+ ){0,3}(we|i) (will |would )?(have|need) to do|what to do)\b"),
           ("recall", r"\b(why did (we|you|i)|what did (we|you|i)|did (we|you)|we (decided|agreed|chose)|last time|earlier|remind me|again|previous(ly)?)\b"),
           ("where", r"\b(where (is|are|do|does|did|can|should)|which (file|folder|module|function|class)|find the|located|lives?)\b"),
           ("impact", r"\b(what (will|would|could) (break|change|happen)|affects?|impact|break|safe to|side[- ]effects?|depends? on|regress)|^what if\b"),
           ("orientation", r"\b(how (does|do|is|are)|what (is|are|does|do)|explain|walk me through|overview|architecture|understand|purpose of|how many)\b|"
                           r"^(does|do|is|are|will|can) (this|it|that|readback|the (plugin|app|hook|card|board))\b")]
QUESTION_RE = r"(?i)\?\s*$|^(what|why|how|where|which|who|when|is|are|does|do|did)\b"
REQUEST_RE = r"(?i)^(can|could|would|will|please|pls)\b.*\byou\b"
# talking the work over ("shall we start?", "agree?", "done?") is a decision, not a gap in what someone knows
DISCUSS_RE = (r"(?i)^(and |or |so |but )?(shall|should|can|could|must) (we|i)\b|^(and |so )?how (can|should|could|would) (we|i)\b|"
              r"\b(agree|right|think so|got it|make sense|sound good|okay|ok)\W*$|^\W*(\w+\W+){0,2}(done|good|correct|yes|no)\W*$")


def transcripts_dir(root):
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(base, "projects", re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(root)))


def user_text(e):
    """The words a person typed: no tool results, reminders, pasted blocks or messages from other sessions."""
    if e.get("type") != "user" or e.get("isMeta") or e.get("isCompactSummary") or e.get("toolUseResult") is not None:
        return None
    c = (e.get("message") or {}).get("content")
    if isinstance(c, list):
        if any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
            return None
        c = " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    if not isinstance(c, str):
        return None
    c = re.sub(r"(?s)<(system-reminder|pasted_content|command-[a-z-]+|local-command-[a-z-]+)\b[^>]*>.*?</\1>", " ", c).strip()
    if not c or c.startswith(("[Request interrupted", "Another Claude session", "This session is being continued", "Caveat:", "<")):
        return None
    return c


def gap_kind(q):
    return next((k for k, rx in GAP_RES if re.search("(?i)" + rx, q)), "unsure")


def tool_paths(e):
    for x in (e.get("message") or {}).get("content") or []:
        if isinstance(x, dict) and x.get("type") == "tool_use":
            for k in ("file_path", "notebook_path", "path"):
                if isinstance((x.get("input") or {}).get(k), str):
                    yield x["input"][k]


def path_tile(root, m, p):
    try:
        rel = os.path.relpath(os.path.abspath(p), root).replace(os.sep, "/")
    except ValueError:
        return None
    if rel.startswith(".."):
        return None
    t = tile_of(m, "features", rel) or tile_of(m, "parts", rel)
    return t["id"] if t else None


def gap_labels_path(root):
    return os.path.join(root, ".readback", "gaps.json")


def gap_scan(root, m, days=30):
    """Questions typed in this repo's sessions, each sorted by kind and pinned to the tiles Claude
    then had to read or search to answer it (the next 8 tool paths)."""
    labels = {}
    if os.path.exists(gap_labels_path(root)):
        with open(gap_labels_path(root), encoding="utf-8") as f:
            labels = json.load(f).get("labels", {})
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - days * 86400))
    d = transcripts_dir(root)
    res, asked = {"sessions": 0, "questions": 0, "kinds": {}, "tiles": {}, "none": 0, "unsure": []}, set()
    for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not fn.endswith(".jsonl"):
            continue
        qs, active, prompted = [], [], False
        with open(os.path.join(d, fn), encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("timestamp") and e["timestamp"] < cutoff:
                    continue
                t = user_text(e)
                if t is not None:
                    prompted, active = True, []
                    for s in [s.strip() for s in re.split(r"(?<!\be\.g\.)(?<!\bi\.e\.)(?<=[.?!])\s+|\n+", t) if s.strip()][:40]:
                        qid = "q" + hashlib.sha1(" ".join(s.lower().split()).encode()).hexdigest()[:7]
                        s = re.sub(r"(?i)^((okay|ok|so|and|but|or|yea|yeah|yes|hmm|also|well|now)\b[\s,]*)+", "", s) or s
                        if not re.search(QUESTION_RE, s) or re.search(DISCUSS_RE, s) or qid in asked:
                            continue  # a resumed session repeats earlier prompts: each question counts once
                        asked.add(qid)
                        k = labels.get(qid) or gap_kind(s)
                        if k == "unsure" and re.search(REQUEST_RE, s):
                            continue  # "can you commit this?" is a request, not a gap
                        q = {"id": qid, "kind": k, "text": s[:200], "tiles": set(), "budget": 8}
                        qs.append(q)
                        active.append(q)
                elif e.get("type") == "assistant" and active:
                    for p in tool_paths(e):
                        tid = path_tile(root, m, p)
                        for q in active:
                            if q["budget"] > 0:
                                q["budget"] -= 1
                                if tid:
                                    q["tiles"].add(tid)
        res["sessions"] += 1 if prompted else 0
        for q in qs:
            res["questions"] += 1
            res["kinds"][q["kind"]] = res["kinds"].get(q["kind"], 0) + 1
            if q["kind"] == "unsure":
                res["unsure"].append((q["id"], q["text"]))
            for tid in q["tiles"]:
                res["tiles"].setdefault(tid, {})[q["kind"]] = res["tiles"].get(tid, {}).get(q["kind"], 0) + 1
            res["none"] += 0 if q["tiles"] else 1
    return res, d


def top_gap_tiles(res, n=6):
    return sorted(res["tiles"], key=lambda t: (-sum(res["tiles"][t].values()), t))[:n]


def cmd_gaps(argv):
    """rb.py gaps [--days 30] [--format term|json]   where people had to ask, by tile and kind
       rb.py gaps --unsure                          the questions the rules could not sort, to label
       rb.py gaps --label ID orientation|impact|recall|where|next"""
    root = repo_root()
    m = load_map(root)
    if not m:
        print("No map yet in %s. Run /readback init first (or run this from the project folder)." % root)
        return
    if "--label" in argv:
        i = argv.index("--label")
        qid, kind = (argv[i + 1:i + 3] + ["", ""])[:2]
        if kind not in GAP_KINDS:
            print("usage: rb.py gaps --label ID " + "|".join(GAP_KINDS))
            return
        readback_ignore(root, "gaps.json")
        data = json.load(open(gap_labels_path(root), encoding="utf-8")) if os.path.exists(gap_labels_path(root)) else {"labels": {}}
        data["labels"][qid] = kind
        with open(gap_labels_path(root), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        print("%s -> %s" % (qid, kind))
        return
    days = int(args_get(argv, "--days", "30"))
    res, d = gap_scan(root, m, days)
    if not res["sessions"]:
        print("Readback gaps: no sessions for this repo in the last %d days (looked in %s)." % (days, d))
        return
    if "--unsure" in argv:
        print("Questions the rules could not sort (shown here only; never stored):")
        for qid, text in res["unsure"]:
            print("  %s  %s" % (qid, text[:120]))
        print("Label each with: " + rb_cmd() + " gaps --label ID " + "|".join(GAP_KINDS))
        return
    log_event(root, {"ev": "gaps", "tiles": res["tiles"], "kinds": res["kinds"], "sessions": res["sessions"], "questions": res["questions"]})
    if args_get(argv, "--format") == "json":
        print(json.dumps(dict(res, unsure=len(res["unsure"])), indent=1))  # counts only, never the questions
        return
    names = {t["id"]: t["name"] for lv in LEVELS for t in m["levels"][lv]["tiles"]}
    L = ["Readback gaps · this repo's sessions, last %d days · %s · %s (%.1f per session)" % (
        days, plural(res["sessions"], "session"), plural(res["questions"], "question"), res["questions"] / res["sessions"]), "─" * 78]
    if res["tiles"]:
        L.append("Where people had to ask (questions, then what Claude had to read to answer)")
        for tid in top_gap_tiles(res, 10):
            ks = res["tiles"][tid]
            L.append("  %-22s %3d   %s" % (names.get(tid, tid)[:22], sum(ks.values()), " · ".join("%s %d" % (k, n) for k, n in sorted(ks.items(), key=lambda kv: -kv[1]))))
    if res["none"]:
        L.append("  %-22s %3d" % ("(no tile)", res["none"]))
    L += ["", "By kind: " + " · ".join("%s %d" % (k, res["kinds"][k]) for k in GAP_KINDS + ("unsure",) if res["kinds"].get(k))]
    if res["unsure"]:
        L.append("%d could not be sorted: %s gaps --unsure to label them" % (len(res["unsure"]), rb_cmd()))
    L.append("Next: %s brief, to write .readback/context.md for the top tiles" % rb_cmd())
    print("\n".join(L))


CONTEXT_TEMPLATE = """## {name} (tile: {id})
Purpose: one line, in product words
Flow: Step → Step → Step
Decisions: YYYY-MM-DD what was decided, and why
Where: entry files, config, keys and flags
Open: what is still undecided
"""


def context_path(root):
    return os.path.join(root, ".readback", "context.md")


def load_context(root):
    """.readback/context.md: one '## Name (tile: id)' section per tile, with Purpose/Flow/Decisions/Where/Open lines."""
    if not os.path.exists(context_path(root)):
        return {}
    with open(context_path(root), encoding="utf-8") as f:
        text = f.read()
    out = {}
    for sec in re.split(r"(?m)^##\s+", text)[1:]:
        head, _, body = sec.partition("\n")
        mm = re.search(r"\(tile:\s*([\w.-]+)\)", head)
        fields = {}
        for line in body.splitlines():
            fm = re.match(r"\s*[-*]?\s*(Purpose|Flow|Decisions?|Where|Open)\s*:\s*(.+)", line, re.I)
            if fm:
                fields[fm.group(1).lower().rstrip("s")] = fm.group(2).strip()
        out[mm.group(1) if mm else head.strip().lower()] = dict(fields, name=re.sub(r"\s*\(tile:.*", "", head).strip())
    return out


def brief_text(root, m, tiles=None, n=12):
    """'Before you ask': the context entries for what is being worked on now, in 12 lines or fewer."""
    ctx = load_context(root)
    if not ctx:
        return ""
    if not tiles:
        tiles = list(plan_expected(load_run(root)).get("features", []))
        for e in reversed([e for e in read_log(root) if e.get("ev") == "done"][-3:]):
            tiles += [t for t in e.get("touched", []) if t not in tiles]
        tiles = [t for t in tiles if t in ctx] or list(ctx)[:4]
    L = ["Before you ask · " + (m.get("product") or "Product") + " (from .readback/context.md)"]
    for tid in tiles:
        e = ctx.get(tid)
        if not e or len(L) >= n:
            continue
        L.append(e["name"] + ": " + e.get("purpose", ""))
        rest = " | ".join(k.title() + ": " + e[k] for k in ("flow", "where", "decision", "open") if e.get(k))
        if rest:
            L.append("  " + clip(rest, 160, "brief", []))
    return "\n".join(L[:n])


def cmd_brief(argv):
    """rb.py brief                  what to write in .readback/context.md, for the most-asked tiles
       rb.py brief --show [--tiles a,b]   the short 'Before you ask' brief"""
    root = repo_root()
    m = load_map(root)
    if not m:
        print("No map yet in %s. Run /readback init first (or run this from the project folder)." % root)
        return
    if "--show" in argv:
        tl = args_get(argv, "--tiles")
        print(brief_text(root, m, [x.strip() for x in tl.split(",")] if tl else None) or
              "No .readback/context.md yet. Run %s brief to start one." % rb_cmd())
        return
    res, _ = gap_scan(root, m)
    by = {t["id"]: t for lv in LEVELS for t in m["levels"][lv]["tiles"]}
    top = [t for t in top_gap_tiles(res) if t in by] or [t["id"] for t in m["levels"]["features"]["tiles"][:5]]
    have = load_context(root)
    L = ["Readback brief: write .readback/context.md, one section per tile below (most-asked first). Use the code and what you "
         "know of this repo; say it in your own words and never quote anyone's prompt. One line per field; drop a field you can't fill.",
         ("context.md exists: update these sections and keep the others." if have else
          "Start the file with: # What to know before you ask"), ""]
    for tid in top:
        ks = res["tiles"].get(tid, {})
        L.append("- " + by[tid]["name"] + (" (asked: " + ", ".join("%s %d" % kv for kv in sorted(ks.items(), key=lambda kv: -kv[1])) + ")" if ks else ""))
    L += ["", "Each section:", CONTEXT_TEMPLATE.format(name=by[top[0]]["name"], id=top[0]).rstrip(),
          "", "Then run %s brief --show and show it to the user to review, like the map." % rb_cmd()]
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
        print("No map yet in %s. Run /readback init first (or run this from the project folder)." % root)
        return
    role = args_get(argv, "--as")
    level, lens = "features", "change"
    if role and role in m.get("roles", {}):
        level, lens = m["roles"][role]["home"]
    level = args_get(argv, "--level", level)
    lens = args_get(argv, "--lens", lens)
    fmt = args_get(argv, "--format", "term")
    pos = [a for i, a in enumerate(argv) if not a.startswith("--") and (i == 0 or not argv[i - 1].startswith("--") or argv[i - 1] in BOOL_FLAGS)]
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
    if "--card" in argv:  # the "What changed" card, checked against the plan card when there is one
        b = boards_for(m, files, run, "features", "change", kind=kind)
        c = done_card_model(m, run, b, diff_detail(rng), reader_level(m, argv), run.get("text") if run.get("source") in ("plan", "pr") else None,
                            load_context(root))
        if fmt == "json":
            print(json.dumps(c, indent=1, ensure_ascii=False))
        elif fmt in ("html", "md"):
            write_out(render_card_html(c) if fmt == "html" else render_card_md(c, args_get(argv, "--link-base")), args_get(argv, "--out"))
        else:
            print(render_card_widget(c) if fmt == "widget" else render_card_term(c))
        return
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


BOOL_FLAGS = {"--card", "--show", "--unsure", "--force"}


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
        print("No map yet in %s. Run /readback init first (or run this from the project folder)." % root)
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
            c = card_model(m, run, reader_level(m, argv))
            c["relation"] = also_planned(root, m, c["keys"])
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
    """The launcher next to this file: it finds a working Python on whatever machine runs the command,
    where the hook's own interpreter can be a venv or the Windows Store stub."""
    return "sh \"" + os.path.join(os.path.dirname(os.path.abspath(__file__)), "rb").replace(os.sep, "/") + "\""


CARD_SCHEMA = ('{"headline": "the change in plain words, max 90 chars, no risk or hedge word first", '
               '"risk": {"text": "one risk naming its subject", "source": "plan|possible", "tile": "id"} or null, '
               '"items": [{"tile": "id" or "new_tile": "Name", "area": "for new_tile", "what": "<=32 chars", '
               '"mark": "change|new|removed|maybe|risk", "layer": "screen|logic|data|config|outside"}], '
               '"outside": [{"name": "Bank", "what": "<=32 chars"}], '
               '"flow": {"steps": ["Pay", "Bank"], "branches": [{"text": "OK: done", "mark": "change"}, {"text": "Timeout: retry", "mark": "risk"}]}, '
               '"reach": {"core": "Retry", "near": ["Orders"], "people": ["Buyers"]}, '
               '"before_after": {"before": [["Checkout", ["Cart list"]]], "after": [["Cart", ["Cart list"]]]}, '
               '"acts": {"start": "", "merge": "who checks what", "release": ""}, '
               '"keep": ["id of a tile the plan says must not change"]}')


def map_request(m, source="plan"):
    what = ("map this plan to the product map before editing. Pick only the tiles the plan says it will change "
            "(features and parts; leave out anything it does not ask for)." if source == "plan" else
            "before your first edit, say which tiles the change you are about to make will touch "
            "(features and parts; only what the user asked for).")
    return ("Readback: " + what + " Put tiles you are sure of in --tiles and "
            "tiles you are only partly sure of in --maybe:\n" + tile_list(m) +
            "\nAlso write the 'About to change' card as JSON (leave out flow, reach, before_after or acts when they don't apply; "
            "risk source is 'plan' only when the plan itself says it): " + CARD_SCHEMA +
            "\nThen run, from this project's folder as is (don't cd into the plugin), with the card on stdin (a heredoc):  " + rb_cmd() + " plan --tiles id1,id2 --maybe id3" +
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
    if run.get("card"):  # a plan card exists: show the same card checked against the diff
        c = done_card_model(m, run, b, diff_detail(), reader_level(m, []), run.get("text") if run.get("source") in ("plan", "pr") else None,
                            load_context(root))
        emit(render_card_term(c) + "\n   (/readback --card for the picture)")
        return
    emit(render_term(b))


def hook_start():
    """SessionStart: put the short 'Before you ask' brief in Claude's context, when context.md exists."""
    hook_input()
    root = repo_root()
    m = load_map(root)
    text = brief_text(root, m) if m else ""
    if text:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))


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
    elif c == "hook-start":
        hook_start()
    elif c == "deck":
        cmd_deck(rest)
    elif c == "gaps":
        cmd_gaps(rest)
    elif c == "brief":
        cmd_brief(rest)
    else:
        cmd_show(argv)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except SystemExit as e:
        if e.code and not str(e.code).isdigit():
            print(e.code)
