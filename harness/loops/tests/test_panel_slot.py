"""A panel round reserves memory on `heavy-slot` before its seats start (#874).

The reservation runs the real `heavy-slot` against a fake /proc/meminfo and a private
queue directory, so "the round waits" is the broker's own behaviour, not a double's.

Run: pytest harness/loops/tests/test_panel_slot.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import panel_slot
import pytest

BIN = Path(__file__).resolve().parents[2] / "bin"
LOOPS = Path(__file__).resolve().parents[1]


@pytest.fixture
def queue(tmp_path, monkeypatch):
    """`heavy-slot` on PATH, on a 16G box with a 4G reserve: a 12G budget."""
    fakes = tmp_path / "bin"
    fakes.mkdir()
    (fakes / "heavy-slot").write_text(f"#!{sys.executable}\n" + (BIN / "heavy-slot").read_text())
    (fakes / "heavy-slot").chmod(0o755)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 16777216 kB\nMemAvailable: 16777216 kB\n")
    monkeypatch.setenv("PATH", f"{fakes}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HEAVY_SLOT_DIR", str(tmp_path / "q"))
    monkeypatch.setenv("HEAVY_SLOT_MEMINFO", str(meminfo))
    monkeypatch.setenv("HEAVY_SLOT_BUDGET", "12G")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("QB_PANEL_MEMORY", "6G")
    monkeypatch.setattr(panel_slot, "_HELD", [])
    return fakes


def status() -> dict:
    out = subprocess.run([shutil.which("heavy-slot"), "status", "--json"], capture_output=True,
                         text=True, check=True).stdout
    return json.loads(out)


def test_a_round_holds_its_reservation_until_the_holder_is_let_go(queue):
    notes: list[str] = []
    holder = panel_slot.reserve(notes, 861)
    try:
        held = status()["holders"]
        assert [(h["label"], h["bytes"]) for h in held] == [("panel PR #861", 6 << 30)]
        assert notes == [] and [holder] == panel_slot._HELD
    finally:
        holder.stdin.close()
        holder.wait(timeout=10)
    assert status()["holders"] == []


def test_a_reservation_the_box_can_never_grant_is_a_note_and_the_round_runs(queue, monkeypatch):
    monkeypatch.setenv("QB_PANEL_MEMORY", "20G")
    notes: list[str] = []
    assert panel_slot.reserve(notes, 861) is None
    assert notes == ["memory: heavy-slot exited 3 before granting 20G, so this round ran "
                     "without a reservation"]


def test_without_heavy_slot_the_round_runs_as_before_and_says_so_once(monkeypatch, capsys):
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setenv("QB_PANEL_MEMORY", "6G")
    notes: list[str] = []
    assert panel_slot.reserve(notes, 861) is None
    assert notes == []
    assert capsys.readouterr().err.count("does not wait for memory") == 1


@pytest.mark.parametrize("off", ["0", "0G", "0.0M"])
def test_qb_panel_memory_zero_turns_the_wait_off(queue, monkeypatch, off):
    monkeypatch.setenv("QB_PANEL_MEMORY", off)
    notes: list[str] = []
    assert panel_slot.reserve(notes, 861) is None
    assert notes == [] and status()["holders"] == []


def test_a_holder_that_answers_with_anything_but_a_grant_is_not_a_reservation(queue):
    (queue / "heavy-slot").write_text("#!/bin/sh\necho 'heavy-slot: something odd'\n")
    notes: list[str] = []
    assert panel_slot.reserve(notes, 861) is None
    assert notes == ["memory: heavy-slot exited 0 before granting 6G, so this round ran "
                     "without a reservation"]


def test_the_round_reserves_before_it_reads_the_pr_claims_it_or_starts_a_seat():
    src = (LOOPS / "panel.py").read_text()
    reserve = src.index("panel_slot.reserve(notes, pr_number)")
    assert reserve < src.index('head_sha = meta["headRefOid"]')
    assert reserve < src.index("hold_pr(cfg[\"path\"], pr_number, round_no)")
    assert reserve < src.index("with ThreadPoolExecutor(max_workers=len(ALL_REVIEWERS) + 1)")
