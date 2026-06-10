# Analyst memo: Kalshi crypto hourly threshold favorite-longshot bias

## Verdict

**WEAKEN.** Do not kill, but do not call the original candidate confirmed.

The original taker rule is positive after estimated one-way taker fees and survives event-clustered standard errors: **+2.94c per contract, n=611, 478 events, cluster SE 1.12c, t=2.62**. It also remains positive under a 2x fee stress: **+2.25c**. The breakpoints are:

- The favorite leg is timing-specific. The 65-75c favorite band works at T-15, but nearby entry minutes are weak or negative.
- The original 2-5c longshot leg is positive overall, but the May split almost disappears.
- ETH support is small; June is not interpretable because only 8 total June-settled events are in the file.
- Maker results are conditional on fill. The panel has quote/volume snapshots, not queue position or displayed size.

The candidate survives only as a narrower, more explicit, pre-registered rule: **buy upper favorites at T-15 and short wider longshots at T-5, with positive reported volume and at most one threshold per event-side.**

## Data and method

Input file: `crypto_panel_nontest.parquet`

Panel shape: **863,137 rows**, **879 events**, **20,403 threshold contracts**. Coverage is April 2, 2026 through June 1, 2026 by settlement timestamp. Event counts by settlement month:

| Month | BTC events | ETH events |
|---|---:|---:|
| 2026-04 | 328 | 79 |
| 2026-05 | 364 | 100 |
| 2026-06 | 5 | 3 |

I used `close_utc = date + (hour + 4) hours`; snapshots then span T-59 through T-0. Payout is `1` for `result == "yes"`, otherwise `0`.

P&L definitions, in dollars per contract:

- Buy YES: `outcome - entry_price`
- Sell YES: `entry_price - outcome`
- Taker fee: `0.07 * p * (1 - p)` per entry fill. This is 1.75c at 50c, matching the stated 3.5c round-trip.
- Maker fee: `0`

All effect sizes below are cents per contract. Standard errors are clustered by `event_id`, using event-level residual sums so correlated thresholds in the same event do not get naive independent-trade treatment.

The `volume` field is treated only as a fill proxy. It is not enough to prove fill size or queue priority. Positive volume is required in the frozen rule.

## Original candidate, taker implementation

Original candidate tested exactly:

- Favorite leg: at T-15, buy YES at `yes_ask` if ask is 0.65-0.75.
- Longshot leg: at T-5, sell YES at `yes_bid` if bid is 0.02-0.05.
- Hold to settlement.

| Slice | n | Events | Gross | Fee | Net | Cluster SE | t |
|---|---:|---:|---:|---:|---:|---:|---:|
| Combined | 611 | 478 | 3.63 | 0.69 | **2.94** | 1.12 | 2.62 |
| Buy 65-75c favorites | 240 | 238 | 6.69 | 1.45 | **5.24** | 2.70 | 1.94 |
| Sell 2-5c longshots | 371 | 357 | 1.66 | 0.20 | **1.45** | 0.60 | 2.43 |

Month-by-month, original candidate:

| Month | Slice | n | Events | Net | Cluster SE | t |
|---|---|---:|---:|---:|---:|---:|
| 2026-04 | Combined | 302 | 227 | **4.42** | 1.45 | 3.04 |
| 2026-05 | Combined | 305 | 248 | **1.99** | 1.69 | 1.18 |
| 2026-06 | Combined | 4 | 3 | **-36.27** | 16.94 | -2.14 |
| 2026-04 | Buy favorite | 117 | 116 | **7.06** | 3.75 | 1.88 |
| 2026-05 | Buy favorite | 121 | 120 | **4.80** | 3.84 | 1.25 |
| 2026-06 | Buy favorite | 2 | 2 | **-74.86** | 1.45 | -51.61 |
| 2026-04 | Sell longshot | 185 | 179 | **2.75** | 0.07 | 38.50 |
| 2026-05 | Sell longshot | 184 | 176 | **0.14** | 1.20 | 0.12 |
| 2026-06 | Sell longshot | 2 | 2 | **2.33** | 0.47 | 4.99 |

Asset split, original candidate:

| Asset | Slice | n | Events | Net | Cluster SE | t |
|---|---|---:|---:|---:|---:|---:|
| BTC | Combined | 571 | 439 | **2.73** | 1.18 | 2.31 |
| ETH | Combined | 40 | 39 | **5.98** | 3.39 | 1.76 |
| BTC | Buy favorite | 227 | 225 | **4.77** | 2.80 | 1.70 |
| ETH | Buy favorite | 13 | 13 | **13.45** | 10.23 | 1.31 |
| BTC | Sell longshot | 344 | 330 | **1.38** | 0.64 | 2.14 |
| ETH | Sell longshot | 27 | 27 | **2.38** | 0.14 | 16.54 |

