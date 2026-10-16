"""What happens to a break after the matcher finds it: severity, owner, and lifecycle.

Lifecycle: every break has a stable break_key. If today's run raises a key that
was open yesterday, it is the same break, so it keeps its break_id and its
first_seen_date. A key that was open yesterday and is gone today has been resolved.
"""
import pandas as pd

from . import config

BREAK_TYPES = ["missing_at_broker", "missing_internally", "quantity_mismatch",
               "price_mismatch", "side_mismatch", "duplicate", "settlement_date_mismatch"]

# 3 = high, 2 = medium, 1 = low. Price breaks are 1 or 2 depending on the money at stake.
SEVERITY = {
    "side_mismatch": 3,             # wrong direction: position is out by twice the quantity
    "quantity_mismatch": 3,         # position and cash both wrong
    "missing_internally": 3,        # exposure nobody has booked
    "missing_at_broker": 2,         # booked but unconfirmed: will fail to settle if not chased
    "duplicate": 2,                 # risk of settling the same trade twice
    "settlement_date_mismatch": 2,  # cash and stock move on the wrong day
    "price_mismatch": 1,
}

OWNER = {
    "missing_at_broker": "Trade Support - chase the broker",
    "missing_internally": "Trade Support - check with the desk",
    "quantity_mismatch": "Trade Support",
    "price_mismatch": "Trade Support, then the desk",
    "side_mismatch": "Trade Support - escalate quickly",
    "duplicate": "Trade Support",
    "settlement_date_mismatch": "Settlements",
}


def score(breaks):
    """Add severity, suggested_owner and break_key to the matcher's raw breaks."""
    b = breaks.copy()
    b["severity"] = b["break_type"].map(SEVERITY)
    big_price = (b["break_type"] == "price_mismatch") & \
        (pd.to_numeric(b["price_impact"], errors="coerce").fillna(0) >= config.PRICE_IMPACT_HIGH)
    b.loc[big_price, "severity"] = 2
    b["suggested_owner"] = b["break_type"].map(OWNER)
    b["break_key"] = (b["break_type"] + "|" + b["trade_id"].fillna("") + "|"
                      + b["broker_ref"].fillna("") + "|" + b["field"].fillna(""))
    return b.drop_duplicates("break_key")


class BreakIds:
    """Hands out BRK-000001, BRK-000002, ... across a whole multi-day run."""

    def __init__(self, start=1):
        self.n = start - 1

    def next(self):
        self.n += 1
        return f"BRK-{self.n:06d}"


def apply_lifecycle(scored, previous_open, run_date, ids):
    """Carry break_id and first_seen_date forward for breaks still open; return
    (today's open breaks, breaks resolved since the previous run)."""
    today = scored.copy()
    if today.empty:  # a clean day: nothing open, everything from yesterday resolved
        today = today.assign(break_id=None, first_seen_date=None, status=None, run_date=None)
        resolved = previous_open if previous_open is not None else today
        return today, resolved
    if previous_open is not None and len(previous_open):
        prev = previous_open.set_index("break_key")
        carried = today["break_key"].isin(prev.index)
        today.loc[carried, "break_id"] = today.loc[carried, "break_key"].map(prev["break_id"])
        today.loc[carried, "first_seen_date"] = today.loc[carried, "break_key"].map(prev["first_seen_date"])
        resolved = previous_open[~previous_open["break_key"].isin(today["break_key"])]
    else:
        carried = pd.Series(False, index=today.index)
        resolved = pd.DataFrame(columns=today.columns)

    new = ~carried
    today.loc[new, "break_id"] = [ids.next() for _ in range(int(new.sum()))]
    today.loc[new, "first_seen_date"] = run_date
    today["status"] = ["OPEN" if c else "NEW" for c in carried]
    today["run_date"] = run_date
    return today, resolved
