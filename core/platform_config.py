import errno
import os
import random
from pathlib import Path
from typing import Any

from parsehub.types import Platform as PPlatform
from pydantic import AnyUrl, BaseModel, ConfigDict, PrivateAttr, SecretStr, ValidationError, field_serializer
from yaml import YAMLError, safe_dump, safe_load

from log import logger
from utils.helpers import mask_secret

from .config import bs

logger = logger.bind(name="PlatformConfig")

PLATFORM_CONFIG_FILE = bs.config_path / "platform_config.yaml"
PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")


class PlatformConfigError(ValueError):
    pass


class MaskedSecretStr(SecretStr):
    def _display(self) -> str:
        value = self._secret_value
        return mask_secret(value)


def _load_netscape_cookie_file(file: Path) -> str:
    """将 yt-dlp 导出的 Netscape Cookie 文件转换为 Cookie 请求头字符串。"""
    try:
        content = file.read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"无法读取 Cookie 文件 [{file}]: {e}") from e

    cookies: dict[str, str] = {}
    for line in content.splitlines():
        # Netscape 格式允许 #HttpOnly_ 开头的有效记录，其余 # 开头内容均为注释。
        if line.startswith("#") and not line.startswith("#HttpOnly_"):
            continue

        fields = line.split("\t", 6)
        if len(fields) < 7:
            continue

        name = fields[5].strip()
        if name:
            cookies[name] = fields[6].strip()

    if not cookies:
        raise ValueError(f"Cookie 文件 [{file}] 中未找到有效 Cookie")

    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def url_str(url: AnyUrl) -> str:
    """去掉 AnyUrl 自动补上的根路径 `/`"""
    s = str(url)
    if url.path == "/" and not url.query and not url.fragment and s.endswith("/"):
        return s[:-1]
    return s


class Platform(BaseModel):
    model_config = ConfigDict(extra="forbid")

    disable_parser_proxy: bool = False
    disable_downloader_proxy: bool = False
    parser_proxies: list[AnyUrl] | None = None
    downloader_proxies: list[AnyUrl] | None = None
    cookies: list[MaskedSecretStr] | None = None
    cookie_files: list[Path] | None = None
    _file_cookies: dict[Path, str] = PrivateAttr(default_factory=dict)

    def model_post_init(self, __context: object) -> None:
        if not self.cookie_files:
            return

        self._file_cookies = {file: _load_netscape_cookie_file(file) for file in self.cookie_files}
        file_cookies = [MaskedSecretStr(value) for value in self._file_cookies.values()]
        self.cookies = [*(self.cookies or []), *file_cookies] or None

    @field_serializer("cookies")
    def serialize_cookies(self, cookies: list[SecretStr] | None) -> list[str] | None:
        if cookies is None:
            return None
        return [str(cookie) for cookie in cookies]

    def roll_cookie(self) -> MaskedSecretStr | None:
        if not self.cookies:
            return None
        return random.choice(self.cookies)

    def roll_parser_proxy(self) -> str | None:
        if not self.parser_proxies:
            return None
        return str(random.choice(self.parser_proxies))

    def roll_downloader_proxy(self) -> str | None:
        if not self.downloader_proxies:
            return None
        return str(random.choice(self.downloader_proxies))

    def to_data(self) -> dict[str, Any]:
        """导出为可写回 yaml 的数据, cookie 为明文"""
        data: dict[str, Any] = {}
        if self.disable_parser_proxy:
            data["disable_parser_proxy"] = True
        if self.disable_downloader_proxy:
            data["disable_downloader_proxy"] = True
        if self.parser_proxies is not None:
            data["parser_proxies"] = [url_str(u) for u in self.parser_proxies]
        if self.downloader_proxies is not None:
            data["downloader_proxies"] = [url_str(u) for u in self.downloader_proxies]
        values = [c.get_secret_value() for c in self.cookies or []]
        active_files = {
            path: value for path, value in self._file_cookies.items()
            if path in (self.cookie_files or []) and value in values
        }
        if active_files:
            # Keep file sources, rather than snapshotting their credentials into yaml.
            data["cookie_files"] = [str(path) for path in self.cookie_files or [] if path in active_files]
        inline_values = [value for value in values if value not in active_files.values()]
        if inline_values:
            data["cookies"] = inline_values
        return data


class PlatformsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_parser_proxies: list[AnyUrl] | None = None
    default_downloader_proxies: list[AnyUrl] | None = None
    platforms: dict[str, Platform] = {}

    @classmethod
    def load_config(cls, file: Path) -> "PlatformsConfig":
        try:
            return cls.read_file(file)
        except PlatformConfigError as e:
            logger.error(str(e))
            raise SystemExit(1) from e

    @classmethod
    def read_file(cls, file: Path) -> "PlatformsConfig":
        """读取配置文件, 配置有误时抛出 PlatformConfigError"""
        if not file.exists():
            logger.info("未找到 platform_config.yaml, 跳过加载")
            return cls()

        try:
            with open(file, encoding="utf-8") as f:
                data = safe_load(f)
        except YAMLError as e:
            raise PlatformConfigError(f"platform_config.yaml 格式错误:\n{e}") from e

        if not data:
            logger.info("platform_config.yaml 为空, 跳过加载")
            return cls()

        pc = cls.from_data(data, base_dir=file.parent)
        logger.debug(f"已载入平台配置: {pc.model_dump_json(indent=4)}")
        return pc

    @classmethod
    def parse_text(cls, text: str) -> "PlatformsConfig":
        """解析 yaml 文本, 配置有误时抛出 PlatformConfigError"""
        try:
            data = safe_load(text)
        except YAMLError as e:
            raise PlatformConfigError(f"platform_config.yaml 格式错误:\n{e}") from e
        return cls.from_data(data) if data else cls()

    @classmethod
    def from_data(cls, data: Any, *, base_dir: Path | None = None) -> "PlatformsConfig":
        if not isinstance(data, dict):
            raise PlatformConfigError("platform_config.yaml 顶层必须是键值对")

        platforms = {}
        if data.get("platforms"):
            pid_list = [p.id for p in PPlatform]
            for name, pdata in data["platforms"].items():
                if name not in pid_list:
                    raise PlatformConfigError(f"平台 [{name}] 不存在, 支持的平台id: {pid_list}")

                if not pdata:
                    continue

                try:
                    platform_data = dict(pdata)
                    raw_cookie_files = platform_data.get("cookie_files")
                    if raw_cookie_files is not None:
                        platform_data["cookie_files"] = [
                            cls._resolve_path(cookie_file, base_dir or PLATFORM_CONFIG_FILE.parent)
                            for cookie_file in cls._2l(raw_cookie_files) or []
                        ]
                    platforms[name] = Platform(**platform_data)
                except (ValueError, TypeError, OSError) as e:
                    raise PlatformConfigError(f"平台 [{name}] 配置错误:\n{e}") from e

        try:
            return cls(
                default_parser_proxies=cls._2l(data.get("default_parser_proxies", None)),
                default_downloader_proxies=cls._2l(data.get("default_downloader_proxies", None)),
                platforms=platforms,
            )
        except ValidationError as e:
            raise PlatformConfigError(f"全局默认代理配置错误:\n{e}") from e

    def to_data(self) -> dict[str, Any]:
        """导出为可写回 yaml 的数据, cookie 为明文"""
        data: dict[str, Any] = {}
        if self.default_parser_proxies is not None:
            data["default_parser_proxies"] = [url_str(u) for u in self.default_parser_proxies]
        if self.default_downloader_proxies is not None:
            data["default_downloader_proxies"] = [url_str(u) for u in self.default_downloader_proxies]
        platforms = {pid: pdata for pid, p in self.platforms.items() if (pdata := p.to_data())}
        if platforms:
            data["platforms"] = platforms
        return data

    def save(self, file: Path) -> None:
        """原子写入配置文件; 文件被单独挂载 (Docker bind mount) 无法替换时退回直接覆盖写入"""
        text = safe_dump(self.to_data(), allow_unicode=True, sort_keys=False)
        tmp = file.with_name(f"{file.name}.tmp")
        tmp.write_text(text, encoding="utf-8")
        try:
            os.replace(tmp, file)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            if e.errno != errno.EBUSY:
                raise
            file.write_text(text, encoding="utf-8")

    def update_from(self, other: "PlatformsConfig") -> None:
        """原地替换为另一份配置, 已导入 pl_cfg 的模块会同步看到新值"""
        for name in type(self).model_fields:
            setattr(self, name, getattr(other, name))

    @staticmethod
    def _2l[T](v: T | list[T] | None) -> list[T] | None:
        if v is None:
            return None
        if isinstance(v, list):
            return v
        return [v]

    @staticmethod
    def _resolve_path(value: str | Path, base_dir: Path) -> Path:
        path = Path(os.path.expanduser(os.path.expandvars(str(value))))
        return path if path.is_absolute() else base_dir / path

    def get(self, platform_id: str) -> Platform | None:
        return self.platforms.get(platform_id)

    def roll_cookie(self, platform_id: str) -> MaskedSecretStr | None:
        if not (pc := self.get(platform_id)):
            return None
        return pc.roll_cookie()

    def roll_parser_proxy(self, platform_id: str) -> str | None:
        if not (pc := self.get(platform_id)):
            pc = Platform()
        if pc.disable_parser_proxy:
            return None

        if platform_proxy := pc.roll_parser_proxy():
            return platform_proxy
        if self.default_parser_proxies:
            return str(random.choice(self.default_parser_proxies))
        return None

    def roll_downloader_proxy(self, platform_id: str) -> str | None:
        if not (pc := self.get(platform_id)):
            pc = Platform()
        if pc.disable_downloader_proxy:
            return None

        if platform_proxy := pc.roll_downloader_proxy():
            return platform_proxy
        if self.default_downloader_proxies:
            return str(random.choice(self.default_downloader_proxies))
        return None


pl_cfg = PlatformsConfig.load_config(PLATFORM_CONFIG_FILE)
