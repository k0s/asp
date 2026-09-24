# asp

**asp** (asset pipeline) is a filesystem data pipeline. It watches directories,
records every change in a durable journal, and runs rules that react to it.

Status: **pre-code.** This README records the agreed design, and the first test
(expected to fail until the engine exists) pins down the entry point.

asp is meant to run equally well **as a container**: an input volume, an output
volume, and a config. The in-process test runs the same arrangement, with temp
directories standing in for the mounts.

## Nouns

| noun | meaning |
|---|---|
| **engine** | `asp` itself: daemon, journal, reconciler, monitor, CLI, web UI |
| **monitor** | the watchdog/inotify component that feeds filesystem events in |
| **watcher** | an external package of rules, loaded by explicit config declaration |
| **rule** | declarative match + output pattern, wrapping a handler |
| **handler** | the rule's body: arbitrary, idempotent Python |

## Rules

A rule is a decorator that configures a generic function. The decorator says
*where* the output goes and *with what parameters*; the function only turns an
input into an output.

```python
@rule(name="thumb-128", on={"created", "modified"},
      match="**/*.{jpg,png,gif}", output="{dir}/thumbs/128/{name}",
      params={"size": 128})
@rule(name="thumb-1024", on={"created", "modified"},
      match="**/*.{jpg,png,gif}", output="{dir}/thumbs/1024/{name}",
      params={"size": 1024})
def thumbnail(event: Event, out: BinaryIO | None, size: int) -> BinaryIO | None:
    if out is None or not is_image(event.path):
        return None                 # nothing to do; the engine records that
    save(resize(open_image(event.path), size), out, format=image_format(event.path))
    return out                      # the engine publishes it to the declared path
```

### Roots and patterns

A **root** is a named directory that asp manages. Rules refer to roots by name,
and `asp.json` binds each name to a path, so a watcher never contains a
host-specific path:

```json
{"roots": {"in": "/home/me/site", "out": "/home/me/derived"},
 "watchers": ["snakewatchers.thumbs"]}
```

Patterns may carry a **root prefix**, `<root>:<pattern>`:

```python
match="in:**/*.txt"          # any .txt anywhere under the "in" root
output="out:{dir}/{name}"    # the same relative spot under the "out" root
```

The rest of the pattern is relative to that root: `{dir}` is the input's
directory relative to its root, and `{name}` is its filename. So
`in:sub/b.txt` becomes `out:sub/b.txt`. With no prefix, a `match` applies in
every root, and an `output` lands in the input's own root, which is why the
thumbnail rule above writes next to its image. Text before the first `:` is a
root only if it names a declared root; referencing an undeclared one is a
load-time error.

### What a handler receives, and what it returns

