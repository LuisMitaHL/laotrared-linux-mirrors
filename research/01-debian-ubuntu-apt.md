# Debian / Ubuntu (apt): package + index caching tools

Researched 2026-10-26 against primary sources (upstream docs, man pages, source code, packaged configs).
Versions inspected: `apt-cacher-ng` 3.7.5-1.3 (Debian sid/forky; 3.7.5-1 in trixie), `approx` 5.13-3 (trixie), `squid-deb-proxy` 0.9.2 (Ubuntu universe), `squid` 7.7-1 / 6.13, `apt` 3.0.3.

Target workload for the requester: several small servers, limited disk → **on-demand caching, not mirroring**; must also cache *index* files so `apt update` survives an unreachable or rotating upstream; easy docker-compose deploy; tolerant of flaky/404 upstreams; ideally multi-distro; transparent (DNS-level) interception preferred.

## Comparison table

| tool | caches debs | caches indexes | upstream failover / stale | transparent-capable | docker compose | disk profile | maintenance |
|---|---|---|---|---|---|---|---|
| **apt-cacher-ng** | yes, on demand | yes (volatile, revalidated) | mirror rotation + blacklisting; stale only in `Offlinemode` | **no** ("Transparent proxy mode not implemented yet") | community images only | unbounded; age-based pruning of unreferenced files | active-ish: 3.7.5-1.3 in sid, 3.7.5-1 in trixie |
| **approx** | yes, on demand | yes (re-checked every `$interval` min) | `$offline true` serves stale when upstream is down — best stale story | no (URL-prefix rewrite only) | community only, abandoned | unbounded, no documented size cap | Debian-maintained: 5.13-3 sid, 5.13-1 trixie, bookworm-backports |
| **squid-deb-proxy** | yes | yes, but only via `refresh-ims` (always revalidates) | squid semantics; 5xx on failure | only if you rewrite its squid config to `intercept` | community only, 2016 | fixed `cache_dir 40000` (40 GB) + LRU | Ubuntu-packaged config, low activity |
| **squid (intercept)** | yes | yes | `max-stale` / `offline_mode` / `store-stale` | **yes** (`http_port … intercept`/`tproxy`), HTTPS needs `ssl-bump`+CA | official-ish `ubuntu/squid` image | `cache_dir` size, LRU | upstream active (7.x) |
| **nginx `proxy_cache`** | yes | yes (with tuning) | `proxy_cache_use_stale` + `proxy_next_upstream http_404` | **yes**, as a Host-header reverse proxy | official `nginx` image | `max_size` + LRU | upstream active |
| **aptly** | no — *mirror* (publishes its own repo) | downloads whole mirror metadata + debs | n/a | no | community | **full mirror** = large | active (v1.6.3, 2026-06) |
| **debmirror / apt-mirror** | no — *fetchers/mirrors* | yes (full) | n/a | no | no | **full/partial mirror** = large | debmirror packaged; apt-mirror last release 2017 |

## apt-cacher-ng

Caching HTTP proxy purpose-built for apt/dpkg traffic; also serves rpm/Kali/Arch via remaps.

