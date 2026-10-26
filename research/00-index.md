# Research notes

Findings collected while choosing a low-space, multi-distro (Debian / Ubuntu / Arch)
package cache. The design that came out of it lives in [PLAN.md](PLAN.md); the runnable
parts are one directory up (`docker-compose.yml`, `nginx/`, `tools/`).

| File | Covers |
|---|---|
| [01-debian-ubuntu-apt.md](01-debian-ubuntu-apt.md) | apt-side tool survey: apt-cacher-ng, approx, squid-deb-proxy, squid intercept, nginx, and why mirror tools (aptly/debmirror) are disqualified on disk. Includes the transparent-proxy reality check and 39 primary sources. |
| [02-arch-pacman.md](02-arch-pacman.md) | pacman side: pacoloco in depth (config keys, 404 retry, DB revalidation, purge, Docker), the Arch Wiki shared-cache option list, nginx/HAProxy, the precise 404-on-new-package mechanics, and the HTTPS arithmetic for transparency. |
| [03-nginx-cache-findings.md](03-nginx-cache-findings.md) | The findings behind the shipped config: exact nginx cache directive semantics, by-hash vs Arch's lack of it, how 404 failover works at three independent layers, what breaks transparency, verified upstream inventory, measured test results, the build pitfalls that cost debugging time, and how mirror balancing (load spread + 404 hop + passive health) was verified. |
| [04-mirror-selection-tooling.md](04-mirror-selection-tooling.md) | The tool survey for *choosing* mirrors: rate-mirrors, reflector, netselect-apt, flexo, variants of "give me the fastest N", what each actually measures, and the discovery endpoints per distro (Arch status JSON incl. tiers, Debian masterlist, Launchpad). Includes why BBR cannot be detected from a client. |
| [05-mirror-scanner.md](05-mirror-scanner.md) | The scanner that was built (`tools/scan-mirrors.py`): discovery per distro, the freshness gates (and why a frozen suite's `Date:` is useless), the Host-header trap with its 409/530/301 measurements, proxying-free measuring of throughput, DNS pinning, the generated file shape, and the pitfalls that cost time. |
| [06-cold-fetch-acceleration.md](06-cold-fetch-acceleration.md) | Why one big uncached package is slow: `slice`, the ETag trap, and what prewarming actually wins. |

## TL;DR of the research

- A cache, not a mirror: full Arch x86_64 is 118 GB, Debian amd64 883 GB, Ubuntu noble 756 GB.
- One nginx `proxy_cache` covers Debian + Ubuntu + Arch, can be DNS-transparent for plain
  HTTP clients, and retries other mirrors on 404 (`proxy_next_upstream http_404`).
- apt is safe to cache in 2026 because of by-hash; Arch is not, so its `*.db` files need a
  short TTL plus revalidation.
- Each distro prefix balances over a pool of mirrors (`random two least_conn`, weighted
  towards the CDN) instead of pinning one; a 404 hops to the next peer, and a 404 never
  counts as a mirror failure, so "not synced yet" cannot blacklist a healthy mirror.
- Nothing in the lightweight class can cache HTTPS without a fleet-wide CA.
- Mirror choice is generated, not guessed: freshness is a hard gate (a lagging mirror is
  what 404s new packages), speed is measured by a 12 MiB ranged download from your own
  network, and anycast CDNs legitimately beat geographically nearer mirrors.
- One nginx `upstream` group must share one path root and one `Host:` header; a
  Cloudflare-fronted peer sent a foreign `Host` answers 409, and nginx cannot retry 409 or
  3xx at request time, so such peers are dropped when the pool is built.
