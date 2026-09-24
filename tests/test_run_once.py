"""One reconciliation pass over an input root, publishing into an output root.

This is the batch shape of asp, and the shape of the container: an input
mount, an output mount, and a config naming the watchers to load.
"""

import json
import random
import string
from pathlib import Path

import pytest

SEED = 20260924


def random_text(rng: random.Random, words: int = 50) -> str:
    """Lowercase ASCII words, so the uppercased form is unambiguous."""
    return " ".join(
        "".join(rng.choices(string.ascii_lowercase, k=rng.randint(1, 10)))
        for _ in range(words)
    )


def files_under(root: Path) -> set[str]:
    """Every regular file below root, as root-relative POSIX paths."""
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


@pytest.mark.xfail(reason="the engine does not exist yet")
def test_run_once_publishes_matching_files(tmp_path: Path) -> None:
    from asp import Engine

    rng = random.Random(SEED)
    in_root = tmp_path / "in"
    out_root = tmp_path / "out"
    (in_root / "sub").mkdir(parents=True)
    out_root.mkdir()

    inputs = {
        "a.txt": random_text(rng),
        "sub/b.txt": random_text(rng),
        "c.md": random_text(rng),        # no rule matches *.md
    }
    for rel, text in inputs.items():
        (in_root / rel).write_text(text)

    config = tmp_path / "asp.json"
    config.write_text(json.dumps({
        "roots": {"in": str(in_root), "out": str(out_root)},
        "watchers": ["watchers.upper"],
    }))

    Engine.from_config(config).run_once()

    # Exactly the matching files, at their input-relative paths: this is
    # also how "*.md produces nothing" and "no temp files left" are checked.
    assert files_under(out_root) == {"a.txt", "sub/b.txt"}
    assert (out_root / "a.txt").read_text() == inputs["a.txt"].upper()
    assert (out_root / "sub/b.txt").read_text() == inputs["sub/b.txt"].upper()

    # Inputs are never touched.
    assert files_under(in_root) == set(inputs)
    for rel, text in inputs.items():
        assert (in_root / rel).read_text() == text
