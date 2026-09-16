# Instagram Cookie 优先扩展

仅在 `production` 分支维护；继续使用 ParseHub 原有 Instagram 接口，不引入 yt-dlp 回退。

## 原因与行为

ParseHub 2.2.3 的 Instagram 解析器先匿名请求，仅在特定的 metadata 错误后使用配置的 Cookie。
HTTP 401 不会触发这一回退，因此即使文件已加载，失败请求也可能没有使用登录 Cookie。

`extensions/instagram_cookie.py` 只覆盖 `_parse` 的默认 Cookie 选择：有配置时优先使用；
无配置时保留匿名流程。请求、媒体转换、错误处理、短链接处理仍由上游实现负责。
此扩展不增加重试，不在登录请求失败后额外尝试匿名请求；Bot 原有的外层重试保持不变。
Cookie 的文件读取、轮换及代理选择均不变，文件更新后仍需重启加载。

## 接入与上游同步

`services/parser.py` 在创建 `ParseHub()` 后调用 `enable_instagram_cookie_priority`。
扩展只替换该实例的原始 Instagram 解析器，不修改全局注册表、依赖安装目录或其他平台。

更新 ParseHub 后运行 `python -m pytest test/test_instagram_cookie.py`，确认继承接口、
首次请求的登录 Cookie、匿名兼容路径和媒体转换仍正确。如果上游已实现相同行为，可移除此扩展。
回退时移除服务中的扩展导入和启用调用，再重新构建部署即可。

测试使用虚构 Cookie 和 HTTP MockTransport，不连接 Instagram，不下载媒体。
一次带 Cookie 的原接口验证成功，不代表 Cookie 永不过期，也不保证所有链接都能解析。
