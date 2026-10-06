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
