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
    FALLBACK_SUBSCRIPTION_POOL,
    LAYOUT_BY_TITLE,
    LAYOUT_FLAT,
    OFFICIAL_BASE_URL,
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

# mp4容器的最小合法魔数字节：偏移4-8是'ftyp'
MP4_MAGIC_CONTENT = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 40


def _video_response(status_code=206):
    return MagicMock(status_code=status_code, headers={"Content-Type": "video/mp4"}, content=MP4_MAGIC_CONTENT)


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


class TestEpisodeAndSeasonVariant:
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

    def test_build_season_variant_link_replaces_season_keeps_rest(self):
        original = "https://pro.pili.cc.cd/resources.ani.rip/2026-7/ep.mp4?d=mp4"
        result = StrmRelinkService.build_season_variant_link(original, "2026-4")
        assert result == "https://pro.pili.cc.cd/resources.ani.rip/2026-4/ep.mp4?d=mp4"

    def test_build_season_variant_link_returns_none_when_no_season_pattern(self):
        assert StrmRelinkService.build_season_variant_link("https://example.com/no-season-here", "2026-4") is None

    def test_season_distance_same_year(self):
        assert StrmRelinkService.season_distance("2026-7", "2026-4") == 3

    def test_season_distance_across_year_boundary(self):
        assert StrmRelinkService.season_distance("2026-1", "2025-10") == 3

    def test_season_distance_zero_for_same_season(self):
        assert StrmRelinkService.season_distance("2026-7", "2026-7") == 0


class TestProbeLatencyAndSpeed:
    def test_probe_latency_ms_success_with_valid_video_content(self):
        request_utils = MagicMock()
        request_utils.get_res.return_value = _video_response()
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

    def test_probe_latency_ms_rejects_html_error_page_despite_200(self):
        # 回归测试：光看HTTP状态码不够，服务器可能返回200/206但吐的是错误页
        request_utils = MagicMock()
        request_utils.get_res.return_value = MagicMock(
            status_code=200,
            headers={"Content-Type": "text/html"},
            content=b"<!doctype html><html><body>404 Not Found</body></html>",
        )
        service = StrmRelinkService(request_factory=lambda: request_utils)

        latency_ms, reason = service.probe_latency_ms("https://fake-200.example/ep.mp4?d=mp4")

        assert latency_ms is None
        assert "不是视频数据" in reason

    def test_probe_latency_ms_rejects_html_by_content_even_without_content_type(self):
        request_utils = MagicMock()
        request_utils.get_res.return_value = MagicMock(
            status_code=206,
            headers={},
            content=b"<html><body>error</body></html>",
        )
        service = StrmRelinkService(request_factory=lambda: request_utils)

        latency_ms, reason = service.probe_latency_ms("https://fake-206.example/ep.mp4?d=mp4")

        assert latency_ms is None

    def test_probe_latency_ms_accepts_mp4_magic_bytes(self):
        request_utils = MagicMock()
        request_utils.get_res.return_value = _video_response()
        service = StrmRelinkService(request_factory=lambda: request_utils)

        latency_ms, reason = service.probe_latency_ms("https://real-video.example/ep.mp4?d=mp4")

        assert latency_ms is not None
        assert reason is None

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
        request_utils.get_res.return_value = _video_response()
        service = StrmRelinkService(request_factory=lambda: request_utils)

        assert service._verify_reachable("https://alive.example/ep.mp4?d=mp4") is True
        request_utils.update_headers.assert_called_once_with({"Range": "bytes=0-63"})
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
            domain_to_source={"resources.ani.rip": "当前配置"},
            accelerator_prefixes=["https://pro.pili.cc.cd"],
        )

        assert result["total"] == 3
        assert result["by_category"] == {
            "当前配置 + https://pro.pili.cc.cd": 2,
            "当前配置 裸链": 1,
        }

    def test_unrecognized_domain_falls_back_to_domain_label(self, tmp_path):
        (tmp_path / "a.strm").write_text("https://unknown.example/x?d=mp4", encoding="utf-8")
        result = StrmRelinkService.scan_local_distribution(str(tmp_path), {}, [])
        assert result["by_category"] == {"unknown.example 裸链": 1}

    def test_empty_directory_returns_zero(self, tmp_path):
        result = StrmRelinkService.scan_local_distribution(str(tmp_path / "does-not-exist"), {}, [])
        assert result == {"total": 0, "by_category": {}}

    def test_none_storage_path_returns_zero(self):
        result = StrmRelinkService.scan_local_distribution(None, {}, [])
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
    def test_is_subtitle_file(self):
        assert StrmFileService.is_subtitle_file("[ANi] 示例 - 01.srt") is True
        assert StrmFileService.is_subtitle_file("[ANi] 示例 - 01.ASS") is True
        assert StrmFileService.is_subtitle_file("[ANi] 示例 - 01.mp4") is False

    def test_is_blacklisted(self):
        assert StrmFileService.is_blacklisted("[ANi] 示例 PV [1080P].mp4", "预告@PV@NCOP") is True
        assert StrmFileService.is_blacklisted("[ANi] 示例 - 01 [1080P].mp4", "预告@PV@NCOP") is False

    def test_is_blacklisted_empty_config_never_matches(self):
        assert StrmFileService.is_blacklisted("随便什么标题", "") is False


