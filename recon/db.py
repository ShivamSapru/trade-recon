"""Thin PostgreSQL layer: create the schema, bulk-load DataFrames, read query results."""
from decimal import Decimal

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

from . import config


def connect(url=None):
    return psycopg2.connect(url or config.DB_URL)


def run_sql_file(conn, name):
    with conn.cursor() as cur:
        cur.execute((config.SQL_DIR / name).read_text())
    conn.commit()


def init_db(conn):
    run_sql_file(conn, "schema.sql")
    run_sql_file(conn, "views.sql")


def insert_df(conn, table, df, columns):
    if df.empty:
        return 0
    frame = df[columns].astype(object).where(df[columns].notna(), None)
    rows = [tuple(r) for r in frame.itertuples(index=False, name=None)]
    with conn.cursor() as cur:
        execute_values(cur, f"INSERT INTO {table} ({', '.join(columns)}) VALUES %s", rows,
                       page_size=5000)
    return len(rows)


def read_df(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [c.name for c in cur.description]
        rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=cols)
    for c in df.columns:  # numeric columns come back as Decimal
        if len(df) and isinstance(df[c].dropna().iloc[0] if df[c].notna().any() else None, Decimal):
            df[c] = df[c].astype(float)
    return df
