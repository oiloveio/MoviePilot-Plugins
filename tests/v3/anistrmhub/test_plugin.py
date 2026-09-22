"""ANiStrm插件纯逻辑单测。

按V3插件开发指南要求：普通单测不依赖公网状态，外部HTTP一律mock。
在宿主虚拟环境下运行：../MoviePilot/.venv/bin/python -m pytest tests/v3/anistrmhub
"""
import asyncio
import time
from unittest.mock import MagicMock
from urllib.parse import unquote

import pytest
from fastapi.responses import StreamingResponse

from app.plugins.anistrmhub import (
    ANiStrmHub,
    AniRssAggregator,
    EPISODE_NUM_RE,
    RelayService,
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
            reference_links=["https://alive.example/new.mp4?d=mp4"],
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
            reference_links=["https://alive.example/2026-7/other.mp4?d=mp4"],
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
            reference_links=["https://alive.example/2026-7/other.mp4?d=mp4"],
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
            reference_links=["https://alive.example/2026-7/other.mp4?d=mp4"],
        )

        assert stats["无法识别(保留原文件)"] == 1
        assert strm_file.read_text() == "not-a-url-at-all"

    def test_falls_back_to_next_candidate_when_first_is_unreachable(self, tmp_path):
        # 回归测试：长任务跑到一半，唯一参照源被限流/抖动一下就会拖累整批
        # 全部失败(实测踩过)。现在支持传多个候选前缀，第一个不通自动换下一个。
        strm_file = tmp_path / "老标题.mp4.strm"
        strm_file.write_text("https://dead.example/2025-1/ep.mp4?d=mp4", encoding="utf-8")

        request_utils = MagicMock()
        # 第一个候选(dead-mirror)每次探测都403，第二个候选(good-mirror)206
        def get_res_side_effect(url, **kwargs):
            if "dead-mirror" in url:
                return MagicMock(status_code=403)
            return MagicMock(status_code=206)

        request_utils.get_res.side_effect = get_res_side_effect
        service = StrmRelinkService(request_factory=lambda: request_utils)

        stats = service.relink_existing(
            storage_path=str(tmp_path),
            title_map={},
            reference_links=[
                "https://dead-mirror.example/2026-7/other.mp4?d=mp4",
                "https://good-mirror.example/2026-7/other.mp4?d=mp4",
            ],
        )

        assert stats["路径迁移成功"] == 1
        assert strm_file.read_text() == "https://good-mirror.example/2025-1/ep.mp4?d=mp4"

    def test_all_candidates_unreachable_reports_each_reason(self, tmp_path):
        strm_file = tmp_path / "老标题.mp4.strm"
        strm_file.write_text("https://dead.example/2025-1/ep.mp4?d=mp4", encoding="utf-8")

        request_utils = MagicMock()
        request_utils.get_res.return_value = MagicMock(status_code=403)
        service = StrmRelinkService(request_factory=lambda: request_utils)

        stats = service.relink_existing(
            storage_path=str(tmp_path),
            title_map={},
            reference_links=[
                "https://mirror-a.example/2026-7/other.mp4?d=mp4",
                "https://mirror-b.example/2026-7/other.mp4?d=mp4",
            ],
        )

        assert stats["路径迁移失败(保留原文件)"] == 1
        assert request_utils.get_res.call_count == 2  # 两个候选都试过了


class TestProbeLatencyAndSpeed:
    def test_probe_latency_ms_success(self):
        request_utils = MagicMock()
        request_utils.get_res.return_value = MagicMock(status_code=206)
        service = StrmRelinkService(request_factory=lambda: request_utils)

        latency_ms, reason = service.probe_latency_ms("https://alive.example/ep.mp4?d=mp4")

        assert latency_ms is not None and latency_ms >= 0
        assert reason is None

    def test_probe_latency_ms_reports_http_status_as_reason(self):
        request_utils = MagicMock()
        request_utils.get_res.return_value = MagicMock(status_code=403)
        service = StrmRelinkService(request_factory=lambda: request_utils)

        latency_ms, reason = service.probe_latency_ms("https://blocked.example/ep.mp4?d=mp4")

        assert latency_ms is None
        assert reason == "HTTP 403"

    def test_probe_latency_ms_reports_no_response_reason(self):
        request_utils = MagicMock()
        request_utils.get_res.return_value = None
        service = StrmRelinkService(request_factory=lambda: request_utils)

        latency_ms, reason = service.probe_latency_ms("https://timeout.example/ep.mp4?d=mp4")

        assert latency_ms is None
        assert "无响应" in reason

    def test_probe_speed_kbps_computes_from_downloaded_bytes(self):
        request_utils = MagicMock()
        request_utils.get_res.return_value = MagicMock(status_code=206, content=b"x" * 1024)
        service = StrmRelinkService(request_factory=lambda: request_utils)

        speed = service.probe_speed_kbps("https://alive.example/ep.mp4?d=mp4", chunk_bytes=1024)

        assert speed is not None and speed > 0

    def test_probe_speed_kbps_returns_none_when_unreachable(self):
        request_utils = MagicMock()
        request_utils.get_res.return_value = MagicMock(status_code=403)
        service = StrmRelinkService(request_factory=lambda: request_utils)

        assert service.probe_speed_kbps("https://blocked.example/ep.mp4?d=mp4") is None

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


