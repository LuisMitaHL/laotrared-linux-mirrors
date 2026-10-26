# Arch Linux / pacman: package-cache and proxy options

Research target: cache packages **and** indexes for Arch without running a full mirror, on a small
disk, deployable via Docker Compose, tolerant of upstream mirrors that 404 on brand-new packages,
ideally shared with Debian/Ubuntu, ideally transparent (DNS-level, clients unconfigured).

All claims below were checked against primary sources (upstream source code at a pinned commit,
man pages, Arch wiki raw text, upstream docs). Anything not verified is labelled **unverified**.
Empirical checks were run on 2026-10-26 against `geo.mirror.pkgbuild.com`.

---

## Comparison table

| Tool | Caches pkgs | Caches DB/index | Multi-upstream failover on 404 | Transparent (DNS only) | Docker | Maintenance |
|---|---|---|---|---|---|---|
| **pacoloco** | yes | yes, but **revalidates every request** (`If-Modified-Since`, 304 → cached copy) | **yes** — any error incl. 404 advances to next `url`/`urls`/`mirrorlist` entry | no (client mirrorlist must point at it, plain HTTP) | official image + compose example | active (`[extra]` 1.9, 2026-08-15) |
| **Flexo** | yes | **no** — `.db`, `.db.sig`, `.files`, `.files.sig` deliberately uncacheable | **yes** — tries mirrors in rated order, 404 → next mirror | no (same reason) | official image (`nroi/flexo`) | active (AUR `flexo-git`, last modified 2026-07-04) |
| **nginx reverse proxy** (`proxy_store`/`proxy_cache`) | yes | config-dependent; the wiki-linked reference config **bypasses** DB/sig/files | **yes if you add `proxy_next_upstream ... http_404`** (not the default) | no | trivial (official image) | nginx itself active; reference config is a community repo |
| **HAProxy** | no useful cache (RAM, max 4095 MB) | no | yes (`retry-on 404`, `option redispatch`) — but it is a router, not a cache | no | yes | active |
| **Squid** | yes | technically yes, but the Arch wiki recipe deliberately caches **only** `.pkg.tar.*` | no documented per-request failover to another parent on 404 | intercept mode only on plain HTTP; HTTPS needs `ssl_bump` + local CA | yes | active (7.7 stable 2026-08-24; 8.x in beta) |
| **apt-cacher-ng** | yes | yes (extracts index from `*.db.tar.gz`) | **unverified** — backend mirror list exists, no documented 404→next-backend retry | no (HTTP proxy or URL rewrite) | yes (community images) | active |
| **`CacheServer=` read-only cache** (python http.server / darkhttpd / nginx `root`) | serves only what another host already downloaded | **never** — pacman does not use cache servers for DBs | n/a (no upstream at all; 404s never demote the cache server) | no | n/a | n/a (it is just a static file server) |
| **pacredir** | no central cache — redirects clients to *other clients* that already have the file | no | n/a | no | no | `[extra]` 0.7.15 (2026-10-15), mDNS-based |
| **pacserve** | yes (LAN peer-to-peer / proxy mode) | **unverified** | **unverified** | no | no | stale: AUR `pacserve 2021-4`, last modified 2022-07-15 |
| **mirrorbits** | no — HTTP redirector to mirrors | n/a | n/a (it picks a mirror per request) | partly (redirect, not cache) | yes | active, but wrong tool |
| **aptly / Pulp / Nexus** | no Arch support (see §5) | — | — | — | — | — |

Legend: "transparent" = works with **zero client configuration** after DNS redirection. Every cache above
requires a client-side `Server =`/`CacheServer =` line, because pacman talks HTTPS by default (§6).

---

## 1. pacoloco

Upstream: <https://github.com/anatol/pacoloco>. Code read at master commit
`9bfa1d8a7de3602b7f21d0fc098b2abf05669f92` ("Add db_path config option", 2026-10-23).

### Config: there is no `mirror` + `fallback_mirror`

