# 04 — Mirror Selection & Ranking Tooling: Measurement, Freshness Oracles, and the BBR Question

Research target: we run a transparent-caching nginx in front of Debian, Ubuntu and Arch package repositories (plain HTTP only). We want a scriptable
job that discovers upstream mirrors, ranks them on *freshness → measured speed → measured proximity*, and emits nginx `upstream` blocks. This report
documents what existing tools actually measure, which machine-readable freshness oracles exist per distro, and what can and cannot be concluded about
a remote server's TCP congestion control.

All live probes below were executed on **2026-10-26** from a single vantage point; they are labelled "observed". Anything I could not confirm from a
primary source is marked **UNVERIFIED**.

---

## 1. A. Tools that discover and rank distro mirrors

### 1.1 `rate-mirrors` (Rust, Arch-first, also `stdin` mode)

Upstream: [github.com/westandskif/rate-mirrors](https://github.com/westandskif/rate-mirrors) — note the correct owner is **`westandskif`, not
`westandskenn`** (the URL in the brief 404s). Flags below were read from the installed binary (`rate-mirrors --version` → `rate-mirrors config
0.34.0`, `rate-mirrors --help`) and from the source tree.

- **Protocol tested: HTTP(S) GET, never ICMP.** `src/speed_test.rs` does `client.get(mirror.url_to_test).timeout(per_mirror_timeout).send()`, then
  consumes `response.chunk()` until a stopping rule fires. There is no ping, no TCP handshake timing, no Range header.
- **What the number means:** `connection_time` = wall time until response *headers* arrive; the rate is `bytes_downloaded / elapsed` where `elapsed`
  starts **after** headers, i.e. TCP slow start is *included* in the reported speed and connect/TLS time is *excluded*.
- **Stopping rule (this is the "tolerance"):** per-chunk speed is computed as `chunk_size / duration_since(prev_ts)`; testing stops early once the
  relative standard deviation of the last `--eps-checks` chunks is `<= --eps`, but only after `--min-bytes-per-mirror` bytes and `--min-per-mirror`
  duration; a hard cap applies at `--max-per-mirror`.
- Defaults that matter: `--eps 0.0625`, `--eps-checks 40`, `--min-bytes-per-mirror 70000`, `--min-per-mirror 300` ms, `--max-per-mirror 1000` ms,
  `--per-mirror-timeout 8000` ms, `--concurrency 16`.
- **The flags in the brief that do not exist:** there is no `--sort-by` and no `--tolerance`. Sorting is `--sort-mirrors-by` on the *subcommand*, one
  of `score_asc | score_desc | delay_asc | delay_desc | random` (default `score_asc`). `--max-delay` exists **only on the `arch` subcommand**, is in
  **seconds**, default `86400`, and means "max acceptable delay since the last sync".
- `--protocol` is repeatable and filters the candidate list by URL scheme (`http`, `https`, `rsync`); `--entry-country` / `--include-countries` /
  `--exclude-countries` take 2-letter ISO codes (`ZZ` = unknown) and drive a neighbour-expansion heuristic (`--country-neighbors-per-country`,
  `--country-test-mirrors-per-country`, `--top-mirrors-number-to-retest`, `--max-jumps`). Country is used to *choose what to test*, not as the score.
- **Freshness-aware: yes, for Arch.** It fetches `https://archlinux.org/mirrors/status/json/` (overridable with `--mirror-source`), and with
  `--fetch-first-tier-only` it uses `https://archlinux.org/mirrors/status/tier/1/json/`. Its filter is essentially
  `completion_pct >= --completion` **and** `delay <= --max-delay`; mirrors with `null` delay or completion are **dropped**, which is desirable for us.
- **Output:** `#`-comment progress lines plus `Server = https://host/$repo/os/$arch` lines, i.e. a pacman mirrorlist on stdout (`--save`,
  `--max-mirrors-to-output`, `--disable-comments`). The `stdin` subcommand can rewrite an arbitrary list with `--path-to-return`, `--output-prefix`,
  `--separator`.
- Plain HTTP: supported, because the scheme comes from the upstream mirror list (`--protocol http`).

### 1.2 `reflector` (Arch, Python)

Man page: [man.archlinux.org/man/reflector.1.en](https://man.archlinux.org/man/reflector.1.en); source tarball
[xyne.dev/projects/reflector](https://xyne.dev/projects/reflector/); usage notes
[wiki.archlinux.org/title/Reflector](https://wiki.archlinux.org/title/Reflector). (The GitHub org link in the brief is dead; the man page itself
defers to `--help`, so flags below come from `Reflector.py` in the upstream tarball.)

- **Freshness-aware: yes.** It downloads the archweb status JSON ([archlinux.org/mirrors/status/json/](https://archlinux.org/mirrors/status/json/))
  and caches it locally (`--cache-timeout`, default 300 s).
- `filter_mirrors()` drops entries with no `last_sync`, then applies `completion_pct >= --completion-percent` (metavar `[0-100]`, default **100.0**),
  country, protocol, `--include`/ `--exclude` regexes, `--age` (hours since `last_sync`) and `--delay`.
- **`--delay` is in HOURS, not seconds** (the value is multiplied by 3600 before comparison) — an easy way to build a wrong upstream list.
- `--sort` accepts `age | rate | country | score | delay`, where `age` is "last server synchronization" and `score`/`delay` are the archweb fields;
  `--latest N` and `--score N` take the N best on those axes; `--number N`, `--fastest N` truncate the result.
- **How it measures rate:** `rate_http()` fetches the whole `extra/os/x86_64/extra.db` with `urllib.request` (`len(handle.read())`) and divides size
  by elapsed time; `--download-timeout` and `--connection-timeout` default to 5 s each. So the rate is a *full-file* download capped by a timeout —
  not a fixed byte budget, and not resumable/range-based. `rsync://` mirrors are rated by shelling out to `rsync`.
- **Output:** a mirrorlist (`Server = ...`) via `--save`, or a filtered list via `--url`; `--info` prints the raw metadata instead.
- Plain HTTP: supported through `--protocol http` (repeatable / comma-separated), though most Arch mirrors advertise https first.

### 1.3 Debian / Ubuntu side

- **`netselect`** ([man page](https://manpages.debian.org/unstable/netselect/netselect.1.en.html)): measures approximate **ping time, hop count, and
  the percentage of probes answered**, then a blended "score" (lower is better). Default probes are **UDP like traceroute**; `-I` switches to ICMP;
  `-t` sets packets (default 10). It is protocol-agnostic and therefore **never proves the mirror serves HTTP**: the man page's own example shows
  hosts answering ping while the HTTP service is unusable, and warns that `9999 ms` means probes are filtered.
- **`netselect-apt`** ([man page](https://manpages.debian.org/unstable/netselect-apt/netselect-apt.1.en.html)): downloads
  `http://www.debian.org/mirror/mirrors_full`, runs `netselect`, then validates candidates with `curl` over HTTP or FTP (`-f`). Its LIMITATIONS
  section is the direct answer to the freshness question: *"netselect-apt will not check if the mirror it suggests as the 'fastest' mirror is either
  valid or up-to-date."* It also needs root/setuid for the ICMP probes and cannot work behind a proxy. The page is dated **March 6, 2008**.
- **`apt-spy`: dead.** `https://packages.debian.org/sid/apt-spy` returns "No such package" (observed); do not build on it. (`apt-fast` is also absent
  from the Debian archive, observed.)
- **`debmirror`** ([package](https://packages.debian.org/sid/debmirror)) is a *replicator*, not a selector; `devscripts` and `debian-goodies` contain
  no mirror-ranking tool.
- **Ubuntu:** `https://mirrors.ubuntu.com/mirrors.txt` returns **one line** (`http://archive.ubuntu.com/ubuntu/`, 34 bytes observed) — it is *not* a
  global mirror list. The per-country files (`/US.txt`, `/DE.txt`) do work and list mirror bases over http/https. The real directory is Launchpad:
  [launchpad.net/ubuntu/+archivemirrors](https://launchpad.net/ubuntu/+archivemirrors) (592 mirrors observed, each with country, protocols, link
  capacity and an "Up to date" status).
- I found **no** maintained `ubuntu-mirror-speedtest` / `mirror-speedtest` tool in the Debian/Ubuntu archives — **UNVERIFIED** that any such package
  exists.

### 1.4 Generic building blocks

- **MirrorBrain** ([mirrorbrain.org](https://mirrorbrain.org/)) is a *server-side* redirector: it owns the mirror list, hashes the URL to pick a
  mirror, and can emit Metalinks. Useful as a design reference, useless as a client-side ranker, and it requires controlling the download server.
- **Metalink** ([RFC 5854](https://datatracker.ietf.org/doc/html/rfc5854)) is the standard format for advertising multiple mirror URLs plus checksums
  for one object — i.e. the data model our generated nginx upstream list is a poor man's version of.
- **`apt-fast`** ([github.com/ilikenwf/apt-fast](https://github.com/ilikenwf/apt-fast)) wraps apt and `aria2c` to fetch one file over several
  connections *from a single mirror*. It is not a selector, but its existence is a hint: parallel connections per mirror change measured throughput,
  which matters when we calibrate rankings.
- **`curl -w`** is the most portable measurement primitive we have. Documented variables
  ([curl.se/docs/manpage.html](https://curl.se/docs/manpage.html)) include `time_namelookup`, `time_connect`, `time_appconnect`, `time_pretransfer`,
  `time_starttransfer` (first byte, includes server think time), `time_redirect`, `time_total`, `size_download` (body only, excludes headers),
  `size_header`, `size_delivered`, `speed_download` (average bytes/s *for the complete download*), `num_connects`, `response_code`. Because
  `speed_download` averages over the whole transfer, including slow start, it systematically under-reports short transfers on high-BDP paths.
- **Smokeping** ([oss.oetiker.ch/smokeping](https://oss.oetiker.ch/smokeping/)) is the reference implementation for *longitudinal* latency/loss
  measurement (RRD-backed); it measures the path, not the mirror's HTTP throughput.

### 1.5 Comparison

| tool | distro focus | measures | protocol | output | freshness-aware? |
|---|---|---|---|---|---|
| `rate-mirrors` | Arch (+`stdin` for any list) | HTTP GET throughput (incl. slow start), connect time | http, https, rsync | pacman `Server =` lines / rewritten list | **yes** — archweb `delay`, `completion_pct`, `/tier/1/` |
| `reflector` | Arch | full-file download rate of `extra.db`, or nothing (`--latest`/`--score`) | http, https, rsync | mirrorlist / filtered list | **yes** — archweb `last_sync` age, `delay`, `score` |
| `netselect` | any | ICMP/UDP ping, hops, loss → blended score | ICMP / UDP | scored host list | no (no HTTP at all) |
| `netselect-apt` | Debian | netselect score + curl reachability | ICMP + http/ftp | `sources.list` | **no** (documented) |
| `apt-spy` | Debian | — | — | — | **removed from Debian** |
| `debmirror` | Debian | nothing (replicates) | http/ftp/rsync | mirror tree | no |
| MirrorBrain | server-side | hash-based redirect | http | redirect + Metalink | n/a |
| `apt-fast`/`aria2c` | any | multi-connection download | http/https | files | no |
| `curl -w` | any | per-phase timing, bytes, average speed | http/https | text/JSON | no |
| Smokeping | any | latency, loss over time | ICMP/TCP/HTTP probes | RRD graphs | no |

Plain-HTTP verdict: `netselect`, `debmirror`, `apt-fast`/`aria2c`, `curl` and MirrorBrain are HTTP-version-agnostic; `rate-mirrors` and `reflector`
follow whatever scheme the upstream list advertises, so both can be constrained to plain HTTP; `netselect-apt` defaults to HTTP but its "fastest"
answer is ping-derived and explicitly not freshness-checked.

---

## 2. B. Freshness / tier oracles (the highest-value part)

### 2.1 Arch — the good case

Endpoint: [archlinux.org/mirrors/status/json/](https://archlinux.org/mirrors/status/json/), documented in prose at
[archlinux.org/mirrors/status/](https://archlinux.org/mirrors/status/) and
[wiki.archlinux.org/title/Mirrors](https://wiki.archlinux.org/title/Mirrors).

- **Top-level keys (observed, `version` 3):** `cutoff`, `last_check`, `num_checks`, `check_frequency`, `urls`, `version`. Observed values: `cutoff = 86400`, `num_checks = 92`, `check_frequency = 905`, `last_check = 2026-10-26T16:55:34.495Z`, **1190 entries** in `urls`.
- **Per-mirror keys (observed):** `url`, `protocol`, `last_sync`, `completion_pct`, `delay`, `duration_avg`, `duration_stddev`, `score`, `active`,
  `country`, `country_code`, `isos`, `ipv4`, `ipv6`, `details`. **There is no `last_sync_check` field** — the brief's field list is wrong for v3; the
  check timestamp is the top-level `last_check`.
- Units are seconds for durations and delay, UTC for timestamps, exactly as the status page states.
- **The thresholds are documented on the status page.** "μ Delay … the mean value of last check − last sync … **any value under one hour should be
  viewed as ideal**"; `completion_pct` below 100% means the mirror "may be unreliable"; and the score is **`(hours delay + average duration + standard deviation) / completion percentage`**, lower better. `cutoff` (86400) is the age bound: `last_sync` older than 24 h puts a mirror in "Out of Sync".
- **Tier endpoints exist and are the cheapest tier oracle we have:** `https://archlinux.org/mirrors/status/tier/1/json/` (270 URLs observed) and
  `.../tier/2/json/` (910 URLs), same schema. `rate-mirrors --fetch-first-tier-only` already uses the tier-1 URL.
- 93 of 1190 entries carried `null` for both `delay` and `last_sync` (observed) — these must be filtered out explicitly, because nulls compare wrongly
  in most languages.

### 2.2 Debian — no JSON, but a rich text/HTML status

- **There is no JSON oracle.** `mirror-master.debian.org/status/mirrors.json`, `.../status/JSON/Mirrors.json` and `www.debian.org/mirror/mirrors.json`
  all return **404** (observed), and the long-documented `.../status/HTML/Mirrors.html` is now **404** too.
- What exists is a directory of generated reports at [mirror-master.debian.org/status/](https://mirror-master.debian.org/status/):
  `Mirrors.masterlist` (85 KB, regenerated continuously), `mirror-status.html` (870 KB) with a companion `mirror-status.js`, `mirror-hierarchy.html`,
  and per-host pages under `mirror-info/`.
- **`Mirrors.masterlist`** ([direct link](https://mirror-master.debian.org/status/Mirrors.masterlist)) is RFC-822-style stanzas (`Site:`, `Country:`,
  `Type:`, `Archive-http:`, `Ports-http:`, `CDImage-http:`, `Sponsor:`, `Comment:`, `Location:`) — easy to parse with Python's stdlib and the best
  authoritative source for "which hosts are official and what do they serve". It also carries the CDN facts used in §3.4: `deb.debian.org` and
  `security.debian.org` are `Type: GeoDNS` with `Sponsor: Fastly` (and, for `deb.debian.org`, also `Sponsor: Amazon Cloudfront`).
- **`mirror-status.html`** is the de-facto Debian freshness report. Observed columns: `Site | mastertrace | archive version | last update | score |
  ~#/d | delay (μ/σ) | extra | traces | ftpsync | configured upstream | architectures` — i.e. per-mirror "last update" (e.g. `21 h`), archive version
  (e.g. `20180513`), a signed score (e.g. `-98.64`) and delay μ/σ. It is HTML with no published schema, so treat it as a scrape target with a tolerant
  parser, not as a contract.
- **Per-mirror freshness probe from the archive itself** (authoritative, works for any mirror): fetch `dists/<suite>/Release` (or `InRelease`) and
  read `Date:` and `Valid-Until:`. Debian's repository format documents both fields, plus `Acquire-By-Hash`, at
  [wiki.debian.org/DebianRepository/Format](https://wiki.debian.org/DebianRepository/Format). Observed 2026-10-26: `dists/stable/Release` has `Date:
  Sat, 12 Sep 2026 07:55:41 UTC` and **no `Valid-Until`**; `dists/stable-updates/Release` has `Valid-Until` exactly 7 days after `Date`;
  `debian-security/dists/trixie-security/Release` also carries a 7-day `Valid-Until` and `Acquire-By-Hash: yes`.
- **`Acquire-By-Hash: yes` matters for us twice.** First, apt can fetch index files by their SHA256 under `by-hash/SHA256/<hex>`, which makes a
  caching proxy safe against mid-sync races. Second, it gives a content-addressed freshness test (see §2.4). Caveat we verified the hard way: the
  by-hash path works for index files *listed inside* `Release`, **not** for `Release`/`InRelease` itself — fetching
  `dists/stable/by-hash/SHA256/<sha256 of InRelease>` returned 404, because a Release file does not list its own hash.

### 2.3 Ubuntu — Launchpad is the oracle

- `https://mirrors.ubuntu.com/mirrors.txt` returns a single line pointing at `archive.ubuntu.com` (observed, 34 bytes); per-country files such as
  `/US.txt` (85 lines) and `/DE.txt` are the useful ones. Over TLS the host did not answer at all in our probe, while plain HTTP worked — relevant
  because we are HTTP-only.
- [launchpad.net/ubuntu/+archivemirrors](https://launchpad.net/ubuntu/+archivemirrors) is the authoritative directory: 592 mirrors observed, each with
  country, protocol badges (`https`/`http`/`rsync`), link capacity in Gbps, and a status string ("Up to date"). It is HTML, not an API; a tolerant
  parser over the country sections is the practical approach. I found no documented JSON/API equivalent — **UNVERIFIED** whether Launchpad exposes
  one.
- Ubuntu does **not** appear to publish per-mirror sync *age* in machine-readable form on any of these endpoints; the "Up to date" flag is the only
  published signal.

### 2.4 Headers and a content-hash staleness test that works everywhere

- `Last-Modified` on `dists/<suite>/Release` ([RFC 9110 §8.8.2](https://www.rfc-editor.org/rfc/rfc9110.html#section-8.8.2)) is the *file's* mtime at
  that mirror; `Date` ([RFC 9110 §6.6.1](https://www.rfc-editor.org/rfc/rfc9110.html#section-6.6.1)) is when the response was generated. On a CDN the
  `Date` is the edge's clock, so a fresh-looking `Date` says nothing about mirror content; only `Last-Modified` (and the body) reflect sync state.
  Observed on `deb.debian.org`: `last-modified: Sat, 12 Sep 2026 07:59:10 GMT` with `x-cache: HIT, HIT` and `via: 1.1 varnish`.
- **Do not trust `Date:` inside `Release` alone.** Frozen suites never move it: `dists/noble/Release` observed `Date: Thu, 25 Apr 2024 15:10:33 UTC`
  (its release date), while `dists/noble-security/` moved the same day. `stable` on Debian only changes `Date` at point releases.
- **The robust test is a hash comparison against a reference archive** (e.g. `deb.debian.org`, `archive.ubuntu.com`, `geo.mirror.pkgbuild.com`). Fetch `dists/<suite>/InRelease` from the reference and from the candidate: identical
  bytes mean the mirror is at least as new as the reference; differing bytes mean you parse the candidate's `Date:`/`Valid-Until:` and compare — an older `Date` is a stale mirror, a newer one means the reference was the
  laggard. For stronger evidence on rolling suites, hash an index listed *inside* `Release` on both sides and compare (this is exactly the `by-hash` namespace, and it avoids downloading whole indexes when hashes differ).
  For Arch the equivalent probe is the mirror's `/lastsync` file ([wiki.archlinux.org/title/Mirrors](https://wiki.archlinux.org/title/Mirrors)), which is what archweb itself polls; `last_sync` in the JSON is the
  server-side result of that poll.

---

## 3. C. BBR vs CUBIC — what a client can and cannot see

### 3.1 The algorithms differ in *what they react to*

- **CUBIC** is loss-based. It grows `cwnd` as a cubic function of time since the last congestion event, remembers `W_max`, uses a multiplicative
  decrease of **0.7**, and keeps a Reno-friendly region so it does not lose to Reno on small-BDP paths ([RFC
  9438](https://www.rfc-editor.org/rfc/rfc9438.txt), which obsoletes RFC 8312 — Fastly's own docs still cite the older RFC). Consequence: it treats
  *any* loss as congestion, so on a path with random loss it collapses its rate and recovers slowly.
- **BBR** is model-based. It continuously estimates the bottleneck bandwidth and the round-trip propagation delay, derives a BDP, and paces packets to
  that estimate rather than waiting for loss ([google/bbr](https://github.com/google/bbr); BBR paper: *BBR: Congestion-Based Congestion Control*, [ACM
  Queue](https://queue.acm.org/detail.cfm?id=3022184)). Consequence: random loss does not reduce its rate, and it does not need to fill on-path
  buffers to find capacity.
- BBR is **not an IETF standard**; the current specification work is an Internet-Draft
  ([draft-ietf-ccwg-bbr](https://datatracker.ietf.org/doc/draft-ietf-ccwg-bbr/)), and Fastly's own documentation calls BBR "the newest of these
  algorithms and … still evolving and considered experimental" while describing CUBIC as "a pure loss-based algorithm" that "leads to consistent
  filling of on-path buffers" ([Fastly VCL
  reference](https://www.fastly.com/documentation/reference/vcl/variables/client-connection/client-socket-congestion-algorithm/)).

### 3.2 Can a client detect the sender's congestion control? **No — not reliably.**

- There is no field in IP, TCP, TLS or HTTP that announces the peer's CC algorithm. A client's `TCP_INFO` describes **its own** socket (its own
  `cwnd`, `snd_cwnd`, RTT, retransmits); it says nothing about the sender's controller. Any claim to "detect BBR" is inference from behaviour.
- What is *inferable*, with care, and only as behavioural evidence:
  - **Loss tolerance:** we cannot inject loss, but observe that throughput stays flat while retransmit counts rise — consistent with BBR, inconsistent with CUBIC.
  - **Throughput vs RTT:** loss-based CC throughput scales roughly as `MSS/(RTT·√p)`, so throughput should fall steeply as RTT grows; BBR should be much flatter. One RTT sample per mirror is not enough to fit this.
  - **Stability/burstiness:** BBR paces and holds a steady rate; CUBIC sawtooths. Visible as chunk-to-chunk variance, which is exactly what `rate-mirrors --eps` measures.
  - **Slow-start exit behaviour:** CUBIC backs off hard at the first loss; BBR usually does not.
- Practical conclusion: treat "is this mirror BBR-enabled?" as **not measurable from the client**. Measure the thing we actually care about — achieved
  throughput from *our* vantage point — and let the algorithm be an unobserved explanation.

### 3.3 Designing a short download probe that is fair to BBR-like senders

The failure mode to avoid is a test that mostly measures slow start. Fixed-duration tests reward whichever sender ramps fastest, and 1-second windows
are dominated by ramp-up on high-BDP paths.

- **Fixed byte budget, not fixed time:** request e.g. 8–32 MiB with `Range: bytes=0-…` from a stable large file, and measure `bytes / elapsed` **after
  discarding the first 500 ms–1 s** of the stream. This approximates steady state without needing a long test.
- **Minimum duration *and* minimum bytes:** run until both a floor (e.g. 2 s or 16 MiB) is satisfied, cap at a hard timeout; aborting purely on a byte
  count penalises slow-starting but ultimately fast senders, aborting purely on time favours bursty ones.
- **Parallel streams:** 2–4 concurrent ranged GETs reduce per-connection window limits and better approximate what a package manager doing several
  fetches would see (`apt-fast` exists precisely because one connection is often not the bottleneck).
- **Repeat and take the median**, at least 3 samples spread over minutes; discard the first sample (cold DNS, cold cache) and any sample with a
  redirect or non-200.
- **Instrument with `curl -w`** to separate path from server: `time_namelookup`, `time_connect`, `time_starttransfer` (server think time),
  `size_download`, `speed_download`, `num_connects`, `response_code`.
- **Reproducible `curl` invocation shape:**
  ```
  curl -sS --http1.1 --no-progress-meter -H 'Accept-Encoding: identity' --range 0-33554431 --max-time 20 \
    --resolve host:80:IP -o /dev/null \
    -w '%{time_namelookup} %{time_connect} %{time_starttransfer} %{size_download} %{speed_download} %{response_code}\n' \
    http://host/path
  ```
  `--resolve` pins the mirror we already chose; `--http1.1` keeps framing comparable; identity encoding keeps byte counts honest.
- **Why 1-second fixed-duration tests favour high-BDP paths:** a high-BDP path needs more RTTs to grow its window, so in one second it is still in
  slow start/mostly idle from a client's view — the result is a noisy measure of initial conditions, not of achievable rate. Range-based byte budgets
  with the first second discarded are the honest compromise.

### 3.4 The CDN angle

- `fastly.mirror.pkgbuild.com` is an official Arch mirror in the status JSON (observed: `delay 61`, `duration_avg 0.109`, `duration_stddev 0.064`,
  empty `country_code`), alongside `geo.mirror.pkgbuild.com` and the per-continent `*.mirror.pkgbuild.com` names.
- `deb.debian.org` and `security.debian.org` are `Type: GeoDNS` in `Mirrors.masterlist` with `Sponsor: Fastly` (and CloudFront for `deb.debian.org`) —
  primary-source evidence of anycast CDN fronting, corroborated by `via: 1.1 varnish` / `x-served-by:` headers on their responses.
- `archive.ubuntu.com` is Canonical-operated; Ubuntu's mirror directory lists the CDN and national mirrors separately
  ([launchpad.net/ubuntu/+archivemirrors](https://launchpad.net/ubuntu/+archivemirrors)).
- **"Fastly uses BBR" is NOT documented.** Fastly's docs describe BBR, expose a `client.socket.congestion_algorithm` VCL variable (which reveals the
  algorithm of a *client-facing* connection), and call BBR experimental — but I found no Fastly statement that BBR is its default egress algorithm.
  Treat the claim as **UNVERIFIED / user-observed**, and do not encode it into ranking logic.

---

## 4. D. Recommendation: what the weekly/daily job should do

1. **Rank freshness first, as a hard filter, then speed, then proximity.** Drop any mirror that fails the freshness gate before spending a single byte
   of bandwidth on it.
2. **Arch:** fetch `https://archlinux.org/mirrors/status/tier/1/json/`, keep entries with `completion_pct == 1.0`, `delay <= 3600`, `last_sync` within
   `cutoff` (86400 s), non-null `delay`; intersect with the plain-HTTP subset of `https://archlinux.org/mirrors/status/json/`; break ties on `score`
   ascending. Fall back to tier 2 only if tier 1 yields fewer than N hosts.
3. **Debian:** parse `https://mirror-master.debian.org/status/Mirrors.masterlist` for official hosts and their `Archive-http:` paths (keep
   `deb.debian.org` as the GeoDNS anchor), then for each candidate compare `dists/stable/InRelease` and `dists/stable-updates/Release` bytes against
   the reference; reject a mirror whose `Date:` is older than the reference's.
4. **Ubuntu:** scrape `https://launchpad.net/ubuntu/+archivemirrors` (country sections, protocol badges, "Up to date" status), keep `http`-capable
   entries only, and confirm each with a `dists/<suite>-security/Release` fetch — `dists/<suite>/Release` is frozen at release date and must not be
   used as a freshness signal.
5. **Measure speed with the range/discard-ramp probe from §3.3**, not with `netselect`-style ping and not with a 1-second fixed-duration test; 3
   samples, median, and record `time_starttransfer` separately so a slow origin is distinguishable from a slow path.
6. **Measure proximity as latency *to the mirror's HTTP port*** (`time_connect` on port 80), never as ICMP and never as geography — ICMP is filtered
   on many hosts and says nothing about TCP service.
7. **Do not try to detect BBR.** It is not observable from the client; encode the measured throughput and the observed stability instead, and keep
   CDN-fronted hosts as candidates on merit, not on a congestion-control assumption.
8. **Pin the chosen hosts into nginx `upstream` blocks** with `server <host>:80 max_fails=… fail_timeout=…`, preferring resolved IPs for the CDN edges
   to avoid re-resolving to a different anycast point mid-week; keep a `resolver`-based server line as the fallback.
9. **Validate after writing:** for each emitted upstream, fetch one real package path over the proxy and assert `200` plus a sane `Content-Length`; a
   mirror that passes status but 404s on `pool/…`/`extra/os/…` must be demoted automatically.
10. **Run daily, but re-rank weekly**: freshness checks are cheap and should run every day; throughput sampling is noisy and bandwidth-hungry, so
   cache the ranking and refresh it weekly, re-validating only the top N and any newly appearing mirrors.

---

## 5. Sources

- **Selection tools:** [rate-mirrors](https://github.com/westandskif/rate-mirrors) · [reflector man page](https://man.archlinux.org/man/reflector.1.en) · [reflector source](https://xyne.dev/projects/reflector/) · [ArchWiki: Reflector](https://wiki.archlinux.org/title/Reflector) · [netselect(1)](https://manpages.debian.org/unstable/netselect/netselect.1.en.html) · [netselect-apt(1)](https://manpages.debian.org/unstable/netselect-apt/netselect-apt.1.en.html) · [debmirror](https://packages.debian.org/sid/debmirror) · [apt-spy (removed)](https://packages.debian.org/sid/apt-spy) · [curl write-out vars](https://curl.se/docs/manpage.html) · [MirrorBrain](https://mirrorbrain.org/) · [apt-fast](https://github.com/ilikenwf/apt-fast) · [Smokeping](https://oss.oetiker.ch/smokeping/) · [RFC 5854 Metalink](https://datatracker.ietf.org/doc/html/rfc5854)
- **Freshness oracles:** [Arch status JSON](https://archlinux.org/mirrors/status/json/) · [Arch status page (thresholds, score formula)](https://archlinux.org/mirrors/status/) · [Arch tier 1 JSON](https://archlinux.org/mirrors/status/tier/1/json/) · [ArchWiki: Mirrors](https://wiki.archlinux.org/title/Mirrors) · [Debian mirror status](https://mirror-master.debian.org/status/) · [Mirrors.masterlist](https://mirror-master.debian.org/status/Mirrors.masterlist) · [Debian repository format](https://wiki.debian.org/DebianRepository/Format) · [Debian mirror list](https://www.debian.org/mirror/list) · [Ubuntu mirrors.txt](https://mirrors.ubuntu.com/mirrors.txt) · [Launchpad Ubuntu mirrors](https://launchpad.net/ubuntu/+archivemirrors) · [RFC 9110 HTTP semantics](https://www.rfc-editor.org/rfc/rfc9110.html)
- **Congestion control:** [RFC 9438 CUBIC](https://www.rfc-editor.org/rfc/rfc9438.txt) · [RFC 5681 TCP congestion control](https://www.rfc-editor.org/rfc/rfc5681.html) · [google/bbr](https://github.com/google/bbr) · [BBR paper (ACM Queue)](https://queue.acm.org/detail.cfm?id=3022184) · [draft-ietf-ccwg-bbr](https://datatracker.ietf.org/doc/draft-ietf-ccwg-bbr/) · [Fastly VCL: client.socket.congestion_algorithm](https://www.fastly.com/documentation/reference/vcl/variables/client-connection/client-socket-congestion-algorithm/)
