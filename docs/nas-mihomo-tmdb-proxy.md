# NAS Mihomo 接入 TMDB：完整部署步骤

这套方案只接入已经独立运行在 NAS Docker 中的 Mihomo，不会由 Media Hub 安装、启动或管理 Mihomo。未启用代理的用户继续使用现有 DoH 链路。

## 准备信息

开始前先确认以下内容：

- Mihomo 容器在 NAS 上已经能够正常使用代理节点。
- Mihomo 配置中存在 HTTP 或 `mixed-port`，本文示例使用 `7890`。
- 如果 Mihomo 启用了代理认证，准备好用户名和密码。
- 知道现有 Mihomo Compose 中 `services:` 下的服务名。本文使用 `mihomo`；如果你的名称不同，必须替换示例中的 `mihomo`。

机场或代理服务商提供的订阅 URL 属于 Mihomo 配置，不属于 Media Hub。已有 Mihomo 正常更新订阅时，不需要为 Media Hub 重复填写订阅 URL。

## 第一步：检查 Mihomo 配置

打开 Mihomo 当前使用的 `config.yaml`，确认至少包含：

```yaml
mixed-port: 7890
allow-lan: true
bind-address: "*"
```

如果使用订阅提供器，订阅 URL 填在 Mihomo 的配置中：

```yaml
proxy-providers:
  my-provider:
    type: http
    url: "在这里替换为你的真实订阅URL"
    path: ./proxy_providers/my-provider.yaml
    interval: 21600
```

注意：

- 不要把订阅 URL 填进 Media Hub 设置页、`.env` 或本文的 Docker 网络 YAML。
- 不要提交包含真实订阅 URL、节点或密码的 `config.yaml` 到 Git。
- 如果现有 Mihomo 已正常工作，不要为了 Media Hub 替换整份配置，只需确认代理端口和 Docker 网络。
- 建议让当前代理组使用 `url-test` 或 `fallback`，以便节点故障时由 Mihomo 自动换到可用节点。

还要确认 Mihomo 自己会把 TMDB 发往代理节点，而不是 `DIRECT`。如果现有规则的最终 `MATCH` 已指向代理组，不必增加规则；否则把下面两条放在会命中 `DIRECT` 的规则之前，并将 `你的代理组名称` 替换成现有的 `select`、`url-test` 或 `fallback` 代理组名：

```yaml
rules:
  - DOMAIN,api.themoviedb.org,你的代理组名称
  - DOMAIN,image.tmdb.org,你的代理组名称
  # 保留你原来的其他规则
```

Media Hub 的固定白名单只决定哪些应用请求允许交给 Mihomo；最终选择代理节点还是 `DIRECT`，仍由 Mihomo 规则决定。

## 第二步：创建专用 Docker 网络

### 使用 NAS 图形界面

不同 NAS 的按钮名称可能略有差异，通常依次点击：

1. 打开“Container Manager”“容器管理”或“Docker”。
2. 点击左侧“网络”。
3. 点击“新增”或“创建”。
4. 网络名称填写 `media-hub-egress`。
5. 驱动选择 `bridge`。
6. IP 范围、网关等选项保持自动，不要与家庭局域网手工设置成同一网段。
7. 点击“应用”或“创建”。

### 使用 SSH

```bash
docker network create --driver bridge media-hub-egress
```

只需创建一次。这里的“外部网络”是独立于某个 Compose 项目存在的 Docker 私有网络，不是公网。

## 第三步：让现有 Mihomo 加入网络

将项目中的 `nas-mihomo/docker-compose.network.example.yml` 复制到 Mihomo Compose 文件旁边。如果现有服务名不是 `mihomo`，把文件中的：

```yaml
services:
  mihomo:
```

替换为实际服务名，例如：

```yaml
services:
  clash-meta:
```

然后使用现有 Mihomo Compose 和网络片段共同更新容器：

