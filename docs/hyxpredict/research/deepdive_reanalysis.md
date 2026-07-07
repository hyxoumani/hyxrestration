# Re-analysis: Polymarket deep-dive + crypto-arbitrage backup options

**Status:** scoping/analysis only. Does NOT reopen hyxpredict or override the
Phase 0 post-mortem deferral (`docs/phase0_postmortem.md` §8). Written 2026-07-06.

**Subject:** `polymarket_wallet_deepdive.md` (the "compass" research artifact,
previously untracked at repo root), plus an independent evaluation of the crypto
arbitrage routes it describes, framed as a *backup track* to the primary plan
(Phase 0 write-up + job search).

---

## 1. Claim audit of the deep-dive artifact

Method: same posture as Phase 0 — separate verified claims from promotional
claims, check the math, and look for framing drift. Verification was done
against current (July 2026) sources; links in §5.

### 1.1 Verified (independently confirmed)

| Claim | Verdict |
|---|---|
| IMDEA arbitrage study exists; ~$40M realized arb profit; two types (rebalancing + combinatorial) | **Confirmed** — arXiv 2508.03474, published AFT 2025 |
| Polymarket international crypto fees: 0.07 factor taker, peak ~$1.75–1.80/100 shares at 50¢ | **Confirmed** — docs.polymarket.com + third-party guides |
| Polymarket US regulated entity (QCEX acquisition, CFTC DCM, Nov 2025 amended order) | **Confirmed** |
| Kalshi taker fee `ceil(0.07·P·(1−P)·100)/100` per contract; maker ≈ ¼ of taker | **Confirmed** — kalshi.com/fee-schedule |
| Short-window crypto markets resolve via Chainlink Data Streams (not UMA) | **Confirmed** |
| Latency arb mechanism is real and documented | **Confirmed** — a 2026 arXiv paper (PolySwarm, 2604.03888) measured ~55s average human reaction lag to Chainlink feed updates; open-source "oracle-lag sniper" repos exist on GitHub |
| Fees introduced Jan–Apr 2026 partly to blunt HFT taker strategies | **Confirmed** — multiple sources note pure taker HFT "became less viable" post-fees |

### 1.2 Unverifiable (the artifact says so itself, but buries it)

- **Everything about the profiled wallet.** No on-chain corroboration, no
  third-party indexing, point-in-time UI snapshots only. Part 1 of the artifact
  is an anecdote. The inference that it's an automated MM/scalper (two-sided
  holdings, ~1.3% net margin on volume) is *sound reasoning* but the anchor
  numbers can't be checked. Weight accordingly: illustrative, not evidentiary.
- **All strategy performance claims from bot guides** ("78–85% win rate",
  "1–3% monthly returns" for market-making). Promotional sources (VPS vendors,
  exchange-affiliated blogs, Medium bot guides). Treat as marketing.

### 1.3 Framing problems

1. **Base rate vs. recommendations mismatch.** The artifact's own numbers —
   84.1% of wallets lose; 0.1% of accounts capture 67% of profits — say the
   modal outcome for a new entrant is loss. Its "Recommendations" section then
   pivots to winner playbooks without pricing that in. Classic
   survivorship-conditioned advice.
2. **Stale-regime playbooks.** Every published guide predates or straddles the
   Jan–Apr 2026 fee introduction. The $40M IMDEA figure covers Apr 2024–Apr 2025:
   a zero-fee regime *and* a US-election liquidity bonanza. Neither condition
   holds in July 2026. The artifact notes the fee change but doesn't propagate
   the implication through its recommendations.
3. **Aggregate ≠ addressable.** "$40M/yr extracted" is total across all actors;
   rebalancing arb is a latency race won by a handful of bots. The addressable
   share for a new entrant at retail latency is approximately zero.
4. **It quietly confirms the hyxpredict post-mortem.** The winners' taxonomy
   (information edge / structural MM / speed) contains **no "better forecaster"
   playbook at retail scale**. This is the same venue-conditions argument that
   deferred hyxpredict (Susquehanna on Kalshi, Jane Street on Polymarket,
   active LLM-bot industry). The artifact strengthens the deferral, it does not
   weaken it.

---

## 2. The fee math the artifact didn't do

The single most decision-relevant fact found in verification: **as of April 2026,
both venues' fee schedules approximately close the naive cross-platform arb at
mid prices.**

Worked example — taker on both legs of a matched BTC market near 50¢:

```
Gross edge:  buy YES @ 48¢ (Kalshi) + buy NO @ 49¢ (Polymarket US)
             → payout $1.00, cost 97¢, gross 3¢/share

Fees:        Kalshi taker  ≈ 0.07 × .48 × .52 ≈ 1.75¢
             Poly-US taker ≈ 0.05 × .49 × .51 ≈ 1.25¢
             total         ≈ 3.0¢/share

Net edge:    ≈ 0
```

Implications:

- The "consistent 2–5% spreads" claim from arb-bot guides is **pre-fee-regime
  or gross-of-fees**. At mid prices you now need >3¢ of spread just to break
  even taker-taker. Spreads that wide on liquid matched markets get taken by
  faster bots.
- Both fee curves are parabolic (peak at 50¢, →0 at extremes), so the residual
  opportunity lives at **skewed prices** (e.g., 90/10: combined fees ≈ 1.1¢)
  and in **maker-one-leg** executions — which reintroduces naked-leg risk, the
  #1 documented failure mode.
- Polymarket's maker rebate (−0.0125 factor US; 20–25% taker-fee share intl)
  means the only fee-*positive* seat at the table is market-making. The venue
  is explicitly structured to pay makers and tax takers. Any taker-side
  "backup strategy" is swimming against the venue's own design intent.

