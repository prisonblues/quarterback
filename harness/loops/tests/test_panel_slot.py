"""A panel round reserves memory on `heavy-slot` before it reads the PR (#874).

The reservation runs the real `heavy-slot` against a fake /proc/meminfo and a private
queue directory, so "the round holds memory" is the broker's own answer, not a double's.

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
    """`heavy-slot` on PATH with every config key pinned: a 12G budget."""
    fakes = tmp_path / "bin"
    fakes.mkdir()
    (fakes / "heavy-slot").write_text(f"#!{sys.executable}\n" + (BIN / "heavy-slot").read_text())
    (fakes / "heavy-slot").chmod(0o755)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 16777216 kB\nMemAvailable: 16777216 kB\n")
    monkeypatch.setenv("PATH", f"{fakes}{os.pathsep}{os.environ['PATH']}")
    for key, value in {"DIR": str(tmp_path / "q"), "BUDGET": "12G", "RESERVE": "4G",
                       "MIN_FREE": "2G", "MEMINFO": str(meminfo)}.items():
        monkeypatch.setenv(f"HEAVY_SLOT_{key}", value)
    monkeypatch.setenv("QB_PANEL_MEMORY", "6G")
    monkeypatch.delenv("QB_PANEL_MEMORY_WAIT", raising=False)
    monkeypatch.setattr(panel_slot, "_HELD", [])
    return fakes


def holders() -> list[dict]:
    out = subprocess.run([shutil.which("heavy-slot"), "status", "--json"], capture_output=True,
                         text=True, check=True).stdout
    return json.loads(out)["holders"]


def test_a_round_holds_its_reservation_for_the_rest_of_the_process(queue):
    assert panel_slot.reserve(861) is None
    assert [(h["label"], h["bytes"]) for h in holders()] == [("panel PR #861", 6 << 30)]
    (holder,) = panel_slot._HELD
    holder.stdin.close()  # what the process's exit does
    holder.wait(timeout=10)
    assert holders() == []


@pytest.mark.parametrize("off", ["0", "0G", "0.0M", "0GB"])
def test_qb_panel_memory_zero_turns_the_wait_off(queue, monkeypatch, off):
    monkeypatch.setenv("QB_PANEL_MEMORY", off)
    assert panel_slot.reserve(861) is None and holders() == []


def test_without_heavy_slot_the_round_runs_as_before(monkeypatch):
    monkeypatch.setenv("QB_PANEL_MEMORY", "6G")  # past the off switch, to the PATH check
    monkeypatch.setenv("PATH", "/nonexistent")
    assert panel_slot.reserve(861) is None and panel_slot._HELD == []


@pytest.mark.parametrize(("size", "script"), [
    ("20G", None),  # bigger than the whole budget: heavy-slot exits 3
    ("6G", "#!/bin/sh\necho 'heavy-slot: something odd'\n"),
])
def test_no_grant_is_a_note_and_the_round_runs(queue, monkeypatch, size, script):
    if script:
        (queue / "heavy-slot").write_text(script)
    monkeypatch.setenv("QB_PANEL_MEMORY", size)
    note = panel_slot.reserve(861)
    assert note.startswith("memory: heavy-slot exited") and f"granting {size}" in note
    assert panel_slot._HELD == []


def test_the_round_reserves_after_its_refusals_and_before_it_reads_the_pr():
    src = (LOOPS / "panel.py").read_text()
    reserve = src.index("panel_slot.reserve(pr_number)")
    for earlier in ("panel_caps.enabled_refusal(cfg) or review_refusal(cfg)",
                    "if round_no > cap:"):
        assert src.index(earlier) < reserve, earlier
    for later in ('head_sha = meta["headRefOid"]', 'hold_pr(cfg["path"], pr_number, round_no)'):
        assert reserve < src.index(later), later
    assert src.count("panel_slot.reserve(") == 1
