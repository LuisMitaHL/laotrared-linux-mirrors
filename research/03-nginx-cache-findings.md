# nginx `proxy_cache` as a multi-distro package cache — what was learned

Research target: cache packages **and** indexes for Debian, Ubuntu and Arch on a small disk,
one Docker Compose service, upstream failover on 404, optionally transparent via DNS.

Method: read the nginx directive reference and the apt/pacman sources, then **build the
config in this repo and test it against the real archives** with real `apt` and `pacman`
clients. Every number in §6 comes from a run on 2026-10-26, not from documentation.
Companion files: [01-debian-ubuntu-apt.md](01-debian-ubuntu-apt.md) (apt tool survey),
[02-arch-pacman.md](02-arch-pacman.md) (pacman/pacoloco survey).

---

## 1. The decisive protocol fact: by-hash exists for apt and not for pacman

- Debian `stable`, `stable-security`, Ubuntu `noble`, `noble-updates`, `noble-security` all
  advertise **`Acquire-By-Hash: yes`** in their `dists/*/Release` (checked with `curl`).
  apt 3.x therefore fetches indexes from `/by-hash/SHA256/<hash>` — a content-addressed,
  immutable URL. A cache can never hand out an index newer than the `InRelease` that
  referenced it, so the classic `Hash Sum mismatch` is structurally gone.
- Arch has **no by-hash**: `core.db` is a moving name, and `<repo>.db.sig` returns 404 on
  every mirror (verified on three), because Arch does not sign repo DBs
  (`wiki.archlinux.org/title/DeveloperWiki:Repo_DB_Signing`).

Consequence for the design: indexes must be treated as *mutable* (short TTL, revalidated)
and everything else as *immutable* (cache it forever). That split is the whole config.

## 2. nginx semantics that actually matter

From `ngx_http_proxy_module` (saved copy in `.scratch/nginx_proxy.xml`):

| Directive | Verified behaviour |
|---|---|
| `proxy_cache_valid` | "If only caching time is specified ... then only 200, 301, and 302 responses are cached." So a bare `proxy_cache_valid 200 2s` caches exactly that. |
| `proxy_cache_revalidate on` | Revalidates expired items with `If-Modified-Since` / `If-None-Match`. Required, otherwise every index expiry is a full re-download. |
| `proxy_cache_use_stale` | Accepts the same parameters as `proxy_next_upstream`, plus `updating`. This is the mechanism behind "serve the last good index while upstreams are down". |
| `proxy_next_upstream http_404` | Legal and specifically does **not** mark the peer dead ("the cases of `http_403` and `http_404` are never considered unsuccessful attempts") — exactly right for "mirror has not synced yet". |
| `proxy_cache_lock on` | "only one request at a time will be allowed to populate a new cache element". Turns 50 simultaneous `apt-get update` runs into one upstream fetch. |
| `proxy_cache_path inactive=` | Default is **10 minutes**, not days: "Cached data that are not accessed during the time specified by the inactive parameter get removed from the cache regardless of their freshness." The most common cause of a cache that "doesn't cache". |
| `proxy_cache_path max_size` / `min_free` | The cache manager evicts LRU when `max_size` is hit or free space falls under `min_free`. This is the disk cap. |
| `proxy_force_ranges on` | Needed so `Range` works on *cached* objects; apt and pacman both resume partial downloads. |
| with caching enabled | nginx does **not** forward client `If-Modified-Since`, `If-None-Match`, `Range`, `If-Range` upstream — the cache owns conditional semantics. |

## 3. How the 404 problem is solved per distro

Three independent layers, so it survives even if one is missing:

1. **nginx**, `upstream arch_mirrors` + `proxy_next_upstream ... http_404`: 404 from mirror
   #1 → mirror #2, same request. **Measured** with `geo.mirror.pkgbuild.com` (which 404s
   `/archlinux/*`) as first peer and `mirror.rackspace.com` as second: `200`, 131,635 bytes,
   `X-Upstream-Addr` showing all failed attempts then the working one.
2. **pacman**, `lib/libalpm/dload.c`: a 404 hits the `default:` arm of the curl message
   switch which calls `curl_retry_next_server()` (`dload.c:438-460`), logging
   `retrying from %s`. So even a single `Server =` line fails over at the client level when
   several are listed.
3. **pacoloco** (optional): `downloader.go:97-111` loops `for _, u := range urls` and
   `continue`s on any error; special-cases 404 `.db.sig` (`downloader.go:183-190`).