class TestPickHealthyReferenceLinks:
    def test_skips_unreachable_and_returns_all_reachable_in_order(self):
        # 对应实测踩过的坑：一个源RSS能拉到，但样本直链探测不通(比如国内被墙的
        # 官方裸域名，或者被Cloudflare挑战拦截的镜像)，之前只挑"第一个能用的"
        # 当唯一参照，长任务跑到一半这个源被限流就会拖累整批全部失败。现在
        # 返回全部健康候选(保持顺序)，供relink_existing逐个尝试兜底。
        plugin = ANiStrmHub()
        plugin._client.set_sources(
            "https://dead.example/rss.xml\nhttps://alive1.example/rss.xml\nhttps://alive2.example/rss.xml"
        )
        entries_by_url = {
            "https://dead.example/rss.xml": [{"title": "x", "link": "https://dead-video.example/2026-7/x.mp4?d=mp4"}],
            "https://alive1.example/rss.xml": [{"title": "y", "link": "https://alive1-video.example/2026-7/y.mp4?d=mp4"}],
            "https://alive2.example/rss.xml": [{"title": "z", "link": "https://alive2-video.example/2026-7/z.mp4?d=mp4"}],
        }
        plugin._client.fetch_one_source = MagicMock(side_effect=lambda url: entries_by_url[url])
        plugin._relink_service.probe_latency_ms = MagicMock(
            side_effect=lambda link: (None, "HTTP 403") if "dead-video" in link else (50.0, None)
        )

        result = getattr(plugin, "_ANiStrmHub__pick_healthy_reference_links")()

        assert result == [
            "https://alive1-video.example/2026-7/y.mp4?d=mp4",
            "https://alive2-video.example/2026-7/z.mp4?d=mp4",
        ]

    def test_returns_empty_list_when_all_sources_unreachable(self):
        plugin = ANiStrmHub()
        plugin._client.set_sources("https://dead.example/rss.xml")
        plugin._client.fetch_one_source = MagicMock(
            return_value=[{"title": "x", "link": "https://dead-video.example/x.mp4?d=mp4"}]
        )
        plugin._relink_service.probe_latency_ms = MagicMock(return_value=(None, "HTTP 403"))

        result = getattr(plugin, "_ANiStrmHub__pick_healthy_reference_links")()

        assert result == []


class TestSeasonFilter:
    ENTRIES = [
        {"title": "a", "link": "https://x.example/2026-7/a.mp4?d=mp4"},
        {"title": "b", "link": "https://x.example/2026-4/b.mp4?d=mp4"},
        {"title": "c", "link": "https://x.example/2025-10/c.mp4?d=mp4"},
    ]

    def test_all_returns_everything_unchanged(self):
        plugin = ANiStrmHub()
        plugin._season_filter = ["all"]
        result = getattr(plugin, "_ANiStrmHub__apply_season_filter")(self.ENTRIES)
        assert result == self.ENTRIES

    def test_empty_filter_returns_everything_unchanged(self):
        plugin = ANiStrmHub()
        plugin._season_filter = []
        result = getattr(plugin, "_ANiStrmHub__apply_season_filter")(self.ENTRIES)
        assert result == self.ENTRIES

    def test_latest_picks_max_season_only(self):
        plugin = ANiStrmHub()
        plugin._season_filter = ["latest"]
        result = getattr(plugin, "_ANiStrmHub__apply_season_filter")(self.ENTRIES)
        assert [e["title"] for e in result] == ["a"]

    def test_specific_season_selected(self):
        plugin = ANiStrmHub()
        plugin._season_filter = ["2026-4"]
        result = getattr(plugin, "_ANiStrmHub__apply_season_filter")(self.ENTRIES)
        assert [e["title"] for e in result] == ["b"]

    def test_multiple_seasons_selected(self):
        plugin = ANiStrmHub()
        plugin._season_filter = ["2026-4", "2025-10"]
        result = getattr(plugin, "_ANiStrmHub__apply_season_filter")(self.ENTRIES)
        assert [e["title"] for e in result] == ["b", "c"]


