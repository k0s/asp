"""The monitor and the reconciler loop.

The monitor only says "something changed": it never tells the engine what.
Each wake-up runs an ordinary reconciliation pass, which finds the changes by
scanning. inotify drops events under load, so the scan stays the authority and
the monitor only shortens the wait. A periodic pass catches what it missed.
"""

import logging
import threading
import time
from pathlib import Path, PurePosixPath

from .engine import Engine, is_temp

log = logging.getLogger("asp")
MAX_SETTLE = 30.0   # a steady stream of changes can't postpone a pass for longer


class Reconciler(threading.Thread):
    """Runs passes in the background: one at start, one `settle` seconds
    after the roots go quiet following a change (with `watch`), and one every
    `interval` seconds regardless."""

    def __init__(self, engine: Engine, *, interval: float | None = None,
                 watch: bool = False, settle: float = 1.0) -> None:
        super().__init__(name="asp-reconcile", daemon=True)
        self.engine = engine
        self.interval = interval
        self.watch = watch
        self.settle = settle
        self.error: str | None = None       # the last pass's failure, if it failed
        self.passes = 0                     # passes completed
        self.idle = threading.Event()       # set while waiting with nothing pending
        self._changed = threading.Event()
        self._stop = threading.Event()
        self._last_change = 0.0
        self._pending = threading.Lock()    # idle and _changed change together
        self._observer = None

    def describe(self) -> str:
        parts = (["watching"] if self.watch else []) + (
            [f"every {self.interval:g} s"] if self.interval else [])
        return ", ".join(parts) or "on demand"

    # --- the monitor ---

    def relevant(self, path: str) -> bool:
        """Whether a changed path is one a pass would look at. The engine's
        own temp files and journal, and ignored paths, must not wake it, or
        its own writes would keep it running."""
        changed = Path(path)
        if is_temp(changed.name) or (self.engine.journal
                                     and changed in self.engine.journal.files):
            return False
        for root in self.engine.config.roots.values():
            if changed.is_relative_to(root):
                rel = PurePosixPath(changed.relative_to(root).as_posix())
                return not any(self.engine.ignored(PurePosixPath(*rel.parts[:n]))
                               for n in range(1, len(rel.parts) + 1))
        return False

    def notify(self) -> None:
        """Something changed (or someone wants a pass): wake the loop."""
        with self._pending:
            self._last_change = time.monotonic()
            self.idle.clear()
            self._changed.set()

    def start_watching(self) -> None:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        reconciler = self

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event) -> None:
                if event.event_type in ("opened", "closed_no_write"):
                    return      # reads, which change nothing
                paths = [event.src_path, getattr(event, "dest_path", "")]
                if any(p and reconciler.relevant(str(p)) for p in paths):
                    reconciler.notify()

        self._observer = Observer()
        for root in self.engine.config.roots.values():
            self._observer.schedule(Handler(), str(root), recursive=True)
        self._observer.start()

    # --- the loop ---

    def run(self) -> None:
        if self.watch:
            self.start_watching()
        try:
            while not self._stop.is_set():
                with self._pending:
                    self._changed.clear()
                    self.idle.clear()
                try:
                    self.engine.run_once()
                    self.error = None
                except Exception as exc:    # keep going; the dashboard shows it
                    log.exception("pass failed")
                    self.error = f"{type(exc).__name__}: {exc}"
                self.passes += 1
                with self._pending:
                    if not self._changed.is_set():
                        self.idle.set()
                if not (self.watch or self.interval):
                    return
                if self._changed.wait(self.interval) and not self._stop.is_set():
                    self.settle_down()
        finally:
            if self._observer is not None:
                self._observer.stop()
                self._observer.join()

    def settle_down(self) -> None:
        """Wait until nothing has changed for `settle` seconds, so a burst of
        writes, or one file still being written, becomes one pass."""
        began = time.monotonic()
        while not self._stop.is_set():
            quiet = time.monotonic() - self._last_change
            if quiet >= self.settle or time.monotonic() - began >= MAX_SETTLE:
                return
            self._stop.wait(self.settle - quiet)

    def stop(self) -> None:
        self._stop.set()
        self._changed.set()

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Block until a pass has finished with no change pending."""
        return self.idle.wait(timeout)
