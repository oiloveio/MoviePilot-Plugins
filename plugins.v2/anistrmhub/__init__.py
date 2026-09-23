# 本文件是 plugins.v3/anistrmhub/__init__.py 的V2兼容副本。
# V2宿主没有app.sdk.*稳定出口，只能用app.core.config/app.log/app.utils.http这几个旧路径，
# 所以业务逻辑没法跨版本共用同一份源码，只能保留两份——这里除了下面4行import外，
# 其余代码必须和V3版本保持一致。改动业务逻辑时两个文件都要同步改。
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote, urlparse, urlunparse

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.utils.http import RequestUtils

DEFAULT_SUBSCRIPTION_SOURCES = """https://api.pili.cc.cd/ani-download.xml
https://aniapi.op5.de5.net/ani-download.xml
https://api.ani.rip/ani-download.xml
# https://aniapi.v300.eu.org/ani-download.xml  RSS能拉到,但视频直链域名proi.v300.eu.org被Cloudflare挑战拦截,实测无法取到视频数据,已禁用
# https://aniapi.td.ee/ani-download.xml  RSS能拉到,但视频直链域名ani.td.ee返回"temporarily rate limited",实测无法取到视频数据,已禁用
# http://open.ani.rip/ani-download.xml  当前403(Cloudflare拦截)，恢复后可去掉#启用
# https://openani.an-i.workers.dev/ani-download.xml  当前429(限流)，恢复后可去掉#启用"""

# 借鉴shanhai2333/ANiStrmPro：非视频附属文件的常见后缀，直接跳过不生成strm
SUBTITLE_EXTENSIONS = (".srt", ".vtt", ".ass", ".ssa")
# 从直链里提取季度目录，如 .../2026-7/xxx.mp4 -> 2026-7
SEASON_RE = re.compile(r"/(\d{4}-\d{1,2})/")
# 匹配ANi标题/文件名里的集数，形如" - 11 ["，用于资源补齐时定位并替换集数数字。
# 要求前有"-"后有"["，避免误命中季度目录(yyyy-mm)或分辨率(1080P)里的数字。
EPISODE_NUM_RE = re.compile(r"(-\s*)(\d{1,4})(\s*\[)")


