"""Reader levels: one card squeezed for dev, senior, lead or manager, with risks and owners carried up.

Run from the repo root: python -m unittest discover tests
"""
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_card import MAP, CARDS, run_of, rb  # noqa: E402


def flat(c):
    return " ".join(rb.render_card_term(c, width=400).split())


class LevelTest(unittest.TestCase):
    def test_what_a_lower_reader_saw_is_there_for_every_reader_above(self):
        for name, fx in CARDS.items():
            run = run_of(fx)[0]
            cards = [rb.card_model(MAP, run, lv) for lv in rb.READERS]
            for i, low in enumerate(cards):
                for f in rb.card_facts(low):
                    word = f if isinstance(f, str) else f[0]
                    for up in cards[i + 1:]:
                        self.assertIn(word, flat(up), "%s: %s lost at %s" % (name, word, up["level"]))

    def test_the_risk_reaches_the_manager(self):
        c = rb.card_model(MAP, run_of(CARDS["5-payment-retry"])[0], "manager")
        self.assertIn("A slow bank could charge the buyer twice", flat(c))
        self.assertEqual((c["tree"], c["sketches"]), ([], []))
        self.assertLessEqual(len(c["slots"]), 1)

    def test_budgets(self):
        for name, fx in CARDS.items():
            run = run_of(fx)[0]
            for lv in ("dev", "senior", "lead"):
                c = rb.card_model(MAP, run, lv)
                self.assertLessEqual(len(c["tree"]), rb.TREE_BUDGET[lv] + 1, name + " " + lv)
                if lv != "dev":
                    self.assertFalse(any(" files," in l for _, l, _ in c["tree"]), name)
            self.assertLessEqual(len(rb.card_model(MAP, run, "lead")["sketches"]), 1, name)

    def test_senior_sees_what_it_touches_underneath_first(self):
        c = rb.card_model(MAP, run_of(CARDS["5-payment-retry"])[0], "senior")
        self.assertEqual(c["sketches"], ["tree", "radius", "flow"])

    def test_squeeze_keeps_loud_rows_and_counts_the_rest(self):
        rows = [(0, "App", "root")] + [(1, "Tile %d" % i, "change") for i in range(10)] + [(1, "Payments", "risk")]
        out = rb.squeeze_tree(rows, 5)
        self.assertIn((1, "Payments", "risk"), out)
        self.assertEqual(out[-1], (1, "+8 more", "more"))
        self.assertEqual(len(out), 5)

    def test_lead_collapses_a_tile_to_one_row(self):
        rows = [(0, "App", "root"), (1, "Checkout", "group"), (2, "split page", "change"), (2, "new cart", "new")]
        self.assertEqual(rb.collapse_tree(rows), [(0, "App", "root"), (1, "Checkout · 2 changes", "change")])

    def test_only_the_deck_widget_has_buttons(self):
        for name, fx in CARDS.items():
            for lv in rb.READERS:
                w = rb.render_card_widget(rb.card_model(MAP, run_of(fx)[0], lv))
                self.assertNotIn("onclick", w, name + " " + lv)
                self.assertNotRegex(w, r"(?i)<!doctype|<script|position:\s*fixed", name + " " + lv)

    def test_reader_from_flag_or_role(self):
        self.assertEqual(rb.reader_level(MAP, ["--for", "lead"]), "lead")
        self.assertEqual(rb.reader_level(MAP, ["--as", "pm"]), "lead")
        self.assertEqual(rb.reader_level(dict(MAP, roles={"pm": {"for": "manager"}}), ["--as", "pm"]), "manager")
        self.assertEqual(rb.reader_level(MAP, []), "dev")


if __name__ == "__main__":
    unittest.main()
