import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote, unquote, urlparse, urlunparse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.network import RequestUtils

# 唯一默认订阅源。0.6.0起订阅源不再是用户要管理的"列表"，用户只填一个自己
# 认可的地址；下面这个内置候选池是抓取失败时后台自动依次尝试的容灾兜底，
# 不作为UI概念暴露——4.0.0"多源同时聚合谁先抓到用谁"和5.0.0"列表+选择器"
# 都是把这件事暴露成用户要理解的概念，这正是复杂度跟需求不匹配的地方。
DEFAULT_SUBSCRIPTION_SOURCE = "https://api.pili.cc.cd/ani-download.xml"
# ANi官方直链域名，用于"还原成裸链接"时重建地址——不管当前strm内容被套了
# 几层壳，extract_resource_path()都能从URL末尾定位出跟域名无关的"季度/
# 文件名?query"这一段，配上这个官方域名就能拼出一个确定的裸直链，不需要
# "记住"当初到底是被哪个加速源套过壳(单值配置模型下也没地方存这个记忆)。
OFFICIAL_BASE_URL = "https://resources.ani.rip"
FALLBACK_SUBSCRIPTION_POOL: Tuple[str, ...] = (
    "https://api.pili.cc.cd/ani-download.xml",
    "https://aniapi.op5.de5.net/ani-download.xml",
    "https://api.ani.rip/ani-download.xml",
)

# 非正片附属文件的标题关键词，硬编码常量不再让用户自己配置——预告/OP/ED这类
# 标记在几乎所有ANi/fansub命名习惯里含义固定，没必要为此暴露一个配置项
NON_EPISODE_BLACKLIST = "预告@PV@NCOP@NCED"
SUBTITLE_EXTENSIONS = (".srt", ".vtt", ".ass", ".ssa")
# 从直链里提取季度目录，如 .../2026-7/xxx.mp4 -> 2026-7
SEASON_RE = re.compile(r"/(\d{4}-\d{1,2})/")
# 匹配ANi标题/文件名里的集数，形如" - 11 ["，用于资源补齐时定位并替换集数数字。
# 要求前有"-"后有"["，避免误命中季度目录(yyyy-mm)或分辨率(1080P)里的数字。
EPISODE_NUM_RE = re.compile(r"(-\s*)(\d{1,4})(\s*\[)")
# 从ANi标题里切出剧名，用于"按番剧名称聚合"的目录归档。ANi命名格式固定是
# "[ANi] 剧名 - 集数 [1080P][Baha]..."，剧名就是开头的发布组标签之后、
# " - 集数 ["之前那一段。两个要点：
# 1. 集数不一定是整数——真实数据里有"- 12.5 ["(半集)和"- 電影 ["(剧场版)，
#    所以不能复用只认整数的EPISODE_NUM_RE，这里用"- 任意非方括号内容 ["；
# 2. 剧名本身可能含" - "(比如"Fate - Grand Order")，所以剧名部分用贪婪
#    匹配，取最后一个" - xxx ["当分隔点，不会把剧名截断成前半截。
SERIES_TITLE_RE = re.compile(r"^(?:\[[^\]]+\]\s*)?(.+)\s+-\s+[^\[\]]+\s*\[")
# strm存放方式
LAYOUT_FLAT = "flat"
LAYOUT_BY_TITLE = "by_title"

# 探测连通性时校验响应内容用：光看HTTP状态码不够，服务器完全可能返回200但
# 吐的是错误页(html/json)。这几个是明显的"这不是视频数据"标记。
HTML_LIKE_PREFIXES = (b"<!doctype", b"<html", b"<?xml", b"{")
NON_VIDEO_CONTENT_TYPES = ("text/html", "text/plain", "application/json", "application/xml", "text/xml")
PROBE_RANGE_BYTES = 64  # 够读到mp4的ftyp box或分辨明显的错误页，开销依然很小