class TestBuildProxiedUrl:
    # 借鉴用户实测确认的"Proxy Everything"风格前缀拼接格式：
    # 代理前缀 + 原host + 原path + 原query，保留原host(不是域名替换)

    def test_wraps_bare_official_link(self):
        result = StrmRelinkService.build_proxied_url(
            "https://resources.ani.rip/2025-10/xxx?d=mp4", "https://pro.pili.cc.cd"
        )
        assert result == "https://pro.pili.cc.cd/resources.ani.rip/2025-10/xxx?d=mp4"

    def test_wraps_link_without_query(self):
        result = StrmRelinkService.build_proxied_url(
            "https://resources.ani.rip/2025-10/xxx.mp4", "https://pro.pili.cc.cd"
        )
        assert result == "https://pro.pili.cc.cd/resources.ani.rip/2025-10/xxx.mp4"

    def test_strips_trailing_slash_on_prefix(self):
        result = StrmRelinkService.build_proxied_url(
            "https://resources.ani.rip/2025-10/xxx?d=mp4", "https://pro.pili.cc.cd/"
        )
        assert result == "https://pro.pili.cc.cd/resources.ani.rip/2025-10/xxx?d=mp4"

    def test_does_not_double_wrap_already_proxied_link(self):
        already = "https://pro.pili.cc.cd/resources.ani.rip/2025-10/xxx?d=mp4"
        result = StrmRelinkService.build_proxied_url(already, "https://pro.pili.cc.cd")
        assert result == already

    def test_can_rewrap_link_proxied_by_a_different_prefix(self):
        # 已经走了op5的壳，现在想统一换成pili——应该整体再包一层，
        # 不需要先识别/剥离旧前缀，效果上等同于换了个代理
        already_op5 = "https://pro.op5.de5.net/resources.ani.rip/2025-10/xxx?d=mp4"
        result = StrmRelinkService.build_proxied_url(already_op5, "https://pro.pili.cc.cd")
        assert result == "https://pro.pili.cc.cd/pro.op5.de5.net/resources.ani.rip/2025-10/xxx?d=mp4"


class TestApplyProxyPrefixTask:
    def _make_plugin(self, reachable: bool):
        plugin = ANiStrmHub()
        request_utils = MagicMock()
        request_utils.get_res.return_value = MagicMock(status_code=206 if reachable else 403)
        plugin._relink_service._request_factory = lambda: request_utils
        return plugin

    def test_wraps_bare_links_when_reachable(self, tmp_path):
        plugin = self._make_plugin(reachable=True)
        plugin._storageplace = str(tmp_path)
        plugin._proxy_prefixes = "https://pro.pili.cc.cd"
        strm_file = tmp_path / "示例.mp4.strm"
        strm_file.write_text("https://resources.ani.rip/2025-10/xxx?d=mp4", encoding="utf-8")

        getattr(plugin, "_ANiStrmHub__apply_proxy_prefix_task")()

        assert strm_file.read_text().strip() == "https://pro.pili.cc.cd/resources.ani.rip/2025-10/xxx?d=mp4"
        status = plugin.get_data("task_status")["proxy_prefix"]
        assert status["status"] == "done"
        assert "已套上代理=1" in status["summary"]

    def test_keeps_original_when_probe_fails(self, tmp_path):
        plugin = self._make_plugin(reachable=False)
        plugin._storageplace = str(tmp_path)
        plugin._proxy_prefixes = "https://pro.pili.cc.cd"
        strm_file = tmp_path / "示例.mp4.strm"
        original = "https://resources.ani.rip/2025-10/xxx?d=mp4"
        strm_file.write_text(original, encoding="utf-8")

        getattr(plugin, "_ANiStrmHub__apply_proxy_prefix_task")()

        assert strm_file.read_text().strip() == original
        status = plugin.get_data("task_status")["proxy_prefix"]
        assert "探测不可达(保留原文件)=1" in status["summary"]

    def test_skips_when_already_wrapped(self, tmp_path):
        plugin = self._make_plugin(reachable=True)
        plugin._storageplace = str(tmp_path)
        plugin._proxy_prefixes = "https://pro.pili.cc.cd"
        strm_file = tmp_path / "示例.mp4.strm"
        already = "https://pro.pili.cc.cd/resources.ani.rip/2025-10/xxx?d=mp4"
        strm_file.write_text(already, encoding="utf-8")

        getattr(plugin, "_ANiStrmHub__apply_proxy_prefix_task")()

        assert strm_file.read_text().strip() == already
        status = plugin.get_data("task_status")["proxy_prefix"]
        assert "无需更新(已套过)=1" in status["summary"]

    def test_no_proxy_prefix_configured_is_a_noop(self, tmp_path):
        plugin = self._make_plugin(reachable=True)
        plugin._storageplace = str(tmp_path)
        plugin._proxy_prefixes = ""
        strm_file = tmp_path / "示例.mp4.strm"
        original = "https://resources.ani.rip/2025-10/xxx?d=mp4"
        strm_file.write_text(original, encoding="utf-8")

        getattr(plugin, "_ANiStrmHub__apply_proxy_prefix_task")()

        assert strm_file.read_text().strip() == original
        status = plugin.get_data("task_status")["proxy_prefix"]
        assert status["summary"] == "未配置加速源"


