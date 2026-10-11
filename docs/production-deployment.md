# production 本地部署

`main` 仅追踪上游；自定义功能、合并兼容修复和部署说明放在 `production`。

## 配置与权限

1. 将根目录的 `docker-compose.override.yaml.example` 复制为
   `docker-compose.override.yaml`，按实际路径设置宿主机 Cookie 目录。
2. 在本地 `.env` 中分别配置管理员数字 Telegram ID：
   `ADMIN_IDS` 授予 `/admin` 平台配置管理权限，`USAGE_ADMIN_IDS` 授予
   `/stats` 和 `/user` 使用统计权限；多个 ID 用英文逗号分隔。
   时间可用 `USAGE_TIMEZONE=Asia/Shanghai`。
3. 平台配置目录 `./data/config` 可写，支持 `/admin` 原子保存 YAML。
   Cookie 目录仍为只读。必须挂载配置**目录**，而不是单个配置文件。
4. `.env`、实际覆盖配置、平台配置、Cookie 和会话文件均不提交到 Git。
   修改管理员环境变量后需重建容器，仅重启进程不会更新容器环境变量。

没有代理需求时，不设置 `BOT_PROXY` 或平台代理即可；示例不额外添加代理。
两类管理员命令共用私聊菜单，但分别检查各自权限，不自动互相授权。

## 构建与切换

部署前保留旧镜像的独立标签，并备份本地配置及数据库。构建期间保留旧容器运行，
不要清理正在使用的镜像、Cookie 或数据卷。使用完整源码及冻结的 `uv.lock`，
不要只覆盖少量源码文件而保留不匹配的旧依赖。

```sh
docker buildx build --builder default --load -t parse_hub_bot:staged .
# 对 staged 镜像完成离线测试后再切换。
docker tag parse_hub_bot:staged parse_hub_bot:local
docker compose up -d --no-deps --force-recreate --no-build bot
```

部署后检查容器启动、数据库迁移、管理员菜单、配置目录可写和 Cookie 目录只读。
更新外部 Cookie 文件后，可以通过 `/admin` 重载配置读取新内容。
面板新增/替换 Cookie 使用请求头字符串格式，不接受 Netscape 多行文件；
文件型 Cookie 继续用平台配置中的 `cookie_files` 管理。

回滚时将保留的旧镜像标签重新标记为 `parse_hub_bot:local`，按需恢复本地配置，
再重新创建容器。若涉及不向后兼容的数据库迁移，还需要恢复部署前数据库备份。
备份包含敏感信息，应仅限管理员读取。
