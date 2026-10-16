"""The matching engine. Pure pandas: two DataFrames in, matches and breaks out.

Passes run in a fixed order, and each pass only sees what earlier passes left:
  1. Duplicate check  - repeated broker_ref / trade_id are flagged and set aside
  2. Exact match      - join on trade_id, then compare the economic fields
  3. Tolerance check  - prices within config.PRICE_TOLERANCE count as equal
  4. Fuzzy match      - leftovers matched on trade_date, instrument, side, quantity
                        and price in tolerance, only when exactly one candidate exists
  5. Partial match    - (added after accuracy testing) leftovers on the same
                        trade_date, instrument, currency and settle_date that differ
                        in exactly one of side / quantity / price, unique on both sides
  6. Classify rest    - internal leftovers are missing at broker, broker leftovers
                        are missing internally
"""
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

COMPARED_FIELDS = ["side", "quantity", "price", "currency", "settle_date"]
FUZZY_KEYS = ["trade_date", "instrument", "side", "quantity"]
ECONOMIC_FIELDS = ["trade_date", "instrument", "side", "quantity", "price", "currency", "settle_date"]

# A currency difference changes the amount owed, so it is raised as a price break.
FIELD_TO_BREAK = {
    "side": "side_mismatch",
    "quantity": "quantity_mismatch",
    "price": "price_mismatch",
    "currency": "price_mismatch",
    "settle_date": "settlement_date_mismatch",
}

BREAK_COLUMNS = ["break_type", "trade_id", "broker_ref", "related_ref", "field", "internal_value",
                 "broker_value", "detail", "notional", "price_impact"]
MATCH_COLUMNS = ["trade_id", "broker_ref", "match_method", "fields_differing"]


@dataclass
class ReconResult:
    matches: pd.DataFrame
    breaks: pd.DataFrame
    stats: dict = field(default_factory=dict)


def prices_equal(internal_price, broker_price, tolerance):
    """True where the relative difference is within tolerance (tolerance=0.0001 is 1bp)."""
    internal_price = np.asarray(internal_price, dtype=float)
    broker_price = np.asarray(broker_price, dtype=float)
    return np.abs(broker_price - internal_price) <= tolerance * np.abs(internal_price) + 1e-9


def _normalise_ids(s):
    s = s.astype(object).where(s.notna(), None)
    return s.map(lambda v: v.strip() if isinstance(v, str) and v.strip() else None)


