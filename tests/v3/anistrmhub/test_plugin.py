"""ANiStrm插件纯逻辑单测。

按V3插件开发指南要求：普通单测不依赖公网状态，外部HTTP一律mock。
在宿主虚拟环境下运行：../MoviePilot/.venv/bin/python -m pytest tests/v3/anistrmhub
"""
from unittest.mock import MagicMock

import pytest

from app.plugins.anistrmhub import (
    ANiStrmHub,
    AniRssAggregator,
    StrmFileService,
    StrmRelinkService,
)

SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:anime="https://resources.ani.rip" version="2.0">
<channel>
<title>ANi Download API</title>
<item>
<title>[ANi] 示例番剧 - 01 [1080P][Baha][WEB-DL][AAC AVC][CHT].mp4</title>
<link>https://pro.example.cc/resources.ani.rip/2026-7/%5BANi%5D%20example-01.mp4?d=mp4</link>
<pubDate>Tue, 15 Sep 2026 11:30:51 GMT</pubDate>
<anime:size>573.0 MB</anime:size>
</item>
<item>
<title>[ANi] 缺链接的条目 - 02.mp4</title>
</item>
</channel>
</rss>"""


class TestGetSourceUrls:
    def test_filters_blank_and_commented_lines(self):
        aggregator = AniRssAggregator(
            sources_text="https://a.example/rss.xml\n\n# https://disabled.example/rss.xml\nhttps://b.example/rss.xml"
        )
        assert aggregator.get_source_urls() == [
            "https://a.example/rss.xml",
            "https://b.example/rss.xml",
        ]

    def test_dedupes_identical_lines(self):
        aggregator = AniRssAggregator(sources_text="https://a.example/rss.xml\nhttps://a.example/rss.xml")
        assert aggregator.get_source_urls() == ["https://a.example/rss.xml"]


class TestParseRss:
    def test_extracts_complete_items_and_skips_incomplete(self):
        entries = AniRssAggregator._parse_rss(SAMPLE_RSS.encode("utf-8"))
        assert len(entries) == 1
        assert entries[0]["title"] == "[ANi] 示例番剧 - 01 [1080P][Baha][WEB-DL][AAC AVC][CHT].mp4"
        assert entries[0]["link"].endswith("?d=mp4")
        assert entries[0]["size"] == "573.0 MB"


class TestFetchAllEntriesDedup:
    def test_first_source_wins_on_title_collision(self):
        aggregator = AniRssAggregator(sources_text="https://a.example/rss.xml\nhttps://b.example/rss.xml")

        def fake_fetch_one(url):
            if url == "https://a.example/rss.xml":
                return [{"title": "同一集", "link": "https://a.example/ep.mp4?d=mp4"}]
            return [
                {"title": "同一集", "link": "https://b.example/ep.mp4?d=mp4"},
                {"title": "另一集", "link": "https://b.example/ep2.mp4?d=mp4"},
            ]

        aggregator._fetch_one = fake_fetch_one
        entries, stats = aggregator.fetch_all_entries()

        assert len(entries) == 2
        assert entries[0]["link"] == "https://a.example/ep.mp4?d=mp4"
        assert stats["https://a.example/rss.xml"] == "1条"
        assert stats["https://b.example/rss.xml"] == "2条"

    def test_failed_source_is_skipped_not_fatal(self):
        aggregator = AniRssAggregator(sources_text="https://broken.example/rss.xml\nhttps://ok.example/rss.xml")

        def fake_fetch_one(url):
            if url == "https://broken.example/rss.xml":
                raise ValueError("HTTP状态异常：403")
            return [{"title": "正常条目", "link": "https://ok.example/ep.mp4?d=mp4"}]

        aggregator._fetch_one = fake_fetch_one
        entries, stats = aggregator.fetch_all_entries()

        assert len(entries) == 1
        assert stats["https://broken.example/rss.xml"] == "失败"


class TestResourcePathExtraction:
    @pytest.mark.parametrize(
        "url,expected_path",
        [
            (
                "https://pro.pili.cc.cd/resources.ani.rip/2026-7/%5BANi%5D%20ep.mp4?d=mp4",
                "2026-7/%5BANi%5D%20ep.mp4?d=mp4",
            ),
            (
                # td.ee镜像没有resources.ani.rip中间路径这一段，正则要能兼容
                "https://ani.td.ee/2026-7/%5BANi%5D%20ep.mp4?d=mp4",
                "2026-7/%5BANi%5D%20ep.mp4?d=mp4",
            ),
            ("https://resources.ani.rip/2025-10/ep.mp4?d=mp4", "2025-10/ep.mp4?d=mp4"),
        ],
    )
    def test_extract_resource_path(self, url, expected_path):
        assert StrmRelinkService.extract_resource_path(url) == expected_path

    def test_extract_resource_path_returns_none_for_unrecognized_url(self):
        assert StrmRelinkService.extract_resource_path("https://example.com/not-ani-shaped") is None

    def test_derive_prefix(self):
        url = "https://pro.pili.cc.cd/resources.ani.rip/2026-7/ep.mp4?d=mp4"
        assert StrmRelinkService.derive_prefix(url) == "https://pro.pili.cc.cd/resources.ani.rip/"


class TestTouchStrmFile:
    def test_creates_file_with_raw_url_unmodified(self, tmp_path):
        # 回归测试：早期版本曾把?d=mp4改写成.mp4后缀导致目标站点404，
        # 现在必须原样写入RSS给的link，不做任何后缀改写。
        service = StrmFileService()
        raw_url = "https://pro.pili.cc.cd/resources.ani.rip/2026-7/ep.mp4?d=mp4"

        status = service.touch_strm_file(
            storage_path=str(tmp_path), file_name="[ANi] 示例.mp4", file_url=raw_url
        )

        assert status == "created"
        written = (tmp_path / "[ANi] 示例.mp4.strm").read_text(encoding="utf-8")
        assert written == raw_url

    def test_skips_when_file_already_exists(self, tmp_path):
        service = StrmFileService()
        service.touch_strm_file(str(tmp_path), "同一集.mp4", "https://a.example/ep.mp4?d=mp4")

        status = service.touch_strm_file(str(tmp_path), "同一集.mp4", "https://b.example/ep.mp4?d=mp4")

        assert status == "exists"
        assert "a.example" in (tmp_path / "同一集.mp4.strm").read_text(encoding="utf-8")

    def test_sanitizes_slashes_in_file_name(self, tmp_path):
        service = StrmFileService()
        service.touch_strm_file(str(tmp_path), "含/斜杠.mp4", "https://a.example/ep.mp4?d=mp4")
        assert (tmp_path / "含_斜杠.mp4.strm").exists()

    def test_relative_dir_organizes_into_season_subfolder(self, tmp_path):
        # 借鉴MangMax/ANiStrm Plus的按季度分子目录能力
        service = StrmFileService()
        status = service.touch_strm_file(
            str(tmp_path), "ep.mp4", "https://a.example/ep.mp4?d=mp4", relative_dir="2026-7"
        )
        assert status == "created"
        assert (tmp_path / "2026-7" / "ep.mp4.strm").exists()


class TestFilenameHelpers:
    # 借鉴shanhai2333/ANiStrmPro的文件名清洗/黑名单/字幕过滤

    def test_is_subtitle_file(self):
        assert StrmFileService.is_subtitle_file("[ANi] 示例 - 01.srt") is True
        assert StrmFileService.is_subtitle_file("[ANi] 示例 - 01.ASS") is True
        assert StrmFileService.is_subtitle_file("[ANi] 示例 - 01.mp4") is False

    def test_is_blacklisted(self):
        assert StrmFileService.is_blacklisted("[ANi] 示例 PV [1080P].mp4", "预告@PV@NCOP") is True
        assert StrmFileService.is_blacklisted("[ANi] 示例 - 01 [1080P].mp4", "预告@PV@NCOP") is False

    def test_is_blacklisted_empty_config_never_matches(self):
        assert StrmFileService.is_blacklisted("随便什么标题", "") is False

    def test_clean_file_name_removes_configured_tokens(self):
        result = StrmFileService.clean_file_name("[ANSUB][ANi] 示例 - 01.mp4", "[ANSUB]@NC-Raw")
        assert result == "[ANi] 示例 - 01.mp4"

    def test_clean_file_name_empty_config_returns_original(self):
        assert StrmFileService.clean_file_name("原样标题.mp4", "") == "原样标题.mp4"


class TestScanDomainDistribution:
    def test_counts_by_netloc(self, tmp_path):
        (tmp_path / "a.strm").write_text("https://pro.pili.cc.cd/x/1.mp4?d=mp4", encoding="utf-8")
        (tmp_path / "b.strm").write_text("https://pro.pili.cc.cd/x/2.mp4?d=mp4", encoding="utf-8")
        (tmp_path / "c.strm").write_text("https://resources.ani.rip/x/3.mp4?d=mp4", encoding="utf-8")

        result = StrmRelinkService.scan_domain_distribution(str(tmp_path))

        assert result["__total__"] == 3
        assert result["by_domain"] == {"pro.pili.cc.cd": 2, "resources.ani.rip": 1}

    def test_empty_directory_returns_zero(self, tmp_path):
        result = StrmRelinkService.scan_domain_distribution(str(tmp_path / "does-not-exist"))
        assert result == {"__total__": 0, "by_domain": {}}


class TestRelinkExisting:
    def _make_service(self, reachable: bool):
        request_utils = MagicMock()
        request_utils.get_res.return_value = MagicMock(status_code=206 if reachable else 403)
        return StrmRelinkService(request_factory=lambda: request_utils)

    def test_title_match_overwrites_with_latest_link(self, tmp_path):
        service = self._make_service(reachable=True)
        strm_file = tmp_path / "在RSS窗口内的标题.mp4.strm"
        strm_file.write_text("https://dead.example/old.mp4?d=mp4", encoding="utf-8")

        stats = service.relink_existing(
            storage_path=str(tmp_path),
            title_map={"在RSS窗口内的标题.mp4": "https://alive.example/new.mp4?d=mp4"},
            reference_link="https://alive.example/new.mp4?d=mp4",
        )

        assert stats["标题精确匹配更新"] == 1
        assert strm_file.read_text() == "https://alive.example/new.mp4?d=mp4"

    def test_path_migration_only_writes_when_probe_succeeds(self, tmp_path):
        service = self._make_service(reachable=True)
        strm_file = tmp_path / "不在RSS窗口内的老标题.mp4.strm"
        strm_file.write_text("https://dead.example/2025-1/ep.mp4?d=mp4", encoding="utf-8")

        stats = service.relink_existing(
            storage_path=str(tmp_path),
            title_map={},
            reference_link="https://alive.example/2026-7/other.mp4?d=mp4",
        )

        assert stats["路径迁移成功"] == 1
        assert strm_file.read_text() == "https://alive.example/2025-1/ep.mp4?d=mp4"

    def test_path_migration_keeps_original_when_probe_fails(self, tmp_path):
        service = self._make_service(reachable=False)
        strm_file = tmp_path / "探测不通过的老标题.mp4.strm"
        original = "https://dead.example/2025-1/ep.mp4?d=mp4"
        strm_file.write_text(original, encoding="utf-8")

        stats = service.relink_existing(
            storage_path=str(tmp_path),
            title_map={},
            reference_link="https://alive.example/2026-7/other.mp4?d=mp4",
        )

        assert stats["路径迁移失败(保留原文件)"] == 1
        assert strm_file.read_text() == original

    def test_unrecognized_content_is_left_untouched(self, tmp_path):
        service = self._make_service(reachable=True)
        strm_file = tmp_path / "无法识别.strm"
        strm_file.write_text("not-a-url-at-all", encoding="utf-8")

        stats = service.relink_existing(
            storage_path=str(tmp_path),
            title_map={},
            reference_link="https://alive.example/2026-7/other.mp4?d=mp4",
        )

        assert stats["无法识别(保留原文件)"] == 1
        assert strm_file.read_text() == "not-a-url-at-all"

    def test_verify_reachable_merges_range_header_instead_of_replacing(self):
        # 回归测试：get_res(headers=...)是整体替换不是合并，早期实现丢了默认UA
        # 导致探测请求被目标站点当可疑流量拦截误判为不可达，已改用update_headers()。
        request_utils = MagicMock()
        request_utils.get_res.return_value = MagicMock(status_code=206)
        service = StrmRelinkService(request_factory=lambda: request_utils)

        assert service._verify_reachable("https://alive.example/ep.mp4?d=mp4") is True
        request_utils.update_headers.assert_called_once_with({"Range": "bytes=0-0"})
        request_utils.get_res.assert_called_once_with("https://alive.example/ep.mp4?d=mp4")


class TestStopServiceNonBlocking:
    def test_shutdown_called_with_wait_false(self):
        # 回归测试：shutdown()默认wait=True会阻塞到正在运行的job跑完才返回，
        # 用户点保存时如果上一次探测/修复任务还没跑完，保存动作会被真实卡住
        # (已用真实APScheduler实测复现过)。必须显式传wait=False。
        plugin = ANiStrmHub()
        fake_scheduler = MagicMock()
        fake_scheduler.running = True
        plugin._scheduler = fake_scheduler

        plugin.stop_service()

        fake_scheduler.shutdown.assert_called_once_with(wait=False)
        assert plugin._scheduler is None


class TestTaskStatusGuard:
    def test_running_task_is_detected_and_done_is_not(self):
        plugin = ANiStrmHub()
        getattr(plugin, "_ANiStrmHub__save_task_status")("relink", "running", "进行中")
        assert getattr(plugin, "_ANiStrmHub__is_task_running")("relink") is True

        getattr(plugin, "_ANiStrmHub__save_task_status")("relink", "done", "跑完了")
        assert getattr(plugin, "_ANiStrmHub__is_task_running")("relink") is False

    def test_unknown_task_is_not_running(self):
        plugin = ANiStrmHub()
        assert getattr(plugin, "_ANiStrmHub__is_task_running")("never_ran") is False


class TestPickHealthyReferenceLink:
    def test_skips_source_whose_video_domain_is_unreachable(self):
        # 对应实测踩过的坑：一个源RSS能拉到，但样本直链探测不通(比如国内被墙的
        # 官方裸域名，或者被Cloudflare挑战拦截的镜像)，之前会被盲目当参照源，
        # 导致所有路径迁移候选全部超时失败。现在应该跳过它，选下一个真正能播的。
        plugin = ANiStrmHub()
        plugin._client.set_sources("https://dead.example/rss.xml\nhttps://alive.example/rss.xml")
        plugin._client.fetch_one_source = MagicMock(
            side_effect=lambda url: (
                [{"title": "x", "link": "https://dead-video.example/2026-7/x.mp4?d=mp4"}]
                if url == "https://dead.example/rss.xml"
                else [{"title": "y", "link": "https://alive-video.example/2026-7/y.mp4?d=mp4"}]
            )
        )
        plugin._relink_service._verify_reachable = MagicMock(
            side_effect=lambda link: "alive-video.example" in link
        )

        result = getattr(plugin, "_ANiStrmHub__pick_healthy_reference_link")()

        assert result == "https://alive-video.example/2026-7/y.mp4?d=mp4"

    def test_returns_none_when_all_sources_unreachable(self):
        plugin = ANiStrmHub()
        plugin._client.set_sources("https://dead.example/rss.xml")
        plugin._client.fetch_one_source = MagicMock(
            return_value=[{"title": "x", "link": "https://dead-video.example/x.mp4?d=mp4"}]
        )
        plugin._relink_service._verify_reachable = MagicMock(return_value=False)

        result = getattr(plugin, "_ANiStrmHub__pick_healthy_reference_link")()

        assert result is None
