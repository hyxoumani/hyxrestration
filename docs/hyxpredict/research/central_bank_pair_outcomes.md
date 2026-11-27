# Central-bank decision markets: do the two venues resolve alike?

2026-11-27. Descriptive, results only: **no price, candle, trade or PnL was
read for any market compared** (one abandoned listing's trade DATES and
volume were read to identify it, below). Input for the user's pair
verification (strategy queue item 4), not a strategy reading.

## Question

The 11-27 pair report's top class is central-bank decision events listed on
both venues. Before a pair can enter `watchlist.json` the user verifies that
the two venues' resolution rules coincide. This asks the archive the
empirical half of that: over the meetings both venues have already settled,
did they resolve the same way?

## Method

`hyxlab.Fri.duckdb` (the 11-27 03:30Z backup slot; nothing live held).
Kalshi: settled markets of `KXFEDDECISION` and every `KXCBD*` series (406
markets, 72 bank-meetings). Polymarket: settled markets whose title holds
"meeting" and "bps" or "no change" (124). Each title is parsed to a bank and
a bucket (hold; cut / hike of 25; cut / hike of more than 25); a Kalshi
meeting is paired with the same bank's Polymarket meeting closing within 3
days. Bucket mapping, fixed before the comparison was read: Polymarket
"25 bps" = Kalshi "25bps" or "1-25bps"; Polymarket "50+ bps" = any Kalshi
bucket above 25 (">25bps", or "50bps" and ">50bps" where Kalshi lists three
sizes). A Polymarket market's Kalshi-implied result is YES when any mapped
Kalshi market settled YES.

## Result

- 36 of the 72 Kalshi meetings have a settled Polymarket meeting within 3
  days, over 15 banks (Fed 3, ECB 3, England 3, Japan 3, Australia 3,
  Brazil 3, Mexico 3, Korea 4, Russia 3, India 2, Israel 2, Chile, New
  Zealand, South Africa, Switzerland 1 each). 20 Kalshi markets (Colombia)
  did not parse to a bank and are left out.
- **118 Polymarket markets compared: 115 agree, 1 disagrees, 2 have no
  Kalshi bucket to compare to** (a "50 bps" Polymarket market where Kalshi
  lists only "more than 25").
- 15 of the 36 meetings were a move (the rest holds), every one in the
  25-or-under bucket.

## The one disagreement is a duplicate listing, not a rule

Fed, September 2026 (Kalshi: hike 25 YES, hold NO). Polymarket holds TWO
markets titled "Will there be no change in Fed interest rates after the
September 2026 meeting?" with the same close:

| condition id | slug | result | volume | trades in the archive |
|---|---|---|---|---|
| `0xa3b36b2d6104…` | `…-september-2026-meeting-6…` | no | $56.4M | 54,800, 05-14..09-16 |
| `0x5c0eb0be1b96…` | `…-september-2026-meeting` | yes | $12.8k | 369, 04-30..05-11 |

The second is an abandoned earlier listing (no trade after 05-11, zero
liquidity from the first day the archive saw it) that the live one replaced
two days later; the live one agrees with Kalshi. The same event carries
duplicate "50+ bps" listings too (both NO). Why the abandoned one reads
`yes` is not established -- the archive derives a Polymarket result from the
final outcome price (`collector/venues/polymarket.py`), so it is either how
the venue closed the dead listing or that derivation on a market that never
resolved normally.

## What this does and does not show

- On every meeting both venues settled, the live markets resolved alike:
  115 of 115. That is evidence the two venues read the same decision the
  same way; it is not the rule check.
- **Untested edges, which are exactly what the user verifies:** no meeting
  in the sample moved by anything but 25 bps, so Kalshi's "1-25bps" against
  Polymarket's "25 bps" (a 10 or 15 bps move), and ">25bps" against
  "50+ bps" (a 35 or 40 bps move), never came up; nor did an inter-meeting
  move or a postponed meeting.
- **A pair is a pair of condition ids, never of titles.** Polymarket lists
  duplicates under one title; the watchlist entry must name the live
  listing (the one with the volume), and a title match can land on a dead
  one that resolved the other way.
- The two venues close hours apart (Fed: Kalshi 18:59Z, Polymarket 23:59Z
  on the decision day; other banks 0.01-1.9 d), so a pair's books overlap
  only before the earlier close.
