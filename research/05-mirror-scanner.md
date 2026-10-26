# 05 — Finding mirrors automatically: discovery sources, freshness gates, throughput ranking, and the Host-header trap

The cache needs an `upstream` pool per distro that is (a) current, (b) actually fast from *this* network, and (c) editable by a script instead of by hand. `tools/scan-mirrors.py` implements that. This file records what was verified on 2026-10-26 while building it — including one finding that changed the design: **a mirror pool can only contain peers that accept the same `Host` header**.

Everything below was measured from one host (`10.64.20.182/23`, La Paz, Bolivia) unless it is marked as documentation.

---

## 1. Where the candidate lists come from (verified endpoints)

| distro | source | what it gives | notes |
|---|---|---|---|
| Arch | `https://archlinux.org/mirrors/status/json/` | 1190 entries: `url`, `protocol`, `active`, `completion_pct`, `delay`, `last_sync`, `duration_avg`, `score`, `ipv4/ipv6`, `country` | v3 schema; **93 entries have `delay: null`** and must be dropped, not treated as "0 s behind" |
| Arch | `https://archlinux.org/mirrors/status/tier/1/json/` and `/tier/2/json/` | 270 tier‑1 and 910 tier‑2 URLs | the only explicit *tier* signal any of the three distros publishes |
| Debian | `https://mirror-master.debian.org/status/Mirrors.masterlist` | 358 `Site:` blocks, 344 `Archive-http:` | official; there is **no JSON feed** (every `/status/*.json` path 404s) |
| Debian | `https://mirror-master.debian.org/status/mirror-status.html` | per-mirror archive version, last update, score | HTML; usable as a cross-check, not parsed here |
| Ubuntu | `https://launchpad.net/ubuntu/+archivemirrors` | 592 mirrors with protocol and "Up to date" | the only machine-readable-ish list that worked |
| Ubuntu | `http://mirrors.ubuntu.com/mirrors.txt` | **one line**: `archive.ubuntu.com` | the per-country files work, the HTTPS endpoint was dead from here |

Two things that look like freshness oracles and are not:

- **`Release` `Date:` for a frozen suite.** `dists/noble/Release` is stamped `2024-04-25` and `dists/stable/Release` only moves at a point release. Any "is this mirror up to date" check based on `Date:` age will reject every mirror of a stable suite, or accept every mirror of it. Use the **content hash** against the origin instead.
- **`by-hash`.** It makes *index entries* immutable, but `Release`/`InRelease` themselves are not published by-hash (`/dists/noble/by-hash/.../Release` 404s), so `Release` is the one object that must be fetched and judged.

## 2. Freshness gates actually used

| distro | hard gate | why |
|---|---|---|
| Arch | `active`, `completion_pct == 1.0`, `delay is not None`, `delay <= --max-delay` (default 3600 s), path root exactly `/archlinux`, and `core.db` must answer `200` with gzip magic | Arch publishes real sync state per mirror; a lagging mirror is the reason a brand-new package 404s |
| Debian | frozen suite → `sha256(Release)` must equal the origin's; rolling suite (`-security`, `-updates`, `testing`) → `Date:` within `--max-stale` | frozen suites never change `Date:`; rolling suites never stay byte-identical |
| Ubuntu | same as Debian | same |

The path-root check is not cosmetic: of 346 freshly synced plain-HTTP Arch mirrors, only 250 serve the archive at `/archlinux` (309 in the full list); 46 serve it at `/`, 18 at `/arch`, 11 at `/pub/archlinux`. Because one `proxy_pass` prefix serves a whole pool, a peer with a different root 404s every request.

## 3. The Host-header trap (this is the finding that matters)

nginx cannot vary `Host` per selected upstream: one `proxy_set_header` applies to the whole group. Peers split into three classes, measured:

