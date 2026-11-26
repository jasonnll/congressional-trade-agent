# Backtest: mirroring Congress buys (rules 2026-11-25.2)

Generated 2026-11-26T03:39 UTC from 2567 Congress stock buys in the ledger (2014-01-24 to 2026-11-23); 591 tickers had price data.

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

Small samples (under ~30 trades) are noise; read them as hints, not results.
