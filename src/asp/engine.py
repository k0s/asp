"""The engine: load rules from watchers, then reconcile roots by running them."""

import importlib
import os
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from .config import Config, ConfigError, load_config
from .journal import RUNNABLE, Journal, now
from .patterns import PatternError, matches, output_glob, render_output, split_root
from .rules import Event, Rule, rules_of

TEMP_PREFIX = ".asp-"
TEMP_SUFFIX = ".tmp"
MAX_ATTEMPTS = 3    # a job that raises this many times is parked
MAX_DEPTH = 8       # a file this many handlers from an authored one runs nothing


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
    # (root, glob) of what this rule's handler publishes, under any of its
    # rules. A rule never runs on those: a thumbnail of a thumbnail is a loop.
    own_outputs: tuple[tuple[str | None, str], ...] = ()

    def is_own_output(self, root: str, rel: PurePosixPath) -> bool:
        return any(out_root in (None, root) and matches(glob, rel)
                   for out_root, glob in self.own_outputs)

    def overwrites(self, event: Event) -> bool:
        """Whether this rule's output for the event is the event's own file."""
        return (self.output is not None
                and self.output_root in (None, event.root)
                and render_output(self.output, event.rel) == event.rel)

    def applies_to(self, event: Event) -> bool:
        # A rule that would overwrite its input still applies, so that running
        # it fails loudly instead of the rule silently never matching.
        return (event.kind in self.rule.on
                and self.match_root in (None, event.root)
                and matches(self.match, event.rel)
                and (self.overwrites(event)
                     or not self.is_own_output(event.root, event.rel)))


@dataclass
class Pass:
    """What one reconciliation pass did."""
    files: int = 0
    events: int = 0
    jobs: dict[str, int] = field(default_factory=dict)   # by final state

    def count(self, state: str) -> None:
        self.jobs[state] = self.jobs.get(state, 0) + 1


@dataclass(frozen=True)
class Planned:
    """A job a pass would run."""
    rule: str
    kind: str                       # the event's kind
    root: str
    rel: str
    output: tuple[str, str] | None  # (root, rel) it would publish, if any
    overwrites: bool                # that output is already on disk
    why: str                        # "new", or the state of a job waiting to rerun


@dataclass
class Plan:
    """What a pass would do: the result of a dry run."""
    files: int = 0
    events: dict[str, int] = field(default_factory=dict)    # by kind
    jobs: list[Planned] = field(default_factory=list)


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
    outputs: dict[int, list[tuple[str | None, str]]] = {}
    for b in bound:
        if b.output is not None:
            outputs.setdefault(id(b.rule.handler), []).append(
                (b.output_root, output_glob(b.output)))
    return [BoundRule(b.rule, b.match_root, b.match, b.output_root, b.output,
                      tuple(outputs.get(id(b.rule.handler), ())))
            for b in bound]


def current_umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