Also worth knowing: pacman's `CacheServer=` is **never** used for DBs
(`be_sync.c:189`, `pacman.conf.5:263-267`), so a naive "add the cache as CacheServer"
design cannot serve indexes at all.

## 4. Transparent proxy: what is and is not possible

- A DNS redirect alone cannot change the port, so the cache must listen on **80**, and it
  must reconstruct the original hostname from the `Host` header. That is a reverse proxy,
  not a proxy: `proxy_pass http://$host$request_uri` with `resolver` for runtime DNS.
- **Plain HTTP works** for Debian and Ubuntu (their default sources are `http://`), verified
  end-to-end with an unmodified `debian:trixie-slim` container whose `deb.debian.org`
  resolved to the cache box: `apt-get update` and a package download both succeeded with no
  client configuration.
- **HTTPS cannot work without MITM.** The client validates the certificate for
  `deb.debian.org`; a cache box has no such certificate. squid's `ssl_bump` requires
  installing a CA on every client, which defeats "zero client config".
- **Arch is https-only out of the box** — the shipped `mirrorlist` is 425/425 `https://`
  entries — so Arch uses the path-prefix form (`Server = http://cache.lan/archlinux/$repo/os/$arch`)
  or a mirrorlist edited to plain `http`. Both were tested; the one-line form is less
  surprising.
- Anti-open-proxy: a default `server` block that only serves known path prefixes
  (`/debian`, `/debian-security`, `/ubuntu`, `/ubuntu-ports`, `/archlinux`) and 404s
  everything else. Verified: `Host: evil.example.com` + `/anything/at/all` → 404.

## 5. Upstream inventory (verified reachable over plain HTTP)

| Upstream | Path root | Notes |
|---|---|---|
| `deb.debian.org` | `/debian` | by-hash yes, no `Valid-Until` |
| `security.debian.org` | `/debian-security` | by-hash yes, **`Valid-Until` ~7 days** |
| `archive.ubuntu.com` | `/ubuntu` | by-hash yes |
| `security.ubuntu.com` | `/ubuntu` | by-hash yes |
| `ports.ubuntu.com` | `/ubuntu-ports` | arm64/armhf |
| `mirror.rackspace.com`, `mirror.leaseweb.net`, `ftp.halifax.rwth-aachen.de` | `/archlinux` | all 200 for `core/os/x86_64/core.db` (131,635 B), all ignore the `Host` header |
| `geo.mirror.pkgbuild.com` | `/` (not `/archlinux`) | roots elsewhere; unusable in an `/archlinux` group |

Lesson: mirrors differ in path root, so an upstream group must contain only mirrors with the
same root, and "Host-agnostic" is worth testing before adding a mirror.

## 6. Measurements from the test rig

| Test | Result |
|---|---|
| Cold then warm index fetch | `MISS` → `HIT`, 138,612 B `Release` |
| 10 concurrent cold `.deb` fetches | 1 `MISS` + 9 `HIT`, all 270,064 B, correct checksums |
| 3 concurrent cold `apt-get update` | per index: 1 `MISS` + 2 `HIT`; all three clients `rc=0` |
| 404 failover | mirror 404 → next mirror → `200`, 131,635 B |
| Upstream dead, cached index | `200` + `X-Cache-Status: STALE`, identical bytes |
| `Range: bytes=0-1023` on cached `.deb` | `206`, `Content-Range: bytes 0-1023/270064` |
| Arch `pacman -Sy` + `pacman -S unzip` | success, integrity checks pass |
| `*.db.sig` short-circuit | `pacman -Sy` 2.0 s → 0.38 s (three dead upstream attempts removed) |
| Cache growth during all of the above | 10 MB in 8 files |

Disk contrast measured the same day: Arch x86_64 all repos = 118 GB; Debian amd64 = 883 GB;
Ubuntu noble amd64 (+updates/security) = 756 GB.

## 7. Build pitfalls hit while making it work

These cost real debugging time and are easy to hit again:

1. **`proxy_cache` twice in one location is a hard error** (`"proxy_cache" directive is
   duplicate`) — including when one comes from an `include`. Keep exactly one, inside the
   shared snippet.
2. **`resolver 127.0.0.11` only exists on user-defined Docker networks.** A container
   started with plain `docker run` (default bridge) produces
   `recv() failed (111: Connection refused) while resolving` and then
   `deb.debian.org could not be resolved (110: Operation timed out)`. Compose creates a
   user-defined network, so the deliverable is fine; ad-hoc `docker run` tests were not.
