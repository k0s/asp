import json
from pathlib import Path

import pytest

from asp import ConfigError
from asp.config import load_config


def write_config(where: Path, data: object) -> Path:
    path = where / "asp.json"
    path.write_text(json.dumps(data))
    return path


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    for d in ("conf/in", "env/in", "abs/in", "home/in"):
        (tmp_path / d).mkdir(parents=True)
    return tmp_path


def test_relative_roots_resolve_against_the_config_directory(tree: Path) -> None:
    config = load_config(write_config(tree / "conf", {"roots": {"in": "in"}, "watchers": []}), {})
    assert config.roots == {"in": tree / "conf/in"}


def test_relative_roots_resolve_against_asp_root_when_set(tree: Path) -> None:
    path = write_config(tree / "conf", {"roots": {"in": "in"}, "watchers": []})
    assert load_config(path, {"ASP_ROOT": str(tree / "env")}).roots == {"in": tree / "env/in"}


def test_empty_asp_root_counts_as_unset(tree: Path) -> None:
    path = write_config(tree / "conf", {"roots": {"in": "in"}, "watchers": []})
    assert load_config(path, {"ASP_ROOT": ""}).roots == {"in": tree / "conf/in"}


def test_absolute_roots_win_over_asp_root(tree: Path) -> None:
    path = write_config(tree / "conf", {"roots": {"in": str(tree / "abs/in")}, "watchers": []})
    assert load_config(path, {"ASP_ROOT": str(tree / "env")}).roots == {"in": tree / "abs/in"}


def test_tilde_is_expanded_and_wins_over_asp_root(tree: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tree / "home"))
    path = write_config(tree / "conf", {"roots": {"in": "~/in"}, "watchers": []})
    assert load_config(path, {"ASP_ROOT": str(tree / "env")}).roots == {"in": tree / "home/in"}


def test_watchers_are_kept_in_order(tree: Path) -> None:
    path = write_config(tree / "conf", {"roots": {"in": "in"}, "watchers": ["b", "a"]})
    assert load_config(path, {}).watchers == ("b", "a")


@pytest.mark.parametrize("data, message", [
    ([], "not a JSON object"),
    ({"roots": {"in": "in"}, "watchers": [], "extra": 1}, "unknown keys"),
    ({"watchers": []}, "'roots'"),
    ({"roots": {}, "watchers": []}, "'roots'"),
    ({"roots": {"in": 1}, "watchers": []}, "'roots'"),
    ({"roots": {"in": "in"}}, "'watchers'"),
    ({"roots": {"in": "in"}, "watchers": "upper"}, "'watchers'"),
    ({"roots": {"9in": "in"}, "watchers": []}, "bad root name"),
    ({"roots": {"in": "missing"}, "watchers": []}, "not a directory"),
])
def test_bad_configs_are_rejected(tree: Path, data: object, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(write_config(tree / "conf", data), {})


def test_unreadable_config_is_rejected(tree: Path) -> None:
    (tree / "conf/asp.json").write_text("{not json")
    with pytest.raises(ConfigError):
        load_config(tree / "conf/asp.json", {})
    with pytest.raises(ConfigError):
        load_config(tree / "nowhere.json", {})
