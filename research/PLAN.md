# Plan: a small multi-distro package cache (Debian, Ubuntu, Arch)

**Recommendation: one nginx `proxy_cache` reverse proxy in Docker Compose, on port 80,
serving both as a path-prefixed mirror and as a DNS-level transparent cache.**
No client config changes are strictly required, it covers all three distros with one
config, and it fails over between Arch mirrors on 404. Everything below was built and
measured in this repository (`docker-compose.yml`, `nginx/`, `tools/`), not just read
off a feature list.

---

## 1. Why not a real mirror

A full mirror is out of the question on a "not really big" disk. Sizes are the whole
archive, from official/computed sources ([Debian mirror size page][debsize], Arch mirror
DBs, Ubuntu `Packages.xz` `Size:` fields):

| What | Packages | Compressed size |
|---|---|---|
| Arch `core` + `extra` + `multilib`, x86_64 | 15,540 | **118 GB** |
| Debian `stable`, amd64 only | — | **883 GB** (all arches 5,410 GB) |
| Ubuntu 24.04 `noble`, amd64, incl. -updates/-security | 119,719 | **756 GB** |

Ubuntu alone decomposes into noble 158 GB + noble-updates 311 GB + noble-security 287 GB.
`aptly` / `debmirror` / `apt-mirror` / `pulp` / `nexus` are all in this "materialise the
archive" class: they are disqualified on disk, not on features.

A cache stores only what your machines actually request. Measured in this repo while
testing: after two Arch DB syncs, one apt update and three package downloads, the cache
held **10 MB in 8 files**. That is the whole difference.

[debsize]: https://www.debian.org/mirror/size

## 2. Requirement → design

| # | Requirement | How this does it |
|---|---|---|
| 1 | Easy to deploy, Docker Compose | One service, `docker compose up -d`, 3 mounted files + a cache directory. No database, no daemon to babysit. |
| 2 | Multiple upstreams, retry on 404 | Three `upstream` groups (`debian_mirrors`, `ubuntu_mirrors`, `arch_mirrors`) balanced with `random two least_conn`, plus `proxy_next_upstream ... http_404` for per-request failover. **Measured**: 20 requests spread over 6 peers; a mirror answering 404 → next mirror used, correct 131,635-byte `core.db` returned. The peer list is generated from measurement, not hand-picked — §7. Details in §6. |
| 3 | Debian + Ubuntu + Arch from one thing | Three path prefixes (`/debian`, `/debian-security`, `/ubuntu`, `/ubuntu-ports`, `/archlinux`) into one cache zone. Ubuntu/Debian and Arch both tested end-to-end with the real clients. |
| 4 | Transparent proxy via DNS | Second `server` block: if the request arrives with `Host: deb.debian.org`, it is proxied to the real host with the path untouched. No client config. Works for http-only clients (Debian/Ubuntu default). Verified with real `apt-get update`. |

Non-goals, stated up front: **HTTPS is not proxied or MITM'd.** nginx cannot serve
`https://deb.debian.org` from a cache without a fake CA on every client; that is the
one thing this plan does not solve, and it is why Arch is configured with an
`http://` mirror line instead of relying on redirection (see §6).

## 3. Architecture

```
                       clients: apt / pacman / curl
                                 |
        +------------------------+-------------------------+
        |                                                  |
  (A) path-prefixed                                (B) transparent
  http://cache.lan/debian/...                      DNS: deb.debian.org -> cache.lan
  http://cache.lan/ubuntu/...                      request arrives with Host: deb.debian.org
  http://cache.lan/archlinux/...                   (no client config at all)
        |                                                  |
        +-----------------> nginx :80 --------------------- +
                                 |
          map $uri -> target (hostname = pinned, group name = balanced)
                    map $target -> Host: header the mirror sees
                                 |
              +------------------+------------------+
              |                  |                  |
        debian_mirrors    ubuntu_mirrors      arch_mirrors
        deb.debian.org w3 archive.ubuntu w2  rackspace  w1
        halifax        w1 halifax        w1  leaseweb   w1
        leaseweb       w1 leaseweb       w1  halifax    w1
        rackspace      w1 rackspace      w1
              |                  |                  |
              +------------------+------------------+
              each group: random two least_conn, max_fails=2 fail_timeout=60s,
              keepalive 8; 404 = "try the next mirror", never "that mirror is dead"
                                 |
                    proxy_cache_path /cache/pkg
                    max_size=200g  min_free=10g  inactive=45d
                    one file per URL path (byte-identical to upstream)
```

Two client modes share **one** cache: the stored path is exactly the upstream path, and
the key is `$host$request_uri`, so `/debian/...` fetched via prefix mode is the same file
served to a transparent client asking for `deb.debian.org/debian/...`.

## 4. How "no breakages" is actually achieved

Requirement 1 in the prompt is really *"don't corrupt my package managers"*. Three
distinct mechanisms, in decreasing order of importance:

**a) by-hash (Debian/Ubuntu only).** Both archives advertise `Acquire-By-Hash: yes`
(verified live on `deb.debian.org`, `security.debian.org`, `archive.ubuntu.com`), so apt
fetches indexes at `/by-hash/SHA256/<hash>` — a *content-addressed* URL. An index can
never be served "one version newer than the `InRelease` that pointed at it", which is the
classic `Hash Sum mismatch`. Those objects land in the immutable location and are cached
for a year. This is the single biggest reason a plain HTTP cache is safe for apt in 2026
and was not safe in 2015.

