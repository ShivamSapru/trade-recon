"""Synthetic data: a clean blotter, a damaged broker copy, and the answer key.

Flow for each business day:
  1. Generate the clean internal blotter.
  2. Copy it into a broker statement (new broker_ref, shuffled order, no counterparty).
  3. Add harmless noise: blank or mistyped trade_ids, tiny price differences.
  4. Plant real breaks in about 3-5% of trades and write each one to the answer key.
  5. Emit fixes (AMEND / CANCEL / late confirms) on later statements for breaks
     planted earlier, so some breaks resolve the next day and some carry over.

Everything comes from one numpy Generator seeded from config.SEED, so the
output is identical on every run.
"""
from collections import defaultdict
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import config

COUNTERPARTIES = [  # fictional executing brokers
    "Harbour Point Securities",
    "Larkfield Capital Markets",
    "Northgate Brokerage",
    "Redwood Street Execution",
]
LOT_SIZES = np.array([100, 200, 300, 500, 1000, 1500, 2000, 2500, 5000, 10000, 25000])
LOT_WEIGHTS = np.array([10, 8, 6, 10, 14, 6, 10, 6, 8, 5, 2], dtype=float)
LOT_WEIGHTS /= LOT_WEIGHTS.sum()

BROKER_COLUMNS = ["broker_ref", "version", "statement_date", "record_status", "trade_id",
                  "trade_date", "instrument", "side", "quantity", "price", "currency",
                  "settle_date"]
INTERNAL_COLUMNS = ["trade_id", "trade_date", "instrument", "side", "quantity", "price",
                    "currency", "counterparty", "settle_date"]
ANSWER_KEY_COLUMNS = ["plant_date", "break_type", "trade_id", "broker_ref", "field",
                      "original_value", "changed_value", "planned_resolution_date"]


@dataclass
class SyntheticData:
    internal: pd.DataFrame     # internal_trades
    broker: pd.DataFrame       # broker_trades, every version
    answer_key: pd.DataFrame   # answer_key
    days: list


def load_instruments():
    return pd.read_csv(config.DATA_DIR / "instruments.csv")


def trading_days(start=None, n=None):
    start = start or config.START_DATE
    n = n or config.N_DAYS
    return [d.date() for d in pd.bdate_range(start, periods=n)]


def add_business_days(d, n):
    return pd.Timestamp(np.busday_offset(np.datetime64(d, "D"), int(n), roll="forward")).date()


def _closing_prices(rng, instruments, days):
    """Random-walk closes from each instrument's base price: shape [n_days, n_instruments]."""
    base = instruments["base_price"].to_numpy()
    returns = rng.normal(0.0, 0.014, size=(len(days), len(base)))
    return base * np.exp(np.cumsum(returns, axis=0))


def _clean_day(rng, instruments, day, closes, n):
    weights = instruments["weight"].to_numpy(dtype=float)
    idx = rng.choice(len(instruments), size=n, p=weights / weights.sum())
    inst = instruments.iloc[idx].reset_index(drop=True)
    prices = np.round(closes[idx] * (1 + rng.normal(0.0, 0.004, size=n)), 4)
    lags = inst["settle_lag"].to_numpy()
    settle = [add_business_days(day, lag) for lag in lags]
    return pd.DataFrame({
        "trade_id": [f"T{day:%Y%m%d}-{i:05d}" for i in range(1, n + 1)],
        "trade_date": day,
        "instrument": inst["ticker"],
        "side": rng.choice(["BUY", "SELL"], size=n),
        "quantity": rng.choice(LOT_SIZES, size=n, p=LOT_WEIGHTS).astype(int),
        "price": prices,
        "currency": inst["currency"],
        "counterparty": rng.choice(COUNTERPARTIES, size=n),
        "settle_date": settle,
    })


def _garble(trade_id, rng):
    """A realistic typo that never points at another real trade."""
    # A transposed-digit typo could land on another real trade_id, which would make the
    # answer key ambiguous, so only typos that can never produce a valid id are used.
    prefix, seq = trade_id.split("-")
    if rng.random() < 0.5 or "0" not in seq:
        return prefix + seq                           # dropped the hyphen
    return f"{prefix}-{seq.replace('0', 'O', 1)}"     # letter O keyed for zero


