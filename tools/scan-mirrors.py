#!/usr/bin/env python3
"""Find, verify and rank upstream mirrors, then emit an nginx include file.

Design, in the order the checks run (cheapest and most decisive first):

  1. DISCOVER  candidates from the distro's own machine-readable list: Arch
               mirror status JSON, the Debian masterlist, the Launchpad
               archive list. No hostnames are invented.
  2. FRESHNESS a stale mirror is worse than a slow one: it serves 404s for
               packages that were just published. Arch publishes `delay`
               (seconds since last sync) and it is used as a hard gate.
               Debian/Ubuntu publish nothing per mirror, so for a *frozen*
               suite (stable, noble - whose Release only changes at a point
               release) the Release content hash must equal the origin's, and
               for a *rolling* suite (-updates, -security, testing) the
               Release `Date:` delta must stay under --max-stale.
  3. DISTANCE  one TCP connect on port 80, discarded afterwards. Used only to
               choose who gets the expensive download test, never to rank:
               "near" is not "fast".
  4. SPEED     an HTTP Range request for a fixed byte budget from a big file,
               twice, best kept. This ranks the mirrors and is also the only
               practical proxy for the sender's congestion control: a
               BBR-paced sender holds a high rate at high RTT and under loss,
               CUBIC collapses, and both show up as sustained MB/s.

DNS: this script resolves candidates with the SAME public resolvers nginx
uses (nginx/resolver.conf), never the host's resolver. That matters on
a network whose resolver redirects mirror names to a local cache - the host
resolver answered 10.64.21.103 for mirror.fcix.net while the real address is
23.152.160.16, so measuring via the local resolver would measure that box (or,
once transparent DNS is on, the cache itself).

Plain HTTP only (what the cache speaks), python3 stdlib only, no root.

    python3 tools/scan-mirrors.py                        # all three distros
    python3 tools/scan-mirrors.py --distros arch         # just Arch
    python3 tools/scan-mirrors.py --dry-run              # print, write nothing
    python3 tools/scan-mirrors.py --only halifax,rackspace
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import hashlib
import http.client
import json
import random
import re
import socket
import struct
import sys
import time
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

UA = "pkgcache-mirror-scan/1.0"
DEFAULT_RESOLVERS = ("1.1.1.1", "9.9.9.9", "149.112.112.112", "8.8.8.8")

# --------------------------------------------------------------------------
# Per-distro knowledge. `bases` are always candidates so the CDN/origin is in
# the pool even when the public list omits it. `index` proves the mirror serves
# the suite we care about; `blob` is the big file for the throughput test
# (first entry that answers wins).
# --------------------------------------------------------------------------
DISTROS = {
    "debian": {
        "host_header": "deb.debian.org",
        "bases": ["http://deb.debian.org/debian"],
        "index": "/dists/{suite}/Release",
        "blob": [
            "/dists/{suite}/main/Contents-amd64.gz",
            "/dists/{suite}/main/binary-amd64/Packages.xz",
        ],
        "default_suite": "stable",
    },
    "ubuntu": {
        "host_header": "archive.ubuntu.com",
        "bases": ["http://archive.ubuntu.com/ubuntu"],
        "index": "/dists/{suite}/Release",
        "blob": [
            "/dists/{suite}/Contents-amd64.gz",
            "/dists/{suite}/main/binary-amd64/Packages.xz",
        ],
        "default_suite": "noble",
    },
    "arch": {
        # One Host header serves the whole group (nginx cannot vary it per
        # selected peer), so it must be a name that every peer tolerates.
        # Plain-Apache mirrors ignore it; Cloudflare-fronted mirrors do NOT -
        # they route by Host and answer 409 for a zone they do not proxy, so
        # they are filtered out by the probe below. See research/PLAN.md section 7.
        "host_header": "ftp.halifax.rwth-aachen.de",
        "bases": ["http://mirror.rackspace.com/archlinux"],
        # Arch has no Release file; core.db is revalidated on every pacman -Sy,
        # so a mirror that cannot serve it is simply broken.
        "index": "/core/os/x86_64/core.db",
        "blob": ["/iso/latest/archlinux-x86_64.iso"],
        "default_suite": "",
    },
}

ARCH_STATUS = "https://archlinux.org/mirrors/status/json/"
ARCH_TIERS = ("https://archlinux.org/mirrors/status/tier/%d/json/", (1, 2))
DEBIAN_MASTERLIST = "https://mirror-master.debian.org/status/Mirrors.masterlist"
UBUNTU_LIST = "https://launchpad.net/ubuntu/+archivemirrors"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# minimal DNS client - nginx resolves upstreams through public resolvers, so
# the scan has to as well. getaddrinfo() would use the host's resolver and on a
# mirror-redirecting LAN that answers with the local cache.
# --------------------------------------------------------------------------
def dns_a(name: str, servers: tuple[str, ...] = DEFAULT_RESOLVERS,
          timeout: float = 3.0) -> list[str]:
    tid = random.randrange(0x10000)
    pkt = struct.pack("!HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    for label in name.split("."):
        pkt += bytes([len(label)]) + label.encode()
    pkt += b"\x00" + struct.pack("!HH", 1, 1)
    for server in servers:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        try:
            s.sendto(pkt, (server, 53))
            data, _ = s.recvfrom(4096)
        except OSError:
            continue
        finally:
            s.close()
        if len(data) < 12 or data[:2] != pkt[:2]:
            continue
        ancount = struct.unpack("!H", data[6:8])[0]
        i, out = 12, []
        try:
            while data[i]:
                i += data[i] + 1
            i += 5
            for _ in range(ancount):
                if data[i] & 0xC0 == 0xC0:
                    i += 2
                else:
                    while data[i]:
                        i += data[i] + 1
                    i += 1
                rtype, _, _, rdlen = struct.unpack("!HHIH", data[i:i + 10])
                i += 10
                if rtype == 1 and rdlen == 4:
                    out.append(socket.inet_ntoa(data[i:i + 4]))
                i += rdlen
        except (IndexError, struct.error):
            continue
        if out:
            return out
    return []


def is_private(ip: str | None) -> bool:
    if not ip:
        return False
    a, b = (ip.split(".") + ["0", "0"])[:2]
    return a in ("10", "127") or (a == "172" and 16 <= int(b) <= 31) \
        or (a == "192" and b == "168") or a == "169"


# --------------------------------------------------------------------------
# HTTP with an optional DNS pin: connect to `pin` (an IP we resolved through
# public DNS) while sending the real Host header, exactly like nginx does.
# --------------------------------------------------------------------------
def http_get(url: str, *, timeout: float, headers: dict | None = None,
             limit: int | None = None, pin: str | None = None,
             sink=None) -> tuple[int, bytes, dict, float]:
    u = urlparse(url)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    hdrs = {"User-Agent": UA, "Accept-Encoding": "identity", "Host": u.hostname}
    if headers:
        hdrs.update(headers)
    t0 = time.monotonic()
    try:
        if u.scheme == "https":
            conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout)
        else:
            conn = http.client.HTTPConnection(pin or u.hostname, u.port or 80,
                                              timeout=timeout)
        conn.request("GET", path, headers=hdrs)
        resp = conn.getresponse()
        if sink is not None:
            total, first = 0, None
            while total < (limit or 1 << 62):
                chunk = resp.read(262144)
                if not chunk:
                    break
                if first is None:
                    first = time.monotonic() - t0
                total += len(chunk)
                sink(chunk)
            conn.close()
            return resp.status, str(total).encode(), dict(resp.headers), \
                time.monotonic() - t0
        body = resp.read(limit) if limit else resp.read()
        conn.close()
        return resp.status, body, dict(resp.headers), time.monotonic() - t0
    except (http.client.HTTPException, urllib.error.URLError, socket.timeout,
            OSError, ValueError) as e:
        return 0, str(e).encode()[:200], {}, time.monotonic() - t0


def tcp_connect_ms(ip: str, port: int = 80, timeout: float = 3.0) -> float | None:
    t0 = time.monotonic()
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return (time.monotonic() - t0) * 1000.0
    except OSError:
        return None


@dataclass
class Mirror:
    distro: str
    base: str                       # e.g. http://deb.debian.org/debian (no trailing /)
    host: str
    ips: list[str] = field(default_factory=list)
    delay: int | None = None        # freshness: arch `delay`, apt seconds behind origin
    rtt_ms: float | None = None
    speed_mbs: float | None = None
    ttfb_ms: float | None = None
    ok: bool = False
    why: str = ""
    blob: str = ""
    tier: int = 3                    # 1 = authoritative (tier-1 / origin / GeoDNS)
    notes: list[str] = field(default_factory=list)

    @property
    def pin(self) -> str | None:
        return self.ips[0] if self.ips else None


# --------------------------------------------------------------------------
# 1. discovery
# --------------------------------------------------------------------------
def mirror_of(distro: str, base: str, **kw) -> Mirror:
    return Mirror(distro, base.rstrip("/"), urlparse(base).hostname, **kw)


def arch_tiers() -> dict[str, int]:
    """Arch publishes an explicit tier list; tier 1 are the boxes that the
    archive's own builders push to, so they are the least likely to lag."""
    tpl, which = ARCH_TIERS
    out: dict[str, int] = {}
    for tier in which:
        status, body, _, _ = http_get(tpl % tier, timeout=20)
        if status != 200:
            continue
        for m in json.loads(body).get("urls", []):
            out[m["url"].rstrip("/")] = tier
    return out


