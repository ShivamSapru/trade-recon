# Price tolerance sweep

Same data, same matcher; only `PRICE_TOLERANCE` changes. 1bp = 0.0001.

| Tolerance | Distinct breaks | Price breaks | False positives | Missed price plants | Recall | Precision |
|---|---|---|---|---|---|---|
| 0.0 (0bp) | 5520 | 4348 | 3920 | 1 | 99.56% | 28.99% |
| 1e-05 (0.1bp) | 4285 | 3118 | 2684 | 1 | 99.63% | 37.36% |
| 5e-05 (0.5bp) | 1606 | 458 | 1 | 0 | 99.88% | 99.94% |
| 0.0001 (1bp) | 1606 | 458 | 1 | 0 | 99.88% | 99.94% |
| 0.0005 (5bp) | 1566 | 417 | 3 | 41 | 97.26% | 99.81% |
| 0.001 (10bp) | 1505 | 353 | 7 | 105 | 93.22% | 99.53% |
| 0.005 (50bp) | 1180 | 28 | 17 | 429 | 72.37% | 98.56% |
