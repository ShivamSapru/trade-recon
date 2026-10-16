"""Hand-made trades for the unit tests."""
from datetime import date

import pandas as pd

D1 = date(2026, 9, 14)
D2 = date(2026, 9, 15)
TOL = 0.0001


def internal_trade(trade_id, instrument="AAPL", side="BUY", quantity=1000, price=100.00,
                   currency="USD", trade_date=D1, settle_date=D2, counterparty="Northgate Brokerage"):
    return dict(trade_id=trade_id, trade_date=trade_date, instrument=instrument, side=side,
                quantity=quantity, price=price, currency=currency, counterparty=counterparty,
                settle_date=settle_date)


def broker_trade(broker_ref, trade_id, **overrides):
    """A broker record that agrees with internal_trade(trade_id) unless overridden."""
    base = internal_trade(trade_id or "x")
    base.pop("counterparty")
    base["trade_id"] = trade_id
    base.update(overrides)
    base["broker_ref"] = broker_ref
    return base


def frames(internal_rows, broker_rows):
    return pd.DataFrame(internal_rows), pd.DataFrame(broker_rows)


def book(n=5):
    """A small clean book: n trades in different instruments with different sizes."""
    names = ["AAPL", "MSFT", "NVDA", "JPM", "KO", "XOM", "PG", "V"]
    return [internal_trade(f"T{i}", instrument=names[i], quantity=100 * (i + 1), price=50.0 + i)
            for i in range(n)]


def mirror(internal_rows):
    return [broker_trade(f"B{i}", r["trade_id"], instrument=r["instrument"], quantity=r["quantity"],
                         price=r["price"]) for i, r in enumerate(internal_rows)]
