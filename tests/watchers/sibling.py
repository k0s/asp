"""Unprefixed patterns: the output lands next to its input, in the same root."""

from asp import Event, rule


@rule(name="sibling", on={"created"}, match="**/*.txt", output="{dir}/copies/{name}",
      params={"suffix": "!"})
def sibling(event: Event, out, suffix: str):
    out.write(event.path.read_bytes() + suffix.encode())
    return out