class TestExtractSeriesTitle:
    @pytest.mark.parametrize(
        "file_name,expected",
        [
            ("[ANi] 盡墳王 - 11 [1080P][Baha][WEB-DL][AAC AVC][CHT]", "盡墳王"),
            (
                "[ANi] SPY×FAMILY 間諜家家酒 Season 3 - 46 [1080P][Baha][WEB-DL][AAC AVC][CHT]",
                "SPY×FAMILY 間諜家家酒 Season 3",
            ),
            # 半集：集数不是整数，只认整数的EPISODE_NUM_RE会漏掉，这里要能切出剧名
            ("[ANi] 史萊姆 第四季 - 12.5 [1080P][Baha][WEB-DL][AAC AVC][CHT]", "史萊姆 第四季"),
            # 剧场版：集数位置压根不是数字
            ("[ANi] 某劇場版 - 電影 [1080P][Baha][WEB-DL][AAC AVC][CHT]", "某劇場版"),
            # 剧名自带" - "：贪婪匹配取最后一个分隔点，不能把剧名截成前半截
            ("[ANi] Fate - Grand Order - 10 [1080P][Baha][WEB-DL][AAC AVC][CHT]", "Fate - Grand Order"),
            # 文件名尾部带.mp4不影响剧名切分
            ("[ANi] 一拳超人 第三季 - 25 [1080P][Baha][WEB-DL][AAC AVC][CHT].mp4", "一拳超人 第三季"),
            # 没有发布组标签也能切
            ("沒有發布組標籤 - 03 [1080P][Baha]", "沒有發布組標籤"),
        ],
    )
    def test_extracts_series_title(self, file_name, expected):
        assert StrmFileService.extract_series_title(file_name) == expected

    def test_returns_none_for_unrecognized_name(self):
        assert StrmFileService.extract_series_title("完全不符合格式的名字") is None

    def test_safe_dir_name_blocks_path_separators(self):
        assert StrmFileService.safe_dir_name("剧名/带斜杠") == "剧名_带斜杠"
        assert StrmFileService.safe_dir_name("  两边空格  ") == "两边空格"


class TestResolveRelativeDir:
    def test_flat_layout_returns_none(self):
        plugin = ANiStrmHub()
        plugin._strm_layout = LAYOUT_FLAT
        result = getattr(plugin, "_ANiStrmHub__resolve_relative_dir")(
            "[ANi] 盡墳王 - 11 [1080P][Baha][WEB-DL][AAC AVC][CHT]"
        )
        assert result is None

    def test_by_title_layout_returns_series_folder(self):
        plugin = ANiStrmHub()
        plugin._strm_layout = LAYOUT_BY_TITLE
        result = getattr(plugin, "_ANiStrmHub__resolve_relative_dir")(
            "[ANi] 盡墳王 - 11 [1080P][Baha][WEB-DL][AAC AVC][CHT]"
        )
        assert result == "盡墳王"

    def test_by_title_layout_falls_back_to_root_when_unrecognized(self):
        plugin = ANiStrmHub()
        plugin._strm_layout = LAYOUT_BY_TITLE
        result = getattr(plugin, "_ANiStrmHub__resolve_relative_dir")("完全不符合格式的名字")
        assert result is None


