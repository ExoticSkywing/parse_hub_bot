# 2026-10-19 上游同步

本次同步将上游 `main` 分支的更新合入本地 `production`，升级 ParseHub 依赖，解决冲突并调整本地扩展以兼容上游架构演进。同步后已构建镜像、通过离线单元测试并热更新运行容器。

- Bot 仓库：`main` 快进至上游 `49b7c0f`（PR #57），并合入 `production`（合并提交 `d7f3492`）。
- 备份分支：`backup/production-pre-upstream-20261019`。
- ParseHub 核心库：从 `2.3.3` 升级至 `2.3.11`。

## 上游重大更新

1. **图文与多媒体富文本发送 (`rich_media_mode`, `rich_slideshow`)**
   - 支持图文与多媒体内容采用 Telegram Native Rich Message 发送，纯视频走常规媒体通道。
   - 新增幻灯片/拼图布局切换，超过 10 张图自动折叠并置顶文案。
   - 提取公共富文本逻辑 `plugins/parse/rich.py`。
2. **游客模式解析 (`@Client.on_guest_message`)**
   - 支持群组/频道内访客查询与本地媒体上传流水线，提供更细粒度的速率控制与兜底回复。
3. **Watchdog 重连机制重构**
   - 移除了旧有的固定重试计数器与心跳重置定时器，采用 120 秒优雅重连宽限期（`reconnect_grace_seconds`）。
   - 主连接断开后等待自动重连，超时仍未恢复则正常终止交给 Docker 容器生命周期管理。
4. **Cookie 健康度判定修复**
   - 修复了此前解析失败时误计 Cookie 异常的问题，改用只有错误携带 `cookie` 字段时才计入该 Cookie 失败。
5. **ParseHub 2.3.11 架构调整**
   - TikTok 原生转向纯 Web 爬虫并移除了旧版废弃的 App Feed 接口。
   - Instagram 解析器重构为标准 `_parse(shortcode: str)` 签名并内置多阶段回退。
   - 新增微信视频号（Channels）支持与四列对齐帮助表。

## 冲突与兼容适配

| 模块 / 文件 | 上游变动 | 本地保留与适配 |
| --- | --- | --- |
| `core/watchdog.py` | 改为宽限期自动重连 | 接受上游优雅重连逻辑，清理废弃计数逻辑；同步重写 `test/test_watchdog.py` 确保覆盖完整测试矩阵 |
| `i18n/*.yaml` (16 语言) | 引入富媒体设置与新键 | 完整合并新特性词条，同时严格保留本地品牌尾部署名（`**Source: @nebuluxe**`） |
| `extensions/tiktok_web.py` | 上游已全量支持 Web 爬虫 | 依照 `docs/tiktok-web.md` 原则退役本地猴子补丁与独立测试，无缝回退至上游原生支持 |
| `extensions/instagram_cookie.py` | 上游签名重构为单参数 `shortcode` | 调整 `CookieFirstInstagramParser` 匹配 `_parse(shortcode: str)` 与 `FallbackStage` 多阶段机制，保留直连兜底与 Cookie 健康度上报 |

## 验证与状态

- 离线测试：在构建的 staged 容器内执行 `pytest`，共 **202 项测试全部通过**（0 失败）。
- 部署：镜像更新为 `parse_hub_bot:local`，容器 `parse_hub_bot-bot-1` 重建并启动。
- 实机运行：TikTok 与常规链接解析均已在容器内验证正常，Watchdog 正常初始化，日志记录清晰无冗余 DEBUG。
