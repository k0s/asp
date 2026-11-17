"""The live shape of asp: a monitor wakes the reconciler when files change.

This is the first test's scenario against a running loop instead of one
synchronous pass.
"""

import json
import time
from pathlib import Path

import pytest

from asp import Engine
from asp.monitor import Reconciler


def files_under(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def wait_for(condition, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def live(tmp_path: Path):
    (tmp_path / "in/sub").mkdir(parents=True)
    (tmp_path / "out").mkdir()
    (tmp_path / "asp.json").write_text(json.dumps({
        "roots": {"in": "in", "out": "out"}, "db": "state/asp.db",
        "watchers": ["watchers.upper"]}))
    reconciler = Reconciler(Engine.from_config(tmp_path / "asp.json", {}),
                            watch=True, settle=0.05)
    reconciler.start()
    assert reconciler.wait_idle()
    yield tmp_path, reconciler
    reconciler.stop()
    reconciler.join(5)


def test_changes_are_published_without_anyone_asking(live) -> None:
    tree, reconciler = live
    # The non-matching file goes in first. Every pass scans the whole tree, so
    # the pass that publishes the last file has also seen this one: once the
    # loop is idle, "nothing was made for c.md" is a fact, not a timeout.
    (tree / "in/c.md").write_text("markdown")
    (tree / "in/a.txt").write_text("alpha")
    (tree / "in/sub/b.txt").write_text("beta")

    assert wait_for(lambda: (tree / "out/sub/b.txt").exists()
                    and (tree / "out/a.txt").exists())
    assert reconciler.wait_idle()
    assert files_under(tree / "out") == {"a.txt", "sub/b.txt"}
    assert (tree / "out/a.txt").read_text() == "ALPHA"
    assert files_under(tree / "in") == {"c.md", "a.txt", "sub/b.txt"}
    assert reconciler.error is None


def test_the_loop_goes_quiet_after_its_own_writes(live) -> None:
    tree, reconciler = live
    (tree / "in/a.txt").write_text("alpha")
    assert wait_for(lambda: (tree / "out/a.txt").exists())
    # Publishing wakes the monitor once more, to journal the output; then
    # nothing is left to do and the passes stop.
    assert wait_for(lambda: reconciler.idle.is_set() and any(
        e["root"] == "out" for e in reconciler.engine.journal.events()))
    settled = reconciler.passes
    time.sleep(0.5)
    assert reconciler.passes == settled


def test_a_modified_file_is_redone(live) -> None:
    tree, reconciler = live
    (tree / "in/a.txt").write_text("alpha")
    assert wait_for(lambda: (tree / "out/a.txt").exists())
    (tree / "in/a.txt").write_text("a longer second draft")
    assert wait_for(lambda: (tree / "out/a.txt").read_text() == "A LONGER SECOND DRAFT")


def test_what_wakes_the_monitor(live) -> None:
    tree, reconciler = live
    relevant = reconciler.relevant
    assert relevant(str(tree / "in/a.txt"))
    assert relevant(str(tree / "out/deep/er/b.txt"))
    assert not relevant(str(tree / "in/.asp-abc123.tmp"))
    assert not relevant(str(tree / "in/.git/objects/ab/cdef"))
    assert not relevant(str(tree / "in/photos/.stversions/a.jpg"))
    assert not relevant(str(tree / "state/asp.db-wal"))
    assert not relevant(str(tree / "elsewhere.txt"))


def test_an_interval_alone_also_reconciles(tmp_path: Path) -> None:
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()
    (tmp_path / "asp.json").write_text(json.dumps({
        "roots": {"in": "in", "out": "out"}, "db": "asp.db",
        "watchers": ["watchers.upper"]}))
    reconciler = Reconciler(Engine.from_config(tmp_path / "asp.json", {}), interval=0.05)
    assert reconciler.describe() == "every 0.05 s"
    reconciler.start()
    try:
        (tmp_path / "in/a.txt").write_text("alpha")
        assert wait_for(lambda: (tmp_path / "out/a.txt").exists())
    finally:
        reconciler.stop()
        reconciler.join(5)
