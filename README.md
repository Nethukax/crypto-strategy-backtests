# Testing crypto trading strategies honestly

[![tests](https://github.com/Nethukax/crypto-strategy-backtests/actions/workflows/tests.yml/badge.svg)](https://github.com/Nethukax/crypto-strategy-backtests/actions/workflows/tests.yml)

I wanted a bot that trades crypto with real money using the ICT / "smart money"
concepts popular on YouTube: **AMD (Accumulation, Manipulation, Distribution)**,
liquidity sweeps, Fair Value Gaps, support/resistance, volume profile POC.

So I built the bot, plus the tools to check whether it actually works before
risking money. Then I tested a second, completely different strategy (trend following).

**Result: neither strategy passed. Neither is traded with real money.**
This repo is the full pipeline and an honest write-up of what the data showed.

---

## TL;DR

| Strategy | Test | Result |
|---|---|---|
| AMD / smart money (15m) | 8 coins it was never tuned on, 2 years, 369 trades | **Loses**: profit factor 0.88, −27.8R |
| AMD loss post-mortem | 283 losing trades on 10 fresh coins | 72% were simply the wrong direction; no fix or feature beat noise |
| Trend following (4h) | 11 coins, 4 years, 914 trades, no tuning | Weak edge: profit factor 1.26 (bar was 1.3) |
| Trend following, all coins in one account | 6 years (2020–2026) | +54% total, 7.5%/yr, −36% drawdown, 810 days underwater |
| Buy & hold BTC | same 6 years | **+621%**, −77% drawdown |

Over this period, simply holding BTC beat every bot here.

---

## What's in the repo

| File | What it does |
|---|---|
| `concepts.py` | ATR, Fair Value Gaps, swing highs/lows, S/R clustering, volume profile + POC, engulfing candles, higher-timeframe bias |
| `strategy.py` | The AMD strategy as a bar-by-bar state machine. **The same code runs in backtests and live.** |
| `backtest.py` | Conservative simulator: fees, slippage, stop assumed hit first if stop and target share a candle, gaps fill at the open, realistic limit-order fills |
| `sweep.py` | Tests many settings on the first 70% of data, then checks them on the unseen last 30% |
| `coins.py` | Runs one frozen set of rules on coins that never influenced any setting |
| `trend.py` | Donchian breakout + ATR trailing stop, rules fixed before seeing any results |
| `portfolio.py` | All coins in one account, overlapping positions, compared with buy & hold |
| `postmortem.py` | Why each losing trade lost: what the bot expected vs what happened, loss causes, and whether a fix would help **all** trades |
| `live.py` | Exchange execution via ccxt (testnet by default) with safety rails |
| `data.py` | Downloads candles from the exchange's public API, with caching and retries |

### Live trading safety (`live.py`)
- Runs on **testnet** unless `live = True` in `config.py` **and** the env var `I_ACCEPT_REAL_MONEY_RISK=YES` is set
- Every position gets an exchange-side stop-loss; if the stop can't be placed, the position is closed immediately
- Checks every 15 seconds for any position without a stop
- Daily loss limit (−3%), max trades per day, leverage cap, flat by session close
- API keys only from environment variables, never in code
- Tested against a fake exchange: limit fill, rejected stop-loss, unknown position, expired order

---

## Method: how I tried not to fool myself

Backtests are easy to make look amazing. The usual ways people fool themselves, and what I did instead:

1. **Lookahead bias**: using future data by accident. Every signal is re-derived using *only* data up to that moment and must match. Swing points only become visible after they're confirmed, and a 1h candle is only used after it closes.
2. **Overfitting**: tuning until the past looks good. Settings are chosen on the first 70% of the data and judged on the last 30%.
3. **Overfitting the test set**: after looking at the out-of-sample results several times, they stop being clean. So the final test used **coins that never influenced any decision**.
4. **Moving the goalposts**: every pass/fail bar was written in the code **before** running the test, and never changed after seeing results.
5. **Ignoring costs**: taker/maker fees, slippage, and perpetual funding are all included.

---

## Results

### 1. AMD / smart money concepts (15m candles)

**Rules:** Asian session (00:00–07:00 UTC) range = accumulation. A wick beyond the range that closes back inside during London = manipulation (liquidity sweep). Enter on the displacement candle that leaves a Fair Value Gap; target the other side of the range = distribution. Plus volume, POC, S/R and engulfing-candle confluence.

- **BTC, 1 year:** 21 trades, profit factor 0.33, −9.1%.
- **TAO, 24 variants** (15m/1h, cost filters, confluence, breakouts): the best in-sample profit factor was 1.00. Nothing made money.
- **TAO, 36 variants with a 1h direction filter** (only trade with the 1h trend):
  the filter improved **every** variant compared with no filter, which is the one robust finding.
  But the top in-sample row (PF 1.72) collapsed to 0.50 out-of-sample: classic overfitting.
  The most balanced variant was PF 1.34 in-sample and 1.02 out-of-sample, which is break-even.
- **Final test, frozen rules on 8 fresh coins (2 years):**

| Coin | Trades | PF | Total R |
|---|---|---|---|
| BTC | 18 | 1.03 | +0.3 |
| ETH | 39 | 0.85 | −3.9 |
| XRP | 49 | 1.03 | +1.0 |
| BNB | 29 | 1.67 | +8.8 |
| AVAX | 60 | 0.79 | −7.8 |
| LINK | 57 | 0.50 | −21.4 |
| ADA | 61 | 1.00 | +0.1 |
| NEAR | 56 | 0.86 | −4.9 |
| **All** | **369** | **0.88** | **−27.8** |
| *TAO (tuned on)* | *57* | *1.23* | *+7.4* |

TAO, the coin it was tuned on, looks profitable. Coins it never saw lose. That gap is what overfitting looks like.

**Verdict: no edge.**

#### Post-mortem: can the bot learn from its losses?

The natural next idea: after every loss, look at what the bot expected, what actually happened,
and learn from the mistake. `postmortem.py` does exactly that for all 470 trades on the 10 fresh coins
(SOL and DOGE included this time): 40% win rate, −0.05R per trade.

**Why the 283 losses happened:**

| Cause | Share |
|---|---|
| Stopped out and price **never** reached the target afterwards (wrong direction) | 72% |
| Still losing when the session closed | 11% |
| Stopped out, then price hit the target later that session | 8% |
| Got 75%+ of the way to target, then reversed | 7% |
| Went straight against the trade | 2% |

**Would a fix have helped?** Replayed on *all* trades, winners included (before fees):

| Change | Avg R per trade |
|---|---|
| Current rules | +0.052 |
| Stop 1.5x wider | +0.004 |
| Stop 2x wider | +0.018 |
| Target halfway | +0.082 |

The best fix adds +0.03R, far less than the 0.1–0.2R that fees cost per trade.

**Does any feature separate winners from losers?** Side, time of day, target size, confluence score,
volume spike, POC, liquidity pool, engulfing candle: **none** differed from the average by more than
random noise would produce. If anything, more confirmation looked *worse*: trades with all 3 confluence
signals won 29% of the time (−0.30R), trades with none +0.11R.

One loss shows how random it is: an ETH short reached +1.40R of a +1.41R target, missed by 0.01R,
then reversed to the stop.

**Conclusion:** 72% of losses were simply the wrong direction, and there is no repeatable pattern in them.
A bot that adjusts itself after every loss would be chasing noise. That's exactly the overfitting
the earlier tests caught, just done automatically and live.

### 2. Trend following (4h candles)

**Rules, fixed in advance:** go long when a candle closes above the 20-day high, short below the 20-day low. Stop at 3 ATR, trailing behind the best close. No profit target. 1% risk per trade. Nothing tuned.

**11 coins, 4 years:** 914 trades, profit factor 1.26, +0.12R per trade, profitable on 9/11 coins, positive in both halves of the period.
Longs made +106.7R; shorts made −1.3R.
It failed the pre-set bar (PF ≥ 1.3) narrowly.

**All coins in one account:**

| | 4 years (2022–2026) | 6 years (2020–2026) |
|---|---|---|
| Total return | +98% | +54% |
| Per year | +18.7% | +7.5% |
| Worst drawdown | −36.2% | −36.2% |
| Longest below a previous high | 292 days | 810 days |
| Buy & hold BTC | +327% (−53% drawdown) | +621% (−77% drawdown) |

By year: 2020 +1%, 2021 **−19%**, 2022 **−6%**, 2023 +55%, 2024 +4%, 2025 −6%, 2026 +31%.

![Trend portfolio vs buy & hold](portfolio.png)

The 4-year result looked borderline-promising. Adding 2020–2022 killed it: the strategy lost money in the 2021 bull run **and** in the 2022 crash it was supposed to protect against.

**Verdict: not worth trading.** Drawdowns were shallower than holding, but it gave up almost all the upside to get that.

---

## Lessons

- **Concepts that look obvious on a chart are hard to make profitable as rules.** Many "setups" are only clear in hindsight.
- **One good backtest means nothing.** The AMD bot's best variant went from PF 1.72 to 0.50 on unseen data.
- **Test on data that never influenced a decision.** Untouched coins and untouched years were what actually decided things.
- **Write the pass/fail bar before you look.** Otherwise "1.26 is basically 1.3" is very tempting.
- **Fees matter most on short timeframes.** On 15m charts the stops are so tight that fees and slippage eat a big share of each trade's risk, so the bot had to skip most BTC setups.
- **Compare with doing nothing.** A bot that makes +54% sounds good until buy & hold made +621%.
- **Losses aren't always mistakes.** The post-mortem found no repeatable pattern in 283 losses. "Learning" from each one would just fit noise.
- **Test your tests.** My first lookahead check was broken: it silently tested zero signals and reported "all match". I only caught it because a later run returned 0 signals where I expected some.

---

## Tests

31 automated tests run on every push (GitHub Actions, Python 3.11 and 3.13):

| File | What it guards |
|---|---|
| `tests/test_no_lookahead.py` | Signals, 1h bias and trend entries are identical when recomputed with only the data available at the time |
| `tests/test_backtest.py` | Fill rules on hand-made candles: stop-first when stop and target share a candle, gaps, limit orders, session close, 1% sizing |
| `tests/test_trend.py` | No edge on random data, clear edge on trending data, portfolio matches single-coin trades exactly |
| `tests/test_live.py` | Real-money path against a fake exchange: refuses without the env var, rejected stop closes the position, unknown positions closed |
| `tests/test_concepts.py` | FVG, swing confirmation delay, S/R clustering, volume profile POC |
| `tests/test_postmortem.py` | The post-mortem's trade replay follows the backtester's rules |

The tests were checked by planting bugs on purpose (an FVG that peeks at the next candle, a 1h bias that uses an unfinished candle,
a channel that includes the current candle, target checked before stop). The suite caught all four.

```bash
pip install pytest
pytest
```

---

## Run it yourself

```bash
pip install -r requirements.txt

python run.py backtest --days 365      # AMD backtest (Bybit public data, no API key)
python run.py signals --days 30        # every AMD setup with the reasons it fired
python sweep.py --days 730             # AMD variants: in-sample vs out-of-sample
python coins.py --days 730             # AMD frozen rules on fresh coins
python trend.py                        # trend following, 11 coins
python portfolio.py --days 2190        # trend following, one account, vs buy & hold
python postmortem.py                   # AMD loss post-mortem on the fresh coins
```

Add `--exchange binanceusdm` to use Binance futures data, or `--synthetic` to test the code offline with fake data.

Live / testnet (not recommended with real money, see results above):
```bash
export EXCHANGE_API_KEY=...  EXCHANGE_API_SECRET=...   # testnet keys, trading permission only
python run.py live
```

---

*Not financial advice. Backtests can't predict the future, and crypto derivatives can lose more than you put in.*
