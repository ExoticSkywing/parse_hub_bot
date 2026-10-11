"""File cookies survive upstream admin configuration edits without duplication."""

import asyncio
import errno
from pathlib import Path
from unittest.mock import Mock

import pytest
from yaml import safe_dump, safe_load

from core import platform_config as config_module
from core.platform_config import MaskedSecretStr, PlatformConfigError, PlatformsConfig, _load_netscape_cookie_file
from services import platform_config as service_module


def cookie_file(path: Path, value: str = "synthetic") -> Path:
    path.write_text(
        "# Netscape HTTP Cookie File\n"
        f"#HttpOnly_.tiktok.com\tTRUE\t/\tTRUE\t2000000000\tsessionid\t{value}\n"
        ".tiktok.com\tTRUE\t/\tTRUE\t2000000000\tsid_tt\ttest-session\n",
        encoding="utf-8",
    )
    return path


def headers(config: PlatformsConfig) -> list[str]:
    platform = config.get("tiktok")
    return [cookie.get_secret_value() for cookie in platform.cookies or []] if platform else []


@pytest.fixture
def configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = cookie_file(tmp_path / "session.txt")
    file = tmp_path / "platform_config.yaml"
    file.write_text(safe_dump({"platforms": {"tiktok": {"cookie_files": ["session.txt"]}}}), encoding="utf-8")
    config = PlatformsConfig.read_file(file)
    monkeypatch.setattr(service_module, "pl_cfg", config)
    monkeypatch.setattr(service_module, "PLATFORM_CONFIG_FILE", file)
    monkeypatch.setattr(service_module, "cookie_health", Mock())
    return config, service_module.PlatformConfigService(), file, source


def test_netscape_loader_keeps_http_only_entries(tmp_path: Path) -> None:
    source = cookie_file(tmp_path / "session.txt")
    assert _load_netscape_cookie_file(source) == "sessionid=synthetic; sid_tt=test-session"


def test_file_cookie_export_preserves_sources_not_credentials(configuration) -> None:
    config, _, _, source = configuration
    assert headers(config) == ["sessionid=synthetic; sid_tt=test-session"]
    data = config.to_data()["platforms"]["tiktok"]
    assert data == {"cookie_files": [str(source)]}


def test_mixed_inline_and_file_cookies_roundtrip_without_duplication(configuration) -> None:
    config, _, _, source = configuration
    config.get("tiktok").cookies.insert(0, MaskedSecretStr("sessionid=inline"))
    for _ in range(3):
        data = config.to_data()
        assert data["platforms"]["tiktok"] == {
            "cookies": ["sessionid=inline"], "cookie_files": [str(source)],
        }
        config = PlatformsConfig.from_data(data)
        assert headers(config) == ["sessionid=inline", "sessionid=synthetic; sid_tt=test-session"]


def test_environment_and_scalar_cookie_paths_are_resolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = cookie_file(tmp_path / "session.txt")
    monkeypatch.setenv("COOKIE_TEST_DIR", str(tmp_path))
    config = PlatformsConfig.from_data({"platforms": {"tiktok": {"cookie_files": "${COOKIE_TEST_DIR}/session.txt"}}})
    assert config.get("tiktok").cookie_files == [source]
    monkeypatch.setattr(config_module, "PLATFORM_CONFIG_FILE", tmp_path / "platform_config.yaml")
    imported = PlatformsConfig.parse_text("platforms:\n  tiktok:\n    cookie_files: session.txt\n")
    assert imported.get("tiktok").cookie_files == [source]


def test_proxy_edit_keeps_file_cookie_reference_and_live_config_identity(configuration) -> None:
    config, service, file, source = configuration
    original = headers(config)
    asyncio.run(service.edit(lambda draft: setattr(draft.get("tiktok"), "disable_parser_proxy", True)))
    assert service_module.pl_cfg is config
    assert headers(config) == original
    data = safe_load(file.read_text(encoding="utf-8"))["platforms"]["tiktok"]
    assert data == {"disable_parser_proxy": True, "cookie_files": [str(source)]}
    assert headers(PlatformsConfig.read_file(file)) == original


