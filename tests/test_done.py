"""The "What changed" card: the plan card checked against a real diff, in a throwaway git repo.

Run from the repo root: python -m unittest discover tests
"""
import json, os, shutil, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_card import RB, rb  # noqa: E402

PLAN = {"headline": "Shows prices in the buyer's currency",
        "risk": {"text": "Rounding could change totals", "source": "plan", "tile": "prices"},
        "items": [{"tile": "prices", "what": "local currency"}, {"tile": "settings", "what": "currency picker"}],
        "outside": [{"name": "FX feed", "what": "daily rates"}],
        "acts": {"merge": "Finance: check rounding"},
        "keep": ["card_charge"]}


def lines(n, tag):
    return "".join("%s line %d\n" % (tag, i) for i in range(n))


class DoneTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, ".readback"))
        shutil.copy(os.path.join(HERE, "fixtures", "shop-map.json"), os.path.join(self.dir, ".readback", "map.json"))
        self.write({"src/checkout/price.js": lines(20, "price"), "src/account/settings.js": lines(20, "settings"),
                    "src/payments/charge.js": lines(20, "charge"), "src/home/banner.js": lines(20, "banner"),
                    "tests/price.test.js": "it('adds', () => {})\n", "package.json": "{}\n"})
        for a in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"],
                  ["checkout", "-q", "-b", "feat"]):
            self.git(*a)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def git(self, *a):
        subprocess.run(["git"] + list(a), cwd=self.dir, check=True, capture_output=True)

    def write(self, files):
        for p, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(self.dir, p)) or self.dir, exist_ok=True)
            with open(os.path.join(self.dir, p), "w", encoding="utf-8", newline="\n") as f:
                f.write(text)

    def rb(self, *args, stdin=""):
        p = subprocess.run([sys.executable, RB] + list(args), cwd=self.dir, input=stdin, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def plan(self):
        self.rb("plan", "--tiles", "prices,settings", "--card", "-", "--format", "json", stdin=json.dumps(PLAN))

    def card(self, *extra):
        return json.loads(self.rb("show", "--card", "--format", "json", *extra))

    def test_as_planned(self):
        self.plan()
        self.write({"src/checkout/price.js": lines(30, "local price"), "src/account/settings.js": lines(25, "picker")})
        c = self.card()
        self.assertEqual(c["headline"], "Done as planned: Shows prices in the buyer's currency")
        self.assertEqual(c["meta"], ["as planned"])
        self.assertIn([2, "Prices · local currency · 1 file, 50 lines", "done"], c["tree"])
        self.assertIn("Needs outside step: FX feed", c["chips"])
        self.assertIn(["Before merge", ["Finance: check rounding"]], c["slots"])

    def test_off_plan_unplanned_missed_and_keep(self):
        self.plan()
        self.write({"src/checkout/price.js": lines(30, "local price"), "src/home/banner.js": lines(30, "new banner"),
                    "src/payments/charge.js": lines(30, "charge v2")})
        c = self.card()
        marks = {l.split(" · ")[0]: mk for _, l, mk in c["tree"]}
        self.assertEqual((marks["Prices"], marks["Banner"], marks["Settings"], marks["Card charge"]), ("done", "unplanned", "missed", "keep"))
        self.assertIn("not in the plan: Banner", c["headline"])
        self.assertIn("not touched: Settings", c["headline"])
        self.assertEqual(c["meta"], ["off plan"])
        self.assertIn("Plan said leave Card charge alone, but it changed", [f["text"] for f in c["flags"]])
        self.assertEqual(next(f for f in c["flags"] if "Card charge" in f["text"])["who"], "payments")  # the tile's owner

    def test_evidence_tests_and_deps(self):
        self.plan()
        self.write({"src/checkout/price.js": lines(5, "price") + "changed\n" + lines(14, "price")[len("price line 0\n") * 0:],
                    "tests/price.test.js": "it.skip('adds', () => {})\nit('rounds', () => {})\n",
                    "tests/currency.test.js": "it('converts', () => {})\n", "package.json": '{"dependencies": {"fx": "1"}}\n'})
        c = self.card()
        ev = dict((n, dict((p, hs) for p, hs in fs)) for n, fs in c["evidence"])
        self.assertIn("src/checkout/price.js", ev["Prices"])
        self.assertTrue(ev["Prices"]["src/checkout/price.js"])
        self.assertIn("Test files: 1 added, 1 changed · 1 skip/only marker added", c["lines"])
        self.assertTrue(any(f["text"].startswith("Dependencies changed: package.json") for f in c["flags"]))
        md = self.rb("show", "--card", "--format", "md", "--link-base", "https://example.test/blob/abc")
        self.assertRegex(md, r"https://example\.test/blob/abc/src/checkout/price\.js#L\d+")

    def test_claims_against_a_pr_body(self):
        self.write({"src/checkout/price.js": lines(30, "local price")})
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "prices")
        body = "Shows local prices. No money impact, and tests added.\nReadback-Plan: Prices"
        c = json.loads(self.rb("show", "main..HEAD", "--card", "--plan", "-", "--format", "json", stdin=body))
        texts = [f["text"] for f in c["flags"]]
        self.assertIn('Says "No money impact", but Prices changed (src/checkout/price.js)', texts)
        self.assertIn('Says "tests added", but no test file changed', texts)
        self.assertEqual(next(f for f in c["flags"] if "money" in f["text"])["who"], "finance")

    def test_levels_carry_the_keep_flag_up(self):
        self.plan()
        self.write({"src/payments/charge.js": lines(30, "charge v2"), "src/checkout/price.js": lines(30, "p")})
        c = self.card("--for", "manager")
        text = " ".join(rb.render_card_term({**c, "tree": [tuple(r) for r in c["tree"]], "slots": [tuple(s) for s in c["slots"]],
                                             "evidence": []}, width=400).split())
        self.assertIn("Card charge", text)

    def test_stop_hook_prints_the_done_card_once(self):
        self.plan()
        self.rb("plan", "--show")  # shown, so the hook does not ask for it
        self.write({"src/checkout/price.js": lines(30, "local price")})
        out = json.loads(self.rb("hook-done", stdin=json.dumps({"session_id": "s"})))
        self.assertIn("What changed", out["systemMessage"])
        self.assertIn("Prices", out["systemMessage"])
        self.assertNotIn("decision", out)
        self.assertEqual(self.rb("hook-done", stdin=json.dumps({"session_id": "s"})).strip(), "")

    def test_without_a_plan_card_show_is_the_old_board(self):
        self.write({"src/checkout/price.js": lines(30, "local price")})
        out = self.rb("show")
        self.assertIn("Prices", out)
        self.assertNotIn("Done as planned", out)


if __name__ == "__main__":
    unittest.main()