| parameter | type | meaning |
|---|---|---|
| `event` | `Event` | what happened: kind, path, source, journal id and timestamp, plus file facts when they are known. Full shape under [Handler contract](#handler-contract). |
| `out` | `BinaryIO \| None` | a temp file the engine opened (binary, writable) for this rule's declared output, or `None` when the rule declares `output=None`. The handler writes to it and never computes a destination itself. `out.name` is its path, for tools that need one. |
| `**params` | whatever the rule declared | the decorator's `params` dict, passed through unchanged (`size=128` above). These are the function's own arguments; the engine does not interpret them. |

**Returns `BinaryIO | None`** — `out` to publish what it wrote, or `None` for
"nothing to do." The engine then atomically renames the temp file to the path the
pattern computes, or deletes it. The file exists either way, so the return value
is what tells "wrote an empty file" apart from "wrote nothing."

Every handler shares this one signature and may always return `None`, so the
annotation is `BinaryIO | None` even for `thumbnail`. The engine resolves the union
per rule: a rule with an `output` pattern always passes a file, and a rule with
`output=None` always passes `None`.

`out` is binary. A text handler encodes (`out.write(s.encode())`) rather than
wrapping `out` in `io.TextIOWrapper`, which would close the engine's file when
it is garbage-collected. A `mode=` keyword, spelled like `open()`'s, is reserved
for later: text output, and editing an existing output copy-on-write.

A rule with side effects and no output returns `None`:

```python
@rule(name="purge", on={"created", "modified"},
      match="site:**/*", output=None)
def purge(event: Event, out: BinaryIO | None) -> BinaryIO | None:
    cdn.purge(event.path)
    return None                     # nothing written, ever
```

- **Zero or one output per rule.** `output` is one invertible path pattern, or
  `None` for rules with side effects only (purge, notify). Multiple outputs come
  from stacking rules on one function, and each rule is its own job with its own
  retries. Invertibility is what makes cycle detection, staleness checks,
  self-ignore, and orphan detection possible. It also means derived files can be
  told apart from authored ones without the database.
- **`output` is a pattern, never code.** The function never knows its output path
  or how many siblings it has.
- **Parameters go in `params`**, not loose decorator keywords, so a handler argument
  named `match` or `output` can't collide with the decorator's own.
- **Handlers are unordered and independent.** Delivery is at-least-once, so handlers
  must be idempotent. To get "A then B," chain through directories: stage N's
  output is stage N+1's match. The DAG is computed from the rules, never written.
- **Cycles:** a static check at registration, plus runtime provenance and a depth cap.

## Handler contract

```python
EventKind = Literal["created", "modified", "deleted", "moved"]

@dataclass(frozen=True)
class Event:
    id: int                  # journal id
    ts: datetime
    kind: EventKind
    root: str                # the root it is under, by name
    rel: PurePosixPath       # its path relative to that root: the identity
    path: Path               # its absolute path on this host, for reading
    source: str              # "inotify" | "scan" | "handler:<name>"
    depth: int               # chain depth; 0 unless a handler caused it
    size: int | None         # catalog facts, present when known
    mtime: float | None
    sha256: str | None
    mimetype: str | None

Handler = Callable[..., BinaryIO | None]

def handler(event: Event, out: BinaryIO | None, **params: Any) -> BinaryIO | None: ...

def rule(*, name: str, on: set[EventKind], match: str,
         output: str | None = None,
         params: dict[str, Any] | None = None) -> Callable[[Handler], Handler]: ...
```

**`out`** is a temporary file the engine opened in the destination's directory, or
`None` when the rule declares no output. The engine owns the write: it names temp
files so it can ignore their events, keeps them on the destination's filesystem so
publishing is one atomic rename, and publishes only to the declared path. It
publishes by path, not through the handle, so a tool that replaces the file at
`out.name` works too.

**The return value says what the handler did**: `out` if it wrote, `None` if it
didn't. That's a statement of intent rather than something the engine has to infer
from the filesystem, where "chose to write nothing" and "failed to write" look alike.

| handler returns | the engine |
|---|---|
| `out` | closes it, then atomically renames it to the declared path |
| `None` | records "nothing to do" and deletes the temp file |
| anything else | fails the job — a contract violation |
| *raises* | retries with backoff, then parks the job in the dead-letter queue |

For a rule with `output=None`, `out` is `None` and the handler must return `None`.

Staleness comes from the **job record**: rule R ran on this input and produced this
output, or nothing. A file-age check alone would re-run no-output rules on every scan.

The journal records `root` and `rel`, never the absolute path, because absolute
paths differ between a host and a container over the same volumes.

*Open:* how `moved` carries its old and new paths.

## Decided

- **The path is the identity.** Handlers receive paths and may act on them, so
  identical bytes at two paths are two inputs. Nothing is deduplicated; a content
  hash is stored for queries only.
- **SQLite is the record for events and job state only.** The catalog is a
  rebuildable index over the filesystem. Deleting the database loses only history
  and queued work.
- **Reconciliation is authoritative.** A periodic scan is the source of truth;
  inotify only reduces latency, because it drops events.
- **Config is JSON only** (`asp.json`). No TOML anywhere, except `pyproject.toml`,
  which Python packaging requires.
- **Metrics come from a Prometheus exporter** at `/metrics`, scraped rather than
  pushed, so the TSDB stays a swappable view.
- **Watchers live outside this repo** and load from an explicit import list, with
  no entry-point discovery and no hot reload. The first is `snakewatchers`.
- **Single writer:** handlers run only on the always-on host. Everywhere else the
  engine only observes.
- **The container is a first-class way to run asp**: mount inputs and outputs,
  supply `asp.json`, and use it that way. Nothing in the engine may assume a bare
  host.
- **The monitor never follows symlinks.** Verified against watchdog 6.0.0: it skips
  linked directories and passes `IN_DONT_FOLLOW`, even for a root. Using symlinks
  as pipeline plumbing therefore needs a link index in asp (not yet designed).

## M1: the observatory

Daemon (monitor, journal, reconciler), CLI, localhost web UI, `/metrics`, and
Grafana dashboards, with **zero real handlers**. It is useful on its own, because
it shows what actually changes in synced trees. Done when a sync storm can be
watched live in Grafana and paged through in the UI.

## Development

Test-first, with `uv`:

```sh
uv sync --group dev
uv run pytest
```

The first test ([`tests/test_run_once.py`](tests/test_run_once.py)) builds an
in-process engine from an `asp.json` binding temp `in/` and `out/` roots, with one
fixture watcher whose rule uppercases `in:**/*.txt` into `out:{dir}/{name}`. After
a single `run_once()` reconciliation pass, `out/` must hold exactly the uppercased
`.txt` files (so `*.md` produced nothing and no temp file leaked), and `in/` must
be unchanged. A pass is synchronous, so the negative case needs no idle signal.

Tests written ahead of the code are marked `xfail`; `xfail_strict` makes an
unexpected pass fail the run, so each marker comes off as soon as the code lands.

## Open

- **Invalidating derived outputs when the generator changes.** If a handler's logic
  changes, existing outputs are wrong, and nothing on disk says so: the inputs didn't
  change, so mtime comparisons see nothing. **Deliberately not decided.** A declared
  generator version on the rule is one option; caching against the handler's own code,
  or an explicit cache-invalidation command, are others. Not in the current plan —
  settle the design before writing the code that assumes it.
- **TODO: a multi-output form of the decorator**, for handlers where the outputs
  share expensive work (transcode once, emit several renditions). Stacked rules
  repeat that work. One candidate keeps every static check: a rule declares a
  per-input *directory* (`{dir}/renditions/{name}/`) and the handler fills it
  freely, choosing how many files and what they're named.
- **Modifying the input in place.** Renaming or rewriting an authored file (for
  example, date-stamping a new post) fits only as an `output=None` rule today: the
  engine can't verify it, and the change raises a new event on the input, so only
  the rule's own match keeps it from looping.
- **Many-to-one outputs.** An index page, a feed, or a gallery depends on a *set*
  of inputs. Nothing in the rule model expresses that yet; a blogging engine will
  need it.

- Symlink semantics: the link index, projecting events onto aliases, and
  containment
- An observable idle state, so a test against the live daemon can assert that
  nothing happened without sleeping
- Where the journal lives (in a container, likely a third volume), and how
  relative paths in `asp.json` resolve
- Journal retention, per-rule settle windows