def discover_arch(max_delay: int, want_root: str = "/archlinux") -> list[Mirror]:
    status, body, _, _ = http_get(ARCH_STATUS, timeout=30)
    if status != 200:
        raise SystemExit(f"arch status json: HTTP {status}")
    data = json.loads(body)
    tiers = arch_tiers()
    log(f"[arch]   tier map: {sum(1 for v in tiers.values() if v == 1)} tier-1, "
        f"{sum(1 for v in tiers.values() if v == 2)} tier-2")
    out = []
    for m in data["urls"]:
        if m.get("protocol") != "http" or not m.get("active"):
            continue
        if (m.get("completion_pct") or 0) < 1.0:
            continue
        # 93 of 1190 entries have a null delay/last_sync; they must not sort as
        # "0 seconds behind".
        if m.get("delay") is None or m["delay"] > max_delay:
            continue
        # The cache publishes one prefix per distro, so a peer must serve the
        # archive at that same path root or every request 404s.
        if urlparse(m["url"]).path.rstrip("/") != want_root:
            continue
        mm = mirror_of("arch", m["url"], delay=m["delay"])
        mm.tier = tiers.get(m["url"].rstrip("/"), 3)
        out.append(mm)
    roots: dict[str, int] = {}
    for m in data["urls"]:
        if m.get("protocol") == "http" and m.get("active"):
            p = urlparse(m["url"]).path.rstrip("/") or "/"
            roots[p] = roots.get(p, 0) + 1
    log(f"[arch]   {len(out)} usable http mirrors at {want_root} "
        f"(synced <={max_delay}s, of {len(data['urls'])} published); "
        f"roots seen: {dict(sorted(roots.items(), key=lambda kv: -kv[1])[:4])}")
    return out