class TestRegroupLocalStrmTask:
    RAW_NAMES = [
        "[ANi] 盡墳王 - 11 [1080P][Baha][WEB-DL][AAC AVC][CHT].strm",
        "[ANi] 盡墳王 - 10 [1080P][Baha][WEB-DL][AAC AVC][CHT].strm",
        "[ANi] 一拳超人 第三季 - 25 [1080P][Baha][WEB-DL][AAC AVC][CHT].strm",
    ]

    def _seed_flat(self, tmp_path):
        for name in self.RAW_NAMES:
            (tmp_path / name).write_text("https://resources.ani.rip/2026-7/x?d=mp4", encoding="utf-8")

    def test_groups_flat_files_into_series_folders(self, tmp_path):
        plugin = ANiStrmHub()
        plugin._storageplace = str(tmp_path)
        plugin._strm_layout = LAYOUT_BY_TITLE
        self._seed_flat(tmp_path)

        getattr(plugin, "_ANiStrmHub__regroup_local_strm_task")()

        assert (tmp_path / "盡墳王").is_dir()
        assert len(list((tmp_path / "盡墳王").glob("*.strm"))) == 2
        assert len(list((tmp_path / "一拳超人 第三季").glob("*.strm"))) == 1
        assert list(tmp_path.glob("*.strm")) == []
        status = plugin.get_data("task_status")["regroup"]
        assert "已归档=3" in status["summary"]

    def test_flattens_series_folders_back_to_root(self, tmp_path):
        plugin = ANiStrmHub()
        plugin._storageplace = str(tmp_path)
        plugin._strm_layout = LAYOUT_FLAT
        series_dir = tmp_path / "盡墳王"
        series_dir.mkdir()
        (series_dir / self.RAW_NAMES[0]).write_text("https://resources.ani.rip/2026-7/x?d=mp4", encoding="utf-8")

        getattr(plugin, "_ANiStrmHub__regroup_local_strm_task")()

        assert (tmp_path / self.RAW_NAMES[0]).exists()
        # 搬空的子目录要清理掉，不留一堆空文件夹
        assert not series_dir.exists()

    def test_migrates_out_of_legacy_season_folders(self, tmp_path):
        # 历史版本按"季度目录"存放过，重新归档要能把这些文件也收进剧名文件夹
        plugin = ANiStrmHub()
        plugin._storageplace = str(tmp_path)
        plugin._strm_layout = LAYOUT_BY_TITLE
        season_dir = tmp_path / "2026-7"
        season_dir.mkdir()
        (season_dir / self.RAW_NAMES[0]).write_text("https://resources.ani.rip/2026-7/x?d=mp4", encoding="utf-8")

        getattr(plugin, "_ANiStrmHub__regroup_local_strm_task")()

        assert (tmp_path / "盡墳王" / self.RAW_NAMES[0]).exists()
        assert not season_dir.exists()

    def test_already_correct_position_is_untouched(self, tmp_path):
        plugin = ANiStrmHub()
        plugin._storageplace = str(tmp_path)
        plugin._strm_layout = LAYOUT_BY_TITLE
        series_dir = tmp_path / "盡墳王"
        series_dir.mkdir()
        (series_dir / self.RAW_NAMES[0]).write_text("https://resources.ani.rip/2026-7/x?d=mp4", encoding="utf-8")

        getattr(plugin, "_ANiStrmHub__regroup_local_strm_task")()

        assert (series_dir / self.RAW_NAMES[0]).exists()
        status = plugin.get_data("task_status")["regroup"]
        assert "位置已正确=1" in status["summary"]

    def test_unrecognized_name_flattens_to_root_instead_of_being_left_behind(self, tmp_path):
        plugin = ANiStrmHub()
        plugin._storageplace = str(tmp_path)
        plugin._strm_layout = LAYOUT_BY_TITLE
        season_dir = tmp_path / "2026-7"
        season_dir.mkdir()
        (season_dir / "完全不符合格式的名字.strm").write_text("https://x.example/y?d=mp4", encoding="utf-8")

        getattr(plugin, "_ANiStrmHub__regroup_local_strm_task")()

        assert (tmp_path / "完全不符合格式的名字.strm").exists()
        status = plugin.get_data("task_status")["regroup"]
        assert "识别不出剧名(平铺到根目录)=1" in status["summary"]

    def test_existing_target_is_not_overwritten(self, tmp_path):
        plugin = ANiStrmHub()
        plugin._storageplace = str(tmp_path)
        plugin._strm_layout = LAYOUT_BY_TITLE
        (tmp_path / self.RAW_NAMES[0]).write_text("新的内容", encoding="utf-8")
        series_dir = tmp_path / "盡墳王"
        series_dir.mkdir()
        (series_dir / self.RAW_NAMES[0]).write_text("早就在文件夹里的内容", encoding="utf-8")

        getattr(plugin, "_ANiStrmHub__regroup_local_strm_task")()

        assert (series_dir / self.RAW_NAMES[0]).read_text(encoding="utf-8") == "早就在文件夹里的内容"
        assert (tmp_path / self.RAW_NAMES[0]).read_text(encoding="utf-8") == "新的内容"
        status = plugin.get_data("task_status")["regroup"]
        assert "目标已存在(保留原文件)=1" in status["summary"]

    def test_storage_dir_missing_is_a_noop(self, tmp_path):
        plugin = ANiStrmHub()
        plugin._storageplace = str(tmp_path / "does-not-exist")

        getattr(plugin, "_ANiStrmHub__regroup_local_strm_task")()

        status = plugin.get_data("task_status")["regroup"]
        assert status["summary"] == "存储目录不存在"


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


class TestSubscriptionCandidates:
    def test_primary_first_then_pool_without_duplicate(self):
        plugin = ANiStrmHub()
        primary = "https://aniapi.op5.de5.net/ani-download.xml"
        result = getattr(plugin, "_ANiStrmHub__subscription_candidates")(primary)
        assert result[0] == primary
        assert result.count(primary) == 1
        assert set(result) == set(FALLBACK_SUBSCRIPTION_POOL)

    def test_unknown_primary_prepended_to_full_pool(self):
        plugin = ANiStrmHub()
        primary = "https://my-own-mirror.example/ani-download.xml"
        result = getattr(plugin, "_ANiStrmHub__subscription_candidates")(primary)
        assert result[0] == primary
        assert set(result[1:]) == set(FALLBACK_SUBSCRIPTION_POOL)


