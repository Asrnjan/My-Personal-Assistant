from __future__ import annotations

import runpy
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"


def test_offline_demo_runs_the_whole_flow(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    demo = runpy.run_path(str(SCRIPTS / "seed_demo_data.py"))
    summary = demo["run"](tmp_path / "demo.db")
    assert summary["delegations_sent"] == 2
    assert summary["telegram_messages"] == 2
    assert summary["jira_reassigned"] == 1
    assert summary["meetings_declined"] == 1
    assert summary["acknowledged"] == 1
    out = capsys.readouterr().out
    assert "Shall I go ahead?" in out
    assert "Rahul Kumar accepted Vendor contract review" in out
    assert demo["main"](["--db", str(tmp_path / "kept.db")]) == 0
    assert (tmp_path / "kept.db").exists()
