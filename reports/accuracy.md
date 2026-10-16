# Detector accuracy

Scored against the answer key of planted errors (seed 20261016, 20 business days, price tolerance 0.0001).

| Planted | Detected breaks | True positives | Missed | False positives | Recall | Precision | Lifecycle accuracy |
|---|---|---|---|---|---|---|---|
| 1607 | 1606 | 1605 | 2 | 1 | 99.88% | 99.94% | 100.00% |

SQL cross-check: `v_planted_detection` marks 1605 plants as detected (Python scorer: 1605).

*Lifecycle accuracy* = share of detected plants whose break closed on exactly the day the broker's fix arrived (or stayed open to the end when no fix was sent).

## By break type

| Type | Planted | Detected | TP | Missed | FP | Recall | Precision |
|---|---|---|---|---|---|---|---|
| missing_at_broker | 202 | 201 | 201 | 1 | 0 | 99.50% | 100.00% |
| missing_internally | 130 | 129 | 129 | 1 | 0 | 99.23% | 100.00% |
| quantity_mismatch | 302 | 302 | 302 | 0 | 0 | 100.00% | 100.00% |
| price_mismatch | 457 | 458 | 457 | 0 | 1 | 100.00% | 99.78% |
| side_mismatch | 145 | 145 | 145 | 0 | 0 | 100.00% | 100.00% |
| duplicate | 129 | 129 | 129 | 0 | 0 | 100.00% | 100.00% |
| settlement_date_mismatch | 242 | 242 | 242 | 0 | 0 | 100.00% | 100.00% |

## Missed plants

- 2026-09-18 missing_internally trade_id=None broker_ref=BR20260918002002 (record: None -> SELL 2000 MSFT)
- 2026-09-18 missing_at_broker trade_id=T20260918-01375 broker_ref=None (record: None -> None)

## False positives

- 2026-09-18 price_mismatch trade_id=T20260918-01375 broker_ref=BR20260918002002: price: booked 457.2951, broker 456.0068 (-0.282%, 2,576.60 USD at stake) [MSFT, partial match]
