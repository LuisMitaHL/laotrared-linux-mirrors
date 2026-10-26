# ---------------------------------------------------------------------------
# The two locations every server block shares. `$target` is set by the server
# block (prefix-mode map, or $host for transparent mode) and is "none" when
# the request must not be proxied anywhere.
# ---------------------------------------------------------------------------

# ---- Arch: repo DBs are never signed upstream -----------------------------
# pacman always asks for <repo>.db.sig and every mirror answers 404, so without
# this the request would burn three upstream attempts per sync (measured ~0.9s)
# and pollute the log. Verified 404 on rackspace, leaseweb and halifax.
location ~ \.db\.sig$ {
    return 404;
}

# ---- mutable index files: revalidate on every hit -------------------------
# InRelease / Release / Packages / Sources / Contents / Translation / *.db
# These are the only objects whose name stays the same while the body changes,
# so they are revalidated with If-Modified-Since instead of being trusted for
# days. Everything the index *points at* is either by-hash (Debian/Ubuntu) or
# an immutable package file, which is why a stale index never breaks a client.
location ~ ^/.*/(InRelease|Release(\.gpg|\.key)?|Packages(\.(xz|gz|bz2|zst))?|Sources(\.(xz|gz|bz2|zst))?|Contents[^/]*|Translation[^/]*|[A-Za-z0-9_.+-]+\.(db|files|abs))$ {
    if ($target = none) { return 404; }

    proxy_pass http://$target$request_uri;
    include /config/nginx/snippets/proxy.inc;

    proxy_buffering on;
    proxy_buffers 16 64k;
    proxy_busy_buffers_size 256k;

    proxy_cache_valid 200 2s;          # stale almost immediately -> revalidate
    proxy_cache_revalidate on;

    # Index files are small, so 20s of upstream silence means "dead", not
    # "slow". It also caps the damage from a mirror that accepts the connection
    # and stalls instead of answering 404 (archive.ubuntu.com does that on
    # paths that do not exist), which would otherwise hold an apt client for a
    # full minute.
    proxy_read_timeout 20s;

    # A mirror that is briefly dead must not turn into an apt/pacman error.
    proxy_cache_use_stale error timeout updating http_500 http_502 http_503 http_504;

    add_header X-Cache-Status $upstream_cache_status always;
    add_header X-Cache-Kind  index always;
}

# ---- large package files: cached in slices, so they can be filled in parallel
# A cold object is fetched from ONE upstream connection, so a 40 MB .deb from a
# 2 MB/s mirror takes 20s no matter what apt does. `slice` splits the object
# into fixed-size pieces, each cached under its own key: when several range
# requests arrive at once (tools/prewarm.py, or aria2c/apt-fast on a client)
# nginx fetches several slices CONCURRENTLY and the cache fills at N x the
# single-stream rate. Measured on a 2 MB/s upstream, 32 MiB: 21s -> 2.0s with
# 8 parallel ranges, and the follow-up plain GET was a HIT. A normal client that
# sends no Range still gets a correct, complete file -- it just walks the slices
# in order (same speed as before, but now every warm of the object is shareable).
#
# Only package-sized objects are sliced: index files stay whole (2s TTL +
# revalidation, and they are small), and by-hash objects have no extension.
location ~ ^/.*/(pool|os|iso|images)/.*\.(deb|udeb|zst|iso|tar\.(xz|gz|bz2|zst))$ {
    if ($target = none) { return 404; }

    slice 8m;                                   # 8 MiB per slice
    proxy_set_header Range $slice_range;        # each subrequest asks for one slice
    # the slice is part of the cache key, otherwise every slice overwrites the
    # previous one (same URL, different bytes)
    proxy_cache_key "$host$request_uri$slice_range";

    proxy_pass http://$target$request_uri;
    include /config/nginx/snippets/proxy.inc;

    proxy_read_timeout 30s;
    # One subrequest = one 8 MiB slice. Without temp files the slice must fit
    # in proxy_buffers; with `proxy_max_temp_file_size 0` the response was
    # truncated at ~2 MiB, so buffering to a temp file is allowed here.
    proxy_buffering on;
    proxy_buffers 16 64k;
    proxy_busy_buffers_size 256k;
    proxy_max_temp_file_size 1024m;
    proxy_cache_valid 200 206 3650d;
    proxy_cache_use_stale error timeout updating http_500 http_502 http_503 http_504 http_404;

    add_header X-Cache-Status $upstream_cache_status always;
    add_header X-Cache-Kind  slice always;
}

# ---- immutable content: cache until LRU evicts it ------------------------
# by-hash objects, *.deb/*.dsc, pacman *.pkg.tar.*, tarballs, signatures.
location / {
    if ($target = none) { return 404; }

    proxy_pass http://$target$request_uri;
    include /config/nginx/snippets/proxy.inc;

    # Per-read inactivity timeout, not total transfer time: a mirror trickling
    # a 500 MB package is fine, a mirror that accepts the connection and then
    # goes silent is not. 30s means a stalled mirror costs half a minute before
    # `timeout` in proxy_next_upstream moves to the next peer; a mirror that
    # really is streaming never waits that long between two reads.
    proxy_read_timeout 30s;

    proxy_buffering on;
    proxy_buffers 16 64k;
    proxy_busy_buffers_size 256k;
    proxy_max_temp_file_size 0;    # stream big files straight into the cache

    proxy_cache_valid 200 301 302 3650d;
    # Serving a stale copy of a package the mirror has since deleted is a
    # feature here: old client DBs still resolve.
    proxy_cache_use_stale error timeout updating http_500 http_502 http_503 http_504 http_404;

    add_header X-Cache-Status $upstream_cache_status always;
    add_header X-Cache-Kind  immutable always;
}
