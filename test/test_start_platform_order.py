from pathlib import Path

import pytest
from yaml import safe_load

from plugins import helpers


def test_selected_chinese_platforms_follow_xhs(monkeypatch) -> None:
    platforms = [
        {"id": "coolapk", "name": "酷安", "supported_types": ["图文"]},
        {"id": "tieba", "name": "贴吧", "supported_types": ["视频", "图文"]},
        {"id": "douban", "name": "豆瓣", "supported_types": ["视频", "图文"]},
        {"id": "zhihu", "name": "知乎", "supported_types": ["问答", "专栏"]},
        {"id": "pipix", "name": "皮皮虾", "supported_types": ["视频", "图文"]},
        {"id": "zuiyou", "name": "最右", "supported_types": ["视频", "图文"]},
        {"id": "douyin", "name": "抖音", "supported_types": ["视频", "图文"]},
        {"id": "xhs", "name": "小红书", "supported_types": ["视频", "图文"]},
        {"id": "youtube", "name": "Youtube", "supported_types": ["视频", "音乐"]},
        {"id": "future", "name": "Future", "supported_types": ["视频"]},
    ]
    monkeypatch.setattr(helpers, "ParseHub", lambda: type("Hub", (), {"get_platforms": lambda self: platforms})())

    lines = helpers.get_supported_platforms().splitlines()

    assert [line.split("**", 2)[1] for line in lines] == [
        "抖音",
        "小红书",
        "酷安",
        "贴吧",
        "豆瓣",
        "知乎",
        "皮皮虾",
        "最右",
        "Youtube",
        "Future",
    ]
    assert len(lines) == len(platforms)
    rows = helpers._platform_table("zh-hans").cells[1:]
    assert [row[0].text for row in rows] == [line.split("**", 2)[1] for line in lines]


def test_order_falls_back_to_existing_sort_without_xhs(monkeypatch) -> None:
    platforms = [
        {"id": "coolapk", "name": "酷安", "supported_types": ["图文"]},
        {"id": "youtube", "name": "Youtube", "supported_types": ["视频"]},
    ]
    monkeypatch.setattr(helpers, "ParseHub", lambda: type("Hub", (), {"get_platforms": lambda self: platforms})())

    assert helpers.get_supported_platforms().splitlines() == [
        "**酷安** __(图文)__",
        "**Youtube** __(视频)__",
    ]


@pytest.mark.parametrize("language", ["zh-hans", "zh-hant", "en-us", "ja-jp"])
def test_rich_help_retains_instance_attribution(language: str) -> None:
    message = helpers.build_help_rich_message(language)
    text = repr(message)
    assert "Source: " in text and "@nebuluxe" in text
    assert "https://t.me/nebuluxe" in text
    assert "github.com/z-mio" not in text
    assert "/ocfg" in text


@pytest.mark.parametrize("language", ["zh-hans", "zh-hant", "en-us", "ja-jp"])
def test_legacy_inline_help_retains_instance_attribution(language: str) -> None:
    text = helpers.build_start_text()[language]
    assert "Source: @nebuluxe" in text
    assert "github.com/z-mio" not in text


def test_merged_translations_are_valid_and_keep_instance_attribution() -> None:
    directory = Path(__file__).resolve().parents[1] / "i18n"
    files = list(directory.glob("*.yaml"))
    assert len(files) == 16
    for file in files:
        text = file.read_text(encoding="utf-8")
        data = safe_load(text)
        assert isinstance(data, dict)
        assert all(isinstance(key, str) and isinstance(value, str) for key, value in data.items())
        assert not any(marker in text for marker in ("<<<<<<<", "=======", ">>>>>>>"))
        assert "**Source: @nebuluxe**" in text
