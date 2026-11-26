# Backtest: mirroring Congress buys (rules 2026-11-25.2)

Generated 2026-11-26T03:45 UTC from 2567 Congress stock buys in the ledger (2014-01-24 to 2026-11-23); 591 tickers had price data.

Entry is the first close after the disclosure date; exit is the holding period or the member's next full, non-batch sale. Excess = return minus the same dollars in SPY over the same dates. Trades still inside their hold are marked to the latest close.

**Not included:** the AI score and committee match (they'd need the model re-run on history), real fills, costs and taxes. Leaders/chairs are today's list applied to past trades.

| Scenario | Trades (open) | Avg return | Avg SPY | Avg excess | Median excess | Win rate | Beat SPY | Sized return vs SPY |
|---|---|---|---|---|---|---|---|---|
| Current rules (sized, rule-based holds) | 272 (12) | +10.41% | +9.40% | +1.01 pts | -0.66 pts | 71% | 46% | +11.38% vs +10.08% |
| Every Congress stock buy, 90-day hold | 2485 (424) | +5.07% | +3.87% | +1.20 pts | +0.05 pts | 63% | 50% | +5.07% vs +3.87% |
| Hold 30 days (current entry rules) | 272 (1) | +5.67% | +3.61% | +2.06 pts | +2.01 pts | 73% | 59% | +5.67% vs +3.61% |
| Hold 60 days (current entry rules) | 272 (8) | +1.74% | +1.39% | +0.35 pts | +0.06 pts | 56% | 51% | +1.74% vs +1.39% |
| Hold 90 days (current entry rules) | 272 (10) | +5.44% | +5.32% | +0.12 pts | -0.56 pts | 68% | 45% | +5.44% vs +5.32% |
| Hold 180 days (current entry rules) | 272 (12) | +16.23% | +13.20% | +3.03 pts | -1.89 pts | 76% | 46% | +16.23% vs +13.20% |
| Hold 180 days, ignore member sales | 272 (12) | +16.22% | +13.22% | +3.00 pts | -1.89 pts | 76% | 46% | +16.22% vs +13.22% |
| Leaders & committee chairs only | 17 (5) | +14.44% | +8.84% | +5.60 pts | +4.16 pts | 65% | 59% | +14.44% vs +8.84% |
| Rank-and-file members only | 255 (6) | +5.34% | +5.47% | -0.13 pts | -0.74 pts | 67% | 44% | +5.34% vs +5.47% |
| Batch-filing buys only (what the filter removes) | 47 (33) | +1.91% | -0.48% | +2.39 pts | +2.66 pts | 64% | 62% | +1.91% vs -0.48% |
| $15k-$50k disclosed | 131 (7) | +6.35% | +5.39% | +0.96 pts | -0.57 pts | 70% | 43% | +6.35% vs +5.39% |
| $50k-$250k disclosed | 101 (3) | +4.78% | +5.81% | -1.03 pts | -0.36 pts | 69% | 47% | +4.78% vs +5.81% |
| $250k+ disclosed | 40 (0) | +4.12% | +3.85% | +0.27 pts | -0.70 pts | 58% | 50% | +4.12% vs +3.85% |
| Filed within 15 days | 172 (3) | +6.39% | +5.38% | +1.02 pts | -0.74 pts | 72% | 44% | +6.39% vs +5.38% |
| Filed 16-30 days after trade | 260 (10) | +6.66% | +5.33% | +1.33 pts | +0.12 pts | 68% | 52% | +6.66% vs +5.33% |
| Filed 31+ days after trade | 133 (13) | +2.94% | +3.94% | -0.99 pts | -2.48 pts | 56% | 44% | +2.94% vs +3.94% |

**95% range for the average excess** (bootstrap): if the range includes 0, the scenario hasn't shown it beats SPY yet.

| Scenario | Avg excess | 95% range |
|---|---|---|
| Current rules (sized, rule-based holds) | +1.01 pts | -1.63 to +3.54 |
| Every Congress stock buy, 90-day hold | +1.20 pts | +0.59 to +1.81 |
| Hold 30 days (current entry rules) | +2.06 pts | +0.86 to +3.28 |
| Hold 60 days (current entry rules) | +0.35 pts | -1.13 to +1.75 |
| Hold 90 days (current entry rules) | +0.12 pts | -1.64 to +1.81 |
| Hold 180 days (current entry rules) | +3.03 pts | -0.50 to +6.34 |
| Hold 180 days, ignore member sales | +3.00 pts | -0.53 to +6.29 |
| Leaders & committee chairs only | +5.60 pts | -3.44 to +14.63 |
| Rank-and-file members only | -0.13 pts | -2.02 to +1.73 |
| Batch-filing buys only (what the filter removes) | +2.39 pts | -0.54 to +5.14 |
| $15k-$50k disclosed | +0.96 pts | -1.10 to +2.98 |
| $50k-$250k disclosed | -1.03 pts | -3.79 to +1.78 |
| $250k+ disclosed | +0.27 pts | -5.71 to +7.22 |
| Filed within 15 days | +1.02 pts | -0.90 to +2.80 |
| Filed 16-30 days after trade | +1.33 pts | -0.70 to +3.49 |
| Filed 31+ days after trade | -0.99 pts | -3.71 to +1.75 |

## By member (current rules, 10+ trades)

| | Trades | Avg return | Avg excess | 95% range | Beat SPY |
|---|---|---|---|---|---|
| Charles Patrick Roberts | 54 | +11.30% | +0.66 pts | -3.98 to +5.81 | 39% |
| John Wright Hickenlooper | 17 | -12.66% | -23.31 pts | -36.10 to -10.20 | 24% |
| Marjorie Taylor Greene | 12 | +1.53% | -1.79 pts | -7.83 to +4.24 | 42% |
| Michael T. McCaul | 17 | +3.61% | -3.64 pts | -10.84 to +3.44 | 35% |
| Nancy Pelosi | 37 | +12.21% | +4.42 pts | -4.38 to +15.40 | 54% |
| Ronald Lee Wyden | 90 | +15.58% | +4.74 pts | +1.70 to +7.86 | 52% |
| Sheldon Whitehouse | 13 | +18.77% | +4.83 pts | -6.14 to +16.37 | 54% |

## By year disclosed (current rules)

| | Trades | Avg return | Avg excess | 95% range | Beat SPY |
|---|---|---|---|---|---|
| 2014 | 9 | +29.12% | +19.08 pts | -0.44 to +42.80 | 56% |
| 2015 | 5 | +8.48% | -3.69 pts | -9.97 to +2.59 | 40% |
| 2017 | 12 | +24.91% | +10.53 pts | +0.17 to +20.96 | 83% |
| 2018 | 12 | +7.91% | -5.47 pts | -11.06 to +0.80 | 25% |
| 2020 | 122 | +14.37% | +3.22 pts | +0.63 to +5.90 | 46% |
| 2021 | 10 | +8.06% | -1.11 pts | -13.71 to +10.60 | 60% |
| 2022 | 17 | +3.95% | -1.13 pts | -8.53 to +6.48 | 47% |
| 2023 | 9 | +40.83% | +26.64 pts | -1.70 to +64.17 | 67% |
| 2024 | 13 | +11.01% | +4.56 pts | -5.00 to +12.84 | 69% |
| 2025 | 23 | -6.15% | -13.55 pts | -25.31 to -2.67 | 39% |
| 2026 | 36 | -4.57% | -7.83 pts | -13.19 to -2.37 | 28% |

Small samples (under ~30 trades) are noise; read them as hints, not results.
