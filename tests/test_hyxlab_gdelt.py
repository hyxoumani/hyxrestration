"""GDELT GKG parsing: filter-and-discard, topic tagging, dedup, tone,
title extraction. Fixture rows mirror the live 27-field format probed
2026-07-11."""

from datetime import datetime

from collector.venues import gdelt

TEMPLATES = {"inflation": ["ECON_INFLATION", "ECON_PRICE"], "energy": ["ECON_OILPRICE"]}


def _row(url, themes, tone="-3.5,1.0,4.5,5.5,20.0,0.2,700", title="Some headline"):
    f = [""] * 27
    f[0] = "20260711191500-1"
    f[1] = "20260711191500"
    f[4] = url
    f[7] = themes
    f[15] = tone
    f[26] = f"<PAGE_TITLE>{title}</PAGE_TITLE>"
    return "\t".join(f)


def test_parse_filters_tags_and_dedups():
    text = "\n".join(
        [
            _row("https://a.example/cpi", "ECON_INFLATION;TAX_FNCACT_LEADER"),
            _row("https://b.example/oil", "ECON_OILPRICE;ECON_PRICE"),  # both tags
            _row("https://c.example/sports", "SOC_POINTSOFINTEREST"),  # discard
            _row("https://a.example/cpi", "ECON_INFLATION"),  # dup URL in batch
            "short\tline",  # malformed
        ]
    )
    items = gdelt.parse_gkg(text, TEMPLATES)
    assert [i.topics for i in items] == ["inflation", "energy,inflation"]
    assert items[0].knowable_at == datetime(2026, 7, 11, 19, 15)
    assert items[0].tone == -3.5
    assert items[0].title == "Some headline"
    assert items[0].url_hash == gdelt.url_hash("https://a.example/cpi")


def test_prefix_matching_survives_taxonomy_drift():
    text = _row("https://d.example/fed", "ECON_PRICE_INDEXES")  # variant code
    items = gdelt.parse_gkg(text, TEMPLATES)
    assert items and items[0].topics == "inflation"


def test_gkg_urls_grid():
    urls = gdelt.gkg_urls(datetime(2026, 7, 11, 12, 7), datetime(2026, 7, 11, 12, 40))
    assert urls == [
        "http://data.gdeltproject.org/gdeltv2/20260711120000.gkg.csv.zip",
        "http://data.gdeltproject.org/gdeltv2/20260711121500.gkg.csv.zip",
        "http://data.gdeltproject.org/gdeltv2/20260711123000.gkg.csv.zip",
    ]


def test_shipped_templates_load():
    t = gdelt.load_templates()
    assert "inflation" in t and "_comment" not in t
