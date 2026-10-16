"""Breaks carrying over, ageing and resolving across days."""
from datetime import date

import pandas as pd
from helpers import D1, D2, broker_trade, internal_trade

from recon.pipeline import run_all

D3 = date(2026, 9, 16)


def broker_rows(*rows):
    return pd.DataFrame(rows)


def versioned(ref, trade_id, version, statement_date, status="NEW", **kw):
    r = broker_trade(ref, trade_id, **kw)
    r.update(version=version, statement_date=statement_date, record_status=status)
    return r


def test_break_carries_over_then_resolves_on_amend():
    internal = pd.DataFrame([internal_trade("T1", quantity=1000)])
    broker = broker_rows(
        versioned("B1", "T1", 1, D1, quantity=100),
        versioned("B1", "T1", 2, D3, "AMEND", quantity=1000),  # fixed two days later
    )
    res = run_all(internal, broker, [D1, D2, D3])
    day1, day2, day3 = (r.breaks for r in res)
    assert day1["break_type"].tolist() == ["quantity_mismatch"]
    assert day1.iloc[0]["status"] == "NEW"
    assert day2.iloc[0]["status"] == "OPEN"
    assert day2.iloc[0]["break_id"] == day1.iloc[0]["break_id"]
    assert day2.iloc[0]["first_seen_date"] == D1
    assert day3.empty and len(res[2].resolved) == 1


def test_late_confirmation_resolves_missing_at_broker():
    internal = pd.DataFrame([internal_trade("T1")])
    broker = broker_rows(versioned("B9", "T1", 1, D2))
    res = run_all(internal, broker, [D1, D2])
    assert res[0].breaks["break_type"].tolist() == ["missing_at_broker"]
    assert res[1].breaks.empty


def test_cancel_resolves_missing_internally():
    internal = pd.DataFrame([internal_trade("T1")])
    broker = broker_rows(
        versioned("B1", "T1", 1, D1),
        versioned("B2", None, 1, D1, instrument="TSLA", quantity=700, price=330.0),
        versioned("B2", None, 2, D2, "CANCEL", instrument="TSLA", quantity=700, price=330.0),
    )
    res = run_all(internal, broker, [D1, D2])
    assert res[0].breaks["break_type"].tolist() == ["missing_internally"]
    assert res[1].breaks.empty


def test_open_duplicate_without_trade_id_stays_a_duplicate():
    """Regression: the original record must be re-checked with its duplicate on later days."""
    internal = pd.DataFrame([internal_trade("T1")])
    broker = broker_rows(versioned("B1", None, 1, D1), versioned("B2", None, 1, D1))
    res = run_all(internal, broker, [D1, D2, D3])
    for r in res:
        assert r.breaks["break_type"].tolist() == ["duplicate"]


def test_amend_to_an_already_matched_trade_is_rechecked():
    """Regression: an AMEND on a clean-matched record must be paired again, not left orphaned."""
    internal = pd.DataFrame([internal_trade("T1", side="BUY")])
    broker = broker_rows(
        versioned("B1", "T1", 1, D1),
        versioned("B1", "T1", 2, D2, "AMEND", side="SELL"),
    )
    res = run_all(internal, broker, [D1, D2])
    assert res[0].breaks.empty
    assert res[1].breaks["break_type"].tolist() == ["side_mismatch"]
