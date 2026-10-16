# Interview Prep: defending the Trade Reconciliation Tool

Use this with [BUILD_GUIDE.md](BUILD_GUIDE.md). The guide explains everything. This page is what to *say*.

## The 30-second pitch

> "I built the daily reconciliation that a Trade Support team runs every morning: it matches our internal trade blotter against broker statements, finds every break, classifies it into one of seven types, and ranks it by severity and age for follow-up. It's Python and pandas for the matching, PostgreSQL for storage and the reporting views. Because I used synthetic data, I planted about 1,600 errors with a known answer key, so I could actually measure it: it catches 99.9% of them with one false positive over 20 days and 40,000 trades."

## The 2-minute version (add these if they want more)

1. **The hard part is that the broker's copy of our trade ID is unreliable**, blank or mistyped about 4% of the time. So matching runs in passes: exact on trade ID, then fuzzy on the trade's economics, taking a match only when there's exactly one candidate on each side.
2. **Prices get a tolerance of 1 basis point**, because brokers report slightly different average prices. I showed with a sweep that at zero tolerance you get about 3,900 false breaks, and at 50bp you silently miss 429 real price errors.
3. **Breaks have a lifecycle.** The broker fixes things with amendments and cancels on later statements, so each day I re-check yesterday's open breaks. A break keeps its ID and age until it is fixed, and the report sorts by severity, then age.
4. **Measuring accuracy changed the design.** My first version, built exactly to plan, had 97.6% recall and 94 false positives. Digging into the misses found two root causes, collisions and ambiguous duplicates. I fixed both, and two lifecycle bugs, and got to 99.9%.

## Numbers to remember

| | |
|---|---|
| Period / volume | 20 business days, 2,000 trades a day = 40,000 trades; 41,423 broker records |
| Planted errors | 1,607 (3-5% a day), across 7 types, deliberately uneven |
| Accuracy | Recall **99.88%** (1,605/1,607), precision **99.94%** (1 FP), lifecycle **100%** |
| First version | Recall 97.57%, precision 94.34%, 94 false positives |
| Match rate | 98.6-99.6% a day; clean match rate 91-97% |
| Fallback share | About 5% of matches needed the fuzzy or partial pass |
| Tolerance | 1bp default; 0bp → 3,920 FPs; 50bp → recall 72% |
| Tests | 39 pytest tests, including one per break type and a DB integration test |
| Runtime | About 8s to reconcile all 20 days |

## Walk one trade through the system

Use a concrete trade if you are asked "explain how it works".

1. **Generation:** on 2026-09-15 the blotter gets `T20260915-00793`: BUY 2,500 CVX at ~$155, settle 2026-09-16 (US, T+1).
2. **Broker copy:** it is copied into the broker file as `BR20260915000473`, in shuffled order, with no counterparty.
3. **Planting:** the generator picks it for a quantity error, so the broker record now says 2,600. The answer key records `quantity_mismatch, T20260915-00793, BR20260915000473, 2500 → 2600`. It draws "never fixed".
4. **Day 1 run:** there are no duplicates. The exact join on trade_id pairs it. Comparing fields, quantity differs, so a `quantity_mismatch` break is raised: severity 3, owner Trade Support, status NEW, break `BRK-…`, first_seen 2026-09-15.
5. **Day 2 and later:** the trade has an open break, so it is in the population again. The broker's latest version still says 2,600, so the same break_key is raised. It keeps its break_id and first_seen, status OPEN, and its age goes up by one each business day.
6. **Report on 2026-10-09:** it is #2 in the top 10 (High severity, 18 business days old).
7. **Accuracy:** the break matches the answer key on (quantity_mismatch, T20260915-00793), so it is a true positive. It was still open on the last day and no fix was planned, so lifecycle is correct.

Had a fix been planned for 09-17, the broker statement that day would contain version 2 (AMEND, quantity 2,500). The re-check would find no difference, and the break would count as resolved on 09-17.

## Likely questions and answers

### About the domain

**Why does reconciliation matter?**
Settlement moves real cash and stock. If our record and the broker's disagree, either the trade fails (penalties, funding costs, upset clients) or we settle the wrong thing and carry a position we don't know about. With T+1 in the US there is about one business day to fix it.

**Why is side mismatch "escalate quickly"?**
A booked buy that the broker has as a sell means our position is out by twice the quantity, and the cash goes the wrong way. It is usually a booking error that also affects risk and P&L.

**Why is missing internally high severity but missing at broker only medium?**
Missing internally means the market thinks we did a trade our systems don't know about: unbooked exposure and unknown risk. Missing at broker is booked and visible to risk. It just needs chasing, though it will fail to settle if nobody does.

**Why are some settle dates T+2?**
The five UK/EU instruments settle T+2 and the US ones T+1. That matches the real markets: the US moved to T+1 in May 2024, and Europe hasn't yet. The generator uses business-day arithmetic.

### About the design

**Why match in passes instead of one clever join?**
Each pass has a different confidence level. Exact on trade ID is near-certain, fuzzy on economics is less so, and partial less again. Running them strictest-first means the loose passes only see the few leftovers, so they rarely have more than one candidate and rarely pair wrongly. It also lets me record *how* each trade matched, which an analyst needs in order to trust a pairing.

**Why must a fuzzy match be unique on both sides?**
Suppose two internal trades are BUY 1,000 NVDA at nearly the same price and one broker record has no ID. Picking either is a guess, and a wrong guess hides a real break behind a fake clean match. Leaving them unmatched produces visible breaks a human can resolve. In reconciliation a false "all clear" is worse than a false alarm.

**Why is the duplicate check first?**
Otherwise both copies could match the same internal trade, or one copy could match and the other become a confusing "missing internally". Removing duplicates first makes everything after it strictly one-to-one.