```
# Cloudflare-fronted peer, asked with somebody else's Host
curl -H "Host: ftp.halifax.rwth-aachen.de" http://mirror.sahil.world/archlinux/core/os/x86_64/core.db
  -> 409  (17 bytes)
curl -H "Host: deb.debian.org"             http://mirror.sahil.world/...
  -> 530  (17 bytes)

# plain-Apache peer, asked with a Cloudflare name
curl -H "Host: arch.s-levi.com"            http://ftp.halifax.rwth-aachen.de/...
  -> 200  131635 bytes

# plain peer that redirects plain HTTP
curl                                       http://mirror.rackspace.com/archlinux/core/os/x86_64/core.db
  -> 301  Location: https://mirror.rackspace.com/...
```

So:

1. A **Cloudflare-fronted** mirror answers only for `Host` values inside zones Cloudflare proxies. Given a foreign `Host` it returns `409`/`530`/`403`. It ignores the peer you dialled and serves the zone named in `Host` — i.e. for such a peer the *name in the pool* selects a PoP, and the *Host header* selects the content.
2. A **plain** mirror ignores `Host` entirely and serves its default vhost, which is the archive, so it tolerates any value.
3. A mirror that **redirects to HTTPS** answers `301` no matter what, so it can never serve a plain-HTTP cache.

Consequences, both applied in the config:

- `tools/scan-mirrors.py` probes and speed-tests every candidate **with the pool's own `Host` header**, exactly like nginx will. Peers that 301/403/409 under that header are dropped at pool-build time. With `Host: ftp.halifax.rwth-aachen.de`, 58 of 131 Arch candidates survive (was 118 when each peer was probed with its own name) and 220 of 334 Debian mirrors survive (was 306).
- `proxy_next_upstream` has **no 3xx and no 409 case** — nginx accepts only `error timeout invalid_header http_403 http_404 http_429 http_500..http_504 non_idempotent off`, and anything else is `[emerg] invalid value "http_301"`. A redirecting peer therefore cannot be hopped over at request time, which is why the gate lives in the scanner. `http_403` and `http_404` *can* be hopped, and both are.
- The `Host` value travels with the pool: the generator emits `map $target $target_host { ... }` into `mirrors.conf` next to the `upstream` blocks, so the value the probe tested and the value nginx sends cannot drift apart.

### 3.1 The Cloudflare name override, and what it buys

`--host-header arch=us.mirrors.cicku.me` sets a Cloudflare-proxied name as the pool's `Host`. Cloudflare-fronted peers come back into the pool and speed recovers — same scan, 12 MiB budget:

| pool `Host` | peers that pass | fastest measured |
|---|---|---|
| `ftp.halifax.rwth-aachen.de` (plain, default) | 58 / 131 | 4.6 MB/s |
| `us.mirrors.cicku.me` (Cloudflare) | more, incl. CF-fronted | **7.9 MB/s** |

The catch: every CF-fronted peer now depends on that zone still being proxied. If it stops, they answer `409`, which nginx cannot retry. Plain peers are unaffected (they ignore `Host`), so the failure is partial, not fatal. Default stays plain; the override exists for people who want the extra speed and accept the dependency.

### 3.2 Why Cloudflare peers are so fast here

`CF-Ray: ...-LPB` on a `2.8 ms` TCP connect to `104.18.8.165` — Cloudflare has a La Paz PoP. That is a genuine 3 ms anycast hop, not a transparent proxy: `151.101.2.132` (Fastly) answers in `22 ms`, `23.152.160.16` (fcix) in `200 ms`, and port 22 times out on all of them. So on this network the fastest package path is "any Cloudflare-fronted mirror + the right Host header", and the second fastest is Fastly (`deb.debian.org`, `archive.ubuntu.com`, 22 ms, 7–11 MB/s).

## 4. Proximity and speed are measured, not assumed