class ANiStrmHub(_PluginBase):
    plugin_name = "ANiStrmHub"
    plugin_desc = "订阅源+加速源各选一个生效，自动抓取ANi新番资源生成strm文件，mp刮削入库，媒体服务器直连播放"
    plugin_icon = "https://raw.githubusercontent.com/oiloveio/MoviePilot-Plugins/main/icons/anistrmhub.png"
    plugin_version = "5.0.0"
    plugin_author = "oiloveio,honue"
    author_url = "https://github.com/honue"
    plugin_config_prefix = "anistrmhub_"
    plugin_order = 15
    auth_level = 2

    _enabled = False
    _use_proxy = True
    _cron = None
    _onlyonce = False
    _storageplace = None
    _filename_remove = ""
    _filename_blacklist = ""
    _season_dir = False
    _season_filter: List[str] = ["all"]

    _subscription_sources = DEFAULT_SUBSCRIPTION_SOURCES
    _active_subscription_source: Optional[str] = None
    _accelerator_sources = ""
    _active_accelerator_source: Optional[str] = None

    _target_subscription_source: Optional[str] = None
    _target_accelerator_source: Optional[str] = None
    _apply_local_strm_once = False

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
        self._filename_remove = config.get("filename_remove") or ""
        self._filename_blacklist = config.get("filename_blacklist") or ""
        self._season_dir = config.get("season_dir", False)
        self._season_filter = config.get("season_filter") or ["all"]

        self._subscription_sources = config.get("subscription_sources")
        if not self._subscription_sources:
            self._subscription_sources = DEFAULT_SUBSCRIPTION_SOURCES
        self._active_subscription_source = config.get("active_subscription_source")
        self._accelerator_sources = config.get("accelerator_sources") or ""
        self._active_accelerator_source = config.get("active_accelerator_source")

        self._target_subscription_source = config.get("target_subscription_source")
        self._target_accelerator_source = config.get("target_accelerator_source")
        self._apply_local_strm_once = config.get("apply_local_strm_once", False)

        self._backfill_once = config.get("backfill_once", False)
        self._detect_once = config.get("detect_once", False)

        self._client.set_use_proxy(self._use_proxy)
        logger.info(
            f"ANiStrmHub配置加载：enabled={self._enabled}, onlyonce={self._onlyonce}, "
            f"use_proxy={self._use_proxy}, storage={self._storageplace}, "
            f"生效订阅源={self.__get_active_subscription_source()}, "
            f"生效加速源={self.__get_active_accelerator_source()}"
        )

        if not (
            self._enabled
            or self._onlyonce
            or self._apply_local_strm_once
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

        if self._apply_local_strm_once:
            if self.__is_task_running("apply_local_strm"):
                logger.warning("ANiStrmHub本地strm维护：上一次任务还在运行中，本次跳过排队，等它跑完再重新勾选")
            else:
                logger.info(
                    f"ANiStrmHub服务启动，立即应用本地strm维护：目标订阅源="
                    f"{self._target_subscription_source or '(不变)'}，"
                    f"目标加速源={self._target_accelerator_source or '(不加速)'}"
                )
                self._scheduler.add_job(
                    func=self.__apply_local_strm_task,
                    trigger="date",
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                    name="ANiStrmHub本地strm维护",
                )
            self._apply_local_strm_once = False

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
                logger.info("ANiStrmHub服务启动，立即探测订阅源x加速源连通性矩阵")
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

    @staticmethod
    def __parse_source_list(text: str) -> List[str]:
        """解析多行列表配置(一行一个地址，#开头禁用，去重保序)。订阅源和加速源
        共用同一套解析规则——这是5.0.0重构的核心：两者是完全对称的"列表+单选
        生效项"模型，不再是订阅源多源聚合、加速源列表选择这两套不对称机制"""
        items = []
        for raw_line in (text or "").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            items.append(line.rstrip("/"))
        return list(dict.fromkeys(items))

    def __parse_subscription_sources(self) -> List[str]:
        return self.__parse_source_list(self._subscription_sources)

    def __parse_accelerator_sources(self) -> List[str]:
        return self.__parse_source_list(self._accelerator_sources)

    def __get_active_subscription_source(self) -> Optional[str]:
        """返回当前生效的订阅源：用户选中的那个(如果还在候选列表里)，否则
        退化成列表第一个未禁用的。列表为空返回None。

        同一时刻只有一个订阅源在被使用，不再像旧版那样多个源同时聚合、按
        标题去重、谁先抓到内容就用谁——那套模型正是"一个本该被#禁用的失效
        源意外生效，贡献内容后又被自动套上一层不相关加速前缀"这类bug的
        成因。生效源连不上了也不会自动切换到列表里别的源，而是概览页的
        连通性矩阵会清楚显示出来，由用户自己决定换哪个。"""
        sources = self.__parse_subscription_sources()
        if not sources:
            return None
        if self._active_subscription_source and self._active_subscription_source in sources:
            return self._active_subscription_source
        return sources[0]

    def __get_active_accelerator_source(self) -> Optional[str]:
        """返回当前生效的加速源；None表示不加速，strm写裸直链"""
        sources = self.__parse_accelerator_sources()
        if not sources:
            return None
        if self._active_accelerator_source and self._active_accelerator_source in sources:
            return self._active_accelerator_source
        return sources[0]

    def __build_subscription_options(self) -> List[Dict[str, str]]:
        return [{"title": url, "value": url} for url in self.__parse_subscription_sources()]

    def __build_accelerator_options(self) -> List[Dict[str, str]]:
        return [{"title": url, "value": url} for url in self.__parse_accelerator_sources()]

    def __finalize_strm_link(self, real_link: str) -> str:
        """决定新生成的strm文件里最终写入的地址，__task和__backfill_task这两个
        "生成新文件"的入口都调这一个函数，不各写各的。套用当前生效加速源
        (没配置就是原始直链)。不需要"套壳后探测，不可达就回退裸链接"这类
        运行时安全网——加速源永远只套在"当前生效订阅源"自己的直链上，这个
        组合是否可达，用户在概览页连通性矩阵里已经能提前看到。"""
        active_accelerator = self.__get_active_accelerator_source()
        if active_accelerator:
            return StrmRelinkService.build_proxied_url(real_link, active_accelerator)
        return real_link

    def __build_season_options(self) -> List[Dict[str, str]]:
        """拉当前生效订阅源的样本条目，从里面提取当前RSS窗口内出现过的季度，
        供配置页「拉取季度筛选」下拉用。跟honue原版的"季度多选"不是一回事——
        原版靠的是目录扫描API(能列出所有历史季度文件夹)，那套接口已经确认
        死了(见docs)；这里只能从RSS滚动窗口(近期约50~60条)里已经出现的
        季度里选，选不到没在窗口内的老季度。"""
        seasons = set()
        active_source = self.__get_active_subscription_source()
        if active_source:
            try:
                entries = self._client.fetch_one_source(active_source)
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
        active_source = self.__get_active_subscription_source()
        if not active_source:
            logger.info("未配置任何订阅源，任务结束")
            self.__save_task_status("task", "done", "未配置订阅源")
            return

        logger.info(f"ANiStrmHub任务开始：生效订阅源={active_source}，storage={self._storageplace}")

        try:
            entries = self._client.fetch_one_source(active_source)
        except Exception as err:
            logger.warning(f"ANiStrmHub任务：生效订阅源抓取失败，任务结束：{active_source} - {err}")
            self.__save_task_status("task", "done", f"订阅源抓取失败：{err}")
            return

        if not entries:
            logger.warning("ANiStrmHub生效订阅源RSS无内容，本次任务结束")
            self.__save_task_status("task", "done", "订阅源RSS无内容")
            return

        entries = self.__apply_season_filter(entries)
        if not entries:
            logger.warning("ANiStrmHub季度筛选后没有条目，本次任务结束")
            self.__save_task_status("task", "done", "季度筛选后无条目")
            return

        active_accelerator = self.__get_active_accelerator_source()
        if active_accelerator:
            logger.info(f"ANiStrmHub任务：新生成的strm将自动套用加速源 {active_accelerator}")

        total_created = 0
        total_exists = 0
        total_failed = 0
        total_skipped = 0
        for entry in entries:
            title = entry["title"]
            if StrmFileService.is_subtitle_file(title):
                total_skipped += 1
                continue
            if StrmFileService.is_blacklisted(title, self._filename_blacklist):
                logger.info(f"ANiStrmHub文件名命中黑名单，跳过：{title}")
                total_skipped += 1
                continue

            display_name = StrmFileService.clean_file_name(title, self._filename_remove)
            season = SEASON_RE.search(entry["link"])
            relative_dir = season.group(1) if (self._season_dir and season) else None

            file_url = self.__finalize_strm_link(entry["link"])

            status = self._strm_service.touch_strm_file(
                storage_path=self._storageplace,
                file_name=display_name,
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
            f"订阅源{active_source}共{len(entries)}条，新增={total_created}，"
            f"跳过(已存在)={total_exists}，跳过(字幕/黑名单)={total_skipped}，失败={total_failed}"
        )
        logger.info(f"ANiStrmHub任务完成：{summary}")
        self.__save_task_status("task", "done", summary)

    def __apply_local_strm_task(self):
        """统一的本地strm批量维护：用"目标订阅源"+"目标加速源"两个选择器
        表达所有组合，取代4.0.0四个分开的一次性任务(修复链接/一键换源/
        一键加速/一键还原)：

          目标订阅源=不变 + 目标加速源=不加速  等价于旧版"一键还原"
          目标订阅源=不变 + 目标加速源=选中     等价于旧版"一键加速"
          目标订阅源=选中 + 目标加速源=不加速  等价于旧版"一键切换来源"
          目标订阅源=选中 + 目标加速源=选中     切换来源+加速一步到位(新)

        每个文件先剥掉已知加速源前缀，还原出"当前底层直链"；再按目标订阅源
        决定要不要换成另一个源给出的直链——标题还在该源RSS窗口内的优先精确
        匹配，不在窗口内的用derive_prefix/extract_resource_path拼域名前缀；
        最后按目标加速源决定要不要套壳。写入前实测确认最终地址可达才覆盖，
        不可达保留原文件——这条安全机制从4.0.0延续下来。"""
        self.__save_task_status("apply_local_strm", "running", "进行中")
        directory = Path(self._storageplace)
        if not directory.exists():
            logger.warning(f"ANiStrmHub本地strm维护：目录不存在 {self._storageplace}")
            self.__save_task_status("apply_local_strm", "done", "存储目录不存在")
            return

        target_subscription = self._target_subscription_source or None
        target_accelerator = self._target_accelerator_source or None
        accelerator_prefixes = self.__parse_accelerator_sources()

        title_map: Dict[str, str] = {}
        target_domain_prefix: Optional[str] = None
        if target_subscription:
            try:
                entries = self._client.fetch_one_source(target_subscription)
            except Exception as err:
                logger.warning(
                    f"ANiStrmHub本地strm维护：目标订阅源抓取失败，任务结束：{target_subscription} - {err}"
                )
                self.__save_task_status("apply_local_strm", "done", f"目标订阅源抓取失败：{err}")
                return
            if not entries:
                logger.warning(f"ANiStrmHub本地strm维护：目标订阅源RSS无内容，任务结束：{target_subscription}")
                self.__save_task_status("apply_local_strm", "done", "目标订阅源RSS无内容")
                return
            title_map = {
                StrmFileService.clean_file_name(entry["title"], self._filename_remove): entry["link"]
                for entry in entries
            }
            target_domain_prefix = StrmRelinkService.derive_prefix(entries[0]["link"])

        stats = {
            "标题精确匹配更新": 0,
            "按订阅源迁移更新": 0,
            "仅调整加速套壳": 0,
            "无需更新": 0,
            "探测不可达(保留原文件)": 0,
            "无法识别(保留原文件)": 0,
        }

        for strm_file in sorted(directory.rglob("*.strm")):
            try:
                old_content = strm_file.read_text(encoding="utf-8").strip()
            except Exception as err:
                logger.warning(f"ANiStrmHub本地strm维护：读取失败，跳过 {strm_file.name} - {err}")
                stats["无法识别(保留原文件)"] += 1
                continue

            de_accelerated, _ = StrmRelinkService.strip_known_accelerator(old_content, accelerator_prefixes)

            if target_subscription:
                matched_link = title_map.get(strm_file.stem)
                if matched_link:
                    bare_link = matched_link
                    match_kind = "标题精确匹配更新"
                elif target_domain_prefix:
                    resource_path = StrmRelinkService.extract_resource_path(de_accelerated)
                    if not resource_path:
                        stats["无法识别(保留原文件)"] += 1
                        continue
                    bare_link = target_domain_prefix + resource_path
                    match_kind = "按订阅源迁移更新"
                else:
                    stats["无法识别(保留原文件)"] += 1
                    continue
            else:
                if not de_accelerated.startswith(("http://", "https://")):
                    stats["无法识别(保留原文件)"] += 1
                    continue
                bare_link = de_accelerated
                match_kind = "仅调整加速套壳"

            final_link = (
                StrmRelinkService.build_proxied_url(bare_link, target_accelerator)
                if target_accelerator
                else bare_link
            )

            if final_link == old_content:
                stats["无需更新"] += 1
                continue

            time.sleep(0.3)
            latency_ms, fail_reason = self._relink_service.probe_latency_ms(final_link)
            if latency_ms is not None:
                strm_file.write_text(final_link, encoding="utf-8")
                stats[match_kind] += 1
                logger.info(f"ANiStrmHub本地strm维护：成功({latency_ms}ms) {strm_file.name}")
            else:
                logger.warning(
                    f"ANiStrmHub本地strm维护：候选链接探测不可达({fail_reason})，保留原文件 {strm_file.name}"
                )
                stats["探测不可达(保留原文件)"] += 1

        summary = "，".join(f"{k}={v}" for k, v in stats.items())
        logger.info(f"ANiStrmHub本地strm维护完成：{summary}")
        self.__save_task_status("apply_local_strm", "done", summary)

    def __backfill_task(self):
        """资源补齐：ani-download.xml这个RSS只是滚动窗口，只含近期资源，更早的
        集数不在里面，但ANi同一部剧全部集数的直链只有集数数字不同，其余部分
        (域名/季度目录/文件名其它属性/查询参数)完全一致——这是用户实测确认的：
        把"- 11"手动改成"- 10"依然能播放，说明季度目录是按剧集首播月份命名，
        不是按每一集实际上传日期命名。

        对本地已有的每部剧，从当前最早一集往前递减集数构造候选直链，严格串行
        探测(不并发)、探测间隔sleep、单次任务设总探测数上限，一旦某一集探测
        不可达就停止继续往前探测这部剧(假设更早的集数同样不可达或已下架，
        没必要继续浪费请求)——这几条都是用户明确要求的限流设计，避免被
        目标站点风控封IP。确认可达才写入，不是无脑改写。"""
        self.__save_task_status("backfill", "running", "进行中")
        directory = Path(self._storageplace)
        if not directory.exists():
            logger.warning(f"ANiStrmHub资源补齐：目录不存在 {self._storageplace}")
            self.__save_task_status("backfill", "done", "存储目录不存在")
            return

        max_probes_total = 30
        max_back_per_series = 20
        probe_interval_sec = 1.5

        series_min_ep: Dict[str, Tuple[int, Path]] = {}
        for strm_file in sorted(directory.rglob("*.strm")):
            stem = strm_file.stem
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

            for offset in range(1, max_back_per_series + 1):
                candidate_ep = min_ep - offset
                if candidate_ep < 1:
                    break
                if total_probed >= max_probes_total:
                    break

                candidate_link = StrmRelinkService.build_episode_variant_link(ref_content, candidate_ep)
                candidate_title = StrmRelinkService.build_title_variant(ref_file.stem, candidate_ep)
                if not candidate_link or not candidate_title:
                    break

                candidate_path = ref_file.with_name(f"{candidate_title}.strm")
                if candidate_path.exists():
                    # 这一集本地已经有了(之前补过/正常拉过)，不用重新探测，
                    # 继续往前查更早的集数
                    continue

                final_link = self.__finalize_strm_link(candidate_link)

                time.sleep(probe_interval_sec)
                total_probed += 1
                latency_ms, fail_reason = self._relink_service.probe_latency_ms(final_link)
                if latency_ms is None:
                    logger.info(
                        f"ANiStrmHub资源补齐：{ref_file.stem} 回溯到第{candidate_ep}集不可达"
                        f"({fail_reason})，停止继续往前探测这部剧"
                    )
                    break

                try:
                    candidate_path.write_text(final_link, encoding="utf-8")
                    total_created += 1
                    logger.info(
                        f"ANiStrmHub资源补齐：成功补上第{candidate_ep}集({latency_ms}ms) {candidate_path.name}"
                    )
                except Exception as err:
                    logger.warning(f"ANiStrmHub资源补齐：写入失败 {candidate_path.name} - {err}")

        summary = f"探测{total_probed}次，成功补齐{total_created}集"
        logger.info(f"ANiStrmHub资源补齐完成：{summary}")
        self.__save_task_status("backfill", "done", summary)

    def __detect_task(self):
        """概览页连通性矩阵：对每个未禁用的订阅源取一条样本直链，测"直连"，
        再测"套上每个未禁用的加速源"之后的连通情况。行=订阅源，列=[直连,
        加速源1,加速源2,...]。这张矩阵直接回答"该选哪个订阅源+哪个加速源
        组合"——这正是重构前那次td.ee坏链接真正需要的诊断工具：用户看一眼
        矩阵就知道某个源/某个组合连不通，不用等生成出坏链接才发现。顺带
        统计本地strm按"订阅源+加速源"分类的分布。"""
        self.__save_task_status("detect", "running", "进行中")
        subscription_urls = self.__parse_subscription_sources()
        accelerator_prefixes = self.__parse_accelerator_sources()
        if not subscription_urls:
            logger.warning("ANiStrmHub连通性探测：未配置任何订阅源，任务结束")
            self.__save_task_status("detect", "done", "未配置订阅源")
            return

        rows = []
        domain_to_source: Dict[str, str] = {}
        for sub_url in subscription_urls:
            row: Dict[str, Any] = {"subscription": sub_url, "rss_ok": False, "error": None, "columns": []}
            try:
                entries = self._client.fetch_one_source(sub_url)
            except Exception as err:
                row["error"] = str(err)
                rows.append(row)
                logger.warning(f"ANiStrmHub连通性探测：{sub_url} RSS抓取失败 - {err}")
                continue

            row["rss_ok"] = True
            if not entries:
                row["error"] = "RSS无条目"
                rows.append(row)
                continue

            sample_link = entries[0]["link"]
            sample_domain = urlparse(sample_link).netloc
            domain_to_source[sample_domain] = sub_url

            time.sleep(0.3)
            latency_ms, fail_reason = self._relink_service.probe_latency_ms(sample_link)
            row["columns"].append({"label": "直连", "latency_ms": latency_ms, "error": fail_reason})

            for prefix in accelerator_prefixes:
                candidate = StrmRelinkService.build_proxied_url(sample_link, prefix)
                time.sleep(0.3)
                latency_ms, fail_reason = self._relink_service.probe_latency_ms(candidate)
                row["columns"].append({"label": prefix, "latency_ms": latency_ms, "error": fail_reason})

            rows.append(row)

        checked_at = datetime.now(tz=pytz.timezone(settings.TZ)).strftime("%Y-%m-%d %H:%M:%S")
        self.save_data(
            "connectivity_matrix",
            {"checked_at": checked_at, "rows": rows},
        )

        distribution = StrmRelinkService.scan_local_distribution(
            self._storageplace, domain_to_source, accelerator_prefixes
        )
        self.save_data("local_distribution", {"checked_at": checked_at, **distribution})

        summary = f"{len(subscription_urls)}个订阅源 x {len(accelerator_prefixes)}个加速源，本地strm共{distribution.get('total', 0)}个"
        logger.info(f"ANiStrmHub连通性探测完成：{summary}")
        self.__save_task_status("detect", "done", summary)

    def __save_task_status(self, task_key: str, status: str, summary: str = ""):
        """记录一次性任务(拉取/维护/补齐/探测)的运行状态，供详情页展示进度，
        也用来防止上一次还没跑完时被重复排队"""
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
        """当前插件不注册后端API，探测/维护都走配置开关+详情页缓存展示。
        5.0.0重构时移除了4.0.0的Relay转发实验性功能(本地局域网代理转发
        规划到以后再做，会设计成"加速源列表里的一种类型"，不是这次这种
        独立整体开关模式)。"""
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
                                "props": {"cols": 12, "md": 6},
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
                        ],
                    },
                    self.__section_title("生成规则（可选）"),
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "filename_remove",
                                            "label": "文件名删除字符串（@分隔）",
                                            "placeholder": "ANSUB@NC-Raw",
                                            "hint": "从生成的strm文件名里删掉这些子串，不影响标题匹配",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "filename_blacklist",
                                            "label": "文件名黑名单（@分隔）",
                                            "placeholder": "预告@PV@NCOP",
                                            "hint": "标题命中关键词则跳过，不生成strm",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "season_dir",
                                            "label": "按季度分子目录存放",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VCard",
                        "props": {"variant": "tonal", "color": "primary", "class": "mt-4"},
                        "content": [
                            {
                                "component": "VCardTitle",
                                "props": {"class": "text-subtitle-1"},
                                "text": "订阅源管理——添加多个候选，只有一个在生效",
                            },
                            {
                                "component": "VCardText",
                                "content": [
                                    {
                                        "component": "VRow",
                                        "content": [
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 7},
                                                "content": [
                                                    {
                                                        "component": "VTextarea",
                                                        "props": {
                                                            "model": "subscription_sources",
                                                            "label": "订阅源列表（一行一个RSS地址）",
                                                            "rows": 6,
                                                            "placeholder": DEFAULT_SUBSCRIPTION_SOURCES,
                                                            "hint": "行首加 # 表示禁用这个源(已知连不上)，不要把 # 开头的行"
                                                            "删掉/改掉，不然那些已知失效的域名会被重新启用",
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
                                                            "model": "active_subscription_source",
                                                            "label": "当前生效订阅源",
                                                            "items": self.__build_subscription_options(),
                                                            "clearable": True,
                                                            "hint": "拉新番只从这一个源抓取，不选则默认用列表第一个未禁用的。"
                                                            "先「探测连通性」看哪个能连再选",
                                                            "persistent-hint": True,
                                                        },
                                                    },
                                                    {
                                                        "component": "VSelect",
                                                        "props": {
                                                            "model": "season_filter",
                                                            "label": "拉取季度筛选",
                                                            "items": self.__build_season_options(),
                                                            "multiple": True,
                                                            "chips": True,
                                                            "clearable": True,
                                                            "class": "mt-2",
                                                            "hint": "只在生效订阅源当前RSS窗口内筛选，默认「不筛选」处理全部",
                                                            "persistent-hint": True,
                                                        },
                                                    },
                                                ],
                                            },
                                        ],
                                    },
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VCard",
                        "props": {"variant": "tonal", "color": "warning", "class": "mt-4"},
                        "content": [
                            {
                                "component": "VCardTitle",
                                "props": {"class": "text-subtitle-1"},
                                "text": "加速源管理——国内直连不通ANi官方域名时，配代理/反代地址让strm能连上",
                            },
                            {
                                "component": "VCardText",
                                "content": [
                                    {
                                        "component": "VRow",
                                        "content": [
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 7},
                                                "content": [
                                                    {
                                                        "component": "VTextarea",
                                                        "props": {
                                                            "model": "accelerator_sources",
                                                            "label": "加速源列表（一行一个代理/加速地址）",
                                                            "rows": 3,
                                                            "placeholder": "https://pro.pili.cc.cd",
                                                            "hint": "把strm链接整体包一层这个地址，格式是「加速地址+原链接」，"
                                                            "行首加#表示禁用",
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
                                                            "model": "active_accelerator_source",
                                                            "label": "当前生效加速源",
                                                            "items": self.__build_accelerator_options(),
                                                            "clearable": True,
                                                            "hint": "留空=不加速，strm写裸直链。选中后，新拉的番自动套上这个"
                                                            "加速源，不用手动跑「本地strm维护」",
                                                            "persistent-hint": True,
                                                        },
                                                    }
                                                ],
                                            },
                                        ],
                                    },
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
                                                "props": {"cols": 12, "md": 4},
                                                "content": [
                                                    {
                                                        "component": "VSelect",
                                                        "props": {
                                                            "model": "target_subscription_source",
                                                            "label": "目标订阅源",
                                                            "items": self.__build_subscription_options(),
                                                            "clearable": True,
                                                            "hint": "不选=保持当前来源不变",
                                                            "persistent-hint": True,
                                                        },
                                                    }
                                                ],
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 4},
                                                "content": [
                                                    {
                                                        "component": "VSelect",
                                                        "props": {
                                                            "model": "target_accelerator_source",
                                                            "label": "目标加速源",
                                                            "items": self.__build_accelerator_options(),
                                                            "clearable": True,
                                                            "hint": "不选=不加速(还原成裸直链)",
                                                            "persistent-hint": True,
                                                        },
                                                    }
                                                ],
                                            },
                                            {
                                                "component": "VCol",
                                                "props": {"cols": 12, "md": 4},
                                                "content": [
                                                    {
                                                        "component": "VSwitch",
                                                        "props": {
                                                            "model": "apply_local_strm_once",
                                                            "label": "立即应用到本地strm",
                                                        },
                                                    }
                                                ],
                                            },
                                        ],
                                    },
                                    {
                                        "component": "div",
                                        "props": {"class": "text-caption mt-2"},
                                        "text": "两个选择器搭配使用：目标订阅源不变+目标加速源选中=批量套壳加速；"
                                        "目标订阅源不变+目标加速源不选=还原成裸直链；目标订阅源选中+目标加速源"
                                        "不选=批量切换到指定来源；两个都选=切换来源同时加速。标题还在目标订阅源"
                                        "当前RSS窗口内的优先精确匹配换最新直链，不在窗口内的按路径迁移公式换源。"
                                        "写入前实测探测确认可达才覆盖，探测不通过的保留原文件不动。",
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
                                        "text": "ani-download.xml只是滚动窗口，只含近期资源；ANi同一部剧全部集数的直链"
                                        "只有集数数字不同，季度目录按首播月份命名。对本地已有的剧，从最早一集"
                                        "往前递减集数构造候选直链，严格串行探测+限流(固定间隔+单次任务探测数上限)，"
                                        "一旦某一集探测不可达就停止这部剧继续往前查，避免高频请求被目标站点风控封IP。"
                                        "确认可达才写入，不是无脑改写。手动触发，不进定时任务，运行状态见下方详情页。",
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
                                "text": "连通性探测——订阅源 x 加速源 矩阵",
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
                                                            "label": "立即探测连通性矩阵",
                                                        },
                                                    }
                                                ],
                                            },
                                        ],
                                    },
                                    {
                                        "component": "div",
                                        "props": {"class": "text-caption mt-2"},
                                        "text": "对每个未禁用的订阅源取一条样本直链，测「直连」，再测「套上每个未禁用"
                                        "的加速源」之后的连通情况，结果列成一张矩阵在下方详情页展示。选订阅源/"
                                        "加速源之前先跑一次这个，看清楚哪个组合真的能连，不要凭感觉选。",
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
                                            "text": "emby容器需要设置代理，docker的环境变量必须要有http_proxy代理变量，大小写敏感，否则无法提取媒体信息，具体见readme.\n"
                                            "https://github.com/honue/MoviePilot-Plugins",
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
            "filename_remove": "",
            "filename_blacklist": "",
            "season_dir": False,
            "season_filter": ["all"],
            "subscription_sources": DEFAULT_SUBSCRIPTION_SOURCES,
            "active_subscription_source": None,
            "accelerator_sources": "",
            "active_accelerator_source": None,
            "target_subscription_source": None,
            "target_accelerator_source": None,
            "apply_local_strm_once": False,
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
                "filename_remove": self._filename_remove,
                "filename_blacklist": self._filename_blacklist,
                "season_dir": self._season_dir,
                "season_filter": self._season_filter,
                "subscription_sources": self._subscription_sources,
                "active_subscription_source": self._active_subscription_source,
                "accelerator_sources": self._accelerator_sources,
                "active_accelerator_source": self._active_accelerator_source,
                "target_subscription_source": self._target_subscription_source,
                "target_accelerator_source": self._target_accelerator_source,
                "apply_local_strm_once": self._apply_local_strm_once,
                "backfill_once": self._backfill_once,
                "detect_once": self._detect_once,
            }
        )

    TASK_LABELS = {
        "task": "拉取新番生成strm",
        "apply_local_strm": "本地strm维护",
        "backfill": "资源补齐(回溯集数)",
        "detect": "连通性矩阵探测",
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
                        "text": "还没有探测数据。去插件配置页勾选「立即探测连通性矩阵」跑一次，"
                        "这里会显示每个订阅源直连、以及套上每个加速源之后分别能不能连，"
                        "还有本地已生成的strm按订阅源+加速源的分布。",
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
                                "content": [{"component": "span", "text": row.get("subscription")}],
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
                            "text": f"订阅源 x 加速源 连通性矩阵（探测于 "
                            f"{connectivity_matrix.get('checked_at', '未知时间')}）",
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
    """按URL抓取并解析单个ANi RSS源(ani-download.xml格式)。

    5.0.0起不再持有"数据源列表"这个状态——订阅源改成"列表+单选生效"模型后，
    具体拉哪个源由调用方(ANiStrmHub)决定，这里只负责"给一个URL，抓取解析成
    条目列表"这一件事，供生效订阅源和本地strm维护里的目标订阅源共用。"""

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
        """借鉴shanhai2333/ANiStrmPro：文件名命中黑名单关键词(@分隔，如"预告@PV@NCOP")则跳过"""
        if not blacklist_config:
            return False
        keywords = [kw.strip() for kw in blacklist_config.split("@") if kw.strip()]
        return any(kw in title for kw in keywords)

    @staticmethod
    def clean_file_name(title: str, remove_config: str) -> str:
        """借鉴shanhai2333/ANiStrmPro：从文件名里删除配置的子串(@分隔，如"ANSUB@NC-Raw")"""
        if not remove_config:
            return title
        cleaned = title
        for token in remove_config.split("@"):
            token = token.strip()
            if token:
                cleaned = cleaned.replace(token, "")
        return cleaned or title

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
    """本地strm链接的构造/探测/归类工具集，供「拉新番」「本地strm维护」
    「资源补齐」「连通性探测」共用。

    核心转换公式：
    1. 域名替换（derive_prefix + extract_resource_path）：ANi各镜像的直链
       结构是 {前缀}/{季度}/{文件名}?d=mp4，其中"季度/文件名?d=mp4"这一段
       在所有镜像间完全一致，只有前缀（域名，以及是否带resources.ani.rip
       中间路径）不同。本地strm维护里"切换到目标订阅源"用的就是这个公式。
    2. 前缀拼接（build_proxied_url）：把原始链接整体包一层反代前缀，格式仿
       "Proxy Everything"这类通用反代工具的用法，保留原host不做域名替换。
       加速源套壳用的是这个公式，跟上面的域名替换是两种不同的转换，不要
       混用。
    3. strip_known_accelerator是build_proxied_url的逆运算，本地strm维护里
       用来判断一个strm当前是不是已经被某个已知加速源包过壳。
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
        结果 https://pro.pili.cc.cd/resources.ani.rip/2025-10/xxx?d=mp4

        跟derive_prefix/extract_resource_path那套"域名替换"(丢掉原host)是
        两种不同的转换——这个是"前缀拼接"(保留原host)，按用户实测确认的
        真实反代格式来，两者不要混用。"""
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

    def probe_latency_ms(self, url: str) -> Tuple[Optional[float], Optional[str]]:
        """探测直链能不能连通、连通要多久（只请求1个字节，类似ping）。
        返回(延迟ms, None)表示成功；返回(None, 失败原因)表示不可达——原因写清楚
        具体HTTP状态码或异常信息，不能只留一句"不可达"就没了，不然出问题
        没法分清到底是链接真死了、单纯超时、还是被限流，这个坑已经踩过。"""
        try:
            # 注意：get_res(url, headers=...)里的headers会整体替换掉RequestUtils构造时
            # 设置的默认header(包括UA)，不是合并。必须用update_headers()把Range头合并
            # 进去，否则探测请求会变成没有UA的裸请求，容易被目标站点当可疑流量拦截(403)，
            # 导致本来可达的链接被误判为不可达——这个坑已经在联调时实测踩过。
            request_utils = self._request_factory()
            request_utils.update_headers({"Range": "bytes=0-0"})
            start = time.monotonic()
            response = request_utils.get_res(url)
            elapsed_ms = (time.monotonic() - start) * 1000
            if not response:
                return None, "无响应(连接失败或超时)"
            if response.status_code not in (200, 206):
                return None, f"HTTP {response.status_code}"
            return round(elapsed_ms, 1), None
        except Exception as err:
            return None, f"异常:{err}"

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
        用于详情页展示分布。domain_to_source是"样本域名 -> 订阅源RSS地址"的
        映射，来自__detect_task当次探测各订阅源拿到的样本直链——不认识的域名
        直接用域名本身当标签。"""
        directory = Path(storage_path)
        by_category: Dict[str, int] = {}
        total = 0
        if not directory.exists():
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