class Engine:
    def __init__(self, config: Config, rules: list[BoundRule],
                 create_journal: bool = True) -> None:
        self.config = config
        self.rules = rules
        self.by_name = {b.rule.name: b for b in rules}
        self.file_mode = 0o666 & ~current_umask()
        # With create_journal=False a journal that doesn't exist yet is left
        # that way, and the engine plans as if it had no history.
        self.journal = (Journal(config.db)
                        if config.db and (create_journal or config.db.exists()) else None)
        self.lock = threading.Lock()    # one pass at a time

    @classmethod
    def from_config(cls, path: str | os.PathLike[str],
                    environ: Mapping[str, str] | None = None,
                    create_journal: bool = True) -> "Engine":
        config = load_config(path, environ)
        return cls(config, load_rules(config), create_journal)

    def ignored(self, rel: PurePosixPath) -> bool:
        return any(matches(glob, rel) for glob in self.config.ignore)

    def walk(self) -> Iterator[tuple[str, PurePosixPath, Path, os.stat_result]]:
        """Every regular file under every root, in a stable order. Symlinks,
        ignored paths, asp's own temp files and the journal are skipped."""
        own = self.journal.files if self.journal else frozenset()
        for root_name, root in self.config.roots.items():
            for dirpath, dirnames, filenames in os.walk(root):
                here = PurePosixPath(Path(dirpath).relative_to(root).as_posix())
                dirnames[:] = sorted(d for d in dirnames if not self.ignored(here / d))
                for filename in sorted(filenames):
                    path, rel = Path(dirpath, filename), here / filename
                    if (is_temp(filename) or self.ignored(rel) or path in own
                            or not path.is_file(follow_symlinks=False)):
                        continue
                    try:
                        st = path.stat()
                    except FileNotFoundError:   # gone since it was listed
                        continue
                    yield root_name, rel, path, st

    def scan(self) -> Iterator[Event]:
        """Every file as a "created" event: what a pass sees with no journal,
        where nothing has been seen before."""
        ts = datetime.now(UTC)
        for n, (root, rel, path, st) in enumerate(self.walk(), 1):
            yield Event(id=n, ts=ts, kind="created", root=root, rel=rel, path=path,
                        source="scan", size=st.st_size, mtime=st.st_mtime)

    def run_once(self) -> Pass:
        """One reconciliation pass. The file list is taken before any handler
        runs, so a pass never sees its own outputs.

        Without a journal every file is new, every applicable rule runs, and a
        handler that raises aborts the pass. With one, only what changed since
        the last pass raises events, and a failing job is recorded and retried
        on later passes rather than stopping this one."""
        with self.lock:
            if self.journal is not None:
                return self.reconcile(self.journal)
            done = Pass()
            for event in list(self.scan()):
                done.files += 1
                done.events += 1
                for bound in self.rules:
                    if bound.applies_to(event):
                        published = self.run_job(bound, event)
                        done.count("done" if published or bound.output is None
                                   else "skipped")
            return done

    def reconcile(self, journal: Journal) -> Pass:
        done = Pass()
        ts = now()
        found = {(root, rel.as_posix()): st for root, rel, _, st in self.walk()}
        done.files = len(found)
        with journal.connect() as db:
            pass_id = db.execute("insert into passes(started) values (?)", (ts,)).lastrowid
            known = {(r["root"], r["rel"]): r for r in db.execute("select * from catalog")}
            changes = self.diff(found, known)
            for kind, root, rel in changes:
                st = found.get((root, rel))
                maker = self.maker(db, root, rel)
                depth = maker["depth"] + 1 if maker else 0
                source = f"handler:{maker['rule']}" if maker else "scan"
                size, mtime = (st.st_size, st.st_mtime) if st else (None, None)
                event_id = db.execute(
                    "insert into events(ts, pass_id, source, kind, root, rel, depth, size, mtime)"
                    " values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (ts, pass_id, source, kind, root, rel, depth, size, mtime)).lastrowid
                if kind == "deleted":
                    db.execute("delete from catalog where root = ? and rel = ?", (root, rel))
                else:
                    db.execute(
                        "insert into catalog(root, rel, size, mtime, first_seen, last_seen,"
                        " depth, derived_by) values (?, ?, ?, ?, ?, ?, ?, ?) "
                        "on conflict(root, rel) do update set size = excluded.size, "
                        "mtime = excluded.mtime, last_seen = excluded.last_seen, "
                        "depth = excluded.depth, derived_by = excluded.derived_by",
                        (root, rel, size, mtime, ts, ts, depth, maker["rule"] if maker else None))
                event = self.event(event_id, ts, kind, root, rel, source, depth, size, mtime)
                for bound in self.rules:
                    if bound.applies_to(event):
                        over = depth >= MAX_DEPTH
                        db.execute(
                            "insert into jobs(ts, rule, event_id, state, error) "
                            "values (?, ?, ?, ?, ?)",
                            (ts, bound.rule.name, event_id, "parked" if over else "queued",
                             f"depth cap: {depth} handlers from an authored file"
                             if over else None))
                        if over:
                            done.count("parked")
            done.events = len(changes)
            runnable = db.execute(
                "select j.id, j.rule, j.attempts, e.id as event_id, e.ts, e.kind, e.root,"
                " e.rel, e.source, e.depth, e.size, e.mtime from jobs j "
                "join events e on e.id = j.event_id "
                f"where j.state in ({', '.join('?' * len(RUNNABLE))}) order by j.id",
                RUNNABLE).fetchall()

        # Each job commits on its own, so a crash loses at most the one running.
        for job in runnable:
            state, output, error = self.attempt(job)
            if state == "failed" and job["attempts"] + 1 >= MAX_ATTEMPTS:
                state = "parked"
            done.count(state)
            with journal.connect() as db:
                db.execute(
                    "update jobs set state = ?, attempts = attempts + 1, output_root = ?,"
                    " output_rel = ?, finished = ?, duration = ?, error = ? where id = ?",
                    (state, *output, now(), error[1], error[0], job["id"]))
        with journal.connect() as db:
            db.execute("update passes set finished = ?, files = ?, events = ?, jobs = ? "
                       "where id = ?", (now(), done.files, done.events, len(runnable), pass_id))
        return done

    def diff(self, found: Mapping, known: Mapping) -> list[tuple[str, str, str]]:
        """(kind, root, rel) for each file whose size or mtime differs from
        the catalog's, or that only one of them has."""
        changes = []
        for key, st in found.items():
            was = known.get(key)
            if was is None:
                changes.append(("created", *key))
            elif (was["size"], was["mtime"]) != (st.st_size, st.st_mtime):
                changes.append(("modified", *key))
        # A root missing from this config is out of scope, not deleted.
        changes += [("deleted", *key) for key in known
                    if key not in found and key[0] in self.config.roots]
        return changes

    @staticmethod
    def maker(db, root: str, rel: str):
        """The job that published a file, if one did. Such a file is derived:
        one step deeper than the input it was made from."""
        return db.execute(
            "select j.rule, e.depth from jobs j join events e on e.id = j.event_id "
            "where j.output_root = ? and j.output_rel = ? and j.state = 'done' "
            "order by j.id desc limit 1", (root, rel)).fetchone()

    def plan(self) -> "Plan":
        """What the next pass would do, without doing it: nothing is written
        to the roots or the journal, and no handler runs."""
        with self.lock:
            found = {(root, rel.as_posix()): st for root, rel, _, st in self.walk()}
            plan = Plan(files=len(found))
            ts = now()
            if self.journal is None:
                known, pending = {}, []
                changes = self.diff(found, known)
                depths = {}
            else:
                with self.journal.connect() as db:
                    known = {(r["root"], r["rel"]): r
                             for r in db.execute("select * from catalog")}
                    changes = self.diff(found, known)
                    depths = {}
                    for _, root, rel in changes:
                        maker = self.maker(db, root, rel)
                        if maker:
                            depths[root, rel] = maker["depth"] + 1
                    pending = db.execute(
                        "select j.rule, j.state, e.kind, e.root, e.rel from jobs j "
                        "join events e on e.id = j.event_id "
                        f"where j.state in ({', '.join('?' * len(RUNNABLE))}) order by j.id",
                        RUNNABLE).fetchall()
            for job in pending:
                bound = self.by_name.get(job["rule"])
                if bound is not None and job["root"] in self.config.roots:
                    plan.jobs.append(self.planned(bound, job["kind"], job["root"],
                                                  PurePosixPath(job["rel"]), job["state"]))
            for kind, root, rel in changes:
                plan.events[kind] = plan.events.get(kind, 0) + 1
                st = found.get((root, rel))
                depth = depths.get((root, rel), 0)
                event = self.event(0, ts, kind, root, rel, "scan", depth,
                                   st.st_size if st else None, st.st_mtime if st else None)
                for bound in self.rules:
                    if bound.applies_to(event):
                        plan.jobs.append(self.planned(
                            bound, kind, root, event.rel,
                            "over the depth cap" if depth >= MAX_DEPTH else "new"))
            return plan

    def planned(self, bound: BoundRule, kind: str, root: str, rel: PurePosixPath,
                why: str) -> "Planned":
        output, exists = None, False
        if bound.output is not None:
            out_root = bound.output_root or root
            out_rel = render_output(bound.output, rel)
            output = (out_root, out_rel.as_posix())
            exists = (self.config.roots[out_root] / out_rel).exists()
        return Planned(rule=bound.rule.name, kind=kind, root=root, rel=rel.as_posix(),
                       output=output, overwrites=exists, why=why)

    def event(self, event_id: int, ts: str, kind: str, root: str, rel: str,
              source: str, depth: int, size: int | None, mtime: float | None) -> Event:
        return Event(id=event_id, ts=datetime.fromisoformat(ts), kind=kind, root=root,
                     rel=PurePosixPath(rel), path=self.config.roots[root] / rel,
                     source=source, depth=depth, size=size, mtime=mtime)

    def attempt(self, job: Mapping) -> tuple[str, tuple[str | None, str | None],
                                              tuple[str | None, float]]:
        """Run one journaled job: (state, (output root, rel), (error, seconds))."""
        started = time.monotonic()
        state, output, error = "done", (None, None), None
        bound = self.by_name.get(job["rule"])
        try:
            if bound is None:
                raise LookupError(f"no rule named {job['rule']!r} is loaded")
            if job["root"] not in self.config.roots:
                raise LookupError(f"no root named {job['root']!r} is configured")
            event = self.event(job["event_id"], job["ts"], job["kind"], job["root"],
                               job["rel"], job["source"], job["depth"], job["size"],
                               job["mtime"])
            if event.kind != "deleted" and not event.path.is_file():
                state, error = "skipped", "the input is gone"
            else:
                published = self.run_job(bound, event)
                if published is not None:
                    output = published
                elif bound.output is not None:
                    state = "skipped"
        except Exception as exc:
            state, error = "failed", self.portable(f"{type(exc).__name__}: {exc}")
        return state, output, (error, time.monotonic() - started)

    def portable(self, text: str) -> str:
        """Text with absolute root paths rewritten as `root:`, since nothing
        the journal keeps may depend on where this host mounts things."""
        for name, path in sorted(self.config.roots.items(),
                                 key=lambda item: -len(str(item[1]))):
            text = text.replace(f"{path}{os.sep}", f"{name}:").replace(str(path), f"{name}:")
        return text

    def run_job(self, bound: BoundRule, event: Event) -> tuple[str, str] | None:
        """Run a rule's handler on an event. Returns the (root, rel) it
        published, or None when it published nothing."""
        rule = bound.rule
        if bound.output is None:
            if rule.handler(event, None, **rule.params) is not None:
                raise ContractError(f"rule {rule.name!r} has no output but returned one")
            return None

        out_root = bound.output_root or event.root
        out_rel = render_output(bound.output, event.rel)
        dest = self.config.roots[out_root] / out_rel
        if dest == event.path:
            raise ContractError(f"rule {rule.name!r} would overwrite its input {event.rel}")
        # The temp file goes in the nearest directory that already exists on
        # the way to the destination: the same filesystem (a directory that
        # doesn't exist yet can't be a mount point), so publishing is still one
        # atomic rename. The destination's own directory is made only when
        # there is something to publish, so a handler that declines leaves
        # nothing behind.
        staging = dest.parent
        while not staging.is_dir():
            staging = staging.parent
        out = tempfile.NamedTemporaryFile(dir=staging, prefix=TEMP_PREFIX,
                                          suffix=TEMP_SUFFIX, delete=False)
        temp = Path(out.name)
        try:
            result = rule.handler(event, out, **rule.params)
            if result is not None and result is not out:
                raise ContractError(f"rule {rule.name!r} returned {result!r}, "
                                    "not its out file or None")
            out.close()
            if result is None:
                temp.unlink()
                return None
            temp.chmod(self.file_mode)
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temp, dest)
        except BaseException:
            out.close()
            temp.unlink(missing_ok=True)
            raise
        return out_root, out_rel.as_posix()
