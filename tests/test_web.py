"""The dashboard and the CLI, over a small journaled tree."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from asp import Engine
from asp.cli import app as cli
from asp.web import activity_chart, create_app, nice_ceiling
from watchers import flaky


@pytest.fixture
def config(tmp_path: Path) -> Path:
    (tmp_path / "in/sub").mkdir(parents=True)
    (tmp_path / "out").mkdir()
    (tmp_path / "in/a.txt").write_text("a")
    (tmp_path / "in/sub/b.txt").write_text("b")
    path = tmp_path / "asp.json"
    path.write_text(json.dumps({"roots": {"in": "in", "out": "out"}, "db": "asp.db",
                                "watchers": ["watchers.upper"]}))
    return path


@pytest.fixture
def client(config: Path) -> TestClient:
    engine = Engine.from_config(config, {})
    engine.run_once()
    engine.run_once()
    return TestClient(create_app(engine))


@pytest.mark.parametrize("url", [
    "/", "/events", "/events?kind=created&root=in&q=sub", "/jobs", "/jobs?state=done",
    "/files", "/files?derived=yes", "/rules", "/file?root=in&rel=a.txt",
    "/file?root=out&rel=a.txt", "/file?root=in&rel=never-existed.txt"])
def test_pages_render(client: TestClient, url: str) -> None:
    response = client.get(url)
    assert response.status_code == 200
    assert "<nav>" in response.text


def test_pages_show_the_journal(client: TestClient) -> None:
    assert "sub/b.txt" in client.get("/events?q=sub").text
    assert "a.txt" not in client.get("/events?q=sub").text.replace("sub/b.txt", "")
    assert "handler:upper" in client.get("/events?root=out").text
    assert "derived by" in client.get("/file?root=out&rel=a.txt").text


def test_raw_serves_only_catalogued_files(client: TestClient, config: Path) -> None:
    assert client.get("/raw?root=out&rel=a.txt").text == "A"
    assert client.get("/raw?root=in&rel=../asp.json").status_code == 404
    assert client.get("/raw?root=nope&rel=a.txt").status_code == 404
    (config.parent / "in/new.txt").write_text("not yet scanned")
    assert client.get("/raw?root=in&rel=new.txt").status_code == 404


def test_run_button_runs_a_pass(client: TestClient, config: Path) -> None:
    (config.parent / "in/new.txt").write_text("new")
    assert client.post("/run", follow_redirects=False).status_code == 303
    assert (config.parent / "out/new.txt").read_text() == "NEW"


def test_metrics(client: TestClient) -> None:
    text = client.get("/metrics").text
    assert 'asp_catalog_files{root="in"} 2' in text
    assert 'asp_jobs{state="done"} 2' in text
    assert client.get("/api/summary").json()["jobs"] == {"done": 2}


def test_retry_from_the_page(config: Path) -> None:
    config.write_text(config.read_text().replace("watchers.upper", "watchers.flaky"))
    flaky.FAIL = True
    engine = Engine.from_config(config, {})
    for _ in range(3):
        engine.run_once()
    client = TestClient(create_app(engine))
    assert "Retry" in client.get("/jobs?state=parked").text
    flaky.FAIL = False
    for job in engine.journal.jobs(state="parked"):
        client.post(f"/jobs/{job['id']}/retry", follow_redirects=False)
    client.post("/run", follow_redirects=False)
    assert engine.journal.summary()["jobs"].get("done") == 2


def test_chart_geometry() -> None:
    assert [nice_ceiling(n) for n in (0, 4, 5, 9, 17, 80, 81, 950)] == [
        4, 4, 8, 12, 20, 80, 120, 1200]
    buckets = [{"hour": f"2026-01-01T{h:02d}:00+00:00", "created": h, "modified": 1,
                "deleted": 0} for h in range(24)]
    chart = activity_chart(buckets)
    assert chart["total"] == sum(range(24)) + 24
    assert len(chart["columns"]) == 24
    tallest = chart["columns"][-1]["segments"]
    assert [s["kind"] for s in tallest] == ["created", "modified"]
    assert all(s["y"] >= chart["top"] for s in tallest)


def test_cli(config: Path) -> None:
    runner = CliRunner()
    args = ["--config", str(config)]
    first = runner.invoke(cli, ["run", *args])
    assert first.exit_code == 0 and "2 files, 2 events, 2 done" in first.output
    assert "upper: on created,modified" in runner.invoke(cli, ["rules", *args]).output
    assert "in:sub/b.txt" in runner.invoke(cli, ["events", *args]).output
    assert "-> out:a.txt" in runner.invoke(cli, ["jobs", *args]).output
    assert "last pass" in runner.invoke(cli, ["status", *args]).output
    assert runner.invoke(cli, ["retry", "1", *args]).exit_code == 1
    assert runner.invoke(cli, ["run", "--config", "/nowhere/asp.json"]).exit_code == 2
