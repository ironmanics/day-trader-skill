#!/usr/bin/env python3
"""Long-term screen for trader-longterm. Standard library only.

Applies fixed, published price rules to two years of daily closes and lists
the stocks that pass, with every number shown. It reuses trader-scanner's
universe, Yahoo fetching and parsing, and (optionally) trader-analyst's SEC
reader, so both folders must sit next to this one.

    python3 screen.py run                                   # the whole universe (scan.py universe first)
    python3 screen.py run --symbols AAPL,KO,BAC
    python3 screen.py run --fundamentals                    # also: revenue up and operating margin positive
    python3 screen.py check AAL IONQ RGTI                   # every rule, pass or fail, for tickers you name

The rules (all must hold; thresholds are flags):
  trend      last close above the 200-day average, and that average higher than 20 days ago
  momentum   12-1 return above 0: the close 21 trading days ago against the close 252 days ago
             (the last month is skipped on purpose, the usual way this measure is built)
  near high  last close within 25 % of the 52-week high
  drawdown   worst peak-to-trough fall over the last 126 trading days no deeper than 40 %
Volatility (annualised, one year) is shown, never filtered.

Numbers only: this tool never says whether a stock is a buy.
"""

import argparse
import importlib.util
import json
import math
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MIN_BARS = 253          # 252 days back from the close 21 days ago needs 253 closes
NOTE = ("A screen lists stocks that matched past price rules. Past price behaviour does not predict "
        "returns, and this is not financial advice.")


class ScreenError(Exception):
    pass