**b) short TTL + revalidation for the mutable names.** Only names that are reused with
changing bodies (`InRelease`, `Release`, `Packages*`, `Sources*`, `Contents*`,
`Translation*`, and Arch's `*.db`/`*.files`/`*.abs`) get `proxy_cache_valid 200 2s` plus
`proxy_cache_revalidate on` and `proxy_cache_lock on`. So `apt-get update` is never more
than 2 s behind upstream, and 50 clients updating at once cause **one** upstream fetch.
**Measured**: 3 concurrent cold `apt-get update` runs produced exactly 1 `MISS` + 2 `HIT`
per index, and 10 concurrent cold package fetches produced 1 `MISS` + 9 `HIT`.

**c) stale-on-failure, scoped.** Indexes use
`proxy_cache_use_stale error timeout updating http_500 http_502 http_503 http_504`:
if every upstream is down, clients get the last good index instead of an error.
**Measured**: with the upstream replaced by a closed port, the request returned
`200`, `X-Cache-Status: STALE`, and byte-identical content. Package paths additionally
include `http_404` in that list, because a package that the mirror has since deleted
should still resolve for a client with an older index.

One ceiling to know about: `security.debian.org` publishes `Valid-Until` ~7 days ahead
(observed: `Mon, 02 Nov 2026`), so a stale Debian security index stops being usable after
a week of total upstream outage. Nothing can fix that without lying to apt.

Arch has no by-hash (`/by-hash/` 404s on real mirrors), so its DBs rely on (b) and on
pacman's own atomic `.db` download. A 2 s TTL is what keeps that window tiny.

## 5. Deploy

```sh
mkdir -p cache
# the image runs nginx workers as `abc`; PUID/PGID in compose remap it, so the
# host cache dir just has to belong to that uid/gid
sudo chown 1000:1000 cache        # or set PUID/PGID to your own ids
docker compose up -d
curl -sI http://127.0.0.1/debian/dists/stable/Release   # 200 + X-Cache-Status
```

