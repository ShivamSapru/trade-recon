"""Full-period accuracy in memory, plus a database round trip when PostgreSQL is up."""
import pandas as pd
import pytest

from recon import config
from recon.accuracy import evaluate
from recon.generate import generate, trading_days
from recon.pipeline import run_all


@pytest.fixture(scope="module")
def full_run():
    data = generate()
    results = run_all(data.internal, data.broker, data.days)
    breaks = pd.concat([r.breaks for r in results if len(r.breaks)])
    return data, results, breaks


def test_accuracy_floor(full_run):
    """Guards against regressions: the README numbers come from this exact run."""
    data, _, breaks = full_run
    summary, _, _, _ = evaluate(data.answer_key, breaks, data.days)
    assert summary["recall"] >= 0.995
    assert summary["precision"] >= 0.995
    assert summary["lifecycle_accuracy"] >= 0.99


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_accuracy_holds_on_other_seeds(seed):
    """The matcher was tuned on the default seed; check it is not overfitted to it."""
    days = trading_days(n=10)
    data = generate(seed=seed, days=days)
    results = run_all(data.internal, data.broker, days)
    breaks = pd.concat([r.breaks for r in results if len(r.breaks)])
    summary, _, _, _ = evaluate(data.answer_key, breaks, days)
    assert summary["recall"] >= 0.99 and summary["precision"] >= 0.99


def _db_or_skip():
    psycopg2 = pytest.importorskip("psycopg2")
    try:
        return psycopg2.connect(config.DB_URL, connect_timeout=2)
    except psycopg2.OperationalError:
        pytest.skip("PostgreSQL not reachable (docker compose up -d)")


def test_database_views_agree_with_python():
    """Loads a small dataset into a scratch schema and checks the views."""
    from recon import db
    from recon.__main__ import BREAK_DB_COLUMNS, MATCH_DB_COLUMNS, RUN_COLUMNS

    conn = _db_or_skip()
    try:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS recon_test CASCADE; CREATE SCHEMA recon_test; "
                        "SET search_path TO recon_test")
        db.init_db(conn)
        days = trading_days(n=3)
        data = generate(seed=5, days=days, trades_per_day=300)
        db.insert_df(conn, "internal_trades", data.internal, list(data.internal.columns))
        db.insert_df(conn, "broker_trades", data.broker, list(data.broker.columns))
        db.insert_df(conn, "answer_key", data.answer_key, list(data.answer_key.columns))
        results = run_all(data.internal, data.broker, days)
        for r in results:
            db.insert_df(conn, "recon_runs", pd.DataFrame([r.run]), RUN_COLUMNS)
            db.insert_df(conn, "match_results", r.matches, MATCH_DB_COLUMNS)
            db.insert_df(conn, "breaks", r.breaks, BREAK_DB_COLUMNS)

        summary = db.read_df(conn, "SELECT * FROM v_daily_summary ORDER BY run_date")
        assert summary["open_breaks"].tolist() == [r.run["open_breaks"] for r in results]

        by_type = db.read_df(conn, "SELECT break_type, open_count FROM v_breaks_by_type WHERE run_date = %s",
                             (days[-1],))
        assert by_type["open_count"].sum() == len(results[-1].breaks)
        assert len(by_type) == 7

        ageing = db.read_df(conn, "SELECT sum(open_count) AS n FROM v_ageing WHERE run_date = %s", (days[-1],))
        assert int(ageing["n"][0]) == len(results[-1].breaks)

        breaks = pd.concat([r.breaks for r in results if len(r.breaks)])
        py, _, _, _ = evaluate(data.answer_key, breaks, days)
        sql = db.read_df(conn, "SELECT count(*) AS n FROM v_planted_detection WHERE detected")
        assert int(sql["n"][0]) == py["true_pos"]
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS recon_test CASCADE")
        conn.commit()
        conn.close()