class TestResolveAcceleratorForRun:
    def test_returns_none_when_not_configured(self):
        plugin = ANiStrmHub()
        plugin._accelerator_prefix = ""
        result = getattr(plugin, "_ANiStrmHub__resolve_accelerator_for_this_run")("https://x.example/y?d=mp4")
        assert result is None

    def test_returns_prefix_when_probe_succeeds(self):
        plugin = ANiStrmHub()
        plugin._accelerator_prefix = "https://pro.pili.cc.cd"
        plugin._relink_service.probe_latency_ms = MagicMock(return_value=(50.0, None))
        result = getattr(plugin, "_ANiStrmHub__resolve_accelerator_for_this_run")("https://x.example/y?d=mp4")
        assert result == "https://pro.pili.cc.cd"

    def test_returns_none_when_probe_fails(self):
        plugin = ANiStrmHub()
        plugin._accelerator_prefix = "https://pro.pili.cc.cd"
        plugin._relink_service.probe_latency_ms = MagicMock(return_value=(None, "HTTP 403"))
        result = getattr(plugin, "_ANiStrmHub__resolve_accelerator_for_this_run")("https://x.example/y?d=mp4")
        assert result is None


class TestFinalizeStrmLink:
    def test_wraps_with_configured_accelerator(self):
        plugin = ANiStrmHub()
        plugin._accelerator_prefix = "https://pro.pili.cc.cd"
        result = getattr(plugin, "_ANiStrmHub__finalize_strm_link")("https://resources.ani.rip/x?d=mp4")
        assert result == "https://pro.pili.cc.cd/resources.ani.rip/x?d=mp4"

    def test_no_accelerator_configured_returns_bare_link(self):
        plugin = ANiStrmHub()
        plugin._accelerator_prefix = ""
        result = getattr(plugin, "_ANiStrmHub__finalize_strm_link")("https://resources.ani.rip/x?d=mp4")
        assert result == "https://resources.ani.rip/x?d=mp4"


class TestTaskStatusGuard:
    def test_running_task_is_detected_and_done_is_not(self):
        plugin = ANiStrmHub()
        getattr(plugin, "_ANiStrmHub__save_task_status")("refresh_subscription", "running", "进行中")
        assert getattr(plugin, "_ANiStrmHub__is_task_running")("refresh_subscription") is True

        getattr(plugin, "_ANiStrmHub__save_task_status")("refresh_subscription", "done", "跑完了")
        assert getattr(plugin, "_ANiStrmHub__is_task_running")("refresh_subscription") is False

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
    def test_uses_primary_source_when_reachable(self, tmp_path):
        plugin = ANiStrmHub()
        plugin._subscription_source = "https://a.example/rss.xml"
        plugin._storageplace = str(tmp_path)
        plugin._client.fetch_one_source = MagicMock(
            return_value=[{"title": "示例", "link": "https://resources.ani.rip/2026-7/ep.mp4?d=mp4"}]
        )

        getattr(plugin, "_ANiStrmHub__task")()

        plugin._client.fetch_one_source.assert_called_once_with("https://a.example/rss.xml")
        written = (tmp_path / "示例.strm").read_text(encoding="utf-8")
        assert written == "https://resources.ani.rip/2026-7/ep.mp4?d=mp4"

    def test_falls_back_to_pool_when_primary_fetch_fails(self, tmp_path):
        plugin = ANiStrmHub()
        plugin._subscription_source = "https://dead.example/rss.xml"
        plugin._storageplace = str(tmp_path)

        def fake_fetch(url):
            if url == "https://dead.example/rss.xml":
                raise ValueError("HTTP状态异常：403")
            return [{"title": "示例", "link": "https://resources.ani.rip/2026-7/ep.mp4?d=mp4"}]

        plugin._client.fetch_one_source = MagicMock(side_effect=fake_fetch)

        getattr(plugin, "_ANiStrmHub__task")()

        assert (tmp_path / "示例.strm").exists()
        status = plugin.get_data("task_status")["task"]
        assert status["status"] == "done"

    def test_all_candidates_fail_records_status(self, tmp_path):
        plugin = ANiStrmHub()
        plugin._subscription_source = "https://dead.example/rss.xml"
        plugin._storageplace = str(tmp_path)
        plugin._client.fetch_one_source = MagicMock(side_effect=ValueError("HTTP状态异常：403"))

        getattr(plugin, "_ANiStrmHub__task")()

        status = plugin.get_data("task_status")["task"]
        assert "全部" in status["summary"] and "不可用" in status["summary"]
        assert list(tmp_path.glob("*.strm")) == []

    def test_accelerator_auto_disabled_when_precheck_fails(self, tmp_path):
        plugin = ANiStrmHub()
        plugin._subscription_source = "https://a.example/rss.xml"
        plugin._accelerator_prefix = "https://pro.pili.cc.cd"
        plugin._storageplace = str(tmp_path)
        plugin._client.fetch_one_source = MagicMock(
            return_value=[{"title": "示例", "link": "https://resources.ani.rip/2026-7/ep.mp4?d=mp4"}]
        )
        plugin._relink_service.probe_latency_ms = MagicMock(return_value=(None, "HTTP 403"))

        getattr(plugin, "_ANiStrmHub__task")()

        written = (tmp_path / "示例.strm").read_text(encoding="utf-8")
        assert written == "https://resources.ani.rip/2026-7/ep.mp4?d=mp4"

    def test_accelerator_applied_when_precheck_succeeds(self, tmp_path):
        plugin = ANiStrmHub()
        plugin._subscription_source = "https://a.example/rss.xml"
        plugin._accelerator_prefix = "https://pro.pili.cc.cd"
        plugin._storageplace = str(tmp_path)
        plugin._client.fetch_one_source = MagicMock(
            return_value=[{"title": "示例", "link": "https://resources.ani.rip/2026-7/ep.mp4?d=mp4"}]
        )
        plugin._relink_service.probe_latency_ms = MagicMock(return_value=(50.0, None))

        getattr(plugin, "_ANiStrmHub__task")()

        written = (tmp_path / "示例.strm").read_text(encoding="utf-8")
        assert written == "https://pro.pili.cc.cd/resources.ani.rip/2026-7/ep.mp4?d=mp4"

    def test_flat_layout_writes_into_storage_root(self, tmp_path):
        plugin = ANiStrmHub()
        plugin._subscription_source = "https://a.example/rss.xml"
        plugin._storageplace = str(tmp_path)
        plugin._strm_layout = LAYOUT_FLAT
        plugin._client.fetch_one_source = MagicMock(
            return_value=[
                {
                    "title": "[ANi] 盡墳王 - 11 [1080P][Baha][WEB-DL][AAC AVC][CHT]",
                    "link": "https://resources.ani.rip/2026-7/ep.mp4?d=mp4",
                }
            ]
        )

        getattr(plugin, "_ANiStrmHub__task")()

        assert (tmp_path / "[ANi] 盡墳王 - 11 [1080P][Baha][WEB-DL][AAC AVC][CHT].strm").exists()

    def test_by_title_layout_writes_into_series_folder(self, tmp_path):
        plugin = ANiStrmHub()
        plugin._subscription_source = "https://a.example/rss.xml"
        plugin._storageplace = str(tmp_path)
        plugin._strm_layout = LAYOUT_BY_TITLE
        plugin._client.fetch_one_source = MagicMock(
            return_value=[
                {
                    "title": "[ANi] 盡墳王 - 11 [1080P][Baha][WEB-DL][AAC AVC][CHT]",
                    "link": "https://resources.ani.rip/2026-7/ep.mp4?d=mp4",
                }
            ]
        )

        getattr(plugin, "_ANiStrmHub__task")()

        assert (
            tmp_path / "盡墳王" / "[ANi] 盡墳王 - 11 [1080P][Baha][WEB-DL][AAC AVC][CHT].strm"
        ).exists()

    def test_skips_subtitle_and_blacklisted_entries(self, tmp_path):
        plugin = ANiStrmHub()
        plugin._subscription_source = "https://a.example/rss.xml"
        plugin._storageplace = str(tmp_path)
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


