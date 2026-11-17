"""Two stacked rules that publish next to their inputs, like thumbnails."""

from asp import Event, rule


@rule(name="small", on={"created", "modified"},
      match="**/*.txt", output="{dir}/renditions/small/{name}", params={"n": 1})
@rule(name="large", on={"created", "modified"},
      match="**/*.txt", output="{dir}/renditions/large/{name}", params={"n": 3})
def rendition(event: Event, out, n: int):
    out.write(event.path.read_bytes() * n)
    return out