---

## 3. The four arb routes, ranked as a backup track

Evaluated against our actual constraints: US-based, one person, job-searching
(time-poor), strong local compute (irrelevant here — these games are network-
latency-bound, not compute-bound), no committed capital.

### D. Market-making / liquidity rewards — *viable mechanics, immaterial income*
The only structurally fee-advantaged role. But: inventory risk in trending
markets, competition from professional MMs, and income scales with capital.
Even granting the promotional 1–3%/month, $10K deployed ≈ $100–300/month
pre-risk, pre-tax, pre-time-cost. **As income it's immaterial; as a
learning/portfolio project it's the best of the four.**

### B. Cross-platform Poly↔Kalshi — *real but thin and shrinking*
The §2 math says taker-taker is dead at mid prices; what remains is skewed-price
and maker-leg variants with execution risk, plus **resolution-rule mismatch**
(platforms can settle "the same" event differently — turns a locked arb into a
double loss). Multiple open-source bots target exactly the obvious pairs (BTC
hourly). **Only defensible as a measurement study first** (§4).

### A. Intra-market YES+NO rebalancing — *commoditized, latency race, not for us*
Risk-free in theory, won in milliseconds in practice, ~$10.6M/yr across *all*
participants in the zero-fee era. Post-fee, thinner. No entry for retail-grade
latency.

### C. Latency/oracle arb on short-window crypto — *arms race, worst fit*
Requires co-located VPS + WebSocket CEX feeds + sub-second execution; the fee
regime was designed specifically against it; open-source snipers have
commoditized the entry level, so the remaining edge belongs to whoever wins the
infrastructure race. The RTX 5090 box buys nothing here — this is network
latency, not compute. **Reject.**

### Bottom line on "backup"

As **backup income**: no. To replace even a modest salary fraction
(~$2K/month) at optimistic MM returns you'd need ~$100K deployed on
prediction-market venues — that's not a backup plan, that's a capital
allocation decision, and per project rules real-money scale is a user-only
call.

As a **backup project** (skills/portfolio, capital-free): yes, one narrow
version survives — see §4.

---

## 4. The one action that survives: a Phase-0-style measurement study

If a backup track is wanted at all, the defensible entry is the same move that
worked in Phase 0: **turn the promotional claims into falsifiable numbers
before any capital is touched.** Zero dollars at risk; pure data engineering.

**Design sketch (not a pre-registration — that would come later, if ever):**

1. Log order books on matched BTC/ETH short-window and hourly markets:
   Polymarket US + Kalshi, via their public APIs, for 2–4 weeks.
2. Measure, fee-adjusted under the *current* (Apr 2026) schedules:
   - realized cross-venue spread distribution (net of both legs' fees),
   - opportunity frequency, persistence (seconds until closed), and depth at
     touch (how many $ could actually cross),
   - maker-fill feasibility proxy: how often does one leg's book sit crossable
     long enough to leg in.
3. Pre-register a go/no-go before looking: e.g., *"proceed only if fee-adjusted
   opportunities ≥ 50bp persist ≥ 10s at ≥ $500 depth, ≥ N times/day."*
   Otherwise the result is a clean public null — which, per the Phase 0
   post-mortem, is itself a portfolio artifact ("we measured the arb everyone
   blogs about; here's why it's gone").

**Cost:** a weekend to build the logger, then it runs unattended.
**What it must not become:** a trading bot built before the measurement returns
a PASS. That ordering is the entire lesson of Phase 0.

**Priority:** strictly below the Phase 0 write-up. The write-up is still the
locked, highest-value next action and remains untouched since April.

---

## 5. Sources used in verification

- IMDEA arbitrage study: [arXiv 2508.03474](https://arxiv.org/abs/2508.03474)
- Polymarket fees: [docs.polymarket.com/trading/fees](https://docs.polymarket.com/trading/fees),
  [startpolymarket.com fee table](https://startpolymarket.com/learn/polymarket-fees/),
  [Yahoo Finance on the fee overhaul](https://finance.yahoo.com/markets/crypto/articles/polymarket-fee-overhaul-pushes-daily-054836739.html)
- Kalshi fees: [kalshi.com/fee-schedule](https://kalshi.com/fee-schedule),
  [June 2026 fee schedule PDF](https://kalshi.com/docs/kalshi-fee-schedule.pdf)
- Latency arb: [PolySwarm paper, arXiv 2604.03888](https://arxiv.org/html/2604.03888v1),
  [oracle-lag-sniper (GitHub)](https://github.com/JonathanPetersonn/oracle-lag-sniper)
- Cross-platform arb bots: [ImMike/polymarket-arbitrage](https://github.com/ImMike/polymarket-arbitrage),
  [CarlosIbCu/polymarket-kalshi-btc-arbitrage-bot](https://github.com/CarlosIbCu/polymarket-kalshi-btc-arbitrage-bot),
  [dev.to build write-up](https://dev.to/realfishsam/how-i-built-a-risk-free-arbitrage-bot-for-polymarket-kalshi-4f)
- Chainlink resolution: [Polymarket × Chainlink press release](https://www.prnewswire.com/news-releases/polymarket-partners-with-chainlink-to-enhance-accuracy-of-prediction-market-resolutions-302555123.html),
  [The Block coverage](https://www.theblock.co/post/370444/polymarket-turns-to-chainlink-oracles-for-resolution-of-price-focused-bets)
- Polymarket US status: [Bitcoin.com on US perps launch](https://news.bitcoin.com/polymarket-unveils-perpetual-futures-trading-for-us-markets-in-2026/)