class TestRefreshSubscriptionTask:
    def _make_plugin(self, reachable: bool = True):
        plugin = ANiStrmHub()
        request_utils = MagicMock()
        request_utils.get_res.return_value = _video_response(206 if reachable else 403)
        plugin._relink_service._request_factory = lambda: request_utils
        return plugin

    def test_title_match_uses_latest_link(self, tmp_path):
        plugin = self._make_plugin(reachable=True)
        plugin._storageplace = str(tmp_path)
        plugin._client.fetch_one_source = MagicMock(
            return_value=[{"title": "在窗口内的标题", "link": "https://alive.example/new.mp4?d=mp4"}]
        )
        strm_file = tmp_path / "在窗口内的标题.strm"
        strm_file.write_text("https://dead.example/old.mp4?d=mp4", encoding="utf-8")

        getattr(plugin, "_ANiStrmHub__refresh_subscription_task")()

        assert strm_file.read_text().strip() == "https://alive.example/new.mp4?d=mp4"
        status = plugin.get_data("task_status")["refresh_subscription"]
        assert "标题精确匹配更新=1" in status["summary"]

    def test_path_migration_when_title_not_matched(self, tmp_path):
        plugin = self._make_plugin(reachable=True)
        plugin._storageplace = str(tmp_path)
        plugin._client.fetch_one_source = MagicMock(
            return_value=[{"title": "别的标题", "link": "https://alive.example/2026-7/other.mp4?d=mp4"}]
        )
        strm_file = tmp_path / "不在窗口内的老标题.strm"
        strm_file.write_text("https://dead.example/2026-7/老标题.mp4?d=mp4", encoding="utf-8")

        getattr(plugin, "_ANiStrmHub__refresh_subscription_task")()

        assert strm_file.read_text().strip() == "https://alive.example/2026-7/老标题.mp4?d=mp4"
        status = plugin.get_data("task_status")["refresh_subscription"]
        assert "路径迁移更新=1" in status["summary"]

    def test_applies_configured_accelerator_after_refresh(self, tmp_path):
        plugin = self._make_plugin(reachable=True)
        plugin._storageplace = str(tmp_path)
        plugin._accelerator_prefix = "https://pro.pili.cc.cd"
        plugin._client.fetch_one_source = MagicMock(
            return_value=[{"title": "示例", "link": "https://resources.ani.rip/2026-7/ep.mp4?d=mp4"}]
        )
        strm_file = tmp_path / "示例.strm"
        strm_file.write_text("https://dead.example/old.mp4?d=mp4", encoding="utf-8")

        getattr(plugin, "_ANiStrmHub__refresh_subscription_task")()

        assert strm_file.read_text().strip() == "https://pro.pili.cc.cd/resources.ani.rip/2026-7/ep.mp4?d=mp4"

    def test_keeps_original_when_probe_fails(self, tmp_path):
        plugin = self._make_plugin(reachable=False)
        plugin._storageplace = str(tmp_path)
        plugin._client.fetch_one_source = MagicMock(
            return_value=[{"title": "在窗口内的标题", "link": "https://alive.example/new.mp4?d=mp4"}]
        )
        strm_file = tmp_path / "在窗口内的标题.strm"
        original = "https://dead.example/old.mp4?d=mp4"
        strm_file.write_text(original, encoding="utf-8")

        getattr(plugin, "_ANiStrmHub__refresh_subscription_task")()

        assert strm_file.read_text().strip() == original
        status = plugin.get_data("task_status")["refresh_subscription"]
        assert "探测不可达(保留原文件)=1" in status["summary"]

    def test_subscription_fetch_failure_ends_task_gracefully(self, tmp_path):
        plugin = self._make_plugin(reachable=True)
        plugin._storageplace = str(tmp_path)
        plugin._client.fetch_one_source = MagicMock(side_effect=ValueError("HTTP状态异常：403"))

        getattr(plugin, "_ANiStrmHub__refresh_subscription_task")()

        status = plugin.get_data("task_status")["refresh_subscription"]
        assert "订阅源抓取失败" in status["summary"]

    def test_storage_dir_missing_is_a_noop(self, tmp_path):
        plugin = self._make_plugin(reachable=True)
        plugin._storageplace = str(tmp_path / "does-not-exist")

        getattr(plugin, "_ANiStrmHub__refresh_subscription_task")()

        status = plugin.get_data("task_status")["refresh_subscription"]
        assert status["summary"] == "存储目录不存在"