def _sibling(folder, name):
    path = os.path.join(os.path.dirname(HERE), folder, name + ".py")
    if not os.path.isfile(path):
        raise ScreenError("needs %s next to this folder (looked for %s)" % (folder, path))
    spec = importlib.util.spec_from_file_location("trader_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


scan = _sibling("trader-scanner", "scan")


# ---------------------------------------------------------------- the numbers

def metrics(bars):
    """[(date, close)] oldest first -> the long-term numbers, or None with under MIN_BARS closes."""
    c = [x for _, x in bars if x and x > 0]
    if len(c) < MIN_BARS:
        return None
    last = c[-1]
    sma = sum(c[-200:]) / 200
    sma_before = sum(c[-220:-20]) / 200
    peak, worst = c[-126], 0.0
    for x in c[-126:]:
        peak = max(peak, x)
        worst = min(worst, x / peak - 1)
    rets = [math.log(c[i] / c[i - 1]) for i in range(len(c) - 251, len(c))]
    return {"price": last, "sma200": sma, "sma200_rising": sma > sma_before,
            "mom_12_1": c[-22] / c[-253] - 1, "off_high": last / max(c[-252:]) - 1,
            "max_dd_6m": worst, "vol_1y": statistics.stdev(rets) * math.sqrt(252)}


def rule_results(m, r):
    """Each rule -> True or False. r holds the thresholds as fractions."""
    return {"above 200-day average": m["price"] > m["sma200"],
            "200-day average rising": m["sma200_rising"],
            "12-1 momentum": m["mom_12_1"] > r["min_momentum"],
            "near 52-week high": m["off_high"] >= -r["max_off_high"],
            "6-month drawdown": m["max_dd_6m"] >= -r["max_drawdown"]}


def screen_prices(data, symbols, now_utc, r, min_price, min_dollar_volume):
    """Pure. data = {SYM: series}. Returns (rows, counts, last_date); every scanned stock is a row."""
    rows, dates = [], {}
    counts = {"requested": len(symbols), "fetched": 0, "illiquid": 0, "short": 0, "scanned": 0}
    for sym in symbols:
        s = data.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = scan.daily_closes(s, now_utc)
        meta = s.get("meta") or {}
        price = meta.get("regularMarketPrice")
        if price is None and bars:
            price = bars[-1][1]
        vol = meta.get("regularMarketVolume")
        # shortcut: one session's volume stands in for average volume, upgrade if Yahoo adds it to spark
        dollars = price * vol if price is not None and vol is not None else None
        if price is None or price < min_price or (min_dollar_volume > 0 and (dollars or 0) < min_dollar_volume):
            counts["illiquid"] += 1
            continue
        m = metrics(bars)
        if m is None:
            counts["short"] += 1
            continue
        counts["scanned"] += 1
        d_last = bars[-1][0]
        dates[d_last] = dates.get(d_last, 0) + 1
        rules = rule_results(m, r)
        rows.append({"symbol": sym, "close_date": d_last.isoformat(), "price": round(m["price"], 2),
                     "mom_12_1_pct": round(100 * m["mom_12_1"], 1), "off_high_pct": round(100 * m["off_high"], 1),
                     "max_dd_6m_pct": round(100 * m["max_dd_6m"], 1), "vol_1y_pct": round(100 * m["vol_1y"], 1),
                     "vs_sma200_pct": round(100 * (m["price"] / m["sma200"] - 1), 1),
                     "dollar_volume": int(dollars) if dollars is not None else None,
                     "rules": rules, "passed": all(rules.values())})
    last_date = max(dates, key=lambda d: (dates[d], d)) if dates else None
    counts["passed"] = sum(1 for x in rows if x["passed"])
    return rows, counts, last_date


# ---------------------------------------------------------------- optional SEC pass

def fundamentals(facts, symbols, ua=None, offline=None, budget=None):
    """{SYM: {revenue_growth_yoy, operating_margin, ..., pass}}; pass is None when SEC has no figure.
    budget caps live SEC requests; cached answers are free. Stocks the budget or a SEC block
    cut off are marked deferred, and the next run picks them up from the cache."""
    guard = facts.sec_guard.Guard(budget=budget) if budget else None
    f = facts.Fetcher(ua=ua, offline=offline, guard=guard)
    try:
        ciks = facts.ticker_ciks(f)
    except facts.FactsError as e:
        raise ScreenError(str(e))
    out = {}
    for i, s in enumerate(symbols):
        cik, name = ciks.get(s.replace("-", "."), (None, None))
        if not cik:
            out[s] = {"error": "no SEC filer found (an ETF or foreign issuer?)", "pass": None}
            continue
        try:
            cf = f.json(facts.FACTS_URL % cik, "companyfacts-CIK%s.json" % cik, cache=True)
            row = facts.metrics_row(s, facts.analyse(cf, quarters=6, name=name))
        except facts.SecStopped as e:
            for rest in symbols[i:]:
                out[rest] = {"error": str(e), "deferred": True, "pass": None}
            break
        except facts.FactsError as e:
            out[s] = {"error": str(e), "pass": None}
            continue
        g, om = row["revenue_growth_yoy"], row["operating_margin"]
        out[s] = {"revenue_growth_yoy": g, "operating_margin": om, "period_end": row["period_end"],
                  "pass": None if g is None or om is None else (g > 0 and om > 0)}
    return out


# ---------------------------------------------------------------- commands

def run(a):
    t0 = time.time()
    now = scan.parse_now(a.now)
    offline_syms = None
    if a.offline:
        data, failed = scan.load_offline(a.offline)
        offline_syms = sorted(set(data) | set(failed))
    if a.cmd == "check":
        a.symbols, a.watchlist, a.limit, a.universe = ",".join(a.tickers), None, None, None
        a.min_price, a.min_dollar_volume = 0.0, 0.0
    symbols = scan.read_symbols(a, offline_syms)
    facts = ua = None
    if a.fundamentals:
        facts = _sibling("trader-analyst", "facts")
        ua = facts.user_agent(a.user_agent, a.config)
        if not ua and not a.facts_offline:
            raise ScreenError(facts.UA_HELP)
    if not a.offline:
        scan.progress(a, "Fetching two years of daily closes for %s symbols (%d spark requests)..."
                      % ("{:,}".format(len(symbols)), (len(symbols) + scan.SPARK_MAX - 1) // scan.SPARK_MAX))
        data, failed = scan.fetch_spark(symbols, "2y", "1d", a.pause, a)
    r = {"min_momentum": a.min_momentum / 100.0, "max_off_high": a.max_off_high / 100.0,
         "max_drawdown": a.max_drawdown / 100.0}
    rows, counts, last_date = screen_prices(data, symbols, now, r, a.min_price, a.min_dollar_volume)
    check = a.cmd == "check"
    if facts:
        need = [x["symbol"] for x in rows if check or x["passed"]]
        scan.progress(a, "Reading SEC figures for %d stocks..." % len(need))
        funds = fundamentals(facts, need, ua, a.facts_offline, a.sec_budget)
        for x in rows:
            if x["symbol"] in funds:
                x["fundamentals"] = funds[x["symbol"]]
    listed = rows if check else sorted((x for x in rows if x["passed"]), key=lambda x: (-x["mom_12_1_pct"], x["symbol"]))
    if facts and not check:
        fl = [x["fundamentals"] for x in listed]
        counts["sec_deferred"] = sum(1 for g in fl if g.get("deferred"))
        counts["no_sec_figures"] = sum(1 for g in fl if g["pass"] is None and not g.get("deferred"))
        listed = [x for x in listed if x["fundamentals"]["pass"]]
    counts["listed"] = len(listed)
    doc = {"created_by": "trader-longterm", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": a.cmd,
           "as_of": ("%s close" % last_date.isoformat()) if last_date else None, "source": "yahoo",
           "rules": {"min_momentum_pct": a.min_momentum, "max_off_high_pct": a.max_off_high,
                     "max_drawdown_pct": a.max_drawdown, "min_price": a.min_price,
                     "min_dollar_volume": a.min_dollar_volume, "fundamentals": bool(facts)},
           "counts": counts, "note": NOTE, "rows": listed}
    if a.cmd == "run" and not a.no_write:
        folder = os.path.dirname(a.out)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
    if a.json:
        print(json.dumps(dict(doc, seconds=round(time.time() - t0, 1)), indent=2))
    else:
        render(a, doc, last_date, symbols, 0 if check else len(symbols) - counts["fetched"])
    return 0 if listed else 1


def _fund_cell(x):
    f = x.get("fundamentals")
    if f is None:
        return ""
    if "error" in f:
        return "  SEC: n/a"
    g, om = f.get("revenue_growth_yoy"), f.get("operating_margin")
    verdict = "" if f["pass"] is None else ("  ok" if f["pass"] else "  fails")
    return "  SEC: revenue %s, operating margin %s%s" % (
        "n/a" if g is None else "%+.1f%%" % g, "n/a" if om is None else "%.1f%%" % om, verdict)


def render(a, doc, last_date, symbols, nfailed):
    rl, counts = doc["rules"], doc["counts"]
    print("Long-term screen, closes through %s (Yahoo, two years of daily closes)." % (
        last_date.isoformat() if last_date else "nothing"))
    print("Fetched %s of %s charts%s; %s scanned%s." % (
        "{:,}".format(counts["fetched"]), "{:,}".format(len(symbols)),
        (" (%s failed or had no data)" % "{:,}".format(nfailed)) if nfailed else "",
        "{:,}".format(counts["scanned"]),
        (", %d illiquid, %d with under %d closes" % (counts["illiquid"], counts["short"], MIN_BARS))
        if counts["illiquid"] or counts["short"] else ""))
    if a.cmd == "run":
        print("Rules (all must hold): above a rising 200-day average; 12-1 momentum above %g%%; "
              "within %g%% of the 52-week high; 6-month drawdown no worse than -%g%%; price $%g+ and "
              "$%s+ traded in the last session." % (rl["min_momentum_pct"], rl["max_off_high_pct"],
                                                     rl["max_drawdown_pct"], rl["min_price"],
                                                     "{:,.0f}".format(rl["min_dollar_volume"])))
        extra = (", %d of those pass the SEC check (%d had no SEC figures, %d deferred: run again to continue)" % (
            counts["listed"], counts["no_sec_figures"], counts["sec_deferred"])) if rl["fundamentals"] else ""
        print("%d passed the price rules%s. Highest 12-1 momentum first:" % (counts["passed"], extra))
    for x in doc["rows"][:a.show]:
        print("  %-6s $%9.2f  12-1 %+7.1f%%  vs 52wk high %6.1f%%  6mo drawdown %6.1f%%  vol %5.1f%%  vs 200d %+6.1f%%%s" % (
            x["symbol"], x["price"], x["mom_12_1_pct"], x["off_high_pct"], x["max_dd_6m_pct"],
            x["vol_1y_pct"], x["vs_sma200_pct"], _fund_cell(x)))
        if a.cmd == "check":
            print("         " + ", ".join("%s: %s" % (k, "pass" if v else "FAIL") for k, v in x["rules"].items()))
    if a.cmd == "check":
        have = {x["symbol"] for x in doc["rows"]}
        missing = [s for s in symbols if s not in have]
        if missing:
            print("  no usable data (under %d closes or not found): %s" % (MIN_BARS, ", ".join(missing)))
    if len(doc["rows"]) > a.show:
        print("  ... and %d more in the file (--show N for more)." % (len(doc["rows"]) - a.show))
    if a.cmd == "run" and not a.no_write:
        print("Wrote %s (%d stock%s)." % (a.out, len(doc["rows"]), "" if len(doc["rows"]) == 1 else "s"))
    print(NOTE)


def _parser():
    ap = argparse.ArgumentParser(description="Long-term screen (trader-longterm): fixed price rules on two years of daily closes.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--min-price", type=float, default=5.0)
        p.add_argument("--min-dollar-volume", type=float, default=5e6)
        p.add_argument("--min-momentum", type=float, default=0.0, help="12-1 return must exceed this, percent (0)")
        p.add_argument("--max-off-high", type=float, default=25.0, help="percent below the 52-week high (25)")
        p.add_argument("--max-drawdown", type=float, default=40.0, help="worst 6-month peak-to-trough fall, percent (40)")
        p.add_argument("--fundamentals", action="store_true", help="SEC pass: revenue up year on year, operating margin positive")
        p.add_argument("--sec-budget", type=int, default=300,
                       help="live SEC requests per run, 0 for no cap (300); cached answers are free")
        p.add_argument("--user-agent", help='SEC User-Agent: "Your Name you@example.com"')
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--facts-offline", metavar="DIR")
        p.add_argument("--offline", metavar="PATH", help="fixture folder or file instead of the network")
        p.add_argument("--now", help='pretend it is this time ("2026-10-19 16:30" New York, or ...Z)')
        p.add_argument("--pause", type=float, default=scan.PAUSE)
        p.add_argument("--show", type=int, default=50, help="rows to print (all are written)")
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("run", help="screen a symbol list")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--symbols", help="comma-separated tickers instead of the universe")
    g.add_argument("--watchlist", metavar="FILE", help="a watchlist.json (hits) or a text file of tickers")
    p.add_argument("--universe", help="symbol list, one SYMBOL|Name a line (default %s)" % scan.DEFAULT_UNIVERSE)
    p.add_argument("--limit", type=int, help="screen only the first N symbols")
    p.add_argument("--out", default="trader/longterm.json")
    p.add_argument("--no-write", action="store_true")
    common(p)
    p = sub.add_parser("check", help="every rule, pass or fail, for tickers you name (no liquidity filter)")
    p.add_argument("tickers", nargs="+")
    common(p)
    return ap


def main(argv=None):
    a = _parser().parse_args(argv)
    if not a.cmd:
        _parser().print_help()
        return 2
    try:
        return run(a)
    except (ScreenError, scan.ScanError) as e:
        print("longterm: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
