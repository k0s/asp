"""Reconciliation against a journal: only changes raise events."""

import json
import os
from pathlib import Path

import pytest

from asp import ConfigError, Engine
from watchers import flaky


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "in/sub").mkdir(parents=True)
    (tmp_path / "out").mkdir()
    (tmp_path / "in/a.txt").write_text("a")
    (tmp_path / "in/sub/b.txt").write_text("b")
    (tmp_path / "in/c.md").write_text("c")
    return tmp_path


def engine(tree: Path, *watchers: str, roots: dict | None = None, **extra) -> Engine:
    config = tree / "asp.json"
    config.write_text(json.dumps({"roots": roots or {"in": "in", "out": "out"},
                                  "watchers": list(watchers), "db": "state/asp.db",
                                  **extra}))
    return Engine.from_config(config, {})


def files_under(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def kinds(e: Engine) -> list[tuple[str, str, str]]:
    return [(r["kind"], r["root"], r["rel"])
            for r in reversed(e.journal.events(limit=1000))]


def touch(path: Path, text: str) -> None:
    """Rewrite a file and move its mtime, whatever the clock's resolution."""
    before = path.stat().st_mtime
    path.write_text(text)
    os.utime(path, (before + 10, before + 10))


def test_first_pass_journals_every_file_and_runs_the_rules(tree) -> None:
    e = engine(tree, "watchers.upper")
    done = e.run_once()
    assert (done.files, done.events, done.jobs) == (3, 3, {"done": 2})
    assert files_under(tree / "out") == {"a.txt", "sub/b.txt"}
    assert kinds(e) == [("created", "in", "a.txt"), ("created", "in", "c.md"),
                        ("created", "in", "sub/b.txt")]
    jobs = e.journal.jobs()
    assert {(j["rule"], j["rel"], j["state"], j["output_root"], j["output_rel"])
            for j in jobs} == {("upper", "a.txt", "done", "out", "a.txt"),
                               ("upper", "sub/b.txt", "done", "out", "sub/b.txt")}


def test_second_pass_sees_only_its_own_outputs_and_runs_nothing(tree) -> None:
    e = engine(tree, "watchers.upper")
    e.run_once()
    done = e.run_once()
    assert (done.events, done.jobs) == (2, {})
    derived = [r for r in e.journal.events() if r["root"] == "out"]
    assert {(r["source"], r["depth"]) for r in derived} == {("handler:upper", 1)}
    third = e.run_once()
    assert (third.files, third.events, third.jobs) == (5, 0, {})


def test_a_modified_input_is_redone_and_a_deleted_one_is_journaled(tree) -> None:
    e = engine(tree, "watchers.upper")
    e.run_once()
    e.run_once()
    touch(tree / "in/a.txt", "changed")
    (tree / "in/sub/b.txt").unlink()
    done = e.run_once()
    assert done.jobs == {"done": 1}
    assert (tree / "out/a.txt").read_text() == "CHANGED"
    assert kinds(e)[-2:] == [("modified", "in", "a.txt"), ("deleted", "in", "sub/b.txt")]
    # Outputs are never deleted by the engine.
    assert (tree / "out/sub/b.txt").exists()


def test_a_handler_never_runs_on_its_own_renditions(tree) -> None:
    e = engine(tree, "watchers.renditions", roots={"in": "in"})
    for _ in range(4):
        e.run_once()
    assert files_under(tree / "in") == {
        "a.txt", "c.md", "sub/b.txt",
        "renditions/small/a.txt", "renditions/large/a.txt",
        "sub/renditions/small/b.txt", "sub/renditions/large/b.txt"}
    assert e.run_once().events == 0


def test_a_failing_job_is_retried_then_parked_and_can_be_requeued(tree) -> None:
    flaky.FAIL = True
    flaky.CALLS.clear()
    (tree / "in/sub/b.txt").unlink()
    e = engine(tree, "watchers.flaky")
    assert e.run_once().jobs == {"failed": 1}
    assert e.run_once().jobs == {"failed": 1}
    assert e.run_once().jobs == {"parked": 1}
    assert e.run_once().jobs == {}                  # parked jobs stay parked
    (job,) = e.journal.jobs()
    assert (job["state"], job["attempts"], job["error"]) == (
        "parked", 3, "RuntimeError: not today: in:a.txt")
    assert files_under(tree / "out") == set()

    flaky.FAIL = False
    assert e.journal.retry(job["id"])
    assert e.run_once().jobs == {"done": 1}
    assert (tree / "out/a.txt").read_text() == "a"
    assert len(flaky.CALLS) == 4


def test_ignored_paths_and_the_journal_itself_are_not_scanned(tree) -> None:
    (tree / "in/.stversions").mkdir()
    (tree / "in/.stversions/a.txt").write_text("old copy")
    (tree / "in/skip.txt").write_text("skipped by config")
    e = engine(tree, roots={"all": "."}, ignore=["**/.stversions", "**/skip.txt", "asp.json"])
    e.run_once()
    assert files_under(tree / "state") >= {"asp.db"}
    assert [rel for _, _, rel in kinds(e)] == ["in/a.txt", "in/c.md", "in/sub/b.txt"]
    assert e.run_once().events == 0


def test_the_journal_survives_a_new_engine(tree) -> None:
    engine(tree, "watchers.upper").run_once()
    again = engine(tree, "watchers.upper")
    assert again.run_once().events == 2      # only the first pass's outputs
    summary = again.journal.summary()
    assert {r["root"]: (r["files"], r["derived"]) for r in summary["roots"]} == {
        "in": (3, 0), "out": (2, 2)}
    assert summary["jobs"] == {"done": 2}


@pytest.mark.parametrize("extra, message", [({"db": 3}, "'db'"), ({"ignore": "x"}, "'ignore'")])
def test_bad_journal_config(tree, extra, message) -> None:
    with pytest.raises(ConfigError, match=message):
        engine(tree, **extra)