Fill proxy, original candidate:

| Slice | Positive volume | Median volume | p10 volume | p90 volume | Median spread |
|---|---:|---:|---:|---:|---:|
| Buy favorite | 99.6% | 3,428 | 605 | 10,359 | 2c |
| Sell longshot | 97.0% | 2,163 | 276 | 6,711 | 1c |

Requiring volume thresholds did not kill the original taker rule:

| Volume filter | Combined n | Events | Net | Cluster SE | t |
|---|---:|---:|---:|---:|---:|
| none | 611 | 478 | **2.94** | 1.12 | 2.62 |
| `volume > 0` | 599 | 468 | **2.91** | 1.14 | 2.55 |
| `volume >= 100` | 576 | 450 | **2.86** | 1.16 | 2.46 |
| `volume >= 1000` | 468 | 386 | **3.30** | 1.35 | 2.44 |

## Sensitivity tests

### Entry timing, original bands

Favorite leg, buy ask 65-75c:

| Entry | n | Events | Net | Cluster SE | t | Apr net | May net |
|---|---:|---:|---:|---:|---:|---:|---:|
| T-5 | 126 | 126 | -0.33 | 4.08 | -0.08 | -0.47 | 0.54 |
| T-10 | 202 | 201 | 2.78 | 3.04 | 0.91 | -0.68 | 6.50 |
| T-15 | 240 | 238 | **5.24** | 2.70 | 1.94 | 7.06 | 4.80 |
| T-20 | 278 | 276 | 1.44 | 2.65 | 0.55 | 4.29 | -0.69 |
| T-25 | 305 | 301 | -0.82 | 2.64 | -0.31 | 3.89 | -5.48 |
| T-30 | 334 | 328 | -3.60 | 2.59 | -1.39 | 0.33 | -7.90 |
| T-40 | 421 | 408 | -0.25 | 2.25 | -0.11 | 5.07 | -6.07 |
| T-50 | 462 | 438 | -2.27 | 2.19 | -1.03 | 4.86 | -8.18 |

Longshot leg, sell bid 2-5c:

| Entry | n | Events | Net | Cluster SE | t | Apr net | May net |
|---|---:|---:|---:|---:|---:|---:|---:|
| T-1 | 125 | 125 | 0.55 | 1.36 | 0.41 | -0.10 | 1.18 |
| T-2 | 200 | 200 | 2.92 | 0.07 | 40.33 | 2.85 | 2.99 |
| T-3 | 252 | 248 | 0.44 | 0.96 | 0.46 | 2.03 | -0.90 |
| T-5 | 371 | 357 | **1.45** | 0.60 | 2.43 | 2.75 | 0.14 |
| T-10 | 568 | 511 | 0.82 | 0.58 | 1.43 | 1.02 | 0.60 |
| T-15 | 784 | 622 | -0.68 | 0.72 | -0.95 | -1.07 | -0.27 |
| T-20 | 999 | 688 | 0.48 | 0.56 | 0.86 | 0.68 | 0.20 |
| T-30 | 1,241 | 734 | 0.70 | 0.50 | 1.41 | 0.02 | 1.38 |
| T-40 | 1,298 | 738 | 1.39 | 0.45 | 3.12 | 1.02 | 1.76 |

Conclusion: the favorite leg is the fragile part. T-15 is not just an arbitrary timestamp; it is where the effect concentrates. Longshots are more robust to timing, but the exact T-5 original band is not month-stable.

### Price-band edges

Favorite leg at T-15, taker buy at ask:

| Band | n | Events | Net | Cluster SE | Apr net | May net |
|---|---:|---:|---:|---:|---:|---:|
| 60-70c | 193 | 192 | 3.13 | 3.29 | 2.09 | 4.96 |
| 60-75c | 317 | 312 | 4.95 | 2.42 | 6.24 | 5.10 |
| 65-70c | 116 | 116 | 2.51 | 4.19 | 0.92 | 4.22 |
| 65-75c | 240 | 238 | **5.24** | 2.70 | 7.06 | 4.80 |
| 68-75c | 181 | 181 | **6.28** | 2.99 | 10.00 | 4.48 |
| 70-75c | 149 | 149 | **7.91** | 3.16 | 12.03 | 6.32 |
| 65-80c | 357 | 348 | 4.16 | 2.19 | 6.00 | 3.24 |
| 70-80c | 266 | 264 | 5.29 | 2.38 | 8.30 | 3.60 |

