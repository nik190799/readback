"""The "About to change" card: validation, sketch choice, renderers and the hooks around it.

Run from the repo root: python -m unittest discover tests
"""
import glob, json, os, re, shutil, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RB = os.path.join(ROOT, "scripts", "rb.py")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import rb  # noqa: E402

MAP = json.load(open(os.path.join(HERE, "fixtures", "shop-map.json"), encoding="utf-8"))
CARDS = {os.path.basename(p)[:-5]: json.load(open(p, encoding="utf-8"))
         for p in sorted(glob.glob(os.path.join(HERE, "fixtures", "cards", "*.json")))}


def run_of(fx):
    card, warn = rb.validate_card(MAP, fx["card"])
    return {"source": "plan", "mapped": True, "card": card,
            "expected": rb.expected_from_tiles(MAP, fx["tiles"]),
            "maybe": rb.expected_from_tiles(MAP, fx["maybe"]) if fx["maybe"] else {}}, warn


class PickTest(unittest.TestCase):
    def test_six_examples_pick_the_agreed_sketches(self):
        self.assertEqual(len(CARDS), 6)
        for name, fx in CARDS.items():
            run, warn = run_of(fx)
            self.assertEqual(warn, [], name)
            c = rb.card_model(MAP, run)
            self.assertEqual(c["severity"], fx["expect"]["severity"], name)
            self.assertEqual(c["sketches"], fx["expect"]["sketches"], name)

    def test_never_more_than_three_sketches(self):
        fx = json.loads(json.dumps(CARDS["5-payment-retry"]))
        fx["card"]["before_after"] = {"before": [["A", ["x"]]], "after": [["B", ["x"]]]}
        c = rb.card_model(MAP, run_of(fx)[0])
        self.assertEqual(len(c["sketches"]), 3)
        self.assertEqual(c["sketches"][0], "tree")


class ValidateTest(unittest.TestCase):
    def check(self, card):
        return rb.validate_card(MAP, dict({"headline": "Adds a thing", "items": []}, **card))

    def test_unknown_tile_is_dropped_and_named(self):
        card, warn = self.check({"items": [{"tile": "nope", "what": "x"}, {"tile": "settings", "what": "y"}]})
        self.assertEqual([i["tile"] for i in card["items"]], ["settings"])
        self.assertTrue(any('"nope"' in w for w in warn))

    def test_tile_names_work_like_ids(self):
        card, warn = self.check({"items": [{"tile": "Card charge", "what": "x"}]})
        self.assertEqual(card["items"][0]["tile"], "card_charge")
        self.assertEqual(warn, [])

    def test_bad_enums_fall_back(self):
        card, warn = self.check({"items": [{"tile": "settings", "what": "x", "mark": "big", "layer": "cloud"}],
                                 "risk": {"text": "Could break", "source": "guess"}})
        self.assertEqual((card["items"][0]["mark"], card["items"][0]["layer"]), ("change", "screen"))
        self.assertEqual(card["risk"]["source"], "possible")
        self.assertEqual(len(warn), 3)

    def test_headline_must_lead_with_the_change(self):
        for h in ("Risk: payments may break", "May drop rows on export", "Possible data loss"):
            self.assertTrue(any("headline" in w for w in self.check({"headline": h})[1]), h)
        self.assertEqual(self.check({"headline": "Rewrites CSV export for big accounts"})[1], [])

    def test_long_labels_are_cut(self):
        card, warn = self.check({"items": [{"tile": "settings", "what": "a very long description of what changes here"}]})
        self.assertTrue(card["items"][0]["what"].endswith("…"))
        self.assertLessEqual(len(card["items"][0]["what"]), 32)
        self.assertTrue(warn)

    def test_possible_risk_becomes_a_question_before_start(self):
        card, _ = self.check({"risk": {"text": "Rows may be dropped", "source": "possible"}})
        self.assertIn("Rows may be dropped", card["acts"]["start"])
        card, _ = self.check({"risk": {"text": "Rows may be dropped", "source": "plan"}})
        self.assertFalse(card["acts"].get("start"))

    def test_the_question_keeps_the_whole_risk(self):
        risk = "A timed-out charge may already have gone through, so the retry can double-charge"
        card, _ = self.check({"risk": {"text": risk, "source": "possible"}})
        self.assertEqual(card["acts"]["start"], "Ask the author: " + risk + "?")

    def test_new_tile_needs_no_map_entry(self):
        card, warn = self.check({"items": [{"new_tile": "Cart", "area": "Checkout", "what": "new screen"}]})
        self.assertEqual((card["items"][0]["name"], card["items"][0]["mark"]), ("Cart", "new"))
        self.assertEqual(warn, [])

    def test_half_filled_sketch_data_is_dropped(self):
        card, warn = self.check({"flow": {"steps": ["Pay"], "branches": []}, "reach": {"core": "X"}})
        self.assertNotIn("flow", card)
        self.assertNotIn("reach", card)
        self.assertEqual(len(warn), 2)


HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
VAR_WITH_FALLBACK = re.compile(r"var\(--[a-z0-9-]+,\s*#[0-9a-fA-F]{3,8}\)")


