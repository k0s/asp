"""A watcher with one rule: uppercase every ``*.txt`` under ``in`` into ``out``."""

from typing import BinaryIO

from asp import Event, rule


@rule(name="upper", on={"created", "modified"},
      match="in:**/*.txt", output="out:{dir}/{name}")
def upper(event: Event, out: BinaryIO | None) -> BinaryIO | None:
    assert out is not None          # the rule declares an output
    out.write(event.path.read_text().upper().encode())
    return out
