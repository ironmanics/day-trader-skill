---
name: trader-longterm
description: >-
  Long-Term Screen. A neutral, rules-based screen for holding periods of
  months to years, not day trades. Reads two years of free Yahoo daily
  closes for US-listed common stocks (the universe trader-scanner builds)
  and lists only the stocks that pass fixed, published rules: last close
  above a rising 200-day average, 12-1 momentum above zero (the close 21
  trading days ago against 252 days ago), within 25 % of the 52-week high,
  and a 6-month drawdown no deeper than 40 %. Shows volatility, never
  filters on it. An optional SEC pass keeps only stocks with revenue up
  year on year and a positive operating margin. The check command scores
  tickers you name against every rule, pass or fail. Numbers only, never a
  buy, sell or ranking of what is promising. Use when the user says "long
  term screen", "screen for long-term holds", "check my holdings against the
  rules", "which stocks are in an uptrend", "12-1 momentum", "above the
  200-day", "how do AAL, IONQ and RGTI score", or wants a rules-based list
  that is not a day-trading setup.
argument-hint: "[run | check TICKERS] [--fundamentals]"
---

# trader-longterm

The Long-Term Screen answers one narrow question: which stocks pass a fixed
set of trend and drawdown rules, measured over two years of daily closes. It
is the slow-timeframe sibling of trader-scanner. It does not time an entry,
set a stop, size a position or place an order. It is a screen, not a
recommendation, and it is not financial advice.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).
It needs `trader-scanner` and `trader-analyst` installed beside it.

```bash
python3 "${CLAUDE_SKILL_DIR}/screen.py" run                          # the whole universe, about 5 minutes
python3 "${CLAUDE_SKILL_DIR}/screen.py" run --symbols AAPL,KO,BAC
python3 "${CLAUDE_SKILL_DIR}/screen.py" run --fundamentals           # also the SEC revenue and margin check
python3 "${CLAUDE_SKILL_DIR}/screen.py" check AAL IONQ RGTI          # every rule, pass or fail
```

The universe comes from `python3 .../trader-scanner/scan.py universe`, which
writes `trader/universe.txt`. Run that first if the file is missing.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/universe.txt` | the symbol list from trader-scanner (`run` only) |
| Yahoo spark API | two years of daily closes, 20 symbols a request, free and unofficial |
| SEC EDGAR | with `--fundamentals`: company facts through trader-analyst, one stock at a time |
| `trader/longterm.json` | with `run`: `{created_by, created_at, as_of, rules, counts, note, rows}`, one row per stock listed |

## The rules

All of them must hold. The thresholds are flags, and the output prints the
ones used.

| rule | default | flag |
| --- | --- | --- |
| above the 200-day average | last close above it | none |
| 200-day average rising | higher than 20 trading days ago | none |
| 12-1 momentum | close 21 days ago over close 252 days ago, above 0 % | `--min-momentum` |
| near the 52-week high | within 25 % of it | `--max-off-high` |
| 6-month drawdown | worst peak-to-trough fall no deeper than 40 % | `--max-drawdown` |
| liquidity | price $5 or more and $5 million or more traded in the last session | `--min-price`, `--min-dollar-volume` |

Volatility (annualised, one year) and the distance from the 200-day average
are shown for context and never filter. A stock needs 253 daily closes to be
scored, so recent listings are left out and counted.

The SEC pass (`--fundamentals`) looks only at stocks that passed the price
rules. It keeps those whose latest quarter shows revenue up on the same
quarter a year earlier and a positive operating margin. Banks, insurers,
REITs and ETFs often have no usable figure; they are dropped and counted as
"no SEC figures", not called failures.

## Steps

1. If `trader/universe.txt` is missing, run `trader-scanner/scan.py universe`.
2. Run `screen.py run` (add `--fundamentals` if the user wants the SEC pass).
   It takes about five minutes for the full universe. Say so before starting.
3. Show the user the counts line, the rules line and the table exactly as
   printed, highest 12-1 momentum first.
4. For `check`, show each ticker's numbers and which rules passed or failed.
   A failed rule is a fact about the past two years, nothing more.
5. Say once that this is a rules screen: it does not predict returns, and
   free Yahoo data is unofficial and can be wrong or late.

## Rules for Claude

- Repeat the tool's output. Do not add "promising", "top picks", "worth
  buying", "cheap" or any ranking of your own. The only order is the printed
  one, by 12-1 momentum.
- Never tell the user to buy, sell, hold or switch a position, and never name
  a stock as a better home for money. If asked, say you are not a licensed
  advisor and the decision is theirs.
- Never describe a pass as a good sign or a fail as a bad one for the future.
- Never loosen a threshold to make a stock pass. Say the flag and the
  user decides.
- Every number comes from the run. Always say the data date ("closes through").

## What it never does

It never places or sizes an order, never sets a stop or target, never reads
news, never judges a company's quality or price, and never says whether a
stock will go up. Taxes are not modelled: selling at a loss has tax rules
(including the wash-sale rule) this tool knows nothing about.

## Safety

- Education and tooling, not financial advice. A pass is a rules check, not a recommendation.
- Past price behaviour does not predict returns. Screens like this pass stocks that later fall.
- Free Yahoo prices are unofficial, can lag, and are split-adjusted but not dividend-adjusted.
- The liquidity check uses one session's volume, not an average.
- Nothing in this pack places an order except trader-broker, and only when the user types an explicit yes for that one order and clicks Allow once on Claude's own card.