class TestParseProxyPrefixesAndActive:
    def test_parse_filters_blank_and_commented_disables_active(self):
        plugin = ANiStrmHub()
        plugin._proxy_prefixes = "https://pro.pili.cc.cd/\n\n# https://disabled.example\nhttps://pro.op5.de5.net"
        assert getattr(plugin, "_ANiStrmHub__parse_proxy_prefixes")() == [
            "https://pro.pili.cc.cd",
            "https://pro.op5.de5.net",
        ]

    def test_active_prefix_defaults_to_first_when_unset(self):
        plugin = ANiStrmHub()
        plugin._proxy_prefixes = "https://pro.pili.cc.cd\nhttps://pro.op5.de5.net"
        assert getattr(plugin, "_ANiStrmHub__get_active_proxy_prefix")() == "https://pro.pili.cc.cd"

    def test_active_prefix_honors_explicit_selection(self):
        plugin = ANiStrmHub()
        plugin._proxy_prefixes = "https://pro.pili.cc.cd\nhttps://pro.op5.de5.net"
        plugin._active_proxy_prefix = "https://pro.op5.de5.net"
        assert getattr(plugin, "_ANiStrmHub__get_active_proxy_prefix")() == "https://pro.op5.de5.net"

    def test_active_prefix_falls_back_when_selection_no_longer_in_list(self):
        # 用户之前选的加速源被从列表里删掉了，不能报错，退化成第一个
        plugin = ANiStrmHub()
        plugin._proxy_prefixes = "https://pro.pili.cc.cd"
        plugin._active_proxy_prefix = "https://removed.example"
        assert getattr(plugin, "_ANiStrmHub__get_active_proxy_prefix")() == "https://pro.pili.cc.cd"

    def test_returns_none_when_nothing_configured(self):
        plugin = ANiStrmHub()
        plugin._proxy_prefixes = ""
        assert getattr(plugin, "_ANiStrmHub__get_active_proxy_prefix")() is None


class TestTaskAutoAppliesActivePrefix:
    def test_new_strm_gets_wrapped_when_active_prefix_configured(self, tmp_path):
        # 3.9.0并入4.0.0的真实遗漏修复：配置了加速源后，__task拉新番生成的
        # strm要自动套壳，不用再手动跑一次批量套壳
        plugin = ANiStrmHub()
        plugin._storageplace = str(tmp_path)
        plugin._proxy_prefixes = "https://pro.pili.cc.cd"
        plugin._client.set_sources("https://source.example/rss.xml")
        plugin._client.fetch_all_entries = MagicMock(
            return_value=(
                [{"title": "示例", "link": "https://resources.ani.rip/2026-7/ep.mp4?d=mp4"}],
                {"https://source.example/rss.xml": "1条"},
            )
        )

        getattr(plugin, "_ANiStrmHub__task")()

        written = (tmp_path / "示例.strm").read_text(encoding="utf-8")
        assert written == "https://pro.pili.cc.cd/resources.ani.rip/2026-7/ep.mp4?d=mp4"

    def test_new_strm_stays_bare_when_no_prefix_configured(self, tmp_path):
        plugin = ANiStrmHub()
        plugin._storageplace = str(tmp_path)
        plugin._proxy_prefixes = ""
        plugin._client.set_sources("https://source.example/rss.xml")
        plugin._client.fetch_all_entries = MagicMock(
            return_value=(
                [{"title": "示例", "link": "https://resources.ani.rip/2026-7/ep.mp4?d=mp4"}],
                {"https://source.example/rss.xml": "1条"},
            )
        )

        getattr(plugin, "_ANiStrmHub__task")()

        written = (tmp_path / "示例.strm").read_text(encoding="utf-8")
        assert written == "https://resources.ani.rip/2026-7/ep.mp4?d=mp4"


