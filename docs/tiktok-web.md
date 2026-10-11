# TikTok 视频网页优先扩展

在 `production` 分支维护，兼容已合并的 ParseHub 2.3.3。调整解析顺序及 TikTok 请求预算，不引入 yt-dlp、
第三方解析服务或 Mirrorbot 通信，也不修改 Cookie 加载方式和代理配置。

## 行为

- `/video/`：优先调用上游网页提取及媒体转换；失败后调用上游 App 接口。
- `/photo/`：保留上游 App 解析路径，不尝试尚不支持图文的网页解析。
- `vt.tiktok.com`、`vm.tiktok.com` 和 `tiktok.com/t/`：通过上游短链展开逻辑取得真实链接后再分流。
- 其他未识别链接：交还上游原解析逻辑。

网页成功后不请求 App；回退直接调用 App 接口，两条路径都失败时不额外再跑网页。
视频的网页、App 各最多请求一次；HTTP 429 表示限流，立即停止，不请求另一接口或继续直连重试。
若 App 限流，错误保留此前的网页失败与 App HTTP 错误；其他双路径失败也分别保留 `web` 和 `app` 原因。
TikTok 的 Bot 外层不重复相同代理请求，至多一次代理尝试、一次直连兜底；本来直连时只尝试一次。
短链展开使用同样的外层预算；图文仍保留上游内部实现。任务取消不触发回退。
预算是解析轮数；短链展开的重定向可能产生额外 HTTP 请求。
保留上游的作品 ID 核对、媒体地址选择、下载器及输出格式。
保留 `ContentUnavailableError` 类型，不把确定不可用的作品包装成双路径失败，以便服务停止重试并跳过 Cookie 失败统计。

此调整基于同一视频的原 App 接口出现空响应/429、而网页路径可以返回正常数据的实测。
网页响应仍可能波动，不能保证所有链接成功，也不能据此认定 Cookie 转换差异就是此前失败的原因。
本扩展不实现浏览器模拟或验证页处理。

## 同步与回退

实现独立放在 `extensions/tiktok_web.py`。`services/parser.py` 保留启用调用，并按 TikTok 平台限制外层重试。
仅替换该 ParseHub 实例的 TikTok 解析器，不修改全局注册表、安装的依赖或已有 Instagram 扩展。

同步上游后运行 `python -m pytest test/test_tiktok_web.py test/test_parser_retry.py test/test_instagram_cookie.py`。
测试全部使用虚构 Cookie 和 MockTransport，不联网、不下载。
如果上游实现相同行为，或需要回退，移除服务中的 TikTok 扩展导入、启用调用及 TikTok 专用重试分支，再重新部署。
