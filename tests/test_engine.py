import json
import os
from pathlib import Path

import pytest

from asp import ConfigError, ContractError, Engine
from watchers import record


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    (tmp_path / "in/sub").mkdir(parents=True)
    (tmp_path / "out").mkdir()
    (tmp_path / "in/a.txt").write_text("a")
    (tmp_path / "in/sub/b.txt").write_text("b")
    return tmp_path / "in", tmp_path / "out"


def engine(tmp_path: Path, *watchers: str, roots: dict | None = None) -> Engine:
    config = tmp_path / "asp.json"
    config.write_text(json.dumps({"roots": roots or {"in": "in", "out": "out"},
                                  "watchers": list(watchers)}))
    return Engine.from_config(config, {})


def files_under(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_returning_none_publishes_nothing(tmp_path, roots) -> None:
    engine(tmp_path, "watchers.decline").run_once()
    assert files_under(roots[1]) == set()


def test_a_bad_return_is_a_contract_error(tmp_path, roots) -> None:
    with pytest.raises(ContractError, match="wrong"):
        engine(tmp_path, "watchers.wrong").run_once()
    assert files_under(roots[1]) == set()


def test_a_raising_handler_leaves_no_temp_file(tmp_path, roots) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        engine(tmp_path, "watchers.boom").run_once()
    assert files_under(roots[1]) == set()


def test_a_rule_without_output_gets_none(tmp_path, roots) -> None:
    record.SEEN.clear()
    engine(tmp_path, "watchers.record").run_once()
    assert record.SEEN == [("in", "a.txt", None), ("in", "sub/b.txt", None)]


def test_unprefixed_output_lands_in_the_input_root(tmp_path, roots) -> None:
    engine(tmp_path, "watchers.sibling", roots={"in": "in"}).run_once()
    assert files_under(roots[0]) == {"a.txt", "sub/b.txt", "copies/a.txt", "sub/copies/b.txt"}
    assert (roots[0] / "sub/copies/b.txt").read_text() == "b!"


def test_an_output_over_its_input_is_refused(tmp_path, roots) -> None:
    with pytest.raises(ContractError, match="overwrite"):
        engine(tmp_path, "watchers.inplace", roots={"in": "in"}).run_once()
    assert (roots[0] / "a.txt").read_text() == "a"


def test_published_files_follow_the_umask(tmp_path, roots) -> None:
    old = os.umask(0o027)
    try:
        engine(tmp_path, "watchers.upper").run_once()
    finally:
        os.umask(old)
    assert (roots[1] / "a.txt").stat().st_mode & 0o777 == 0o640


def test_scan_skips_temp_files_and_symlinks(tmp_path, roots) -> None:
    (roots[0] / ".asp-x.tmp").write_text("partial")
    (roots[0] / "link.txt").symlink_to(roots[0] / "a.txt")
    seen = {(e.root, e.rel.as_posix()) for e in engine(tmp_path).scan()}
    assert seen == {("in", "a.txt"), ("in", "sub/b.txt")}


@pytest.mark.parametrize("watchers, message", [
    (["watchers.badroot"], "nowhere"),
    (["watchers.upper", "watchers.upper_again"], "duplicate rule name 'upper'"),
    (["watchers.missing"], "watchers.missing"),
])
def test_load_errors(tmp_path, roots, watchers, message) -> None:
    with pytest.raises(ConfigError, match=message):
        engine(tmp_path, *watchers)