class TestRestoreProxyTask:
    def _make_plugin(self, reachable: bool):
        plugin = ANiStrmHub()
        request_utils = MagicMock()
        request_utils.get_res.return_value = MagicMock(status_code=206 if reachable else 403)
        plugin._relink_service._request_factory = lambda: request_utils
        return plugin

    def test_restores_proxied_link_to_bare_official_when_reachable(self, tmp_path):
        plugin = self._make_plugin(reachable=True)
        plugin._storageplace = str(tmp_path)
        strm_file = tmp_path / "示例.mp4.strm"
        strm_file.write_text("https://pro.pili.cc.cd/resources.ani.rip/2025-10/xxx?d=mp4", encoding="utf-8")

        getattr(plugin, "_ANiStrmHub__restore_proxy_task")()

        assert strm_file.read_text().strip() == "https://resources.ani.rip/2025-10/xxx?d=mp4"
        status = plugin.get_data("task_status")["restore_proxy"]
        assert "已还原为官方直链=1" in status["summary"]

    def test_keeps_original_when_official_probe_fails(self, tmp_path):
        plugin = self._make_plugin(reachable=False)
        plugin._storageplace = str(tmp_path)
        strm_file = tmp_path / "示例.mp4.strm"
        original = "https://pro.pili.cc.cd/resources.ani.rip/2025-10/xxx?d=mp4"
        strm_file.write_text(original, encoding="utf-8")

        getattr(plugin, "_ANiStrmHub__restore_proxy_task")()

        assert strm_file.read_text().strip() == original
        status = plugin.get_data("task_status")["restore_proxy"]
        assert "探测不可达(保留原文件)=1" in status["summary"]

    def test_already_bare_official_link_is_a_noop(self, tmp_path):
        plugin = self._make_plugin(reachable=True)
        plugin._storageplace = str(tmp_path)
        strm_file = tmp_path / "示例.mp4.strm"
        original = "https://resources.ani.rip/2025-10/xxx?d=mp4"
        strm_file.write_text(original, encoding="utf-8")

        getattr(plugin, "_ANiStrmHub__restore_proxy_task")()

        assert strm_file.read_text().strip() == original
        status = plugin.get_data("task_status")["restore_proxy"]
        assert "无需还原(已是官方直链)=1" in status["summary"]


class TestDetectProxyTask:
    def test_ranks_fastest_prefix_as_recommended(self):
        plugin = ANiStrmHub()
        plugin._proxy_prefixes = "https://slow.example\nhttps://fast.example"
        plugin._client.get_source_urls = MagicMock(return_value=["https://source.example/rss.xml"])
        plugin._client.fetch_one_source = MagicMock(
            return_value=[{"title": "样本", "link": "https://resources.ani.rip/2026-7/ep.mp4?d=mp4"}]
        )

        def fake_probe_latency(url):
            return (50.0, None)

        def fake_probe_speed(url, chunk_bytes=None):
            return 200.0 if "fast.example" in url else 20.0

        plugin._relink_service.probe_latency_ms = MagicMock(side_effect=fake_probe_latency)
        plugin._relink_service.probe_speed_kbps = MagicMock(side_effect=fake_probe_speed)

        getattr(plugin, "_ANiStrmHub__detect_proxy_task")()

        saved = plugin.get_data("proxy_health")
        recommended = [r for r in saved["results"] if r.get("recommended")]
        assert len(recommended) == 1
        assert recommended[0]["prefix"] == "https://fast.example"

    def test_unreachable_prefix_has_no_speed_and_is_not_recommended(self):
        plugin = ANiStrmHub()
        plugin._proxy_prefixes = "https://dead.example"
        plugin._client.get_source_urls = MagicMock(return_value=["https://source.example/rss.xml"])
        plugin._client.fetch_one_source = MagicMock(
            return_value=[{"title": "样本", "link": "https://resources.ani.rip/2026-7/ep.mp4?d=mp4"}]
        )
        plugin._relink_service.probe_latency_ms = MagicMock(return_value=(None, "HTTP 403"))

        getattr(plugin, "_ANiStrmHub__detect_proxy_task")()

        saved = plugin.get_data("proxy_health")
        assert saved["results"][0]["latency_ms"] is None
        assert saved["results"][0]["speed_kbps"] is None
        assert not saved["results"][0].get("recommended")

    def test_no_prefix_configured_is_a_noop(self):
        plugin = ANiStrmHub()
        plugin._proxy_prefixes = ""

        getattr(plugin, "_ANiStrmHub__detect_proxy_task")()

        status = plugin.get_data("task_status")["detect_proxy"]
        assert status["summary"] == "未配置加速源"
        assert plugin.get_data("proxy_health") is None