def discover_debian() -> list[Mirror]:
    """Debian: official masterlist (deb822-ish blocks), not the HTML page.

    There is no JSON status feed for Debian (the /status/*.json endpoints 404).
    Mirrors.masterlist is authoritative and states, per site, the HTTP path
    that serves the archive (`Archive-http: /debian/`). Sites whose path is
    /debian-ports/ or a CD image path would 404 under our /debian/ prefix.
    """
    status, body, _, _ = http_get(DEBIAN_MASTERLIST, timeout=40)
    if status != 200:
        raise SystemExit(f"{DEBIAN_MASTERLIST}: HTTP {status}")
    out, seen = [], set()
    for block in re.split(rb"\n\s*\n", body):
        site = arch_http = mtype = None
        for line in block.split(b"\n"):
            if line.startswith(b"Site: "):
                site = line[6:].decode().strip()
            elif line.startswith(b"Archive-http: "):
                arch_http = line[14:].decode().strip()
            elif line.startswith(b"Type: "):
                mtype = line[6:].decode().strip()
        if not site or not arch_http or site in seen:
            continue
        if arch_http.startswith("http"):
            base = arch_http
        elif arch_http.rstrip("/") == "/debian":
            base = f"http://{site}/debian"
        else:
            continue
        seen.add(site)
        mm = mirror_of("debian", base)
        # Only a handful of sites carry Type: Origin/GeoDNS/RoundRobinDNS -
        # those are the ones Debian itself operates or fronts with a CDN, and
        # the only "tier" signal the masterlist offers for HTTP.
        if mtype in ("Origin", "GeoDNS", "RoundRobinDNS"):
            mm.tier = 1
            mm.notes.append(f"{mtype.lower()}")
        out.append(mm)
    log(f"[debian] {len(out)} archive mirrors from the official masterlist")
    return out


