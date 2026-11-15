"""HOSTFS's btrfs clause: is `statvfs` free actually free (2026-11-15)?

On btrfs a write fails ENOSPC when METADATA cannot grow, whatever `df` says.
The fixture is this box's own `btrfs filesystem usage -b data` at 21:10Z on
2026-11-15, verbatim, 3.1 d from full by the digest's pace clause.
"""

from __future__ import annotations

import dataclasses
import inspect
import subprocess

from collector import health

USAGE = """Overall:
    Device size:		     1999269527552
    Device allocated:		     1943497867264
    Device unallocated:		       55771660288
    Device missing:		                 0
    Device slack:		                 0
    Used:			     1803272134656
    Free (estimated):		      181958139904	(min: 154072309760)
    Free (statfs, df):		      181957091328
    Data ratio:			              1.00
    Metadata ratio:		              2.00
    Global reserve:		         536870912	(used: 0)
    Multiple profiles:		                no

Data,single: Size:1917711286272, Used:1791524806656 (93.42%)

Metadata,DUP: Size:12884901888, Used:5873418240 (45.58%)

System,DUP: Size:8388608, Used:245760 (2.93%)
"""
GIB = 1 << 30


def test_the_measured_box_parses_to_the_five_numbers_and_reads_usable():
    a = health.parse_btrfs_usage(USAGE)
    assert a == health.BtrfsAlloc(
        55771660288, 1917711286272, 1791524806656, 12884901888, 5873418240, 536870912, 2
    )
    # The two free terms are statvfs's figure, to the megabyte: the clause
    # splits the number the rest of the line quotes, it does not add to it.
    assert abs(a.unallocated_b + a.data_size_b - a.data_used_b - 181957091328) < 2 * 10**6
    text = health.btrfs_text(a)
    assert text.startswith("btrfs: 56 GB unallocated + 126 GB free inside data chunks")
    assert "metadata 5.5 of 12.0 GiB (6.0 GiB of room past the reserve)" in text
    assert text.endswith("the free figure is usable") and "TIGHT" not in text


def test_full_metadata_with_nothing_unallocated_is_tight_whatever_data_holds():
    a = health.parse_btrfs_usage(USAGE)
    tight = dataclasses.replace(a, unallocated_b=GIB, meta_used_b=a.meta_size_b - GIB)
    assert "METADATA TIGHT" in health.btrfs_text(tight)
    # 126 GB still free inside data chunks: statvfs would read it all as free.
    assert tight.data_size_b - tight.data_used_b > 100 * 10**9


def test_tight_needs_both_halves():
    a = health.parse_btrfs_usage(USAGE)
    full_meta = dataclasses.replace(a, meta_used_b=a.meta_size_b - GIB)
    assert "another chunk still fits" in health.btrfs_text(full_meta)
    no_unalloc = dataclasses.replace(a, unallocated_b=0)
    assert health.btrfs_text(no_unalloc).endswith("the free figure is usable")


def test_a_dup_chunk_costs_two_raw_gib_and_a_single_one_costs_one():
    a = dataclasses.replace(health.parse_btrfs_usage(USAGE), unallocated_b=GIB + GIB // 2)
    assert not a.meta_can_grow
    assert dataclasses.replace(a, meta_copies=1).meta_can_grow
    single = health.parse_btrfs_usage(USAGE.replace("Metadata,DUP", "Metadata,single"))
    assert single.meta_copies == 1


def test_the_reserve_is_not_room():
    a = health.parse_btrfs_usage(USAGE)
    edge = dataclasses.replace(a, unallocated_b=0, meta_used_b=a.meta_size_b - GIB - 1)
    assert "TIGHT" in health.btrfs_text(edge)  # 1 GiB free, 512 MiB of it reserved
    assert "TIGHT" not in health.btrfs_text(dataclasses.replace(edge, reserve_b=0))


def test_every_field_moves_the_clause_or_its_verdict():
    a = health.parse_btrfs_usage(USAGE)
    base = health.btrfs_text(a)
    for field in ("unallocated_b", "data_size_b", "data_used_b", "meta_size_b", "meta_used_b"):
        bumped = dataclasses.replace(a, **{field: getattr(a, field) + 3 * 10**9})
        assert health.btrfs_text(bumped) != base, field
    assert health.btrfs_text(dataclasses.replace(a, reserve_b=2 * GIB)) != base


def test_a_partial_or_foreign_output_is_unread_never_a_partial_reading(monkeypatch):
    assert health.parse_btrfs_usage("") is None
    assert health.parse_btrfs_usage("ERROR: not a btrfs filesystem: /tmp") is None
    for needle in ("Device unallocated", "Global reserve", "Data,single", "Metadata,DUP"):
        cut = "\n".join(ln for ln in USAGE.splitlines() if needle not in ln)
        assert health.parse_btrfs_usage(cut) is None, needle
    assert "unread" in health.btrfs_text(None)

    def boom(cmd, **kw):
        raise FileNotFoundError("btrfs")

    monkeypatch.setattr(health.subprocess, "run", boom)
    assert health.read_btrfs_usage() is None


def test_the_read_asks_for_bytes_on_the_archive_path(monkeypatch):
    asked = []

    def fake_run(cmd, **kw):
        asked.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=USAGE, stderr="WARNING: run as root")

    monkeypatch.setattr(health.subprocess, "run", fake_run)
    assert health.read_btrfs_usage() == health.parse_btrfs_usage(USAGE)
    assert asked == [["btrfs", "filesystem", "usage", "-b", health.HOSTFS_PATH]]


def test_the_digest_prints_the_clause_it_computes():
    assert "btrfs_text(read_btrfs_usage())" in inspect.getsource(health.main)