class TestApplyAcceleratorTask:
    def _make_plugin(self, reachable: bool = True):
        plugin = ANiStrmHub()
        request_utils = MagicMock()
        request_utils.get_res.return_value = _video_response(206 if reachable else 403)
        plugin._relink_service._request_factory = lambda: request_utils
        return plugin

    def test_wraps_bare_link_with_configured_accelerator(self, tmp_path):
        plugin = self._make_plugin(reachable=True)
        plugin._storageplace = str(tmp_path)
        plugin._accelerator_prefix = "https://pro.pili.cc.cd"
        strm_file = tmp_path / "示例.strm"
        strm_file.write_text("https://resources.ani.rip/2025-10/xxx?d=mp4", encoding="utf-8")

        getattr(plugin, "_ANiStrmHub__apply_accelerator_task")()

        assert strm_file.read_text().strip() == "https://pro.pili.cc.cd/resources.ani.rip/2025-10/xxx?d=mp4"
        status = plugin.get_data("task_status")["apply_accelerator"]
        assert "已套上加速源=1" in status["summary"]

    def test_restores_previously_wrapped_link_when_accelerator_cleared(self, tmp_path):
        # 回归测试：还原不依赖"记住"当初套的是哪个加速源，靠extract_resource_path
        # 定位资源路径+官方域名重建，所以清空加速源字段也能正确还原
        plugin = self._make_plugin(reachable=True)
        plugin._storageplace = str(tmp_path)
        plugin._accelerator_prefix = ""
        strm_file = tmp_path / "示例.strm"
        strm_file.write_text(
            "https://pro.pili.cc.cd/resources.ani.rip/2025-10/xxx?d=mp4", encoding="utf-8"
        )

        getattr(plugin, "_ANiStrmHub__apply_accelerator_task")()

        assert strm_file.read_text().strip() == f"{OFFICIAL_BASE_URL}/2025-10/xxx?d=mp4"
        status = plugin.get_data("task_status")["apply_accelerator"]
        assert "已还原为裸链接=1" in status["summary"]

    def test_switches_from_one_accelerator_to_another(self, tmp_path):
        plugin = self._make_plugin(reachable=True)
        plugin._storageplace = str(tmp_path)
        plugin._accelerator_prefix = "https://pro.op5.de5.net"
        strm_file = tmp_path / "示例.strm"
        strm_file.write_text(
            "https://pro.pili.cc.cd/resources.ani.rip/2025-10/xxx?d=mp4", encoding="utf-8"
        )

        getattr(plugin, "_ANiStrmHub__apply_accelerator_task")()

        assert strm_file.read_text().strip() == "https://pro.op5.de5.net/resources.ani.rip/2025-10/xxx?d=mp4"

    def test_keeps_original_when_probe_fails(self, tmp_path):
        plugin = self._make_plugin(reachable=False)
        plugin._storageplace = str(tmp_path)
        plugin._accelerator_prefix = "https://pro.pili.cc.cd"
        strm_file = tmp_path / "示例.strm"
        original = "https://resources.ani.rip/2025-10/xxx?d=mp4"
        strm_file.write_text(original, encoding="utf-8")

        getattr(plugin, "_ANiStrmHub__apply_accelerator_task")()

        assert strm_file.read_text().strip() == original
        status = plugin.get_data("task_status")["apply_accelerator"]
        assert "探测不可达(保留原文件)=1" in status["summary"]

    def test_already_in_desired_state_is_a_noop(self, tmp_path):
        plugin = self._make_plugin(reachable=True)
        plugin._storageplace = str(tmp_path)
        plugin._accelerator_prefix = "https://pro.pili.cc.cd"
        strm_file = tmp_path / "示例.strm"
        already = "https://pro.pili.cc.cd/resources.ani.rip/2025-10/xxx?d=mp4"
        strm_file.write_text(already, encoding="utf-8")

        getattr(plugin, "_ANiStrmHub__apply_accelerator_task")()

        assert strm_file.read_text().strip() == already
        status = plugin.get_data("task_status")["apply_accelerator"]
        assert "无需更新=1" in status["summary"]

    def test_storage_dir_missing_is_a_noop(self, tmp_path):
        plugin = self._make_plugin(reachable=True)
        plugin._storageplace = str(tmp_path / "does-not-exist")

        getattr(plugin, "_ANiStrmHub__apply_accelerator_task")()

        status = plugin.get_data("task_status")["apply_accelerator"]
        assert status["summary"] == "存储目录不存在"


