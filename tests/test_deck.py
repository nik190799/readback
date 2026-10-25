"""/readback deck: many ideas, one card each, decided in chat, landing on one summary tree.

Run from the repo root: python -m unittest discover tests
"""
import glob, json, os, re, shutil, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_card import HEX, VAR_WITH_FALLBACK, RB  # noqa: E402

IDEAS = os.path.join(HERE, "fixtures", "deck-ideas.json")


class DeckTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, ".readback"))
        shutil.copy(os.path.join(HERE, "fixtures", "shop-map.json"), os.path.join(self.dir, ".readback", "map.json"))
        for a in (["init", "-q"], ["checkout", "-q", "-b", "ideas"]):
            subprocess.run(["git"] + a, cwd=self.dir, check=True)
        self.out, self.err = self.rb("deck", "add", IDEAS)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def rb(self, *args, stdin=""):
        p = subprocess.run([sys.executable, RB] + list(args), cwd=self.dir, input=stdin, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout, p.stderr

    def deck(self):
        with open(os.path.join(self.dir, ".readback", "deck.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_add_and_prep(self):
        self.assertIn("Added 10 ideas (1–10)", self.out)
        with open(os.path.join(self.dir, ".readback", ".gitignore"), encoding="utf-8") as f:
            self.assertIn("deck.json", f.read())
        out, _ = self.rb("deck", "prep")
        self.assertIn("1 near-duplicate set(s)", out)
        self.assertIn("similar: 1 Local currency prices, 2 Prices in local currency", out)
        self.assertIn("too vague to place: 8 Make it nicer", out)
        by = {i["id"]: i for i in self.deck()["ideas"]}
        self.assertIn(3, by[1]["overlaps"])  # the picker also changes prices
        self.assertEqual(self.deck()["order"][-1], 8)  # vague ideas go last

    def test_next_is_a_card_with_four_buttons(self):
        out, err = self.rb("deck", "next", "--format", "widget")
        self.assertIn("Idea", out)
        self.assertEqual(len(re.findall(r"<button", out)), 4)
        self.assertIn("sendPrompt(", out)
        self.assertNotRegex(out, r"(?i)<!doctype|<html|<script|position:\s*fixed")
        self.assertEqual(HEX.findall(VAR_WITH_FALLBACK.sub("", out)), [])
        self.assertIn("deck decide", err)
        term, _ = self.rb("deck", "next")
        self.assertIn("Choose: 1 Build", term)

    def test_decide_walks_the_deck_and_merge_finds_the_twin(self):
        first = self.deck()["order"][0]
        out, _ = self.rb("deck", "decide", str(first), "build")
        self.assertNotIn("Idea %d of" % first, out)
        self.rb("deck", "decide", "2", "merge")  # no --into: goes to its near-duplicate
        self.assertEqual({i["id"]: i for i in self.deck()["ideas"]}[2]["into"], 1)
        out, _ = self.rb("deck", "decide", "99", "build")
        self.assertIn("no idea 99", out)

    def test_summary_shows_collisions_and_parallel_groups(self):
        for iid, d in ((1, "build"), (3, "build"), (4, "build"), (5, "later"), (6, "drop")):
            self.rb("deck", "decide", str(iid), d)
        out, _ = self.rb("deck", "summary")
        self.assertIn("3 ideas to build, in 2 independent groups", out)
        self.assertRegex(out, r"Prices · idea 1, 3")
        self.assertIn("Can be built in parallel", out)
        self.assertIn("Later: 5 Login code", out)
        c = json.loads(self.rb("deck", "summary", "--format", "json")[0])
        self.assertIn([2, "Prices · idea 1, 3", "collide"], c["tree"])

    def test_plan_makes_an_idea_the_current_plan(self):
        self.rb("deck", "decide", "4", "build")
        out, _ = self.rb("deck", "plan", "4")
        self.assertIn("Retries a card payment once when the bank times out", out)
        runs = glob.glob(os.path.join(self.dir, ".readback", "runs", "*.json"))
        self.assertEqual(len(runs), 1)
        with open(runs[0], encoding="utf-8") as f:
            run = json.load(f)
        self.assertEqual(run["card"]["headline"], "Retries a card payment once when the bank times out")
        self.assertIn("card_charge", run["expected"]["features"])


if __name__ == "__main__":
    unittest.main()
