# Security boundary v1

This contract freezes the HTTP and WebSocket trust boundary for Cockpit's
no-token mode. It applies to every HTTP route, including health, the Web entry,
and static assets, not only to `/api` routes.

## No-token HTTP

A request is accepted only when all of these checks pass:

- the ASGI peer address is loopback;
- exactly one `Host` header is present and names `localhost`, an IPv4 loopback
  address, or an IPv6 loopback address;
- the `Host` port is absent or a canonical decimal integer from 1 through
  65535; IPv6 addresses use brackets;
- there is at most one `Origin` header; when present, it has no user info,
  path, query, or fragment and its normalized scheme, host, and effective port
  equal the request scheme and `Host`;
- no `Forwarded`, `X-Forwarded-*`, or `X-Real-IP` header is present.

Host names are case-insensitive and IP literals are compared in normalized
form. An absent port means 80 for HTTP and 443 for HTTPS, so an omitted port
and the matching explicit default port are equivalent. Different loopback
names or addresses remain different origins: for example, `localhost` does
not equal `127.0.0.1`.

HTTP may omit `Origin` so local CLI clients remain usable. A browser-provided
`Origin` is never ignored, including on safe methods and public routes.
Missing, repeated, malformed, non-loopback, or mismatched headers fail closed
with HTTP 403. Proxy headers are untrusted rather than used to repair an
invalid peer, host, scheme, or port. The bundled server also disables Uvicorn
proxy-header rewriting in no-token mode.

## No-token WebSocket

The terminal WebSocket handshake uses the same peer, single `Host`, authority,
port, and untrusted-proxy checks. It additionally requires exactly one
`Origin`. `ws` compares with an `http` Origin and `wss` compares with an
`https` Origin. A rejected handshake closes with policy code 1008 before the
server accepts it or touches a terminal session.

## Token mode

This boundary adds no Host or proxy-header restriction when `COCKPIT_TOKEN` is
configured. Existing bearer and cookie authentication, cookie-write CSRF, and
WebSocket cookie/origin behavior remain authoritative. Public health and Web
entries also retain their existing token-mode behavior.
