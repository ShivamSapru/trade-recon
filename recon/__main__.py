"""Command line entry point.

    python -m recon init-db      create tables and views (drops existing data)
    python -m recon generate     build synthetic data and load it into PostgreSQL
    python -m recon run          reconcile every day in order and store the results
    python -m recon report       write the daily break report (default: last day)
    python -m recon accuracy     score detected breaks against the answer key
    python -m recon sweep        show how the price tolerance moves the break count
    python -m recon all          init-db, generate, run, report, accuracy, sweep
"""
import argparse
import warnings
import time
from datetime import date

import pandas as pd

from . import config, db
from .accuracy import evaluate
from .generate import generate, trading_days
from .pipeline import run_all

# pandas 2.2 warns about concatenating frames whose related_ref column is all empty;
# the result is correct, the warning is noise.
warnings.simplefilter("ignore", FutureWarning)

RUN_COLUMNS = ["run_date", "tolerance", "internal_count", "broker_count", "exact_matches",
               "fuzzy_matches", "partial_matches", "clean_matches", "unmatched_internal",
               "unmatched_broker", "duplicates_flagged", "new_breaks", "resolved_breaks",
               "open_breaks"]
MATCH_DB_COLUMNS = ["run_date", "trade_id", "broker_ref", "match_method", "fields_differing"]
BREAK_DB_COLUMNS = ["run_date", "break_id", "break_key", "break_type", "severity", "trade_id",
                    "broker_ref", "related_ref", "field", "internal_value", "broker_value",
                    "detail", "notional", "first_seen_date", "status", "suggested_owner"]


def cmd_init_db(conn, args):
    db.init_db(conn)
    print("schema and views created")


def cmd_generate(conn, args):
    t = time.time()
    data = generate()
    with conn.cursor() as cur:
        cur.execute("TRUNCATE breaks, match_results, recon_runs, answer_key, broker_trades, "
                    "internal_trades RESTART IDENTITY")
    db.insert_df(conn, "internal_trades", data.internal, list(data.internal.columns))
    db.insert_df(conn, "broker_trades", data.broker, list(data.broker.columns))
    db.insert_df(conn, "answer_key", data.answer_key, list(data.answer_key.columns))
    conn.commit()
    print(f"generated {len(data.internal):,} internal trades, {len(data.broker):,} broker records "
          f"and {len(data.answer_key):,} planted breaks over {len(data.days)} days "
          f"(seed {config.SEED}, {time.time() - t:.1f}s)")


def _load_inputs(conn):
    internal = db.read_df(conn, "SELECT * FROM internal_trades")
    broker = db.read_df(conn, "SELECT * FROM broker_trades")
    answer_key = db.read_df(conn, "SELECT * FROM answer_key ORDER BY plant_id")
    if internal.empty:
        raise SystemExit("No trades loaded. Run `python -m recon generate` first.")
    days = sorted(internal["trade_date"].unique())
    return internal, broker, answer_key, days


def cmd_run(conn, args):
    t = time.time()
    internal, broker, _, days = _load_inputs(conn)
    results = run_all(internal, broker, days, args.tolerance)
    with conn.cursor() as cur:
        cur.execute("TRUNCATE breaks, match_results, recon_runs")
    for r in results:
        db.insert_df(conn, "recon_runs", pd.DataFrame([r.run]), RUN_COLUMNS)
        db.insert_df(conn, "match_results", r.matches, MATCH_DB_COLUMNS)
        db.insert_df(conn, "breaks", r.breaks, BREAK_DB_COLUMNS)
    conn.commit()
    for r in results:
        x = r.run
        print(f"{x['run_date']}  compared {x['internal_count']:>5,}  exact {x['exact_matches']:>5,}  "
              f"fuzzy {x['fuzzy_matches']:>3}  partial {x['partial_matches']:>2}  "
              f"new {x['new_breaks']:>3}  resolved {x['resolved_breaks']:>3}  open {x['open_breaks']:>3}")
    print(f"reconciled {len(results)} days in {time.time() - t:.1f}s (tolerance {results[0].run['tolerance']})")


def cmd_report(conn, args):
    from .report import build_report
    run_date = date.fromisoformat(args.date) if args.date else None
    print(f"report written to {build_report(conn, run_date)}")


def _pct(v):
    return "-" if v is None or v != v else f"{v * 100:.2f}%"


