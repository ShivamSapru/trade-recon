"""One test per break type, plus the behaviour of each matching pass."""
from helpers import D2, TOL, book, broker_trade, frames, internal_trade, mirror

from recon.matcher import prices_equal, reconcile


def run(internal_rows, broker_rows, tol=TOL):
    i, b = frames(internal_rows, broker_rows)
    return reconcile(i, b, tol)


def types(result):
    return sorted(result.breaks["break_type"])


# --- clean case --------------------------------------------------------------------
def test_identical_files_have_no_breaks():
    trades = book()
    r = run(trades, mirror(trades))
    assert r.breaks.empty
    assert r.stats["exact_matches"] == 5 and r.stats["clean_matches"] == 5


# --- the seven break types -----------------------------------------------------------
def test_missing_at_broker():
    trades = book()
    r = run(trades, mirror(trades)[1:])
    assert types(r) == ["missing_at_broker"]
    assert r.breaks.iloc[0]["trade_id"] == "T0"


def test_missing_internally():
    trades = book()
    ghost = broker_trade("B99", None, instrument="TSLA", quantity=700, price=330.0)
    r = run(trades, mirror(trades) + [ghost])
    assert types(r) == ["missing_internally"]
    assert r.breaks.iloc[0]["broker_ref"] == "B99"


def test_quantity_mismatch():
    trades = book()
    brk = mirror(trades)
    brk[2]["quantity"] = 3000          # booked 300
    r = run(trades, brk)
    assert types(r) == ["quantity_mismatch"]
    row = r.breaks.iloc[0]
    assert (row["internal_value"], row["broker_value"]) == ("300", "3000")


def test_price_mismatch_beyond_tolerance():
    trades = [internal_trade("T1", price=101.25)]
    r = run(trades, [broker_trade("B1", "T1", price=101.52)])   # cents digits swapped
    assert types(r) == ["price_mismatch"]


def test_side_mismatch():
    trades = book()
    brk = mirror(trades)
    brk[0]["side"] = "SELL"
    r = run(trades, brk)
    assert types(r) == ["side_mismatch"]


def test_duplicate_confirmation():
    trades = book()
    brk = mirror(trades) + [dict(mirror(trades)[3], broker_ref="B98")]
    r = run(trades, brk)
    assert types(r) == ["duplicate"]
    row = r.breaks.iloc[0]
    assert row["broker_ref"] == "B98" and row["related_ref"] == "B3"
    assert r.stats["exact_matches"] == 5          # the original still matches once


def test_settlement_date_mismatch():
    trades = book()
    brk = mirror(trades)
    brk[4]["settle_date"] = D2.replace(day=16)    # T+1 booked, T+2 confirmed
    r = run(trades, brk)
    assert types(r) == ["settlement_date_mismatch"]


# --- tolerance -----------------------------------------------------------------------
def test_price_within_tolerance_is_not_a_break():
    trades = [internal_trade("T1", price=100.00)]
    r = run(trades, [broker_trade("B1", "T1", price=100.005)])  # 0.5bp
    assert r.breaks.empty


def test_tolerance_is_a_parameter():
    trades = [internal_trade("T1", price=100.00)]
    brk = [broker_trade("B1", "T1", price=100.005)]
    assert run(trades, brk, tol=0.0).breaks["break_type"].tolist() == ["price_mismatch"]
    assert run(trades, brk, tol=0.0001).breaks.empty


def test_prices_equal_boundary():
    assert prices_equal(100.0, 100.01, 0.0001)        # exactly 1bp
    assert not prices_equal(100.0, 100.0101, 0.0001)


# --- duplicate check runs first ---------------------------------------------------------
def test_duplicate_trade_id_cannot_match_twice():
    trades = [internal_trade("T1")]
    brk = [broker_trade("B1", "T1"), broker_trade("B2", "T1")]
    r = run(trades, brk)
    assert len(r.matches) == 1 and r.matches.iloc[0]["broker_ref"] == "B1"
    assert types(r) == ["duplicate"]


def test_duplicate_without_trade_id_detected_on_economics():
    trades = [internal_trade("T1")]
    brk = [broker_trade("B1", None), broker_trade("B2", None)]
    r = run(trades, brk)
    assert types(r) == ["duplicate"]
    assert r.stats["fuzzy_matches"] == 1


def test_internal_double_booking_is_a_duplicate():
    trades = [internal_trade("T1"), internal_trade("T1")]
    r = run(trades, [broker_trade("B1", "T1")])
    assert types(r) == ["duplicate"]


# --- fuzzy pass --------------------------------------------------------------------------
def test_blank_trade_id_is_matched_fuzzily():
    trades = book()
    brk = mirror(trades)
    brk[1]["trade_id"] = None
    brk[2]["trade_id"] = "T2-typo"
    r = run(trades, brk)
    assert r.breaks.empty
    assert r.stats["exact_matches"] == 3 and r.stats["fuzzy_matches"] == 2
    assert set(r.matches.loc[r.matches.match_method == "fuzzy", "trade_id"]) == {"T1", "T2"}


def test_fuzzy_match_still_reports_field_differences():
    trades = [internal_trade("T1")]
    r = run(trades, [broker_trade("B1", None, settle_date=D2.replace(day=16))])
    assert types(r) == ["settlement_date_mismatch"]
    assert r.matches.iloc[0]["match_method"] == "fuzzy"


def test_fuzzy_ties_are_left_unmatched():
    """Two identical internal trades and one anonymous broker record: no safe choice."""
    trades = [internal_trade("T1"), internal_trade("T2")]
    r = run(trades, [broker_trade("B1", None)])
    assert r.stats["fuzzy_matches"] == 0
    assert types(r) == ["missing_at_broker", "missing_at_broker", "missing_internally"]


def test_fuzzy_mutual_nearest_price_breaks_a_tie():
    trades = [internal_trade("T1", price=100.000), internal_trade("T2", price=100.009)]
    brk = [broker_trade("B1", None, price=100.000), broker_trade("B2", None, price=100.009)]
    r = run(trades, brk)
    assert r.breaks.empty
    assert dict(zip(r.matches.trade_id, r.matches.broker_ref)) == {"T1": "B1", "T2": "B2"}


def test_same_trade_id_different_instrument_is_rejected():
    trades = [internal_trade("T1", instrument="AAPL")]
    r = run(trades, [broker_trade("B1", "T1", instrument="MSFT", quantity=999)])
    assert types(r) == ["missing_at_broker", "missing_internally"]


# --- partial pass ------------------------------------------------------------------------
def test_blank_id_and_quantity_error_found_by_partial_pass():
    trades = book()
    brk = mirror(trades)
    brk[3].update(trade_id=None, quantity=9999)
    r = run(trades, brk)
    assert types(r) == ["quantity_mismatch"]
    assert r.stats["partial_matches"] == 1


def test_partial_pass_needs_a_unique_candidate():
    trades = [internal_trade("T1", quantity=100), internal_trade("T2", quantity=200)]
    r = run(trades, [broker_trade("B1", None, quantity=300)])
    assert r.stats["partial_matches"] == 0
