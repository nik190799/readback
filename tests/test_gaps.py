"""fill-me: questions from this repo's sessions, sorted by kind and pinned to tiles; the brief and its hook.

Run from the repo root: python -m unittest discover tests
"""
import json, os, re, shutil, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_card import RB  # noqa: E402

NOW = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
OLD = "2001-01-01T00:00:00.000Z"
SECRET = "the launch codes are 1234"


def user(text, ts=NOW, **kw):
    return dict({"type": "user", "timestamp": ts, "sessionId": "s1", "message": {"role": "user", "content": text}}, **kw)


def reads(*paths):
    return {"type": "assistant", "timestamp": NOW, "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": p}} for p in paths]}}


class GapsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, ".readback"))
        shutil.copy(os.path.join(HERE, "fixtures", "shop-map.json"), os.path.join(self.dir, ".readback", "map.json"))
        subprocess.run(["git", "init", "-q"], cwd=self.dir, check=True)
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=self.dir, capture_output=True, text=True).stdout.strip()
        tdir = os.path.join(self.cfg, "projects", re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(top)))
        os.makedirs(tdir)
        price = os.path.join(top, "src", "checkout", "price.js")
        lines = [
            user("<system-reminder>ignore me?</system-reminder>How does the price rounding work?"),
            reads(price, os.path.join(top, "src", "payments", "charge.js")),
            user("What will break if I change the currency field?"),
            reads(price),
            user("Why did we pick banker's rounding? Also " + SECRET + "."),
            reads(price),
            user("Can you commit this?"),  # a request, not a gap
            user("Hmm, thoughts on lunch?"),  # no rule fits: unsure
            user("Shall we start? Agree?"),  # talking it over, not a gap
            user("How does the price rounding work?", sessionId="s2"),  # a resumed session repeats it: counted once
            user("what's next?", ts=OLD),  # older than --days
            {"type": "user", "timestamp": NOW, "message": {"role": "user", "content": [{"type": "tool_result", "content": "Where is X?"}]}},
            user("Where is the login code?", isMeta=True),
        ]
        with open(os.path.join(tdir, "s1aaaaaa-0000.jsonl"), "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(l) for l in lines) + "\n")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self.cfg, ignore_errors=True)

    def rb(self, *args, stdin=""):
        env = dict(os.environ, CLAUDE_CONFIG_DIR=self.cfg)
        p = subprocess.run([sys.executable, RB] + list(args), cwd=self.dir, input=stdin, capture_output=True, text=True,
                           encoding="utf-8", env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def test_questions_by_kind_and_tile(self):
        res = json.loads(self.rb("gaps", "--format", "json"))
        self.assertEqual((res["sessions"], res["questions"]), (1, 4))
        self.assertEqual(res["kinds"], {"orientation": 1, "impact": 1, "recall": 1, "unsure": 1})
        self.assertEqual(res["tiles"]["prices"], {"orientation": 1, "impact": 1, "recall": 1})
        self.assertEqual(res["tiles"]["card_charge"], {"orientation": 1})
        out = self.rb("gaps")
        self.assertIn("Prices", out)
        self.assertIn("1 could not be sorted", out)

    def test_nothing_typed_is_stored(self):
        self.rb("gaps")
        with open(os.path.join(self.dir, ".readback", "log.jsonl"), encoding="utf-8") as f:
            log = f.read()
        self.assertIn('"ev": "gaps"', log)
        for words in (SECRET, "rounding", "lunch", "currency"):
            self.assertNotIn(words, log)

    def test_label_an_unsure_question(self):
        out = self.rb("gaps", "--unsure")
        qid = re.search(r"(q[0-9a-f]{7})\s+thoughts on lunch", out).group(1)
        self.rb("gaps", "--label", qid, "next")
        self.assertEqual(json.loads(self.rb("gaps", "--format", "json"))["kinds"].get("next"), 1)

    def test_brief_template_then_show_then_hook(self):
        out = self.rb("brief")
        self.assertIn("- Prices (asked:", out)
        self.assertIn("(tile: prices)", out)
        self.assertEqual(self.rb("hook-start", stdin="{}").strip(), "")  # no context.md yet
        with open(os.path.join(self.dir, ".readback", "context.md"), "w", encoding="utf-8") as f:
            f.write("# What to know before you ask\n\n## Prices (tile: prices)\nPurpose: shows what a buyer pays\n"
                    "Flow: Cart → Price → Total\nDecisions: 2026-10-01 banker's rounding, to match the bank\nOpen: tax-inclusive display\n")
        brief = self.rb("brief", "--show")
        self.assertIn("Prices: shows what a buyer pays", brief)
        self.assertLessEqual(len(brief.splitlines()), 12)
        ctx = json.loads(self.rb("hook-start", stdin="{}"))["hookSpecificOutput"]
        self.assertEqual(ctx["hookEventName"], "SessionStart")
        self.assertIn("Before you ask", ctx["additionalContext"])

    def test_stats_tracks_questions_per_session(self):
        self.rb("gaps")
        self.assertIn("Questions per session          4.0", self.rb("stats"))


if __name__ == "__main__":
    unittest.main()
