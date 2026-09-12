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

```python
@rule(name="thumbnail", version=3, on={"created", "modified"},
      match="**/*.{jpg,png,gif}", output="{dir}/thumbs/thumb_{name}")
def thumbnail(event, ctx):
    make_thumbnail(event.path, ctx.output)  # see "Handler contract" below
```

- **`output` is a pattern, never code.** It must be invertible. That is what makes
  cycle detection, staleness checks, self-ignore, and orphan detection possible.
- **Handlers are unordered and independent.** Delivery is at-least-once, so handlers
  must be idempotent. To get "A then B," chain through directories: stage N's
  output is stage N+1's match. The DAG is computed from the rules, never written.
- **Cycles:** a static check at registration, plus runtime provenance and a depth cap.

## Handler contract

`handler(event, ctx) -> None`. Parts of this are known; the rest is proposed.

**`event`**: what happened, as recorded in the journal. *Known:*

| field | meaning |
|---|---|
| `kind` | `created`, `modified`, `deleted`, or `moved` |
| `path` | the file the event is about |
| `source` | `inotify` or `scan`, or `handler:<name>` when another handler's output caused it |
| `id`, `ts` | journal id and timestamp |

Handler-caused events also carry a chain depth, which enforces the runtime cycle
cap. *Open:* whether `path` is absolute, root-relative, or both (named roots in the
output grammar suggest both), and how `moved` carries its old and new paths.

**`ctx`**: *not yet defined.* Proposed contents, each one implied by the design:

- `ctx.output`: the output path computed from the rule's `output` pattern. Handlers
  write there instead of computing their own path, so the pattern stays the single
  source of truth for cycles, staleness, and self-ignore.
- An atomic-write helper (temp file, then rename), because handlers must not leave
  partial files where other rules can see them.
- The rule's `name` and `version`.
- Read access to catalog facts (size, mtime, hash, mimetype), so handlers don't
  recompute them.
- Later, not in the MVP: enough information for a handler to decide its own cache hits.

**Return value**: *not yet decided.* Proposed: `None`. Returning normally means
success. Raising means failure: the engine retries with backoff, then parks the job
in a dead-letter queue. The engine already knows the declared output from the
pattern, so returning the written paths would only duplicate it and let the two
disagree.

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

- Symlink semantics: the link index, projecting events onto aliases, and
  containment
- Output-pattern grammar: named roots, so `in/` → `out/` can be expressed
- An observable idle state, which negative tests need as a sync point
- Journal retention, batch matching, per-rule settle windows
