"""Raises after writing part of its output."""

from asp import Event, rule


@rule(name="boom", on={"created"}, match="in:**/*.txt", output="out:{dir}/{name}")
def boom(event: Event, out):
    out.write(b"partial")
    raise RuntimeError("boom")
