# Build Guide: how and why everything was built

This guide explains the whole project: the business problem, every table, every module, every design decision and the reasoning behind it, the bugs found along the way, and how the accuracy numbers are produced. Read it top to bottom once and you should be able to rebuild the project from memory.

Contents

1. [The business problem](#1-the-business-problem)
2. [Architecture at a glance](#2-architecture-at-a-glance)
3. [Repository tour](#3-repository-tour)
4. [Data model](#4-data-model)
5. [Synthetic data generation](#5-synthetic-data-generation)
6. [The matching engine](#6-the-matching-engine)
7. [Break types, severity and owners](#7-break-types-severity-and-owners)
8. [Running across days: population, lifecycle, ageing](#8-running-across-days-population-lifecycle-ageing)
9. [SQL reporting views](#9-sql-reporting-views)
10. [The daily report](#10-the-daily-report)
11. [Measuring accuracy](#11-measuring-accuracy)
12. [Build log: what went wrong and how it was fixed](#12-build-log-what-went-wrong-and-how-it-was-fixed)
13. [The tolerance sweep](#13-the-tolerance-sweep)
14. [Testing](#14-testing)
15. [Limitations and what I would do next](#15-limitations-and-what-i-would-do-next)
16. [Running, configuring and extending](#16-running-configuring-and-extending)
17. [Glossary](#17-glossary)

---

## 1. The business problem

When a trader buys 1,000 Apple shares, two records of that trade come into existence:

- **The internal booking**: the firm's own record in its trade blotter (order management or booking system). It says what *we* think we did.
- **The broker's confirmation**: the executing broker sends its own record (a confirm, or an end-of-day statement). It says what *they* think we did.

These must agree on every economic detail (instrument, side, quantity, price, currency, settlement date) **before settlement**, because settlement moves real cash and securities. If our record says BUY 1,000 and the broker says BUY 100, then on settlement day either we receive fewer shares than our books show, or the trade fails. Since US equities moved to **T+1 settlement** (May 2024), there is roughly one business day to find and fix a disagreement.

A disagreement is called a **break**. Finding breaks every morning, working out what kind each one is, and getting it to the right person is the core daily job of **Trade Support** and **Settlements** teams. This tool automates that job.

What makes it hard in practice (and in this project):
- The broker's copy of our trade ID is unreliable. It is sometimes blank and sometimes mistyped, so a naive join on trade_id misses trades.
- Prices legitimately differ by tiny amounts (rounding, average-price calculation), so exact equality is wrong. You need a tolerance.
- Breaks don't vanish overnight. Some are fixed the next day, some linger for a week, so you need to track **age**.
- The same trade can be sent twice, and the broker can AMEND or CANCEL records on later statements.

## 2. Architecture at a glance

```
                    ┌────────────────────┐
                    │ data/instruments.csv│  25 real tickers, base prices
                    └─────────┬──────────┘
                              ▼
                    ┌────────────────────┐
                    │   generate.py      │  seeded RNG, 20 business days
                    └─┬───────┬────────┬─┘
                      ▼       ▼        ▼
          internal_trades  broker_trades  answer_key        (PostgreSQL tables)
                      │       │
                      ▼       ▼
                    ┌────────────────────┐
                    │   pipeline.py      │  for each day, in order:
                    │  select_population │   which trades to compare today
                    │  matcher.reconcile │   6 passes -> matches + raw breaks
                    │  breaks.score      │   severity, owner, break_key
                    │  apply_lifecycle   │   carry / resolve / age
                    └─┬───────┬────────┬─┘
                      ▼       ▼        ▼
               recon_runs  match_results  breaks             (PostgreSQL tables)
                      │
                      ▼
                 sql/views.sql  ──► report.py   ──► reports/break_report_<date>.md + PNGs
                                ──► accuracy.py ──► reports/accuracy.md (joins answer_key)
```

**Key design choice: the matching logic is pure pandas, separate from the database.** `matcher.reconcile()` takes two DataFrames and returns DataFrames. It never touches PostgreSQL. That means:
- the tests run in milliseconds with hand-made trades and no database;
- the tolerance sweep can rerun the whole 20 days seven times in memory without changing stored results;
- PostgreSQL does what it is good at: storing results durably and answering reporting questions with SQL views.

## 3. Repository tour

| Path | What it is |
|---|---|
| `docker-compose.yml` | PostgreSQL 16 on host port **5433** (avoids clashing with a local Postgres on 5432). Volume `recon_pgdata` keeps data between restarts. |
| `sql/schema.sql` | All six tables, with CHECK constraints. Re-runnable (drops first). |
| `sql/views.sql` | Seven reporting views and two accuracy views. |
| `data/instruments.csv` | 25 tickers: 20 US (USD, T+1), 5 European (GBP/EUR, T+2), with base price and a popularity weight. |
| `recon/config.py` | **Every tunable number**: seed, dates, trade count, error rate and mix, ID noise, price noise, resolution delays, tolerance, severity threshold, DB URL. |
| `recon/generate.py` | Synthetic data and the answer key. |
| `recon/matcher.py` | The six-pass matching engine. |
| `recon/breaks.py` | Severity, owner, break keys, lifecycle. |
| `recon/pipeline.py` | Runs the days in order; decides each day's population. |
| `recon/db.py` | Connect, run SQL files, bulk insert, read query results. |
| `recon/report.py` | The daily Markdown report and two charts, built from views only. |
| `recon/accuracy.py` | Precision, recall and lifecycle accuracy against the answer key. |
| `recon/__main__.py` | The CLI (`python -m recon <command>`). |
| `tests/` | 39 pytest tests (see section 14). |
| `reports/` | Generated output, committed as a sample. |

## 4. Data model

Six tables. The plan called for four (internal_trades, broker_trades, match_results, breaks) plus the answer_key. I added `recon_runs` because the report needs per-day counts (exact vs fuzzy, unmatched, resolved) that can't be recovered from the other tables cheaply.

### internal_trades: our blotter

`trade_id` (PK), `trade_date`, `instrument`, `side` (BUY/SELL), `quantity`, `price numeric(18,4)`, `currency`, `counterparty`, `settle_date`.

- `trade_id` format: `T20260914-00042` (date + sequence). On our side it is trustworthy, which is why it is the primary key.
- `price` is `numeric`, not float. Money must not be stored as binary floating point in a database.
- CHECK constraints (`side IN ('BUY','SELL')`, `quantity > 0`, `price > 0`) stop impossible data at the door.

### broker_trades: the broker's statements, **versioned**

PK is `(broker_ref, version)`. Columns: `statement_date`, `record_status` (NEW / AMEND / CANCEL), `trade_id` (nullable, unreliable), and the same economic fields.

Why versioned? In real life a broker fixes a bad confirm by sending an **amendment** on a later statement, and withdraws a bogus one with a **cancel**. They don't edit history. So each broker record can have version 1 (NEW), then version 2 (AMEND or CANCEL) on a later statement date. **The broker's current view as of day D = the highest version of each broker_ref with statement_date ≤ D.** This is what lets breaks *resolve*: tomorrow's amendment is how a price break gets fixed.

There is no counterparty column, because the broker *is* the counterparty. The `trade_id` column is our ID as the broker quoted it. It can be NULL (3% of records) or mistyped (1%).

### answer_key: the planted errors

`plant_date`, `break_type`, `trade_id` (true internal ID), `broker_ref` (the damaged broker record), `field`, `original_value`, `changed_value`, `planned_resolution_date`.

**The matcher never reads this table.** Only `accuracy.py` and the `v_planted_detection` view do. That separation is what makes the accuracy numbers honest: the detector cannot cheat by looking at the answers.

### recon_runs: one row per daily run

Counts: `internal_count`, `broker_count`, `exact_matches`, `fuzzy_matches`, `partial_matches`, `clean_matches`, `unmatched_internal`, `unmatched_broker`, `duplicates_flagged`, `new_breaks`, `resolved_breaks`, `open_breaks`, and the `tolerance` used. Storing the tolerance means a run can always be explained afterwards.

### match_results: one row per matched pair per run

`run_date`, `trade_id`, `broker_ref`, `match_method` (exact / fuzzy / partial), `fields_differing text[]`. This is the audit trail of *how* each trade was paired. A Postgres array holds the list of differing fields (e.g. `{price,settle_date}`).

### breaks: daily snapshot of open breaks

PK `(run_date, break_id)`. Columns: `break_key`, `break_type`, `severity` (1-3), `trade_id`, `broker_ref`, `related_ref`, `field`, `internal_value`, `broker_value`, `detail`, `notional`, `first_seen_date`, `status` (NEW / OPEN), `suggested_owner`.

Design decision: **snapshot, not one row per break lifetime.** Each day, every break that is open gets a row. The same break keeps the same `break_id` and `first_seen_date` across days. Benefits:
- "What was open on 2026-09-25?" is a simple `WHERE run_date = ...`.
- Day-over-day change is a simple comparison.
- Ageing = business days between `first_seen_date` and `run_date`.
- A break *resolved* on day D is one that has a row on D-1 and none on D. The pipeline counts these into `recon_runs.resolved_breaks`.

The cost is more rows (about 4,000 over 20 days), which is trivial.

`break_key` = `type|trade_id|broker_ref|field`. This is the break's identity across days (section 8). `related_ref` is set only on duplicates: it names the original record that was duplicated (section 12, fix 3).

## 5. Synthetic data generation

File: `recon/generate.py`. Everything flows from **one** `numpy.random.default_rng(SEED)`, consumed in a fixed order, so the same seed always produces byte-identical data (a test checks this).

### Step 1: prices
`data/instruments.csv` holds 25 real tickers with an approximate price level each. These are approximate public price levels, not exact closes. They only need to be plausible. The daily close for each instrument is a **random walk**: `close_t = base × exp(cumulative sum of N(0, 1.4%))`. 1.4% daily volatility is roughly realistic for large-cap equities.

*Why not download real prices?* So the project runs offline, deterministically, with no API key. To use real closes, replace the `base_price` column (see section 16).

### Step 2: the clean blotter (2,000 trades a day × 20 business days = 40,000)
For each trade:
- **instrument**: weighted choice (AAPL and NVDA trade more than PEP).
- **side**: 50/50 BUY/SELL.
- **quantity**: weighted choice of realistic lot sizes (100 to 25,000).
- **price**: the day's close × (1 + N(0, 0.4%)), i.e. intraday variation, to 4 decimals.
- **settle_date**: trade date + **1 business day for US, 2 for UK/EU** (`numpy.busday_offset`). This mirrors the real T+1 / T+2 split.
- **counterparty**: one of four fictional executing brokers.

Business days come from `pandas.bdate_range` starting Monday 2026-09-14, giving 20 days through Friday 2026-10-09.

### Step 3: the broker copy
Each internal trade is copied, the order is **shuffled** (broker files never come in your order), each record gets its own `broker_ref` (`BR20260914000001`…), and counterparty is dropped. A hidden `true_trade_id` travels with each record during generation so the answer key can name the real trade. It is never saved.

### Step 4: harmless noise (not breaks, and not in the answer key)
- **3% of broker records get a blank trade_id** and **1% get a mistyped one**. Two typo styles: a dropped hyphen (`T2026091400042`) or a letter O for zero (`T20260914-O0042`). Deliberately *not* digit transposition, which could produce another real trade's ID and make the answer key ambiguous.
- **10% of broker prices are nudged by up to ±0.3bp**, simulating rounding or average-price differences. These must *not* be reported. This noise is why a tolerance exists, and the sweep in section 13 shows what happens without one.

### Step 5: planted breaks (3-5% of trades a day)
Daily error count = 2,000 × 4% × a random factor in [0.75, 1.25], so between 60 and 100 a day. The types are drawn from a deliberately uneven mix (`config.ERROR_MIX`): price 28%, quantity 18%, settle date 15%, missing at broker 13%, missing internally 10%, duplicate 9%, side 7%. Each target is a distinct trade.

| Planted type | What the generator does to the broker file |
|---|---|
| price_mismatch | Half the time swap the first two decimals (101.25 → 101.52, the classic keying error); otherwise move the price by 5-50bp. Both are always beyond the 1bp tolerance. |
| quantity_mismatch | ×10, ÷10, ±100, or + 100/500/1,000 (fat finger or partial fill). |
| side_mismatch | BUY ↔ SELL. |
| settlement_date_mismatch | Push settle_date out one business day (T+1 booked, T+2 confirmed). |
| missing_at_broker | Delete the broker record. |
| duplicate | Append a copy with a new broker_ref (same trade_id, which may itself be blank). |
| missing_internally | Add a ghost broker trade that nobody booked: random instrument, side, size and price; trade_id blank (70%) or a fake ID that doesn't exist (30%). |

Every plant writes one answer_key row.

### Step 6: resolution (so ageing means something)
Each plant draws a delay from `RESOLUTION_LAG_PROBS`: 1 day 55%, 2 days 22%, 3 days 8%, 4 days 4%, 6 days 3%, never 8%. On the fix day the generator writes a new record into that day's broker statement:

| Break | The broker's fix |
|---|---|
| Field breaks | **AMEND**: version + 1, with the original value restored |
| Missing at broker | **Late confirm**: a NEW record with a new broker_ref and correct details |
| Duplicate, missing internally | **CANCEL**: version + 1 of that broker_ref |

The fix date is stored as `planned_resolution_date`, so accuracy can also check that the tool closed each break on the right day.

**Final dataset:** 40,000 internal trades; 41,423 broker records (40,229 NEW, 981 AMEND, 213 CANCEL); 1,607 planted breaks; 241 never fixed inside the window.

## 6. The matching engine

File: `recon/matcher.py`, function `reconcile(internal, broker, tolerance)`. The passes run in a **fixed order**, and **each pass only sees what the previous passes left unmatched**. Order matters: the strict, high-confidence passes go first, so the looser passes have fewer candidates and less chance of a wrong pairing.

Implementation trick: before joining, every column is prefixed (`i_price`, `b_price`), so after a merge both sides' values sit side by side and can be compared column against column. All comparisons are vectorised pandas/numpy, not Python loops over rows.

### Pass 1: duplicate check (`find_duplicates`)
Runs *before* matching so that two broker records can never both match the same internal trade.
- **Internal side:** repeated `trade_id` means a double booking.
- **Broker side:** a repeated `broker_ref`; a repeated non-blank `trade_id` (the same trade confirmed twice); and, added after testing, **two blank-trade_id records with identical economics** (date, instrument, side, quantity, price to 4dp, currency, settle date). Without that last rule, a duplicated anonymous record could never be caught.
- The record with the lowest reference is kept for matching. Each later copy is raised as a `duplicate` break, with `related_ref` pointing at the original, and removed from the pool.

### Pass 2: exact match on trade_id (`exact_match`)
Inner join internal ↔ broker on `trade_id` (blank IDs excluded). Guard: if the pair has **different instruments**, it is rejected. Same ID but different stock means the ID is wrong, not that this is the same trade with a field error. Both sides go on to the later passes.

### Pass 3: tolerance check (inside `_compare_pairs`)
For every pair, compare side, quantity, price, currency and settle_date. Price is equal when

```
|broker_price − internal_price| ≤ tolerance × internal_price        (tolerance = 0.0001 = 1 basis point)
```

The tolerance is **relative** (basis points), not absolute cents, because 1 cent on a $9 stock is 11bp while on a $700 stock it is 0.14bp. It lives in one place, `config.PRICE_TOLERANCE`, and can be overridden with `--tolerance`. Each differing field becomes a break. The list of differing fields is written to `match_results.fields_differing`.

### Pass 4: fuzzy match on leftovers (`fuzzy_match`)
For trades with no usable trade_id: join leftovers on **trade_date + instrument + side + quantity**, keep candidates whose **price is within tolerance**, and accept a pair only if it is the **only candidate on both sides** (one-to-one). Requiring uniqueness both ways is what keeps fuzzy matching safe: if two internal trades could match one broker record, we don't guess.

One refinement, added after testing: for ambiguous groups, accept a pair if each side is the other's **strictly nearest price** (mutual nearest neighbour). Exact ties stay unmatched. A fuzzy match still compares currency and settle date, so a blank-ID trade with a settle date error is still caught.

### Pass 5: partial match (`partial_match`, added after accuracy testing)
This handles the hardest case: a record with **no trade_id AND a planted error in side, quantity or price**. Exact can't see it (no ID), and fuzzy can't either (the error is in one of its keys). Without this pass, each such trade shows up as two false "missing" breaks. Rule: candidates share **trade_date, instrument, currency and settle_date**, differ in **exactly one** of side / quantity / price, and are the **unique candidate on both sides**. The one differing field is raised as a break, with `match_method = 'partial'` so an analyst knows the pairing is inferred.

### Pass 6: classify the rest
Internal leftovers become **missing_at_broker**. Broker leftovers become **missing_internally**, with the trade_id the broker quoted shown in the detail.

Finally, each duplicate break takes the `trade_id` that its original record matched, so a duplicate that stays open keeps the right trade in tomorrow's population (section 12, fix 3).

**Output:** `ReconResult(matches, breaks, stats)`. Stats include exact, fuzzy and partial counts, clean matches, unmatched counts, duplicates flagged, exact pairs rejected for instrument, and ambiguous fuzzy groups.

## 7. Break types, severity and owners

File: `recon/breaks.py`.

| Type | Severity | Why |
|---|---|---|
| side_mismatch | 3 High | Position is wrong by twice the quantity, and cash flows the wrong way. |
| quantity_mismatch | 3 High | Both the position and the cash are wrong. |
| missing_internally | 3 High | There is exposure nobody has booked, so risk and P&L are blind to it. |
| missing_at_broker | 2 Medium | Booked but unconfirmed: it will fail to settle unless chased. |
| duplicate | 2 Medium | Risk of settling the same trade twice. |
| settlement_date_mismatch | 2 Medium | Cash and stock move on the wrong day, causing a failed or early settlement. |
| price_mismatch | 1 Low, **2 if \|Δprice\| × quantity ≥ 1,000** | Small price differences are routine. Large money-at-stake ones are not. |

The plan said "small price mismatch low". I defined "small" by **money at stake** rather than percentage, because 0.3% on 100 shares is trivial while 0.3% on 25,000 shares of a $700 stock is $52,500.

**Ranking** (view `v_top_breaks`): severity desc → age desc → notional desc → break_id. The oldest high-severity break is the most urgent, because it is the one most likely to cause a failed settlement.

Owners follow the plan: Trade Support for most; "escalate quickly" for side mismatches; Settlements for settlement-date breaks. A currency difference has no type of its own (the plan fixed seven), so it is raised as `price_mismatch`, because it changes the amount owed.

## 8. Running across days: population, lifecycle, ageing

File: `recon/pipeline.py`. `run_all()` processes the days **strictly in order**, because each day depends on what was open the day before. A full 20-day run takes about 8 seconds, so `python -m recon run` always recomputes from scratch. That makes it idempotent (running twice gives the same result) and avoids partial-state bugs.

### Which trades are compared on day D (`select_population`)
- **Internal side:** trades dated D, plus older trades with an **open break**, plus older trades whose broker record was **re-sent today** (an AMEND or CANCEL to a previously agreed trade).
- **Broker side:** take the latest version of every record known as of D (statement_date ≤ D). Keep it if it is on today's statement, belongs to an open break (or is the original of an open duplicate), or quotes a trade ID in today's internal population. Drop CANCELled records.

This is how breaks resolve. On the day an AMEND arrives, the trade is compared again with the corrected record, the break isn't raised, and it counts as resolved. If no fix arrives, the same break is raised again and its age grows.

### Lifecycle (`apply_lifecycle`)
Each break has a **break_key** = `type|trade_id|broker_ref|field`. Compare today's keys with yesterday's open breaks:
- key seen yesterday → **same break**: keep `break_id` and `first_seen_date`, status `OPEN`;
- new key → new `break_id` (`BRK-000123`), `first_seen_date = D`, status `NEW`;
- open yesterday, missing today → **resolved**.

### Ageing
Age = number of reconciliation runs after `first_seen_date` up to `run_date`, computed in SQL (`v_open_breaks`). It is counted in **business days**, so a break raised on Friday is 1 day old on Monday, not 3. The report buckets ages as 0, 1, 2-3 and 4+ days, as the plan specified.

## 9. SQL reporting views

File: `sql/views.sql`. The report reads only from these, so the reporting logic lives in SQL as the plan required.

| View | What it answers | Technique worth knowing |
|---|---|---|
| `v_open_breaks` | Every open break with `age_days` | Correlated subquery counting `recon_runs` rows between first_seen and run_date (business-day age without a holiday calendar) |
| `v_daily_summary` | Trades compared, match rate, clean match rate, open breaks, change vs previous run | `LAG() OVER (ORDER BY run_date)` window function; `NULLIF` to avoid divide by zero |
| `v_breaks_by_type` | Count and share for all seven types, zeros included | `VALUES` list CROSS JOINed to runs, then LEFT JOIN, so empty types show 0; share via `sum(count(*)) OVER (PARTITION BY run_date)` |
| `v_top_breaks` | Breaks ranked for follow-up | `ROW_NUMBER() OVER (PARTITION BY run_date ORDER BY severity DESC, age_days DESC, notional DESC)` |
| `v_ageing` | Open breaks per age bucket | `CASE` bucketing plus the same zero-fill pattern |
| `v_matcher_health` | Exact / fuzzy / partial / unmatched, fallback share | Simple arithmetic on `recon_runs` |
| `v_detected_breaks` | Each distinct break ever raised, first and last day | `GROUP BY break_key` with `min/max(run_date)` |
| `v_planted_detection` | Each planted error, detected or not | `LEFT JOIN LATERAL` with a `CASE` choosing the identifying key per type |

`v_planted_detection` re-implements the accuracy join in SQL. The accuracy report prints both counts (1,605 = 1,605) as a cross-check that the Python scorer and the SQL agree.

## 10. The daily report

`python -m recon report [--date YYYY-MM-DD]` writes `reports/break_report_<date>.md` plus two PNGs (matplotlib, headless `Agg` backend). Its five sections follow the plan:

1. **Headline**: trades compared, match rate, clean match rate, open breaks, change vs previous run, new and resolved today.
2. **Breaks by type**: count and share for all seven types, plus a bar chart.
3. **Top 10**: severity, type, IDs, what differs, age, owner.
4. **Ageing**: the 0 / 1 / 2-3 / 4+ buckets.
5. **Matcher health**: exact, fuzzy and partial counts, unmatched counts, duplicates, and the share that needed a fallback.

The second chart is the match rate over the 20 days. I report two rates because they mean different things:
- **Match rate** (98.6-99.6%) = internal trades paired with *some* broker record.
- **Clean match rate** (91-97%) = paired *and* every field agrees. It drifts down over the period because breaks that are never fixed accumulate in the population.

## 11. Measuring accuracy

File: `recon/accuracy.py`. **Unit of scoring:** one distinct break (`break_key`) across the whole period, compared with one planted error.

How a detected break is linked to a plant:
- `missing_internally` and `duplicate` → by **broker_ref**. There is no internal trade for a ghost, and for a duplicate the internal trade is fine; the *extra record* is the error.
- every other type → by **(break_type, trade_id)**.

Definitions:
- **True positive**: a plant raised with the correct type.
- **False negative (missed)**: a plant never raised, or raised only as a different type.
- **False positive**: a raised break that matches no plant.
- **Recall** = TP / planted = 1,605 / 1,607 = **99.88%**.
- **Precision** = TP / (TP + FP) = 1,605 / 1,606 = **99.94%**.
- **Lifecycle accuracy** = share of detected plants whose last open day was exactly the day before the broker's fix (or the final day, for never-fixed plants) = **100%**.

Note that the type has to be right too. A side error that surfaces as missing_at_broker + missing_internally counts as one miss *and* two false positives. That is stricter than "we flagged something on that trade", and it is the honest measure.

The 10% harmless price noise and the 4% blank or garbled IDs are *not* in the answer key. Flagging any of them would count as a false positive, so precision also measures whether the tool stays quiet when it should.

## 12. Build log: what went wrong and how it was fixed

This is the most useful section for interviews: it shows how the accuracy measurement drove the design.

| Version | Change | Recall | Precision | FP |
|---|---|---|---|---|
| v1 | Plan as written: duplicate check, exact, tolerance, fuzzy, classify | 97.57% | 94.34% | 94 |
| v2 | + economic duplicate rule, + mutual-nearest tie-break, + partial pass | 99.88% | 99.75% | 4 |
| v3 | + `related_ref` and duplicate trade_id back-fill (open-duplicate bug) | 99.88% | **99.94%** | **1** |
| v4 | + re-check trades whose broker record is re-sent (amend bug) | 99.88% | 99.94% | 1 (but 96 → 7 at 10bp tolerance) |
| v5 | Fixed a crash on a day with zero breaks (found by a unit test) | – | – | – |

**Fix 1: collisions (v1 → v2).** Diagnosis showed 39 misses with the same cause: a planted side, quantity or price error that landed on a broker record whose trade_id was *also* blank or garbled. Exact can't see it (no ID) and fuzzy can't either (the error is in a fuzzy key), so it fell through as missing_at_broker + missing_internally: 1 miss and 2 false positives each. Fix: the **partial pass**.

**Fix 2: ambiguity (v1 → v2).** 7 duplicates were missed because the duplicated record had a blank ID: two identical anonymous broker records gave fuzzy two candidates, so it matched neither. Fix: the **economic duplicate rule** in pass 1. Separately, two genuine NVDA trades of 1,000 shares within 0.9bp of each other, both with blank broker IDs, made a 2×2 ambiguous group. Fix: **mutual nearest price**.

**Fix 3: an open duplicate turning into a ghost (v2 → v3).** A duplicate that stayed open past day 1 was re-checked the next day *without* its original record, because the original wasn't in any open break and had no usable trade_id. Alone, the duplicate looked like a trade nobody booked, so it was raised as missing_internally. Fix: duplicates carry **`related_ref`** (the original record), the population pulls it in, and the duplicate break takes the internal `trade_id` its original matched, so that trade is pulled in too.

**Fix 4: amendments to agreed trades (v3 → v4).** Found by asking why the tolerance sweep showed *false positives rising* as tolerance widened. A price error inside a wide tolerance matched clean. When the broker then sent an AMEND, the trade had no open break so it wasn't re-checked, and the amended record looked unbooked. Fix: remember every matched pair and **re-check a trade whenever its broker record is re-sent**. This matches real operations, where an amendment to an agreed trade reopens it.

**Overfitting check.** All tuning was on seed 20261016. `test_accuracy_holds_on_other_seeds` runs seeds 1, 2 and 3 and requires recall and precision ≥ 99% on each.

## 13. The tolerance sweep

`python -m recon sweep` reruns all 20 days in memory at seven tolerances (nothing in the DB changes):

| Tolerance | Distinct breaks | False positives | Missed price plants | Recall | Precision |
|---|---|---|---|---|---|
| 0bp | 5,520 | 3,920 | 1 | 99.56% | 28.99% |
| 0.1bp | 4,285 | 2,684 | 1 | 99.63% | 37.36% |
| 0.5bp | 1,606 | 1 | 0 | 99.88% | 99.94% |
| **1bp (default)** | **1,606** | **1** | **0** | **99.88%** | **99.94%** |
| 5bp | 1,566 | 3 | 41 | 97.26% | 99.81% |
| 10bp | 1,505 | 7 | 105 | 93.22% | 99.53% |
| 50bp | 1,180 | 17 | 429 | 72.37% | 98.56% |

How to read it:
- **Too tight (0bp):** the ±0.3bp rounding noise becomes ~3,900 false price breaks. Analysts drown in noise. Recall also dips, because fuzzy matching needs price-in-tolerance, so anonymous records with noise stop matching.
- **Sweet spot (0.5-1bp):** above the noise and below the smallest real error (5bp).
- **Too loose (5bp+):** real 5-50bp price errors are silently accepted. Recall falls to 72% at 50bp. That is the dangerous failure: breaks you never see.

The general lesson: the tolerance has to sit between the size of the legitimate noise and the size of the smallest error you care about. Here that is easy to show because both are known.

## 14. Testing

`python -m pytest`: 39 tests, about 16 seconds.

| File | Covers |
|---|---|
| `tests/test_matcher.py` (21) | One test **per break type**, built from a handful of hand-made trades (the plan's requirement). Plus: clean files give no breaks; tolerance in and out; tolerance as a parameter; the exact 1bp boundary; a duplicate can't match twice; the economic duplicate rule; internal double booking; fuzzy on blank and garbled IDs; fuzzy still reports field differences; ties left unmatched; the mutual-nearest tie-break; same ID with a different instrument rejected; the partial pass finds a blank-ID quantity error; the partial pass needs uniqueness. |
| `tests/test_lifecycle.py` (5) | A break carries over (same ID, same first_seen, NEW → OPEN) and resolves on an AMEND; a late confirm resolves missing_at_broker; a CANCEL resolves missing_internally; regression tests for fixes 3 and 4. |
| `tests/test_generate.py` (8) | Same seed gives identical data; a different seed gives different data; error rate 3-5% every day; all seven types are planted; field plants really changed the record; unique internal IDs; garbled IDs never hit a real trade; fixes arrive on the planned day. |
| `tests/test_end_to_end.py` (5) | Full 20-day accuracy floor (≥ 99.5%); accuracy ≥ 99% on three other seeds; **DB integration**: loads a small dataset into a scratch schema, then checks the views agree with Python (open counts, the seven types, ageing totals, SQL vs Python TP). It skips if Postgres is down. |

## 15. Limitations and what I would do next

**Known limitations (documented, not hidden):**
1. **Partial-pass false pairing.** This is the only remaining error (2 misses + 1 FP). On 2026-09-18, booked trade T20260918-01375 (MSFT) was never confirmed, and an unbooked broker trade BR20260918002002 (MSFT, same day and settle date, same side and size) differed only on price. The partial pass paired them and raised a price break. It is a trade-off: the pass rescues about 40 plants and 80 false positives for this one error. Mitigation idea: require a price within, say, 1% for partial matches, or route partial matches to a "suggested match" queue for a human to confirm.
2. **Exact twins** with no broker ID can't be told apart. This is by design.
3. **One-to-one only.** There are no allocations or block trades (one broker fill against several internal bookings, which needs many-to-one matching on summed quantity).
4. **Synthetic input.** There is no parsing of real broker formats (CSV variants, SWIFT MT515/MT518 confirms, FIX execution reports).
5. **In-memory recompute.** Fine for 40k trades in 8s. At millions of trades a day I'd push the exact join into SQL and process incrementally.

**Next steps if I continued:** a small dashboard (the plan's optional extra); net cash and position impact per break; a resolution workflow (assign, comment, close with a reason); a holiday calendar for settle dates; per-broker match statistics.

## 16. Running, configuring and extending

```bash
docker compose up -d --wait        # start Postgres (port 5433)
pip install -r requirements.txt
python -m recon all                # everything, about 2 minutes including the sweep
python -m recon run --tolerance 0.0005 && python -m recon report && python -m recon accuracy
python -m pytest -v
docker compose down                # stop (add -v to delete the data volume)
```

- **Connect with psql:** `docker exec -it trade-recon-db psql -U recon` then e.g. `SELECT * FROM v_top_breaks WHERE run_date = '2026-10-09' AND rank <= 10;`
- **Different database:** set `RECON_DB_URL=postgresql://user:pass@host:port/db`.
- **Change anything numeric:** `recon/config.py` (seed, trade count, error mix, noise, resolution delays, tolerance, severity threshold).
- **Real prices:** replace `base_price` in `data/instruments.csv` with real end-of-day closes. Add or remove tickers freely; `settle_lag` is 1 for US and 2 for UK/EU.
- **Add a break type:** plant it in `generate.py`, detect it in `matcher.py`, add severity and owner in `breaks.py`, add it to the CHECK constraint and the `v_breaks_by_type` VALUES list, and add a unit test.

## 17. Glossary

| Term | Meaning |
|---|---|
| Blotter | The firm's own list of its trades. |
| Confirm / confirmation | The counterparty's record of a trade, sent after execution. |
| Break | Any disagreement between the two records. |
| Reconciliation ("rec") | Matching two sets of records and explaining every difference. |
| Trade Support | The operations team that validates trades after execution and chases breaks. |
| Settlements | The team making sure cash and securities actually move on settle date. |
| T+1 / T+2 | Settlement 1 or 2 business days after trade date. US equities have been T+1 since May 2024; UK/EU equities are T+2. |
| Failed settlement / fail | A trade that doesn't settle on time, which costs money (penalties, funding). |
| Basis point (bp) | 0.01%. 1bp tolerance on a $100 price = $0.01. |
| Notional | Quantity × price: the trade's value. |
| AMEND / CANCEL | A broker's correction or withdrawal of a record it previously sent. |
| Ageing | How long a break has been open, in business days. |
| Precision / recall | Of what we flagged, how much was real / of what was real, how much we flagged. |