The favorite edge strengthens in the upper half of the prior band. The lower 65-70c segment is weak.

Longshot leg at T-5, taker sell at bid:

| Band | n | Events | Net | Cluster SE | Apr net | May net |
|---|---:|---:|---:|---:|---:|---:|
| 1-3c | 487 | 453 | 1.02 | 0.36 | 1.63 | 0.34 |
| 1-5c | 596 | 530 | 1.09 | 0.47 | 2.05 | 0.09 |
| 2-4c | 321 | 314 | 1.58 | 0.54 | 2.45 | 0.69 |
| 2-5c | 371 | 357 | **1.45** | 0.60 | 2.75 | 0.14 |
| 2-6c | 405 | 386 | 1.31 | 0.65 | 2.45 | 0.16 |
| 2-8c | 478 | 448 | **1.76** | 0.62 | 2.35 | 1.15 |
| 3-5c | 214 | 211 | 1.62 | 0.93 | 3.48 | -0.09 |
| 4-8c | 216 | 214 | 2.10 | 1.21 | 2.51 | 1.70 |

The original 2-5c upper edge is too narrow if the objective is month stability. The 2-8c band is more stable across April and May.

### Fee sensitivity

Original candidate:

| Fee multiplier | Net | Cluster SE | t |
|---:|---:|---:|---:|
| 0x | 3.63 | 1.12 | 3.23 |
| 1x | **2.94** | 1.12 | 2.62 |
| 2x | **2.25** | 1.12 | 2.00 |
| 3x | 1.55 | 1.12 | 1.39 |

Sharpened capped rule, defined below:

| Fee multiplier | Net | Cluster SE | t |
|---:|---:|---:|---:|
| 1x | **3.27** | 0.92 | 3.56 |
| 2x | **2.72** | 0.92 | 2.97 |
| 3x | **2.17** | 0.92 | 2.37 |

### Same-event dependence

Original taker rule has 611 trades but only 478 event clusters. 117 events have both a favorite and longshot leg. Event-clustered SE is therefore the right uncertainty measure. Side-level event P&L correlation in events with both legs was **-0.04**, so the two legs were not strongly positively dependent in sample.

For the sharpened all-eligible version, 95 of 502 traded events had both sides and side-level event P&L correlation was **-0.08**. Capping to one threshold per event-side reduced trades from 627 to 597 with little loss of edge: **+3.27c vs +3.22c**.

## Frozen pre-registration spec

Use this exact spec for the next out-of-sample test. It is the single combined rule I would carry forward.

### Universe

Kalshi BTC and ETH hourly threshold ladders with fields matching this panel. Trade only contracts with non-missing tradable quote on the specified side, non-crossed market (`yes_ask >= yes_bid` when both exist), and `volume > 0` at the entry snapshot.

### Clock

For each event, define settlement close as:

`close_utc = date + (hour + 4) hours`

Entry snapshots are exact integer minutes before close. If the exact minute row is missing, skip that leg for that event.

### Taker variant

At most one favorite and one longshot trade per event.

1. **Favorite leg**
   - Entry time: T-15.
   - Signal: `yes_ask` is in **[0.70, 0.75]**, inclusive.
   - Selection if multiple thresholds qualify in the same event: choose the contract with `yes_ask` closest to **0.72**; tie-break to lower ask.
   - Action: buy 1 YES at `yes_ask`.
   - Exit: hold to settlement.

2. **Longshot leg**
   - Entry time: T-5.
   - Signal: `yes_bid` is in **[0.02, 0.08]**, inclusive.
   - Selection if multiple thresholds qualify in the same event: choose the contract with the highest `yes_bid`; tie-break lexically by `threshold_ticker`.
   - Action: sell 1 YES at `yes_bid`.
   - Exit: hold to settlement.

3. **Cost hurdle**
   - Use one-way taker fee `0.07 * p * (1 - p)` per entry fill.
   - In this sample, average fee was about **1.39c** on favorite buys and **0.26c** on longshot sells.
   - The rule must remain positive under a 2x fee stress before promotion to production sizing.

Expected in-sample net for this exact capped taker rule:

