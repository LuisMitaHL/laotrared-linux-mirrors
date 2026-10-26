# laotrared-linux-mirrors

A small package cache for Debian, Ubuntu and Arch Linux. One nginx container, failover to
another mirror when one 404s, and optional DNS-level transparency so clients need no
configuration at all.

Reasoning, alternatives and measurements: [research/PLAN.md](research/PLAN.md).

## Quick start

```sh
mkdir -p cache
sudo chown 1000:1000 cache   # nginx workers run as `abc`; PUID/PGID remap it
docker compose up -d
curl -sI http://127.0.0.1/debian/dists/stable/Release | grep -i x-cache
```

Runs on [lscr.io/linuxserver/nginx](https://github.com/linuxserver/docker-nginx) (nginx
1.30.x, s6-overlay). The compose file mounts a vhost over the image's welcome page and
keeps the cache in `cache/`, outside the image's `/config` tree.

Point clients at it:

```
# Debian/Ubuntu sources
URIs: http://cache.lan/debian          # + http://cache.lan/debian-security

# Arch mirrorlist (one line, nothing else)
Server = http://cache.lan/archlinux/$repo/os/$arch
```

For transparent mode, send the real mirror hostnames to this box by DNS. See
research/PLAN.md §9.

## Layout

```
docker-compose.yml                     one service, port 80, PUID/PGID, mounts
cache/                                 the cache itself (git-ignored)
nginx/site-confs/default.conf          cache zone, path map, servers
nginx/snippets/locations.inc           index / immutable / sliced-package policies
nginx/snippets/proxy.inc               shared proxy behaviour
nginx/resolver.conf                    resolver override (ipv6=off)
nginx/mirrors.conf                     generated upstream pools (do not hand-edit)
pacoloco/pacoloco.yaml                 optional Arch-only alternative
tools/scan-mirrors.py                  regenerates nginx/mirrors.conf
tools/prewarm.py                       parallel chunk warm-up for big packages
research/00-index.md                   research index
research/PLAN.md                       the full plan
research/01-debian-ubuntu-apt.md       apt caching research
research/02-arch-pacman.md             pacman caching research
research/03-nginx-cache-findings.md    nginx semantics and measurements
research/04-mirror-selection-tooling.md how mirrors get chosen
research/05-mirror-scanner.md          the scanner
research/06-cold-fetch-acceleration.md slicing, prewarm, the ETag trap
```

The vhost is mounted at `/config/nginx/site-confs/default.conf` and the includes at
`/config/nginx/snippets/`. Both stay read-write so the image's init can chown them without
errors; append `:ro` if you would rather they were immutable.

## Mirrors: balanced, not pinned

Each prefix balances over a pool of mirrors (`random two least_conn`, weighted towards the
CDN) and a 404 is retried against the next peer, so a mirror that has not synced a brand-new
package costs one retry instead of a failed install.

```nginx
upstream arch_mirrors {            # generated into nginx/mirrors.conf
    zone arch_mirrors 64k;
    random two least_conn;
    server mirror.rackspace.com:80       weight=1 resolve max_fails=2 fail_timeout=60s;
    server mirror.leaseweb.net:80        weight=1 resolve max_fails=2 fail_timeout=60s;
    server ftp.halifax.rwth-aachen.de:80 weight=1 resolve max_fails=2 fail_timeout=60s;
    keepalive 8;
}
```

The real peers and weights come from the scanner, not from this page.

`map $uri $path_target` decides which: a hostname means always that one, a group name means
balance the pool. `debian-security` and `ubuntu-ports` stay pinned; each has a single
origin.

- add a mirror: append a `server` line after checking it uses the same path root, answers
  plain HTTP, and returns a real `404` for a bogus path
- park one: add `down` to its line
- pin one: replace the group name in the map with the hostname

Rationale and weights: research/PLAN.md §6. Where the peers come from and how they are
ranked: research/PLAN.md §7.

## Keeping the pools fresh

`nginx/mirrors.conf` is generated. Do not edit it; re-run the scanner.

```sh
python3 tools/scan-mirrors.py                 # ~2-3 min, ~940 candidates, no root
docker exec pkgcache nginx -t && docker exec pkgcache nginx -s reload
```

It collects candidates from the Arch status JSON, Debian's `Mirrors.masterlist` and
Launchpad, drops any mirror that is behind the origin, then downloads 12 MiB from each
survivor over your own network and keeps the six fastest per distro. Each peer keeps a
`# <speed> MB/s, rtt <ms>, tier N` comment, so the ranking can be checked later.

Daily from cron:

```sh
# /etc/cron.daily/mirror-scan   (chmod +x)
cd /opt/pkgcache && python3 tools/scan-mirrors.py \
  && docker exec pkgcache nginx -t \
  && docker exec pkgcache nginx -s reload
```

Two constraints if you edit a pool by hand: every peer must serve the same path root
(`/debian`, `/ubuntu`, `/archlinux`), and it must accept the pool's `Host:` header. A
Cloudflare-fronted mirror answers 409 to a foreign `Host`, and nginx cannot retry a 409.

## Big uncached packages

The first download of a large package is one stream from one mirror. That is the one case a
cache cannot hide. Two things help:

- **8 MiB slicing** on package files (`slice 8m` in `nginx/snippets/locations.inc`): ranges
  are served from partial content, several machines fetching the same package pull each
  slice once, and a file can be warmed in parallel.
- **`tools/prewarm.py`** fetches a file in parallel chunks before a client asks:

```sh
apt-get install --print-uris -y nginx | sudo tee /tmp/uris
python3 tools/prewarm.py --base http://cache.lan --from-print-uris /tmp/uris --jobs 16
```

On a slow mirror (1.5 MB/s) that is 40.2 s single stream against 14.1 s in 8 chunks. On a
fast CDN there is nothing to win, because one stream already saturates the link. The URL
you prewarm must match the client's exactly, query string included. Details:
research/PLAN.md §8.

Slicing found one real trap: mirrors disagree on `ETag`, and nginx's slice module aborts
the response when a later slice carries a different one, which truncates the download
silently. `proxy_hide_header ETag` is what makes a pool safe here. See research/PLAN.md
§8.2.

## Cache knobs

`max_size` (cap), `min_free` (free-space floor) and `inactive` (45 days; nginx's 10-minute
default would evict everything) sit at the top of `nginx/site-confs/default.conf`.

Every response carries `X-Cache-Status` and `X-Cache-Kind`, and the access log records
cache status per request. For a home lab, that is enough monitoring.
