"""Raises while FAIL is set; counts its calls."""

from asp import Event, rule

FAIL = True
CALLS: list[str] = []


@rule(name="flaky", on={"created", "modified"}, match="in:**/*.txt",
      output="out:{dir}/{name}")
def flaky(event: Event, out):
    CALLS.append(event.rel.as_posix())
    if FAIL:
        raise RuntimeError(f"not today: {event.path}")
    out.write(event.path.read_bytes())
    return out