def _swap_cents(price):
    """101.25 -> 101.52: the classic keying error."""
    whole, frac = f"{price:.4f}".split(".")
    if frac[0] == frac[1]:
        return None
    return float(f"{whole}.{frac[1]}{frac[0]}{frac[2:]}")


class _RefCounter:
    def __init__(self, day):
        self.day = day
        self.n = 0

    def next(self):
        self.n += 1
        return f"BR{self.day:%Y%m%d}{self.n:06d}"


def _fmt(value):
    if value is None:
        return None
    if isinstance(value, float):
        return f"{value:.4f}"
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def generate(seed=None, days=None, trades_per_day=None):
    seed = config.SEED if seed is None else seed
    days = days or trading_days()
    trades_per_day = trades_per_day or config.TRADES_PER_DAY
    rng = np.random.default_rng(seed)
    instruments = load_instruments()
    closes = _closing_prices(rng, instruments, days)

    lag_values = np.array(list(config.RESOLUTION_LAG_PROBS))
    lag_probs = np.array(list(config.RESOLUTION_LAG_PROBS.values()))
    lag_probs /= lag_probs.sum()
    mix_types = list(config.ERROR_MIX)
    mix_probs = np.array(list(config.ERROR_MIX.values()))
    mix_probs /= mix_probs.sum()

    internal_parts = []
    broker_rows = []
    answer_rows = []
    latest = {}                     # broker_ref -> the broker's current record
    scheduled = defaultdict(list)   # date -> fixes the broker sends on that statement

    def schedule(day_index, event):
        lag = int(rng.choice(lag_values, p=lag_probs))
        if lag == 0 or day_index + lag >= len(days):
            return None
        fix_day = days[day_index + lag]
        scheduled[fix_day].append(event)
        return fix_day

    for di, day in enumerate(days):
        refs = _RefCounter(day)
        internal = _clean_day(rng, instruments, day, closes[di], trades_per_day)
        internal_parts.append(internal)

        # --- broker copy, shuffled, with its own reference numbers ---------
        order = rng.permutation(len(internal))
        day_rows = []
        for rec in internal.iloc[order].to_dict("records"):
            rec.pop("counterparty")
            rec["true_trade_id"] = rec["trade_id"]
            rec.update(broker_ref=refs.next(), version=1, statement_date=day, record_status="NEW")
            day_rows.append(rec)

        # --- harmless noise -------------------------------------------------
        for rec in day_rows:
            r = rng.random()
            if r < config.BLANK_ID_RATE:
                rec["trade_id"] = None
            elif r < config.BLANK_ID_RATE + config.GARBLED_ID_RATE:
                rec["trade_id"] = _garble(rec["trade_id"], rng)
            if rng.random() < config.PRICE_NOISE_RATE:
                bump = rng.uniform(-config.PRICE_NOISE_BP, config.PRICE_NOISE_BP) / 10_000
                rec["price"] = round(rec["price"] * (1 + bump), 4)

        # --- planted breaks -------------------------------------------------
        n_err = int(round(len(internal) * config.ERROR_RATE * rng.uniform(0.75, 1.25)))
        kinds = rng.choice(mix_types, size=n_err, p=mix_probs)
        targets = rng.choice(len(day_rows), size=n_err, replace=False)
        removed = set()
        extra_rows = []

        for kind, t in zip(kinds, targets):
            rec = day_rows[t]
            key = dict(plant_date=day, break_type=kind, trade_id=rec["true_trade_id"],
                       broker_ref=rec["broker_ref"], field=None, original_value=None,
                       changed_value=None, planned_resolution_date=None)

            if kind in ("price_mismatch", "quantity_mismatch", "side_mismatch",
                        "settlement_date_mismatch"):
                field = {"price_mismatch": "price", "quantity_mismatch": "quantity",
                         "side_mismatch": "side", "settlement_date_mismatch": "settle_date"}[kind]
                original = rec[field]
                if kind == "price_mismatch":
                    changed = _swap_cents(original) if rng.random() < 0.5 else None
                    if changed is None:  # small fat-finger: 5 to 50 basis points off
                        pct = rng.uniform(0.0005, 0.005) * rng.choice([-1, 1])
                        changed = round(original * (1 + pct), 4)
                elif kind == "quantity_mismatch":
                    q = int(original)
                    options = [q * 10, q // 10 if q >= 1000 else q + 100, q - 100 if q > 100 else q + 100,
                               q + int(rng.choice([100, 500, 1000]))]
                    changed = int(rng.choice(options))
                elif kind == "side_mismatch":
                    changed = "SELL" if original == "BUY" else "BUY"
                else:
                    changed = add_business_days(original, 1)  # T+1 booked, T+2 confirmed
                rec[field] = changed
                key.update(field=field, original_value=_fmt(original), changed_value=_fmt(changed))
                key["planned_resolution_date"] = schedule(
                    di, ("amend", rec["broker_ref"], field, original))

            elif kind == "missing_at_broker":
                removed.add(t)
                key.update(broker_ref=None, field="record")
                late = {k: v for k, v in rec.items()}
                late["trade_id"] = rec["true_trade_id"]
                late["price"] = float(internal.loc[internal.trade_id == rec["true_trade_id"], "price"].iloc[0])
                key["planned_resolution_date"] = schedule(di, ("late_confirm", late))

            elif kind == "duplicate":
                dup = dict(rec)
                dup["broker_ref"] = refs.next()
                extra_rows.append(dup)
                key.update(broker_ref=dup["broker_ref"], field="record",
                           changed_value=f"copy of {rec['broker_ref']}")
                key["planned_resolution_date"] = schedule(di, ("cancel", dup["broker_ref"]))

            elif kind == "missing_internally":
                i = rng.choice(len(instruments), p=instruments["weight"] / instruments["weight"].sum())
                ins = instruments.iloc[i]
                ghost_id = None if rng.random() < 0.7 else f"T{day:%Y%m%d}-9{rng.integers(1000, 9999)}"
                ghost = dict(
                    broker_ref=refs.next(), version=1, statement_date=day, record_status="NEW",
                    trade_id=ghost_id, true_trade_id=None, trade_date=day,
                    instrument=ins["ticker"], side=str(rng.choice(["BUY", "SELL"])),
                    quantity=int(rng.choice(LOT_SIZES, p=LOT_WEIGHTS)),
                    price=round(float(closes[di][i] * (1 + rng.normal(0, 0.004))), 4),
                    currency=ins["currency"],
                    settle_date=add_business_days(day, ins["settle_lag"]),
                )
                extra_rows.append(ghost)
                key.update(trade_id=None, broker_ref=ghost["broker_ref"], field="record",
                           changed_value=f"{ghost['side']} {ghost['quantity']} {ghost['instrument']}")
                key["planned_resolution_date"] = schedule(di, ("cancel", ghost["broker_ref"]))

            answer_rows.append(key)

        day_rows = [r for j, r in enumerate(day_rows) if j not in removed] + extra_rows
        for rec in day_rows:
            latest[rec["broker_ref"]] = rec
        broker_rows.extend(day_rows)

        # --- fixes for breaks planted on earlier days ------------------------
        for event in scheduled.pop(day, []):
            if event[0] == "amend":
                _, ref, field, original = event
                fixed = dict(latest[ref], version=latest[ref]["version"] + 1,
                             statement_date=day, record_status="AMEND")
                fixed[field] = original
            elif event[0] == "cancel":
                ref = event[1]
                fixed = dict(latest[ref], version=latest[ref]["version"] + 1,
                             statement_date=day, record_status="CANCEL")
            else:  # late confirm of a trade the broker had left off
                fixed = dict(event[1], broker_ref=refs.next(), version=1,
                             statement_date=day, record_status="NEW")
            latest[fixed["broker_ref"]] = fixed
            broker_rows.append(fixed)

    internal_all = pd.concat(internal_parts, ignore_index=True)[INTERNAL_COLUMNS]
    broker_all = pd.DataFrame(broker_rows)[BROKER_COLUMNS]
    broker_all["quantity"] = broker_all["quantity"].astype(int)
    answer_key = pd.DataFrame(answer_rows, columns=ANSWER_KEY_COLUMNS)
    return SyntheticData(internal_all, broker_all, answer_key, days)
