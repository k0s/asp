"""Breaks the contract: returns bytes instead of its out file."""

from asp import Event, rule


@rule(name="wrong", on={"created"}, match="in:**/*.txt", output="out:{dir}/{name}")
def wrong(event: Event, out):
    out.write(b"partial")
    return b"not a file"
