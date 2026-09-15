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
def thumbnail(event, out, size):
    save(resize(open_image(event.path), size), out)
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
def handler(event, out, **params) -> None
```

**`event`** describes what happened, as recorded in the journal:

| field | meaning |
|---|---|
| `kind` | `created`, `modified`, `deleted`, or `moved` |
| `path` | the file the event is about |
| `source` | `inotify` or `scan`, or `handler:<name>` when another handler's output caused it |
| `id`, `ts` | journal id and timestamp |

Handler-caused events also carry a chain depth, which enforces the runtime cycle
cap. Facts about the file (size, mtime, hash, mimetype) belong on the event too.
*Open:* whether `path` is absolute, root-relative, or both, and how `moved`
carries its old and new paths.

**`out`** is a temporary path the engine chose, or `None` when the rule declares
no output. The handler writes there. After it returns, the engine atomically
renames the file to the path the pattern computes. The engine owns the write: it
names temp files so it can ignore their events, and it publishes only what the
declared output allows.

**Return value:** `None`. Returning normally means success. Raising means failure:
the engine retries with backoff, then parks the job in a dead-letter queue. If a
handler leaves `out` unwritten, that is recorded as "nothing to do this time,"
not treated as an error.

Staleness comes from the **job record**: rule R at version V ran on this input
and produced this output, or nothing. A file-age check alone would re-run
no-output rules on every scan.

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