class TestEpisodeVariant:
    def test_build_title_variant_replaces_episode_number(self):
        result = StrmRelinkService.build_title_variant(
            "[ANi] 盡墳王 - 11 [1080P][Baha][WEB-DL][AAC AVC][CHT]", 10
        )
        assert result == "[ANi] 盡墳王 - 10 [1080P][Baha][WEB-DL][AAC AVC][CHT]"

    def test_build_title_variant_returns_none_when_no_episode_pattern(self):
        assert StrmRelinkService.build_title_variant("没有集数格式的标题", 10) is None

    def test_build_episode_variant_link_replaces_number_keeps_rest(self):
        original = (
            "https://pro.pili.cc.cd/resources.ani.rip/2026-7/"
            "%5BANi%5D%20%E7%9B%9C%E5%A2%93%E7%8E%8B%20-%2011%20"
            "%5B1080P%5D%5BBaha%5D%5BWEB-DL%5D%5BAAC%20AVC%5D%5BCHT%5D?d=mp4"
        )
        result = StrmRelinkService.build_episode_variant_link(original, 10)
        assert result is not None
        assert "- 10 " in unquote(result)
        assert "- 11" not in unquote(result)
        # 季度目录/域名/查询参数原样保留，只有集数数字变了
        assert result.startswith("https://pro.pili.cc.cd/resources.ani.rip/2026-7/")
        assert result.endswith("?d=mp4")

    def test_build_episode_variant_link_returns_none_when_no_episode_pattern(self):
        assert StrmRelinkService.build_episode_variant_link("https://example.com/no-episode-here", 10) is None


class TestBackfillTask:
    def _make_plugin(self, reachable_down_to: int = 0):
        """reachable_down_to: 探测在这个集数(含)以上都可达，低于它的一律403，
        模拟"回溯到某一集连不上就停"的场景"""
        plugin = ANiStrmHub()
        request_utils = MagicMock()

        def get_res_side_effect(url, **kwargs):
            match = EPISODE_NUM_RE.search(unquote(url))
            ep = int(match.group(2)) if match else 0
            return MagicMock(status_code=206 if ep >= reachable_down_to else 403)

        request_utils.get_res.side_effect = get_res_side_effect
        plugin._relink_service._request_factory = lambda: request_utils
        return plugin

    def test_backfills_reachable_earlier_episodes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda *_: None)
        plugin = self._make_plugin(reachable_down_to=8)
        plugin._storageplace = str(tmp_path)
        existing = tmp_path / "[ANi] 示例 - 11 [1080P][Baha][WEB-DL][AAC AVC][CHT].strm"
        existing.write_text(
            "https://pro.pili.cc.cd/resources.ani.rip/2026-7/"
            "%5BANi%5D%20%E7%A4%BA%E4%BE%8B%20-%2011%20%5B1080P%5D%5BBaha%5D%5BWEB-DL%5D%5BAAC%20AVC%5D%5BCHT%5D?d=mp4",
            encoding="utf-8",
        )

        getattr(plugin, "_ANiStrmHub__backfill_task")()

        created = sorted(p.name for p in tmp_path.glob("*.strm") if p != existing)
        assert len(created) == 3  # 10, 9, 8 补上，7探测失败后停止
        status = plugin.get_data("task_status")["backfill"]
        assert "成功补齐3集" in status["summary"]

    def test_stops_at_first_unreachable_episode(self, tmp_path, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda *_: None)
        plugin = self._make_plugin(reachable_down_to=99)  # 全部不可达
        plugin._storageplace = str(tmp_path)
        existing = tmp_path / "[ANi] 示例 - 03 [1080P][Baha][WEB-DL][AAC AVC][CHT].strm"
        existing.write_text(
            "https://pro.pili.cc.cd/resources.ani.rip/2026-7/"
            "%5BANi%5D%20%E7%A4%BA%E4%BE%8B%20-%2003%20%5B1080P%5D%5BBaha%5D%5BWEB-DL%5D%5BAAC%20AVC%5D%5BCHT%5D?d=mp4",
            encoding="utf-8",
        )

        getattr(plugin, "_ANiStrmHub__backfill_task")()

        created = [p for p in tmp_path.glob("*.strm") if p != existing]
        assert created == []
        status = plugin.get_data("task_status")["backfill"]
        assert "探测1次" in status["summary"]
        assert "成功补齐0集" in status["summary"]

    def test_skips_probe_for_episode_already_present_locally(self, tmp_path, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda *_: None)
        plugin = self._make_plugin(reachable_down_to=1)
        plugin._storageplace = str(tmp_path)
        existing_11 = tmp_path / "[ANi] 示例 - 11 [1080P][Baha][WEB-DL][AAC AVC][CHT].strm"
        existing_11.write_text(
            "https://pro.pili.cc.cd/resources.ani.rip/2026-7/"
            "%5BANi%5D%20%E7%A4%BA%E4%BE%8B%20-%2011%20%5B1080P%5D%5BBaha%5D%5BWEB-DL%5D%5BAAC%20AVC%5D%5BCHT%5D?d=mp4",
            encoding="utf-8",
        )
        existing_10 = tmp_path / "[ANi] 示例 - 10 [1080P][Baha][WEB-DL][AAC AVC][CHT].strm"
        existing_10.write_text("https://already-have.example/ep10?d=mp4", encoding="utf-8")

        getattr(plugin, "_ANiStrmHub__backfill_task")()

        # 第10集本地已经有了，不该被覆盖也不该被重新探测
        assert existing_10.read_text() == "https://already-have.example/ep10?d=mp4"

    def test_no_recognizable_series_is_a_noop(self, tmp_path):
        plugin = ANiStrmHub()
        plugin._storageplace = str(tmp_path)
        (tmp_path / "没有集数格式.strm").write_text("https://example.com/x?d=mp4", encoding="utf-8")

        getattr(plugin, "_ANiStrmHub__backfill_task")()

        status = plugin.get_data("task_status")["backfill"]
        assert status["summary"] == "本地无可识别集数的资源"


