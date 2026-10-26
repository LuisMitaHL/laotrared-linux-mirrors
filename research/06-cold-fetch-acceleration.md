# Cold fetches: why one big uncached package is slow, and what actually helps

Everything here was measured on this box (nginx 1.30.6 in the linuxserver image, docker
29.9.2, a 59,917,200-byte Debian package and a 32 MiB synthetic file on a rate-limited
peer). Numbers are wall-clock from `curl -w`, not estimates.

## 1. The question

`apt` is famously single-threaded per file. If a package is **not** in the cache yet, the
first client pays the full mirror latency for it, and on a slow mirror that is minutes.
Can the cache make that faster?

## 2. What apt actually does

Measured: cold `apt-get update` + `install vim` through the cache, empty cache:
**10.9 s wall**, and the access log (`$msec` now logged, see §6) shows six package
downloads starting within 1.1 s of each other, each taking 0.1–0.7 s.

So apt *does* download concurrently — the serialisation people complain about is **per
file**. Installing 40 packages is fine; installing one 600 MB package is one stream.

## 3. What nginx does with a client `Range` (before `slice`)

A `Range` request for an object that is **not** in the cache makes nginx fetch the whole
object from upstream and cache it, then serve the requested range out of it. Measured with
a 32 MiB file behind a 2 MB/s limit:

| Test | Result |
|---|---|
| 8 parallel 4 MiB ranges, cold, plain `proxy_cache` location | **21.0 s** (one full 32 MiB fetch), following plain GET = `HIT` |
| same 8 ranges with `slice 8m` | **1.97 s** (10.6×), following plain GET = `HIT` |

So without `slice`, a resume or a parallel downloader *hurts*: every range request pulls
the entire file through one connection.

## 4. The `slice` directive, and the bug it exposed

The slice location (in `nginx/snippets/locations.inc`):

```nginx
location ~ ^/.*/(pool|os|iso|images)/.*\.(deb|udeb|zst|iso|tar\.(xz|gz|bz2|zst))$ {
    if ($target = none) { return 404; }
    slice 8m;
    proxy_set_header Range $slice_range;
    proxy_cache_key "$host$request_uri$slice_range";
    proxy_cache_valid 200 206 3650d;
    proxy_max_temp_file_size 1024m;
    ...
}
```

Each 8 MiB slice is its own cache key and its own upstream request, so a file is fetched
(and shared between clients) in 8 MiB units instead of whole-file units.

**This broke large cold downloads**, and the failure looked like a network problem:

- `curl` reported `end of response with N bytes missing`, with `Content-Length` correct;
- the truncation point moved between runs (2,100,504 B, 8,388,608 B, 25,165,824 B,
  33,554,432 B) and vanished when the pool was pinned to a single mirror;
- the error log had the real answer:

```
[error] 473#473: *78 etag mismatch in slice response while reading response header
from upstream, client: 172.18.0.1, ... upstream: "http://138.186.10.35:80/debian/..."
```

The slice module remembers the `ETag` of the first slice and aborts the whole response if
a later slice comes back with a different one. Mirrors generate their own ETags for the
same file:

| Mirror | `ETag` |
|---|---|
| `deb.debian.org` (Fastly) | `"3924390-57958e6e556a2"` |
| `mirror.hnd.cl` | `"3924390-5bd6b4b5-acdd4;;;"` |
| `debian.pop-sc.rnp.br` | `"3924390-57958e6e556a2"` |

Every peer answered correctly (`206`, `Content-Range: bytes 0-8388607/59917200`,
`Content-Length: 8388608`) — the responses were fine, they just disagreed on the ETag.
A balanced pool therefore truncates large downloads at the first slice served by a
different mirror; a pinned single mirror never does.

