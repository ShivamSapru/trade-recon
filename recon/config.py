"""Every tunable number lives here. Change a value and rerun; nothing else hard-codes these."""
import os
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SQL_DIR = ROOT / "sql"
REPORTS_DIR = ROOT / "reports"

DB_URL = os.environ.get("RECON_DB_URL", "postgresql://recon:recon@localhost:5433/recon")

# --- Synthetic data -------------------------------------------------------
SEED = 20261016                 # fixed seed: same seed, same data, same accuracy numbers
START_DATE = date(2026, 9, 14)  # a Monday; 20 business days runs to 2026-10-09
N_DAYS = 20
TRADES_PER_DAY = 2000

# Share of each day's trades that get a planted break. Each day's actual rate is
# jittered by +/-25%, which keeps it inside the 3-5% band from the plan.
ERROR_RATE = 0.04

# How planted breaks are spread across the types. Deliberately uneven, like real life.
ERROR_MIX = {
    "price_mismatch": 0.28,
    "quantity_mismatch": 0.18,
    "settlement_date_mismatch": 0.15,
    "missing_at_broker": 0.13,
    "missing_internally": 0.10,
    "duplicate": 0.09,
    "side_mismatch": 0.07,
}

# The broker's trade_id is unreliable on purpose, so the fuzzy matcher has work to do.
BLANK_ID_RATE = 0.03     # broker left trade_id empty
GARBLED_ID_RATE = 0.01   # broker mistyped trade_id

# Harmless price noise: the broker reports a slightly different average price
# (well inside tolerance). This is what the tolerance exists to absorb.
PRICE_NOISE_RATE = 0.10
PRICE_NOISE_BP = 0.3     # up to +/-0.3 basis points

# How many business days a planted break stays open before the broker fixes it.
# 0 means it is never fixed inside the window.
RESOLUTION_LAG_PROBS = {1: 0.55, 2: 0.22, 3: 0.08, 4: 0.04, 6: 0.03, 0: 0.08}

# --- Matching -------------------------------------------------------------
# Prices count as equal when |broker - internal| / internal <= PRICE_TOLERANCE.
# 0.0001 = 0.01% = 1 basis point.
PRICE_TOLERANCE = 0.0001

# --- Severity -------------------------------------------------------------
# A price break is severity 2 when the money at stake (|price diff| x quantity)
# reaches this amount in the trade currency, otherwise severity 1.
PRICE_IMPACT_HIGH = 1000.0