```bash
docker compose \
  -f /你的Mihomo目录/docker-compose.yml \
  -f /你的Mihomo目录/docker-compose.network.yml \
  up -d
```

如果使用 NAS 图形界面编辑项目，把以下内容合并进原 Mihomo YAML，不要删除原来的镜像、卷、端口或环境变量：

```yaml
services:
  mihomo: # 如果原服务名不同，请替换这里
    networks:
      default:
      media-hub-egress:
        aliases:
          - tmdb-egress-proxy

networks:
  media-hub-egress:
    external: true
    name: media-hub-egress
```

固定别名 `tmdb-egress-proxy` 很重要。Media Hub 使用这个名称寻找 Mihomo，不依赖容器随机 IP。

## 第四步：让 Media Hub 加入网络

不要修改默认 `docker-compose.yml`。部署时同时选择项目提供的两个文件：

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.tmdb-proxy.yml \
  up -d --build
```

如果 NAS 图形界面只允许粘贴一份 YAML，请把以下片段合并到 Media Hub 原 YAML：

```yaml
services:
  pt-media-hub:
    networks:
      - default
      - media-hub-egress

networks:
  media-hub-egress:
    external: true
    name: media-hub-egress
```

不要在 Media Hub 中添加全局 `HTTP_PROXY`、`HTTPS_PROXY` 或 `ALL_PROXY`。TMDB 白名单由应用代码强制执行，其他业务请求保持直连。

## 第五步：确认两个容器已连接

在 NAS 的 Docker 网络页面打开 `media-hub-egress`，成员列表中应同时看到：

- Mihomo 容器
- `pt-media-hub` 容器

使用 SSH 时可以检查：

```bash
docker network inspect media-hub-egress
```

在 Mihomo 容器的网络信息中还应看到别名 `tmdb-egress-proxy`。

## 第六步：在 Media Hub 中测试并启用

1. 浏览器打开 Media Hub。
2. 点击左侧“设置”。
3. 进入“媒体搜索”或“TMDB 配置”。
4. 保持已有 TMDB Bearer Token。
5. 展开“网络连接”。
6. 打开“NAS Mihomo 代理访问”。
7. 代理协议选择 `HTTP / Mihomo mixed-port`。
8. 代理主机填写 `tmdb-egress-proxy`。
9. 代理端口填写 Mihomo 的 `mixed-port`，示例为 `7890`。
10. Mihomo 没有代理认证时，用户名和密码都留空；启用了认证时填写对应值。
11. 点击“测试并启用”。

测试会依次检查：

1. 能否解析 `tmdb-egress-proxy`。
2. 能否连接代理端口。
3. 代理认证和 HTTPS 隧道是否可用。
4. 能否通过代理访问 `api.themoviedb.org`。
5. 能否通过代理获取 `image.tmdb.org` 图片。

全部成功后代理立即启用。运行中发生短暂故障时，Media Hub 只会在同一代理路线内有限重试，不会静默改走 DoH。

## 常见错误

- “找不到 NAS Mihomo 容器”：两个容器不在同一个 `media-hub-egress` 网络，或 Mihomo 没有 `tmdb-egress-proxy` 别名。
- “无法连接代理端口”：端口填写错误，或 Mihomo 没有监听 `mixed-port`。
- “代理认证失败”：用户名或密码错误；未配置认证时应将两项都留空。
- “无法建立到 TMDB 的代理隧道”：检查 Mihomo 当前节点、代理组、订阅更新和节点健康状态。
- API 成功但图片失败：检查 Mihomo 日志中 `image.tmdb.org` 请求；Media Hub 不允许只代理其中一个域名。

## 关闭与恢复

在 Media Hub 中关闭代理并执行“测试并启用”后，TMDB 会恢复到原有 DoH 链路。删除或停止 Mihomo 不会损坏 DoH 配置，但应先在 Media Hub 关闭代理，避免代理模式持续报错。