class TestRelayService:
    # Part C(Relay转发)里不依赖MoviePilot框架路由/鉴权机制的纯逻辑部分。
    # get_api()注册和MP标准鉴权会不会拦住Emby请求这一点，必须等真实宿主验证，
    # 这里只测URL拼接/token校验/响应头过滤这几个跟框架无关的函数。

    def test_is_authorized_matches_configured_token(self):
        service = RelayService(token="secret123")
        assert service.is_authorized("secret123") is True
        assert service.is_authorized("wrong") is False
        assert service.is_authorized(None) is False

    def test_is_authorized_always_false_when_no_token_configured(self):
        service = RelayService(token="")
        assert service.is_authorized("") is False
        assert service.is_authorized(None) is False

    def test_build_relay_link_encodes_real_url_and_appends_token(self):
        service = RelayService(token="secret123")
        result = service.build_relay_link(
            "http://192.168.1.10:3000", "https://resources.ani.rip/2025-10/xxx?d=mp4"
        )
        assert result == (
            "http://192.168.1.10:3000/api/v1/plugin/ANiStrmHub/relay"
            "?url=https%3A%2F%2Fresources.ani.rip%2F2025-10%2Fxxx%3Fd%3Dmp4&token=secret123"
        )

    def test_build_relay_link_strips_trailing_slash_on_base_url(self):
        service = RelayService(token="secret123")
        result = service.build_relay_link("http://192.168.1.10:3000/", "https://resources.ani.rip/x?d=mp4")
        assert result.startswith("http://192.168.1.10:3000/api/v1/plugin/ANiStrmHub/relay")

    def test_filter_upstream_headers_keeps_media_headers_strips_hop_by_hop(self):
        upstream_headers = {
            "Content-Type": "video/mp4",
            "Content-Length": "104857600",
            "Content-Range": "bytes 0-1023/104857600",
            "Accept-Ranges": "bytes",
            "Connection": "keep-alive",
            "Transfer-Encoding": "chunked",
            "Keep-Alive": "timeout=5",
        }
        filtered = RelayService.filter_upstream_headers(upstream_headers)
        assert filtered == {
            "Content-Type": "video/mp4",
            "Content-Length": "104857600",
            "Content-Range": "bytes 0-1023/104857600",
            "Accept-Ranges": "bytes",
        }


