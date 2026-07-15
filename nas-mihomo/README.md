# NAS Mihomo

This directory contains an optional Mihomo example for Media Search proxy mode. Mihomo is not bundled or started by the default Media Hub Compose project.

## Files

- `config.example.yaml`: Media Hub 专用模板。复制为 `config.yaml`，然后填写订阅地址和代理密码。
- `config.yaml`: your private Mihomo config. It is ignored by git and should not be committed.
- `../docker-compose.yml`: starts only PT Media Hub. Add your own Mihomo service or use another existing HTTP/HTTPS proxy.

## Network Boundary

In Settings > Media Search, enter the authenticated proxy address from the template, for example `http://mediahub:YOUR_PASSWORD@mihomo:7890`, then choose whether the TMDB data API and TMDB image CDN should use it. Media Hub enforces that allowlist for every outbound TMDB request; all unselected sites remain direct.

qBittorrent, M-Team, NAS storage checks, login, and all other app traffic are direct-only. Media Hub never injects global proxy environment variables and does not edit the external Mihomo configuration or subscription.

Mihomo does not transparently take over the Media Hub container. Media Hub explicitly sends only approved requests to Mihomo's `mixed-port`. For a Mihomo instance dedicated to Media Hub, the final `MATCH,MEDIA-HUB-PROXY` rule is sufficient and TMDB domains do not need to be duplicated in Mihomo YAML.

Keep the two containers on the same Docker network. Do not enable Mihomo TUN, transparent proxy, `network_mode: service:mihomo`, or global `HTTP_PROXY` / `HTTPS_PROXY` variables for Media Hub. Do not publish port `7890` to the NAS LAN unless authentication is enabled and actually changed from the example password.

If your provider gives individual node entries instead of a subscription URL, replace `proxy-providers` with those `proxies` entries and list their names under the `MEDIA-HUB-PROXY` group. If the provider gives a complete Clash/Mihomo configuration, you may use that file directly, but keep ordinary `mixed-port` mode and avoid TUN/transparent routing.
