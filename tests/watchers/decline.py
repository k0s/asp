"""Matches every ``*.txt`` under ``in`` but always declines to write."""

from typing import BinaryIO

from asp import Event, rule


@rule(name="decline", on={"created"}, match="in:**/*.txt", output="out:{dir}/{name}")
def decline(event: Event, out: BinaryIO | None) -> BinaryIO | None:
    assert out is not None
    out.write(b"never published")
    return None
