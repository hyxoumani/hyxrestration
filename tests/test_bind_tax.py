"""DuckDB's binding retries a failed `import pandas` twice per bound value.

Measured 2026-11-18 in the stable venv (no pandas): a 250,000-row x 8-column
stage took 50.04 s, and 0.46 s with pandas importable. That was the sweeps'
45-80 s `long write burst` on the collector's archive, invisible to every
replay because the replays ran from the dev venv (mistakes #212).
"""

import subprocess
import sys
from pathlib import Path

from collector import stmtcost

ROOT = Path(__file__).parent.parent


def test_stable_venv_pins_pandas_for_duckdb():
    pins = (ROOT / "scripts" / "requirements-stable.txt").read_text()
    lines = [ln.split("#")[0].strip() for ln in pins.splitlines()]
    assert any(ln.startswith("pandas==") for ln in lines)
    assert any(ln.startswith("numpy==") for ln in lines)


def test_promote_smoke_imports_pandas_in_the_stable_venv():
    promote = (ROOT / "scripts" / "promote.sh").read_text()
    smoke = [
        ln
        for ln in promote.splitlines()
        if "import collector.streamd" in ln or "collector.streamd," in ln
    ]
    assert smoke and all("import pandas," in ln for ln in smoke), smoke


def test_warning_only_when_pandas_is_missing():
    assert stmtcost.bind_tax_warning(pandas_importable=True) is None
    line = stmtcost.bind_tax_warning(pandas_importable=False)
    assert line is not None and "~50s" in line and "per million values" in line
    # The suite's own interpreter must have it, or every staged test pays the tax.
    assert stmtcost.bind_tax_warning() is None


def test_attach_prints_the_warning_once_per_process(monkeypatch, capsys):
    class Store:
        conn = object()

    monkeypatch.setattr(stmtcost, "_TAX_WARNED", False)
    monkeypatch.setattr(stmtcost, "bind_tax_warning", lambda: "[store] WARNING: tax")
    stmtcost.StatementCost.attach(Store())
    stmtcost.StatementCost.attach(Store())
    assert capsys.readouterr().out.count("[store] WARNING: tax") == 1


def test_the_mechanism_a_missing_pandas_is_imported_per_value():
    """The premise, from outside the process: with pandas unimportable DuckDB
    asks for it again on every value; importable, it asks a bounded number of
    times. If a DuckDB upgrade caches the miss, this goes red and the pin can go."""
    probe = (
        "import builtins, sys\n"
        "if sys.argv[1] == 'hide':\n"
        "    import importlib.abc\n"
        "    class Hide(importlib.abc.MetaPathFinder):\n"
        "        def find_spec(self, name, path=None, target=None):\n"
        "            if name.split('.')[0] == 'pandas':\n"
        "                raise ModuleNotFoundError(name)\n"
        "    sys.meta_path.insert(0, Hide())\n"
        "import duckdb\n"
        "n = [0]; orig = builtins.__import__\n"
        "def imp(name, *a, **k):\n"
        "    if name == 'pandas': n[0] += 1\n"
        "    return orig(name, *a, **k)\n"
        "builtins.__import__ = imp\n"
        "duckdb.connect().execute('select count(*) from (select unnest(?::VARCHAR[]))', [['a'] * 500]).fetchall()\n"
        "print(n[0])\n"
    )
    counts = {}
    for arm in ("hide", "show"):
        out = subprocess.run(
            [sys.executable, "-c", probe, arm], capture_output=True, text=True, check=True
        )
        counts[arm] = int(out.stdout.strip())
    assert counts["hide"] >= 500, counts  # at least once per value
    assert counts["show"] < 50, counts