Runs on **[lscr.io/linuxserver/nginx](https://github.com/linuxserver/docker-nginx)**
(Alpine 3.24, nginx 1.30.x, s6-overlay v3). That image was chosen deliberately over
`nginx:alpine`: it is explicitly meant to be configured from a persistent `/config` tree,
it runs as the unprivileged `abc` user instead of root-then-drop, and it already ships a
`proxy_cache_path` and a resolver — i.e. less to hand-wire. The trade-off is its layout
(see below), not its behaviour.

Files:

- `docker-compose.yml` — the service, port 80, PUID/PGID, the two config mounts,
  the cache mount, healthcheck.
- `nginx/site-confs/default.conf` — cache zone, path→upstream map, Arch upstream
  group, the two `server` blocks. Mounted at
  `/config/nginx/site-confs/default.conf`, replacing the image's welcome-page vhost.
- `nginx/snippets/locations.inc` — the two cache policies (mutable index vs
  immutable blob). Mounted at `/config/nginx/snippets/`.
- `nginx/snippets/proxy.inc` — shared proxy/caching behaviour.
- `nginx/resolver.conf` — overrides the resolver the image auto-generates
  (`ipv6=off`, so a v4-only host never burns attempts on AAAA records).
- `pacoloco/pacoloco.yaml` — optional Arch-only alternative (commented out in
  compose), if you prefer pacoloco's prefetch/purge model over nginx's.

### How this image differs from upstream nginx (and what it forces)

| Image detail | Consequence for this config |
|---|---|
| `/etc/nginx/nginx.conf` is a one-line stub: `include /config/nginx/nginx.conf;` | The real config is generated under `/config/nginx/` on first start and is yours to edit. |
| `site-confs/*.conf` are included **inside** `http {}` | `proxy_cache_path`, `map`, `upstream`, `log_format` are all legal in the vhost file — the whole cache config stays in one readable file. |
| The default vhost is created only **if `site-confs/default.conf` is missing** | Mounting your own `default.conf` is enough to remove the welcome page; no `rm`, no duplicate `default_server`. |
| `/config/nginx/resolver.conf` is auto-generated (`resolver 127.0.0.11 valid=30s;` in Docker) and included into `http {}` | **Do not** add your own `resolver` line to the vhost — a second one in the same context is a duplicate-directive error. Mount `nginx/resolver.conf` over that file instead; that is also where `ipv6=off` lives. |
| The stock `nginx.conf` already declares `proxy_cache_path /tmp/nginx-proxy-cache keys_zone=lsio-proxy:10m` | A second `proxy_cache_path` is fine, but the zone must be a different name (`pkg` here) and the path must not be `/tmp`, which is wiped on restart. |
| Image runs as `abc` (uid 911, gid 1001) and `PUID`/`PGID` remap it | The cache bind mount must be writable by that uid; hence the `chown` above or `PUID`/`PGID` matching your user. |
| `init-permissions` chmods `/config` recursively on every start | Mounting the configs `:ro` works but prints `chmod: ... Read-only file system` on each boot; the compose file mounts them read-write to keep the logs clean. Add `:ro` back if you prefer hardening over silence. |
| Logs go to `/config/log/nginx/{access,error}.log`, and there is already a global `access_log` | The cache log is a second file in the same directory (`cache.log`), declared with the `cache` `log_format` from the vhost. |

Tunables, all in `site-confs/default.conf`:

| Knob | Default here | Meaning |
|---|---|---|
| `max_size` | `200g` | hard cap; LRU eviction by the cache manager |
| `min_free` | `10g` | never fill the filesystem below this |
| `inactive` | `45d` | drop entries nobody asked for in 45 days (**nginx's default is 10 minutes** — the most common way this setup "randomly" evicts everything) |
| `keys_zone` | `80m` | ~800k URLs of key metadata |
| index TTL | `2s` | freshness window for `InRelease`/`Packages`/`*.db` |

If the box is not Docker, run plain nginx instead: point `resolver` at the host's
resolver and include the vhost from `/etc/nginx/conf.d/`. The image's `resolver.conf`
trick only exists because Docker's embedded DNS is at `127.0.0.11`.

## 6. Mirror management: pinning, balancing, failing over

Three different questions get conflated; keep them apart.

| Question | Mechanism |
|---|---|
| Which mirror serves *this* request? | `upstream` group + `random two least_conn` — load balancing |
| The chosen mirror does not have *this file*? | `proxy_next_upstream ... http_404` — per-request failover |
| A mirror is broken or stalls, rather than missing a file? | `max_fails`/`fail_timeout` — passive health check (404s excluded) |
| Which mirrors are in the group at all? | `tools/scan-mirrors.py` regenerates them from measurement — see §7 |

Short answer to "are we pinned to one master mirror?": **no.** Each distro prefix has a
pool of six real mirrors, all serving the same path root (`/debian`, `/ubuntu`,
`/archlinux`), spread with a weighted *random two, least-connections* policy.
`debian-security` and `ubuntu-ports` stay pinned because each has exactly one origin.

### 6.1 Where the choice is made

`map $uri $path_target` in `default.conf` maps a path prefix to either a **hostname**
(pinned) or an **upstream group name** (balanced):

```nginx
map $uri $path_target {
    default              none;                   # unknown path -> 404
    ~^/debian-security/  security.debian.org;    # pinned: single origin
    ~^/debian/           debian_mirrors;         # balanced
    ~^/ubuntu-ports/     ports.ubuntu.com;       # pinned: single origin
    ~^/ubuntu/           ubuntu_mirrors;         # balanced
    ~^/archlinux/        arch_mirrors;           # balanced
}
```

Switching a prefix between pinned and balanced is a one-word edit. The groups themselves
are not in this file: they live in the generated `nginx/mirrors.conf`, together
with a `map $target $target_host` that supplies the `Host:` header. That map is generated
next to the pools on purpose — see §7.3 for why the two must not drift apart. The cache key
stays `$host$request_uri`, so pooling does not change which files are shared between
clients.

### 6.2 The policy, directive by directive

| Directive | Value | Why |
|---|---|---|
| `random two least_conn` | — | Picks two peers at random (weighted) and sends to the less busy one. Round-robin gives every mirror equal *requests*, which is wrong when one is 3× faster; plain `least_conn` keeps favouring whichever peer is idle and can starve the good ones. |
| `weight=` | 1–5, from measurement | Set by the scanner from measured throughput, relative inside the pool. A fast CDN edge gets 5, a slow mirror 1, so a slow peer gets ~1/18 of the requests rather than 1/6. |
| `max_fails=2 fail_timeout=60s` | — | Two failures park a peer for a minute. **A 404 is not a failure** — nginx never marks a peer down for 403/404 — so "this mirror has not synced that package yet" cannot blacklist a healthy mirror. |
| `resolve` | — | CDN names rotate IPs; without it nginx resolves once at startup and keeps a dead address. Requires a `zone` and an http-level `resolver` (see `nginx/resolver.conf`). |
| `keepalive 8` | — | Reuses upstream connections instead of a TCP handshake per package. |
| `proxy_next_upstream` | `error timeout invalid_header http_403 http_404 http_429 http_502 http_503 http_504` | 404 = "not synced yet", 403 = "peer refused this Host", 429 = "rate limiting". Note what is **missing and cannot be added**: nginx has no 3xx and no 409 case, so a peer that redirects to HTTPS or answers 409 cannot be hopped over at request time (see §7.3). |
| `proxy_next_upstream_tries 6` | — | One try per peer in the pool: a mirror lagging on one file can never exhaust the budget and return 502 while another peer has the file. |
| `proxy_read_timeout` | 20s indexes / 30s blobs | Per-*read* inactivity, not total transfer time. It bounds the damage of a mirror that accepts the connection and then goes silent; `timeout` in `proxy_next_upstream` then moves on. A mirror that is really streaming never waits that long between two reads. |

### 6.3 What was measured

- **Spread**: 20 consecutive cold requests for one Debian index touched 7 distinct
  upstream addresses on 6 peers (Fastly's front for `deb.debian.org` → 9 of them,
  `mirror.insacom.cl` 5, `debian.repo.cure.edu.uy` 2, `plug-mirror.rcac.purdue.edu` 2,
  `mirror.hnd.cl` 1). All 20 answered `200`.
- **Absent file, worst case**: a path that exists on no mirror returned `404` in **0.97 s**
  after trying all six peers (`$upstream_addr` in the log lists all six, in order). The
  earlier all-mirror version of this pool took 20.8 s for the same request because
  `archive.ubuntu.com` accepts the connection and then stalls instead of answering 404.
- **Failover and balancing compose**: a throwaway nginx with one group = a mirror that
  404s every `/archlinux/*` request + a good mirror produced
  `X-Upstream-Status: 502, 404, 200` and served the correct 131,635-byte `core.db`.
- **Real clients**: `apt-get update` + `install jq` through Debian prefix mode, Debian
  transparent mode and Ubuntu transparent mode (stock deb822 sources, DNS override) — all
  succeeded; `pacman -Sy` + `pacman -S unzip` succeeded.
- **Concurrency**: 10 simultaneous cold downloads of one package → 1 `MISS` + 9 `HIT`,
  so the lock prevents ten upstream fetches of the same file.

### 6.4 Adding, parking, removing, pinning a mirror

Everything below is a *manual* edit to `nginx/mirrors.conf`, which is generated —
a re-run of the scanner overwrites it. For anything permanent, change the scanner inputs
(`nginx/mirrors.conf` header comment, or `DISTROS` in `tools/scan-mirrors.py`).

- **Add** — append inside the pool:
  `server host.example.org:80 weight=1 resolve max_fails=2 fail_timeout=60s;`
  Check three things first: (1) it serves the same path root (`/debian`, `/ubuntu`,
  `/archlinux`); (2) it answers plain **HTTP** (DNS-based transparency cannot help
  HTTPS); (3) it answers the pool's `Host:` header — a peer that redirects to HTTPS or
  answers 403/409 for that header cannot be used.
- **Park temporarily** — add `down` to that server line: it stays in the file,
  documented, and is never selected.
- **Pin to one mirror** — replace the group name in `map $uri $path_target` with the
  hostname (e.g. `deb.debian.org`). That is exactly the pre-balancing behaviour, useful
  when you want byte-identical upstream snapshots.
- **Prefer one mirror** — raise its `weight` (or lower the others). Weights are relative,
  so `weight=10` on a LAN mirror makes it serve most of the traffic.

### 6.5 Caveats found the hard way

- **Not every mirror answers 404 correctly.** `archive.ubuntu.com` accepts the connection
  and then stalls on paths that do not exist (measured: 15 s, zero bytes), while
  `deb.debian.org` answers `404` in 0.05 s. It is no longer in the Debian pool, which is
  why the absent-file case dropped from 20.8 s to 0.97 s.
- **A mirror's DNS may return a private address.** `mirror.leaseweb.net` resolved to
  `10.64.21.103` from one network. Fine there, a liability anywhere else — and the reason
  both nginx and the scanner resolve through public resolvers (see §7.3).
- **One group must have one path root.** `geo.mirror.pkgbuild.com` serves `/core/...`,
  not `/archlinux/core/...`, so it cannot share the Arch group.
- **404 failover is per request, not remembered.** Nothing records "mirror X lacked
  `foo-1.2`"; the next request may pick X again. That is intentional (X will have synced
  by then); `max_fails` is what keeps a *broken* X out of rotation.
- **A rewrite of `mirrors.conf` needs a reload.** The file is mounted by content: nginx
  keeps serving the old one until `nginx -s reload`. `docker compose up -d` is not enough
  if the file changed after the container started — a regenerated file plus no reload
  looks exactly like "the new pool is broken".

### 6.6 Verifying a change

```sh
docker exec pkgcache nginx -t                              # "syntax is ok"
curl -sI http://127.0.0.1/debian/dists/stable/Release | grep -i x-cache-status
docker exec pkgcache tail -f /config/log/nginx/cache.log
```

The 6th column of `cache.log` is `$upstream_addr`: one address means "served by that
peer", a comma-separated list means "that many attempts, in this order" — the cheapest way
to see balancing and failover both working. To see the spread rather than one request:

```sh
for i in $(seq 20); do curl -so /dev/null "http://127.0.0.1/debian/dists/stable/InRelease?b=$i"; done
docker exec pkgcache grep "InRelease?b=" /config/log/nginx/cache.log \
  | sed -E 's/.*" ([0-9.]+:[0-9]+)".*/\1/' | sort | uniq -c | sort -rn
```

## 7. Finding the mirrors: the generated pools

`nginx/mirrors.conf` is written by `tools/scan-mirrors.py`, which discovers
candidates, checks freshness, measures throughput from *this* network, and picks the
fastest six per distro. Nothing in it is hand-maintained, and nothing is guessed by
geography.

### 7.1 Where candidates come from

| distro | source | size |
|---|---|---|
| Arch | `https://archlinux.org/mirrors/status/json/` | 1190 published, 346 usable plain-HTTP, 250 with the right path root |
| Debian | `https://mirror-master.debian.org/status/Mirrors.masterlist` (official) | 358 sites, 344 with `Archive-http:` |
| Ubuntu | `https://launchpad.net/ubuntu/+archivemirrors` | 474 `/ubuntu` candidates |

There is no JSON status feed for Debian (every `/status/*.json` path 404s) and
`mirrors.ubuntu.com/mirrors.txt` returns a single line, so those two endpoints are not
options. Arch tier information comes from `/status/tier/1/json/` and `/tier/2/json/`.

### 7.2 Freshness is a hard gate, speed is only a ranking

A mirror that is 20 minutes behind serves **404 for the package you just asked for** —
which is exactly requirement #2 from the brief. So freshness is checked before anything is
timed:

| distro | gate | why |
|---|---|---|
| Arch | `active`, `completion_pct = 1.0`, `delay ≤ 3600 s`, path root exactly `/archlinux`, `core.db` returns gzip | Arch publishes real per-mirror sync state |
| Debian / Ubuntu, frozen suite (`stable`, `noble`) | `sha256(Release)` must equal the origin's | the `Date:` of a frozen suite only moves at point releases — `noble/Release` still says 2024-04-25, so date age is meaningless |
| Debian / Ubuntu, rolling suite (`-security`, `-updates`, `testing`) | `Release` `Date:` within 6 h of the origin | these are rewritten often, so `Date:` is meaningful and a hash match would falsely drop a mirror that is minutes behind |

A mirror is also dropped if it serves the archive at a different path root, if it answers
the pool's `Host:` header with a redirect or 403/409, or if `Valid-Until` on the index has
already expired.

### 7.3 One Host header per pool (the constraint that shapes everything)

nginx cannot vary `Host` per selected peer: one `proxy_set_header` covers the whole group.
Peers split into three classes, all measured:

```
Cloudflare-fronted peer, sent somebody else's Host   -> 409 (17 bytes) or 530
plain Apache peer, sent a Cloudflare name            -> 200 131635 bytes
peer that redirects plain HTTP to HTTPS              -> 301, always
```

Consequences, both already applied:

- The scanner probes and speed-tests **with the pool's own `Host` header**, exactly as
  nginx will, and drops peers that redirect or refuse it. With `Host:
  ftp.halifax.rwth-aachen.de`, 58 of 131 Arch candidates survive (118 when each peer was
  tested under its own name) and 220 of 334 Debian mirrors survive (306 before). The
  numbers are smaller and the pool actually works.
- `proxy_next_upstream` has no 3xx or 409 case (`[emerg] invalid value "http_301"`), so a
  redirecting peer cannot be hopped over at request time. The gate has to happen when the
  pool is built.
- `map $target $target_host` is generated into `mirrors.conf` next to the pools, so the
  header the probe tested and the header nginx sends cannot diverge.

The default `Host` is a plain, always-HTTP mirror (`ftp.halifax.rwth-aachen.de`). Because
Cloudflare-fronted mirrors are the fastest from some networks, `--host-header
arch=us.mirrors.cicku.me` lets a Cloudflare-proxied name be the pool's `Host`; Cloudflare
peers then come back into the pool and Arch speed recovered from 4.6 MB/s to 7.9 MB/s in
testing. The cost is a dependency on that name still being proxied — if it stops, those
peers answer 409 and cannot be retried, while plain peers are unaffected.

Both nginx and the scanner resolve mirror names through **public resolvers**, never the
LAN resolver: on a network whose DNS redirects mirror names to this cache (that is the
point of transparent mode), the LAN resolver would make nginx resolve its own upstream to
itself, and it already answers with a local appliance for names like `mirror.fcix.net`
(`10.64.21.103` while the real address is `23.152.160.16`). `nginx/resolver.conf`
and the scanner's `--resolvers` must name the same servers.

### 7.4 How the ranking works

1. **Proximity**: one TCP connect to port 80, never ICMP. Used only to choose who gets the
   expensive test.
2. **Throughput**: a `Range` GET of 12 MiB (two rounds, best kept) from a large file
   (`Contents-amd64.gz`, `Packages.xz`, or the Arch ISO). This is also the only honest
   proxy for the sender's congestion control: a BBR-paced sender holds a high rate at
   high RTT, CUBIC collapses, and both show up as sustained MB/s. Nothing in an HTTP
   response advertises the algorithm, so "does this mirror use BBR" cannot be answered
   directly and is not guessed.
3. **No RTT bias**: the download test runs on half the nearest peers **plus an even sample
   of the rest**, because anycast CDNs sit far away in RTT terms and still win — measured
   here, `deb.debian.org` at 22 ms beat `mirror.hnd.cl` at 51 ms.
4. **Tier as a tie-breaker**: `score = speed × 1.10` for a tier-1/origin source (Arch
   tier 1, Debian `Type: GeoDNS`/`Origin`, the canonical Ubuntu archive). Freshness is
   already a gate, so tier only keeps the pool on the boxes other mirrors sync *from*; a
   genuinely faster leaf still wins.
5. **Weight**: `clamp(round(5 × score / best), 1, 5)`, relative inside the pool.

Measured on 2026-10-26 from this network (La Paz, Bolivia), best peer per distro:
`deb.debian.org` 11.3 MB/s at 22 ms, `mirror.eliv.digital` 7.6 MB/s at 3 ms,
`mirrors.ic.unicamp.br` 7.5 MB/s at 76 ms. The 3 ms peers are anycast edges of a local
peering point (a TCP connect to `104.18.8.165` takes 2.8 ms while `23.152.160.16` takes
200 ms), which is why an anycast-fronted mirror can beat a geographically closer one.

### 7.5 Running it

```sh
# all three distros, ~2-3 minutes, ~940 candidates
python3 tools/scan-mirrors.py

# apply without recreating the container
docker exec pkgcache nginx -t && docker exec pkgcache nginx -s reload
```

Daily for freshness, weekly would be enough for speed. Suggested timer:

```sh
# /etc/cron.daily/mirror-scan   (chmod +x)
cd /opt/pkgcache && python3 tools/scan-mirrors.py \
  && docker exec pkgcache nginx -t \
  && docker exec pkgcache nginx -s reload
```

`.scratch/mirror-report.json` keeps every measured candidate, not just the winners, so
"why is this mirror in the pool" is answerable afterwards.

### 7.6 Knobs

| flag | default | meaning |
|---|---|---|
| `--distros` | `debian,ubuntu,arch` | must cover every prefix `map $uri $path_target` references, or that prefix 502s |
| `--top` | 6 | peers kept per pool |
| `--speed-candidates` | 16 | how many mirrors get the download test |
| `--max-delay` | 3600 | Arch: drop mirrors not synced in this many seconds |
| `--max-stale` | 21600 | apt rolling suites: drop mirrors this far behind the origin |
| `--bytes` / `--rounds` | 16 MiB / 2 | download-test budget |
| `--tier-bonus` | 10 | percent score bonus for tier-1/origin sources |
| `--host-header` | — | `distro=name` override for the pool's `Host` (see §7.3) |
| `--resolvers` | 1.1.1.1, 9.9.9.9, 149.112.112.112, 8.8.8.8 | must match `nginx/resolver.conf` |
| `--dry-run` | — | print the file, write nothing |

## 8. Cold fetches: the one slow case left, and what fixes it

Everything else in this plan is about *repeat* traffic. This section is about the first
client that asks for a package nobody has: that request is a single stream from one mirror,
and on a slow mirror it is the one thing that still feels slow.

`apt` is not the problem: a cold `apt-get install` measured here started six package
downloads within 1.1 s of each other. The serialisation is **per file** — one 600 MB
package is one stream. Two mechanisms address that.

### 8.1 Slicing: fetch big packages in 8 MiB units, not whole files

Without `slice`, a client `Range` request for an uncached object makes nginx download the
**entire** file through one connection (measured: 21.0 s for 8 parallel 4 MiB ranges on a
32 MiB object behind a 2 MB/s peer). With `slice 8m`, each slice has its own cache key and
its own upstream request: the same 8 ranges finished in **1.97 s** (10.6×) and the
following plain GET was a `HIT`.

Practical consequences:

- a resumed or interrupted download no longer refetches megabytes it already had;
- ten machines installing the same package fetch each 8 MiB slice **once**;
- a large file can be pulled in parallel chunks (`tools/prewarm.py`, §8.4).

The cost is one upstream request per 8 MiB instead of one per file (a 600 MB package is 75
requests) and an 8 MiB cache-granularity tail. 8 MiB is deliberately not smaller: these
requests land on volunteer mirrors.

### 8.2 The trap this uncovered: mirrors disagree on `ETag`

The first version truncated every large cold download with
`curl: (18) end of response with N bytes missing`, at a different byte offset each run, and
**only when the pool had more than one peer**. The log line that explains it:

```
[error] etag mismatch in slice response while reading response header from upstream
```

The slice module records the first slice's `ETag` and aborts the response if a later slice
carries a different one. Mirrors generate their own ETags for the same bytes — Fastly's
`"3924390-57958e6e556a2"` vs `mirror.hnd.cl`'s `"3924390-5bd6b4b5-acdd4;;;"` — while all
of them return a perfectly valid `206` with `Content-Range: bytes 0-8388607/59917200`.
Balancing therefore truncated downloads at the first slice served by another mirror.

Fix: `proxy_hide_header ETag;` in `nginx/snippets/proxy.inc`. Nothing is lost —
client conditional headers are ignored anyway, and `Last-Modified` (the file's mtime) is
identical across mirrors, so revalidation keeps working. After the change, 5/5 cold
downloads of a 59,917,200-byte package were complete.

### 8.3 What was measured (59,917,200-byte package, cold)

| Upstream | Method | Wall time | Rate |
|---|---|---|---|
| Fastly, direct | single stream | 4.29 s | 14.0 MB/s |
| Fastly, direct | 8 parallel ranges | 4.43 s | no gain — link-bound |
| pool via cache | single stream | 6.67 s | 9.0 MB/s |
| Fastly pinned, via cache | single stream | 5.42 s | 11.0 MB/s |
| Fastly pinned, via cache | prewarm, 8 chunks | 5.00 s | 11.3 MiB/s |
| `ftp.halifax.rwth-aachen.de`, via cache | single stream | **40.17 s** | 1.49 MB/s |
| same slow peer, via cache | prewarm, 8 chunks | **14.1 s** (2.9×) | 4.1 MiB/s |

So: **no win against a fast mirror** (one stream already saturates this link), **~3× against
a slow one**, because the limit is the peer's per-connection rate. A single cold stream
through the pool is ~23 % slower than pinned to the best peer, because with `slice`
consecutive slices are dispatched by `random two least_conn` and a slower peer can take
over mid-file — the price of balancing, and the reason the weights come from measured
throughput.

### 8.4 `tools/prewarm.py`: pay the cost before a client asks

```sh
# one file
python3 tools/prewarm.py --base http://cache.lan \
  http://cache.lan/debian/pool/main/f/fonts-cns11643/fonts-cns11643-kai_103.1+20181001-1_all.deb

# a whole install, from apt's own URL list
apt-get install --print-uris -y nginx postgresql | sudo tee /tmp/uris
python3 tools/prewarm.py --base http://cache.lan --from-print-uris /tmp/uris --jobs 16
```

Stdlib only, no root: it asks for `bytes=0-0` to learn the real size, then fetches
`--chunk` (8 MiB) ranges concurrently. `--dry-run` prints the plan.

Cron turns it into "keep the deploy set hot":

```sh
# /etc/cron.daily/prewarm   (chmod +x)
apt-get install --print-uris -y $(cat /etc/pkgcache/deploy-packages) > /tmp/uris
python3 /opt/pkgcache/tools/prewarm.py --base http://127.0.0.1 --from-print-uris /tmp/uris
```

### 8.5 Caveats

- **The URL must match exactly**, query string included: the cache key is
  `$host$request_uri$slice_range`.
- `slice` applies only to the package regex in `locations.inc`; indexes keep their 2 s +
  revalidate policy, so §4 is untouched.
- Buffering had to move out of `proxy.inc` into each location: a location cannot override
  a directive set by an include in the same context (`"proxy_read_timeout" directive is
  duplicate`), and the slice location needs temp files
  (`proxy_max_temp_file_size 1024m`) while the other two stream without them.
- After a container restart the log may show `could not be resolved (110: Operation timed
  out)` for a dozen pool names at once. It is a burst of nginx resolver lookups, not the
  network (40/40 `dig` queries from the same docker network succeed); it self-heals in
  seconds and `proxy_next_upstream` uses another peer meanwhile.

## 9. Client configuration

**Debian / Ubuntu (prefix mode), `/etc/apt/sources.list.d/cache.sources`:**

```
Types: deb
URIs: http://cache.lan/debian
Suites: trixie trixie-updates
Components: main contrib non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb
URIs: http://cache.lan/debian-security
Suites: trixie-security
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
```

Ubuntu: `URIs: http://cache.lan/ubuntu`, `Suites: noble noble-updates noble-security`
(and `http://cache.lan/ubuntu-ports` on arm64/armhf).

**Arch (prefix mode), `/etc/pacman.d/mirrorlist` — one line, nothing else:**

```
Server = http://cache.lan/archlinux/$repo/os/$arch
```

Keeping just this one line is deliberate: pacoloco's docs warn about the loop you create
by listing a cache alongside real mirrors, and the same applies here.

**Transparent mode (requirement 4):** point DNS at the cache box for the mirror
hostnames, e.g. in dnsmasq/unbound/pihole:

```
address=/deb.debian.org/10.0.0.10
address=/security.debian.org/10.0.0.10
address=/archive.ubuntu.com/10.0.0.10
address=/security.ubuntu.com/10.0.0.10
address=/ports.ubuntu.com/10.0.0.10
```

`site-confs/default.conf` already whitelists exactly these names in the transparent
`server` block;
anything else falls through to the default server, which only serves the five known path
prefixes and answers 404 to everything else. **Verified**: `Host: evil.example.com` +
`/anything/at/all` → 404 (no open proxy), while the same host + `/debian/...` is served
from the known mirror. Real `apt-get update` inside a container with `deb.debian.org`
resolved to the cache box worked with **zero** changes to the client.

Two honest caveats on transparency:

1. **Only plain HTTP works.** All apt traffic in Debian/Ubuntu defaults to `http://`, so
   this covers requirement 4. The moment a client uses `https://` (or `Acquire::https::`
   pinning), the TLS handshake fails because our box has no certificate for
   `deb.debian.org`. Do not add `https` entries to the transparent `server_name` list.
2. **Arch needs prefix mode anyway.** Arch's shipped mirrorlist is 425/425 `https://`
   URLs (checked against the live file), so DNS redirection alone would break pacman.
   Either use the one-line `http://cache.lan/archlinux/...` above, or edit the mirrorlist
   to a plain-`http` mirror and let DNS redirect it — the latter works, but keeping the
   mirrorlist https and using the path prefix is less surprising.

## 10. What was actually validated

Every row below is a real run against the shipped files, not an expectation. They
were first proven on `nginx:1.27-alpine`, then re-run end-to-end on
`lscr.io/linuxserver/nginx:latest` (nginx 1.30.6) after the switch, including real `apt`
prefix + transparent installs and a real `pacman -Sy`; the image changes plumbing, not
caching behaviour:

| Test | Result |
|---|---|
| Cold + warm fetch, Debian `Release` | `200`, then `X-Cache-Status: HIT` |
| Transparent `apt-get update` + `install -d`, Debian 13 | success, no client config |
| Prefix `apt-get update` + `install -d`, Debian 13 | success |
| Arch `pacman -Sy` + `pacman -S unzip` through the cache | success, integrity checks pass |
| 404 failover (mirror #1 returns 404) | next mirror used, 131,635 B `core.db` served |
| Balanced pools over 20 cold requests | 7 distinct upstream addresses on 6 peers, all `200` |
| Failover + balancing together | `X-Upstream-Status: 502, 404, 200` → `200`, correct bytes |
| Absent file, no peer has it | `404` in 0.97 s after trying all 6 peers (was 20.8 s while `archive.ubuntu.com` was in the pool: it stalls instead of answering 404) |
| Upstream dead, stale index available | `200`, `X-Cache-Status: STALE`, identical bytes |
| 10 concurrent cold package fetches | 1 `MISS`, 9 `HIT` (cache lock) |
| 3 concurrent cold `apt-get update` | 1 `MISS` + 2 `HIT` per index |
| Ubuntu transparent mode, stock deb822 sources | `apt-get update` + `install jq-1.7` success |
| Generated pools wired in (`include /config/nginx/mirrors.conf`) | Debian / Ubuntu / Arch all `200`: 138,612 / 254,968 / 131,635 B |
| Regenerated `mirrors.conf` without reload | prefix `502` (`upstream group could not be resolved`) until `nginx -s reload` — see §6.5 |
| `Range: bytes=0-1023` on cached `.deb` | `206`, correct `Content-Range` (resume works) |
| Unknown host + unknown path | `404` (not an open proxy) |
| Arch `*.db.sig` (never signed upstream) | short-circuited to `404`; pacman `-Sy` went from ~2 s to 0.38 s |
| `slice` on a cold 59,917,200 B package | full body 5/5 runs, slices served by different peers |
| same, before `proxy_hide_header ETag` | truncated at 8 MiB with `etag mismatch in slice response` (§8.2) |
| 8 parallel 4 MiB ranges, cold 32 MiB object | `21.0 s` (nginx fetched the whole file) → `1.97 s` with `slice 8m` |
| prewarm against a 1.49 MB/s peer | `40.17 s` single stream → `14.1 s` with 8 chunks (§8.3) |

## 11. Operations

- **Cache status is visible**: every response carries `X-Cache-Status`
  (`MISS/HIT/STALE/REVALIDATED/EXPIRED/UPDATING`) and `X-Cache-Kind` (`index` vs
  `immutable`); the access log line is
  `$remote_addr "$host$request_uri" $status $request_time $upstream_cache_status "$upstream_addr" $body_bytes_sent`.
  A single `grep STALE` tells you whether upstreams are healthy.
- **Inspecting the cache** is plain host tooling: it is the bind mount `cache`
  (plus `pkg/`), owned by the PUID you configured. `du -sh cache` works.
- **Clearing** is `rm -rf cache/pkg/*` while the container runs, or
  `docker compose down && rm -rf cache` for a full reset.
- **Image logs** live at `/config/log/nginx/{access,error,cache}.log` (persisted only if
  you also mount `/config`; the compose file does not, so use `docker logs`).
- **Big packages** are stored in 8 MiB slices (§8), so `du -sh cache` can exceed the
  sum of the file sizes you recognise; that is expected, not deduplication failure.
- **Prewarming** is `tools/prewarm.py` (§8.4); run it from cron for the packages you deploy
  on a schedule, and remember the URL must match the client's exactly, query string
  included.
- **Disk accounting** is just the bind mount `cache`; move it to the big disk with
  one line in `docker-compose.yml`.
- **Upgrades**: the config is declarative; `nginx -s reload` (or `docker compose restart`)
  applies changes without dropping cached files.
- **Monitoring**: the cache log plus `du` is enough at this scale; add node_exporter /
  nginx-prometheus-exporter later if you want graphs.

**Sizing guidance.** Indexes are re-fetched often but are small and shared: two Arch DBs
are ~9 MB, one Debian `Packages.xz` ~10 MB. Package data grows with *unique* packages
actually installed across the fleet. With `max_size=200g` + `inactive=45d` the cache
self-limits; start with `max_size=50g` if the disk is tight — eviction is LRU and costs
only a re-download. With ~10 machines on a rolling distro expect a few GB per month of
new unique packages, so 50 GB covers a long time.

## 12. Alternatives considered

| Option | Verdict |
|---|---|
| **nginx `proxy_cache`** (chosen) | Only tool that is simultaneously transparent-capable, multi-distro (apt **and** pacman), and a cache rather than a mirror. 404 failover across mirrors is a documented `proxy_next_upstream http_404`. |
| **pacoloco** | The best *Arch* cache (real multi-URL failover, DB revalidation, prefetch, Prometheus, SQLite). No apt support at all. Included as an optional profile. |
| **apt-cacher-ng** | Best apt protocol knowledge (backend failover, index snapshotting, `FreshIndexMaxAge` to share one `apt-get update`, Arch remap shipped in `conf/acng.conf.in`). **Cannot be transparent** — its own "Known Bugs and Limitations" says "Transparent proxy mode not implemented yet" — and it does *not* fail over on a 404 (`dlcon.cc` blacklists only transport/keyfile failures). Needs one `Acquire::http::Proxy` line per client. Good fallback if you would rather configure clients than run nginx. |
| **approx** | `$offline true` is the cleanest "survive upstream outage" behaviour, Debian-maintained, trivial config. No Docker story, no transparency, no Arch. |
| **squid-deb-proxy** | Debian-only, Ubuntu-focused, image effectively abandoned (2016-era), always revalidates every index. |
| **Squid intercept mode** | Genuinely transparent, but one more protocol-aware daemon to run, and apt's `Cache-Control: max-age=0` semantics plus by-hash handling are more subtle than nginx's explicit two-policy split. |
| **HAProxy / Flexo** | Flexo fails over between mirrors but never caches DBs; HAProxy has no real HTTP cache. |
| **aptly / debmirror / apt-mirror / pulp / nexus** | Mirror tools: 100s of GB. Dismissed on §1. |

The combination "Debian + Ubuntu + Arch + transparent + tiny disk" has no single
purpose-built product; nginx is the only general tool whose primitives map cleanly onto it.

## 13. Risks and what to watch

1. **`inactive=45d` is doing heavy lifting.** Copy-paste configs that omit it inherit
   nginx's 10-minute default and evict everything; that is the usual "my cache doesn't
   work" report.
2. **Transparent DNS redirects also catch unrelated traffic** if a name is whitelisted
   that you did not intend. Keep the `server_name` list minimal; unknown hosts already 404.
3. **Clients that hardcode `https://`** bypass the cache silently (they just work,
   slowly). Check the access log for the absence of expected suites if caching "does
   nothing".
4. **If a machine's clock is far off**, apt's `Valid-Until` check fails on cached
   indexes. Run NTP.
5. **Old releases** move to `archive.debian.org` / `old-releases.ubuntu.com`; those
   upstreams need their own map entry (and `Check-Valid-Until false` client-side).
6. **No TLS interception** means no caching for `https` clients — by design, not a bug to
   fix later without a fleet-wide CA.
7. **Mirror quality is now part of your dependency chain.** A mirror that stalls instead
   of answering 404 (see §6.5) costs a 20–30 s timeout per request that lands on it; a
   mirror that returns a private IP from DNS hangs until the read timeout. `max_fails`
   parks a broken peer for 60 s, but a *slow* peer is not "failing". Prune the pools to
   mirrors you have actually timed from your own network.

## 14. Suggested next steps

1. `mkdir -p cache && docker compose up -d`, then point one Debian VM at
   `http://cache.lan/debian` and confirm `X-Cache-Status: MISS` → `HIT`.
2. Add the DNS `address=` entries once apt is proven, and remove the per-client config.
3. Add a `cron` (or pacoloco prefetch) warm-up for the suites you care about if you want
   first-run latency gone; not needed for correctness.
4. Re-check the mirror pools (§6.4) against your own network: drop `archive.ubuntu.com`
   if its stall-on-404 bothers you, add your ISP/regional mirror if it is faster than the
   CDN, and confirm every host still returns `404` for a bogus path.
4. If Arch traffic dominates, flip the optional pacoloco service on and keep nginx for
   apt — both can share `cache` (pacoloco in `cache/pacoloco`).
