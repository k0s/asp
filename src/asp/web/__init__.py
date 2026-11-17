"""The dashboard: a read-mostly web UI over the journal, for localhost."""

import logging
import math
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..engine import Engine
from ..journal import JOB_STATES

log = logging.getLogger("asp")
TEMPLATES = Path(__file__).parent / "templates"
KINDS = ("created", "modified", "deleted")
PAGE = 100
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".avif"}


def human_bytes(n: int | None) -> str:
    if n is None:
        return ""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def short_time(ts: str | None) -> str:
    """An ISO timestamp as local 'YYYY-MM-DD HH:MM:SS'."""
    if not ts:
        return ""
    return datetime.fromisoformat(ts).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    return f"{seconds * 1000:.0f} ms" if seconds < 1 else f"{seconds:.1f} s"


def is_image(rel: str | None) -> bool:
    return bool(rel) and Path(rel).suffix.lower() in IMAGE_SUFFIXES


def nice_ceiling(n: int) -> int:
    """The smallest of 4, 8, 12, 20, 40, 80, 120, 200... at or above n: an
    axis top whose quarter ticks are whole numbers."""
    if n <= 4:
        return 4
    scale = 10 ** math.floor(math.log10(n))
    return next(m * scale // 10 for m in (12, 20, 40, 80, 120) if m * scale // 10 >= n)


def activity_chart(buckets: list[dict[str, Any]]) -> dict[str, Any]:
    """Geometry for the stacked hourly columns, computed here so the template
    only draws."""
    width, height, left, bottom, top = 960, 200, 40, 24, 8
    plot_w, plot_h = width - left - 8, height - top - bottom
    band = plot_w / max(len(buckets), 1)
    bar = min(24.0, band - 4)
    peak = nice_ceiling(max((sum(b[k] for k in KINDS) for b in buckets), default=0))
    columns = []
    for i, bucket in enumerate(buckets):
        x = left + i * band + (band - bar) / 2
        y = float(top + plot_h)
        segments = []
        for kind in KINDS:
            h = bucket[kind] / peak * plot_h
            if h <= 0:
                continue
            y -= h
            # a 2px gap in the surface colour separates stacked segments
            segments.append({"kind": kind, "x": x, "y": y, "w": bar,
                             "h": max(h - 2, 1)})
        hour = datetime.fromisoformat(bucket["hour"]).astimezone()
        columns.append({
            "x": left + i * band, "w": band, "segments": segments,
            "label": hour.strftime("%a %H:00"), "counts": {k: bucket[k] for k in KINDS},
            "tick": hour.strftime("%H:00") if hour.hour % 6 == 0 else None,
            "day": hour.strftime("%a") if hour.hour == 0 else None,
            "cx": left + i * band + band / 2})
    ticks = [{"value": peak * q // 4, "y": top + plot_h - plot_h * q / 4}
             for q in range(5)]
    return {"width": width, "height": height, "left": left, "top": top,
            "plot_h": plot_h, "right": width - 8, "columns": columns, "ticks": ticks,
            "total": sum(sum(b[k] for k in KINDS) for b in buckets)}


def metrics_text(engine: Engine) -> str:
    """The journal's counts in Prometheus text format."""
    summary = engine.journal.summary()
    lines = ["# TYPE asp_up gauge", "asp_up 1",
             "# TYPE asp_events gauge", f"asp_events {summary['events_total']}",
             "# TYPE asp_catalog_files gauge"]
    lines += [f'asp_catalog_files{{root="{r["root"]}"}} {r["files"]}'
              for r in summary["roots"]]
    lines.append("# TYPE asp_catalog_bytes gauge")
    lines += [f'asp_catalog_bytes{{root="{r["root"]}"}} {r["bytes"]}'
              for r in summary["roots"]]
    lines.append("# TYPE asp_jobs gauge")
    lines += [f'asp_jobs{{state="{state}"}} {summary["jobs"].get(state, 0)}'
              for state in JOB_STATES]
    last = summary["last_pass"]
    if last:
        lines += ["# TYPE asp_last_pass_timestamp_seconds gauge",
                  "asp_last_pass_timestamp_seconds "
                  f"{datetime.fromisoformat(last['finished']).timestamp():.3f}"]
    return "\n".join(lines) + "\n"


def create_app(engine: Engine, interval: float | None = None) -> FastAPI:
    """The dashboard for an engine that has a journal. With `interval`, a
    background thread also runs a reconciliation pass that often."""
    journal = engine.journal
    if journal is None:
        raise ValueError("the dashboard needs a journal: set \"db\" in asp.json")
    state: dict[str, Any] = {"error": None, "interval": interval}
    stop = threading.Event()

    def reconcile_forever() -> None:
        while not stop.is_set():
            try:
                engine.run_once()
                state["error"] = None
            except Exception as exc:    # keep reconciling; show it on the page
                log.exception("pass failed")
                state["error"] = f"{type(exc).__name__}: {exc}"
            stop.wait(interval)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if interval:
            threading.Thread(target=reconcile_forever, name="asp-reconcile",
                             daemon=True).start()
        yield
        stop.set()

    app = FastAPI(title="asp", lifespan=lifespan, docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=TEMPLATES)
    templates.env.filters.update(bytes=human_bytes, when=short_time, duration=duration)
    templates.env.tests["image"] = is_image
    templates.env.globals.update(
        kinds=KINDS, job_states=JOB_STATES,
        query=lambda **params: urlencode({k: v for k, v in params.items()
                                          if v not in (None, "")}))

    def page(request: Request, name: str, **context: Any):
        summary = journal.summary()
        return templates.TemplateResponse(request, name, {
            "summary": summary, "state": state, "config": engine.config,
            "attention": summary["jobs"].get("failed", 0) + summary["jobs"].get("parked", 0),
            **context})

    @app.get("/")
    def overview(request: Request):
        return page(request, "overview.html",
                    chart=activity_chart(journal.activity(48)),
                    events=journal.events(limit=12), jobs=journal.jobs(limit=12),
                    passes=journal.passes(8), rules=engine.rules)

    @app.get("/events")
    def events(request: Request, kind: str = "", root: str = "", q: str = "",
               before: int | None = None):
        rows = journal.events(kind=kind, root=root, q=q, before=before, limit=PAGE)
        return page(request, "events.html", events=rows, kind=kind, root=root, q=q,
                    older=rows[-1]["id"] if len(rows) == PAGE else None)

    @app.get("/jobs")
    def jobs(request: Request, state_: str = Query("", alias="state"), rule: str = "",
             before: int | None = None):
        rows = journal.jobs(state=state_, rule=rule, before=before, limit=PAGE)
        return page(request, "jobs.html", jobs=rows, state_=state_, rule=rule,
                    rules=engine.rules,
                    older=rows[-1]["id"] if len(rows) == PAGE else None)

    @app.get("/files")
    def files(request: Request, root: str = "", q: str = "", derived: str = ""):
        rows = journal.catalog(root=root, q=q, limit=500,
                               derived={"yes": True, "no": False}.get(derived))
        return page(request, "files.html", files=rows, root=root, q=q, derived=derived)

    @app.get("/file")
    def file(request: Request, root: str, rel: str):
        return page(request, "file.html", root=root, rel=rel, **journal.file(root, rel))

    @app.get("/rules")
    def rules(request: Request):
        counts: dict[str, dict[str, int]] = {}
        for row in journal.query("select rule, state, count(*) as n from jobs "
                                 "group by rule, state"):
            counts.setdefault(row["rule"], {})[row["state"]] = row["n"]
        return page(request, "rules.html", rules=engine.rules, counts=counts)

    @app.get("/raw")
    def raw(root: str, rel: str):
        """A catalogued file's bytes, for image previews. Only files the
        journal knows about, and only inside their root."""
        base = engine.config.roots.get(root)
        if base is None or not journal.query(
                "select 1 from catalog where root = ? and rel = ?", (root, rel)):
            raise HTTPException(404)
        path = (base / rel).resolve()
        if not path.is_relative_to(base.resolve()) or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path)

    @app.post("/run")
    def run(request: Request):
        engine.run_once()
        return RedirectResponse(request.headers.get("referer") or "/", status_code=303)

    @app.post("/jobs/{job_id}/retry")
    def retry(request: Request, job_id: int):
        journal.retry(job_id)
        return RedirectResponse(request.headers.get("referer") or "/jobs", status_code=303)

    @app.get("/api/summary")
    def api_summary():
        return journal.summary()

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics():
        return metrics_text(engine)

    return app