class ANiStrmHub(_PluginBase):
    plugin_name = "ANiStrmHub"
    plugin_desc = "填一个订阅源+一个加速地址即可，自动抓取ANi新番资源生成strm文件，mp刮削入库，媒体服务器直连播放"
    plugin_icon = "https://raw.githubusercontent.com/oiloveio/MoviePilot-Plugins/main/icons/anistrmhub.png"
    plugin_version = "0.7.0"
    plugin_author = "oiloveio"
    author_url = "https://github.com/oiloveio"
    plugin_config_prefix = "anistrmhub_"
    plugin_order = 15
    auth_level = 2

    _enabled = False
    _use_proxy = True
    _cron = None
    _onlyonce = False
    _storageplace = None
    _season_filter: List[str] = ["all"]
    _strm_layout = LAYOUT_FLAT

    _subscription_source = DEFAULT_SUBSCRIPTION_SOURCE
    _accelerator_prefix = ""

    _refresh_subscription_once = False
    _apply_accelerator_once = False
    _regroup_once = False
    _backfill_once = False
    _detect_once = False
    _scheduler: Optional[BackgroundScheduler] = None

    def __init__(self):
        super().__init__()
        self._client = AniRssAggregator()
        self._strm_service = StrmFileService()
        self._relink_service = StrmRelinkService(request_factory=self._client.build_request_utils)

    def init_plugin(self, config: dict = None):
        self.stop_service()

        config = config or {}
        self._enabled = config.get("enabled", False)
        use_proxy = config.get("use_proxy")
        self._use_proxy = True if use_proxy is None else use_proxy
        self._cron = config.get("cron") or "20 22,23,0,1 * * *"
        self._onlyonce = config.get("onlyonce", False)
        self._storageplace = config.get("storageplace") or "/downloads/strm"
        self._season_filter = config.get("season_filter") or ["all"]
        self._strm_layout = config.get("strm_layout") or LAYOUT_FLAT

        self._subscription_source = (config.get("subscription_source") or "").strip() or DEFAULT_SUBSCRIPTION_SOURCE
        self._accelerator_prefix = (config.get("accelerator_prefix") or "").strip()

        self._refresh_subscription_once = config.get("refresh_subscription_once", False)
        self._apply_accelerator_once = config.get("apply_accelerator_once", False)
        self._regroup_once = config.get("regroup_once", False)
        self._backfill_once = config.get("backfill_once", False)
        self._detect_once = config.get("detect_once", False)

        self._client.set_use_proxy(self._use_proxy)
        logger.info(
            f"ANiStrmHub配置加载：enabled={self._enabled}, onlyonce={self._onlyonce}, "
            f"use_proxy={self._use_proxy}, storage={self._storageplace}, "
            f"订阅源={self._subscription_source}, 加速源={self._accelerator_prefix or '(不加速)'}"
        )

        if not (
            self._enabled
            or self._onlyonce
            or self._refresh_subscription_once
            or self._apply_accelerator_once
            or self._regroup_once
            or self._backfill_once
            or self._detect_once
        ):
            logger.info("ANiStrmHub未启用且未触发立即运行，跳过任务注册")
            return

        self._scheduler = BackgroundScheduler(timezone=settings.TZ)

        if self._enabled and self._cron:
            try:
                self._scheduler.add_job(
                    func=self.__task,
                    trigger=CronTrigger.from_crontab(self._cron),
                    name="ANiStrmHub文件创建",
                )
                logger.info(f"ANiStrmHub定时任务创建成功：{self._cron}")
            except Exception as err:
                logger.error(f"定时任务配置错误：{err}")

        if self._onlyonce:
            logger.info("ANiStrmHub服务启动，立即运行一次")
            self._scheduler.add_job(
                func=self.__task,
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="ANiStrmHub文件创建",
            )
            self._onlyonce = False

        if self._refresh_subscription_once:
            if self.__is_task_running("refresh_subscription"):
                logger.warning("ANiStrmHub刷新订阅源：上一次任务还在运行中，本次跳过排队，等它跑完再重新勾选")
            else:
                logger.info(f"ANiStrmHub服务启动，立即用当前订阅源刷新本地strm：{self._subscription_source}")
                self._scheduler.add_job(
                    func=self.__refresh_subscription_task,
                    trigger="date",
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                    name="ANiStrmHub刷新订阅源",
                )
            self._refresh_subscription_once = False

        if self._apply_accelerator_once:
            if self.__is_task_running("apply_accelerator"):
                logger.warning("ANiStrmHub套用加速源：上一次任务还在运行中，本次跳过排队，等它跑完再重新勾选")
            else:
                logger.info(f"ANiStrmHub服务启动，立即给本地strm套用/还原加速源：{self._accelerator_prefix or '(还原裸链接)'}")
                self._scheduler.add_job(
                    func=self.__apply_accelerator_task,
                    trigger="date",
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                    name="ANiStrmHub套用加速源",
                )
            self._apply_accelerator_once = False

        if self._regroup_once:
            if self.__is_task_running("regroup"):
                logger.warning("ANiStrmHub重新归档：上一次任务还在运行中，本次跳过排队，等它跑完再重新勾选")
            else:
                logger.info(f"ANiStrmHub服务启动，立即按当前存放方式重新归档本地strm：{self._strm_layout}")
                self._scheduler.add_job(
                    func=self.__regroup_local_strm_task,
                    trigger="date",
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                    name="ANiStrmHub重新归档",
                )
            self._regroup_once = False

        if self._backfill_once:
            if self.__is_task_running("backfill"):
                logger.warning("ANiStrmHub资源补齐：上一次任务还在运行中，本次跳过排队，等它跑完再重新勾选")
            else:
                logger.info("ANiStrmHub服务启动，立即回溯探测本地已有剧集缺失的老集数")
                self._scheduler.add_job(
                    func=self.__backfill_task,
                    trigger="date",
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                    name="ANiStrmHub资源补齐",
                )
            self._backfill_once = False

        if self._detect_once:
            if self.__is_task_running("detect"):
                logger.warning("ANiStrmHub连通性探测：上一次任务还在运行中，本次跳过排队，等它跑完再重新勾选")
            else:
                logger.info("ANiStrmHub服务启动，立即探测订阅源连通性")
                self._scheduler.add_job(
                    func=self.__detect_task,
                    trigger="date",
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                    name="ANiStrmHub连通性探测",
                )
            self._detect_once = False

        self.__update_config()

        if self._scheduler.get_jobs():
            self._scheduler.print_jobs()
            self._scheduler.start()

    def __subscription_candidates(self, primary: str) -> List[str]:
        """主订阅源优先，抓取失败时按顺序自动试内置容灾候选池——这一步完全
        在后台完成，用户不需要理解"多源列表"这个概念，也不会像4.0.0那样把
        多个源的内容混在一起用（那是bug的根因），每次运行始终只用其中一个"""
        ordered = [primary] + [c for c in FALLBACK_SUBSCRIPTION_POOL if c != primary]
        return list(dict.fromkeys(ordered))

    def __resolve_accelerator_for_this_run(self, sample_link: str) -> Optional[str]:
        """加速源现在只有一个配置值。用这一轮实际抓到的样本直链实测一次
        "这个加速源套上这条直链"能不能连通——只测一次，不是每条目都测，
        拉新番的速度不受影响；这一次检查就是从设计上避免"加速源套在一个
        连不通的组合上批量生成坏链接"的安全网，取代4.0.0"完全不测"和
        5.0.0部分场景"逐条测"这两个极端。探测不通过就本次任务不加速，
        写入裸直链，不阻塞整个任务。"""
        prefix = (self._accelerator_prefix or "").strip()
        if not prefix:
            return None
        candidate = StrmRelinkService.build_proxied_url(sample_link, prefix)
        latency_ms, fail_reason = self._relink_service.probe_latency_ms(candidate)
        if latency_ms is None:
            logger.warning(
                f"ANiStrmHub任务：加速源{prefix}套上本次样本直链探测不可达({fail_reason})，"
                f"本次任务不加速，写入裸直链"
            )
            return None
        logger.info(f"ANiStrmHub任务：加速源{prefix}探测可达({latency_ms}ms)，新生成的strm将套用")
        return prefix

    def __resolve_relative_dir(self, file_name: str) -> Optional[str]:
        """按当前"strm存放方式"决定这个文件该放在storageplace下的哪个子目录：
        平铺返回None(直接放根目录)，按番剧名称聚合返回剧名目录。

        决定目录的逻辑只有这一处——拉新番(__task)和一键重新归档
        (__regroup_local_strm_task)都调它；资源补齐用ref_file.with_name()
        天然跟参照文件同目录、刷新订阅源/套用加速源都是原地改写内容不挪
        位置，所以那三个任务不需要各自再解析一遍剧名(各写各的正是"同一部剧
        一半在文件夹里一半在根目录"这类分裂的来源)。"""
        if self._strm_layout != LAYOUT_BY_TITLE:
            return None
        series = StrmFileService.extract_series_title(file_name)
        if not series:
            logger.info(f"ANiStrmHub：识别不出剧名，这个文件平铺到根目录：{file_name}")
            return None
        return StrmFileService.safe_dir_name(series)

    def __finalize_strm_link(self, real_link: str) -> str:
        """资源补齐用：套用当前配置的加速源(没配置就是原始直链)。资源补齐
        本身对每个候选都会探测最终地址，天然有安全网，不需要像__task那样
        额外做一次性的"这个组合能不能用"预检查。"""
        accelerator = (self._accelerator_prefix or "").strip()
        if accelerator:
            return StrmRelinkService.build_proxied_url(real_link, accelerator)
        return real_link

    def __build_season_options(self) -> List[Dict[str, str]]:
        """拉当前配置订阅源的样本条目，从里面提取当前RSS窗口内出现过的季度，
        供配置页「拉取季度筛选」下拉用。"""
        seasons = set()
        subscription = (self._subscription_source or "").strip() or DEFAULT_SUBSCRIPTION_SOURCE
        try:
            entries = self._client.fetch_one_source(subscription)
        except Exception:
            entries = []
        for entry in entries:
            match = SEASON_RE.search(entry.get("link", ""))
            if match:
                seasons.add(match.group(1))
        sorted_seasons = sorted(seasons, key=lambda s: tuple(map(int, s.split("-"))), reverse=True)
        return [
            {"title": "不筛选(全部)", "value": "all"},
            {"title": "最新季(RSS窗口内)", "value": "latest"},
        ] + [{"title": season, "value": season} for season in sorted_seasons]

    def __apply_season_filter(self, entries: List[Dict[str, str]]) -> List[Dict[str, str]]:
        if not self._season_filter or "all" in self._season_filter:
            return entries

        all_seasons = set()
        for entry in entries:
            match = SEASON_RE.search(entry["link"])
            if match:
                all_seasons.add(match.group(1))

        target_seasons = set(s for s in self._season_filter if s not in ("all", "latest"))
        if "latest" in self._season_filter and all_seasons:
            target_seasons.add(max(all_seasons, key=lambda s: tuple(map(int, s.split("-")))))

        if not target_seasons:
            return entries

        filtered = []
        for entry in entries:
            match = SEASON_RE.search(entry["link"])
            if match and match.group(1) in target_seasons:
                filtered.append(entry)

        logger.info(
            f"ANiStrmHub季度筛选：配置={self._season_filter} -> 命中季度={sorted(target_seasons)}，"
            f"{len(entries)}条筛选为{len(filtered)}条"
        )
        return filtered

    def __task(self):
        self.__save_task_status("task", "running", "进行中")
        primary = (self._subscription_source or "").strip() or DEFAULT_SUBSCRIPTION_SOURCE

        entries = None
        used_source = None
        tried = []
        for candidate in self.__subscription_candidates(primary):
            tried.append(candidate)
            try:
                entries = self._client.fetch_one_source(candidate)
                used_source = candidate
                break
            except Exception as err:
                logger.warning(f"ANiStrmHub任务：订阅源抓取失败，自动尝试下一个候选：{candidate} - {err}")
                continue

        if entries is None:
            logger.warning(f"ANiStrmHub任务：全部{len(tried)}个候选订阅源均抓取失败，本次任务结束")
            self.__save_task_status("task", "done", f"全部候选订阅源均不可用({len(tried)}个)")
            return

        if used_source != primary:
            logger.warning(f"ANiStrmHub任务：主订阅源{primary}不可用，本次自动切到{used_source}")

        if not entries:
            logger.warning("ANiStrmHub订阅源RSS无内容，本次任务结束")
            self.__save_task_status("task", "done", "订阅源RSS无内容")
            return

        entries = self.__apply_season_filter(entries)
        if not entries:
            logger.warning("ANiStrmHub季度筛选后没有条目，本次任务结束")
            self.__save_task_status("task", "done", "季度筛选后无条目")
            return

        resolved_accelerator = self.__resolve_accelerator_for_this_run(entries[0]["link"])

        total_created = 0
        total_exists = 0
        total_failed = 0
        total_skipped = 0
        for entry in entries:
            title = entry["title"]
            if StrmFileService.is_subtitle_file(title):
                total_skipped += 1
                continue
            if StrmFileService.is_blacklisted(title, NON_EPISODE_BLACKLIST):
                logger.info(f"ANiStrmHub标题命中非正片关键词，跳过：{title}")
                total_skipped += 1
                continue

            relative_dir = self.__resolve_relative_dir(title)

            file_url = entry["link"]
            if resolved_accelerator:
                file_url = StrmRelinkService.build_proxied_url(file_url, resolved_accelerator)

            status = self._strm_service.touch_strm_file(
                storage_path=self._storageplace,
                file_name=title,
                file_url=file_url,
                relative_dir=relative_dir,
            )
            if status == "created":
                total_created += 1
            elif status == "exists":
                total_exists += 1
            else:
                total_failed += 1

        summary = (
            f"订阅源{used_source}共{len(entries)}条，新增={total_created}，"
            f"跳过(已存在)={total_exists}，跳过(附属文件)={total_skipped}，失败={total_failed}"
        )
        logger.info(f"ANiStrmHub任务完成：{summary}")
        self.__save_task_status("task", "done", summary)

    def __refresh_subscription_task(self):
        """用当前配置的这一个订阅源刷新本地已有的strm：标题还在RSS窗口内的
        直接换成最新直链，不在窗口内的按路径迁移公式换成这个订阅源的域名
        前缀。只有一个订阅源可选，不需要"目标订阅源"这种选择器概念。"""
        self.__save_task_status("refresh_subscription", "running", "进行中")
        directory = Path(self._storageplace)
        if not directory.exists():
            logger.warning(f"ANiStrmHub刷新订阅源：目录不存在 {self._storageplace}")
            self.__save_task_status("refresh_subscription", "done", "存储目录不存在")
            return

        subscription = (self._subscription_source or "").strip() or DEFAULT_SUBSCRIPTION_SOURCE
        try:
            entries = self._client.fetch_one_source(subscription)
        except Exception as err:
            logger.warning(f"ANiStrmHub刷新订阅源：抓取失败，任务结束：{subscription} - {err}")
            self.__save_task_status("refresh_subscription", "done", f"订阅源抓取失败：{err}")
            return
        if not entries:
            logger.warning(f"ANiStrmHub刷新订阅源：RSS无内容，任务结束：{subscription}")
            self.__save_task_status("refresh_subscription", "done", "订阅源RSS无内容")
            return

        title_map = {entry["title"]: entry["link"] for entry in entries}
        domain_prefix = StrmRelinkService.derive_prefix(entries[0]["link"])
        accelerator = (self._accelerator_prefix or "").strip()

        stats = {
            "标题精确匹配更新": 0,
            "路径迁移更新": 0,
            "无需更新": 0,
            "探测不可达(保留原文件)": 0,
            "无法识别(保留原文件)": 0,
        }

        for strm_file in sorted(directory.rglob("*.strm")):
            try:
                old_content = strm_file.read_text(encoding="utf-8").strip()
            except Exception as err:
                logger.warning(f"ANiStrmHub刷新订阅源：读取失败，跳过 {strm_file.name} - {err}")
                stats["无法识别(保留原文件)"] += 1
                continue

            matched_link = title_map.get(strm_file.stem)
            if matched_link:
                bare_link = matched_link
                match_kind = "标题精确匹配更新"
            elif domain_prefix:
                # extract_resource_path对URL末尾的"季度/文件名?query"定位，
                # 不管old_content当前有没有被套壳、被谁套壳都能定位到，不需要
                # 先剥壳——这一段本身就跟域名/加速源无关
                resource_path = StrmRelinkService.extract_resource_path(old_content)
                if not resource_path:
                    stats["无法识别(保留原文件)"] += 1
                    continue
                bare_link = domain_prefix + resource_path
                match_kind = "路径迁移更新"
            else:
                stats["无法识别(保留原文件)"] += 1
                continue

            final_link = StrmRelinkService.build_proxied_url(bare_link, accelerator) if accelerator else bare_link
            if final_link == old_content:
                stats["无需更新"] += 1
                continue

            time.sleep(0.3)
            latency_ms, fail_reason = self._relink_service.probe_latency_ms(final_link)
            if latency_ms is not None:
                strm_file.write_text(final_link, encoding="utf-8")
                stats[match_kind] += 1
                logger.info(f"ANiStrmHub刷新订阅源：成功({latency_ms}ms) {strm_file.name}")
            else:
                logger.warning(
                    f"ANiStrmHub刷新订阅源：候选链接探测不可达({fail_reason})，保留原文件 {strm_file.name}"
                )
                stats["探测不可达(保留原文件)"] += 1

        summary = "，".join(f"{k}={v}" for k, v in stats.items())
        logger.info(f"ANiStrmHub刷新订阅源完成：{summary}")
        self.__save_task_status("refresh_subscription", "done", summary)

    def __apply_accelerator_task(self):
        """用当前配置的这一个加速源(留空=不加速)重新套用/还原本地全部strm。
        只有一个加速源可选，"套上"和"去掉"是同一个操作依据accelerator_prefix
        是否为空决定，不需要"一键加速"/"一键还原"两个分开的开关，也不需要
        "目标加速源"这种选择器概念。

        不管strm当前内容有没有被套壳、被谁套壳，都先用extract_resource_path
        定位出跟域名无关的"季度/文件名?query"这一段，配上官方域名重建裸直链，
        再按需要套用当前加速源——这样即使用户把加速源字段从A改成B、或者
        直接清空，都能正确处理，不需要"记住"当初到底是哪个加速源套的壳。"""
        self.__save_task_status("apply_accelerator", "running", "进行中")
        directory = Path(self._storageplace)
        if not directory.exists():
            logger.warning(f"ANiStrmHub套用加速源：目录不存在 {self._storageplace}")
            self.__save_task_status("apply_accelerator", "done", "存储目录不存在")
            return

        accelerator = (self._accelerator_prefix or "").strip()
        stats = {
            "已套上加速源": 0,
            "已还原为裸链接": 0,
            "无需更新": 0,
            "探测不可达(保留原文件)": 0,
            "无法识别(保留原文件)": 0,
        }

        for strm_file in sorted(directory.rglob("*.strm")):
            try:
                old_content = strm_file.read_text(encoding="utf-8").strip()
            except Exception as err:
                logger.warning(f"ANiStrmHub套用加速源：读取失败，跳过 {strm_file.name} - {err}")
                stats["无法识别(保留原文件)"] += 1
                continue

            resource_path = StrmRelinkService.extract_resource_path(old_content)
            if not resource_path:
                stats["无法识别(保留原文件)"] += 1
                continue
            bare_link = f"{OFFICIAL_BASE_URL}/{resource_path}"

            final_link = StrmRelinkService.build_proxied_url(bare_link, accelerator) if accelerator else bare_link
            if final_link == old_content:
                stats["无需更新"] += 1
                continue

            time.sleep(0.3)
            latency_ms, fail_reason = self._relink_service.probe_latency_ms(final_link)
            if latency_ms is not None:
                strm_file.write_text(final_link, encoding="utf-8")
                stats["已套上加速源" if accelerator else "已还原为裸链接"] += 1
                logger.info(f"ANiStrmHub套用加速源：成功({latency_ms}ms) {strm_file.name}")
            else:
                logger.warning(
                    f"ANiStrmHub套用加速源：候选链接探测不可达({fail_reason})，保留原文件 {strm_file.name}"
                )
                stats["探测不可达(保留原文件)"] += 1

        summary = "，".join(f"{k}={v}" for k, v in stats.items())
        logger.info(f"ANiStrmHub套用加速源完成：{summary}")
        self.__save_task_status("apply_accelerator", "done", summary)

    def __regroup_local_strm_task(self):
        """按当前「strm存放方式」把本地已有的strm重新归档：选"按番剧名称
        聚合"就把散在根目录(以及历史版本留下的季度目录)里的文件搬进
        {剧名}/子目录，选"平铺"就把{剧名}/子目录里的文件搬回根目录。

        只移动文件，不改文件内容，不发任何网络请求——改存放方式是纯本地
        整理，跟链接能不能连通是两回事，没必要在这里探测。目标位置已经有
        同名文件时保留原文件不动，不覆盖。搬完清理掉空掉的子目录。"""
        self.__save_task_status("regroup", "running", "进行中")
        directory = Path(self._storageplace) if self._storageplace else None
        if not directory or not directory.exists():
            logger.warning(f"ANiStrmHub重新归档：目录不存在 {self._storageplace}")
            self.__save_task_status("regroup", "done", "存储目录不存在")
            return

        stats = {
            "已归档": 0,
            "位置已正确": 0,
            "识别不出剧名(平铺到根目录)": 0,
            "目标已存在(保留原文件)": 0,
            "移动失败": 0,
        }

        # 先整体取出文件列表再搬，避免边遍历边改目录结构
        for strm_file in sorted(directory.rglob("*.strm")):
            relative_dir = self.__resolve_relative_dir(strm_file.stem)
            unrecognized = self._strm_layout == LAYOUT_BY_TITLE and relative_dir is None
            target_dir = directory / relative_dir if relative_dir else directory
            target = target_dir / strm_file.name

            if target == strm_file:
                stats["位置已正确"] += 1
                continue
            if target.exists():
                logger.warning(f"ANiStrmHub重新归档：目标位置已有同名文件，保留原文件 {strm_file.name}")
                stats["目标已存在(保留原文件)"] += 1
                continue

            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                strm_file.rename(target)
                stats["识别不出剧名(平铺到根目录)" if unrecognized else "已归档"] += 1
                logger.debug(f"ANiStrmHub重新归档：{strm_file.name} -> {target.parent.name or '根目录'}")
            except Exception as err:
                logger.warning(f"ANiStrmHub重新归档：移动失败 {strm_file.name} - {err}")
                stats["移动失败"] += 1

        # 自底向上清理空目录：rmdir只对空目录成功，非空的直接跳过
        removed_dirs = 0
        for child in sorted(directory.rglob("*"), reverse=True):
            if not child.is_dir():
                continue
            try:
                child.rmdir()
                removed_dirs += 1
            except OSError:
                continue

        summary = "，".join(f"{k}={v}" for k, v in stats.items()) + f"，清理空目录={removed_dirs}"
        logger.info(f"ANiStrmHub重新归档完成：{summary}")
        self.__save_task_status("regroup", "done", summary)

    def __backfill_task(self):
        """资源补齐：ani-download.xml这个RSS只是滚动窗口，只含近期资源，更早的
        集数不在里面，但ANi同一部剧全部集数的直链只有集数数字不同——这是用户
        实测确认的：把"- 11"手动改成"- 10"依然能播放。

        季度目录不总是跟本地已有的最早一集相同：实测确认过一拳超人第三季
        真实起点第25集、SPY×FAMILY第三季真实起点第38集都落在比本地当前
        最早集更靠前的季度文件夹里。所以碰到当前季度文件夹探测不通，不能
        直接弃剧——按跟当前季度的月份距离由近到远，尝试本地已知的其它季度
        文件夹组合，全部试过还是不通才停止继续往前探测这部剧。命中的季度
        会作为下一集的优先候选(大概率连续几集在同一个文件夹)。

        对本地已有的每部剧，从当前最早一集往前递减集数构造候选，严格串行
        探测(不并发)、探测间隔sleep、单次任务设总探测数上限、每集尝试的
        季度候选数上限——这几条都是用户明确要求的限流设计，避免被目标站点
        风控封IP。确认可达才写入，不是无脑改写。"""
        self.__save_task_status("backfill", "running", "进行中")
        directory = Path(self._storageplace)
        if not directory.exists():
            logger.warning(f"ANiStrmHub资源补齐：目录不存在 {self._storageplace}")
            self.__save_task_status("backfill", "done", "存储目录不存在")
            return

        max_probes_total = 30
        max_back_per_series = 20
        max_season_candidates_per_episode = 4
        probe_interval_sec = 1.5

        series_min_ep: Dict[str, Tuple[int, Path]] = {}
        known_seasons: Set[str] = set()
        for strm_file in sorted(directory.rglob("*.strm")):
            stem = strm_file.stem
            try:
                content = strm_file.read_text(encoding="utf-8").strip()
            except Exception:
                content = ""
            season_match = SEASON_RE.search(content)
            if season_match:
                known_seasons.add(season_match.group(1))

            match = EPISODE_NUM_RE.search(stem)
            if not match:
                continue
            ep_num = int(match.group(2))
            series_key = f"{stem[:match.start(2)]}\0{stem[match.end(2):]}"
            if series_key not in series_min_ep or ep_num < series_min_ep[series_key][0]:
                series_min_ep[series_key] = (ep_num, strm_file)

        if not series_min_ep:
            logger.info("ANiStrmHub资源补齐：本地没有可识别集数的strm，任务结束")
            self.__save_task_status("backfill", "done", "本地无可识别集数的资源")
            return

        total_probed = 0
        total_created = 0

        for min_ep, ref_file in series_min_ep.values():
            if total_probed >= max_probes_total:
                logger.info("ANiStrmHub资源补齐：本次任务探测次数已达上限，剩余剧集留到下次手动运行")
                break
            try:
                ref_content = ref_file.read_text(encoding="utf-8").strip()
            except Exception:
                continue

            season_match = SEASON_RE.search(ref_content)
            current_season = season_match.group(1) if season_match else None

            for offset in range(1, max_back_per_series + 1):
                candidate_ep = min_ep - offset
                if candidate_ep < 1:
                    break
                if total_probed >= max_probes_total:
                    break

                candidate_title = StrmRelinkService.build_title_variant(ref_file.stem, candidate_ep)
                if not candidate_title:
                    break
                candidate_path = ref_file.with_name(f"{candidate_title}.strm")
                if candidate_path.exists():
                    # 这一集本地已经有了(之前补过/正常拉过)，不用重新探测，
                    # 继续往前查更早的集数
                    continue

                season_candidates: List[str] = []
                if current_season:
                    season_candidates.append(current_season)
                others = known_seasons - {current_season} if current_season else set(known_seasons)
                for season in sorted(others, key=lambda s: StrmRelinkService.season_distance(s, current_season or s)):
                    season_candidates.append(season)
                    if len(season_candidates) >= max_season_candidates_per_episode:
                        break

                found = False
                for season_option in season_candidates:
                    if total_probed >= max_probes_total:
                        break
                    if season_option == current_season:
                        link_with_season = ref_content
                    else:
                        link_with_season = StrmRelinkService.build_season_variant_link(ref_content, season_option)
                    if not link_with_season:
                        continue
                    candidate_link = StrmRelinkService.build_episode_variant_link(link_with_season, candidate_ep)
                    if not candidate_link:
                        continue

                    final_link = self.__finalize_strm_link(candidate_link)
                    time.sleep(probe_interval_sec)
                    total_probed += 1
                    latency_ms, fail_reason = self._relink_service.probe_latency_ms(final_link)
                    if latency_ms is not None:
                        try:
                            candidate_path.write_text(final_link, encoding="utf-8")
                            total_created += 1
                            found = True
                            current_season = season_option
                            logger.info(
                                f"ANiStrmHub资源补齐：成功补上第{candidate_ep}集"
                                f"(季度={season_option}，{latency_ms}ms) {candidate_path.name}"
                            )
                        except Exception as err:
                            logger.warning(f"ANiStrmHub资源补齐：写入失败 {candidate_path.name} - {err}")
                        break
                    logger.debug(
                        f"ANiStrmHub资源补齐：{ref_file.stem} 第{candidate_ep}集在季度{season_option}"
                        f"不可达({fail_reason})，尝试下一个候选季度"
                    )

                if not found:
                    logger.info(
                        f"ANiStrmHub资源补齐：{ref_file.stem} 回溯到第{candidate_ep}集，"
                        f"尝试过的{len(season_candidates)}个季度文件夹均不可达，停止继续往前探测这部剧"
                    )
                    break

        summary = f"探测{total_probed}次，成功补齐{total_created}集"
        logger.info(f"ANiStrmHub资源补齐完成：{summary}")
        self.__save_task_status("backfill", "done", summary)

    def __detect_task(self):
        """探测当前配置的订阅源(以及内置容灾候选池，仅作只读诊断展示，不是
        要用户管理的配置项)分别的直连情况，以及套上当前配置加速源之后的
        连通情况，顺带统计本地strm按"订阅源+加速源"分类的分布。"""
        self.__save_task_status("detect", "running", "进行中")
        primary = (self._subscription_source or "").strip() or DEFAULT_SUBSCRIPTION_SOURCE
        candidates = self.__subscription_candidates(primary)
        accelerator = (self._accelerator_prefix or "").strip()

        rows = []
        domain_to_label: Dict[str, str] = {}
        for idx, url in enumerate(candidates):
            label = "当前配置" if idx == 0 else f"内置容灾候选{idx}"
            row: Dict[str, Any] = {
                "subscription": url,
                "label": label,
                "rss_ok": False,
                "error": None,
                "columns": [],
            }
            try:
                entries = self._client.fetch_one_source(url)
            except Exception as err:
                row["error"] = str(err)
                rows.append(row)
                logger.warning(f"ANiStrmHub连通性探测：{url} RSS抓取失败 - {err}")
                continue

            row["rss_ok"] = True
            if not entries:
                row["error"] = "RSS无条目"
                rows.append(row)
                continue

            sample_link = entries[0]["link"]
            domain_to_label[urlparse(sample_link).netloc] = label

            time.sleep(0.3)
            latency_ms, fail_reason = self._relink_service.probe_latency_ms(sample_link)
            row["columns"].append({"label": "直连", "latency_ms": latency_ms, "error": fail_reason})

            if accelerator:
                candidate = StrmRelinkService.build_proxied_url(sample_link, accelerator)
                time.sleep(0.3)
                latency_ms, fail_reason = self._relink_service.probe_latency_ms(candidate)
                row["columns"].append({"label": "加速后", "latency_ms": latency_ms, "error": fail_reason})

            rows.append(row)

        checked_at = datetime.now(tz=pytz.timezone(settings.TZ)).strftime("%Y-%m-%d %H:%M:%S")
        self.save_data("connectivity_matrix", {"checked_at": checked_at, "rows": rows})

        distribution = StrmRelinkService.scan_local_distribution(
            self._storageplace, domain_to_label, [accelerator] if accelerator else []
        )
        self.save_data("local_distribution", {"checked_at": checked_at, **distribution})

        summary = f"{len(candidates)}个候选订阅源，本地strm共{distribution.get('total', 0)}个"
        logger.info(f"ANiStrmHub连通性探测完成：{summary}")
        self.__save_task_status("detect", "done", summary)

    def __save_task_status(self, task_key: str, status: str, summary: str = ""):
        """记录一次性任务的运行状态，供详情页展示进度，也用来防止上一次
        还没跑完时被重复排队"""
        all_status = self.get_data("task_status") or {}
        all_status[task_key] = {
            "status": status,  # running / done / failed
            "summary": summary,
            "updated_at": datetime.now(tz=pytz.timezone(settings.TZ)).strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.save_data("task_status", all_status)

    def __is_task_running(self, task_key: str) -> bool:
        all_status = self.get_data("task_status") or {}
        return all_status.get(task_key, {}).get("status") == "running"

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """当前插件不注册远程命令"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """当前插件不注册后端API。如果社区加速源都不稳定，可以自己用GOST/
        Nginx等工具搭一个"Proxy Everything"风格的反向代理，把这个反代地址
        当成"加速源"填进配置里即可——不需要插件在自己进程里重造一遍转发，
        这也是0.6.0移除4.0.0"Relay转发"实验性功能的原因，详见README。"""
        return []

    @staticmethod
    def __section_title(text: str) -> dict:
        return {
            "component": "VRow",
            "content": [
                {
                    "component": "VCol",
                    "props": {"cols": 12},
                    "content": [
                        {
                            "component": "div",
                            "props": {"class": "text-subtitle-1 font-weight-bold mt-3 mb-1"},
                            "text": text,
                        }
                    ],
                }
            ],
        }

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    self.__section_title("核心设置——拉取新番生成strm"),
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件（按执行周期定时拉取）"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "onlyonce", "label": "立即运行一次"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "use_proxy", "label": "使用代理"},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "cron",
                                            "label": "执行周期",
                                            "placeholder": "20 22,23,0,1 * * *",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "storageplace",
                                            "label": "Strm存储地址",
                                            "placeholder": "/downloads/strm",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "strm_layout",
                                            "label": "strm存放方式",
                                            "items": [
                                                {"title": "平铺（全部放在存储目录下）", "value": LAYOUT_FLAT},
                                                {"title": "按番剧名称聚合（每部剧一个文件夹）", "value": LAYOUT_BY_TITLE},
                                            ],
                                            "hint": "改了之后勾选下方「重新归档本地strm」，把已有文件搬到新位置",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 7},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "subscription_source",
                                            "label": "订阅源（ANi的RSS地址）",
                                            "placeholder": DEFAULT_SUBSCRIPTION_SOURCE,
                                            "hint": "只填一个你信得过的地址就行。这个地址抓取失败时，会自动依次"
                                            "尝试内置的几个备用镜像，不用你手动切换",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 5},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "season_filter",
                                            "label": "拉取季度筛选",
                                            "items": self.__build_season_options(),
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                            "hint": "只在订阅源当前RSS窗口内筛选，默认「不筛选」处理全部",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 7},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "accelerator_prefix",
                                            "label": "加速源（国内直连不通时配代理/反代地址，留空=不加速）",
                                            "placeholder": "https://pro.pili.cc.cd",
                                            "hint": "把strm链接整体包一层这个地址。每次拉新番前会自动测一次"
                                            "这个地址能不能用，测不通当次就不加速、写裸直链，不会生成"
                                            "连不上的坏链接",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VCard",
                        "props": {"variant": "tonal", "color": "info", "class": "mt-4"},
                        "content": [
                            {
                                "component": "VCardTitle",
                                "props": {"class": "text-subtitle-1"},
                                "text": "本地strm维护——批量调整已生成的strm，可能耗时几分钟",
                            },
                            {
                                "component": "VCardText",
                                "content": [
                                    {
                                        "component": "VRow",
                                        "content": [
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 6},
                                                "content": [
                                                    {
                                                        "component": "VSwitch",
                                                        "props": {
                                                            "model": "refresh_subscription_once",
                                                            "label": "用当前订阅源刷新本地strm",
                                                        },
                                                    }
                                                ],
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 6},
                                                "content": [
                                                    {
                                                        "component": "VSwitch",
                                                        "props": {
                                                            "model": "apply_accelerator_once",
                                                            "label": "套用/还原当前加速源",
                                                        },
                                                    }
                                                ],
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 6},
                                                "content": [
                                                    {
                                                        "component": "VSwitch",
                                                        "props": {
                                                            "model": "regroup_once",
                                                            "label": "按存放方式重新归档本地strm",
                                                        },
                                                    }
                                                ],
                                            },
                                        ],
                                    },
                                    {
                                        "component": "div",
                                        "props": {"class": "text-caption mt-2"},
                                        "text": "刷新订阅源：标题还在RSS窗口内的直接换成最新直链，不在窗口内的按"
                                        "路径迁移公式换成当前订阅源的域名。套用/还原加速源：加速源填了就套上，"
                                        "留空就还原成裸链接。这两个都实测探测确认可达才覆盖写入，探测不通过的"
                                        "保留原文件不动。重新归档：按上面选的「strm存放方式」把已有文件搬到"
                                        "对应位置(纯本地移动，不改内容不发请求)，改了存放方式之后跑一次即可。"
                                        "运行状态见下方详情页。",
                                    },
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VCard",
                        "props": {"variant": "tonal", "color": "purple", "class": "mt-4"},
                        "content": [
                            {
                                "component": "VCardTitle",
                                "props": {"class": "text-subtitle-1"},
                                "text": "资源补齐——把RSS滚动窗口之外的老集数找回来",
                            },
                            {
                                "component": "VCardText",
                                "content": [
                                    {
                                        "component": "VRow",
                                        "content": [
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 4},
                                                "content": [
                                                    {
                                                        "component": "VSwitch",
                                                        "props": {
                                                            "model": "backfill_once",
                                                            "label": "立即回溯补齐老集数",
                                                        },
                                                    }
                                                ],
                                            },
                                        ],
                                    },
                                    {
                                        "component": "div",
                                        "props": {"class": "text-caption mt-2"},
                                        "text": "对本地已有的剧，从最早一集往前递减集数构造候选直链；同一部剧更早的"
                                        "集数不一定在同一个季度文件夹（实测确认过有的剧真实起点在更早的月份），"
                                        "碰到当前文件夹探测不通会按月份距离尝试其它已知文件夹，不会一碰壁就弃剧。"
                                        "严格串行探测+限流，确认可达才写入，手动触发，不进定时任务。",
                                    },
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VCard",
                        "props": {"variant": "tonal", "color": "success", "class": "mt-4"},
                        "content": [
                            {
                                "component": "VCardTitle",
                                "props": {"class": "text-subtitle-1"},
                                "text": "连通性探测",
                            },
                            {
                                "component": "VCardText",
                                "content": [
                                    {
                                        "component": "VRow",
                                        "content": [
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 4},
                                                "content": [
                                                    {
                                                        "component": "VSwitch",
                                                        "props": {
                                                            "model": "detect_once",
                                                            "label": "立即探测连通性",
                                                        },
                                                    }
                                                ],
                                            },
                                        ],
                                    },
                                    {
                                        "component": "div",
                                        "props": {"class": "text-caption mt-2"},
                                        "text": "测你配置的订阅源直连情况、套上加速源之后的连通情况，顺带列出内置"
                                        "容灾候选池当前谁能连（仅供参考，不需要你选）。结果在下方详情页展示。",
                                    },
                                ],
                            },
                        ],
                    },
                    self.__section_title("使用说明"),
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "抓取ANi的RSS（ani-download.xml），生成strm文件\n"
                                            "存放方式选「按番剧名称聚合」时每部剧一个文件夹，Emby/Jellyfin刮削更干净；"
                                            "多季度的剧季度信息本来就在标题里，会自然分成不同文件夹，不需要额外套Season子目录\n"
                                            "配合目录监控使用，strm文件创建在/downloads/strm\n"
                                            "通过目录监控转移到link媒体库文件夹 如/downloads/link/strm mp会完成刮削",
                                            "style": "white-space: pre-line;",
                                        },
                                    },
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "国内直连不稳定时，除了填社区加速源(如pili/op5)，也可以自己用GOST/"
                                            "Nginx等工具搭一个反向代理，把反代地址当成加速源填进去——不需要插件"
                                            "额外支持，用法跟社区加速源完全一样。",
                                            "style": "white-space: pre-line;",
                                        },
                                    },
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "emby容器需要设置代理，docker的环境变量必须要有http_proxy代理变量，大小写敏感，否则无法提取媒体信息，具体见readme.\n"
                                            "https://github.com/oiloveio/MoviePilot-Plugins",
                                            "style": "white-space: pre-line;",
                                        },
                                    },
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "use_proxy": True,
            "onlyonce": False,
            "storageplace": "/downloads/strm",
            "season_filter": ["all"],
            "strm_layout": LAYOUT_FLAT,
            "subscription_source": DEFAULT_SUBSCRIPTION_SOURCE,
            "accelerator_prefix": "",
            "refresh_subscription_once": False,
            "apply_accelerator_once": False,
            "regroup_once": False,
            "backfill_once": False,
            "detect_once": False,
            "cron": "20 22,23,0,1 * * *",
        }

    def __update_config(self):
        self.update_config(
            {
                "enabled": self._enabled,
                "use_proxy": self._use_proxy,
                "cron": self._cron,
                "onlyonce": self._onlyonce,
                "storageplace": self._storageplace,
                "season_filter": self._season_filter,
                "strm_layout": self._strm_layout,
                "subscription_source": self._subscription_source,
                "accelerator_prefix": self._accelerator_prefix,
                "refresh_subscription_once": self._refresh_subscription_once,
                "apply_accelerator_once": self._apply_accelerator_once,
                "regroup_once": self._regroup_once,
                "backfill_once": self._backfill_once,
                "detect_once": self._detect_once,
            }
        )

    TASK_LABELS = {
        "task": "拉取新番生成strm",
        "refresh_subscription": "刷新订阅源",
        "apply_accelerator": "套用/还原加速源",
        "regroup": "按存放方式重新归档",
        "backfill": "资源补齐(回溯集数)",
        "detect": "连通性探测",
    }

    def get_page(self) -> List[dict]:
        content: List[dict] = []

        task_status = self.get_data("task_status") or {}
        if task_status:
            rows = []
            for key, label in self.TASK_LABELS.items():
                info = task_status.get(key)
                if not info:
                    status_text, color = "从未运行", "grey"
                elif info.get("status") == "running":
                    status_text, color = "🔄 运行中...", "info"
                else:
                    status_text, color = "✅ 已完成", "success"
                summary = (info or {}).get("summary", "")
                updated_at = (info or {}).get("updated_at", "")
                rows.append(
                    {
                        "component": "VRow",
                        "props": {"class": "align-center"},
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{"component": "span", "text": label}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 2},
                                "content": [
                                    {
                                        "component": "VChip",
                                        "props": {"color": color, "size": "small"},
                                        "text": status_text,
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 7},
                                "content": [
                                    {
                                        "component": "span",
                                        "props": {"class": "text-caption"},
                                        "text": f"{updated_at}  {summary}" if updated_at else "",
                                    }
                                ],
                            },
                        ],
                    }
                )
            content.append(
                {
                    "component": "VCard",
                    "props": {"class": "mb-4"},
                    "content": [
                        {"component": "VCardTitle", "text": "任务运行状态"},
                        {"component": "VCardText", "content": rows},
                    ],
                }
            )

        connectivity_matrix = self.get_data("connectivity_matrix") or {}
        local_distribution = self.get_data("local_distribution") or {}

        if not connectivity_matrix and not local_distribution:
            content.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "text": "还没有探测数据。去插件配置页勾选「立即探测连通性」跑一次，这里会显示"
                        "订阅源直连、套上加速源之后能不能连，还有本地已生成的strm按订阅源+加速源的分布。",
                    },
                }
            )
            return content

        if connectivity_matrix:
            rows_content = []
            for row in connectivity_matrix.get("rows", []):
                chips: List[dict] = []
                if not row.get("rss_ok"):
                    chips.append(
                        {
                            "component": "VChip",
                            "props": {"color": "error", "size": "small", "class": "ma-1"},
                            "text": f"RSS抓取失败：{row.get('error')}",
                        }
                    )
                elif not row.get("columns"):
                    chips.append(
                        {
                            "component": "VChip",
                            "props": {"color": "warning", "size": "small", "class": "ma-1"},
                            "text": row.get("error") or "RSS无条目",
                        }
                    )
                else:
                    for col in row["columns"]:
                        if col.get("latency_ms") is not None:
                            chips.append(
                                {
                                    "component": "VChip",
                                    "props": {"color": "success", "size": "small", "class": "ma-1"},
                                    "text": f"{col['label']}：✅ {col['latency_ms']:.0f}ms",
                                }
                            )
                        else:
                            chips.append(
                                {
                                    "component": "VChip",
                                    "props": {"color": "error", "size": "small", "class": "ma-1"},
                                    "text": f"{col['label']}：❌ {col.get('error') or ''}",
                                }
                            )
                rows_content.append(
                    {
                        "component": "VRow",
                        "props": {"class": "align-center mb-1"},
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "span", "text": f"{row.get('label')}：{row.get('subscription')}"}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 9},
                                "content": chips,
                            },
                        ],
                    }
                )
            content.append(
                {
                    "component": "VCard",
                    "props": {"class": "mb-4"},
                    "content": [
                        {
                            "component": "VCardTitle",
                            "text": f"连通性探测（探测于 {connectivity_matrix.get('checked_at', '未知时间')}）",
                        },
                        {
                            "component": "VCardText",
                            "content": rows_content or [{"component": "span", "text": "无数据"}],
                        },
                    ],
                }
            )

        if local_distribution:
            by_category = local_distribution.get("by_category", {})
            total = local_distribution.get("total", 0)
            rows = []
            for category, count in sorted(by_category.items(), key=lambda kv: kv[1], reverse=True):
                percent = f"{count / total * 100:.1f}%" if total else "0%"
                rows.append(
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{"component": "span", "text": category}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{"component": "span", "text": f"{count} 个"}],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{"component": "span", "text": percent}],
                            },
                        ],
                    }
                )
            content.append(
                {
                    "component": "VCard",
                    "content": [
                        {
                            "component": "VCardTitle",
                            "text": f"本地strm分布（共{total}个，统计于 "
                            f"{local_distribution.get('checked_at', '未知时间')}）",
                        },
                        {
                            "component": "VCardText",
                            "content": rows or [{"component": "span", "text": "storageplace目录下没有strm文件"}],
                        },
                    ],
                }
            )

        return content

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    # 注意：shutdown()默认wait=True，会阻塞等正在跑的job(比如探测/维护
                    # 这类耗时几分钟的一次性任务)跑完才返回。用户保存配置会先走到这里，
                    # 如果上一次任务还没跑完，保存动作会被卡住——这是实测踩过的真bug，
                    # 必须wait=False：不等，让旧job在自己的线程里跑完，保存立即返回。
                    self._scheduler.shutdown(wait=False)
                self._scheduler = None
        except Exception as err:
            logger.error(f"退出插件失败：{err}")


class AniRssAggregator:
    """按URL抓取并解析单个ANi RSS源(ani-download.xml格式)。不持有"数据源
    列表"这个状态——具体拉哪个源由调用方(ANiStrmHub)决定，这里只负责
    "给一个URL，抓取解析成条目列表"这一件事。"""

    def __init__(self, use_proxy: bool = False):
        self._use_proxy = use_proxy

    def set_use_proxy(self, use_proxy: bool):
        self._use_proxy = use_proxy

    def build_request_utils(self) -> RequestUtils:
        return RequestUtils(
            ua=settings.USER_AGENT if settings.USER_AGENT else None,
            proxies=settings.PROXY if self._use_proxy and settings.PROXY else None,
        )

    def fetch_one_source(self, url: str) -> List[Dict[str, str]]:
        """抓取单个数据源，不吞异常，抓取失败会直接抛出，由调用方决定要不要
        区分"RSS连不上"和"RSS通了但没内容"两种情况"""
        return self._fetch_one(url)

    def _fetch_one(self, url: str) -> List[Dict[str, str]]:
        def operation():
            response = self.build_request_utils().get_res(url)
            if not response or response.status_code != 200:
                status = response.status_code if response else "无响应"
                raise ValueError(f"HTTP状态异常：{status}")
            # 用response.content交给ET解析：RSS xml声明里自带encoding，
            # 比依赖requests根据HTTP头猜的response.text更准，避免个别源
            # 未声明charset时中文标题被错误解码
            return self._parse_rss(response.content)

        return self._with_retry(operation, tries=2, delay=3)

    @staticmethod
    def _parse_rss(xml_content: bytes) -> List[Dict[str, str]]:
        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError as err:
            raise ValueError(f"RSS解析失败：{err}")

        channel = root.find("channel")
        if channel is None:
            return []

        entries: List[Dict[str, str]] = []
        for item in channel.findall("item"):
            data: Dict[str, str] = {}
            for child in item:
                tag = child.tag.rsplit("}", 1)[-1]
                text = (child.text or "").strip()
                if tag == "title":
                    data["title"] = text
                elif tag == "link":
                    data["link"] = text
                elif tag == "pubDate":
                    data["pub_date"] = text
                elif tag == "size":
                    data["size"] = text
            if data.get("title") and data.get("link"):
                entries.append(data)
        return entries

    @staticmethod
    def _with_retry(operation, tries: int = 2, delay: int = 3):
        remaining = tries
        last_err = None
        while remaining > 0:
            try:
                return operation()
            except Exception as err:
                last_err = err
                remaining -= 1
                if remaining > 0:
                    time.sleep(delay)
        raise last_err


class StrmFileService:
    @staticmethod
    def is_subtitle_file(title: str) -> bool:
        """借鉴shanhai2333/ANiStrmPro：过滤字幕类附属文件，不生成strm"""
        return title.lower().endswith(SUBTITLE_EXTENSIONS)

    @staticmethod
    def is_blacklisted(title: str, blacklist_config: str) -> bool:
        """标题命中黑名单关键词(@分隔)则跳过，不生成strm"""
        if not blacklist_config:
            return False
        keywords = [kw.strip() for kw in blacklist_config.split("@") if kw.strip()]
        return any(kw in title for kw in keywords)

    @staticmethod
    def extract_series_title(file_name: str) -> Optional[str]:
        """从ANi的完整标题/文件名里切出剧名，"按番剧名称聚合"存放和"一键
        重新归档"共用这一个函数——解析规则只有这一份，以后规则要改只改
        这里，不会出现同一部剧一半在文件夹里一半在根目录的分裂。

        识别不出来返回None，调用方应当把这种文件平铺到根目录而不是瞎猜
        一个目录名。"""
        match = SERIES_TITLE_RE.search(file_name)
        if not match:
            return None
        return match.group(1).strip() or None

    @staticmethod
    def safe_dir_name(name: str) -> str:
        """剧名转目录名。真实数据(2650个文件)核对过：ANi的剧名里没有文件
        系统非法字符，也没有超长的，所以只挡住路径分隔符防止意外建出多层
        目录，不做过度清洗。"""
        return name.replace("/", "_").replace("\\", "_").strip()

    def touch_strm_file(
        self,
        storage_path: str,
        file_name: str,
        file_url: str,
        relative_dir: Optional[str] = None,
    ) -> str:
        if not storage_path:
            logger.error("创建strm源文件失败：未配置存储目录")
            return "failed"

        safe_name = file_name.replace("/", "_").replace("\\", "_")
        # RSS里的link本身就是可直接请求的直链(通常带?d=mp4参数)，不要对其做后缀改写，
        # 改写会导致目标站点路由不到实际资源(404)——已实测踩过这个坑
        src_url = file_url

        directory = Path(storage_path)
        if relative_dir:
            directory = directory / relative_dir
        file_path = directory / f"{safe_name}.strm"
        if file_path.exists():
            logger.debug(f"ANiStrmHub跳过已存在文件：{file_path.name}")
            return "exists"

        try:
            directory.mkdir(parents=True, exist_ok=True)
            file_path.write_text(src_url, encoding="utf-8")
            logger.debug(f"ANiStrmHub创建成功：{file_path.name}")
            return "created"
        except Exception as err:
            logger.error(f"创建strm源文件失败：{file_path.name} - {err}")
            return "failed"


class StrmRelinkService:
    """本地strm链接的构造/探测/归类工具集。

    核心转换公式：
    1. 域名替换（derive_prefix + extract_resource_path）：ANi各镜像的直链
       结构是 {前缀}/{季度}/{文件名}?d=mp4，其中"季度/文件名?d=mp4"这一段
       在所有镜像间完全一致，只有前缀（域名）不同。"刷新订阅源"用的就是
       这个公式。
    2. 前缀拼接（build_proxied_url）：把原始链接整体包一层反代前缀，格式仿
       "Proxy Everything"这类通用反代工具的用法，保留原host不做域名替换。
       加速源套壳用的是这个公式，两者不要混用。
    3. strip_known_accelerator是build_proxied_url的逆运算。
    4. build_season_variant_link/build_episode_variant_link是资源补齐用的
       候选构造：分别替换季度目录和集数数字，其余部分原样保留。
    """

    SEASON_PATH_RE = re.compile(r"(\d{4}-\d{1,2}/.+)$")
    DEFAULT_SPEED_TEST_BYTES = 1_048_576  # 1MB，够估算网速又不会跑太久/太费流量

    def __init__(self, request_factory):
        self._request_factory = request_factory

    @classmethod
    def extract_resource_path(cls, url: str) -> Optional[str]:
        match = cls.SEASON_PATH_RE.search(url)
        return match.group(1) if match else None

    @classmethod
    def derive_prefix(cls, url: str) -> Optional[str]:
        resource_path = cls.extract_resource_path(url)
        if not resource_path:
            return None
        return url[: url.index(resource_path)]

    @staticmethod
    def build_proxied_url(original_link: str, proxy_prefix: str) -> str:
        """把原始链接整体包一层反代前缀，格式仿"Proxy Everything"这类通用反代
        工具的用法：新地址 = 代理前缀 + / + 原host + 原path + 原query。

        例：原链接 https://resources.ani.rip/2025-10/xxx?d=mp4，
        代理前缀 https://pro.pili.cc.cd，
        结果 https://pro.pili.cc.cd/resources.ani.rip/2025-10/xxx?d=mp4"""
        proxy_prefix = proxy_prefix.rstrip("/")
        if original_link.startswith(proxy_prefix + "/"):
            return original_link  # 已经套过这层代理，不重复叠加
        parsed = urlparse(original_link)
        suffix = parsed.netloc + parsed.path
        if parsed.query:
            suffix += "?" + parsed.query
        return f"{proxy_prefix}/{suffix}"

    @staticmethod
    def strip_known_accelerator(link: str, accelerator_prefixes: List[str]) -> Tuple[str, Optional[str]]:
        """build_proxied_url的逆运算：如果link是用某个已知加速源前缀套壳的，
        剥掉这层壳，还原成推测的裸直链(重新补回https://让netloc+path+query
        变成可用URL)，同时返回匹配到的那个前缀；不匹配任何已知加速源则原样
        返回link，第二个值为None(视为本来就是裸链接)。"""
        for prefix in accelerator_prefixes:
            marker = prefix.rstrip("/") + "/"
            if link.startswith(marker):
                return "https://" + link[len(marker):], prefix
        return link, None

    @staticmethod
    def build_title_variant(original_title: str, new_episode: int) -> Optional[str]:
        """把标题/文件名里的集数换成new_episode，其余原样保留"""
        match = EPISODE_NUM_RE.search(original_title)
        if not match:
            return None
        return f"{original_title[:match.start(2)]}{new_episode}{original_title[match.end(2):]}"

    @staticmethod
    def build_episode_variant_link(original_link: str, new_episode: int) -> Optional[str]:
        """资源补齐用：把直链里的集数数字换成new_episode，域名/季度目录/文件名
        其它属性/查询参数原样保留。直链的文件名部分是URL编码过的(空格/方括号等)，
        先解码定位集数、替换后只对path重新编码，query本身没有需要编码的字符
        不用动。"""
        decoded = unquote(original_link)
        match = EPISODE_NUM_RE.search(decoded)
        if not match:
            return None
        new_decoded = f"{decoded[:match.start(2)]}{new_episode}{decoded[match.end(2):]}"
        parsed = urlparse(new_decoded)
        new_path = quote(parsed.path, safe="/")
        return urlunparse((parsed.scheme, parsed.netloc, new_path, parsed.params, parsed.query, parsed.fragment))

    @staticmethod
    def build_season_variant_link(link: str, new_season: str) -> Optional[str]:
        """资源补齐用：把直链里的季度目录换成new_season，其余部分原样保留。
        季度目录是纯ASCII(yyyy-m格式)，不涉及URL编码，直接在原始字符串上
        替换即可，不需要像集数那样先unquote再quote。"""
        match = SEASON_RE.search(link)
        if not match:
            return None
        return f"{link[:match.start(1)]}{new_season}{link[match.end(1):]}"

    @staticmethod
    def season_distance(season_a: str, season_b: str) -> int:
        """两个yyyy-m季度目录之间相差多少个月，资源补齐按这个距离由近到远
        尝试候选季度文件夹——同一部剧更早的集数大概率落在离当前季度不太远
        的月份里，优先试近的能省探测次数。"""

        def _month_index(season: str) -> int:
            year, month = season.split("-")
            return int(year) * 12 + int(month)

        return abs(_month_index(season_a) - _month_index(season_b))

    def probe_latency_ms(self, url: str) -> Tuple[Optional[float], Optional[str]]:
        """探测直链能不能连通、连通要多久，同时校验响应内容是不是真的视频
        数据——只看HTTP状态码不够：服务器完全可能返回200/206但吐的是错误页
        (html/json)，这个坑已经在真实排查中确认过。返回(延迟ms, None)表示
        成功；返回(None, 失败原因)表示不可达——原因写清楚具体HTTP状态码/
        内容校验失败/异常信息，不能只留一句"不可达"就没了。"""
        try:
            # 注意：get_res(url, headers=...)里的headers会整体替换掉RequestUtils构造时
            # 设置的默认header(包括UA)，不是合并。必须用update_headers()把Range头合并
            # 进去，否则探测请求会变成没有UA的裸请求，容易被目标站点当可疑流量拦截(403)，
            # 导致本来可达的链接被误判为不可达——这个坑已经在联调时实测踩过。
            request_utils = self._request_factory()
            request_utils.update_headers({"Range": f"bytes=0-{PROBE_RANGE_BYTES - 1}"})
            start = time.monotonic()
            response = request_utils.get_res(url)
            elapsed_ms = (time.monotonic() - start) * 1000
            if not response:
                return None, "无响应(连接失败或超时)"
            if response.status_code not in (200, 206):
                return None, f"HTTP {response.status_code}"
            if not self._looks_like_video(response):
                return None, "响应内容不是视频数据(HTTP状态码正常但可能是错误页)"
            return round(elapsed_ms, 1), None
        except Exception as err:
            return None, f"异常:{err}"

    @staticmethod
    def _looks_like_video(response: Any) -> bool:
        """两层校验：1. Content-Type声明的类型不是网页/JSON这类格式；
        2. 内容开头不是明显的错误页标记，且尽量匹配已知视频容器的魔数字节
        (mp4/mov的'ftyp'box在偏移4字节处，webm/mkv的EBML头在开头)。ANi的
        直链固定是mp4容器，遇到不认识的格式但也没有错误页特征时保守放行，
        避免对没见过的容器类型误杀。"""
        try:
            content_type = str(response.headers.get("Content-Type", "") or "").split(";")[0].strip().lower()
        except Exception:
            content_type = ""
        if content_type in NON_VIDEO_CONTENT_TYPES:
            return False

        content = response.content or b""
        if not content:
            return False
        stripped = content.lstrip()[:20].lower()
        if any(stripped.startswith(marker) for marker in HTML_LIKE_PREFIXES):
            return False
        if content[4:8] == b"ftyp":
            return True
        if content[:4] == b"\x1a\x45\xdf\xa3":
            return True
        return True

    def probe_speed_kbps(self, url: str, chunk_bytes: Optional[int] = None) -> Optional[float]:
        """下载一小段(默认1MB)实测网速，不是下载整部视频。只应该在已经确认
        probe_latency_ms可达之后再调用，避免对本来就连不上的链接白跑一次。"""
        chunk_bytes = chunk_bytes or self.DEFAULT_SPEED_TEST_BYTES
        try:
            request_utils = self._request_factory()
            request_utils.update_headers({"Range": f"bytes=0-{chunk_bytes - 1}"})
            start = time.monotonic()
            response = request_utils.get_res(url)
            elapsed = time.monotonic() - start
            if not response or response.status_code not in (200, 206):
                return None
            downloaded = len(response.content or b"")
            if downloaded <= 0 or elapsed <= 0:
                return None
            return round(downloaded / 1024 / elapsed, 1)
        except Exception:
            return None

    def _verify_reachable(self, url: str) -> bool:
        latency_ms, _ = self.probe_latency_ms(url)
        return latency_ms is not None

    @staticmethod
    def scan_local_distribution(
        storage_path: str,
        domain_to_source: Dict[str, str],
        accelerator_prefixes: List[str],
    ) -> Dict[str, Any]:
        """扫描本地strm，对每个文件归类成"订阅源X + 加速源Y"或"订阅源X 裸链"，
        用于详情页展示分布。domain_to_source是"样本域名 -> 标签"的映射，
        来自__detect_task当次探测各候选订阅源拿到的样本直链——不认识的域名
        直接用域名本身当标签。"""
        directory = Path(storage_path) if storage_path else None
        by_category: Dict[str, int] = {}
        total = 0
        if not directory or not directory.exists():
            return {"total": 0, "by_category": {}}

        for strm_file in directory.rglob("*.strm"):
            try:
                content = strm_file.read_text(encoding="utf-8").strip()
            except Exception:
                continue
            total += 1

            remaining, accelerator_label = StrmRelinkService.strip_known_accelerator(content, accelerator_prefixes)
            domain = urlparse(remaining).netloc or "无法识别"
            source_label = domain_to_source.get(domain, domain)
            category = f"{source_label} + {accelerator_label}" if accelerator_label else f"{source_label} 裸链"
            by_category[category] = by_category.get(category, 0) + 1

        return {"total": total, "by_category": by_category}