def _fmt(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    if isinstance(v, (float, np.floating)):
        return f"{v:.4f}"
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def _describe(row, prefix):
    return (f"{row[prefix + 'side']} {int(row[prefix + 'quantity']):,} {row[prefix + 'instrument']} "
            f"@ {row[prefix + 'price']:.4f} {row[prefix + 'currency']}, "
            f"trade {_fmt(row[prefix + 'trade_date'])}, settle {_fmt(row[prefix + 'settle_date'])}")


def _break(break_type, trade_id, broker_ref, detail, field=None, internal_value=None,
           broker_value=None, notional=None, price_impact=None, related_ref=None):
    # related_ref: for a duplicate, the record it duplicates. The pipeline re-checks
    # that record on later days so the pair is still seen together.
    return dict(break_type=break_type, trade_id=trade_id, broker_ref=broker_ref,
                related_ref=related_ref, field=field,
                internal_value=internal_value, broker_value=broker_value, detail=detail,
                notional=notional, price_impact=price_impact)


# --- pass 1 --------------------------------------------------------------------
def find_duplicates(internal, broker):
    """Flag repeated keys so they can't match the same trade twice.

    The record with the lowest reference is kept for matching; every later copy
    is raised as a duplicate break and removed from the matching pool.
    """
    breaks = []

    internal = internal.sort_values("trade_id", kind="stable")
    dup_i = internal.duplicated("trade_id", keep="first")
    for row in internal[dup_i].to_dict("records"):
        breaks.append(_break("duplicate", row["trade_id"], None,
                             f"trade_id {row['trade_id']} booked more than once internally",
                             field="record", internal_value=row["trade_id"],
                             notional=row["quantity"] * row["price"]))
    internal = internal[~dup_i]

    broker = broker.sort_values("broker_ref", kind="stable")
    dup_ref = broker.duplicated("broker_ref", keep="first")
    for row in broker[dup_ref].to_dict("records"):
        breaks.append(_break("duplicate", row["trade_id"], row["broker_ref"],
                             f"broker_ref {row['broker_ref']} appears more than once in the broker file",
                             field="record", broker_value=row["broker_ref"],
                             notional=row["quantity"] * row["price"]))
    broker = broker[~dup_ref]

    has_id = broker["trade_id"].notna()
    dup_id = has_id & broker.duplicated("trade_id", keep="first")
    if dup_id.any():
        first = broker[has_id & ~dup_id].set_index("trade_id")
        for row in broker[dup_id].to_dict("records"):
            orig = first.loc[row["trade_id"]]
            same = all(orig[c] == row[c] for c in ["instrument", "side", "quantity", "settle_date"]) \
                and bool(prices_equal(orig["price"], row["price"], 1e-9))
            how = "identical economics" if same else "different economics"
            breaks.append(_break("duplicate", row["trade_id"], row["broker_ref"],
                                 f"trade_id {row['trade_id']} confirmed again on {row['broker_ref']} "
                                 f"(first on {orig['broker_ref']}, {how})",
                                 field="record", broker_value=row["broker_ref"],
                                 notional=row["quantity"] * row["price"],
                                 related_ref=orig["broker_ref"]))
        broker = broker[~dup_id]

    # A broker record re-sent with no trade_id cannot be caught by the rule above.
    # Two blank-id records with identical economics (to the 4th decimal of price)
    # are treated as one trade sent twice.
    blank = broker[broker["trade_id"].isna()]
    dup_econ = blank.duplicated(ECONOMIC_FIELDS, keep="first")
    if dup_econ.any():
        firsts = blank[~dup_econ].set_index(ECONOMIC_FIELDS)["broker_ref"]
        for row in blank[dup_econ].to_dict("records"):
            orig_ref = firsts.loc[tuple(row[c] for c in ECONOMIC_FIELDS)]
            breaks.append(_break("duplicate", None, row["broker_ref"],
                                 f"broker record {row['broker_ref']} has no trade_id and repeats "
                                 f"{orig_ref} exactly",
                                 field="record", broker_value=row["broker_ref"],
                                 notional=row["quantity"] * row["price"],
                                 related_ref=orig_ref))
        broker = broker.drop(index=blank[dup_econ].index)

    return internal, broker, breaks


# --- passes 2 and 4 share the field comparison ------------------------------------
def _compare_pairs(pairs, method, tolerance):
    """Compare each matched pair field by field; return match rows and field breaks."""
    if pairs.empty:
        return [], []
    differs = {}
    for f in COMPARED_FIELDS:
        if f == "price":
            differs[f] = ~prices_equal(pairs["i_price"], pairs["b_price"], tolerance)
        else:
            differs[f] = (pairs[f"i_{f}"] != pairs[f"b_{f}"]).to_numpy()

    matches, breaks = [], []
    for k, row in enumerate(pairs.to_dict("records")):
        fields = [f for f in COMPARED_FIELDS if differs[f][k]]
        matches.append(dict(trade_id=row["i_trade_id"], broker_ref=row["b_broker_ref"],
                            match_method=method, fields_differing=fields))
        notional = row["i_quantity"] * row["i_price"]
        for f in fields:
            iv, bv = row[f"i_{f}"], row[f"b_{f}"]
            impact = None
            if f == "price":
                pct = (bv - iv) / iv * 100
                impact = abs(bv - iv) * row["i_quantity"]
                detail = (f"price: booked {iv:.4f}, broker {bv:.4f} ({pct:+.3f}%, "
                          f"{impact:,.2f} {row['i_currency']} at stake)")
            elif f == "quantity":
                detail = f"quantity: booked {int(iv):,}, broker {int(bv):,}"
            elif f == "side":
                detail = f"side: booked {iv}, broker {bv}"
            elif f == "settle_date":
                detail = f"settle date: booked {_fmt(iv)}, broker {_fmt(bv)}"
            else:
                detail = f"currency: booked {iv}, broker {bv}"
            detail += f" [{row['i_instrument']}, {method} match]"
            breaks.append(_break(FIELD_TO_BREAK[f], row["i_trade_id"], row["b_broker_ref"], detail,
                                 field=f, internal_value=_fmt(iv), broker_value=_fmt(bv),
                                 notional=notional, price_impact=impact))
    return matches, breaks


def exact_match(internal, broker):
    """Join on trade_id. A pair on the same trade_id but a different instrument is
    rejected: that is a bad id, not the same trade, so both sides go on to the fuzzy pass."""
    i = internal.add_prefix("i_")
    b = broker[broker["trade_id"].notna()].add_prefix("b_")
    pairs = i.merge(b, left_on="i_trade_id", right_on="b_trade_id", how="inner")
    rejected = int((pairs["i_instrument"] != pairs["b_instrument"]).sum())
    pairs = pairs[pairs["i_instrument"] == pairs["b_instrument"]]
    return pairs, rejected


def fuzzy_match(internal, broker, tolerance):
    """Match leftovers on trade_date + instrument + side + quantity with price in tolerance.
    A pair is taken only when it is the single candidate on both sides."""
    i = internal.add_prefix("i_")
    b = broker.add_prefix("b_")
    cand = i.merge(b, left_on=[f"i_{k}" for k in FUZZY_KEYS],
                   right_on=[f"b_{k}" for k in FUZZY_KEYS], how="inner")
    cand = cand[prices_equal(cand["i_price"], cand["b_price"], tolerance)]
    if cand.empty:
        return cand, 0
    per_internal = cand.groupby("i_trade_id")["b_broker_ref"].transform("size")
    per_broker = cand.groupby("b_broker_ref")["i_trade_id"].transform("size")
    unique = (per_internal == 1) & (per_broker == 1)
    taken = cand[unique]

    # Tie-break for ambiguous groups: accept a pair only when each side is the
    # other's strictly closest price. Exact price ties stay unmatched.
    rest = cand[~unique].copy()
    if len(rest):
        rest["gap"] = (rest["b_price"] - rest["i_price"]).abs().round(6)
        best = _mutual_nearest(rest)
        taken = pd.concat([taken, best])
    ambiguous = int(cand["b_broker_ref"].nunique() - taken["b_broker_ref"].nunique())
    return taken, ambiguous


def _mutual_nearest(cand):
    def strict_min(group_col):
        g = cand.groupby(group_col)["gap"]
        is_min = cand["gap"] == g.transform("min")
        n_min = is_min.groupby(cand[group_col]).transform("sum")
        return is_min & (n_min == 1)
    ok = strict_min("i_trade_id") & strict_min("b_broker_ref")
    return cand[ok].drop(columns="gap")


def partial_match(internal, broker, tolerance):
    """Last-chance pass for leftovers whose broker trade_id is missing AND which carry
    a field error, so neither the exact nor the fuzzy pass can see them.

    A candidate pair must share trade_date, instrument, currency and settle_date and
    disagree on at most one of side / quantity / price. It is taken only when it is
    the single candidate on both sides. The differing field is then raised as a break.
    """
    i = internal.add_prefix("i_")
    b = broker.add_prefix("b_")
    keys = ["trade_date", "instrument", "currency", "settle_date"]
    cand = i.merge(b, left_on=[f"i_{k}" for k in keys], right_on=[f"b_{k}" for k in keys])
    if cand.empty:
        return cand
    n_diff = ((cand["i_side"] != cand["b_side"]).astype(int)
              + (cand["i_quantity"] != cand["b_quantity"]).astype(int)
              + (~prices_equal(cand["i_price"], cand["b_price"], tolerance)).astype(int))
    cand = cand[n_diff == 1]
    if cand.empty:
        return cand
    per_internal = cand.groupby("i_trade_id")["b_broker_ref"].transform("size")
    per_broker = cand.groupby("b_broker_ref")["i_trade_id"].transform("size")
    return cand[(per_internal == 1) & (per_broker == 1)]


# --- the whole run ---------------------------------------------------------------
def reconcile(internal, broker, tolerance):
    internal = internal.copy()
    broker = broker.copy()
    broker["trade_id"] = _normalise_ids(broker["trade_id"])
    stats = dict(internal_count=len(internal), broker_count=len(broker))

    internal, broker, breaks = find_duplicates(internal, broker)
    stats["duplicates_flagged"] = len(breaks)

    pairs, stats["exact_rejected"] = exact_match(internal, broker)
    exact_rows, exact_breaks = _compare_pairs(pairs, "exact", tolerance)
    breaks += exact_breaks
    matched_i = set(pairs["i_trade_id"])
    matched_b = set(pairs["b_broker_ref"])

    left_i = internal[~internal["trade_id"].isin(matched_i)]
    left_b = broker[~broker["broker_ref"].isin(matched_b)]
    fpairs, stats["fuzzy_ambiguous"] = fuzzy_match(left_i, left_b, tolerance)
    fuzzy_rows, fuzzy_breaks = _compare_pairs(fpairs, "fuzzy", tolerance)
    breaks += fuzzy_breaks
    matched_i |= set(fpairs["i_trade_id"]) if len(fpairs) else set()
    matched_b |= set(fpairs["b_broker_ref"]) if len(fpairs) else set()

    left_i = internal[~internal["trade_id"].isin(matched_i)]
    left_b = broker[~broker["broker_ref"].isin(matched_b)]
    ppairs = partial_match(left_i, left_b, tolerance)
    partial_rows, partial_breaks = _compare_pairs(ppairs, "partial", tolerance)
    breaks += partial_breaks
    if len(ppairs):
        matched_i |= set(ppairs["i_trade_id"])
        matched_b |= set(ppairs["b_broker_ref"])

    rest_i = internal[~internal["trade_id"].isin(matched_i)]
    rest_b = broker[~broker["broker_ref"].isin(matched_b)]
    for row in rest_i.add_prefix("i_").to_dict("records"):
        breaks.append(_break("missing_at_broker", row["i_trade_id"], None,
                             f"no broker record for {_describe(row, 'i_')}",
                             field="record", internal_value=row["i_trade_id"],
                             notional=row["i_quantity"] * row["i_price"]))
    for row in rest_b.add_prefix("b_").to_dict("records"):
        claimed = f" (broker quoted trade_id {row['b_trade_id']})" if row["b_trade_id"] else ""
        breaks.append(_break("missing_internally", row["b_trade_id"], row["b_broker_ref"],
                             f"no internal booking for {_describe(row, 'b_')}{claimed}",
                             field="record", broker_value=row["b_broker_ref"],
                             notional=row["b_quantity"] * row["b_price"]))

    matches = pd.DataFrame(exact_rows + fuzzy_rows + partial_rows, columns=MATCH_COLUMNS)

    # A duplicate break names the internal trade its original record matched, so a
    # duplicate that stays open keeps that trade in tomorrow's population.
    ref_to_trade = dict(zip(matches["broker_ref"], matches["trade_id"]))
    for brk in breaks:
        if brk["break_type"] == "duplicate" and brk["related_ref"] in ref_to_trade:
            brk["trade_id"] = ref_to_trade[brk["related_ref"]]

    stats.update(
        exact_matches=len(exact_rows),
        fuzzy_matches=len(fuzzy_rows),
        partial_matches=len(partial_rows),
        clean_matches=int((matches["fields_differing"].map(len) == 0).sum()) if len(matches) else 0,
        unmatched_internal=len(rest_i),
        unmatched_broker=len(rest_b),
    )
    return ReconResult(matches, pd.DataFrame(breaks, columns=BREAK_COLUMNS), stats)