3. **`keys_zone=name:size` names the zone, not the path.** `keys_zone=t:10m` + `proxy_cache test;`
   → `"proxy_cache" zone "test" is unknown`.
4. **`$upstream` is a reserved nginx variable** (the peer address); using it as the
   "target host" variable silently misbehaves. `$target` was used instead, plus a
   `map $target $target_host` because an `upstream` block name is not a hostname — mirrors
   with virtual hosting need a real name in the `Host` header.
5. **In the official nginx image `/var/log/nginx/error.log` is a symlink to `/dev/stderr`.**
   `docker exec ... grep /var/log/nginx/error.log` hangs forever. Use `docker logs`.
6. **Cache directories are `0700` owned by uid 101**, so `du`/`rm -rf` as a normal user
   fails; inspect and clear through `docker exec`.
7. `proxy_cache_valid 200 301 302 3650d` alone never caches 404s, which is good — a 404 for
   a not-yet-synced Arch package must not be pinned for a year.

## 8. linuxserver/nginx image: what it changes (verified on `latest`)

Switching the runtime from `nginx:1.27-alpine` to
[lscr.io/linuxserver/nginx](https://github.com/linuxserver/docker-nginx) (pulled
`1.30.6-r0-ls381`, Alpine 3.24, s6-overlay v3) moved no caching behaviour — every test in
§6 was re-run and passed — but the plumbing is different enough to be worth recording:

- **`/etc/nginx/nginx.conf` is a 34-byte stub**: `include /config/nginx/nginx.conf;`. The
  real config lives in `/config` (a volume) and is seeded from `/defaults/nginx/*.sample`
  by `init-nginx` **only when the target file is missing**. That is why mounting a file at
  `/config/nginx/site-confs/default.conf` cleanly replaces the welcome page instead of
  fighting it: the sample copy is skipped, and there is still exactly one
  `listen 80 default_server`.
- **`site-confs/*.conf` are included inside `http {}`**, so `proxy_cache_path`, `map`,
  `upstream` and `log_format` are all legal in a vhost file. The entire cache config fits
  in one file (`nginx/site-confs/default.conf`), unlike a stock nginx where it must
  be split between `http` and `server` blocks.
- **`/config/nginx/resolver.conf` is auto-generated** from the container's
  `/etc/resolv.conf` (`127.0.0.11` in Docker) and included into `http {}`. Adding a
  `resolver` line to the vhost is therefore a duplicate-directive error — the opposite of
  the alpine image, where you *had* to declare it.
- **`worker_processes.conf`** is generated from `nproc` (16 here); harmless.
- **The stock `nginx.conf` already has `proxy_cache_path /tmp/nginx-proxy-cache
  keys_zone=lsio-proxy:10m;`** — a second `proxy_cache_path` is allowed, but reusing the
  zone name `lsio-proxy` would collide, and `/tmp` is a poor place for a persistent cache
  (wiped on restart, memory-backed on some setups). Hence `/cache/pkg` with `keys_zone=pkg`.
- **Runs as `abc` (uid 911, gid 1001)**, and `PUID`/`PGID` remap the user in `init-adduser`.
  With `PUID=1000`/`PGID=1000` the host bind mount must be owned by 1000 — one `chown` at
  install time, and afterwards `du`/`rm` work from the host without `docker exec`.
- **`init-permissions` chmods `/config` recursively on every start**, so `:ro` config
  mounts produce `chmod: ... Read-only file system` lines on each boot. Harmless, but the
  compose file mounts the configs read-write to keep start-up logs clean; add `:ro` back
  for hardening.
- **Version-date check**: every `*.conf` with a `*.conf.sample` sibling is compared on its
  first `## Version YYYY/MM/DD` line and a table is printed when they differ. Putting
  `## Version 2026/06/27` at the top of a customised vhost silences it.
- **Logs** are pre-created at `/config/log/nginx/{access,error}.log` and a global
  `access_log` already exists; the cache log is added as a second file (`cache.log`) via
  the `cache` `log_format`. `curl` is present at `/usr/bin/curl`, so the healthcheck probe
  works as-is.
- The image also runs PHP-FPM, cron and a 443 listener for its welcome page; none of it is
  used here, and the 443 listener disappears with the welcome vhost.

## 9. Design decisions and why

