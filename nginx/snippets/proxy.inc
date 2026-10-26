# Shared proxy behaviour for every cache location.

proxy_cache pkg;
proxy_cache_lock on;              # one upstream fetch for N concurrent clients
proxy_cache_lock_age 120s;
proxy_cache_lock_timeout 120s;

# Retry the next peer instead of handing a bad answer to the client:
#   404        "this mirror has not synced that file yet" - the Arch problem
#   403        the peer refused this group's Host header (an anycast-fronted
#              peer whose zone changed answers this way)
#   429        the peer is rate limiting us; another one probably is not
# ---
# NOT retryable, and deliberately absent: proxy_next_upstream has no 3xx and no
# 409 case (nginx accepts only error, timeout, invalid_header, http_403,
# http_404, http_429, http_500..http_504, non_idempotent, off - anything else
# is "invalid value"). A peer that 301s plain HTTP to HTTPS, or an anycast peer
# that answers 409 for a zone it no longer proxies, therefore cannot be hopped
# over at request time. That is why tools/scan-mirrors.py probes every
# candidate with the group's own Host header and drops the peers that answer
# with a redirect: the gate is applied when the pool is built, not at runtime.
proxy_next_upstream error timeout invalid_header
                       http_403 http_404 http_429
                       http_502 http_503 http_504;
# One try per peer in the biggest pool plus slack, so a mirror that lags on one
# file can never burn the whole budget and 502 while a peer has the file.
proxy_next_upstream_tries 6;
proxy_next_upstream_timeout 30s;

proxy_http_version 1.1;
proxy_set_header Connection "";
proxy_set_header Host $target_host;   # mirrors that do virtual hosting still
                                      # see their own name, not "arch_mirrors"
proxy_set_header User-Agent $http_user_agent;

# Upstreams sometimes send Cache-Control/Expires that would defeat the cache.
proxy_ignore_headers X-Accel-Expires Expires Cache-Control Set-Cookie Vary;

# Different mirrors return different ETags for the same file. The slice module
# compares the ETag of every slice against the first one and aborts the response
# ("etag mismatch in slice response") when a later slice comes from another peer,
# which truncates the client's download. Hiding ETag disables that comparison and
# costs nothing here: we ignore client conditional headers anyway.
proxy_hide_header ETag;
proxy_cache_convert_head on;

# apt and pacman both resume interrupted downloads with Range requests;
# ranges must work off the cached object.
proxy_force_ranges on;

proxy_connect_timeout 10s;
# NOTE: proxy_read_timeout is set per location, not here - nginx rejects a
# duplicate directive in the same context, so a location that wants a different
# value cannot override an inherited/included one.
proxy_send_timeout  60s;

# Detect upstreams that closed an idle keepalive connection before we reuse it.
proxy_socket_keepalive on;

# buffering is per-location (see locations.inc): the sliced location needs a
# buffer at least as large as one slice, or the response is truncated.
