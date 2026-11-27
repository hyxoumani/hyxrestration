"""Cross-venue pair candidates: the matcher's vocabulary and the per-event view.

Re-run on 2026-11-27 after 4.5 months, the report had two blind spots. The
one pair of events that are the same question on both venues with the same
close day -- the December Fed decision -- scored 0.29 against a 0.4 floor,
because one venue says "Federal Reserve Cut ... 25bps" and the other "Fed
decrease ... 25 bps". And the ranked top 100 named 5 of the 55 series that
matched, the rest of it one strike ladder multiplied by another.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from simulator import pair_candidates as pc

K_CUT = "Will the Federal Reserve Cut rates by 25bps at their December 2026 meeting?"
P_CUT = "Will the Fed decrease interest rates by 25 bps after the December 2026 meeting?"
P_RBA = "Reserve Bank of Australia decreases interest rates by 25 bps at the December 2026 meeting?"
CLOSE = datetime(2026, 12, 9, 19)


def test_the_two_venues_wordings_of_one_fed_decision_clear_the_floor() -> None:
    assert pc.jaccard(pc.tokens(K_CUT), pc.tokens(P_CUT)) >= 0.75


def test_another_central_bank_scores_below_the_fed_for_a_fed_market() -> None:
    fed = pc.jaccard(pc.tokens(K_CUT), pc.tokens(P_CUT))
    rba = pc.jaccard(pc.tokens(K_CUT), pc.tokens(P_RBA))
    # Still a lead above the floor (0.62): the report is for a human to read.
    assert 0.4 < rba < fed


def test_a_cut_is_not_folded_into_a_hike() -> None:
    hike = "Will the Fed increase interest rates by 25 bps after the December 2026 meeting?"
    assert "cut" in pc.tokens(P_CUT) and "hike" not in pc.tokens(P_CUT)
    assert "hike" in pc.tokens(hike) and "cut" not in pc.tokens(hike)
    assert pc.jaccard(pc.tokens(K_CUT), pc.tokens(hike)) < pc.jaccard(
        pc.tokens(K_CUT), pc.tokens(P_CUT)
    )


def test_a_basis_point_size_is_one_quantity_written_two_ways() -> None:
    assert pc.tokens("cut 25bps") == pc.tokens("cut 25 bps") == pc.tokens("cut 25bp")
    # A decimal strike is still one token, and a size is not merged with another.
    assert "4.25" in pc.tokens("above 4.25% following")
    assert pc.tokens("cut 25 bps") != pc.tokens("cut 50 bps")


def test_no_fold_lands_on_a_stop_word_or_folds_twice() -> None:
    for word, folded in pc.CANON.items():
        assert word not in pc.STOP, word
        for f in folded:
            assert f not in pc.STOP and f not in pc.CANON, (word, f)


def test_candidates_reports_the_fed_pair_and_drops_a_far_close() -> None:
    kalshi = [("KXFEDDECISION-26DEC-C25", K_CUT, CLOSE)]
    poly = [
        ("0xnear", P_CUT, CLOSE + timedelta(hours=5)),
        ("0xfar", P_CUT, CLOSE + timedelta(days=22)),
    ]
    rows = pc.candidates(kalshi, poly, 0.4)
    assert [r["poly_id"] for r in rows] == ["0xnear"]
    assert rows[0]["close_delta_days"] == 0.21


def test_a_ladder_against_a_ladder_is_one_event_row_with_its_counts() -> None:
    rows = [
        {"score": 0.5, "kalshi_id": "KXIPO-26-A", "poly_id": "p1"},
        {"score": 0.7, "kalshi_id": "KXIPO-26-B", "poly_id": "p1"},
        {"score": 0.6, "kalshi_id": "KXIPO-26-B", "poly_id": "p2"},
        {"score": 0.9, "kalshi_id": "KXFEDDECISION-26DEC-C25", "poly_id": "p9"},
    ]
    events = pc.by_event(rows)
    assert [e["event"] for e in events] == ["KXFEDDECISION-26DEC", "KXIPO-26"]
    ipo = events[1]
    assert (ipo["score"], ipo["pairs"], ipo["kalshi_markets"], ipo["poly_markets"]) == (
        0.7,
        3,
        2,
        2,
    )
    assert ipo["best"]["kalshi_id"] == "KXIPO-26-B" and ipo["best"]["poly_id"] == "p1"


def test_an_id_with_no_strike_leg_is_its_own_event() -> None:
    assert pc.event_of("KXFED-26DEC-T4.25") == "KXFED-26DEC"
    assert pc.event_of("KXSOLO") == "KXSOLO"
