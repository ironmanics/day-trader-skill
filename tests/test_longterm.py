import datetime
import io
import json
import os
import shutil
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import load

lt = load("trader-longterm", "screen")
scan = lt.scan
NOW = scan.parse_now("2026-11-24 17:00")
R = {"min_momentum": 0.0, "max_off_high": 0.25, "max_drawdown": 0.40}
START = datetime.datetime(2024, 1, 1, 21, 0, tzinfo=datetime.timezone.utc)


def bars(closes):
    d0 = datetime.date(2024, 1, 1)
    return [(d0 + datetime.timedelta(days=i), c) for i, c in enumerate(closes)]


def series(closes, price=None, volume=2000000):
    t = [int((START + datetime.timedelta(days=i)).timestamp()) for i in range(len(closes))]
    return {"t": t, "c": list(closes), "o": None, "h": None, "l": None, "v": None,
            "meta": {"regularMarketPrice": closes[-1] if price is None else price,
                     "regularMarketVolume": volume}}


def trend(rate, n=300, start=100.0):
    return [start * (1 + rate) ** i for i in range(n)]


class Metrics(unittest.TestCase):
    def test_too_few_closes(self):
        self.assertIsNone(lt.metrics(bars(trend(0.001, n=252))))
        self.assertIsNotNone(lt.metrics(bars(trend(0.001, n=253))))

    def test_steady_uptrend_passes_everything(self):
        m = lt.metrics(bars(trend(0.001)))
        self.assertAlmostEqual(m["mom_12_1"], 1.001 ** 231 - 1, places=6)
        self.assertTrue(m["sma200_rising"])
        self.assertAlmostEqual(m["off_high"], 0.0)
        self.assertAlmostEqual(m["max_dd_6m"], 0.0)
        self.assertTrue(all(lt.rule_results(m, R).values()))

    def test_steady_downtrend_fails_trend_and_momentum(self):
        res = lt.rule_results(lt.metrics(bars(trend(-0.002))), R)
        self.assertFalse(res["above 200-day average"])
        self.assertFalse(res["200-day average rising"])
        self.assertFalse(res["12-1 momentum"])

    def test_crash_in_the_last_six_months_fails_drawdown(self):
        closes = trend(0.001, n=250) + [c * 0.5 for c in trend(0.001, n=50, start=100 * 1.001 ** 250)]
        m = lt.metrics(bars(closes))
        self.assertLess(m["max_dd_6m"], -0.40)
        self.assertFalse(lt.rule_results(m, R)["6-month drawdown"])

    def test_bad_closes_are_ignored_not_divided_by(self):
        closes = trend(0.001)
        closes[100] = 0.0
        self.assertIsNotNone(lt.metrics(bars(closes + [closes[-1]])))


class Screen(unittest.TestCase):
    def test_liquidity_filters_and_counts(self):
        data = {"UP": series(trend(0.001)),
                "PENNY": series(trend(0.001), price=3.0),
                "THIN": series(trend(0.001), volume=1000),
                "NEW": series(trend(0.001, n=100))}
        rows, counts, last = lt.screen_prices(data, ["UP", "PENNY", "THIN", "NEW", "GONE"], NOW, R, 5.0, 5e6)
        self.assertEqual([r["symbol"] for r in rows], ["UP"])
        self.assertEqual((counts["fetched"], counts["illiquid"], counts["short"], counts["passed"]),
                         (4, 2, 1, 1))
        self.assertEqual(last, datetime.date(2024, 10, 26))

    def test_zero_floor_scores_everything_and_marks_failures(self):
        data = {"UP": series(trend(0.001)), "DOWN": series(trend(-0.002))}
        rows, counts, _ = lt.screen_prices(data, ["UP", "DOWN"], NOW, R, 0.0, 0.0)
        self.assertEqual({r["symbol"]: r["passed"] for r in rows}, {"UP": True, "DOWN": False})


