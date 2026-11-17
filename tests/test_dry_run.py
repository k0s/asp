"""A dry run says what a pass would do and writes nothing."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from asp import Engine
from asp.cli import app as cli


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "in/sub").mkdir(parents=True)
    (tmp_path / "out/sub").mkdir(parents=True)
    (tmp_path / "in/a.txt").write_text("a")
    (tmp_path / "in/sub/b.txt").write_text("b")
    (tmp_path / "in/c.md").write_text("c")
    (tmp_path / "out/sub/b.txt").write_text("made by hand, long ago")
    (tmp_path / "asp.json").write_text(json.dumps({
        "roots": {"in": "in", "out": "out"}, "db": "state/asp.db",
        "watchers": ["watchers.upper"]}))
    return tmp_path


def snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob("*") if p.is_file()}


def plan_of(tree: Path):
    return Engine.from_config(tree / "asp.json", {}, create_journal=False).plan()


def test_a_first_dry_run_lists_every_job_and_flags_overwrites(tree: Path) -> None:
    before = snapshot(tree)
    plan = plan_of(tree)
    assert (plan.files, plan.events) == (4, {"created": 4})
    assert [(j.rule, j.root, j.rel, j.output, j.overwrites) for j in plan.jobs] == [
        ("upper", "in", "a.txt", ("out", "a.txt"), False),
        ("upper", "in", "sub/b.txt", ("out", "sub/b.txt"), True)]
    assert snapshot(tree) == before             # not even a journal


def test_a_dry_run_after_a_pass_shows_only_what_changed(tree: Path) -> None:
    Engine.from_config(tree / "asp.json", {}).run_once()
    Engine.from_config(tree / "asp.json", {}).run_once()
    assert plan_of(tree).jobs == []
    (tree / "in/new.txt").write_text("new")
    before = snapshot(tree / "in") | snapshot(tree / "out")
    events_before = len(Engine.from_config(tree / "asp.json", {}).journal.events())

    plan = plan_of(tree)
    assert plan.events == {"created": 1}
    assert [(j.rel, j.output, j.overwrites, j.why) for j in plan.jobs] == [
        ("new.txt", ("out", "new.txt"), False, "new")]
    assert snapshot(tree / "in") | snapshot(tree / "out") == before
    assert len(Engine.from_config(tree / "asp.json", {}).journal.events()) == events_before


def test_a_dry_run_includes_jobs_waiting_to_be_retried(tree: Path) -> None:
    from watchers import flaky
    (tree / "asp.json").write_text(
        (tree / "asp.json").read_text().replace("watchers.upper", "watchers.flaky"))
    flaky.FAIL = True
    Engine.from_config(tree / "asp.json", {}).run_once()
    assert sorted((j.rel, j.why) for j in plan_of(tree).jobs) == [
        ("a.txt", "failed"), ("sub/b.txt", "failed")]


def test_cli_dry_run(tree: Path) -> None:
    before = snapshot(tree)
    result = CliRunner().invoke(cli, ["run", "--dry-run", "--config", str(tree / "asp.json")])
    assert result.exit_code == 0
    assert "upper  in:sub/b.txt -> out:sub/b.txt  [OVERWRITES]" in result.output
    assert "2 jobs would run, writing 1 new files and overwriting 1" in result.output
    assert snapshot(tree) == before