class TestBackfillTask:
    def _make_plugin(self, reachable_seasons=None, reachable_down_to=0):
        """reachable_seasons: {season: min_reachable_episode}，只有这个季度
        文件夹里 >= min_reachable_episode 的集数才算可达；不在字典里的季度
        一律不可达。默认(None)时退化成单季度场景：reachable_down_to以上都
        可达，用当前季度。"""
        from urllib.parse import unquote

        plugin = ANiStrmHub()
        request_utils = MagicMock()

        def get_res_side_effect(url, **kwargs):
            decoded = unquote(url)
            ep_match = EPISODE_NUM_RE.search(decoded)
            ep = int(ep_match.group(2)) if ep_match else 0
            season_match = __import__("re").search(r"/(\d{4}-\d{1,2})/", decoded)
            season = season_match.group(1) if season_match else None

            if reachable_seasons is not None:
                min_ep = reachable_seasons.get(season)
                ok = min_ep is not None and ep >= min_ep
            else:
                ok = ep >= reachable_down_to

            return _video_response(206 if ok else 403)

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

    def test_stops_at_first_unreachable_episode_when_no_other_season_available(self, tmp_path, monkeypatch):
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
        assert "成功补齐0集" in status["summary"]

    def test_tries_other_known_season_folder_before_giving_up(self, tmp_path, monkeypatch):
        # 回归测试：真实数据确认过同一部剧更早的集数可能落在更早的季度文件夹
        # (一拳超人S3真实起点25集、SPY×FAMILY S3真实起点38集)。当前季度文件夹
        # 探测不通时，不能直接弃剧，要尝试本地已知的其它季度文件夹。
        monkeypatch.setattr(time, "sleep", lambda *_: None)
        # 当前季度(2026-7)只有11集可达；更早的季度(2026-4)从第10集往下都可达
        plugin = self._make_plugin(reachable_seasons={"2026-7": 11, "2026-4": 1})
        plugin._storageplace = str(tmp_path)
        existing_current = tmp_path / "[ANi] 示例 - 11 [1080P][Baha][WEB-DL][AAC AVC][CHT].strm"
        existing_current.write_text(
            "https://pro.pili.cc.cd/resources.ani.rip/2026-7/"
            "%5BANi%5D%20%E7%A4%BA%E4%BE%8B%20-%2011%20%5B1080P%5D%5BBaha%5D%5BWEB-DL%5D%5BAAC%20AVC%5D%5BCHT%5D?d=mp4",
            encoding="utf-8",
        )
        # 本地已经有一个2026-4季度文件夹的痕迹，让resource_scan能认识这个季度
        other_season_hint = tmp_path / "[ANi] 别的剧 - 01 [1080P][Baha][WEB-DL][AAC AVC][CHT].strm"
        other_season_hint.write_text(
            "https://pro.pili.cc.cd/resources.ani.rip/2026-4/"
            "%5BANi%5D%20%E5%88%AB%E7%9A%84%E5%89%A7%20-%2001%20%5B1080P%5D%5BBaha%5D%5BWEB-DL%5D%5BAAC%20AVC%5D%5BCHT%5D?d=mp4",
            encoding="utf-8",
        )

        getattr(plugin, "_ANiStrmHub__backfill_task")()

        created = tmp_path / "[ANi] 示例 - 10 [1080P][Baha][WEB-DL][AAC AVC][CHT].strm"
        assert created.exists()
        assert "/2026-4/" in created.read_text()

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

    def test_backfilled_file_stays_in_reference_files_folder(self, tmp_path, monkeypatch):
        # 资源补齐用ref_file.with_name()，天然跟参照文件同目录——按番剧名称
        # 聚合时补出来的集数会落在同一个剧名文件夹里，不会掉到根目录，
        # 所以这个任务不需要自己再解析一遍剧名(各写各的正是分裂的来源)
        monkeypatch.setattr(time, "sleep", lambda *_: None)
        plugin = self._make_plugin(reachable_down_to=10)
        plugin._storageplace = str(tmp_path)
        plugin._strm_layout = LAYOUT_BY_TITLE
        series_dir = tmp_path / "示例"
        series_dir.mkdir()
        existing = series_dir / "[ANi] 示例 - 11 [1080P][Baha][WEB-DL][AAC AVC][CHT].strm"
        existing.write_text(
            "https://pro.pili.cc.cd/resources.ani.rip/2026-7/"
            "%5BANi%5D%20%E7%A4%BA%E4%BE%8B%20-%2011%20%5B1080P%5D%5BBaha%5D%5BWEB-DL%5D%5BAAC%20AVC%5D%5BCHT%5D?d=mp4",
            encoding="utf-8",
        )

        getattr(plugin, "_ANiStrmHub__backfill_task")()

        assert (series_dir / "[ANi] 示例 - 10 [1080P][Baha][WEB-DL][AAC AVC][CHT].strm").exists()
        assert list(tmp_path.glob("*.strm")) == []

    def test_no_recognizable_series_is_a_noop(self, tmp_path):
        plugin = ANiStrmHub()
        plugin._storageplace = str(tmp_path)
        (tmp_path / "没有集数格式.strm").write_text("https://example.com/x?d=mp4", encoding="utf-8")

        getattr(plugin, "_ANiStrmHub__backfill_task")()

        status = plugin.get_data("task_status")["backfill"]
        assert status["summary"] == "本地无可识别集数的资源"


