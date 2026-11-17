"""Its output pattern renders to its own input path."""

from asp import Event, rule


@rule(name="inplace", on={"created"}, match="**/*.txt", output="{dir}/{name}")
def inplace(event: Event, out):
    out.write(b"clobbered")
    return out