**Storage / dedup.** Cache tree is `/var/cache/apt-cacher-ng/<repo>/…` and mirrors the *URL path*: "The internal cache directory tree follows the URL requests from the clients unless modified by Remapping rules", e.g. `debrep/dists/sid`, `security.debian.org/dists/lenny` ([config-serv.html §4.3](https://www.unix-ag.uni-kl.de/~bloch/acng/html/config-serv.html), [howtos §8.2](https://www.unix-ag.uni-kl.de/~bloch/acng/html/howtos.html)). So the same file fetched from two *different* mirror hostnames is stored twice unless you configure **Merging**, an "equality list" of base URLs whose cache content and live download stream are shared ([config-serv.html](https://www.unix-ag.uni-kl.de/~bloch/acng/html/config-serv.html)). Deduplication is therefore *mirror-level*, not content-addressed; no hardlink/chunk dedup is documented.

**Static vs volatile.** Two file classes: static (`PFilePattern`, e.g. `/\.deb$`) and volatile (`VfilePattern` — "same URL, changing contents": `Packages`, `Sources`, `Release`, `Pdiff`, translations) ([howtos](https://www.unix-ag.uni-kl.de/~bloch/acng/html/howtos.html#diffs), shipped `/etc/apt-cacher-ng/acng.conf`). Volatile files are revalidated; `VfileUseRangeOps: 1` makes it use `Range`/`If-Range` instead of full refetch (shipped `acng.conf`, default `1`).

**Index caching.** Yes — any index apt fetches through the proxy is stored. `FreshIndexMaxAge: 27` (shipped `acng.conf`, default `27`, seconds) freezes the known state of index files so concurrent `apt update` clients share one upstream fetch; the shipped comment warns higher values "increase the risk of delivering inconsistent responses".

**Failure behaviour.** In `job.cc`, a file is treated as volatile only when `!cfg::offlinemode && data_type == FILE_VOLATILE`, i.e. **with `Offlinemode: 0` (default) a volatile index that cannot be revalidated produces a 5xx to apt — no automatic stale serving** ([job.cc](https://sources.debian.org/src/apt-cacher-ng/3.7.5-1.3/src/job.cc/), error emitted from [dlcon.cc:168](https://sources.debian.org/src/apt-cacher-ng/3.7.5-1.3/src/dlcon.cc/) `"Download Expired"`). With `Offlinemode: 1` the shipped config says it will "Forbid outgoing connections and work without an internet connection or respond with 503 error where it's not possible" — complete cached indexes are then served without revalidation.

**Upstream failover (relevant to flaky Arch-style mirrors).** ACNG rotates between the mirrors listed per repository and *blacklists* bad backends: `"Keyfile N/A, mirror blacklisted"` on HTTP ≥400 for a repo keyfile (`InRelease`/`Release`) — it then reconnects to the next backend ([dlcon.cc:576](https://sources.debian.org/src/apt-cacher-ng/3.7.5-1.3/src/dlcon.cc/)); `"Mirror blocked due to repeated errors"` ([dlcon.cc:296](https://sources.debian.org/src/apt-cacher-ng/3.7.5-1.3/src/dlcon.cc/)); `DlMaxRetries: 2` retries after a reconnect. This is the only tool surveyed whose mirror-rotation/404 tolerance is a core feature rather than a bolt-on.

**Config.** Remaps in `/etc/apt-cacher-ng/acng.conf`:

```
CacheDir: /var/cache/apt-cacher-ng
Port: 3142
Remap-debrep: file:deb_mirror*.gz /debian ; file:backends_debian
Remap-uburep: file:ubuntu_mirrors /ubuntu ; file:backends_ubuntu
Remap-alxrep: file:archlx_mirrors /archlinux          # Arch built in as well
Offlinemode: 0
FreshIndexMaxAge: 27
AllowUserPorts: 80        # only port 80 remotes by default
```

Client side (must be explicit):

```
echo 'Acquire::http::Proxy "http://cache.lan:3142";' > /etc/apt/apt.conf.d/02proxy
# or rewrite sources.list to  deb http://cache.lan:3142/ftp.debian.org/debian trixie main
```

Mixing proxy config *and* rewritten sources "confuses the server" ([config-servquick.html](https://www.unix-ag.uni-kl.de/~bloch/acng/html/config-servquick.html)).

**Transparency: none.** Chapter 10 states outright "Transparent proxy mode not implemented yet" ([bugs.html](https://www.unix-ag.uni-kl.de/~bloch/acng/html/bugs.html)); only Basic proxy auth, only HEAD/GET, CONNECT needs `PassThroughPattern`.

**HTTPS: not cacheable.** "it is not possible to act as an intermediate server (e.g. caching proxy) by the same rules as with HTTP"; DNS rerouting cannot work because the cache "cannot fake the real certificate". Workarounds: `Acquire::https::Proxy "DIRECT"`, listing https URLs as remap targets so ACNG fetches them, `PassThroughPattern: .*` (plain tunnel, no caching), or the `deb http://HTTPS///host/… ` "HTTPS///" trick ([howtos §8.3](https://www.unix-ag.uni-kl.de/~bloch/acng/html/howtos.html)).

**Docker.** No official image. `sameersbn/apt-cacher-ng` (last push 2024-12-02, 8.8M pulls) and `mbentley/apt-cacher-ng` (last push 2026-10-26) on [Docker Hub](https://hub.docker.com/r/mbentley/apt-cacher-ng).

**Disk.** No size cap; the shipped config prunes *unreferenced* files after N days and warns that low values "can delete index files during mirror downtime". With one small-server fleet the footprint is roughly "sum of distinct .deb versions ever requested", which is the lowest of all options here.

## approx

Debian `approx` is "a proxy server for Debian archive files": it answers apt's HTTP requests, downloads missing files from the remote repository, caches them, and delivers ([approx(8)](https://manpages.debian.org/trixie/approx/approx.8.en.html)). Invoked from `inetd`, default port **9999**; a request to `http://approx-server/<repository>/<path>` is proxied to `<remote>/<path>` and cached under `$cache/<repository>/<path>` ([approx.conf(5)](https://manpages.debian.org/trixie/approx/approx.conf.5.en.html)).

```
# /etc/approx/approx.conf
debian   http://ftp.debian.org/debian
ubuntu   http://archive.ubuntu.com/ubuntu
$cache        /var/cache/approx
$interval     60      # minutes before a cached file is checked upstream
$offline      true    # deliver possibly-out-of-date cached files when upstream is unreachable
$max_wait     10
$pdiffs       true
```

`$offline` is the single most attractive feature in this whole survey for "upstreams that rotate/vanish": index files are served from cache instead of erroring. It is **not** transparent (clients rewrite sources.list to `http://approx:9999/debian`). No size cap or expiry is documented in the man page (**unverified** whether any pruning exists at all). Packaging is healthy (5.13-3 in sid, 5.13-1 in trixie, 5.13-1~bpo12+1 in bookworm-backports via [sources.debian.org](https://sources.debian.org/src/approx/)), but Docker support is a single community image `ptman/approx` last pushed 2019-10-28.

## squid-deb-proxy

Not a separate daemon: a **squid config profile** shipped by Ubuntu (`/etc/squid-deb-proxy/squid-deb-proxy.conf`) plus an avahi auto-discovery client. Exact shipped lines (0.9.2):

```
http_port 3563
maximum_object_size 512 MB
cache_dir aufs /var/cache/squid-deb-proxy 40000 16 256      # 40 GB
refresh_pattern deb$ 129600 100% 129600                     # also udeb$, tar.gz/xz/bz2
refresh_pattern \/(Packages|Sources)(|\.bz2|\.gz|\.xz)$ 0 0% 0 refresh-ims
refresh_pattern \/Release(|\.gpg)$ 0 0% 0 refresh-ims
refresh_pattern \/InRelease$ 0 0% 0 refresh-ims
refresh_pattern \/(Translation-.*)(|\.bz2|\.gz|\.xz)$ 0 0% 0 refresh-ims
cache deny !to_archive_mirrors
```

So: debs cached for 90 days (129600 min) at 100% lm-factor; **indexes are cached but never served without revalidation** (`refresh-ims` = "contact the origin when the client sends If-Modified-Since", which apt always does for indexes). That means `apt update` still depends on upstream reachability — it saves bandwidth on package payloads and on unchanged indexes, not availability.

Limitations: destinations are allow-listed to official Debian/Ubuntu hosts by default (`mirror-dstdomain.acl`), so third-party repos need ACL edits; profile is forward-proxy only (transparency would mean adding `http_port 80 intercept` to the squid config yourself); nothing here does upstream mirror failover. Clients either set `Acquire::http::Proxy "http://proxy:3563/"` or install `squid-deb-proxy-client` for avahi discovery ([README in /usr/share/doc/squid-deb-proxy](https://launchpad.net/ubuntu/+source/squid-deb-proxy)). Docker: `pmoust/squid-deb-proxy` last pushed 2016-05-31 — effectively unmaintained.

## Squid in intercept/transparent mode (generic)

Squid *can* be the transparent cache ACNG cannot be:

```
http_port 80 intercept            # or: http_port 80 tproxy (Linux TPROXY)
http_port 443 ssl-bump cert=/etc/squid/bump.pem generate-host-certificates=on
ssl_bump bump all
refresh_pattern \/(InRelease|Release|Packages)$ 0 0% 0 refresh-ims
refresh_pattern .deb$ 43200 100% 43200
refresh_pattern . 0 20% 4320 max-stale=86400 store-stale
offline_mode off
```

- `intercept`/`tproxy` are documented `http_port` modes (IP-layer interception; auth is disabled in intercept mode) ([squid http_port](https://www.squid-cache.org/Doc/config/http_port/), [Intercept examples](https://wiki.squid-cache.org/ConfigExamples/Intercept)).
- `refresh_pattern … max-stale=NN` — "Squid won't serve objects more stale than this even if it failed to validate the object"; `store-stale` stores responses with no freshness info; `refresh-ims` forces origin contact when the client sends `If-Modified-Since` ([refresh_pattern](https://www.squid-cache.org/Doc/config/refresh_pattern/)).
- `offline_mode on` = "Squid will never try to validate cached objects" — global stale serving, the blunt equivalent of approx's `$offline` ([offline_mode](https://www.squid-cache.org/Doc/config/offline_mode/)).
- `cache_peer` with `default`, `round-robin`, `no-query` gives parent-proxy/origin failover, but it does *not* convert upstream failure into stale content ([cache_peer](https://www.squid-cache.org/Doc/config/cache_peer/)).
- HTTPS repos need `ssl-bump` = MITM with a generated cert; the client must trust your CA, otherwise apt fails cert verification. Squid's own wiki warns that CONNECT tunnels are opaque and bumping without consent may be illegal ([HTTPS wiki](https://wiki.squid-cache.org/Features/HTTPS)).
- `stale-while-revalidate` (RFC 5861) is **not implemented** by squid ([Feature page](https://wiki.squid-cache.org/Features/StaleWhileRevalidate)), so background refresh of `Packages` is not available.

**What actually breaks with a stale index** (important for choosing tuning): apt validates every index against the `Release`/`InRelease` it just fetched. If the cache serves an *old* `InRelease` together with an *old* `Packages`, `apt update` succeeds and silently reports nothing new. If freshness diverges — new `InRelease`, stale `Packages` (or vice versa) — apt aborts with `Hash Sum mismatch` ([apt-pkg/acquire-item.cc:963](https://sources.debian.org/src/apt/3.0.3/apt-pkg/acquire-item.cc/), [:1114](https://sources.debian.org/src/apt/3.0.3/apt-pkg/acquire-item.cc/)). A cache must therefore key/expire the index family *together*, or rely on by-hash (below).

## nginx `proxy_cache` as a DNS-redirected transparent cache

Viable for HTTP and, for a "one box, several distros" design, the most flexible option: nginx is a reverse proxy, so it does not care whether the client thinks it is talking to a proxy or to `deb.debian.org` — with DNS pointed at the cache, apt sends an origin-form request whose `Host` header names the real mirror.

```
proxy_cache_path /var/cache/apt levels=1:2 keys_zone=apt:64m max_size=20g inactive=30d use_temp_path=off;

server {
    listen 80 default_server;
    resolver 9.9.9.9 valid=60s ipv6=off;

    location / {
        proxy_pass http://$host$request_uri;   # Host-header routing
        proxy_set_header Host $host;
        proxy_ssl_server_name on;              # only for https upstreams (see below)

        proxy_cache apt;
        proxy_cache_key $host$request_uri;
        proxy_cache_valid 200 302 10m;         # mirrors send no Cache-Control/Expires
        proxy_cache_valid 404 1m;
        proxy_cache_use_stale error timeout updating http_404 http_500 http_502 http_503 http_504;
        proxy_cache_background_update on;
        proxy_cache_lock on;                   # collapse concurrent apt update storms
        proxy_force_ranges on;                 # apt resumes with Range/If-Range
        proxy_next_upstream error timeout http_404;
    }
}
```

Facts behind each line:

- Default cache key is `$scheme$proxy_host$request_uri`; with a templated `proxy_pass` you must set `$host$request_uri` explicitly ([proxy_cache_key](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)). Keeping the host in the key prevents two mirrors' differing paths from colliding.
- `proxy_next_upstream` defaults to `error timeout` only — **404 failover must be listed explicitly**, and it only works against an `upstream {}` group, not against a variable `proxy_pass`; with DNS-based redirection there is no nginx-level mirror rotation (you get "retry the same hostname", not "try another mirror"). Mirror rotation would require naming mirrors in an `upstream` block, which conflicts with `$host` routing.
- `proxy_cache_use_stale` supports `http_404/403/429/500/502/503/504`, `updating`, `error`, `timeout`; `updating` + `proxy_cache_background_update on` gives serve-stale-while-refreshing. nginx also honours `stale-while-revalidate`/`stale-if-error` from upstream `Cache-Control` (1.11.10+), which Debian/Ubuntu mirrors do not send.
- Without `proxy_cache_valid` nothing is cached in practice, because Debian/Ubuntu mirror responses carry no freshness headers.
- `inactive` (default 10m) deletes entries regardless of freshness — must be raised for a cache meant to hold indexes across a weekend.
- Range handling: `proxy_force_ranges` enables byte ranges for cached responses regardless of `Accept-Ranges`; `proxy_cache_max_range_offset` (optional) passes large ranges upstream uncached.

**apt's own request behaviour** (this is what a caching layer must survive), from [methods/http.cc](https://sources.debian.org/src/apt/3.0.3/methods/http.cc/):

- For index files apt sends `Cache-Control: max-age=0` (`Max-Age` config default 0, line ~919) — it explicitly asks for revalidation. Under standard HTTP semantics that `max-age=0` forces revalidation (I did not verify this in squid's source); nginx, by contrast, serves from cache unless you add `proxy_cache_bypass` on client headers, so only nginx actually makes `apt update` work against a dead upstream by default.
- Resuming uses `Range: bytes=<n>-` + `If-Range: <mtime>`; otherwise `If-Modified-Since`. A cache that ignores `If-Range`/mtime can hand back a mismatched fragment → hash failure.
- apt sends absolute-form request URIs **only when a proxy is configured** (line ~900); with no proxy it sends origin-form, which is exactly what a Host-based reverse proxy expects.

**by-hash is the cache's friend.** `Acquire-By-Hash` (optional, default `no`) tells apt that indexes are also available at `<dir>/by-hash/<HashType>/<Hash>` ([DebianRepository/Format](https://wiki.debian.org/DebianRepository/Format)). Those URLs are content-addressed and immutable, so they can be cached forever and shared by every mirror; the spec asks servers to keep the current and "two or more previous versions" of each index, with the canonical filename as a link to the newest. apt 3.0.3 builds the by-hash URI as an *alternative* and tries it first, falling back to the canonical path ([acquire-item.cc ~line 483](https://sources.debian.org/src/apt/3.0.3/apt-pkg/acquire-item.cc/)); `apt.conf` `Acquire::By-Hash` is "True by default, but automatically disabled if the source indicates no support" ([apt.conf(5)](https://manpages.debian.org/trixie/apt/apt.conf.5.en.html)). Because `deb.debian.org`/`archive.ubuntu.com` do advertise it, a cache that stores by-hash URLs never serves a mismatched index — the failure mode degrades to "old but consistent" (silently no updates) instead of `Hash Sum mismatch`.

## Transparent proxy reality check

What DNS redirection can do: point `archive.ubuntu.com`, `deb.debian.org`, … at your cache IP. Every client then connects there with an origin-form request and `Host:` intact, so a **Host-routing reverse proxy (nginx) or squid in `intercept` mode** will serve them. No client config is needed for plain HTTP.

What it cannot do:

1. **Port.** DNS only changes the address, not the port. apt talks to 80/443. So the cache must listen on 80 (and 443) itself. ACNG's `Port: 3142` is unreachable this way, and even moved to 80 it would still need proxy-form requests, since intercept mode is not implemented at all.
2. **HTTPS.** Debian/Ubuntu archives are mostly plain HTTP, but some third-party repos and Ubuntu's `esm.ubuntu.com` are HTTPS. Redirected HTTPS hits your box, your cert does not match, and apt refuses (`Certificate verification failed` / server certificate mismatch). ACNG states this outright: a middlebox "cannot fake the real certificate" ([howtos §8.3](https://www.unix-ag.uni-kl.de/~bloch/acng/html/howtos.html)). Options: (a) leave 443 untouched — only redirect port 80 via NAT/iptables rather than DNS-wide; (b) MITM with `ssl-bump`/`proxy_ssl_*` and install your CA on every client (which is more invasive than one `apt.conf.d` line); (c) per-host `Acquire::https::Proxy "DIRECT"` plus `Acquire::http::Proxy::host "DIRECT"` for the few HTTPS sources ([howtos §8.5](https://www.unix-ag.uni-kl.de/~bloch/acng/html/howtos.html)).
3. **Redirects to another host.** apt follows 302s; each hop re-enters the cache under a new `Host`, which is fine, but any hop that lands on HTTPS is back to problem 2.
4. **Per-host bypass.** Clients that must reach a host directly still need `Acquire::http::Proxy::<host> "DIRECT"`; apt reads `Proxy::<host>` from the config ([http.cc:437](https://sources.debian.org/src/apt/3.0.3/methods/http.cc/)).

Minimal client-side config that realistically remains: **nothing** if you intercept port 80 with nginx/squid and keep HTTPS sources direct; or **one line** `Acquire::http::Proxy "http://cache:PORT/"` if you use ACNG/approx; or CA installation if you want HTTPS cached too. Note also that the *cache* must be able to resolve real names — with DNS hijacked network-wide, the cache needs its own resolver pointing at upstream DNS (or hardcoded mirror IPs), otherwise it hijacks itself.

## Other tools (mirror vs cache, so they can be dismissed on disk grounds)

- **aptly** — repository management: `aptly mirror create` + `update` downloads *all* referenced packages of a distribution, then `publish` serves them; it is a mirror/publishing tool, not an on-demand cache ([aptly mirror docs](https://www.aptly.info/doc/aptly/mirror/create/)). Active: v1.6.3, 2026-06-25 ([GitHub](https://github.com/aptly-dev/aptly/releases)).
- **debmirror** — "download partial mirror of a Debian Archive"; fetches a configured set of arches/sections, i.e. tens of GB for a full one ([debmirror(1)](https://manpages.debian.org/trixie/debmirror/debmirror.1.en.html)). Debian 1:2.49 in sid.
- **apt-mirror** — full-mirror script; last release 0.5.4 (2017-05-29), repo pushed 2024 ([GitHub](https://github.com/apt-mirror/apt-mirror)). Avoid.
- **pulp_deb** — content-management platform with on-demand download support in pulpcore; powerful but heavy (Postgres, workers, Redis). Details of the Debian on-demand path **unverified** — the docs page rendered as navigation only ([docs](https://docs.pulpproject.org/pulp_deb/)).
- **ACNG forks** — none found that are packaged; the upstream project itself is the maintained line.

## Recommendation for this user

- **If "no client config at all" is a hard requirement:** nginx `proxy_cache` on port 80 with `$host$request_uri` routing, `proxy_cache_valid` set explicitly, `proxy_cache_use_stale error timeout http_404 http_5xx`, `proxy_cache_lock on`, `proxy_force_ranges on`, and `inactive=30d`+. It is the only option here that is transparent *and* keeps `apt update` working while upstream is down (it will serve its own cached copy and ignore apt's `max-age=0`). Trade-offs: no true mirror rotation (404 failover needs a fixed `upstream` group, which conflicts with `$host` routing), HTTPS stays client-side, and you must hand-tune expiry or accept silently-stale-but-consistent indexes.
- **If one-line client config is acceptable (recommended trade-off):** **apt-cacher-ng** — mirror rotation with automatic blacklisting of 404/erroring backends (`dlcon.cc:576`), `FreshIndexMaxAge` to damp `apt update` storms, built-in Debian/Ubuntu/Kali/Arch remaps, smallest disk footprint, one container. Cost: `Acquire::http::Proxy` on every client, no transparency, no stale-on-failure unless you flip `Offlinemode` (which also blocks all writes).
- **If surviving upstream outage matters more than transparency:** **approx** with `$offline true` gives stale indexes on demand with a 5-line config, but it is Debian-only packaging, has no docker story, no transparent mode, and no documented size cap — fine for a handful of small servers.
- **Skip** `squid-deb-proxy` (index entries always revalidated, stale 2016 image, 40 GB default cache), `aptly`/`debmirror`/`apt-mirror` (full mirrors), and squid-bump-based HTTPS caching (CA on every client costs more than the proxy line you were avoiding).
