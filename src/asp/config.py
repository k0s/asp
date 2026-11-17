"""asp.json: the roots asp manages and the watchers it loads."""

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .patterns import ROOT_NAME

ROOT_ENV = "ASP_ROOT"
KEYS = frozenset({"roots", "watchers"})


class ConfigError(ValueError):
    """asp.json, or something it names, can't be loaded."""


@dataclass(frozen=True)
class Config:
    path: Path
    roots: dict[str, Path]
    watchers: tuple[str, ...]


def resolve_root(value: str, base: Path) -> Path:
    """Expand ~; an absolute path is used as given, a relative one joins base."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else base / path


def load_config(path: str | os.PathLike[str],
                environ: Mapping[str, str] | None = None) -> Config:
    """Read and check asp.json. Relative roots resolve against $ASP_ROOT when it
    is set, otherwise against the config file's directory."""
    environ = os.environ if environ is None else environ
    path = Path(path).expanduser().absolute()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: not a JSON object")
    unknown = set(data) - KEYS
    if unknown:
        raise ConfigError(f"{path}: unknown keys {sorted(unknown)}")
    roots, watchers = data.get("roots"), data.get("watchers")
    if not (isinstance(roots, dict) and roots
            and all(isinstance(v, str) for v in roots.values())):
        raise ConfigError(f"{path}: 'roots' must map names to paths")
    if not (isinstance(watchers, list) and all(isinstance(w, str) for w in watchers)):
        raise ConfigError(f"{path}: 'watchers' must be a list of module names")

    base = (Path(environ[ROOT_ENV]).expanduser().absolute()
            if environ.get(ROOT_ENV) else path.parent)
    resolved = {}
    for name, value in roots.items():
        if not ROOT_NAME.fullmatch(name):
            raise ConfigError(f"{path}: bad root name {name!r}")
        root = resolve_root(value, base)
        if not root.is_dir():
            raise ConfigError(f"{path}: root {name!r} is not a directory: {root}")
        resolved[name] = root
    return Config(path=path, roots=resolved, watchers=tuple(watchers))