The task brief assumed a `mirror:` plus `fallback_mirror:` pair. That does not exist in the current
(or 1.3-era) code base. Each repo takes exactly one of three keys
([`config.go:24-26`](https://github.com/anatol/pacoloco/blob/9bfa1d8a7de3602b7f21d0fc098b2abf05669f92/config.go#L24-L26)):

```go
URL        string   `yaml:"url"`
URLs       []string `yaml:"urls"`
Mirrorlist string   `yaml:"mirrorlist"`
```

A grep for `fallback` across `*.go`, `*.md`, `*.yaml` returns only CI workflow comments. Failover is
expressed by **`urls`** (ordered list) or by pointing at a **pacman mirrorlist** file.

Sample config ([`pacoloco.yaml.sample`](https://github.com/anatol/pacoloco/blob/master/pacoloco.yaml.sample)):

```yaml
download_timeout: 3600      # seconds; 0 disables
purge_files_after: 2592000  # 30 days; 0 disables purging entirely
repos:
  archlinux:
    urls:                   # tried in order, next one on any error
      - http://mirror.lty.me/archlinux
      - http://mirrors.kernel.org/archlinux
  archlinux-reflector:
    mirrorlist: /etc/pacman.d/mirrorlist   # pacoloco URL must NOT be in this file
  quarry:                   # third-party repo, different path layout
    url: http://pkgbuild.com/~anatolik/quarry/x86_64
# prefetch:                 # optional, see below
#   cron: 0 0 3 * * * *
#   ttl_unaccessed_in_days: 30
#   ttl_unupdated_in_days: 300
```

Client side, in `/etc/pacman.d/mirrorlist`:

```
Server = http://pacoloco.lan:9129/repo/archlinux/$repo/os/$arch
```

URL shape is `/repo/<repo-name>/<rest-of-mirror-path>`; the repo name is chosen by pacoloco, the rest
is passed through verbatim.

### Does it retry another mirror on a 404? Yes — this is the core of the 404 answer

`download()` loops over the configured upstreams and **continues on every error**, including a 404 for
a package that a stale mirror has not synced yet
([`downloader.go:86-112`](https://github.com/anatol/pacoloco/blob/9bfa1d8a7de3602b7f21d0fc098b2abf05669f92/downloader.go#L86-L112)):

```go
for _, u := range urls {
    err := d.downloadFromUpstream(u, proxyURL)
    if err != nil {
        if errors.Is(err, errSignatureNotFound) {
            return nil
        }
        log.Printf("... %v", err)
        continue // try next mirror
    }
    return nil
}
return fmt.Errorf("unable to download file %v", d.key)
```

Status handling is in `downloadFromUpstream`
([`downloader.go:183-193`](https://github.com/anatol/pacoloco/blob/9bfa1d8a7de3602b7f21d0fc098b2abf05669f92/downloader.go#L183-L193)):
`http.StatusNotFound` produces `errSignatureNotFound` **only** for `.db.sig` (Arch does not sign repo
DBs; see `downloader.go:32-39`), otherwise a plain error, which feeds the loop above. So:

* package 404 on mirror A → mirror B is tried immediately;
* `.db.sig` 404 → treated as "expected, nothing to do" (avoids hammering every mirror);
* `304 Not Modified` → cached file is served (`eventNotModified`).

If **all** upstreams fail and nothing is cached, the client gets **HTTP 500**, not 404 — deliberately,
so pacman does not mistake an infrastructure failure for "file does not exist"
([`pacoloco.go:131-146`](https://github.com/anatol/pacoloco/blob/9bfa1d8a7de3602b7f21d0fc098b2abf05669f92/pacoloco.go#L131-L146)). Negative results are never cached.

Also note: if a download fails but a cached copy exists, **the cached copy is served anyway**
([`downloader.go:311-318`](https://github.com/anatol/pacoloco/blob/9bfa1d8a7de3602b7f21d0fc098b2abf05669f92/downloader.go#L311-L318)) — upstream outage degrades to stale-but-working.

### DB / index handling

* DB files **are** cached, but **never trusted blindly**: they are in the forced-revalidation list
  ([`downloader.go:503-513`](https://github.com/anatol/pacoloco/blob/9bfa1d8a7de3602b7f21d0fc098b2abf05669f92/downloader.go#L503-L513)):

  ```go
  var forceCheckFiles = []string{".db", ".db.sig", ".files"}
  ```

  A request for `core.db` therefore skips the local cache fast path, hits the upstream with
  `If-Modified-Since` derived from the cached file's mtime
  ([`downloader.go:141-143`](https://github.com/anatol/pacoloco/blob/9bfa1d8a7de3602b7f21d0fc098b2abf05669f92/downloader.go#L141-L143)),
  and on 304 serves the cached bytes. Net effect: the index is cached (no client latency penalty, no
  re-download of 130 kB+ per client) but a rotated DB is picked up on the next client request, with no
  TTL to tune. `.files.sig` is *not* in the list (suffix mismatch) and is cached like a package.
* Concurrent clients requesting the same file share one download (`downloaders` map + refcount,
  [`downloader.go:376-438`](https://github.com/anatol/pacoloco/blob/9bfa1d8a7de3602b7f21d0fc098b2abf05669f92/downloader.go#L376-L438)).
* Optional **prefetch**: downloads the `.db` into `<cache_dir>/tmp-db`, untars it, extracts
  `%FILENAME%` lines with the regex at
  [`pacoloco.go:27`](https://github.com/anatol/pacoloco/blob/9bfa1d8a7de3602b7f21d0fc098b2abf05669f92/pacoloco.go#L27),
  and pre-downloads every listed package on a 7-field cron
  (`repo_db_mirror.go`). This is what turns the cache into a near-mirror; on a small disk it is a
  liability, so leave it off.

### Cleanup

No cron needed. `purge.go` starts a goroutine that purges once at startup and then every 24 h
(`time.NewTicker(24*time.Hour)`,
[`purge.go:12-26`](https://github.com/anatol/pacoloco/blob/9bfa1d8a7de3602b7f21d0fc098b2abf05669f92/purge.go#L12-L26)), deleting files whose **atime and mtime** are both older than
`purge_files_after` ([`purge.go:31-60`](https://github.com/anatol/pacoloco/blob/master/purge.go#L31-L60)). `purge_files_after: 0` disables purging; README recommends ≥600 s
when enabled. atime-based LRU is exactly right for "limited disk": the 30-day window only bites if a
file has not been *read* for 30 days.

### Docker

Official image `ghcr.io/anatol/pacoloco` (amd64, arm64, armv7), non-root uid/gid **65532**, config at
`/etc/pacoloco.yaml`, cache at `/var/cache/pacoloco`
([README, Docker section](https://github.com/anatol/pacoloco#docker)):

```yaml
---
services:
  pacoloco:
    container_name: pacoloco
    image: ghcr.io/anatol/pacoloco
    ports:
      - "9129:9129"
    volumes:
      - /path/to/cache:/var/cache/pacoloco
      - /path/to/config/pacoloco.yaml:/etc/pacoloco.yaml
    restart: unless-stopped
```

If bind-mounting, `chown 65532:65532 /path/to/cache` or set `user: 1000:1000` (and own the host dirs
accordingly). Prometheus metrics on `/metrics` include
`pacoloco_downloaded_files_total{repo,upstream,status}` — the `status` label is how you *see* per-mirror
404s instead of guessing.

### Verdict on the 404 problem

**Solves it.** Multiple upstreams + continue-on-any-error + no negative caching is precisely the
required behaviour; the `.db.sig` special case avoids the pathological "retry 10 mirrors for a file
that never exists" storm. Residual risk: `urls` order is static, so a permanently stale mirror costs
one extra round-trip per miss; using `mirrorlist` pointing at a `reflector`-generated list is the
pragmatic fix.

---

## 2. Arch Wiki "Network shared pacman cache" — the full option list

`Pacman/Tips_and_tricks#Network_shared_pacman_cache` is now a one-line pointer:
"See [[Package proxy cache]]" (raw wikitext lines 415-417). The canonical page is
<https://wiki.archlinux.org/title/Package_proxy_cache>. Everything it documents:

| Option | What it is | Caches DB/index? | Maintenance |
|---|---|---|---|
| `CacheServer =` + static file server (`python -m http.server`, `darkhttpd`, `miniserve`, nginx `root`) | read-only cache; clients still fetch DBs from real mirrors | **no** — pacman "will not be used for database files" | none needed (static) |
| overlay mount of a shared read-only cache (`sshfs` lowerdir + overlay, `pacman --cachedir`) | shares a warm cache without writing to it | no | none |
| **pacoloco** | caching proxy, multiple upstreams | yes (revalidated) | active |
| **Flexo** | caching proxy, auto mirror selection, parallel fetch | **no** (by design) | active |
| **pacredir** | mDNS redirector: answers pacman requests by pointing at a LAN peer that has the file | no | `[extra]` 0.7.15 |
| **pacserve** | LAN file sharing / HTTP proxy over the same directory | unverified | **stale** (AUR 2021-4, 2022) |
| **pkgdistcache**, **paclan** | distributed caches (AUR) | unverified | `pkgdistcache` updated 2026, `paclan` 2020 |
| PkgD, multipkg | historical | — | unmaintained (wiki says so) |
| rw cache over sshfs/NFS/Samba; rsync/FTP 2-way; Syncthing/Resilio | shares `/var/cache/pacman/pkg` as a filesystem | DBs are whatever the local pacman wrote | varies |
| nginx dynamic reverse proxy cache | `proxy_store` + upstream pool, see §3 | reference config bypasses DBs | community config |
| Squid | generic HTTP cache, wiki gives a package-only refresh recipe | recipe caches **only** `.pkg.tar.*` | active |

Two details from the wiki that matter for this project:

* The proxy-server section warns clients "should only use HTTP mirrors, because a proxy server cannot
  introspect HTTPS connections by default" — the same conclusion as §6.
* There is **no `ReadOnlyPkgCache` option**. A wiki full-text search returns zero hits and pacman's
  source/man pages contain no such keyword; the actual knob is `CacheServer=`. Treat the term as a
  conflation (likely with `CacheServer` + read-only export of `/var/cache/pacman/pkg`).

`CacheServer` semantics, verbatim from `pacman.conf(5)`
([`doc/pacman.conf.5.asciidoc:263-267`](https://gitlab.archlinux.org/pacman/pacman/-/blob/master/doc/pacman.conf.5.asciidoc#L263-L267)):

> A full URL to a location where the packages, and signatures (if available) for this repository can
> be found. Cache servers will be tried *before* any non-cache servers, will **not** be removed from
> the server pool for 404 download errors, and will **not** be used for database files.

That "not removed for 404" is important: a partial cache is expected to miss files, so pacman falls
through to a real mirror instead of blacklisting your cache. It also means a `CacheServer` can never
serve as the only source (and it is HTTP-only in practice).

---

## 3. nginx / HAProxy as a caching reverse proxy

### nginx

The wiki links a working reference config:
<https://github.com/nastasie-octavian/nginx_pacman_cache_config> (`nginx.conf`, 101 lines). Key parts:

```nginx
location ~ \.(db|sig|files)$ {
    proxy_pass http://mirrors$request_uri;      # DBs/sigs are NEVER cached
}

location ~ \.tar\.(xz|zst)$ {
    try_files $uri @pkg_mirror;                 # serve from disk if present
}

location @pkg_mirror {
    proxy_store         on;
    proxy_store_access  user:rw group:rw all:r;
    proxy_next_upstream error timeout http_404; # <-- the line that solves 404s
    proxy_pass          http://mirrors$request_uri;
}

upstream mirrors {
    server 127.0.0.1:8001;
    server 127.0.0.1:8002 backup;
    server 127.0.0.1:8003 backup;
}
```

Two things to know before copying it:

1. **404 failover is opt-in.** nginx's default is `proxy_next_upstream error timeout`
   ([nginx docs](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_next_upstream)), i.e.
   a 404 from the first mirror is *returned to pacman*, not retried. You must add `http_404` (the
   reference config does). `proxy_next_upstream_tries` caps the fan-out.
2. `proxy_store` is a dumb file store, not a cache: no TTL, no metadata, no LRU. Eviction is your
   problem (wiki suggests `paccache -r -k 2 -c {}`). `proxy_cache` gives you
   `proxy_cache_valid`, `proxy_cache_use_stale` and `proxy_cache_lock` (single-flight), but you then
   own the cache-key and staleness policy for **DBs**, which is where it gets dangerous (§4).

Cache sizing: packages are tens to hundreds of MB; set `proxy_max_temp_file_size 0` to avoid
double-writing large bodies to temp files, and put `proxy_store`/`proxy_cache` on the big disk.

### by-hash: Arch does not have it

Debian/Ubuntu repos expose `by-hash/SHA256/...` so a client holding an old index can still fetch the
matching package set. Arch has no such directory — verified empirically:

```
HEAD http://geo.mirror.pkgbuild.com/core/os/x86_64/by-hash/   -> 404
GET  http://geo.mirror.pkgbuild.com/core/os/x86_64/core.db.sig -> 404
GET  core.db -> 200, Content-Length 131635, Last-Modified Sat, 24 Oct 2026 20:30:51 GMT, ETag present
```

So `core.db` is a **single mutable name**, replaced in place on every sync. Consequences for a cache:

* If the cache serves a **stale `.db`** while the mirror has moved on, pacman asks for package
  versions the mirror may already have pruned → 404 per package, or (worse) it "resolves" to an old
  version and the user thinks a partial upgrade is fine.
* If the cache serves a **fresh `.db`** but the *package download* goes to a different, stale mirror,
  the requested `foo-2.0-1-x86_64.pkg.tar.zst` is 404 there. This is the exact scenario the user
  reported.
* Conditional revalidation (ETag/`If-Modified-Since`) is the only safe freshness mechanism; both
  pacoloco and pacman itself use it. A cache with a positive TTL on `.db` is a bug factory.
* Mirror status APIs are the only external signal that a mirror is in sync: the JSON at
  <https://archlinux.org/mirrors/status/json/> exposes per-URL `last_sync`, `delay`, `completion_pct`
  (sample checked 2026-10-26: `"last_sync": "2026-10-26T14:29:33Z", "delay": 2071,
  "completion_pct": 1.0`). `reflector`/pacoloco's `mirrorlist` consume this indirectly. There is no
  `X-Archive-*` header contract on Arch mirrors that a cache could key on (**unverified/absent**);
  mirrorbits-style deployments signal via HTTP redirects, not headers.

### HAProxy

HAProxy can do the dispatch half very well and the cache half badly:

* `retry-on 404` is explicitly supported — the docs list `<status>` values including `"404"` next to
  `conn-failure`, `response-timeout`, `50x`
  ([HAProxy 2.8 config, `retry-on`](https://docs.haproxy.org/2.8/configuration.html#4-retry-on)); pair
  with `option redispatch` and `retries`.
* Its native HTTP cache lives **in RAM**: `total-max-size` is in megabytes, maximum **4095**, and
  `max-object-size` defaults to 1/256 of the cache size
  ([HAProxy 2.8 config, `cache`](https://docs.haproxy.org/2.8/configuration.html#3.1-cache)). Caching
  Arch packages there is not viable on a small server.

Verdict: HAProxy is a useful **front door** (`retry-on 404` in front of two pacolocos, or in front of
a cache + a live mirror), not a cache.

---

## 4. The 404-on-new-package problem, precisely

### How Arch mirrors sync

Arch runs a **2-tier** scheme
([DeveloperWiki:NewMirrors](https://wiki.archlinux.org/title/DeveloperWiki:NewMirrors), raw lines 15-32):

* tier 1 mirrors sync from `archlinux.org` (tier 0) **hourly**;
* every other mirror must sync **from a tier 1 mirror**, "not more often than every hour, but you
  should sync at least once a day", on a random minute, using
  `rsync -rlptH --safe-links --delete-delay --delay-updates`.

So a compliant tier-2 mirror can be **up to ~24 h + 1 h behind**, and the mirror checker measures this:
<https://archlinux.org/mirrors/status/> polls the `lastsync` file in the repo root and publishes
`delay` (mean of `last check − last sync`), `completion_pct`, and an "Out of Sync Mirrors" table. On
2026-10-26 the page showed a live example of `http://mirror.selfnet.de/archlinux/` at **77:42 h**
delay. Arch wiki is blunt about the effect: "Mirrors can be out of sync and the package list from the
old mirror may not correspond to the package list of the new mirror"
([Mirrors](https://wiki.archlinux.org/title/Mirrors), raw line 82).

`--delay-updates` makes rsync stage changed files into a temp dir and rename them at the end, which
narrows but does not eliminate the window: a mirror can be serving a **fresh `core.db`** (just
renamed into place) while some package files from the same batch are still being renamed, or while it
is several hours into a sync cycle. A brand-new package is exactly the file most likely to be missing
when the DB already lists it.

### What pacman itself does on 404

pacman **does** fall through to the next `Server =` line, per-download. In `lib/libalpm/dload.c`
(commit on `master` as of 2026-10-26):

* `respcode >= 400` (which includes 404) logs `failed retrieving file '%s' from %s : The requested URL
  returned error: %ld`, increments a per-host error counter, then calls
  `curl_retry_next_server()` and restarts the transfer against the next server
  ([`dload.c:528-556`](https://gitlab.archlinux.org/pacman/pacman/-/blob/master/lib/libalpm/dload.c#L528-L556)).
* Server selection walks `cache_servers` first, then `servers`
  ([`dload.c:224-247`](https://gitlab.archlinux.org/pacman/pacman/-/blob/master/lib/libalpm/dload.c#L224-L247)).
* `server_error_limit = 3` (`dload.c:136`): after 3 soft errors a **host** is skipped "for the
  remainder of this transaction" (`dload.c:191-205`); hard errors (DNS failure) blacklist immediately
  (`dload.c:208-222`). Cache servers are only skipped on hard errors
  (`dload.c:181-188`) — that is what makes a partial `CacheServer` usable.
* DB downloads use **only** `db->servers`, never `cache_servers`
  ([`be_sync.c:189`](https://gitlab.archlinux.org/pacman/pacman/-/blob/master/lib/libalpm/be_sync.c#L189),
  contrasted with package payloads at
  [`sync.c:849-850`](https://gitlab.archlinux.org/pacman/pacman/-/blob/master/lib/libalpm/sync.c#L849-L850)).

The Arch wiki states the user-visible version of this: "When downloading packages, pacman uses the
mirrors in the order they are listed … If a package download fails (e.g. file not found, connection
timeout), the next list entry is used" ([Mirrors](https://wiki.archlinux.org/title/Mirrors), raw §
"Sorting mirrors").

Caveat: pacman retries **the package**, not **the DB**. If the DB came from stale mirror A and the
package request goes to mirror B, B's 404 is handled — but if the DB itself is stale, the version
pacman wants may no longer exist *anywhere* (old packages are pruned), and then every mirror 404s and
the transaction fails. This is why "cache the DB" is the risky half of the feature.

### Which designs actually solve it

Solving = *attempt the same file on N independent upstreams before failing, and never memoize a
failure*.

1. **pacoloco** — solves it in-process (`downloader.go:86-112`), across `urls`/`mirrorlist`, with no
   negative caching. Best fit.
2. **Flexo** — solves it (404 → next rated mirror); the code even documents the trade-off of *not*
   retrying `.db.sig` because no mirror ever has it
   ([`mirror_flexo.rs:677-688`](https://github.com/nroi/flexo/blob/master/flexo/src/mirror_flexo.rs#L677-L688)).
3. **nginx with `proxy_next_upstream ... http_404`** (plus `backup` mirrors) — solves it, but you
   must write the DB policy yourself. The reference config dodges the hard part by not caching DBs.
4. **HAProxy `retry-on 404`** in front of any of the above or in front of the client's mirror pool —
   solves the retry half, caches nothing.
5. **pacman with 3+ `Server =` lines** — already solves the *package* half natively, with a 3-error
   per-host cap. A pure static `CacheServer` adds nothing here (it is tried first, then the mirrors).
6. **Squid / a TTL cache with one upstream** — does not solve it: a 404 is cached-or-forwarded as a
   real answer, and there is no second upstream per request.

---

## 5. Multi-distro single-tool candidates

The requirement "one tool for Debian + Ubuntu + Arch" is where most marketing collapses. Status of
each candidate, primary-source checked:

| Tool | Debian/Ubuntu | Arch | Reality check |
|---|---|---|---|
| **pacoloco** | no | yes | Arch-only by design (`repos` map is just path pass-through; nothing apt-aware) |
| **apt-cacher-ng** | yes (best-in-class) | **"Mostly usable"** — docs chapter 6.4: "The mirror structure design has been identified by trial-and-error and the regular expressions might need some tuning by experts… The pacman mirror list can be modified the same way." Expiration works "in the basic mode (index data is extracted from `*.db.tar.gz` files)" ([distinstructions §6.4](https://www.unix-ag.uni-kl.de/~bloch/acng/html/distinstructions.html)) | actually supports both, but Arch is a second-class, regex-tuned path; note it still says `.db.tar.gz`, the pre-2020 extension |
| **Squid** | yes | yes, as a *generic* HTTP cache | works for both only over plain HTTP; the Arch wiki recipe deliberately caches only `.pkg.tar.*` (`refresh_pattern \.pkg\.tar\. 0 20% 4320 reload-into-ims` then `refresh_pattern . 0 0% 0`), because caching DBs naively is unsafe. No 404 failover. `refresh_pattern` exists in 7.7 (current stable, 2026-08-24) but its own doc page says it is **not available in v8** ([docs](http://www.squid-cache.org/Doc/config/refresh_pattern/), [versions](https://www.squid-cache.org/Versions/)) |
| **Pulp** | yes (`pulp_deb`) | **no** | plugin list is `pulp_rpm, pulp_deb, pulp_python, pulp_container, pulp_ansible, pulp_file, pulp_gem, pulp_maven, pulp_npm, pulp_rust, …` — no pacman/Arch plugin ([pulpproject.org](https://pulpproject.org/)) |
| **Sonatype Nexus** | yes (APT format) | **no** | "This contains functionality for maven, raw, and APT repository formats" plus npm/Docker/NuGet/PyPI in CE — no Arch ([nexus-public README](https://github.com/sonatype/nexus-public)) |
| **aptly** | yes | **no** | Debian repository management tool (mirrors/snapshots/publishing of `.deb`); not a caching proxy at all |
| **mirrorbits** | n/a | n/a | "a geographical download **redirector**" — it 302s clients to a mirror; it stores no packages ([README](https://github.com/etix/mirrorbits)) |
| **Caddy** | n/a | n/a | core Caddyfile directive index has **no** `cache` directive (0 occurrences on the directives page); caching comes from the third-party `caddyserver/cache-handler` module (Souin-based, RFC 7234, must be built with `xcaddy`) ([cache-handler README](https://github.com/caddyserver/cache-handler)) |
| **Varnish** | generic | generic | a VCL HTTP accelerator; can cache both, but you implement 404 failover and DB freshness in VCL, and it needs HTTP origins |

**Conclusion:** no actively maintained single tool covers apt **and** pacman *well*. The realistic
multi-distro answers are (a) **apt-cacher-ng** (Debian first-class, Arch "mostly usable", one daemon,
one port) or (b) **Squid** over plain HTTP (truly distro-agnostic, but no 404 failover and no index
caching in the documented recipe), or (c) **two containers** — pacoloco for Arch, apt-cacher-ng for
Debian/Ubuntu — behind one DNS name/port via a tiny front proxy. Given requirement (2), (c) is the
only one that satisfies the 404 requirement for Arch.

---

## 6. Transparent proxy for Arch: the HTTPS arithmetic

Facts:

* pacman fetches over whatever scheme the `Server =` line uses. The shipped mirrorlist is
  **100 % HTTPS**: `pacman-mirrorlist` (generated 2026-06-10) has **425/425** `Server = https://`
  entries and **0** `http://` entries
  ([mirrorlist source](https://gitlab.archlinux.org/archlinux/packaging/packages/pacman-mirrorlist/-/raw/main/mirrorlist)).
* Plain HTTP is still served by mirrors (and still listed by the mirror-status API, e.g.
  `"protocol": "http"` entries in
  <https://archlinux.org/mirrors/status/json/>). Verified directly:
  `http://geo.mirror.pkgbuild.com/core/os/x86_64/core.db` → **200**;
  `http://mirror.rackspace.com/archlinux/...` → **200**; `http://mir.archlinux.fr/...` → **200**.
  The Arch wiki's mirrorlist example still shows an `http://` line
  ([Mirrors](https://wiki.archlinux.org/title/Mirrors), raw § "Enabling a specific mirror").
* The generator form pre-checks **both** `protocol=http` and `protocol=https`
  ([mirrorlist generator](https://archlinux.org/mirrorlist/)), so http entries are obtainable, just
  not in the packaged default.

The arithmetic of DNS-only redirection:

```
pacman --(DNS: mirror.example -> cache IP)--> https://mirror.example/core/os/x86_64/core.db
                                              ^ TLS SNI = mirror.example
cache cannot present a valid cert for mirror.example
=> handshake fails, pacman logs a TLS/SSL error, nothing is cached, nothing is served.
```

There is no "cache" outcome there, only a failure. Therefore:

* **(a) client-side mirrorlist pointing at the cache over HTTP** — what every tool in §1-§3 requires:
  `Server = http://cache.lan:9129/repo/archlinux/$repo/os/$arch`. One line per client (or one
  `Include`). Not transparent.
* **(b) local CA + MITM** — Squid `ssl_bump` (or mitmproxy) terminates TLS with a certificate signed
  by a CA installed on every client. Works, but: key distribution, cert rotation, breakage on clients
  that pin, and pacman's own signature checks (default `SigLevel = Required DatabaseOptional`) mean
  the MITM still cannot forge packages — so the trust gain is zero and the operational cost is real.
* **(c) `Server = http://cache/...` in the mirrorlist** — same as (a); note the wiki's explicit warning
  that proxy clients "should only use HTTP mirrors, because a proxy server cannot introspect HTTPS
  connections by default".
* **DNS + intercept on port 80 only** (squid `http_port 3127 intercept` + iptables `REDIRECT`) only
  works if clients are *already* using http:// URLs. With the default HTTPS mirrorlist it catches
  nothing.

**Verdict:** "transparent, DNS-level, clients unconfigured" and "Arch" are mutually exclusive without
MITM. The least-bad compromise is Ansible/pacman hook or a one-line `Server =` (or `CacheServer =`)
push; if the fleet is container images, bake the mirrorlist at build time.

---

## 7. Per-tool verdict: does it solve the 404 problem?

* **pacoloco — yes.** Ordered `urls`/`mirrorlist`, continue on any error, 404 never cached, `.db.sig`
  404s short-circuited, stale copy served on outage. DB cached with mandatory revalidation.
* **Flexo — yes.** 404 → next rated mirror; DB files intentionally not cached (with a source comment
  explaining the stale-DB hazard), so it also avoids the DB-rotation failure mode entirely.
* **nginx — yes, but you build it.** Needs `proxy_next_upstream ... http_404`; the sane DB policy is
  "do not cache DBs" (as the reference config does), which then fails the "cache indexes" requirement.
* **HAProxy — half.** `retry-on 404` gives failover; the RAM-only cache cannot hold packages.
* **Squid — no.** One parent per request, 404 is a valid answer, and the documented Arch recipe caches
  packages only. Add failover only by pointing squid at a pacoloco.
* **apt-cacher-ng — partial/unverified for Arch.** Good apt behaviour, Arch supported "mostly", 404
  failover across backends not documented.
* **static `CacheServer` — no** (and not intended to): it never has the new package either, and pacman
  never asks it for DBs.
* **pacredir / pacserve — no.** pacredir only redirects to peers that already have the file; pacserve
  is unmaintained since 2022.

---

## 8. Recommendation for this user

* **Arch: run pacoloco** (one compose service, `ghcr.io/anatol/pacoloco`, uid 65532, cache volume on
  the big disk). Configure **3-5 `urls`** (or a `mirrorlist` from `reflector`) — that is the entire
  404 fix. Keep `purge_files_after` at ~30 days, `prefetch` **off**, `db_path` on the cache volume.
  Clients get one `Server = http://cache:9129/repo/archlinux/$repo/os/$arch` line (or `CacheServer =`
  if you also want pacman to keep using real mirrors for DBs).
* **Do not cache `.db` behind a TTL.** pacoloco's conditional revalidation is the correct model;
  any nginx `proxy_cache_valid` on `.db`, or a Squid rule that caches the index, will eventually serve
  a stale package set (Arch has no by-hash — verified 404 at `/by-hash/`).
* **Debian/Ubuntu: a second container (apt-cacher-ng)**, not a compromise tool. If a single daemon is
  non-negotiable, apt-cacher-ng is the only one that genuinely handles both, accepting that Arch is
  "mostly usable" and that its own docs are stale (`.db.tar.gz`).
* **Give up on DNS-only transparency** unless you are willing to run a local CA + `ssl_bump`. Push a
  one-line mirrorlist instead (config management or image build); it is less work than MITM and keeps
  pacman's HTTPS path intact. If you must intercept, do it for port 80 only and accept that you have
  to move clients to `http://` mirrors anyway.

---

## Sources

Primary (upstream code / man pages / wiki raw / vendor docs):

* pacoloco — <https://github.com/anatol/pacoloco> (README, `pacoloco.yaml.sample`, `config.go:24-26`,
  `downloader.go:32-39,86-112,141-143,183-193,311-318,376-438,503-513`, `pacoloco.go:27,131-146`,
  `purge.go:12-60`, `repo_db_mirror.go`), pinned at `9bfa1d8`
* Flexo — <https://github.com/nroi/flexo> (README; `flexo/src/mirror_flexo.rs:403-411,612-633,677-688,945-963`)
* pacman — <https://gitlab.archlinux.org/pacman/pacman> (`lib/libalpm/dload.c:136,181-247,528-556`;
  `lib/libalpm/be_sync.c:189`; `lib/libalpm/sync.c:849-850`;
  `doc/pacman.conf.5.asciidoc:263-267`)
* Arch Wiki — [Package proxy cache](https://wiki.archlinux.org/title/Package_proxy_cache),
  [Mirrors](https://wiki.archlinux.org/title/Mirrors),
  [DeveloperWiki:NewMirrors](https://wiki.archlinux.org/title/DeveloperWiki:NewMirrors),
  [Pacman/Tips and tricks](https://wiki.archlinux.org/title/Pacman/Tips_and_tricks) (now a pointer only)
* Arch mirror status / API — <https://archlinux.org/mirrors/status/>,
  <https://archlinux.org/mirrors/status/json/>, <https://archlinux.org/mirrorlist/>
* `pacman-mirrorlist` — <https://gitlab.archlinux.org/archlinux/packaging/packages/pacman-mirrorlist>
* nginx — [`proxy_next_upstream`](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_next_upstream),
  `proxy_cache`, `proxy_store`
* reference nginx pacman config — <https://github.com/nastasie-octavian/nginx_pacman_cache_config>
* HAProxy — [`retry-on`](https://docs.haproxy.org/2.8/configuration.html#4-retry-on),
  [`cache`](https://docs.haproxy.org/2.8/configuration.html#3.1-cache) (RAM, `total-max-size` ≤ 4095)
* Squid — [`refresh_pattern`](http://www.squid-cache.org/Doc/config/refresh_pattern/),
  [released versions](https://www.squid-cache.org/Versions/)
* apt-cacher-ng — [distribution-specific instructions §6.4 Arch Linux](https://www.unix-ag.uni-kl.de/~bloch/acng/html/distinstructions.html)
* Pulp — <https://pulpproject.org/> (plugin list), Nexus — <https://github.com/sonatype/nexus-public>,
  mirrorbits — <https://github.com/etix/mirrorbits>,
  Caddy cache-handler — <https://github.com/caddyserver/cache-handler>,
  Caddy directives index — <https://caddyserver.com/docs/caddyfile/directives>
* Arch package/AUR metadata — `https://archlinux.org/packages/search/json/?name=pacoloco|pacredir`,
  `https://aur.archlinux.org/rpc/v5/info?arg[]=pacserve|flexo-git|paclan|pkgdistcache`

Secondary (blogs/community, used only for colour): none — every claim above traces to the primary
sources listed, except where explicitly marked **unverified**.
