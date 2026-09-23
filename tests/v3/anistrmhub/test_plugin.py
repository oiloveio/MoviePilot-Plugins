"""ANiStrmHub插件纯逻辑单测。

按V3插件开发指南要求：普通单测不依赖公网状态，外部HTTP一律mock。
在宿主虚拟环境下运行：../MoviePilot/.venv/bin/python -m pytest tests/v3/anistrmhub
"""
import time
from unittest.mock import MagicMock

import pytest

from app.plugins.anistrmhub import (
    ANiStrmHub,
    AniRssAggregator,
    EPISODE_NUM_RE,
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


class TestParseRss:
    def test_extracts_complete_items_and_skips_incomplete(self):
        entries = AniRssAggregator._parse_rss(SAMPLE_RSS.encode("utf-8"))
        assert len(entries) == 1
        assert entries[0]["title"] == "[ANi] 示例番剧 - 01 [1080P][Baha][WEB-DL][AAC AVC][CHT].mp4"
        assert entries[0]["link"].endswith("?d=mp4")
        assert entries[0]["size"] == "573.0 MB"


class TestFetchOneSource:
    def test_raises_on_http_error_status(self):
        aggregator = AniRssAggregator()
        aggregator.build_request_utils = MagicMock(
            return_value=MagicMock(get_res=MagicMock(return_value=MagicMock(status_code=403)))
        )
        with pytest.raises(ValueError):
            aggregator.fetch_one_source("https://broken.example/rss.xml")

    def test_returns_parsed_entries_on_success(self):
        aggregator = AniRssAggregator()
        response = MagicMock(status_code=200, content=SAMPLE_RSS.encode("utf-8"))
        aggregator.build_request_utils = MagicMock(return_value=MagicMock(get_res=MagicMock(return_value=response)))
        entries = aggregator.fetch_one_source("https://ok.example/rss.xml")
        assert len(entries) == 1
        assert entries[0]["title"] == "[ANi] 示例番剧 - 01 [1080P][Baha][WEB-DL][AAC AVC][CHT].mp4"


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
        already_op5 = "https://pro.op5.de5.net/resources.ani.rip/2025-10/xxx?d=mp4"
        result = StrmRelinkService.build_proxied_url(already_op5, "https://pro.pili.cc.cd")
        assert result == "https://pro.pili.cc.cd/pro.op5.de5.net/resources.ani.rip/2025-10/xxx?d=mp4"


class TestStripKnownAccelerator:
    def test_strips_matching_prefix_and_reports_it(self):
        wrapped = "https://pro.pili.cc.cd/resources.ani.rip/2025-10/xxx?d=mp4"
        stripped, matched = StrmRelinkService.strip_known_accelerator(
            wrapped, ["https://pro.pili.cc.cd", "https://pro.op5.de5.net"]
        )
        assert stripped == "https://resources.ani.rip/2025-10/xxx?d=mp4"
        assert matched == "https://pro.pili.cc.cd"

    def test_returns_original_when_no_prefix_matches(self):
        bare = "https://resources.ani.rip/2025-10/xxx?d=mp4"
        stripped, matched = StrmRelinkService.strip_known_accelerator(bare, ["https://pro.pili.cc.cd"])
        assert stripped == bare
        assert matched is None

    def test_empty_accelerator_list_returns_original(self):
        bare = "https://resources.ani.rip/2025-10/xxx?d=mp4"
        stripped, matched = StrmRelinkService.strip_known_accelerator(bare, [])
        assert stripped == bare
        assert matched is None


class TestEpisodeVariant:
    def test_build_title_variant_replaces_episode_number(self):
        result = StrmRelinkService.build_title_variant(
            "[ANi] 盡墳王 - 11 [1080P][Baha][WEB-DL][AAC AVC][CHT]", 10
        )
        assert result == "[ANi] 盡墳王 - 10 [1080P][Baha][WEB-DL][AAC AVC][CHT]"

    def test_build_title_variant_returns_none_when_no_episode_pattern(self):
        assert StrmRelinkService.build_title_variant("没有集数格式的标题", 10) is None

    def test_build_episode_variant_link_replaces_number_keeps_rest(self):
        from urllib.parse import unquote

        original = (
            "https://pro.pili.cc.cd/resources.ani.rip/2026-7/"
            "%5BANi%5D%20%E7%9B%9C%E5%A2%93%E7%8E%8B%20-%2011%20"
            "%5B1080P%5D%5BBaha%5D%5BWEB-DL%5D%5BAAC%20AVC%5D%5BCHT%5D?d=mp4"
        )
        result = StrmRelinkService.build_episode_variant_link(original, 10)
        assert result is not None
        assert "- 10 " in unquote(result)
        assert "- 11" not in unquote(result)
        assert result.startswith("https://pro.pili.cc.cd/resources.ani.rip/2026-7/")
        assert result.endswith("?d=mp4")

    def test_build_episode_variant_link_returns_none_when_no_episode_pattern(self):
        assert StrmRelinkService.build_episode_variant_link("https://example.com/no-episode-here", 10) is None


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


class TestScanLocalDistribution:
    def test_categorizes_by_source_and_accelerator(self, tmp_path):
        (tmp_path / "a.strm").write_text(
            "https://pro.pili.cc.cd/resources.ani.rip/2026-7/1.mp4?d=mp4", encoding="utf-8"
        )
        (tmp_path / "b.strm").write_text(
            "https://pro.pili.cc.cd/resources.ani.rip/2026-7/2.mp4?d=mp4", encoding="utf-8"
        )
        (tmp_path / "c.strm").write_text("https://resources.ani.rip/2026-7/3.mp4?d=mp4", encoding="utf-8")

        result = StrmRelinkService.scan_local_distribution(
            str(tmp_path),
            domain_to_source={"resources.ani.rip": "https://api.ani.rip/ani-download.xml"},
            accelerator_prefixes=["https://pro.pili.cc.cd"],
        )

        assert result["total"] == 3
        assert result["by_category"] == {
            "https://api.ani.rip/ani-download.xml + https://pro.pili.cc.cd": 2,
            "https://api.ani.rip/ani-download.xml 裸链": 1,
        }

    def test_unrecognized_domain_falls_back_to_domain_label(self, tmp_path):
        (tmp_path / "a.strm").write_text("https://unknown.example/x?d=mp4", encoding="utf-8")
        result = StrmRelinkService.scan_local_distribution(str(tmp_path), {}, [])
        assert result["by_category"] == {"unknown.example 裸链": 1}

    def test_empty_directory_returns_zero(self, tmp_path):
        result = StrmRelinkService.scan_local_distribution(str(tmp_path / "does-not-exist"), {}, [])
        assert result == {"total": 0, "by_category": {}}


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


class TestParseSourceListAndActiveSelection:
    # 5.0.0核心设计：订阅源和加速源是完全对称的"列表+单选生效项"模型

    def test_parse_filters_blank_and_commented_lines(self):
        plugin = ANiStrmHub()
        plugin._subscription_sources = "https://a.example/rss.xml\n\n# https://disabled.example/rss.xml\nhttps://b.example/rss.xml"
        assert getattr(plugin, "_ANiStrmHub__parse_subscription_sources")() == [
            "https://a.example/rss.xml",
            "https://b.example/rss.xml",
        ]

    def test_parse_dedupes_and_strips_trailing_slash(self):
        plugin = ANiStrmHub()
        plugin._accelerator_sources = "https://pro.pili.cc.cd/\nhttps://pro.pili.cc.cd"
        assert getattr(plugin, "_ANiStrmHub__parse_accelerator_sources")() == ["https://pro.pili.cc.cd"]

    def test_active_subscription_defaults_to_first_when_unset(self):
        plugin = ANiStrmHub()
        plugin._subscription_sources = "https://a.example/rss.xml\nhttps://b.example/rss.xml"
        assert getattr(plugin, "_ANiStrmHub__get_active_subscription_source")() == "https://a.example/rss.xml"

    def test_active_subscription_honors_explicit_selection(self):
        plugin = ANiStrmHub()
        plugin._subscription_sources = "https://a.example/rss.xml\nhttps://b.example/rss.xml"
        plugin._active_subscription_source = "https://b.example/rss.xml"
        assert getattr(plugin, "_ANiStrmHub__get_active_subscription_source")() == "https://b.example/rss.xml"

    def test_active_subscription_falls_back_when_selection_removed_from_list(self):
        plugin = ANiStrmHub()
        plugin._subscription_sources = "https://a.example/rss.xml"
        plugin._active_subscription_source = "https://removed.example/rss.xml"
        assert getattr(plugin, "_ANiStrmHub__get_active_subscription_source")() == "https://a.example/rss.xml"

    def test_active_subscription_none_when_list_empty(self):
        plugin = ANiStrmHub()
        plugin._subscription_sources = ""
        assert getattr(plugin, "_ANiStrmHub__get_active_subscription_source")() is None

    def test_active_accelerator_none_when_list_empty(self):
        plugin = ANiStrmHub()
        plugin._accelerator_sources = ""
        assert getattr(plugin, "_ANiStrmHub__get_active_accelerator_source")() is None

    def test_active_accelerator_honors_explicit_selection(self):
        plugin = ANiStrmHub()
        plugin._accelerator_sources = "https://pro.pili.cc.cd\nhttps://pro.op5.de5.net"
        plugin._active_accelerator_source = "https://pro.op5.de5.net"
        assert getattr(plugin, "_ANiStrmHub__get_active_accelerator_source")() == "https://pro.op5.de5.net"


class TestFinalizeStrmLink:
    def test_wraps_with_active_accelerator(self):
        plugin = ANiStrmHub()
        plugin._accelerator_sources = "https://pro.pili.cc.cd"
        result = getattr(plugin, "_ANiStrmHub__finalize_strm_link")("https://resources.ani.rip/x?d=mp4")
        assert result == "https://pro.pili.cc.cd/resources.ani.rip/x?d=mp4"

    def test_no_accelerator_configured_returns_bare_link(self):
        plugin = ANiStrmHub()
        plugin._accelerator_sources = ""
        result = getattr(plugin, "_ANiStrmHub__finalize_strm_link")("https://resources.ani.rip/x?d=mp4")
        assert result == "https://resources.ani.rip/x?d=mp4"


class TestTaskStatusGuard:
    def test_running_task_is_detected_and_done_is_not(self):
        plugin = ANiStrmHub()
        getattr(plugin, "_ANiStrmHub__save_task_status")("apply_local_strm", "running", "进行中")
        assert getattr(plugin, "_ANiStrmHub__is_task_running")("apply_local_strm") is True

        getattr(plugin, "_ANiStrmHub__save_task_status")("apply_local_strm", "done", "跑完了")
        assert getattr(plugin, "_ANiStrmHub__is_task_running")("apply_local_strm") is False

    def test_unknown_task_is_not_running(self):
        plugin = ANiStrmHub()
        assert getattr(plugin, "_ANiStrmHub__is_task_running")("never_ran") is False


class TestStopServiceNonBlocking:
    def test_shutdown_called_with_wait_false(self):
        # 回归测试：shutdown()默认wait=True会阻塞到正在运行的job跑完才返回，
        # 用户点保存时如果上一次探测/维护任务还没跑完，保存动作会被真实卡住
        # (已用真实APScheduler实测复现过)。必须显式传wait=False。
        plugin = ANiStrmHub()
        fake_scheduler = MagicMock()
        fake_scheduler.running = True
        plugin._scheduler = fake_scheduler

        plugin.stop_service()

        fake_scheduler.shutdown.assert_called_once_with(wait=False)
        assert plugin._scheduler is None


class TestMainTask:
    def test_no_active_subscription_source_is_a_noop(self, tmp_path):
        plugin = ANiStrmHub()
        plugin._subscription_sources = ""
        plugin._storageplace = str(tmp_path)

        getattr(plugin, "_ANiStrmHub__task")()

        status = plugin.get_data("task_status")["task"]
        assert status["summary"] == "未配置订阅源"
        assert list(tmp_path.glob("*.strm")) == []

    def test_creates_strm_from_active_source_only(self, tmp_path):
        plugin = ANiStrmHub()
        plugin._subscription_sources = "https://a.example/rss.xml\nhttps://b.example/rss.xml"
        plugin._active_subscription_source = "https://a.example/rss.xml"
        plugin._storageplace = str(tmp_path)
        plugin._client.fetch_one_source = MagicMock(
            return_value=[{"title": "示例", "link": "https://resources.ani.rip/2026-7/ep.mp4?d=mp4"}]
        )

        getattr(plugin, "_ANiStrmHub__task")()

        plugin._client.fetch_one_source.assert_called_once_with("https://a.example/rss.xml")
        written = (tmp_path / "示例.strm").read_text(encoding="utf-8")
        assert written == "https://resources.ani.rip/2026-7/ep.mp4?d=mp4"

    def test_new_strm_auto_wraps_with_active_accelerator(self, tmp_path):
        plugin = ANiStrmHub()
        plugin._subscription_sources = "https://a.example/rss.xml"
        plugin._accelerator_sources = "https://pro.pili.cc.cd"
        plugin._storageplace = str(tmp_path)
        plugin._client.fetch_one_source = MagicMock(
            return_value=[{"title": "示例", "link": "https://resources.ani.rip/2026-7/ep.mp4?d=mp4"}]
        )

        getattr(plugin, "_ANiStrmHub__task")()

        written = (tmp_path / "示例.strm").read_text(encoding="utf-8")
        assert written == "https://pro.pili.cc.cd/resources.ani.rip/2026-7/ep.mp4?d=mp4"

    def test_subscription_fetch_failure_ends_task_gracefully(self, tmp_path):
        plugin = ANiStrmHub()
        plugin._subscription_sources = "https://a.example/rss.xml"
        plugin._storageplace = str(tmp_path)
        plugin._client.fetch_one_source = MagicMock(side_effect=ValueError("HTTP状态异常：403"))

        getattr(plugin, "_ANiStrmHub__task")()

        status = plugin.get_data("task_status")["task"]
        assert "订阅源抓取失败" in status["summary"]

    def test_skips_subtitle_and_blacklisted_entries(self, tmp_path):
        plugin = ANiStrmHub()
        plugin._subscription_sources = "https://a.example/rss.xml"
        plugin._storageplace = str(tmp_path)
        plugin._filename_blacklist = "PV"
        plugin._client.fetch_one_source = MagicMock(
            return_value=[
                {"title": "字幕.srt", "link": "https://a.example/x.srt?d=mp4"},
                {"title": "预告 PV", "link": "https://a.example/pv.mp4?d=mp4"},
                {"title": "正片", "link": "https://a.example/ep.mp4?d=mp4"},
            ]
        )

        getattr(plugin, "_ANiStrmHub__task")()

        created = list(tmp_path.glob("*.strm"))
        assert len(created) == 1
        assert created[0].stem == "正片"


class TestApplyLocalStrmTask:
    def _make_plugin(self, reachable: bool = True):
        plugin = ANiStrmHub()
        request_utils = MagicMock()
        request_utils.get_res.return_value = MagicMock(status_code=206 if reachable else 403)
        plugin._relink_service._request_factory = lambda: request_utils
        return plugin

    def test_no_target_selected_wraps_with_target_accelerator_only(self, tmp_path):
        # 目标订阅源=不变 + 目标加速源=选中 => 等价于旧版"一键加速"
        plugin = self._make_plugin(reachable=True)
        plugin._storageplace = str(tmp_path)
        plugin._target_subscription_source = None
        plugin._target_accelerator_source = "https://pro.pili.cc.cd"
        strm_file = tmp_path / "示例.mp4.strm"
        strm_file.write_text("https://resources.ani.rip/2025-10/xxx?d=mp4", encoding="utf-8")

        getattr(plugin, "_ANiStrmHub__apply_local_strm_task")()

        assert strm_file.read_text().strip() == "https://pro.pili.cc.cd/resources.ani.rip/2025-10/xxx?d=mp4"
        status = plugin.get_data("task_status")["apply_local_strm"]
        assert "仅调整加速套壳=1" in status["summary"]

    def test_no_target_accelerator_restores_bare_link(self, tmp_path):
        # 目标订阅源=不变 + 目标加速源=不加速 => 等价于旧版"一键还原"
        plugin = self._make_plugin(reachable=True)
        plugin._storageplace = str(tmp_path)
        plugin._accelerator_sources = "https://pro.pili.cc.cd"
        plugin._target_subscription_source = None
        plugin._target_accelerator_source = None
        strm_file = tmp_path / "示例.mp4.strm"
        strm_file.write_text(
            "https://pro.pili.cc.cd/resources.ani.rip/2025-10/xxx?d=mp4", encoding="utf-8"
        )

        getattr(plugin, "_ANiStrmHub__apply_local_strm_task")()

        assert strm_file.read_text().strip() == "https://resources.ani.rip/2025-10/xxx?d=mp4"
        status = plugin.get_data("task_status")["apply_local_strm"]
        assert "仅调整加速套壳=1" in status["summary"]

    def test_target_subscription_title_match_uses_latest_link(self, tmp_path):
        plugin = self._make_plugin(reachable=True)
        plugin._storageplace = str(tmp_path)
        plugin._target_subscription_source = "https://b.example/rss.xml"
        plugin._target_accelerator_source = None
        plugin._client.fetch_one_source = MagicMock(
            return_value=[{"title": "在窗口内的标题", "link": "https://alive.example/new.mp4?d=mp4"}]
        )
        strm_file = tmp_path / "在窗口内的标题.strm"
        strm_file.write_text("https://dead.example/old.mp4?d=mp4", encoding="utf-8")

        getattr(plugin, "_ANiStrmHub__apply_local_strm_task")()

        assert strm_file.read_text().strip() == "https://alive.example/new.mp4?d=mp4"
        status = plugin.get_data("task_status")["apply_local_strm"]
        assert "标题精确匹配更新=1" in status["summary"]

    def test_target_subscription_path_migration_when_title_not_matched(self, tmp_path):
        plugin = self._make_plugin(reachable=True)
        plugin._storageplace = str(tmp_path)
        plugin._target_subscription_source = "https://b.example/rss.xml"
        plugin._target_accelerator_source = None
        plugin._client.fetch_one_source = MagicMock(
            return_value=[{"title": "别的标题", "link": "https://alive.example/2026-7/other.mp4?d=mp4"}]
        )
        strm_file = tmp_path / "不在窗口内的老标题.strm"
        strm_file.write_text("https://dead.example/2026-7/老标题.mp4?d=mp4", encoding="utf-8")

        getattr(plugin, "_ANiStrmHub__apply_local_strm_task")()

        assert strm_file.read_text().strip() == "https://alive.example/2026-7/老标题.mp4?d=mp4"
        status = plugin.get_data("task_status")["apply_local_strm"]
        assert "按订阅源迁移更新=1" in status["summary"]

    def test_target_subscription_and_accelerator_combined(self, tmp_path):
        plugin = self._make_plugin(reachable=True)
        plugin._storageplace = str(tmp_path)
        plugin._target_subscription_source = "https://b.example/rss.xml"
        plugin._target_accelerator_source = "https://pro.pili.cc.cd"
        plugin._client.fetch_one_source = MagicMock(
            return_value=[{"title": "示例", "link": "https://resources.ani.rip/2026-7/ep.mp4?d=mp4"}]
        )
        strm_file = tmp_path / "示例.strm"
        strm_file.write_text("https://dead.example/old.mp4?d=mp4", encoding="utf-8")

        getattr(plugin, "_ANiStrmHub__apply_local_strm_task")()

        assert strm_file.read_text().strip() == "https://pro.pili.cc.cd/resources.ani.rip/2026-7/ep.mp4?d=mp4"

    def test_keeps_original_when_probe_fails(self, tmp_path):
        plugin = self._make_plugin(reachable=False)
        plugin._storageplace = str(tmp_path)
        plugin._target_accelerator_source = "https://pro.pili.cc.cd"
        strm_file = tmp_path / "示例.mp4.strm"
        original = "https://resources.ani.rip/2025-10/xxx?d=mp4"
        strm_file.write_text(original, encoding="utf-8")

        getattr(plugin, "_ANiStrmHub__apply_local_strm_task")()

        assert strm_file.read_text().strip() == original
        status = plugin.get_data("task_status")["apply_local_strm"]
        assert "探测不可达(保留原文件)=1" in status["summary"]

    def test_already_in_desired_state_is_a_noop(self, tmp_path):
        plugin = self._make_plugin(reachable=True)
        plugin._storageplace = str(tmp_path)
        plugin._target_accelerator_source = "https://pro.pili.cc.cd"
        strm_file = tmp_path / "示例.mp4.strm"
        already = "https://pro.pili.cc.cd/resources.ani.rip/2025-10/xxx?d=mp4"
        strm_file.write_text(already, encoding="utf-8")

        getattr(plugin, "_ANiStrmHub__apply_local_strm_task")()

        assert strm_file.read_text().strip() == already
        status = plugin.get_data("task_status")["apply_local_strm"]
        assert "无需更新=1" in status["summary"]

    def test_target_subscription_fetch_failure_ends_task_gracefully(self, tmp_path):
        plugin = self._make_plugin(reachable=True)
        plugin._storageplace = str(tmp_path)
        plugin._target_subscription_source = "https://b.example/rss.xml"
        plugin._client.fetch_one_source = MagicMock(side_effect=ValueError("HTTP状态异常：403"))

        getattr(plugin, "_ANiStrmHub__apply_local_strm_task")()

        status = plugin.get_data("task_status")["apply_local_strm"]
        assert "目标订阅源抓取失败" in status["summary"]

    def test_storage_dir_missing_is_a_noop(self, tmp_path):
        plugin = self._make_plugin(reachable=True)
        plugin._storageplace = str(tmp_path / "does-not-exist")

        getattr(plugin, "_ANiStrmHub__apply_local_strm_task")()

        status = plugin.get_data("task_status")["apply_local_strm"]
        assert status["summary"] == "存储目录不存在"


class TestBackfillTask:
    def _make_plugin(self, reachable_down_to: int = 0):
        """reachable_down_to: 探测在这个集数(含)以上都可达，低于它的一律403，
        模拟"回溯到某一集连不上就停"的场景"""
        from urllib.parse import unquote

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

    def test_backfilled_link_gets_active_accelerator_applied(self, tmp_path, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda *_: None)
        plugin = self._make_plugin(reachable_down_to=10)
        plugin._storageplace = str(tmp_path)
        plugin._accelerator_sources = "https://pro.pili.cc.cd"
        existing = tmp_path / "[ANi] 示例 - 11 [1080P][Baha][WEB-DL][AAC AVC][CHT].strm"
        existing.write_text(
            "https://resources.ani.rip/2026-7/"
            "%5BANi%5D%20%E7%A4%BA%E4%BE%8B%20-%2011%20%5B1080P%5D%5BBaha%5D%5BWEB-DL%5D%5BAAC%20AVC%5D%5BCHT%5D?d=mp4",
            encoding="utf-8",
        )

        getattr(plugin, "_ANiStrmHub__backfill_task")()

        created = [p for p in tmp_path.glob("*.strm") if p != existing]
        assert len(created) == 1
        assert created[0].read_text().startswith("https://pro.pili.cc.cd/resources.ani.rip/")

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

        assert existing_10.read_text() == "https://already-have.example/ep10?d=mp4"

    def test_no_recognizable_series_is_a_noop(self, tmp_path):
        plugin = ANiStrmHub()
        plugin._storageplace = str(tmp_path)
        (tmp_path / "没有集数格式.strm").write_text("https://example.com/x?d=mp4", encoding="utf-8")

        getattr(plugin, "_ANiStrmHub__backfill_task")()

        status = plugin.get_data("task_status")["backfill"]
        assert status["summary"] == "本地无可识别集数的资源"


class TestDetectTask:
    def test_builds_connectivity_matrix_and_local_distribution(self, tmp_path):
        plugin = ANiStrmHub()
        plugin._storageplace = str(tmp_path)
        plugin._subscription_sources = "https://a.example/rss.xml"
        plugin._accelerator_sources = "https://pro.pili.cc.cd"
        plugin._client.fetch_one_source = MagicMock(
            return_value=[{"title": "示例", "link": "https://resources.ani.rip/2026-7/ep.mp4?d=mp4"}]
        )
        plugin._relink_service.probe_latency_ms = MagicMock(return_value=(50.0, None))
        (tmp_path / "示例.strm").write_text(
            "https://pro.pili.cc.cd/resources.ani.rip/2026-7/ep.mp4?d=mp4", encoding="utf-8"
        )

        getattr(plugin, "_ANiStrmHub__detect_task")()

        matrix = plugin.get_data("connectivity_matrix")
        assert matrix["rows"][0]["subscription"] == "https://a.example/rss.xml"
        assert matrix["rows"][0]["columns"] == [
            {"label": "直连", "latency_ms": 50.0, "error": None},
            {"label": "https://pro.pili.cc.cd", "latency_ms": 50.0, "error": None},
        ]

        distribution = plugin.get_data("local_distribution")
        assert distribution["total"] == 1
        assert distribution["by_category"] == {"https://a.example/rss.xml + https://pro.pili.cc.cd": 1}

    def test_no_subscription_sources_is_a_noop(self):
        plugin = ANiStrmHub()
        plugin._subscription_sources = ""

        getattr(plugin, "_ANiStrmHub__detect_task")()

        status = plugin.get_data("task_status")["detect"]
        assert status["summary"] == "未配置订阅源"
        assert plugin.get_data("connectivity_matrix") is None

    def test_rss_fetch_failure_recorded_per_row(self):
        plugin = ANiStrmHub()
        plugin._subscription_sources = "https://broken.example/rss.xml"
        plugin._storageplace = "/does-not-exist"
        plugin._client.fetch_one_source = MagicMock(side_effect=ValueError("HTTP状态异常：403"))

        getattr(plugin, "_ANiStrmHub__detect_task")()

        matrix = plugin.get_data("connectivity_matrix")
        assert matrix["rows"][0]["rss_ok"] is False
        assert "403" in matrix["rows"][0]["error"]
