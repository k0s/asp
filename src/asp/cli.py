"""The asp command line. Everything it prints comes from the same journal
queries the web UI uses."""

import os
from pathlib import Path
from typing import Annotated

import typer

from .config import ConfigError
from .engine import Engine, Pass
from .journal import JOB_STATES, Journal, JournalError

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="asp: a filesystem data pipeline.")

Config = Annotated[Path, typer.Option(
    "--config", "-c", envvar="ASP_CONFIG", help="The asp.json to load.")]
DEFAULT_CONFIG = Path("asp.json")


def load(config: Path) -> Engine:
    try:
        return Engine.from_config(config)
    except (ConfigError, JournalError) as exc:
        typer.echo(f"asp: {exc}", err=True)
        raise typer.Exit(2) from exc


def journal_of(engine: Engine) -> Journal:
    if engine.journal is None:
        typer.echo(f"asp: {engine.config.path} names no journal; add a \"db\" path",
                   err=True)
        raise typer.Exit(2)
    return engine.journal


def describe(done: Pass) -> str:
    jobs = ", ".join(f"{n} {state}" for state, n in sorted(done.jobs.items())) or "no jobs"
    return f"{done.files} files, {done.events} events, {jobs}"


@app.command()
def run(config: Config = DEFAULT_CONFIG) -> None:
    """Run one reconciliation pass and say what it did."""
    done = load(config).run_once()
    typer.echo(describe(done))
    if done.jobs.get("failed") or done.jobs.get("parked"):
        raise typer.Exit(1)


@app.command()
def status(config: Config = DEFAULT_CONFIG) -> None:
    """Roots, rules, and what the journal holds."""
    engine = load(config)
    typer.echo(f"config  {engine.config.path}")
    for name, path in engine.config.roots.items():
        typer.echo(f"root    {name} = {path}")
    typer.echo(f"rules   {len(engine.rules)} from {len(engine.config.watchers)} watchers")
    if engine.journal is None:
        typer.echo("journal none")
        return
    summary = engine.journal.summary()
    typer.echo(f"journal {engine.journal.path}")
    for root in summary["roots"]:
        typer.echo(f"  {root['root']}: {root['files']} files "
                   f"({root['derived']} derived), {root['bytes']} bytes")
    typer.echo(f"  events: {summary['events_total']} total; last 24h "
               f"{summary['events_24h'] or 'none'}")
    typer.echo(f"  jobs: {summary['jobs'] or 'none'}")
    last = summary["last_pass"]
    typer.echo(f"  last pass: {last['finished'] if last else 'never'}")


@app.command()
def events(config: Config = DEFAULT_CONFIG,
           limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
           kind: str | None = None, root: str | None = None,
           path: Annotated[str | None, typer.Option(help="Substring of the path.")] = None,
           ) -> None:
    """The most recent events, newest last."""
    rows = journal_of(load(config)).events(kind=kind, root=root, q=path, limit=limit)
    for e in reversed(rows):
        typer.echo(f"{e['id']:>7} {e['ts']} {e['kind']:<8} {e['root']}:{e['rel']}"
                   f"  [{e['source']}]")


@app.command()
def jobs(config: Config = DEFAULT_CONFIG,
         limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
         state: Annotated[str | None, typer.Option(help=" | ".join(JOB_STATES))] = None,
         rule: str | None = None) -> None:
    """The most recent jobs, newest last."""
    rows = journal_of(load(config)).jobs(state=state, rule=rule, limit=limit)
    for j in reversed(rows):
        target = f" -> {j['output_root']}:{j['output_rel']}" if j["output_rel"] else ""
        error = f"  ({j['error']})" if j["error"] else ""
        typer.echo(f"{j['id']:>7} {j['state']:<7} {j['rule']}  "
                   f"{j['root']}:{j['rel']}{target}{error}")


@app.command()
def retry(job: int, config: Config = DEFAULT_CONFIG) -> None:
    """Requeue a failed or parked job; the next pass runs it."""
    if not journal_of(load(config)).retry(job):
        typer.echo(f"asp: job {job} is not failed or parked", err=True)
        raise typer.Exit(1)
    typer.echo(f"job {job} queued")


@app.command()
def rules(config: Config = DEFAULT_CONFIG) -> None:
    """The loaded rules, in the order they run."""
    for b in load(config).rules:
        typer.echo(f"{b.rule.name}: on {','.join(sorted(b.rule.on))}  "
                   f"{b.rule.match} -> {b.rule.output or '(no output)'}"
                   f"{'  ' + str(b.rule.params) if b.rule.params else ''}")


@app.command()
def serve(config: Config = DEFAULT_CONFIG,
          host: str = "127.0.0.1", port: int = 8765,
          interval: Annotated[float | None, typer.Option(
              help="Also run a pass every this many seconds.")] = None) -> None:
    """Serve the dashboard, and optionally keep reconciling."""
    engine = load(config)
    journal_of(engine)
    try:
        import uvicorn

        from .web import create_app
    except ImportError as exc:
        typer.echo(f"asp: the dashboard needs the 'web' extra: {exc}", err=True)
        raise typer.Exit(2) from exc
    uvicorn.run(create_app(engine, interval=interval), host=host, port=port,
                log_level=os.environ.get("ASP_LOG_LEVEL", "warning"))


def main() -> None:
    app()
