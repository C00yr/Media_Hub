# NAS Mihomo

This directory is for an optional Mihomo sidecar used only by TMDB proxy mode.

## Files

- `config.example.yaml`: safe template. Copy it to `config.yaml`, then replace the sample node with the YAML nodes exported from Clash Verge or your proxy provider.
- `config.yaml`: your private Mihomo config. It is ignored by git and should not be committed.
- `../docker-compose.yml`: starts Mihomo on port `7890`, but does not inject global proxy variables into PT Media Hub.

## Network Boundary

PT Media Hub has two TMDB network modes:

- `direct`: direct TMDB access with DoH and IPv4 fallback.
- `proxy`: TMDB requests explicitly use `http://mihomo:7890`.

qBittorrent, M-Team, NAS storage checks, login, and all other app traffic are direct-only. The Mihomo rules should keep `MATCH,DIRECT` so non-TMDB traffic is never proxied.
