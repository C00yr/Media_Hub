---
status: accepted
---

# Use an external NAS Mihomo container for optional TMDB egress

Media Hub keeps its existing DoH route as the default and, when the user explicitly enables TMDB proxy access, sends both allowlisted TMDB hosts through an independently deployed NAS Mihomo container over the private external Docker network `media-hub-egress`. Mihomo has the stable alias `tmdb-egress-proxy`; Media Hub uses only its HTTP/Mixed proxy endpoint, never its Controller API, TUN mode, global proxy environment variables, or subscription configuration. Proxy failures may be retried only on the same route and never silently fall back to DoH, so the enabled state remains truthful.

## Considered Options

- Bundling a dedicated Mihomo sidecar would simplify first-time setup but would make Media Hub responsible for proxy subscriptions, nodes, upgrades, and a second security boundary.
- Routing through a NAS-published port or `host.docker.internal` would avoid a shared Docker network but depends on host port publication, LAN binding, and platform-specific gateway behavior.
- Per-domain switches were rejected because partially proxying TMDB creates avoidable half-working states where data or images fail independently.

## Consequences

Users without Mihomo remain unaffected. Users who enable proxy access must create the external network once and attach both existing containers. Mihomo remains responsible for node selection and failover; Media Hub performs endpoint and end-to-end TMDB diagnostics only.
