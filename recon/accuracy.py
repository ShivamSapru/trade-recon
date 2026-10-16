"""Scores the detector against the answer key.

Unit of scoring: one distinct break (one break_key) over the whole period,
compared with one planted error.
  - missing_internally and duplicate plants are identified by broker_ref
    (there is no internal trade, or the trade itself is fine);
  - every other plant is identified by (break_type, trade_id).
True positive  = a planted error that the tool raised with the right type.
False negative = a planted error it never raised (or raised as another type).
False positive = a break the tool raised that matches no planted error.
"""
import pandas as pd

from .breaks import BREAK_TYPES

BY_REF = {"missing_internally", "duplicate"}


def _plant_key(row):
    ident = row["broker_ref"] if row["break_type"] in BY_REF else row["trade_id"]
    return f"{row['break_type']}|{ident}"


def distinct_breaks(all_breaks):
    """One row per break_key with the first and last day it was open."""
    g = all_breaks.groupby("break_key")
    first = g.first()[["break_type", "trade_id", "broker_ref", "detail"]]
    first["first_seen"] = g["run_date"].min()
    first["last_seen"] = g["run_date"].max()
    return first.reset_index()


def evaluate(answer_key, all_breaks, days):
    detected = distinct_breaks(all_breaks)
    detected["match_key"] = detected.apply(_plant_key, axis=1)
    plants = answer_key.copy()
    plants["match_key"] = plants.apply(_plant_key, axis=1)

    det_keys = set(detected["match_key"])
    plant_keys = set(plants["match_key"])
    plants["detected"] = plants["match_key"].isin(det_keys)
    detected["planted"] = detected["match_key"].isin(plant_keys)

    # Did the break also close on the day the broker's fix arrived?
    next_day = {d: days[i + 1] for i, d in enumerate(days[:-1])}
    last_seen = detected.groupby("match_key")["last_seen"].max()
    def closed_on_time(row):
        if not row["detected"]:
            return None
        ls = last_seen[row["match_key"]]
        if pd.isna(row["planned_resolution_date"]):
            return ls == days[-1]                       # never fixed: still open at the end
        return next_day.get(ls) == row["planned_resolution_date"]
    plants["lifecycle_ok"] = plants.apply(closed_on_time, axis=1)

    tp = int(plants["detected"].sum())
    fn = int((~plants["detected"]).sum())
    fp = int((~detected["planted"]).sum())

    by_type = []
    for t in BREAK_TYPES:
        p = plants[plants["break_type"] == t]
        d = detected[detected["break_type"] == t]
        t_tp = int(p["detected"].sum())
        t_fp = int((~d["planted"]).sum())
        by_type.append(dict(
            break_type=t, planted=len(p), detected=len(d), true_pos=t_tp,
            false_neg=len(p) - t_tp, false_pos=t_fp,
            recall=t_tp / len(p) if len(p) else None,
            precision=t_tp / (t_tp + t_fp) if (t_tp + t_fp) else None))

    lifecycle = plants["lifecycle_ok"].dropna()
    summary = dict(
        planted=len(plants), detected=len(detected), true_pos=tp, false_neg=fn, false_pos=fp,
        recall=tp / len(plants) if len(plants) else 0.0,
        precision=tp / (tp + fp) if (tp + fp) else 0.0,
        lifecycle_accuracy=float(lifecycle.mean()) if len(lifecycle) else None,
    )
    misses = plants[~plants["detected"]].drop(columns=["match_key"])
    false_positives = detected[~detected["planted"]].drop(columns=["match_key", "planted"])
    return summary, pd.DataFrame(by_type), misses, false_positives
