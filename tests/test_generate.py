"""The synthetic data must be repeatable and its answer key must be honest."""
import pytest

from recon import config
from recon.generate import generate, trading_days

DAYS = trading_days(n=4)


@pytest.fixture(scope="module")
def data():
    return generate(seed=7, days=DAYS, trades_per_day=500)


def test_same_seed_same_data(data):
    again = generate(seed=7, days=DAYS, trades_per_day=500)
    assert data.internal.equals(again.internal)
    assert data.broker.equals(again.broker)
    assert data.answer_key.equals(again.answer_key)


def test_different_seed_different_data(data):
    other = generate(seed=8, days=DAYS, trades_per_day=500)
    assert not data.broker.equals(other.broker)


def test_error_rate_is_between_3_and_5_percent(data):
    for day in DAYS:
        n = (data.answer_key["plant_date"] == day).sum()
        assert 0.03 * 500 <= n <= 0.05 * 500


def test_every_break_type_is_planted():
    big = generate(seed=1, days=DAYS, trades_per_day=2000)
    assert set(big.answer_key["break_type"]) == set(config.ERROR_MIX)


def test_field_plants_really_changed_the_broker_record(data):
    first = data.broker[data.broker["version"] == 1].set_index("broker_ref")
    fields = data.answer_key[data.answer_key["field"].isin(["side", "quantity", "settle_date"])]
    for row in fields.itertuples():
        value = first.loc[row.broker_ref, row.field]
        shown = value.isoformat() if hasattr(value, "isoformat") else str(value)
        assert shown == row.changed_value != row.original_value


def test_internal_trade_ids_are_unique(data):
    assert data.internal["trade_id"].is_unique


def test_garbled_ids_never_point_at_a_real_trade(data):
    real = set(data.internal["trade_id"])
    ids = data.broker["trade_id"].dropna()
    looks_wrong = ids[~ids.isin(real)]
    assert len(looks_wrong) > 0
    assert not looks_wrong.isin(real).any()


def test_fixes_arrive_on_the_planned_day(data):
    planned = data.answer_key.dropna(subset=["planned_resolution_date"])
    later = data.broker[data.broker["version"] > 1]
    for row in planned[planned["break_type"].isin(["price_mismatch", "duplicate"])].itertuples():
        fix = later[later["broker_ref"] == row.broker_ref]
        assert fix["statement_date"].tolist() == [row.planned_resolution_date]
