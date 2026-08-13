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

## Remote Team Inbox

Remote Team message metadata and bodies are untrusted durable Inbox content.
They may be displayed by the explicit Human Inbox read API, but the server must
not fetch them for delivery to an Agent, format them as a prompt, submit them to
Herdr, or derive a local command from them. This prohibition applies regardless
of binding state, lead availability, token mode, or message contents.

The legacy Inbox route and status endpoints remain only as fail-closed
compatibility surfaces. They report `available=false`, use reason
`remote_inbox_pane_delivery_disabled`, report zero fetched and delivered
messages, and never invoke a Hub Inbox fetch or a Pane operation. Stale local
route state is not consumed or exposed.

`POST /api/agent/team-reply` is a separate explicit outbound action. It remains
loopback-only and requires the existing local registry identity, active Session
generation, binding, and reply capability checks. Disabling remote body-to-Pane
delivery does not weaken or retire those checks.