- Proximity = one TCP connect to port 80 (`time_connect`), never ICMP. ICMP is either filtered or rate-limited by most of these hosts and says nothing about HTTP path quality.
- Speed = an HTTP `Range` GET for a fixed byte budget (`12 MiB`, two rounds, best kept) from `Contents-amd64.gz` / `Packages.xz` / the Arch ISO, after a `bytes=0-0` request to find a blob that answers.
- Finalists = half the lowest-RTT peers **plus an even sample of the rest**. Without the second half, anycast CDNs never get measured — which is exactly the mistake the user warned about ("don't assume near = fast"). Observed: `deb.debian.org` at 22 ms RTT beat `mirror.hnd.cl` at 51 ms, and several 3 ms peers served slower than both.
- Ranking = `speed × (1 + tier_bonus)` with `--tier-bonus 10` (percent) for tier-1/origin sources. Freshness is already a hard gate, so tier is only a tie-breaker that keeps the pool on boxes other mirrors sync *from*; a genuinely faster leaf still wins.
- `weight = clamp(round(5 × score / best), 1, 5)`, so weights are relative inside a pool and stay stable as absolute speeds drift.

## 5. BBR: not observable from the client

The question "does this mirror use BBR" cannot be answered by a client. Congestion control is a property of the sender's socket; nothing in the HTTP response advertises it. Fastly's own docs describe BBR and expose `client.socket.congestion_algorithm` to VCL, but nowhere state that their egress defaults to BBR, so the claim is **UNVERIFIED** and is not used in ranking.

What is observable is the *effect* BBR is chosen for: a high sustained rate at higher RTT and under loss. A fixed-byte Range test captures that, which is why throughput — not a guessed algorithm — is the ranking signal. The user's `rate-mirrors` output is consistent with this: `fastly.mirror.pkgbuild.com` 16.4 MB/s (CDN, large BDP) vs `mirror.osbeck.com` 9.9 MB/s (single box, closer). Note that Fastly's *Arch* endpoint is HTTPS-only (`http://` → `301` on every path), so it cannot be a peer of this cache; `deb.debian.org` and `archive.ubuntu.com` are Fastly and do speak plain HTTP.

## 6. DNS: resolve with public resolvers, and pin the address

The host resolver on this network answers with a **local mirror appliance**:

```
getent hosts mirror.fcix.net        -> 10.64.21.103      (private, LAN)
dns @1.1.1.1 mirror.fcix.net        -> 23.152.160.16     (real; 200 ms away)
getent hosts mirror.leaseweb.net    -> 10.64.21.103
```

Measuring through that resolver measures the appliance, not the mirror — an early run reported `mirror.fcix.net` at `76.0 MB/s / 6 ms`, which was that box. Once `nginx/` started using public resolvers, the scanner had to do the same or its numbers would describe a different path than nginx's. So the script speaks DNS itself (`dns_a()`, a 40-line UDP A-record query) and connects to the resolved IP while sending the real name in `Host` — byte-for-byte what nginx does. `nginx/resolver.conf` and the scanner's `--resolvers` must name the same servers, and neither may be the LAN resolver: once transparent DNS points mirror names at the cache, a LAN resolver would make nginx resolve its own upstream to itself.

## 7. What the generated file looks like

`nginx/mirrors.conf`, written by the script, mounted into the container at `/config/nginx/mirrors.conf`, included from `site-confs/default.conf`:

```nginx
upstream arch_mirrors {
    zone arch_mirrors 64k;
    random two least_conn;

    server mirror.csclub.uwaterloo.ca:80 weight=5 resolve max_fails=2 fail_timeout=60s;  #   4.6 MB/s, rtt 121ms, tier 1, sync 500s ago
    ...
    keepalive 8;
}

map $target $target_host {
    default           $target;
    arch_mirrors      ftp.halifax.rwth-aachen.de;
}
```

Every comment field is a measurement, so `grep MB/s mirrors.conf` answers "why is this mirror here".

## 8. Operational shape