def discover_ubuntu() -> list[Mirror]:
    """Ubuntu: Launchpad's archive mirror list.

    mirrors.ubuntu.com/mirrors.txt is a trap: it returns a single line
    (archive.ubuntu.com) and its HTTPS endpoint was unreachable in testing.
    Launchpad lists every mirror with protocol and up-to-date status.
    """
    status, body, _, _ = http_get(UBUNTU_LIST, timeout=40)
    if status != 200:
        raise SystemExit(f"{UBUNTU_LIST}: HTTP {status}")
    html = body.decode("utf-8", "replace")
    bases = {m.group(0).rstrip("/") for m in
             re.finditer(r"http://[a-zA-Z0-9._-]+/ubuntu/?", html)}
    out = [mirror_of("ubuntu", b) for b in sorted(bases)]
    for m in out:
        if m.base in {b.rstrip("/") for b in DISTROS["ubuntu"]["bases"]}:
            m.tier = 1
    log(f"[ubuntu] {len(out)} candidates from Launchpad's archive mirror list")
    return out


def discover(distro: str, max_delay: int) -> list[Mirror]:
    m = {"arch": lambda: discover_arch(max_delay),
         "debian": discover_debian,
         "ubuntu": discover_ubuntu}[distro]()
    known = {x.base for x in m}
    for b in DISTROS[distro]["bases"]:
        if b.rstrip("/") not in known:
            m.append(mirror_of(distro, b))
    return m


def resolve_all(mirrors: list[Mirror], resolvers: tuple[str, ...], jobs: int) -> None:
    def one(m: Mirror) -> None:
        m.ips = dns_a(m.host, resolvers)
        if is_private(m.ips[0] if m.ips else None):
            m.notes.append(f"private IP {m.ips[0]}")
    with futures.ThreadPoolExecutor(jobs) as ex:
        list(ex.map(one, mirrors))


# --------------------------------------------------------------------------
# 2. freshness / correctness
# --------------------------------------------------------------------------
def parse_release(body: bytes) -> tuple[datetime | None, datetime | None]:
    def field(name: bytes) -> datetime | None:
        m = re.search(rb"^" + name + rb":\s*(.+)$", body, re.MULTILINE)
        if not m:
            return None
        try:
            from email.utils import parsedate_to_datetime
            return parsedate_to_datetime(m.group(1).decode().strip())
        except (TypeError, ValueError):
            return None
    return field(b"Date"), field(b"Valid-Until")


def probe(mirror: Mirror, suite: str, ref_date: datetime | None, ref_hash: str,
          frozen: bool, max_stale: int, timeout: float) -> Mirror:
    spec = DISTROS[mirror.distro]
    url = mirror.base + spec["index"].format(suite=suite)
    # Send the group's Host header, not the peer's own name: this is what
    # nginx will do, and it is what exposes peers that route on Host.
    status, body, _, _ = http_get(url, timeout=timeout, limit=4 << 20,
                                  pin=mirror.pin,
                                  headers={"Host": spec["host_header"]})
    if status != 200 or not body:
        if status in (403, 409):
            mirror.why = f"routes on Host, rejects {spec['host_header']} ({status})"
        else:
            mirror.why = f"index HTTP {status}" if status else "index unreachable"
        mirror.ok = False
        return mirror
    if mirror.distro == "arch":
        if not body.startswith(b"\x1f\x8b"):
            mirror.ok, mirror.why = False, "core.db is not gzip"
            return mirror
        mirror.ok, mirror.why = True, "ok"
        return mirror

    date, valid_until = parse_release(body)
    if valid_until and valid_until < datetime.now(timezone.utc):
        mirror.ok, mirror.why = False, f"Valid-Until expired {valid_until:%Y-%m-%d}"
        return mirror
    if frozen:
        # Frozen suite (stable, noble): Release changes only at a point release
        # and its Date is stuck at that moment, so the content hash is the only
        # honest freshness signal. A different hash = a different point release.
        if ref_hash and hashlib.sha256(body).hexdigest() != ref_hash:
            mirror.ok, mirror.why = False, "Release differs from origin (stale)"
            return mirror
        mirror.delay = 0
    elif date and ref_date:
        # Rolling suite (-updates, -security, testing): Release is rewritten
        # often, so Date is meaningful and hash equality would unfairly drop a
        # mirror that is minutes behind.
        behind = int((ref_date - date).total_seconds())
        mirror.delay = max(0, behind)
        mirror.notes.append(f"behind origin {mirror.delay}s")
        if behind > max_stale:
            mirror.ok, mirror.why = False, f"{behind // 3600}h behind origin"
            return mirror
    mirror.ok, mirror.why = True, "ok"
    return mirror