def fake_facts(table):
    """table: {TICKER: (revenue_growth, operating_margin)}; a ticker not in it has no CIK."""
    class FactsError(Exception):
        pass

    class Fetcher(object):
        def __init__(self, ua=None, offline=None):
            pass

        def json(self, url, name):
            return {"cik": url}

    ciks = {t: ("%010d" % i, t) for i, t in enumerate(table, 1)}

    def analyse(cf, quarters=6, name=None):
        g, om = table[name]
        if g == "boom":
            raise FactsError("no quarterly revenue")
        return {"g": g, "om": om}

    def metrics_row(s, res):
        return {"revenue_growth_yoy": res["g"], "operating_margin": res["om"], "period_end": "2026-09-30"}

    return types.SimpleNamespace(Fetcher=Fetcher, FactsError=FactsError, FACTS_URL="u%s",
                                 ticker_ciks=lambda f: ciks, analyse=analyse, metrics_row=metrics_row)


class Fundamentals(unittest.TestCase):
    def test_pass_fail_unknown_and_missing(self):
        f = fake_facts({"GOOD": (12.0, 8.0), "LOSS": (30.0, -5.0), "SHRINK": (-2.0, 9.0),
                        "BANK": (None, None), "ERR": ("boom", 0)})
        out = lt.fundamentals(f, ["GOOD", "LOSS", "SHRINK", "BANK", "ERR", "ETF"])
        self.assertEqual({k: v["pass"] for k, v in out.items()},
                         {"GOOD": True, "LOSS": False, "SHRINK": False, "BANK": None, "ERR": None, "ETF": None})
        self.assertIn("error", out["ERR"])
        self.assertIn("error", out["ETF"])


def spark_doc(table):
    result = []
    for sym, closes in table.items():
        t = [int((START + datetime.timedelta(days=i)).timestamp()) for i in range(len(closes))]
        result.append({"symbol": sym, "response": [{
            "meta": {"symbol": sym, "regularMarketPrice": closes[-1], "regularMarketVolume": 2000000},
            "timestamp": t, "indicators": {"quote": [{"close": closes}]}}]})
    return {"spark": {"result": result, "error": None}}


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.fix = os.path.join(self.d, "spark.json")
        with open(self.fix, "w") as fh:
            json.dump(spark_doc({"UP": trend(0.001), "DOWN": trend(-0.002)}), fh)

    def tearDown(self):
        shutil.rmtree(self.d)

    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = lt.main(args + ["--offline", self.fix, "--now", "2026-11-24 17:00"])
        return code, out.getvalue(), err.getvalue()

    def test_run_lists_only_passers_and_writes_the_file(self):
        out_file = os.path.join(self.d, "trader", "longterm.json")
        code, out, _ = self.run_cli(["run", "--symbols", "UP,DOWN", "--out", out_file])
        self.assertEqual(code, 0)
        with open(out_file) as fh:
            doc = json.load(fh)
        self.assertEqual([r["symbol"] for r in doc["rows"]], ["UP"])
        self.assertEqual(doc["created_by"], "trader-longterm")
        self.assertIn("UP", out)
        self.assertNotIn("DOWN", out)
        self.assertIn("not financial advice", out)

    def test_check_shows_every_ticker_with_each_rule(self):
        code, out, _ = self.run_cli(["check", "UP", "DOWN", "ZZZ"])
        self.assertEqual(code, 0)
        self.assertIn("above 200-day average: FAIL", out)
        self.assertIn("above 200-day average: pass", out)
        self.assertIn("no usable data", out)
        self.assertIn("ZZZ", out)

    def test_output_never_gives_advice_words(self):
        _, out, _ = self.run_cli(["check", "UP", "DOWN"])
        low = out.lower().replace("not financial advice", "")
        for word in ("buy", "sell", "promising", "recommend"):
            self.assertNotIn(word, low, word)

    def test_nothing_passing_exits_1(self):
        code, _, _ = self.run_cli(["run", "--symbols", "DOWN", "--no-write"])
        self.assertEqual(code, 1)

    def test_fundamentals_need_a_user_agent(self):
        code, _, err = self.run_cli(["run", "--symbols", "UP", "--fundamentals", "--no-write",
                                     "--config", os.path.join(self.d, "none.json")])
        self.assertEqual(code, 2)
        self.assertIn("User-Agent", err)


if __name__ == "__main__":
    unittest.main()
