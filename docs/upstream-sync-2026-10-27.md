# 2026-10-27 上游同步

本次同步将上游 `main` 分支（PR #58 ~ #63）合入本地 `production`，并将核心依赖 `parsehub` 从 `2.3.11` 升级至 `2.3.15`。

- Bot 仓库：`main` 从 `49b7c0f` 快进至 `3f27a4d`（包含 release 标签 `v2.2.5`、`v2.2.6`、`v2.2.7`），并合入 `production`（合并提交 `7ee9eb3`）。
- 备份分支：`backup/production-pre-upstream-20261027`。
- ParseHub 核心库：升级至 `2.3.15`。

## 上游变更内容

1. **Bot 交互优化**
   - 游客模式链接检测优化（`plugins/parse/guest.py`）：当在群内 @ 机器人或回复消息时，若包含链接实体且为未支持平台，明确提示「不支持的平台」，而非误报「请加上链接或回复一条消息」。
2. **ParseHub 核心库升级（2.3.12 ~ 2.3.15）**
   - 小红书（xhs）：修复图片抓取 404 中断问题。
   - 抖音（Douyin）：支持备用下载链接回退，保留同画质全部 CDN 节点。
   - 哔哩哔哩（Bilibili）：修复多 P 视频分 P 时长错误读取为稿件总时长的问题。
   - 知乎（Zhihu）：支持移动端专栏链接格式（`www.zhihu.com/appview/p`）。
   - 最右与小黑盒：支持最新分享短链与失效用例更新。
   - yt-dlp：升级底层依赖。

## 验证与部署

- 单元测试：在构建出的 `parse_hub_bot:staged` 容器环境中执行 `pytest`，202 项测试全部通过（0 失败）。
- 部署：前一生产镜像备份为 `parse_hub_bot:backup-20261027`，新镜像标记为 `parse_hub_bot:local` 并重建容器 `parse_hub_bot-bot-1`。
- 运行状态：容器启动正常，Watchdog 连接成功，管理员菜单初始化就绪。