**Why a relative tolerance (basis points), not cents?**
One cent is 11bp on a $9 stock and 0.14bp on a $700 stock. A fixed cent tolerance would be too loose on cheap stocks and too tight on expensive ones.

**How did you choose 1bp?**
From the sweep. The harmless noise is under 0.3bp and the smallest real error is 5bp, so anything from 0.5bp up to just under 5bp works. 1bp sits safely inside. In real life you would measure the noise distribution from your own broker files the same way.

**Why is the matcher pandas and not SQL?**
Testability and the sweep. As a pure function it runs in milliseconds in unit tests with hand-made trades, and I can rerun 20 days at seven tolerances in memory. Postgres does storage, the audit trail and the reporting views. At much larger volumes I'd push the exact join into SQL, since it's a plain indexed join.

**Why store breaks as a daily snapshot?**
"What was open on day X" becomes a simple filter, day-over-day change is trivial, and age falls out of first_seen_date. Resolution is "open yesterday, absent today". The cost is a few thousand extra rows.

**How do you compute age, and why business days?**
Age is the count of reconciliation runs after first_seen_date. A break raised Friday is 1 day old on Monday. Calendar days would make every Monday look like a crisis.

**Why are broker records versioned?**
Brokers don't edit history. They send AMENDs and CANCELs on later statements. The current view is the latest version as of the statement date, which also gives a full audit trail.

**What's the break_key and why do you need it?**
It is `type|trade_id|broker_ref|field`, the identity of a break across days. If tomorrow's run raises the same key, it is the same break and keeps its ID and age. Without it, every day would look like brand-new breaks and ageing would be impossible.

### About accuracy and testing

**How do you know the numbers are real?**
The generator writes every error it plants to an answer_key table that the matcher never reads. Accuracy compares what the matcher found against that. The seed is fixed, so anyone can rerun it and get the same numbers. A SQL view re-does the join independently and agrees: 1,605 = 1,605.

**What counts as a true positive?**
The right break type on the right trade, or for duplicates and ghost trades on the right broker record. If a side error shows up as two "missing" breaks, I count one miss and two false positives. That is strict on purpose.

**Isn't it easy to score well when you wrote the generator?**
That is a fair challenge. Three things work against it. The noise (blank IDs, garbled IDs, price rounding) is independent of the planted errors, so they collide naturally, and those collisions were exactly what my first version failed on. Tuning was done on one seed, and a test checks three other seeds at ≥ 99%. And the remaining error is documented, not hidden. Real data would be messier, and I say so.

**What were the misses in your first version?**
39 collisions: a planted field error on a broker record whose ID was also blank, so neither exact nor fuzzy could pair it. 7 duplicates of blank-ID records, where fuzzy saw two identical candidates and refused both. And 15 false positives from ambiguous near-identical trades. I fixed them with the partial pass, an economic duplicate rule and the mutual-nearest tie-break.

**Tell me about a bug you found.**
Use fix 4. The tolerance sweep showed false positives going *up* as tolerance got looser, which made no sense. The cause: a price error absorbed by the wide tolerance matched clean, and when the broker later amended that record, the trade wasn't re-checked because it had no open break, so the amendment looked like a trade nobody booked. The fix is what operations does anyway: an amendment to an agreed trade reopens it. False positives at 10bp went from 96 to 7.

**What's the remaining error?**
On 2026-09-18 an MSFT trade was never confirmed, and an unrelated unbooked broker MSFT trade on the same day, with the same side and size, differed only on price. The partial pass paired them and raised one price break instead of two missing breaks. I kept the pass because it rescues about 40 real breaks for this one error. To harden it, I'd cap the price gap for partial matches or send partial matches to a "suggested match" queue for a human to confirm.

**How did you test it?**
One pytest case per break type with a handful of hand-made trades, plus tests for each pass's edge cases (tolerance boundary, ties, ID-instrument conflicts), lifecycle tests (carry, resolve by amend, late confirm, cancel), generator tests (determinism, 3-5% error rate, honest answer key), regression tests for each bug, and an integration test that loads Postgres and checks the views agree with Python. A unit test also caught a crash on days with no breaks, which the full dataset never produces.

### About scale and next steps

**How would this scale to millions of trades?**
Do the exact join in SQL with indexes on trade_id. Run fuzzy only on the leftovers, which are a few percent, partitioned by trade_date and instrument. Process incrementally from yesterday's open breaks instead of recomputing all history. The pass design already limits the expensive work to a small leftover set.

**What would you add next?**
Many-to-one matching for allocations and block trades, parsing of real formats (CSV variants, SWIFT MT515/518, FIX), a resolution workflow (assign, comment, close with a reason), cash and position impact per break, per-broker statistics, a holiday calendar, and a small dashboard.

**What would change with real data?**
More format problems, more break types (wrong account, wrong counterparty, commission and fee differences, FX on cross-currency trades), and noise I'd have to measure rather than know. The pipeline, lifecycle and accuracy method all carry over. For real data you'd score against analyst-resolved breaks instead of an answer key.

## What not to overclaim

- Say **"synthetic data"** clearly, every time. Never imply it ran in production.
- The tickers are real, but the prices are **approximate levels used as a random-walk base**, not real historical closes.
- The 99.9% is against errors **I designed**. Present it as "measured against a known answer key", not as "99.9% accurate in the real world".

## Resume bullet (filled in)

> Built a trade reconciliation tool in Python (pandas) and PostgreSQL that matches an internal blotter against broker statements and classifies 7 break types with severity, ownership and ageing, detecting 99.9% of 1,607 planted breaks with 1 false positive across 20 days (40,000 trades) of synthetic data.