| Filter | n | Events | Net | Cluster SE | t |
|---|---:|---:|---:|---:|---:|
| Including zero-volume rows | 597 | 502 | **3.27c** | 0.92c | 3.56 |
| Frozen spec, `volume > 0` | 587 | 492 | **3.24c** | 0.93c | 3.48 |

Frozen-spec side breakdown with `volume > 0`:

| Leg | n | Events | Net | Cluster SE | t |
|---|---:|---:|---:|---:|---:|
| Buy 70-75c favorites | 148 | 148 | **7.78c** | 3.18c | 2.44 |
| Sell 2-8c longshots | 439 | 439 | **1.72c** | 0.68c | 2.54 |

Frozen-spec month split with `volume > 0`:

| Month | n | Events | Net | Cluster SE | t |
|---|---:|---:|---:|---:|---:|
| 2026-04 | 282 | 228 | **4.66c** | 1.17c | 3.97 |
| 2026-05 | 300 | 260 | **2.43c** | 1.40c | 1.73 |
| 2026-06 | 5 | 4 | **-27.89c** | 15.62c | -1.79 |

The June row is a warning flag, not a stable estimate.

Frozen-spec asset split with `volume > 0`:

| Asset | n | Events | Net | Cluster SE | t |
|---|---:|---:|---:|---:|---:|
| BTC | 556 | 461 | **2.96c** | 0.98c | 3.03 |
| ETH | 31 | 31 | **8.28c** | 1.71c | 4.85 |

### Maker variant

Use the same event cap, volume filter, and settlement hold. Results are conditional on actual fills.

1. Favorite leg: at T-15, post a buy YES limit at `yes_bid` if `yes_bid` is in **[0.70, 0.75]**. If multiple thresholds qualify, choose the bid closest to **0.72**, tie-break to lower bid.
2. Longshot leg: at T-5, post a sell YES limit at `yes_ask` if `yes_ask` is in **[0.02, 0.08]**. If multiple thresholds qualify, choose the highest ask, tie-break lexically by `threshold_ticker`.
3. Cancel any unfilled order after the entry minute. Hold filled positions to settlement.
4. Fee hurdle is zero in this analysis, but fill rate and adverse selection must be measured separately before size.

Expected conditional in-sample net for the capped maker rule:

| Filter | n | Events | Net | Cluster SE | t |
|---|---:|---:|---:|---:|---:|
| Including zero-volume rows | 797 | 670 | **3.56c** | 0.70c | 5.11 |
| `volume > 0` | 739 | 618 | **3.59c** | 0.75c | 4.78 |

Maker side breakdown with `volume > 0`:

| Leg | n | Events | Net | Cluster SE | t |
|---|---:|---:|---:|---:|---:|
| Buy 70-75c favorites at bid | 146 | 146 | **6.29c** | 3.41c | 1.85 |
| Sell 2-8c longshots at ask | 593 | 593 | **2.93c** | 0.44c | 6.62 |

Maker month split with `volume > 0`:

| Month | n | Events | Net | Cluster SE | t |
|---|---:|---:|---:|---:|---:|
| 2026-04 | 345 | 289 | **5.26c** | 0.94c | 5.57 |
| 2026-05 | 387 | 324 | **2.49c** | 1.12c | 2.23 |
| 2026-06 | 7 | 5 | **-18.00c** | 9.65c | -1.86 |

Maker asset split with `volume > 0`:

| Asset | n | Events | Net | Cluster SE | t |
|---|---:|---:|---:|---:|---:|
| BTC | 693 | 572 | **3.62c** | 0.79c | 4.57 |
| ETH | 46 | 46 | **3.17c** | 1.85c | 1.72 |

Maker caveat: for the capped maker rule, positive-volume coverage is 100% for the favorite leg but 91% for longshot offers overall, and only 57% in ETH. Treat the maker edge as conditional-on-fill alpha, not as realized deployable P&L without a separate fill study.

## Bottom line

The favorite-longshot bias is not broken, but the original broad narrative is too loose. The best defensible version is:

- **Taker:** buy upper favorites at T-15, sell 2-8c longshots at T-5, cap to one threshold per event-side, require positive volume, pay one-way taker fee. Expected net: **+3.24c per contract** in this development sample.
- **Maker:** same timing and cap, but enter passively at bid for favorites and ask for longshots. Expected conditional net: **+3.59c per filled contract** with positive volume.

This should be promoted only to a frozen out-of-sample test, not to full production sizing. The primary failure modes to monitor are T-15 favorite timing decay, May-like longshot compression, ETH fill quality, and any repetition of the sparse June losses.
