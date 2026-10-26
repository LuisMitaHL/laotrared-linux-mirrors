#!/usr/bin/env python3
"""prewarm.py - fill the package cache in parallel, before `apt-get install` needs it.

Why this exists
---------------
A cold object is fetched from the upstream mirror over ONE connection. If that
mirror gives you 2 MB/s, a 60 MB package keeps an apt client waiting 30s no
matter how fast your LAN is. nginx alone cannot split that fetch, because a
single request is a single upstream stream.

The cache is configured with `slice 8m` for package-sized objects, which means
each 8 MiB piece of a file is cached under its own key. Several range requests
for DIFFERENT pieces are therefore fetched CONCURRENTLY, and the file ends up in
the cache at N x single-stream speed. That is what this script does: it walks a
list of URLs, asks for each 8 MiB chunk with its own request, and lets nginx fan
those out to the mirrors.

The follow-up `apt-get install` is then a HIT and finishes at LAN speed.

Usage
-----
  # 1. find out exactly which files an install would need (no download happens)
  docker run --rm debian:trixie-slim sh -c \
      'apt-get update -qq && apt-get install --print-uris -y --no-install-recommends vim' \
      > /tmp/uris.txt

  # 2. pull them through the cache, 16 requests at a time
  python3 tools/prewarm.py --base http://127.0.0.1 --from-print-uris /tmp/uris.txt

  # or just name URLs
  python3 tools/prewarm.py --base http://cache.lan \
      http://deb.debian.org/debian/pool/main/f/fonts-cns11643/fonts-cns11643-kai_103.1+20181001-1_all.deb

`--base` rewrites scheme+host and keeps the path, so the URLs can be the real
mirror URLs that apt printed. Without `--base` the URLs are fetched as given.

Notes
-----
* Chunk size must match `slice` in nginx/snippets/locations.inc (8m).
  A mismatch still works (nginx aligns slices itself), but 1:1 is tidier.
* Every chunk request is a normal cache request, so `X-Cache-Status` in the
  cache log shows MISS/HIT per slice and `cache.log` is the audit trail.
* Stdlib only, no root, no dependencies.
"""

import argparse
import http.client
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

DEFAULT_CHUNK = 8 * 1024 * 1024


