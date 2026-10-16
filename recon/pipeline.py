"""Runs the reconciliation day by day across the whole period.

Each day's run compares:
  internal side: trades booked that day + older trades that still have an open break
                 + older trades whose broker record was re-sent today
  broker side:   the broker's latest version of every record that is on today's
                 statement, belongs to (or is duplicated by) an open break, or quotes an internal trade
                 in today's population. CANCELled records are dropped.

Re-checking open breaks every day is what lets a break resolve when the broker
sends an AMEND, a late confirmation or a CANCEL, and lets it age when they don't.
"""
from dataclasses import dataclass

import pandas as pd

from . import config
from .breaks import BreakIds, apply_lifecycle, score
from .matcher import reconcile


@dataclass
class DayResult:
    run_date: object
    run: dict              # one recon_runs row
    matches: pd.DataFrame  # match_results rows
    breaks: pd.DataFrame   # breaks rows (open at end of day)
    resolved: pd.DataFrame


def select_population(internal_all, broker_all, run_date, previous_open, paired=None):
    """paired: broker_ref -> trade_id for every pair matched on an earlier day. If the
    broker re-sends one of those records (AMEND/CANCEL), its trade is re-checked too."""
    paired = paired or {}
    open_tids = set(previous_open["trade_id"].dropna()) if previous_open is not None else set()
    open_refs = set()
    if previous_open is not None:
        open_refs = set(previous_open["broker_ref"].dropna()) | set(previous_open["related_ref"].dropna())

    known = broker_all[broker_all["statement_date"] <= run_date]
    latest = known.sort_values(["broker_ref", "version"]).drop_duplicates("broker_ref", keep="last")
    on_today = set(known.loc[known["statement_date"] == run_date, "broker_ref"])
    reopened = {paired[r] for r in on_today if r in paired}

    internal = internal_all[(internal_all["trade_date"] == run_date)
                            | internal_all["trade_id"].isin(open_tids | reopened)]
    wanted = (latest["broker_ref"].isin(on_today | open_refs)
              | latest["trade_id"].isin(set(internal["trade_id"])))
    broker = latest[wanted & (latest["record_status"] != "CANCEL")]
    return internal.reset_index(drop=True), broker.reset_index(drop=True)


def run_all(internal_all, broker_all, days, tolerance=None):
    tolerance = config.PRICE_TOLERANCE if tolerance is None else tolerance
    ids = BreakIds()
    previous_open = None
    paired = {}
    results = []
    for day in days:
        internal, broker = select_population(internal_all, broker_all, day, previous_open, paired)
        res = reconcile(internal, broker, tolerance)
        today, resolved = apply_lifecycle(score(res.breaks), previous_open, day, ids)
        run = dict(run_date=day, tolerance=tolerance, **{
            k: res.stats[k] for k in ["internal_count", "broker_count", "exact_matches",
                                      "fuzzy_matches", "partial_matches", "clean_matches", "unmatched_internal",
                                      "unmatched_broker", "duplicates_flagged"]},
            new_breaks=int((today["status"] == "NEW").sum()),
            resolved_breaks=len(resolved),
            open_breaks=len(today))
        matches = res.matches.assign(run_date=day)
        results.append(DayResult(day, run, matches, today, resolved))
        previous_open = today
        paired.update(zip(res.matches["broker_ref"], res.matches["trade_id"]))
    return results