def test_replacing_file_cookie_detaches_source_without_overwriting_cookie_file(configuration) -> None:
    config, service, file, source = configuration
    original_file = source.read_text(encoding="utf-8")
    asyncio.run(service.edit(lambda draft: setattr(draft.get("tiktok"), "cookies", [MaskedSecretStr("sessionid=new")])))
    assert headers(config) == ["sessionid=new"]
    data = safe_load(file.read_text(encoding="utf-8"))["platforms"]["tiktok"]
    assert data == {"cookies": ["sessionid=new"]}
    assert source.read_text(encoding="utf-8") == original_file
    assert headers(PlatformsConfig.read_file(file)) == ["sessionid=new"]


def test_removing_file_cookie_does_not_reappear_on_reload(configuration) -> None:
    config, service, _, source = configuration
    asyncio.run(service.edit(lambda draft: setattr(draft.get("tiktok"), "cookies", None)))
    assert headers(config) == []
    asyncio.run(service.reload())
    assert headers(config) == []
    assert source.exists()


def test_removing_one_file_cookie_keeps_the_other(tmp_path: Path) -> None:
    first = cookie_file(tmp_path / "first.txt", "first")
    second = cookie_file(tmp_path / "second.txt", "second")
    config = PlatformsConfig.from_data({"platforms": {"tiktok": {"cookie_files": [str(first), str(second)]}}})
    config.get("tiktok").cookies.pop(0)
    data = config.to_data()
    assert data["platforms"]["tiktok"] == {"cookie_files": [str(second)]}
    assert headers(PlatformsConfig.from_data(data)) == ["sessionid=second; sid_tt=test-session"]


def test_reload_reads_new_cookie_contents_without_retaining_old_values(configuration) -> None:
    config, service, _, source = configuration
    cookie_file(source, "refreshed")
    asyncio.run(service.reload())
    assert headers(config) == ["sessionid=refreshed; sid_tt=test-session"]
    assert config.to_data()["platforms"]["tiktok"] == {"cookie_files": [str(source)]}


def test_read_only_save_keeps_live_configuration_unchanged(configuration, monkeypatch: pytest.MonkeyPatch) -> None:
    config, service, file, _ = configuration
    original = file.read_text(encoding="utf-8")
    before = config.to_data()

    def deny_write(*args, **kwargs):
        raise OSError(errno.EROFS, "read-only filesystem")

    monkeypatch.setattr(Path, "write_text", deny_write)
    with pytest.raises(OSError):
        asyncio.run(service.edit(lambda draft: setattr(draft.get("tiktok"), "disable_parser_proxy", True)))
    assert config.to_data() == before
    assert file.read_text(encoding="utf-8") == original
    service_module.cookie_health.prune.assert_not_called()


def test_missing_cookie_file_is_configuration_error_and_failed_reload_is_atomic(configuration) -> None:
    config, service, file, _ = configuration
    before = config.to_data()
    file.write_text("platforms:\n  tiktok:\n    cookie_files: missing.txt\n", encoding="utf-8")
    with pytest.raises(PlatformConfigError):
        asyncio.run(service.reload())
    assert config.to_data() == before
    service_module.cookie_health.prune.assert_not_called()


def test_bind_mount_busy_save_uses_upstream_fallback(configuration, monkeypatch: pytest.MonkeyPatch) -> None:
    config, _, file, _ = configuration

    def busy_replace(*args, **kwargs):
        raise OSError(errno.EBUSY, "busy mount")

    monkeypatch.setattr(config_module.os, "replace", busy_replace)
    config.save(file)
    assert safe_load(file.read_text(encoding="utf-8")) == config.to_data()
    assert not file.with_name(file.name + ".tmp").exists()