def parse_size(text: str) -> int:
    """'8m' / '512k' / '1048576' -> bytes."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmg]?)(?:i?b)?\s*", text, re.I)
    if not m:
        raise argparse.ArgumentTypeError(f"not a size: {text!r}")
    value, unit = float(m.group(1)), m.group(2).lower()
    factor = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[unit]
    return int(value * factor)


def join_url(base: str, url: str) -> str:
    """Keep the path+query of `url`, take scheme+host from `base`."""
    if not base:
        return url
    src, dst = urlsplit(url), urlsplit(base)
    return f"{dst.scheme}://{dst.netloc}{src.path}" + (f"?{src.query}" if src.query else "")


def request(url: str, rng: str | None, timeout: float):
    """One GET. Returns (status, bytes, content_range). Connection: close."""
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    headers = {"Accept-Encoding": "identity", "Connection": "close",
               "User-Agent": "prewarm/1.0"}
    if rng:
        headers["Range"] = rng
    try:
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, len(body), resp.getheader("Content-Range") or ""
    finally:
        conn.close()


def total_size(url: str, timeout: float, hint: int | None) -> int | None:
    if hint:
        return hint
    status, _, crange = request(url, "bytes=0-0", timeout)
    if status == 206 and crange and "/" in crange:
        try:
            return int(crange.rsplit("/", 1)[1])
        except ValueError:
            return None
    if status == 200:
        return None  # server ignored Range; nothing to schedule
    return None


def parse_print_uris(path: str):
    """`apt-get install --print-uris` lines look like:
         'http://deb.debian.org/debian/pool/...deb' vim_9.1.1_amd64.deb 4036892 SHA256:...
    Also tolerates `apt-get --print-uris --yes download` output and bare URLs."""
    out = []
    with (sys.stdin if path in ("-", "/dev/stdin") else open(path)) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            m = re.search(r"'(https?://[^']+)'", line)
            if m:
                url = m.group(1)
                size_m = re.search(r"'\s+\S+\s+(\d+)(?:\s|$)", line)
                hint = int(size_m.group(1)) if size_m else None
                out.append((url, hint))
                continue
            m = re.match(r"^(https?://\S+)", line)
            if m:
                out.append((m.group(1), None))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Prefetch package files through the cache, in parallel.")
    ap.add_argument("urls", nargs="*", help="URLs to warm")
    ap.add_argument("--base", default="", help="rewrite scheme+host, e.g. http://cache.lan")
    ap.add_argument("--from-print-uris", metavar="FILE",
                    help="read `apt-get install --print-uris` output ('-' for stdin)")
    ap.add_argument("--chunk", type=parse_size, default=DEFAULT_CHUNK,
                    help="chunk size, must match `slice` in nginx (default 8m)")
    ap.add_argument("--jobs", type=int, default=16, help="parallel requests (default 16)")
    ap.add_argument("--timeout", type=float, default=60.0, help="per-request timeout (default 60s)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, fetch nothing")
    args = ap.parse_args()

    targets = []
    if args.from_print_uris:
        targets += parse_print_uris(args.from_print_uris)
    targets += [(u, None) for u in args.urls]
    if not targets:
        ap.error("give URLs or --from-print-uris")

    lock = threading.Lock()
    done = {"bytes": 0, "chunks": 0, "errors": 0, "files": 0}
    started = time.time()

    def log(msg):
        with lock:
            print(msg, flush=True)

    def chunk_range(offset: int, size: int, total: int) -> str:
        end = min(offset + size, total) - 1
        return f"bytes={offset}-{end}"

    def do_chunk(url: str, rng: str, expect: int):
        try:
            status, got, _ = request(url, rng, args.timeout)
        except Exception as exc:                     # noqa: BLE001 - report, keep going
            with lock:
                done["errors"] += 1
            log(f"  ! {url} {rng}: {exc}")
            return
        if status not in (200, 206):
            with lock:
                done["errors"] += 1
            log(f"  ! {url} {rng}: HTTP {status}")
            return
        with lock:
            done["bytes"] += got
            done["chunks"] += 1

    def warm(url: str, hint: int | None):
        real = join_url(args.base, url)
        total = total_size(real, args.timeout, hint)
        if total is None:
            # Range not honoured (or unknown size): one plain GET warms it.
            if args.dry_run:
                log(f"  {real} (unknown size, single request)")
                return
            try:
                status, got, _ = request(real, None, args.timeout)
            except Exception as exc:                 # noqa: BLE001
                with lock:
                    done["errors"] += 1
                log(f"  ! {real}: {exc}")
                return
            with lock:
                done["bytes"] += got
                done["files"] += 1
            log(f"  {real}  {got/1048576:.1f} MiB in one request (HTTP {status})")
            return

        offsets = list(range(0, total, args.chunk))
        if args.dry_run:
            log(f"  {real}  {total/1048576:.1f} MiB -> {len(offsets)} chunks of "
                f"{args.chunk/1048576:g} MiB")
            return
        with lock:
            done["files"] += 1
        jobs = []
        for off in offsets:
            rng = chunk_range(off, args.chunk, total)
            jobs.append((rng, min(args.chunk, total - off)))
        for rng, _expect in jobs:
            pool.submit(do_chunk, real, rng, _expect)
        log(f"  {real}  {total/1048576:.1f} MiB in {len(jobs)} chunks")

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        for url, hint in targets:
            warm(url, hint)
        pool.shutdown(wait=True)

    wall = time.time() - started
    mb = done["bytes"] / 1048576
    print(f"\n{done['files']} files, {done['chunks']} chunks, "
          f"{mb:.1f} MiB in {wall:.1f}s = {mb/wall if wall else 0:.1f} MiB/s"
          + (f", {done['errors']} errors" if done["errors"] else ""))
    if done["errors"]:
        print("Some chunks failed; nginx will re-fetch them on the next request "
              "(failed requests are not cached).", file=sys.stderr)
    return 1 if done["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
