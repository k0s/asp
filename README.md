# asp

**asp** (asset pipeline) is a filesystem data pipeline. It watches directories,
records every change in a durable journal, and runs rules that react to it.

Status: **pre-code.** This README records the agreed design so the first tests
start from shared ground.

## Nouns

| noun | meaning |
|---|---|
| **engine** | `asp` itself: daemon, journal, reconciler, monitor, CLI, web UI |
| **monitor** | the watchdog/inotify component that feeds filesystem events in |
| **watcher** | an external package of rules, loaded by explicit config declaration |
| **rule** | declarative match + output pattern + version, wrapping a handler |
| **handler** | the rule's body: arbitrary, idempotent Python |

## Rules

A rule is a decorator that configures a generic function. The decorator says
*where* the output goes and *with what parameters*; the function only turns an
input into an output.

```python
@rule(name="thumb-128", version=1, on={"created", "modified"},
      match="**/*.{jpg,png,gif}", output="{dir}/thumbs/128/{name}",
      params={"size": 128})
@rule(name="thumb-1024", version=1, on={"created", "modified"},
      match="**/*.{jpg,png,gif}", output="{dir}/thumbs/1024/{name}",
      params={"size": 1024})
def thumbnail(event: Event, out: Path, size: int) -> Path:
    save(resize(open_image(event.path), size), out)
    return out          # the engine renames it to the declared path
```

A rule with side effects and no output returns `None`:

```python
@rule(name="purge", version=1, on={"created", "modified"},
      match="site/**/*", output=None)
def purge(event: Event, out: None) -> None:
    cdn.purge(event.path)
    return None
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
  named `version` or `match` can't collide with the decorator's own.
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
    path: Path               # the file the event is about
    source: str              # "inotify" | "scan" | "handler:<name>"
    depth: int               # chain depth; 0 unless a handler caused it
    size: int | None         # catalog facts, present when known
    mtime: float | None
    sha256: str | None
    mimetype: str | None

Handler = Callable[..., Path | None]

def handler(event: Event, out: Path | None, **params: Any) -> Path | None: ...

def rule(*, name: str, version: int, on: set[EventKind], match: str,
         output: str | None = None,
         params: dict[str, Any] | None = None) -> Callable[[Handler], Handler]: ...
```

**`out`** is a temporary path the engine chose, or `None` when the rule declares no
output. The engine owns the write: it names temp files so it can ignore their events,
and it publishes only to the declared path.

**The return value says what the handler did**: `out` if it wrote, `None` if it
didn't. That's a statement of intent rather than something the engine has to infer
from the filesystem, where "chose to write nothing" and "failed to write" look alike.

| handler returns | the engine |
|---|---|
| `out` | checks the file exists, then atomically renames it to the declared path |
| `None` | records "nothing to do"; a leftover temp file means a bug and is reported |
| any other path | fails the job — the handler wrote somewhere it wasn't given |
| *raises* | retries with backoff, then parks the job in the dead-letter queue |

For a rule with `output=None`, `out` is `None` and the handler must return `None`.

Staleness comes from the **job record**: rule R at version V ran on this input and
produced this output, or nothing. A file-age check alone would re-run no-output rules
on every scan.

*Open:* whether `event.path` is absolute, root-relative, or both, and how `moved`
carries its old and new paths.

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
- **The monitor never follows symlinks.** Verified against watchdog 6.0.0: it skips
  linked directories and passes `IN_DONT_FOLLOW`, even for a root. Using symlinks
  as pipeline plumbing therefore needs a link index in asp (not yet designed).

## M1: the observatory

Daemon (monitor, journal, reconciler), CLI, localhost web UI, `/metrics`, and
Grafana dashboards, with **zero real handlers**. It is useful on its own, because
it shows what actually changes in synced trees. Done when a sync storm can be
watched live in Grafana and paged through in the UI.

## Development

Test-first. The first test runs an in-process engine against temp `in/` and `out/`
directories, with a rule on `*.txt` whose handler uppercases seeded random text.
It asserts the output appears with the expected contents, and that non-matching
files (`*.md`) produce nothing.

## Open

- **TODO: a multi-output form of the decorator**, for handlers where the outputs
  share expensive work (transcode once, emit several renditions). Stacked rules
  repeat that work. One candidate keeps every static check: a rule declares a
  per-input *directory* (`{dir}/renditions/{name}/`) and the handler fills it
  freely, choosing how many files and what they're named.

- Symlink semantics: the link index, projecting events onto aliases, and
  containment
- Output-pattern grammar: named roots, so `in/` → `out/` can be expressed
- An observable idle state, which negative tests need as a sync point
- Journal retention, batch matching, per-rule settle windows
