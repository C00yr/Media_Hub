# NAS Mihomo

This directory contains reference files for connecting an existing NAS Mihomo container to Media Hub. Mihomo is not bundled, started, or controlled by Media Hub.

## Files

- `config.example.yaml`: Media Hub 专用模板。复制为 `config.yaml`，然后填写订阅地址和代理密码。
- `config.yaml`: your private Mihomo config. It is ignored by git and should not be committed.
- `docker-compose.network.example.yml`: merge with the Compose file that already runs Mihomo, changing the service key when it is not named `mihomo`.
- `../docker-compose.tmdb-proxy.yml`: attaches Media Hub to the same external Docker network.

## Network Boundary

Create the external bridge network `media-hub-egress`, attach both containers, and give Mihomo the network alias `tmdb-egress-proxy`. In Settings > Media Search, enable NAS Mihomo access and keep the default host `tmdb-egress-proxy`; enter the actual `mixed-port` and optional authentication fields. Both TMDB domains are always routed together.

qBittorrent, M-Team, NAS storage checks, login, and all other app traffic are direct-only. Media Hub never injects global proxy environment variables and does not edit the external Mihomo configuration or subscription.

Mihomo does not transparently take over the Media Hub container. Media Hub explicitly sends only approved requests to Mihomo's `mixed-port`. For a Mihomo instance dedicated to Media Hub, the final `MATCH,MEDIA-HUB-PROXY` rule is sufficient and TMDB domains do not need to be duplicated in Mihomo YAML.

Keep the two containers on the private shared Docker network. Do not enable Mihomo TUN, transparent proxy, `network_mode: service:mihomo`, or global `HTTP_PROXY` / `HTTPS_PROXY` variables for Media Hub. The shared network removes the need to publish port `7890` to the NAS LAN.

See `../docs/nas-mihomo-tmdb-proxy.md` for the complete deployment procedure.

If your provider gives individual node entries instead of a subscription URL, replace `proxy-providers` with those `proxies` entries and list their names under the `MEDIA-HUB-PROXY` group. If the provider gives a complete Clash/Mihomo configuration, you may use that file directly, but keep ordinary `mixed-port` mode and avoid TUN/transparent routing.