class TestRelayEndpoint:
    # get_api()路由注册/MP标准鉴权是否真的被allow_anonymous绕过，这一点
    # 必须在真实MoviePilot宿主上验证(sandbox里没有真实FastAPI路由挂载)。
    # 这里只测relay_endpoint函数体本身的纯逻辑：token校验、上游失败处理、
    # 流式转发的生成器行为，都不依赖MP框架的路由挂载。

    def _make_plugin(self, token="secret123"):
        plugin = ANiStrmHub()
        plugin._relay_token = token
        plugin._relay_service = RelayService(token=token)
        return plugin

    def test_rejects_when_token_invalid(self):
        plugin = self._make_plugin()
        result = plugin.relay_endpoint(url="https://resources.ani.rip/x?d=mp4", token="wrong")
        assert result == {"success": False, "message": "unauthorized"}

    def test_rejects_when_token_missing(self):
        plugin = self._make_plugin()
        result = plugin.relay_endpoint(url="https://resources.ani.rip/x?d=mp4", token=None)
        assert result == {"success": False, "message": "unauthorized"}

    def test_returns_error_dict_when_upstream_unreachable(self):
        plugin = self._make_plugin()
        fake_stream_ctx = MagicMock()
        fake_upstream = MagicMock(status_code=403)
        fake_stream_ctx.__enter__.return_value = fake_upstream
        fake_request_utils = MagicMock()
        fake_request_utils.get_stream.return_value = fake_stream_ctx
        plugin._client.build_request_utils = MagicMock(return_value=fake_request_utils)

        result = plugin.relay_endpoint(url="https://resources.ani.rip/x?d=mp4", token="secret123")

        assert result == {"success": False, "message": "upstream error: 403"}
        fake_stream_ctx.__exit__.assert_called_once()

    def test_streams_upstream_body_and_closes_stream_after_exhausted(self):
        plugin = self._make_plugin()
        fake_stream_ctx = MagicMock()
        fake_upstream = MagicMock(
            status_code=206,
            headers={"Content-Type": "video/mp4", "Content-Range": "bytes 0-1/2", "Connection": "keep-alive"},
        )
        fake_upstream.iter_content.return_value = iter([b"abc", b"def"])
        fake_stream_ctx.__enter__.return_value = fake_upstream
        fake_request_utils = MagicMock()
        fake_request_utils.get_stream.return_value = fake_stream_ctx
        plugin._client.build_request_utils = MagicMock(return_value=fake_request_utils)

        response = plugin.relay_endpoint(url="https://resources.ani.rip/x?d=mp4", token="secret123")

        assert isinstance(response, StreamingResponse)
        assert response.status_code == 206
        assert response.headers.get("content-type") == "video/mp4"
        assert "connection" not in response.headers  # hop-by-hop头必须被剥掉

        # StreamingResponse还没被真正消费之前，上游response不该被提前关闭
        fake_stream_ctx.__exit__.assert_not_called()

        # Starlette把同步生成器包成了async generator(线程池里跑)，要用
        # async for才能正确消费，跟FastAPI真实发响应时的驱动方式一致
        async def _collect():
            return [chunk async for chunk in response.body_iterator]

        chunks = asyncio.run(_collect())
        assert chunks == [b"abc", b"def"]
        fake_stream_ctx.__exit__.assert_called_once()

    def test_forwards_range_header_from_incoming_request(self):
        plugin = self._make_plugin()
        fake_stream_ctx = MagicMock()
        fake_upstream = MagicMock(status_code=206, headers={"Content-Type": "video/mp4"})
        fake_upstream.iter_content.return_value = iter([b"x"])
        fake_stream_ctx.__enter__.return_value = fake_upstream
        fake_request_utils = MagicMock()
        fake_request_utils.get_stream.return_value = fake_stream_ctx
        plugin._client.build_request_utils = MagicMock(return_value=fake_request_utils)

        fake_request = MagicMock()
        fake_request.headers = {"range": "bytes=100-200"}

        plugin.relay_endpoint(url="https://resources.ani.rip/x?d=mp4", token="secret123", request=fake_request)

        fake_request_utils.update_headers.assert_called_once_with({"Range": "bytes=100-200"})


class TestFinalizeStrmLink:
    def test_relay_enabled_wraps_with_relay_link_ignoring_proxy_prefix(self):
        plugin = ANiStrmHub()
        plugin._relay_enabled = True
        plugin._mp_external_url = "http://192.168.1.10:3000"
        plugin._relay_token = "secret123"
        plugin._relay_service = RelayService(token="secret123")
        plugin._proxy_prefixes = "https://pro.pili.cc.cd"  # relay打开时应该被忽略

        result = getattr(plugin, "_ANiStrmHub__finalize_strm_link")("https://resources.ani.rip/x?d=mp4")

        assert result.startswith("http://192.168.1.10:3000/api/v1/plugin/ANiStrmHub/relay?url=")
        assert "pro.pili.cc.cd" not in result

    def test_relay_disabled_falls_back_to_active_proxy_prefix(self):
        plugin = ANiStrmHub()
        plugin._relay_enabled = False
        plugin._proxy_prefixes = "https://pro.pili.cc.cd"

        result = getattr(plugin, "_ANiStrmHub__finalize_strm_link")("https://resources.ani.rip/x?d=mp4")

        assert result == "https://pro.pili.cc.cd/resources.ani.rip/x?d=mp4"

    def test_relay_enabled_but_no_external_url_falls_back_to_proxy_prefix(self):
        # relay开关开了但忘了填对外地址，不能生成一个残废的relay链接，退化成
        # 正常的加速源逻辑(或裸链接)
        plugin = ANiStrmHub()
        plugin._relay_enabled = True
        plugin._mp_external_url = ""
        plugin._proxy_prefixes = "https://pro.pili.cc.cd"

        result = getattr(plugin, "_ANiStrmHub__finalize_strm_link")("https://resources.ani.rip/x?d=mp4")

        assert result == "https://pro.pili.cc.cd/resources.ani.rip/x?d=mp4"

    def test_neither_relay_nor_prefix_configured_returns_bare_link(self):
        plugin = ANiStrmHub()
        result = getattr(plugin, "_ANiStrmHub__finalize_strm_link")("https://resources.ani.rip/x?d=mp4")
        assert result == "https://resources.ani.rip/x?d=mp4"