| Decision | Rationale |
|---|---|
| One cache zone, `proxy_cache_key "$host$request_uri"` | The stored path stays byte-identical to upstream, so prefix mode and transparent mode share files, while `$host` keeps `ports.ubuntu.com/ubuntu/...` and `archive.ubuntu.com/ubuntu/...` from colliding. |
| Two `server` blocks, one default | Prefix mode is the catch-all (works for `cache.lan`, an IP, anything) and can only reach five known upstreams; transparent mode is keyed by an explicit `server_name` whitelist. No open proxy, no self-loop, no need to hardcode the cache's own hostname. |
| Index TTL 2 s + revalidate + lock | Smallest practical window for "index newer than the `InRelease`" on by-hash-less Arch, while still collapsing 50 clients into one fetch. |
| `use_stale` on indexes, plus `http_404` on blobs | Indexes must survive an upstream outage; deleted packages should still resolve for stale clients. |
| `inactive=45d`, `max_size=200g`, `min_free=10g` | A pure TTL-by-freshness cache would either grow forever or evict nightly (`inactive` default 10 min). LRU with a hard cap is the only predictable option on a small disk. |
| nginx over apt-cacher-ng | acng cannot be transparent (its own docs: "Transparent proxy mode not implemented yet") and does not fail over on 404 (`dlcon.cc` blacklists only transport/keyfile errors); it also needs per-client config. nginx wins on requirements 2 and 4 and is distro-agnostic. |
| Balance inside nginx rather than pinning one host | One `upstream` group per distro with `random two least_conn` costs nothing at runtime, keeps the 404 hop as a per-request decision, and lets a mirror be parked with `down` instead of an outage. Pinning one host is still available: put the hostname in the path map instead of the group name. |
| nginx over pacoloco (but pacoloco kept as option) | pacoloco is Arch-only and has no apt story; nginx covers all three with one config. pacoloco stays as a documented, ready-to-enable alternative for Arch-heavy setups. |

## 10. Mirror balancing (added after the first version)

The first version pinned Debian to `deb.debian.org` and Ubuntu to `archive.ubuntu.com`
(both are CDN names that balance internally) and only pooled Arch. Question raised: can
the cache itself balance several mirrors, on top of the 404 protection? Yes, with one
`upstream` group per distro and a *two-level* choice: group selection by path prefix,
peer selection by nginx.

### 10.1 Directives and their documented meaning

Source: `ngx_http_upstream_module` and `ngx_http_proxy_module`.

- `random two least_conn` — "two" picks two servers at random, weighted by `weight`, and
  sends the request to the one with fewer active connections. Plain round-robin equalises
  *requests* (bad when one mirror is 20× slower); plain `least_conn` keeps favouring
  whichever peer happens to be idle, so a fast CDN and a slow mirror both get traffic.
- `server ... resolve` — "monitors changes of the IP addresses that correspond to a domain
  name of the server and automatically changes the configuration without the need to
  restart nginx"; requires `zone` in the group and a resolver in `http`. Without it a CDN
  name is resolved once at start and a rotated IP becomes a dead peer.
- `max_fails=2 fail_timeout=60s` — documented as: "if nginx fails to communicate with the
  server `max_fails` times ... the server is considered unavailable for `fail_timeout`".
  Crucially, `proxy_next_upstream` text says "The cases of `http_403` and `http_404` are
  never considered unsuccessful attempts", so a 404 does **not** contribute to
  `max_fails`. That separation is the whole trick: "mirror has not synced this file" must
  hop, "mirror is broken" must park.
- `keepalive 8` — connection cache per group; documented to require
  `proxy_http_version 1.1` and `proxy_set_header Connection ""`, both already set.
- `proxy_next_upstream_tries` — total attempts per request. The first version had 3, which
  is *fewer than the number of peers in a 4-mirror pool*: three lagging mirrors on one
  file would produce 502 even though the fourth had it. Raised to 6.

### 10.2 Verified upstream properties before pooling

- Identical content at one path root: `deb.debian.org`, `ftp.halifax.rwth-aachen.de`,
  `mirror.leaseweb.net`, `mirror.rackspace.com` all returned 138,612 B for
  `/debian/dists/stable/Release`; the same four returned 254,968 B for
  `/ubuntu/dists/noble/Release`.
- Host-agnostic: `Host: archive.ubuntu.com` against halifax/leaseweb/rackspace returned the
  Ubuntu `Release`; `Host: mirror.leaseweb.net` against the raw IP did the same. So a group
  can send one canonical `Host:` to all peers (needed because `proxy_pass
  http://<group>$request_uri` sends the group name as Host otherwise).
