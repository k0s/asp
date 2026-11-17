"""Fill a directory with sample pictures and text for the sandbox.

    python make_samples.py DIRECTORY

Existing files are left alone, so running it again changes nothing. Needs
Pillow, which the sandbox has through snakewatchers.
"""

import colorsys
import json
import math
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw

SEED = 20261116
GALLERIES = {
    "": [("welcome.png", (900, 600))],
    "landscapes": [(f"hills-{n}.jpg", (1600, 1000)) for n in range(1, 7)],
    "portraits": [(f"tower-{n}.jpg", (900, 1500)) for n in range(1, 5)],
    "patterns": [("rings.png", (800, 800)), ("weave.webp", (1000, 1000)),
                 ("dots.gif", (480, 360)), ("tiny.png", (120, 90))],
}
# The galleries ask for thumbnails in their metadata; the top level does not,
# so welcome.png shows what a declined job looks like.
def asks(title: str, width: int) -> str:
    """An index.json asking for thumbnails, indented by two spaces as
    index.json files are."""
    data = {"version": 1, "title": title,
            "k0sngin": {"images": {"thumbnails": True, "width": width}}}
    return json.dumps(data, indent=2) + "\n"


NOTES = {
    "README.txt": "Drop a picture into landscapes/, portraits/ or patterns/.\n"
                  "asp puts a thumbnail beside it, in thumbs/, because each of those\n"
                  "directories asks for thumbnails in its index.json.\n"
                  "A picture dropped here, at the top, gets none: nothing asks.\n",
    "landscapes/notes.txt": "Text files are journaled too, but no rule matches them.\n",
    "landscapes/index.json": asks("Landscapes", 300),
    "portraits/index.json": asks("Portraits", 150),
    "patterns/index.json": asks("Patterns", 200),
}


def colour(hue: float, light: float, sat: float = 0.55) -> tuple[int, int, int]:
    return tuple(round(c * 255) for c in colorsys.hls_to_rgb(hue % 1, light, sat))


def hills(rng: random.Random, size: tuple[int, int]) -> Image.Image:
    """Layered ridgelines under a gradient sky."""
    width, height = size
    hue = rng.random()
    image = Image.new("RGB", size)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        draw.line([(0, y), (width, y)], fill=colour(hue + 0.08 * y / height,
                                                    0.88 - 0.35 * y / height, 0.6))
    draw.ellipse([width * 0.68, height * 0.12, width * 0.68 + height * 0.16,
                  height * 0.28], fill=colour(hue + 0.1, 0.93, 0.9))
    layers = 6
    for layer in range(layers):
        base = height * (0.42 + 0.1 * layer)
        phases = [(rng.uniform(0.002, 0.012), rng.uniform(0, 6.3),
                   rng.uniform(15, 70)) for _ in range(3)]
        ridge = [(x, base + sum(a * math.sin(f * x + p) for f, p, a in phases))
                 for x in range(0, width + 8, 8)]
        draw.polygon([(0, height), *ridge, (width, height)],
                     fill=colour(hue + 0.45 + 0.02 * layer, 0.62 - 0.09 * layer, 0.4))
    return image


def rings(rng: random.Random, size: tuple[int, int]) -> Image.Image:
    """Concentric shapes on a dark ground."""
    width, height = size
    hue = rng.random()
    image = Image.new("RGB", size, colour(hue, 0.12, 0.4))
    draw = ImageDraw.Draw(image)
    step = max(min(size) // 24, 4)
    for i, r in enumerate(range(min(size) // 2, step, -step)):
        box = [width / 2 - r, height / 2 - r, width / 2 + r, height / 2 + r]
        draw.ellipse(box, fill=colour(hue + 0.035 * i, 0.3 + 0.025 * (i % 14), 0.7))
    return image


def make(path: Path, size: tuple[int, int], rng: random.Random) -> None:
    painter = hills if path.suffix == ".jpg" or path.name == "welcome.png" else rings
    image = painter(rng, size)
    if path.suffix == ".gif":
        image = image.quantize(64)
    image.save(path)


def main(target: Path) -> int:
    made = 0
    for gallery, pictures in GALLERIES.items():
        for name, size in pictures:
            path = target / gallery / name
            # Seeded per file, so one picture is the same whatever else exists.
            rng = random.Random(f"{SEED}/{gallery}/{name}")
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                make(path, size, rng)
                made += 1
    for rel, text in NOTES.items():
        path = target / rel
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            made += 1
    print(f"{made} sample files written to {target}")
    return made


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(Path(sys.argv[1]))