def cmd_accuracy(conn, args):
    _, _, answer_key, days = _load_inputs(conn)
    breaks = db.read_df(conn, "SELECT * FROM breaks")
    summary, by_type, misses, fps = evaluate(answer_key, breaks, days)
    sql_detected = db.read_df(conn, "SELECT count(*) AS n FROM v_planted_detection WHERE detected")["n"][0]

    lines = [
        "# Detector accuracy",
        "",
        f"Scored against the answer key of planted errors (seed {config.SEED}, "
        f"{len(days)} business days, price tolerance {config.PRICE_TOLERANCE}).",
        "",
        "| Planted | Detected breaks | True positives | Missed | False positives | Recall | Precision | Lifecycle accuracy |",
        "|---|---|---|---|---|---|---|---|",
        f"| {summary['planted']} | {summary['detected']} | {summary['true_pos']} | {summary['false_neg']} "
        f"| {summary['false_pos']} | {_pct(summary['recall'])} | {_pct(summary['precision'])} "
        f"| {_pct(summary['lifecycle_accuracy'])} |",
        "",
        f"SQL cross-check: `v_planted_detection` marks {sql_detected} plants as detected "
        f"(Python scorer: {summary['true_pos']}).",
        "",
        "*Lifecycle accuracy* = share of detected plants whose break closed on exactly the day the "
        "broker's fix arrived (or stayed open to the end when no fix was sent).",
        "",
        "## By break type",
        "",
        "| Type | Planted | Detected | TP | Missed | FP | Recall | Precision |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in by_type.itertuples():
        lines.append(f"| {r.break_type} | {r.planted} | {r.detected} | {r.true_pos} | {r.false_neg} "
                     f"| {r.false_pos} | {_pct(r.recall)} | {_pct(r.precision)} |")
    lines += ["", "## Missed plants", ""]
    if misses.empty:
        lines.append("None.")
    for r in misses.itertuples():
        lines.append(f"- {r.plant_date} {r.break_type} trade_id={r.trade_id} broker_ref={r.broker_ref} "
                     f"({r.field}: {r.original_value} -> {r.changed_value})")
    lines += ["", "## False positives", ""]
    if fps.empty:
        lines.append("None.")
    for r in fps.itertuples():
        lines.append(f"- {r.first_seen} {r.break_type} trade_id={r.trade_id} broker_ref={r.broker_ref}: {r.detail}")

    config.REPORTS_DIR.mkdir(exist_ok=True)
    path = config.REPORTS_DIR / "accuracy.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"planted {summary['planted']}, detected {summary['true_pos']} "
          f"(recall {_pct(summary['recall'])}), false positives {summary['false_pos']} "
          f"(precision {_pct(summary['precision'])}), lifecycle {_pct(summary['lifecycle_accuracy'])}")
    print(f"accuracy report written to {path}")


def cmd_sweep(conn, args):
    """Runs in memory only: nothing in the database changes."""
    internal, broker, answer_key, days = _load_inputs(conn)
    rows = []
    for tol in [0.0, 0.00001, 0.00005, 0.0001, 0.0005, 0.001, 0.005]:
        results = run_all(internal, broker, days, tol)
        breaks = pd.concat([r.breaks for r in results if len(r.breaks)])
        s, by_type, _, _ = evaluate(answer_key, breaks, days)
        price = by_type.set_index("break_type").loc["price_mismatch"]
        rows.append((tol, s["detected"], int(price["detected"]), s["false_pos"], int(price["false_neg"]),
                     s["recall"], s["precision"]))
        print(f"tolerance {tol:<8} breaks {s['detected']:>5}  price breaks {int(price['detected']):>5}  "
              f"FP {s['false_pos']:>5}  missed price plants {int(price['false_neg']):>3}  "
              f"recall {_pct(s['recall'])}  precision {_pct(s['precision'])}")
    lines = ["# Price tolerance sweep", "",
             "Same data, same matcher; only `PRICE_TOLERANCE` changes. 1bp = 0.0001.", "",
             "| Tolerance | Distinct breaks | Price breaks | False positives | Missed price plants | Recall | Precision |",
             "|---|---|---|---|---|---|---|"]
    for tol, n, p, fp, fn, rec, prec in rows:
        lines.append(f"| {tol} ({tol * 10000:g}bp) | {n} | {p} | {fp} | {fn} | {_pct(rec)} | {_pct(prec)} |")
    config.REPORTS_DIR.mkdir(exist_ok=True)
    (config.REPORTS_DIR / "tolerance_sweep.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"sweep written to {config.REPORTS_DIR / 'tolerance_sweep.md'}")


def cmd_all(conn, args):
    for step in (cmd_init_db, cmd_generate, cmd_run, cmd_report, cmd_accuracy, cmd_sweep):
        print(f"\n== {step.__name__[4:].replace('_', '-')} ==")
        step(conn, args)


COMMANDS = {"init-db": cmd_init_db, "generate": cmd_generate, "run": cmd_run,
            "report": cmd_report, "accuracy": cmd_accuracy, "sweep": cmd_sweep, "all": cmd_all}


def main():
    parser = argparse.ArgumentParser(prog="python -m recon", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--date", help="report date, YYYY-MM-DD (default: last run)")
    parser.add_argument("--tolerance", type=float, default=config.PRICE_TOLERANCE,
                        help=f"relative price tolerance (default {config.PRICE_TOLERANCE})")
    args = parser.parse_args()
    conn = db.connect()
    try:
        COMMANDS[args.command](conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