- 404 correctness differs per mirror, and this matters for latency (see §10.4):
  `deb.debian.org` 0.05 s, `security.debian.org` 0.55 s, `security.ubuntu.com` 0.42 s,
  halifax 0.38 s, leaseweb 0.33 s, rackspace 0.26 s — but `archive.ubuntu.com` and
  `ports.ubuntu.com` accept the connection and then send nothing (15 s timeout, 0 bytes) on
  paths that do not exist.
- Path roots differ: `geo.mirror.pkgbuild.com` serves `/core/os/...`, not
  `/archlinux/core/os/...`; `ftp.acc.umu.se` has no `/archlinux`, `mirrors.edge.kernel.org`
  has both. A group must hold only mirrors with the *same* root, otherwise every request
  through the group doubles.

### 10.3 Measured behaviour

- **Balancing**: 15 requests over the three pools produced 16 upstream hits on 6 distinct
  addresses — Fastly front for `deb.debian.org` ×7, `mirror.leaseweb.net` ×6 (two A
  records), `ftp.halifax.rwth-aachen.de` ×2, `mirror.rackspace.com` ×1 — all `200`.
- **Balancing + failover**: a throwaway nginx on the same network, one group =
  `geo.mirror.pkgbuild.com` (404s every `/archlinux/*`) + `mirror.rackspace.com`
  (has the file): repeated requests produced
  `X-Upstream-Addr: geo, geo-v6, rackspace` / `X-Upstream-Status: 502, 404, 200` and then
  the correct 131,635 B `core.db`. When the good peer was drawn first: `200` in one
  attempt.
- **Real clients with pools enabled**: Debian prefix `apt-get update && install jq`,
  Debian transparent (stock sources + DNS override), Ubuntu transparent (stock deb822
  sources), `pacman -Sy && pacman -S unzip` — all succeeded.
- **Worst case for an absent file**: `404` after 20.8 s, i.e. one stalled peer hitting the
  20 s index timeout, instead of the previous 60 s / `504`.

### 10.4 Pitfalls specific to balancing

1. **Duplicate directives are fatal, not inherited.** Adding `proxy_read_timeout 20s;` to
   a location that `include`s `proxy.inc` (which already set 60s) gives
   `[emerg] "proxy_read_timeout" directive is duplicate`. Solution used: never set the
   read timeout in the shared include, set it per location (20 s indexes, 30 s blobs).
   Same trap as `proxy_cache`.
2. **`resolve` also resolves AAAA.** On an IPv4-only network, `resolve` in an upstream
   turned into `connect() to [2a04:4e42:400::644]:80 failed (101: Network unreachable)`
   and burned `proxy_next_upstream_tries` budget, producing an occasional `502` for a file
   that plainly exists. Fixed by mounting `/config/nginx/resolver.conf` (overriding the
   image's auto-generated one) with `resolver 127.0.0.11 valid=30s ipv6=off;`.
3. **`$upstream` is a reserved variable** (the group name of the request). The first
   version used it as the Host-header variable and it silently meant nothing useful;
   `$target`/`$target_host` replaced it.
4. **Mirror DNS can return RFC1918.** `mirror.leaseweb.net` resolved to `10.64.21.103`
   from this host (it answered there, fast), and two requests through that address stalled
   for 60 s. A private address in a pool is unreachable on most networks and stalls until
   the read timeout rather than failing fast.
5. **Balancing does not fix a path-root mismatch.** A peer that 404s every request in a
   group is not "failing" by nginx's definition, so it stays in rotation and every request
   pays a hop. Pick peers whose roots match.

## 11. Sources

- nginx `ngx_http_proxy_module` — <https://nginx.org/en/docs/http/ngx_http_proxy_module.html>
  (raw XML: `raw.githubusercontent.com/nginx/nginx.org/master/xml/en/docs/http/ngx_http_proxy_module.xml`)
- apt `Acquire-By-Hash` / `Valid-Until` — live `Release` files of `deb.debian.org`,
  `security.debian.org`, `archive.ubuntu.com`
- pacman `lib/libalpm/dload.c`, `lib/libalpm/be_sync.c`, `doc/pacman.conf.5`
- apt-cacher-ng "Known Bugs and Limitations" — <https://www.unix-ag.uni-kl.de/~bloch/acng/html/bugs.html>
- pacoloco `downloader.go`, `docs/configuration.md` — <https://github.com/anatol/pacoloco>
- Arch repo DB signing — <https://wiki.archlinux.org/title/DeveloperWiki:Repo_DB_Signing>
- Debian mirror size — <https://www.debian.org/mirror/size>
