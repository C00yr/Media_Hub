# NAS Mihomo

这个目录用于在绿联云 NAS 上跟 Media Hub 一起启动 Mihomo。

文件说明：

- `config.yaml`：你的 Mihomo 配置，包含节点信息，已经被 `.gitignore` 忽略。
- `../docker-compose.yml`：已经加入 `mihomo` 服务，并让 `pt-media-hub` 后端通过 `mihomo:7890` 访问外网。

当前分流规则：

- `api.themoviedb.org`、`api.tmdb.org`、`themoviedb.org`、`tmdb.org` 走 `Moccos Cloud`。
- 其他未命中的请求默认 `DIRECT`。

部署时把整个项目目录复制到 NAS，然后在绿联云 Docker 项目里使用根目录的 `docker-compose.yml` 重新部署。