class TestDetectTask:
    def test_builds_connectivity_rows_and_local_distribution(self, tmp_path):
        plugin = ANiStrmHub()
        plugin._storageplace = str(tmp_path)
        plugin._subscription_source = "https://a.example/rss.xml"
        plugin._accelerator_prefix = "https://pro.pili.cc.cd"

        def fake_fetch(url):
            if url == "https://a.example/rss.xml":
                return [{"title": "示例", "link": "https://resources.ani.rip/2026-7/ep.mp4?d=mp4"}]
            return []

        plugin._client.fetch_one_source = MagicMock(side_effect=fake_fetch)
        plugin._relink_service.probe_latency_ms = MagicMock(return_value=(50.0, None))
        (tmp_path / "示例.strm").write_text(
            "https://pro.pili.cc.cd/resources.ani.rip/2026-7/ep.mp4?d=mp4", encoding="utf-8"
        )

        getattr(plugin, "_ANiStrmHub__detect_task")()

        matrix = plugin.get_data("connectivity_matrix")
        primary_row = matrix["rows"][0]
        assert primary_row["subscription"] == "https://a.example/rss.xml"
        assert primary_row["label"] == "当前配置"
        assert primary_row["columns"] == [
            {"label": "直连", "latency_ms": 50.0, "error": None},
            {"label": "加速后", "latency_ms": 50.0, "error": None},
        ]

        distribution = plugin.get_data("local_distribution")
        assert distribution["total"] == 1
        assert distribution["by_category"] == {"当前配置 + https://pro.pili.cc.cd": 1}

    def test_rss_fetch_failure_recorded_per_row(self):
        plugin = ANiStrmHub()
        plugin._subscription_source = "https://broken.example/rss.xml"
        plugin._storageplace = "/does-not-exist"
        plugin._client.fetch_one_source = MagicMock(side_effect=ValueError("HTTP状态异常：403"))

        getattr(plugin, "_ANiStrmHub__detect_task")()

        matrix = plugin.get_data("connectivity_matrix")
        primary_row = matrix["rows"][0]
        assert primary_row["rss_ok"] is False
        assert "403" in primary_row["error"]

    def test_pool_candidates_shown_alongside_primary(self, tmp_path):
        plugin = ANiStrmHub()
        plugin._storageplace = str(tmp_path)
        plugin._subscription_source = "https://my-own-mirror.example/rss.xml"
        plugin._client.fetch_one_source = MagicMock(
            return_value=[{"title": "示例", "link": "https://resources.ani.rip/2026-7/ep.mp4?d=mp4"}]
        )
        plugin._relink_service.probe_latency_ms = MagicMock(return_value=(50.0, None))

        getattr(plugin, "_ANiStrmHub__detect_task")()

        matrix = plugin.get_data("connectivity_matrix")
        assert len(matrix["rows"]) == 1 + len(FALLBACK_SUBSCRIPTION_POOL)
        assert matrix["rows"][0]["label"] == "当前配置"
        assert all(row["label"].startswith("内置容灾候选") for row in matrix["rows"][1:])