**Fix:** `proxy_hide_header ETag;` in `nginx/snippets/proxy.inc`. Hiding it stops
the comparison and costs nothing here — we ignore client conditional headers anyway
(`proxy_ignore_headers ...`), and `Last-Modified` is identical across mirrors (it is the
file's mtime), so revalidation still works. After the fix, 5/5 cold downloads of the same
59,917,200-byte file returned the full body.

## 5. Measurements: when parallelism helps

Same 59,917,200-byte file, cold (unique cache key per run):

| Upstream | Method | Wall time | Rate |
|---|---|---|---|
| `fastly.mirror.pkgbuild.com`/`deb.debian.org`, direct | single stream | 4.29 s | 14.0 MB/s |
| same, direct | 8 parallel ranges | 4.43 s | — (link-bound, no gain) |
| pool, through cache | single stream | 6.67 s | 9.0 MB/s |
| Fastly pinned, through cache | single stream | 5.42 s | 11.0 MB/s |
| Fastly pinned, through cache | prewarm, 8 chunks | 5.00 s | 11.3 MiB/s |
| **`ftp.halifax.rwth-aachen.de` (slow), through cache** | **single stream** | **40.17 s** | **1.49 MB/s** |
| **same slow peer** | **prewarm, 8 chunks** | **14.1 s** | **4.1 MiB/s** |

Two conclusions:

1. **Against a fast mirror there is nothing to win.** One stream to Fastly already
   saturates this link (14 MB/s, and 8 parallel connections do not beat it).
2. **Against a slow mirror, parallel chunks win ~3×** (40.2 s → 14.1 s), because the
   bottleneck is the peer's per-connection rate, not your link.

A single cold stream through the *pool* (6.67 s) is ~23 % slower than the same stream
pinned to the fastest peer (5.42 s): with `slice`, consecutive slices are dispatched by
`random two least_conn`, so a slower peer can take over mid-file. That is the price of
balancing; it is small, and the weights in `nginx/mirrors.conf` are computed from
measured throughput for exactly this reason.

## 6. `tools/prewarm.py`

Purpose: pay the cold cost *before* a client asks, in parallel chunks.

```sh
# one file
python3 tools/prewarm.py --base http://cache.lan \
  http://cache.lan/debian/pool/main/f/fonts-cns11643/fonts-cns11643-kai_103.1+20181001-1_all.deb

# a whole install, from apt's own URL list
apt-get install --print-uris -y nginx postgresql | sudo tee /tmp/uris
python3 tools/prewarm.py --base http://cache.lan --from-print-uris /tmp/uris --jobs 16
```

It is stdlib-only (no `apt`, no root), asks for `bytes=0-0` to learn the real size, then
requests `--chunk` (default 8 MiB) ranges concurrently with `--jobs` workers. `--dry-run`
prints the plan. The `?<query>` on a URL matters: the cache key includes the URI, so a
prewarm URL must be *exactly* the one clients will use.

Cron makes it a "keep the deploy set hot" job rather than a manual step:

```sh
# /etc/cron.daily/prewarm   (chmod +x)
apt-get install --print-uris -y $(cat /etc/pkgcache/deploy-packages) > /tmp/uris
python3 /opt/pkgcache/tools/prewarm.py --base http://127.0.0.1 --from-print-uris /tmp/uris
```

## 7. Cost side

- One upstream request per 8 MiB slice instead of one per file: a 600 MB package is 75
  requests. Against a CDN this is free; against a small mirror it is visible in their
  logs, so do not shrink `slice` further.
- Cache granularity is 8 MiB: a half-downloaded 600 MB file occupies up to 8 MiB more
  than its fetched bytes (`inactive=45d` still applies).
- `slice` only applies to the package regex; indexes keep their own 2 s + revalidate
  policy, so the "no breakages" story in [PLAN.md](PLAN.md) §4 is unchanged.
- 8 MiB slices are cached with `proxy_cache_valid 200 206 3650d`, i.e. treated as
  immutable, which is correct because the path contains the package version.

## 8. Pitfalls met while building this

1. **A location cannot override directives set in an included file** — same context.
   `[emerg] "proxy_read_timeout" directive is duplicate`. Buffering
   (`proxy_buffers`, `proxy_busy_buffers_size`, `proxy_max_temp_file_size`) therefore
   moved out of `proxy.inc` into each location: the slice location needs temp files, the
   index/immutable locations stream without them.
2. **`nginx:alpine` has no `proxy_cache` zone**, so a test vhost using `proxy_cache pkg`
   fails with `zone "pkg" is unknown` unless the `proxy_cache_path` is declared; and
   nginx creates only the last path component, so `/tmp/cache/noslice` fails with
   `mkdir() ... No such file or directory`. Declare cache paths at `/tmp/<one>`.
3. **ETag mismatch** — §4. Symptom is a truncated body plus that log line; the tell is
   "only with a pool, never with a single peer".
4. **A `Range` on a cold object without `slice`** pulls the whole file (§3).
5. **Startup DNS timeouts**: right after a restart, the error log shows
   `[error] <mirror> could not be resolved (110: Operation timed out)` for a dozen pool
   names at once. From the same docker network, 40/40 `dig` queries and 15 simultaneous
   `dig` queries to `1.1.1.1` succeeded, so it is not the network or rate limiting — it is
   a burst of lookups from nginx's resolver. It is transient (self-heals within seconds,
   `proxy_next_upstream` moves to the next peer) and it is why `resolver.conf` lists four
   public resolvers rather than one.
6. **`$msec` shifted the log columns**: an overlap analysis run before adding it compared
   the wrong fields. Add the timestamp first, then measure.

## 9. Sources

- `slice` / `proxy_cache_key` / `proxy_set_header Range`: <https://nginx.org/en/docs/http/ngx_http_slice_module.html>
- `proxy_hide_header`, `proxy_ignore_headers`: <https://nginx.org/en/docs/http/ngx_http_proxy_module.html>
- RFC 9110 §8.8.2 (`Last-Modified` is the file's mtime): <https://www.rfc-editor.org/rfc/rfc9110.html#section-8.8.2>
- RFC 9110 §6.6.1 (`Date` is when the response was generated):
  <https://www.rfc-editor.org/rfc/rfc9110.html#section-6.6.1>
- `apt-get --print-uris`: `man apt-get` on Debian 13 (`--print-uris`), and
  <https://manpages.debian.org/trixie/apt/apt-get.8.en.html>
