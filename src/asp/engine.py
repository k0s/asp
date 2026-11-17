"""The engine: load rules from watchers, then reconcile roots by running them."""

import importlib
import os
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from .config import Config, ConfigError, load_config
from .patterns import PatternError, matches, render_output, split_root
from .rules import Event, Rule, rules_of

TEMP_PREFIX = ".asp-"
TEMP_SUFFIX = ".tmp"


class ContractError(RuntimeError):
    """A handler broke the handler contract."""


@dataclass(frozen=True)
class BoundRule:
    """A rule with its patterns split against the config's roots."""
    rule: Rule
    match_root: str | None   # None: the match applies in every root
    match: str
    output_root: str | None  # None: the output lands in the input's root
    output: str | None       # None: a side-effect rule, with no output

    def applies_to(self, event: Event) -> bool:
        return (event.kind in self.rule.on
                and self.match_root in (None, event.root)
                and matches(self.match, event.rel))


def is_temp(name: str) -> bool:
    return name.startswith(TEMP_PREFIX) and name.endswith(TEMP_SUFFIX)


def bind(rule: Rule, roots: Mapping[str, Path]) -> BoundRule:
    try:
        match_root, match = split_root(rule.match, roots)
        output_root, output = (None, None)
        if rule.output is not None:
            output_root, output = split_root(rule.output, roots)
            render_output(output, PurePosixPath("dir/name.ext"))  # fail at load
    except PatternError as exc:
        raise ConfigError(f"rule {rule.name!r}: {exc}") from exc
    return BoundRule(rule, match_root, match, output_root, output)


def load_rules(config: Config) -> list[BoundRule]:
    """Every rule in the configured watcher modules: modules in config order,
    rules in definition order."""
    bound: list[BoundRule] = []
    seen: dict[str, Rule] = {}
    for module_name in config.watchers:
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise ConfigError(f"watcher {module_name!r}: {exc}") from exc
        for value in vars(module).values():
            for rule in rules_of(value):
                if seen.get(rule.name) is rule:
                    continue            # the same handler bound to two names
                if rule.name in seen:
                    raise ConfigError(f"duplicate rule name {rule.name!r}")
                seen[rule.name] = rule
                bound.append(bind(rule, config.roots))
    return bound


def current_umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


class Engine:
    def __init__(self, config: Config, rules: list[BoundRule]) -> None:
        self.config = config
        self.rules = rules
        self.file_mode = 0o666 & ~current_umask()

    @classmethod
    def from_config(cls, path: str | os.PathLike[str],
                    environ: Mapping[str, str] | None = None) -> "Engine":
        config = load_config(path, environ)
        return cls(config, load_rules(config))

    def scan(self) -> Iterator[Event]:
        """Every regular file under every root, as a "created" event. With no
        journal yet, nothing has been seen before. Symlinks and asp's own temp
        files are skipped."""
        ts = datetime.now(UTC)
        n = 0
        for root_name, root in self.config.roots.items():
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames.sort()
                for filename in sorted(filenames):
                    path = Path(dirpath, filename)
                    if is_temp(filename) or not path.is_file(follow_symlinks=False):
                        continue
                    st = path.stat()
                    n += 1
                    yield Event(id=n, ts=ts, kind="created", root=root_name,
                                rel=PurePosixPath(path.relative_to(root).as_posix()),
                                path=path, source="scan",
                                size=st.st_size, mtime=st.st_mtime)

    def run_once(self) -> None:
        """One reconciliation pass: run each applicable rule on each file.
        The file list is taken before any handler runs, so a pass never sees
        its own outputs."""
        for event in list(self.scan()):
            for bound in self.rules:
                if bound.applies_to(event):
                    self.run_job(bound, event)

    def run_job(self, bound: BoundRule, event: Event) -> None:
        rule = bound.rule
        if bound.output is None:
            if rule.handler(event, None, **rule.params) is not None:
                raise ContractError(f"rule {rule.name!r} has no output but returned one")
            return

        root = self.config.roots[bound.output_root or event.root]
        dest = root / render_output(bound.output, event.rel)
        if dest == event.path:
            raise ContractError(f"rule {rule.name!r} would overwrite its input {event.rel}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        out = tempfile.NamedTemporaryFile(dir=dest.parent, prefix=TEMP_PREFIX,
                                          suffix=TEMP_SUFFIX, delete=False)
        temp = Path(out.name)
        try:
            result = rule.handler(event, out, **rule.params)
            if result is not None and result is not out:
                raise ContractError(f"rule {rule.name!r} returned {result!r}, "
                                    "not its out file or None")
        except BaseException:
            out.close()
            temp.unlink(missing_ok=True)
            raise
        out.close()
        if result is None:
            temp.unlink()
            return
        temp.chmod(self.file_mode)
        os.replace(temp, dest)