- **Daily:** re-run the scanner; it is ~2–3 minutes for all three distros (~940 candidates, 20 speed tests each). Cheap enough for a timer.
- **Weekly** re-rank is enough in practice, but freshness shifts hourly, so daily catches a mirror that fell behind before a user does.
- `docker exec pkgcache nginx -t && docker exec pkgcache nginx -s reload` applies a new file. The mount is a single file, so a rewrite is picked up by reload without recreating the container.
- The scanner writes `.scratch/mirror-report.json` with every measured candidate, not just the winners, so a "why did it pick this" question is answerable after the fact.

## 9. Pitfalls hit while building this

1. `proxy_next_upstream http_301` → `[emerg] invalid value "http_301"`. Only the documented set is accepted; 3xx and 409 are not in it.
2. A run that generated only `arch`/`debian` pools left `ubuntu_mirrors` undefined, so `proxy_pass http://ubuntu_mirrors...` resolved that name over DNS and answered `502`. A generated include must always cover every group the map references — `--distros` needs the full list once the file is live.
3. `TypeError: unsupported format string passed to NoneType` when the origin `Release` fetch failed; the reference fetch now retries against a public-DNS-pinned IP and degrades to a warning instead of a crash.
4. `int(b'timed out')` in the speed test: `http_get` returns the error text in the byte-count slot on transport failure, so the count must be parsed defensively.
5. `mirror.rackspace.com` 301s plain HTTP for Arch, yet had been in the pool for weeks unnoticed, because the *first* version of the pool sent `Host: mirror.rackspace.com` and rackspace answered `200` for its own name. Sending a foreign `Host` (as a mixed pool must) is what exposed it: the same peer returned `301`. Anything that "works" with one peer's own name can be broken with the group's name.
6. `getent hosts` on a mirror name is not evidence about the internet — see §6.
7. Re-generating `mirrors.conf` **after** the container started leaves the running nginx
   on the old file: the symptom was a prefix `502` with `[error] ubuntu_mirrors could not
   be resolved (3: Host not found)` for 30 s, because the loaded config predated the pool.
   `docker compose up -d` does not help if the file changed after start; only
   `nginx -s reload` does. Check with `docker exec pkgcache nginx -T | grep upstream`.

## 9b. Final measurements after Host-aware gating

- 20 cold requests for one Debian index → 7 distinct upstream addresses on 6 peers, all
  `200` (Fastly front ×9, `mirror.insacom.cl` ×5, `cure.edu.uy` ×2, purdue ×2, hnd.cl ×1).
- Absent file → `404` in **0.97 s** with all six peers in `$upstream_addr`; the same
  request cost 20.8 s while `archive.ubuntu.com` was in the pool, because that host accepts
  the connection and sends nothing instead of answering 404.
- Distributed peers, best per distro: `deb.debian.org` 11.3 MB/s at 22 ms,
  `mirror.eliv.digital` 7.6 MB/s at 3 ms, `mirrors.ic.unicamp.br` 7.5 MB/s at 76 ms.

## 10. Sources

- Arch mirror status JSON and tier endpoints — `https://archlinux.org/mirrors/status/json/`, `.../tier/1/json/`, `.../tier/2/json/`
- Arch mirror list schema (v3) — `https://wiki.archlinux.org/title/Mirrors`
- Debian mirror lists — `https://mirror-master.debian.org/status/Mirrors.masterlist`, `https://www.debian.org/mirror/list`
- Debian `Type: GeoDNS` / `Sponsor: Fastly` entries — same masterlist (5 blocks carry `Type:`)
- Ubuntu archive mirrors — `https://launchpad.net/ubuntu/+archivemirrors`
- `proxy_next_upstream` accepted parameters — `https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_next_upstream`
- `proxy_set_header` / `map` / `split_clients` — nginx http core + proxy modules
- Fastly BBR references (no egress default stated) — `https://www.fastly.com/documentation/reference/vcl/variables/client-connection/client-socket-congestion-algorithm/`
- `rate-mirrors`, `reflector`, `netselect-apt` behaviour — see `04-mirror-selection-tooling.md`
