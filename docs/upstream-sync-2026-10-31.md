# 2026-10-31 上游同步

本次同步将上游 `main` 分支（PR #64、#65）合入本地 `production`，并将核心依赖 `parsehub` 从 `2.3.15` 升级至 `2.3.17`。

- Bot 仓库：`main` 从 `3f27a4d` 快进至 `00f0af7`，并合入 `production`。
- 备份分支：`backup/production-pre-upstream-20261031`。
- ParseHub 核心库：升级至 `2.3.17`。

## 上游变更内容

1. **Instagram 抓取架构全面重构（ParseHub 2.3.16）**
   - 切换为基于 `curl_cffi` 的会话网络底层。
   - 匿名流程全面切到 Instagram 新版 GraphQL 接口，以 embed 接口作为降级兜底。
   - 单视频下载优先接入 `yt-dlp` 获取最高画质直链。
2. **视频元数据补齐（ParseHub 2.3.17）**
   - 补齐 Instagram 与最右（Zuiyou）视频解析结果中的时长（`duration`）字段。

## 兼容适配与验证

- 适配测试套件：由于 ParseHub 2.3.16 彻底将 Instagram 内部请求引擎由 `httpx` 换成了 `curl_cffi.requests.AsyncSession`，同步重构了 `test/test_instagram_cookie.py` 的测试打桩，直接通过 `InstagramAPI.get_post` 对接口协议与媒体映射进行严密校验。
- 离线测试：在构建出的 staged 容器中运行单元测试，**199 passed, 0 failed** 全部通过。
- 部署：前一生产镜像标记为 `parse_hub_bot:backup-20261031`，新构建镜像标记为 `parse_hub_bot:local`，容器 `parse_hub_bot-bot-1` 已平滑重建并正常运行。
