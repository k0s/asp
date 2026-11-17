#!/usr/bin/env bash
# The container, end to end: build it with a watcher, run it over a bind mount
# as the calling user, and check what it writes. Run from the repository root.
# Needs docker; CI runs it.
set -euo pipefail

image=asp-container-test
work=$(mktemp -d)
trap 'docker rm -f asp-test >/dev/null 2>&1 || true; rm -rf "$work"' EXIT

docker build -q -t "$image" \
  --build-arg WATCHERS="snakewatchers@git+https://github.com/k0s/snakewatchers" . >/dev/null

mkdir -p "$work/site/gallery" "$work/state" "$work/config"
python3 - "$work/site" <<'PY'
import sys, zlib, struct
from pathlib import Path
site = Path(sys.argv[1])
def png(path, w, h):
    raw = b"".join(b"\x00" + b"\x30\x90\xa0" * w for _ in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
png(site / "gallery/wide.png", 600, 400)
png(site / "logo.png", 600, 400)
(site / "gallery/index.json").write_text(
    '{"version": 1, "k0sngin": {"images": {"thumbnails": true, "width": 150}}}\n')
PY
cat > "$work/config/asp.json" <<'JSON'
{"roots": {"site": "/data/site"}, "db": "/state/asp.db",
 "watchers": ["snakewatchers.thumbs"]}
JSON

run() {
  docker run --rm --user "$(id -u):$(id -g)" \
    -v "$work/site:/data/site" -v "$work/config/asp.json:/config/asp.json:ro" \
    -v "$work/state:/state" "$image" "$@"
}

echo "== one pass"
run run
test -f "$work/site/gallery/thumbs/thumb_wide.png"
test ! -e "$work/site/thumbs"                       # nobody asked at the top
test "$(stat -c %u "$work/site/gallery/thumbs/thumb_wide.png")" = "$(id -u)"
test "$(stat -c %u "$work/state/asp.db")" = "$(id -u)"
python3 -c "import struct,sys; d=open(sys.argv[1],'rb').read(24); w,h=struct.unpack('>II',d[16:24]); assert (w,h)==(150,100),(w,h)" \
  "$work/site/gallery/thumbs/thumb_wide.png"
run status | grep -q "jobs: {'done': 1, 'skipped': 1}"
echo "ok: thumbnail made, owned by the caller, journal written"

echo "== watching"
docker run -d --name asp-test --user "$(id -u):$(id -g)" \
  -v "$work/site:/data/site" -v "$work/config/asp.json:/config/asp.json:ro" \
  -v "$work/state:/state" -p 127.0.0.1:18765:8765 "$image" \
  serve --host 0.0.0.0 --watch --settle 0.5 >/dev/null
for _ in $(seq 60); do curl -sf http://127.0.0.1:18765/api/summary >/dev/null && break; sleep 0.5; done
cp "$work/site/gallery/wide.png" "$work/site/gallery/second.png"   # written on the host
for _ in $(seq 60); do test -f "$work/site/gallery/thumbs/thumb_second.png" && break; sleep 0.5; done
test -f "$work/site/gallery/thumbs/thumb_second.png"
curl -sf http://127.0.0.1:18765/metrics | grep -q 'asp_jobs{state="done"} 2'
echo "ok: a file written on the host was seen through the mount and thumbnailed"