def pick_finalists(reachable: list[Mirror], n: int) -> list[Mirror]:
    """Half the nearest peers plus an even sample of the rest.

    Picking only the lowest-RTT peers bakes in exactly the wrong assumption:
    anycast CDNs (Fastly, Cloudflare) sit far away in RTT terms and still beat
    a same-city CUBIC mirror on throughput.
    """
    ranked = sorted(reachable, key=lambda m: m.rtt_ms or 9e9)
    if len(ranked) <= n:
        return ranked
    half = n // 2
    near, rest = ranked[:half], ranked[half:]
    step = max(1, len(rest) // (n - half))
    return near + rest[::step][: n - half]


# --------------------------------------------------------------------------
# 4. throughput
# --------------------------------------------------------------------------
def speed_test(mirror: Mirror, suite: str, budget: int, timeout: float,
               rounds: int) -> Mirror:
    spec = DISTROS[mirror.distro]
    best, best_ttfb = 0.0, None
    hh = {"Host": spec["host_header"]}
    for path in spec["blob"]:
        url = mirror.base + path.format(suite=suite)
        status, _, _, _ = http_get(url, timeout=timeout, pin=mirror.pin,
                                   headers={**hh, "Range": "bytes=0-0"}, limit=1)
        if status not in (200, 206):
            continue
        for _ in range(rounds):
            t0 = time.monotonic()
            first = [None]

            def sink(chunk: bytes) -> None:
                if first[0] is None:
                    first[0] = time.monotonic() - t0

            status, n, _, dt = http_get(
                url, timeout=timeout, pin=mirror.pin,
                headers={**hh, "Range": f"bytes=0-{budget - 1}"},
                limit=budget, sink=sink)
            # On a transport error http_get returns the exception text here.
            try:
                n = int(n or 0)
            except ValueError:
                continue
            if status not in (200, 206) or n < 512 * 1024 or dt <= 0:
                continue
            rate = n / dt / 1e6  # MB/s (decimal, the unit mirrors quote)
            if rate > best:
                best, best_ttfb = rate, first[0]
                mirror.blob = path.format(suite=suite)
        if best:
            break
    if not best:
        mirror.ok, mirror.why = False, "throughput test failed"
        return mirror
    mirror.speed_mbs = best
    mirror.ttfb_ms = None if best_ttfb is None else best_ttfb * 1000.0
    return mirror


# --------------------------------------------------------------------------
# ranking + emit
# --------------------------------------------------------------------------
def score_for(m: Mirror, tier_bonus: float) -> float:
    """Throughput with a nudge for authoritative sources.

    Freshness is already a hard gate, so tier here buys the second half of
    "the mirror is current": tier-1/origin boxes are the ones everyone else
    syncs FROM, so they are never mid-sync. The bonus is multiplicative and
    small (default 10%) so a genuinely faster leaf still wins.
    """
    sp = m.speed_mbs or 0.0
    return sp * (1.0 + (tier_bonus / 100.0 if m.tier == 1 else 0.0))


def weight_for(speed: float, top: float) -> int:
    """1..5, relative to the fastest peer in the same pool."""
    if top <= 0:
        return 1
    return max(1, min(5, round(5 * speed / top)))


def emit(pools: dict[str, list[Mirror]], path: str, args, meta: dict) -> str:
    lines = [
        "# GENERATED by tools/scan-mirrors.py - do not hand-edit; re-run instead.",
        f"# {meta['when']}  candidates={meta['candidates']} kept={meta['kept']}"
        f"  budget={args.bytes // (1 << 20)}MiB x{args.rounds} rounds"
        f"  resolvers={','.join(args.resolvers)}",
        "#",
        "# Ranked: freshness gate first (a stale mirror 404s new packages),",
        "# then measured sustained throughput. weight is relative within the",
        "# pool (5 = fastest measured peer). `down` parks a peer without",
        "# deleting it. A 404 is not a peer failure, so an out-of-date mirror",
        "# costs one retry via proxy_next_upstream, not a blacklist.",
        "",
    ]
    for name, peers in pools.items():
        if not peers:
            continue
        hh = DISTROS[name.replace("_mirrors", "")]["host_header"]
        top = max(score_for(p, args.tier_bonus) for p in peers)
        lines += [f"# Host header sent for this group: {hh} (see research/PLAN.md 7.3)",
                  f"upstream {name} {{",
                  f"    zone {name} 64k;",
                  "    random two least_conn;",
                  ""]
        for p in peers:
            w = weight_for(score_for(p, args.tier_bonus), top)
            bits = [f"{p.speed_mbs:5.1f} MB/s", f"rtt {p.rtt_ms:.0f}ms",
                    f"tier {p.tier}"]
            if p.delay is not None:
                bits.append(f"sync {p.delay}s ago" if p.distro == "arch"
                            else f"behind origin {p.delay}s")
            for n in p.notes:
                if n.startswith("private"):
                    bits.append(n.upper())
            lines.append(
                f"    server {p.host}:80 weight={w} resolve max_fails=2 fail_timeout=60s;"
                f"  # {', '.join(bits)}")
        lines += ["", "    keepalive 8;", "}", ""]
    # The Host header has to travel with the pool: nginx cannot vary it per
    # selected peer, and it is the value the probe above tested with.
    if pools:
        lines += ["# Which Host header each pool is served under. Generated",
                  "# together with the pools so the two cannot drift apart.",
                  "map $target $target_host {",
                  "    default           $target;"]
        for name, peers in pools.items():
            if peers:
                lines.append(f"    {name:<17} "
                             f"{DISTROS[name.replace('_mirrors', '')]['host_header']};")
        lines += ["}", ""]
    text = "\n".join(lines)
    if not args.dry_run:
        with open(path, "w") as f:
            f.write(text)
    return text


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--distros", default="debian,ubuntu,arch")
    ap.add_argument("--suite", default="", help="apt suite (default stable / noble)")
    ap.add_argument("--top", type=int, default=6, help="peers kept per pool")
    ap.add_argument("--speed-candidates", type=int, default=16,
                    help="how many candidate mirrors get the download test")
    ap.add_argument("--max-delay", type=int, default=3600,
                    help="arch: drop mirrors not synced in this many seconds")
    ap.add_argument("--max-stale", type=int, default=21600,
                    help="apt rolling suites: drop mirrors this many seconds behind")
    ap.add_argument("--tier-bonus", type=float, default=10.0,
                    help="percent throughput bonus for tier-1/origin mirrors "
                         "(Arch tier 1, Debian GeoDNS/Origin, the canonical "
                         "Ubuntu archive); keeps a much faster leaf able to win")
    ap.add_argument("--bytes", type=int, default=16 << 20, help="Range budget per round")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--timeout", type=float, default=8.0)
    ap.add_argument("--jobs", type=int, default=32)
    ap.add_argument("--resolvers", default=",".join(DEFAULT_RESOLVERS),
                    help="public resolvers used to look mirrors up (must match "
                         "nginx/resolver.conf; never the LAN resolver)")
    ap.add_argument("--host-header", action="append", default=[],
                    metavar="DISTRO=NAME",
                    help="override the Host header sent for a pool, e.g. "
                         "arch=us.mirrors.cicku.me. A Cloudflare-fronted mirror "
                         "name lets anycast-fronted peers back in (they route by "
                         "Host) at the price of depending on that name existing; "
                         "see research/PLAN.md 7.3")
    ap.add_argument("--only", default="", help="comma separated host substrings")
    ap.add_argument("--out", default="nginx/mirrors.conf")
    ap.add_argument("--report", default=".scratch/mirror-report.json")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    args.resolvers = tuple(r.strip() for r in args.resolvers.split(",") if r.strip())

    for item in args.host_header:
        d, _, name = item.partition("=")
        if d in DISTROS and name:
            DISTROS[d]["host_header"] = name.strip()

    only = [s.strip() for s in args.only.split(",") if s.strip()]
    pools: dict[str, list[Mirror]] = {}
    candidates = kept = 0
    report: dict[str, list[dict]] = {}

    for distro in (d.strip() for d in args.distros.split(",") if d.strip()):
        suite = args.suite or DISTROS[distro]["default_suite"]
        mirrors = discover(distro, args.max_delay)
        if only:
            mirrors = [m for m in mirrors if any(o in m.host for o in only)]
        candidates += len(mirrors)
        resolve_all(mirrors, args.resolvers, args.jobs)
        unresolved = [m for m in mirrors if not m.ips]
        if unresolved:
            log(f"[{distro}] {len(unresolved)} hosts do not resolve via public DNS")
        mirrors = [m for m in mirrors if m.ips]

        ref = DISTROS[distro]["bases"][0]
        ref_date, ref_hash, frozen = None, "", False
        if distro != "arch":
            ref_url = ref + DISTROS[distro]["index"].format(suite=suite)
            st, body, _, _ = http_get(ref_url, timeout=30, limit=4 << 20)
            if st != 200 or not body:
                # The origin itself can be slow or blocking; retry against an
                # address resolved through public DNS before giving up, because
                # without a reference file there is nothing to compare against.
                ips = dns_a(urlparse(ref_url).hostname or "", args.resolvers)
                if ips:
                    st, body, _, _ = http_get(ref_url, timeout=30, limit=4 << 20,
                                              pin=ips[0])
            if st == 200 and body:
                ref_hash = hashlib.sha256(body).hexdigest()
                ref_date, _ = parse_release(body)
            frozen = bool(ref_date and (datetime.now(timezone.utc) - ref_date).days >= 7)
            if ref_date is None:
                log(f"[{distro}] WARNING: no reference Release for {suite} "
                    f"(HTTP {st}); peers are checked for a 200 index only, "
                    f"freshness is NOT verified this run")
            else:
                log(f"[{distro}] origin Release dated {ref_date:%Y-%m-%d} "
                    f"sha256={ref_hash[:12]}… "
                    f"({'frozen -> hash gate' if frozen else 'rolling -> Date gate'})")

        with futures.ThreadPoolExecutor(args.jobs) as ex:
            probed = list(ex.map(
                lambda m: probe(m, suite, ref_date, ref_hash, frozen,
                                args.max_stale, args.timeout), mirrors))
        good = [m for m in probed if m.ok]
        log(f"[{distro}] {len(good)}/{len(mirrors)} serve a current {suite} index")

        with futures.ThreadPoolExecutor(args.jobs) as ex:
            list(ex.map(lambda m: setattr(m, "rtt_ms", tcp_connect_ms(m.pin)), good))
        reachable = [m for m in good if m.rtt_ms is not None]
        finalists = pick_finalists(reachable, args.speed_candidates)
        log(f"[{distro}] speed test on {len(finalists)} candidates "
            f"(half nearest, half spread: no RTT bias)")

        with futures.ThreadPoolExecutor(min(8, args.jobs)) as ex:
            timed = list(ex.map(
                lambda m: speed_test(m, suite, args.bytes, args.timeout, args.rounds),
                finalists))
        ranked = sorted((m for m in timed if m.ok and m.speed_mbs),
                        key=lambda m: score_for(m, args.tier_bonus), reverse=True)
        pools[f"{distro}_mirrors"] = ranked[: args.top]
        kept += len(pools[f"{distro}_mirrors"])
        report[distro] = [{
            "base": m.base, "ip": m.ips[0] if m.ips else None,
            "speed_mbs": round(m.speed_mbs or 0, 2),
            "rtt_ms": None if m.rtt_ms is None else round(m.rtt_ms),
            "ttfb_ms": None if m.ttfb_ms is None else round(m.ttfb_ms),
            "delay": m.delay, "blob": m.blob, "tier": m.tier, "notes": m.notes,
        } for m in ranked]
        for m in ranked[: args.top]:
            log(f"  {m.speed_mbs:6.1f} MB/s  rtt {m.rtt_ms:6.0f}ms  tier {m.tier}  {m.base}")

    meta = {"when": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "candidates": candidates, "kept": kept}
    text = emit(pools, args.out, args, meta)
    if args.dry_run:
        print(text)
    else:
        with open(args.report, "w") as f:
            json.dump({"meta": meta, "resolvers": list(args.resolvers),
                       "pools": report}, f, indent=1)
        log(f"wrote {args.out} ({kept} peers) and {args.report}")
        log("apply: docker exec pkgcache nginx -t && docker exec pkgcache nginx -s reload")
    return 0


if __name__ == "__main__":
    sys.exit(main())
