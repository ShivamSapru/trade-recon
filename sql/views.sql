-- Reporting views. The daily report reads only from these.

-- Open breaks with age in business days. Age counts reconciliation runs after
-- the break was first seen, so weekends and holidays never inflate it.
CREATE OR REPLACE VIEW v_open_breaks AS
SELECT b.*,
       (SELECT count(*)
          FROM recon_runs r
         WHERE r.run_date > b.first_seen_date
           AND r.run_date <= b.run_date)::int AS age_days
  FROM breaks b;

-- Headline numbers per run, including the change in open breaks since the previous run.
CREATE OR REPLACE VIEW v_daily_summary AS
SELECT r.run_date,
       r.internal_count                                                        AS trades_compared,
       r.broker_count                                                          AS broker_records,
       round(100.0 * (r.exact_matches + r.fuzzy_matches + r.partial_matches) / NULLIF(r.internal_count, 0), 2) AS match_rate_pct,
       round(100.0 * r.clean_matches / NULLIF(r.internal_count, 0), 2)         AS clean_match_rate_pct,
       r.open_breaks,
       r.open_breaks - LAG(r.open_breaks) OVER (ORDER BY r.run_date)           AS change_vs_prev,
       r.new_breaks,
       r.resolved_breaks
  FROM recon_runs r;

-- Count and share of open breaks for each of the seven types, zeros included.
CREATE OR REPLACE VIEW v_breaks_by_type AS
WITH types(break_type, sort_order) AS (
    VALUES ('missing_at_broker', 1), ('missing_internally', 2), ('quantity_mismatch', 3),
           ('price_mismatch', 4), ('side_mismatch', 5), ('duplicate', 6),
           ('settlement_date_mismatch', 7)
)
SELECT r.run_date,
       t.break_type,
       t.sort_order,
       count(b.break_id)::int AS open_count,
       round(100.0 * count(b.break_id)
             / NULLIF(sum(count(b.break_id)) OVER (PARTITION BY r.run_date), 0), 1) AS share_pct
  FROM recon_runs r
 CROSS JOIN types t
  LEFT JOIN breaks b ON b.run_date = r.run_date AND b.break_type = t.break_type
 GROUP BY r.run_date, t.break_type, t.sort_order;

-- Open breaks ranked for follow-up: severity, then age, then money at stake.
CREATE OR REPLACE VIEW v_top_breaks AS
SELECT ob.*,
       ROW_NUMBER() OVER (PARTITION BY ob.run_date
                          ORDER BY ob.severity DESC, ob.age_days DESC,
                                   ob.notional DESC NULLS LAST, ob.break_id) AS rank
  FROM v_open_breaks ob;

-- Open breaks grouped into ageing buckets, with empty buckets shown as zero.
CREATE OR REPLACE VIEW v_ageing AS
WITH buckets(bucket, sort_order) AS (
    VALUES ('0 days', 1), ('1 day', 2), ('2-3 days', 3), ('4+ days', 4)
),
aged AS (
    SELECT run_date,
           CASE WHEN age_days = 0 THEN '0 days'
                WHEN age_days = 1 THEN '1 day'
                WHEN age_days <= 3 THEN '2-3 days'
                ELSE '4+ days' END AS bucket
      FROM v_open_breaks
)
SELECT r.run_date, k.bucket, k.sort_order, count(a.bucket)::int AS open_count
  FROM recon_runs r
 CROSS JOIN buckets k
  LEFT JOIN aged a ON a.run_date = r.run_date AND a.bucket = k.bucket
 GROUP BY r.run_date, k.bucket, k.sort_order;

-- How hard the matcher had to work each day.
CREATE OR REPLACE VIEW v_matcher_health AS
SELECT r.run_date,
       r.exact_matches,
       r.fuzzy_matches,
       r.partial_matches,
       r.unmatched_internal,
       r.unmatched_broker,
       r.duplicates_flagged,
       round(100.0 * (r.fuzzy_matches + r.partial_matches)
             / NULLIF(r.exact_matches + r.fuzzy_matches + r.partial_matches, 0), 2) AS fallback_share_pct
  FROM recon_runs r;

-- Every distinct break the tool ever raised, with the first and last day it was open.
CREATE OR REPLACE VIEW v_detected_breaks AS
SELECT break_key, break_type, trade_id, broker_ref,
       min(run_date) AS first_seen, max(run_date) AS last_seen
  FROM breaks
 GROUP BY break_key, break_type, trade_id, broker_ref;

-- Planted errors joined to what the tool detected. Missing-internally and
-- duplicate plants are identified by broker_ref; all others by trade_id.
CREATE OR REPLACE VIEW v_planted_detection AS
SELECT a.*,
       d.break_key IS NOT NULL AS detected,
       d.first_seen,
       d.last_seen
  FROM answer_key a
  LEFT JOIN LATERAL (
        SELECT * FROM v_detected_breaks d
         WHERE d.break_type = a.break_type
           AND CASE WHEN a.break_type IN ('missing_internally', 'duplicate')
                    THEN d.broker_ref = a.broker_ref
                    ELSE d.trade_id = a.trade_id END
         ORDER BY d.first_seen
         LIMIT 1
  ) d ON true;