class RenderTest(unittest.TestCase):
    def test_widget_is_a_host_fragment(self):
        for name, fx in CARDS.items():
            w = rb.render_card_widget(rb.card_model(MAP, run_of(fx)[0]))
            self.assertNotRegex(w, r"(?i)<!doctype|<html|<body|<head", name)
            self.assertNotRegex(w, r"position:\s*fixed", name)
            self.assertNotRegex(w, r"<script", name)
            self.assertEqual(HEX.findall(VAR_WITH_FALLBACK.sub("", w)), [], name + ": colour outside var(--x, fallback)")
            self.assertIn(fx["card"]["headline"].replace("'", "&#x27;"), w, name)
            self.assertEqual(w.count("<svg"), len(fx["expect"]["sketches"]), name)

    def test_html_page_defines_light_and_dark(self):
        h = rb.render_card_html(rb.card_model(MAP, run_of(CARDS["6-login-code"])[0]))
        self.assertTrue(h.startswith("<!doctype html>"))
        self.assertIn("prefers-color-scheme:dark", h.replace(" ", ""))
        self.assertIn("--text-warning", h)

    def test_term_card(self):
        t = rb.render_card_term(rb.card_model(MAP, run_of(CARDS["5-payment-retry"])[0]))
        lines = t.splitlines()
        self.assertTrue(lines[0].startswith("Readback · Shop app · About to change · high"))
        self.assertEqual(lines[1], "Retries a card payment once when the bank times out")
        self.assertIn("Risk: A slow bank could charge the buyer twice (from plan)", t)
        self.assertIn("├─ Payments", t)
        self.assertIn("Outside the repo", t)
        self.assertIn("What happens: Pay → Bank → OK: done | Timeout: retry once | Both go through: charged twice",
                      " ".join(t.split()))  # wrapped at 80
        self.assertIn("Before merge", t)
        self.assertNotIn("Before start", t)  # a risk from the plan is not turned into a question
        self.assertTrue(all(len(l) <= 80 for l in lines), t)

    def test_term_tree_only_for_a_small_change(self):
        t = rb.render_card_term(rb.card_model(MAP, run_of(CARDS["1-label"])[0]))
        self.assertIn("No risk found", t)
        self.assertNotIn("What happens", t)
        self.assertIn("└─ Product page · button label", t)


class CommandTest(unittest.TestCase):
    """plan --card and the Stop hook, in a throwaway git repo with the fixture map."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, ".readback"))
        shutil.copy(os.path.join(HERE, "fixtures", "shop-map.json"), os.path.join(self.dir, ".readback", "map.json"))
        for a in (["init", "-q"], ["checkout", "-q", "-b", "feat"]):
            subprocess.run(["git"] + a, cwd=self.dir, check=True)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def rb(self, *args, stdin=""):
        p = subprocess.run([sys.executable, RB] + list(args), cwd=self.dir, input=stdin, capture_output=True,
                           text=True, encoding="utf-8")
        return p.stdout, p.stderr

    def test_plan_card_then_show_widget(self):
        fx = CARDS["6-login-code"]
        out, err = self.rb("plan", "--tiles", ",".join(fx["tiles"]), "--card", "-", stdin=json.dumps(fx["card"]))
        self.assertIn("Adds a texted code to login", out)
        self.assertIn("--format widget", err)
        w, _ = self.rb("plan", "--show", "--format", "widget")
        self.assertEqual(w.count("<svg"), 3)

    def test_plan_without_card_keeps_the_old_board(self):
        out, _ = self.rb("plan", "--tiles", "settings")
        self.assertIn("About to change", out)
        self.assertIn("Settings", out)
        self.assertNotIn("<svg", out)

    def test_stop_hook_asks_once_for_an_unshown_card(self):
        fx = CARDS["2-currency"]
        self.rb("plan", "--tiles", ",".join(fx["tiles"]), "--card", "-", "--format", "json", stdin=json.dumps(fx["card"]))
        out, _ = self.rb("hook-done", stdin=json.dumps({"session_id": "s"}))
        self.assertEqual(json.loads(out)["decision"], "block")
        self.assertIn("--format widget", json.loads(out)["reason"])
        out, _ = self.rb("hook-done", stdin=json.dumps({"session_id": "s"}))
        self.assertEqual(out.strip(), "")  # asked once, never loops

    def test_stop_hook_quiet_once_shown(self):
        fx = CARDS["2-currency"]
        self.rb("plan", "--tiles", ",".join(fx["tiles"]), "--card", "-", stdin=json.dumps(fx["card"]))
        out, _ = self.rb("hook-done", stdin=json.dumps({"session_id": "s"}))
        self.assertEqual(out.strip(), "")

    def test_new_plan_drops_the_old_card(self):
        fx = CARDS["2-currency"]
        self.rb("plan", "--tiles", ",".join(fx["tiles"]), "--card", "-", stdin=json.dumps(fx["card"]))
        self.rb("hook-plan", stdin=json.dumps({"tool_input": {"plan": "Something else"}}))
        out, _ = self.rb("hook-done", stdin=json.dumps({"session_id": "s"}))
        self.assertEqual(out.strip(), "")

    def test_map_request_asks_for_the_card(self):
        out, _ = self.rb("hook-plan", stdin=json.dumps({"tool_input": {"plan": "Add a texted login code"}}))
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("--card -", ctx)
        self.assertIn("--format widget", ctx)
        # the launcher, not the hook's own Python (a venv or the Windows Store stub elsewhere)
        self.assertIn('sh "' + os.path.join(ROOT, "scripts", "rb").replace(os.sep, "/") + '" plan', ctx)
        self.assertNotIn(sys.executable.replace(os.sep, "/"), ctx)


if __name__ == "__main__":
    unittest.main()
