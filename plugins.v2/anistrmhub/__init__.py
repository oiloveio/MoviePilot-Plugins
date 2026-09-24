# 本文件是 plugins.v3/anistrmhub/__init__.py 的V2兼容副本。
# V2宿主没有app.sdk.*稳定出口，只能用app.core.config/app.log/app.utils.http这几个旧路径，
# 所以业务逻辑没法跨版本共用同一份源码，只能保留两份——这里除了下面4行import外，
# 其余代码必须和V3版本保持一致。改动业务逻辑时两个文件都要同步改。
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote, unquote, urlparse, urlunparse

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.utils.http import RequestUtils

# 内置订阅源：只作为首次安装时「订阅源列表」的初始内容，列表完全由用户维护，
# 可增删改——内置地址日后失效时用户直接删掉或替换，不依赖插件更新。用哪个
# 由用户选，插件不在后台自动切换。订阅源分两类：社区镜像下发的视频直链已经
# 自带镜像方的加速；ANi 官方下发的是未加速的官方直链，需要自己配置加速源。
# 默认给新用户已加速的镜像，开箱即用。
BUILTIN_SUBSCRIPTIONS: Tuple[Tuple[str, str], ...] = (
    ("https://api.pili.cc.cd/ani-download.xml", "pili 镜像（已加速）"),
    ("https://aniapi.op5.de5.net/ani-download.xml", "op5 镜像（已加速）"),
    ("https://api.ani.rip/ani-download.xml", "ANi 官方（未加速）"),
)
DEFAULT_SUBSCRIPTION_SOURCE = BUILTIN_SUBSCRIPTIONS[0][0]
# 内置加速源：同样只作为「加速源列表」的初始内容。这两个就是上面两个镜像
# 自带加速所用的反代节点，也可以单独套在官方订阅源的直链上使用。
BUILTIN_ACCELERATORS: Tuple[Tuple[str, str], ...] = (
    ("https://pro.pili.cc.cd", "pili 节点"),
    ("https://pro.op5.de5.net", "op5 节点"),
)
# ANi官方直链域名，用于重建官方直链——不管当前strm内容被套了
# 几层壳，extract_resource_path()都能从URL末尾定位出跟域名无关的"季度/
# 文件名?query"这一段，配上这个官方域名就能拼出一个确定的官方直链，不需要
# "记住"当初到底是被哪个加速源套过壳(单值配置模型下也没地方存这个记忆)。
OFFICIAL_BASE_URL = "https://resources.ani.rip"

# 非正片附属文件的标题关键词，固定常量不再作为配置项——预告/OP/ED这类标记
# 在几乎所有ANi/fansub命名习惯里含义固定，没必要为此暴露一个配置项
NON_EPISODE_BLACKLIST = "预告@PV@NCOP@NCED"
SUBTITLE_EXTENSIONS = (".srt", ".vtt", ".ass", ".ssa")
# 从直链里提取季度目录，如 .../2026-7/xxx.mp4 -> 2026-7
SEASON_RE = re.compile(r"/(\d{4}-\d{1,2})/")
# 匹配ANi标题/文件名里的集数，形如" - 11 ["，用于补全历史剧集时定位并替换集数数字。
# 要求前有"-"后有"["，避免误命中季度目录(yyyy-mm)或分辨率(1080P)里的数字。
EPISODE_NUM_RE = re.compile(r"(-\s*)(\d{1,4})(\s*\[)")
# 从ANi标题里切出剧名，用于"按剧集分目录"存放时的目录归档。ANi命名格式固定是
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
OFFICIAL_HOST = urlparse(OFFICIAL_BASE_URL).netloc
# 播放线路测速：下载视频开头一段测持续吞吐，按"量"和"时"双重封顶——4MB
# 足够让读数稳定，8秒封顶保证慢线路不会拖住检测任务。
SPEED_TEST_BYTES = 4 * 1024 * 1024
SPEED_TEST_MAX_SECONDS = 8
SPEED_TEST_CHUNK_BYTES = 64 * 1024
# ANi 1080P 单集约 570MB/24分钟，折合约 400KB/s(3.2Mbps)。线路速度低于这个值
# 播放必然卡顿，达到两倍以上才有余量应对波动。
PLAYBACK_BITRATE_KBPS = 400
# 维护类任务连续探测失败到这个次数就中止。目标地址整体不可达时逐个文件磨
# 下去毫无意义：实测有一次跑满72分钟、1461个文件全部"无响应(连接失败或
# 超时)"、最终一个文件都没改。
MAX_CONSECUTIVE_PROBE_FAILURES = 10


class ANiStrmHub(_PluginBase):
    plugin_name = "ANiStrmHub"
    plugin_desc = "开箱即用的ANi新番strm生成：内置已加速订阅源，也可选官方源自配加速；mp刮削入库，媒体服务器直连播放"
    plugin_icon = "https://raw.githubusercontent.com/oiloveio/MoviePilot-Plugins/main/icons/anistrmhub.png"
    plugin_version = "0.10.0"
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
    _subscription_list = "\n".join(url for url, _ in BUILTIN_SUBSCRIPTIONS)
    _accelerator_list = "\n".join(prefix for prefix, _ in BUILTIN_ACCELERATORS)

    _refresh_subscription_once = False
    _regroup_once = False
    _backfill_once = False
    _detect_once = False
    _scheduler: Optional[BackgroundScheduler] = None

    def __init__(self):
        super().__init__()
        self._client = AniRssAggregator()
        self._strm_service = StrmFileService()
        self._relink_service = StrmRelinkService(request_factory=self._client.build_direct_request_utils)

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

        # 首次安装(键不存在)用默认订阅源；用户留空保存时按列表第一条，见__active_subscription
        subscription_source = config.get("subscription_source")
        self._subscription_source = DEFAULT_SUBSCRIPTION_SOURCE if subscription_source is None else subscription_source.strip()
        self._accelerator_prefix = (config.get("accelerator_prefix") or "").strip().rstrip("/")
        # 列表键不存在(首次安装)才填入内置地址；用户清空列表后保存的是空字符串，
        # 尊重用户的选择，不再自动补回
        subscription_list = config.get("subscription_list")
        self._subscription_list = (
            "\n".join(url for url, _ in BUILTIN_SUBSCRIPTIONS) if subscription_list is None else subscription_list
        )
        accelerator_list = config.get("accelerator_list")
        self._accelerator_list = (
            "\n".join(prefix for prefix, _ in BUILTIN_ACCELERATORS) if accelerator_list is None else accelerator_list
        )

        self._refresh_subscription_once = config.get("refresh_subscription_once", False)
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
                    name="ANiStrmHub订阅同步",
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
                name="ANiStrmHub订阅同步",
            )
            self._onlyonce = False

        pending: List[Tuple[str, str, Any]] = []
        if self._refresh_subscription_once:
            pending.append(("refresh_subscription", "重建直链", self.__refresh_subscription_task))
            self._refresh_subscription_once = False
        if self._regroup_once:
            pending.append(("regroup", "重建目录结构", self.__regroup_local_strm_task))
            self._regroup_once = False
        if self._backfill_once:
            pending.append(("backfill", "补全历史剧集", self.__backfill_task))
            self._backfill_once = False
        if self._detect_once:
            pending.append(("detect", "连通性检测", self.__detect_task))
            self._detect_once = False

        if pending:
            logger.info(
                "ANiStrmHub服务启动，依次执行维护任务：" + "、".join(name for _, name, _ in pending)
            )
            self._scheduler.add_job(
                func=self.__run_pending_tasks,
                args=[pending],
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="ANiStrmHub维护任务",
            )

        self.__update_config()

        if self._scheduler.get_jobs():
            self._scheduler.print_jobs()
            self._scheduler.start()

    def __run_pending_tasks(self, pending: List[Tuple[str, str, Any]]) -> None:
        """把勾选的多个维护开关串成一条队列依次执行，不并发。

        这些任务会扫描并改写同一批 strm 文件：并发跑会互相看到对方写到一半的
        中间状态，本来就受限流约束的探测请求也会成倍增加。实测日志里出现过
        一次同时触发四个任务、彼此交叠运行的情况。"""
        for task_key, task_name, func in pending:
            if self.__is_task_running(task_key):
                logger.warning(f"ANiStrmHub{task_name}：上一次任务还在运行中，本次跳过")
                continue
            try:
                func()
            except Exception as err:
                logger.error(f"ANiStrmHub{task_name}：任务异常终止 - {err}")
                self.__save_task_status(task_key, "done", f"任务异常终止：{err}")

    @staticmethod
    def parse_url_lines(text: Optional[str], strip_slash: bool = False) -> List[str]:
        """解析用户维护的地址列表：一行一个，# 开头为注释，忽略空行和非http(s)
        开头的行，去重并保持用户填写的顺序。加速源是拼接前缀，strip_slash=True
        去掉末尾的/，避免拼出双斜杠。"""
        urls: List[str] = []
        for line in (text or "").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if not line.lower().startswith(("http://", "https://")):
                logger.warning(f"ANiStrmHub：地址列表中忽略无效行（需以http://或https://开头）：{line}")
                continue
            urls.append(line.rstrip("/") if strip_slash else line)
        return list(dict.fromkeys(urls))

    def __subscription_options(self) -> List[str]:
        """订阅源下拉选项 = 用户维护的列表；当前选中的即使不在列表里也保留"""
        options = self.parse_url_lines(self._subscription_list)
        current = (self._subscription_source or "").strip()
        return list(dict.fromkeys(([current] if current else []) + options))

    def __accelerator_options(self) -> List[str]:
        options = self.parse_url_lines(self._accelerator_list, strip_slash=True)
        current = (self._accelerator_prefix or "").strip()
        return list(dict.fromkeys(([current] if current else []) + options))

    @staticmethod
    def __display_name(url: str, builtin: Tuple[Tuple[str, str], ...]) -> str:
        """内置地址显示内置名称，自定义地址显示域名"""
        return dict(builtin).get(url) or (urlparse(url).netloc or url)

    def __active_subscription(self) -> str:
        """当前生效的订阅源：用户选定的那个；没选时取订阅源列表第一条；
        列表也为空返回空字符串，由调用方报"未配置订阅源"，不回退到代码里的内置地址。"""
        current = (self._subscription_source or "").strip()
        if current:
            return current
        listed = self.parse_url_lines(self._subscription_list)
        return listed[0] if listed else ""

    def __check_accelerator_for_this_run(self, sample_link: str) -> Optional[str]:
        """加速源现在只有一个配置值。用这一轮实际抓到的样本直链实测一次
        "这个加速源套上这条直链"能不能连通——只测一次，不是每条目都测，
        拉新番的速度不受影响；这一次检查是避免"加速源连不通却批量生成
        坏链接"的安全网。探测不通过就本次不生成、在运行状态里写明原因，
        不会悄悄换成别的线路——用哪条线路始终由用户的配置决定。只测通不通
        不测速，线路快慢由「连通性检测」负责。

        返回None表示可以继续，返回字符串表示不可达的原因。"""
        prefix = (self._accelerator_prefix or "").strip()
        if not prefix:
            return None
        candidate = StrmRelinkService.compose_link(sample_link, prefix)
        latency_ms, fail_reason = self._relink_service.probe_latency_ms(candidate)
        if latency_ms is None:
            return fail_reason or "不可达"
        logger.info(f"ANiStrmHub订阅同步：加速源{prefix}探测可达({latency_ms}ms)")
        return None

    def __resolve_relative_dir(self, file_name: str) -> Optional[str]:
        """按当前"strm存放方式"决定这个文件该放在storageplace下的哪个子目录：
        平铺存放返回None(直接放根目录)，按剧集分目录返回剧名目录。

        决定目录的逻辑只有这一处——订阅同步(__task)和重建目录结构
        (__regroup_local_strm_task)都调它；补全历史剧集用ref_file.with_name()
        天然跟参照文件同目录、重建直链是原地改写内容不挪
        位置，所以那三个任务不需要各自再解析一遍剧名(各写各的正是"同一部剧
        一半在文件夹里一半在根目录"这类分裂的来源)。"""
        if self._strm_layout != LAYOUT_BY_TITLE:
            return None
        series = StrmFileService.extract_series_title(file_name)
        if not series:
            logger.info(f"ANiStrmHub：无法解析剧名，改为平铺存放：{file_name}")
            return None
        return StrmFileService.safe_dir_name(series)

    def __finalize_strm_link(self, real_link: str) -> str:
        """补全历史剧集用：按当前加速源重组链接(没配置就是官方直链)。参照文件
        可能是任意历史线路，统一经compose_link重组，不会叠出多层壳。这个任务
        本身对每个候选都会探测最终地址，天然有安全网，不需要像__task那样
        额外做一次性的"这个组合能不能用"预检查。"""
        return StrmRelinkService.compose_link(real_link, self._accelerator_prefix)

    def __build_season_options(self) -> List[Dict[str, str]]:
        """拉当前配置订阅源的样本条目，从里面提取当前RSS窗口内出现过的季度，
        供配置页「拉取季度筛选」下拉用。"""
        seasons = set()
        subscription = self.__active_subscription()
        try:
            entries = self._client.fetch_one_source(subscription) if subscription else []
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
        used_source = self.__active_subscription()
        if not used_source:
            logger.warning("ANiStrmHub订阅同步：未配置订阅源，本次任务结束")
            self.__save_task_status("task", "done", "未配置订阅源")
            return
        try:
            entries = self._client.fetch_one_source(used_source)
        except Exception as err:
            logger.warning(f"ANiStrmHub订阅同步：订阅源{used_source}抓取失败，本次任务结束 - {err}")
            self.__save_task_status("task", "done", f"订阅源抓取失败：{err}")
            return

        if not entries:
            logger.warning("ANiStrmHub订阅同步：订阅源RSS无内容，本次任务结束")
            self.__save_task_status("task", "done", "订阅源RSS无内容")
            return

        entries = self.__apply_season_filter(entries)
        if not entries:
            logger.warning("ANiStrmHub订阅同步：季度筛选后没有条目，本次任务结束")
            self.__save_task_status("task", "done", "季度筛选后无条目")
            return

        accelerator = (self._accelerator_prefix or "").strip()
        unreachable_reason = self.__check_accelerator_for_this_run(entries[0]["link"])
        if unreachable_reason:
            logger.warning(
                f"ANiStrmHub订阅同步：加速源{accelerator}探测不可达({unreachable_reason})，本次不生成，"
                f"下次定时运行会重试；可运行「连通性检测」对比线路后更换加速源"
            )
            self.__save_task_status("task", "done", f"加速源不可达，本次未生成：{unreachable_reason}")
            return

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
                logger.info(f"ANiStrmHub订阅同步：标题命中非正片关键词，跳过：{title}")
                total_skipped += 1
                continue

            relative_dir = self.__resolve_relative_dir(title)

            # 加速源留空：原样使用订阅源下发的链接(镜像源自带加速，官方源是官方直链)；
            # 填了加速源：先剥掉镜像自带的那层再套，不会叠出两层壳
            file_url = StrmRelinkService.compose_link(entry["link"], accelerator)

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
        logger.info(f"ANiStrmHub订阅同步完成：{summary}")
        self.__save_task_status("task", "done", summary)

    def __rewrite_local_strm(
        self,
        task_key: str,
        task_name: str,
        kind_labels: List[str],
        resolve,
    ) -> Dict[str, int]:
        """维护类任务共用的"扫描-改写"流程：遍历本地 strm，由 resolve 算出目标
        链接，探测确认可达才覆盖写入。resolve(strm_file, old_content) 返回
        (目标链接, 计数分类)；返回 (None, 计数分类) 表示这个文件只计数不处理。

        三条共用的保护，都来自真实日志里暴露的问题：

        1. **文件中途消失不算异常**：文件列表在任务开头一次性取出，而整个任务
           可能跑几十分钟，这期间目录监控转移走文件是正常的。按"已移除"计数，
           不再当成"无法识别"逐条报警(实测有一次刷了 499 条 WARNING)。
        2. **连续探测失败熔断**：目标地址整体不可达时，逐个文件磨下去毫无意义
           ——实测有一次跑满 72 分钟、1461 个文件全部超时、最终一个都没改。
           连续失败达到 MAX_CONSECUTIVE_PROBE_FAILURES 就中止并在摘要里说明。
        3. **逐文件日志降噪**：成功一律 debug，失败只有前几条 warning，其余
           debug。数量统计交给结束时的汇总——实测一次任务刷了近 5000 行 INFO。
        """
        directory = Path(self._storageplace) if self._storageplace else None
        if not directory or not directory.exists():
            logger.warning(f"ANiStrmHub{task_name}：目录不存在 {self._storageplace}")
            self.__save_task_status(task_key, "done", "存储目录不存在")
            return {}

        stats: Dict[str, int] = {label: 0 for label in kind_labels}
        for label in ("无需更新", "探测不可达(保留原文件)", "无法识别(保留原文件)", "已移除(跳过)"):
            stats.setdefault(label, 0)

        consecutive_failures = 0
        aborted_reason = None
        logged_failures = 0

        for strm_file in sorted(directory.rglob("*.strm")):
            try:
                old_content = strm_file.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                logger.debug(f"ANiStrmHub{task_name}：文件已不在，跳过 {strm_file.name}")
                stats["已移除(跳过)"] += 1
                continue
            except Exception as err:
                logger.warning(f"ANiStrmHub{task_name}：读取失败，跳过 {strm_file.name} - {err}")
                stats["无法识别(保留原文件)"] += 1
                continue

            final_link, match_kind = resolve(strm_file, old_content)
            if not final_link:
                stats[match_kind] += 1
                continue
            if final_link == old_content:
                stats["无需更新"] += 1
                continue

            time.sleep(0.3)
            latency_ms, fail_reason = self._relink_service.probe_latency_ms(final_link)
            if latency_ms is not None:
                consecutive_failures = 0
                try:
                    strm_file.write_text(final_link, encoding="utf-8")
                except FileNotFoundError:
                    stats["已移除(跳过)"] += 1
                    continue
                stats[match_kind] += 1
                logger.debug(f"ANiStrmHub{task_name}：已更新({latency_ms}ms) {strm_file.name}")
                continue

            stats["探测不可达(保留原文件)"] += 1
            consecutive_failures += 1
            message = f"ANiStrmHub{task_name}：探测不可达({fail_reason})，保留原文件 {strm_file.name}"
            if logged_failures < 3:
                logger.warning(message)
                logged_failures += 1
            else:
                logger.debug(message)

            if consecutive_failures >= MAX_CONSECUTIVE_PROBE_FAILURES:
                aborted_reason = f"连续{consecutive_failures}次探测不可达({fail_reason})，判定目标地址整体不通，已中止"
                logger.warning(f"ANiStrmHub{task_name}：{aborted_reason}")
                break

        summary = "，".join(f"{k}={v}" for k, v in stats.items() if v or k in kind_labels)
        if aborted_reason:
            summary = f"{aborted_reason}；{summary}"
        logger.info(f"ANiStrmHub{task_name}完成：{summary}")
        self.__save_task_status(task_key, "done", summary)
        return stats

    def __refresh_subscription_task(self):
        """按当前的「订阅源 + 加速源」重建本地全部 strm 的链接，结果与订阅同步
        新生成的strm完全一致——换了订阅源、换了加速源、清空了加速源，都跑这
        一个任务：
        - 标题还在 RSS 窗口内：直接用订阅源给出的最新链接；
        - 不在窗口内：取原文件的资源路径("季度/文件名?query"，与线路无关)，
          接到当前订阅源的线路前缀上；
        - 最后经compose_link：加速源留空保持订阅源线路，填了就换成该加速源。"""
        self.__save_task_status("refresh_subscription", "running", "进行中")
        # 先确认目录存在再抓RSS：目录都不在就没必要白发一次网络请求
        directory = Path(self._storageplace) if self._storageplace else None
        if not directory or not directory.exists():
            logger.warning(f"ANiStrmHub重建直链：目录不存在 {self._storageplace}")
            self.__save_task_status("refresh_subscription", "done", "存储目录不存在")
            return

        subscription = self.__active_subscription()
        if not subscription:
            logger.warning("ANiStrmHub重建直链：未配置订阅源，任务结束")
            self.__save_task_status("refresh_subscription", "done", "未配置订阅源")
            return
        try:
            entries = self._client.fetch_one_source(subscription)
        except Exception as err:
            logger.warning(f"ANiStrmHub重建直链：订阅源抓取失败，任务结束：{subscription} - {err}")
            self.__save_task_status("refresh_subscription", "done", f"订阅源抓取失败：{err}")
            return
        if not entries:
            logger.warning(f"ANiStrmHub重建直链：订阅源RSS无内容，任务结束：{subscription}")
            self.__save_task_status("refresh_subscription", "done", "订阅源RSS无内容")
            return

        title_map = {entry["title"]: entry["link"] for entry in entries}
        source_prefix = StrmRelinkService.derive_prefix(entries[0]["link"]) or f"{OFFICIAL_BASE_URL}/"
        accelerator = (self._accelerator_prefix or "").strip()

        def resolve(strm_file: Path, old_content: str) -> Tuple[Optional[str], str]:
            matched_link = title_map.get(strm_file.stem)
            if matched_link:
                source_link, match_kind = matched_link, "标题精确匹配更新"
            else:
                resource_path = StrmRelinkService.extract_resource_path(old_content)
                if not resource_path:
                    return None, "无法识别(保留原文件)"
                source_link, match_kind = source_prefix + resource_path, "路径迁移更新"
            return StrmRelinkService.compose_link(source_link, accelerator), match_kind

        self.__rewrite_local_strm(
            "refresh_subscription",
            "重建直链",
            ["标题精确匹配更新", "路径迁移更新"],
            resolve,
        )

    def __regroup_local_strm_task(self):
        """按当前「strm 存放方式」重建本地已有 strm 的目录结构：选"按剧集
        分目录"就把散在根目录(以及历史版本留下的季度目录)里的文件搬进
        {剧名}/子目录，选"平铺存放"就把{剧名}/子目录里的文件搬回根目录。

        只移动文件，不改文件内容，不发任何网络请求——改存放方式是纯本地
        整理，跟链接能不能连通是两回事，没必要在这里探测。目标位置已经有
        同名文件时保留原文件不动，不覆盖。搬完清理掉空掉的子目录。"""
        self.__save_task_status("regroup", "running", "进行中")
        directory = Path(self._storageplace) if self._storageplace else None
        if not directory or not directory.exists():
            logger.warning(f"ANiStrmHub重建目录结构：目录不存在 {self._storageplace}")
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
                logger.warning(f"ANiStrmHub重建目录结构：目标位置已有同名文件，保留原文件 {strm_file.name}")
                stats["目标已存在(保留原文件)"] += 1
                continue

            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                strm_file.rename(target)
                stats["识别不出剧名(平铺到根目录)" if unrecognized else "已归档"] += 1
                logger.debug(f"ANiStrmHub重建目录结构：{strm_file.name} -> {target.parent.name or '根目录'}")
            except Exception as err:
                logger.warning(f"ANiStrmHub重建目录结构：移动失败 {strm_file.name} - {err}")
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
        logger.info(f"ANiStrmHub重建目录结构完成：{summary}")
        self.__save_task_status("regroup", "done", summary)

    def __backfill_task(self):
        """补全历史剧集：ani-download.xml这个RSS只是滚动窗口，只含近期资源，更早的
        集数不在里面，但ANi同一部剧全部集数的直链只有集数数字不同——实测确认
        把"- 11"改成"- 10"依然能播放。

        季度目录不总是跟本地已有的最早一集相同：实测确认过一拳超人第三季
        真实起点第25集、SPY×FAMILY第三季真实起点第38集都落在比本地当前
        最早集更靠前的季度文件夹里。所以碰到当前季度文件夹探测不通，不能
        直接弃剧——按跟当前季度的月份距离由近到远，尝试本地已知的其它季度
        文件夹组合，全部试过还是不通才停止继续往前探测这部剧。命中的季度
        会作为下一集的优先候选(大概率连续几集在同一个文件夹)。

        对本地已有的每部剧，从当前最早一集往前递减集数构造候选，严格串行
        探测(不并发)、探测间隔sleep、单次任务设总探测数上限、每集尝试的
        季度候选数上限——这几条限流设计是为了避免被目标站点风控封IP。确认
        可达才写入，不做无验证的批量改写。"""
        self.__save_task_status("backfill", "running", "进行中")
        directory = Path(self._storageplace)
        if not directory.exists():
            logger.warning(f"ANiStrmHub补全历史剧集：目录不存在 {self._storageplace}")
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
            logger.info("ANiStrmHub补全历史剧集：本地没有可识别集数的strm，任务结束")
            self.__save_task_status("backfill", "done", "本地无可识别集数的资源")
            return

        total_probed = 0
        total_created = 0

        for min_ep, ref_file in series_min_ep.values():
            if total_probed >= max_probes_total:
                logger.info("ANiStrmHub补全历史剧集：本次任务探测次数已达上限，剩余剧集留到下次手动运行")
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
                                f"ANiStrmHub补全历史剧集：成功补上第{candidate_ep}集"
                                f"(季度={season_option}，{latency_ms}ms) {candidate_path.name}"
                            )
                        except Exception as err:
                            logger.warning(f"ANiStrmHub补全历史剧集：写入失败 {candidate_path.name} - {err}")
                        break
                    logger.debug(
                        f"ANiStrmHub补全历史剧集：{ref_file.stem} 第{candidate_ep}集在季度{season_option}"
                        f"不可达({fail_reason})，尝试下一个候选季度"
                    )

                if not found:
                    logger.info(
                        f"ANiStrmHub补全历史剧集：{ref_file.stem} 回溯到第{candidate_ep}集，"
                        f"尝试过的{len(season_candidates)}个季度文件夹均不可达，停止继续往前探测这部剧"
                    )
                    break

        summary = f"探测{total_probed}次，成功补齐{total_created}集"
        logger.info(f"ANiStrmHub补全历史剧集完成：{summary}")
        self.__save_task_status("backfill", "done", summary)

    def __detect_task(self):
        """分两层检测，两层回答的是不同问题：
        1. 订阅源：RSS(XML)能不能拿到。只决定"能不能发现新番"，跟播放快慢无关；
        2. 播放线路：取一条真实视频直链，分别测官方直链、各内置加速节点和用户
           自定义的加速源，每条线路测首包耗时和持续下载速度——这才决定媒体
           服务器起播快慢、播放卡不卡。
        用户维护的订阅源列表、加速源列表里的每个地址都会一起测，结果标注
        "当前使用"和"最快"，换不换由用户在配置里决定，插件不自动切换。

        视频探测不走MP代理(播放器本身不经过它)。只手动触发不进定时任务，每条
        线路只下载开头几MB、线路之间间隔请求，避免被目标站点风控。"""
        self.__save_task_status("detect", "running", "进行中")
        configured = self.__active_subscription()
        accelerator = (self._accelerator_prefix or "").strip().rstrip("/")

        subscriptions: List[Dict[str, Any]] = []
        configured_sample: Optional[Dict[str, str]] = None
        fallback_sample: Optional[Dict[str, str]] = None
        extra_nodes: List[str] = []
        for url in dict.fromkeys(([configured] if configured else []) + self.parse_url_lines(self._subscription_list)):
            row: Dict[str, Any] = {
                "url": url,
                "label": self.__display_name(url, BUILTIN_SUBSCRIPTIONS),
                "current": url == configured,
                "ok": False,
                "entries": 0,
                "elapsed_ms": None,
                "error": None,
            }
            start = time.monotonic()
            try:
                entries = self._client.fetch_one_source(url)
            except Exception as err:
                row["error"] = str(err)
                subscriptions.append(row)
                logger.warning(f"ANiStrmHub连通性检测：订阅源{url}抓取失败 - {err}")
                continue
            row["ok"] = True
            row["elapsed_ms"] = round((time.monotonic() - start) * 1000, 1)
            row["entries"] = len(entries)
            if entries:
                if url == configured:
                    configured_sample = entries[0]
                elif fallback_sample is None:
                    fallback_sample = entries[0]
                embedded = StrmRelinkService.embedded_accelerator(entries[0]["link"])
                if embedded:
                    extra_nodes.append(embedded)
            else:
                row["error"] = "RSS无条目"
            subscriptions.append(row)

        sample = configured_sample or fallback_sample
        # 当前实际使用的线路：填了加速源就是加速源；没填就是当前订阅源自带的线路
        # (镜像源是镜像的加速节点，官方源是官方直链)
        if accelerator:
            current_prefix: Optional[str] = accelerator
        elif configured_sample:
            current_prefix = StrmRelinkService.embedded_accelerator(configured_sample["link"])
        else:
            current_prefix = "unknown"
        source_is_official = bool(configured_sample) and (
            StrmRelinkService.embedded_accelerator(configured_sample["link"]) is None
        )

        routes: List[Dict[str, Any]] = []
        if sample:
            plan: List[Tuple[str, Optional[str]]] = [("官方直链", None)]
            listed = self.parse_url_lines(self._accelerator_list, strip_slash=True)
            for prefix in dict.fromkeys(([accelerator] if accelerator else []) + listed + extra_nodes):
                if any(prefix == existing for _, existing in plan):
                    continue
                if prefix in listed or prefix == accelerator:
                    label = self.__display_name(prefix, BUILTIN_ACCELERATORS)
                else:
                    label = f"镜像节点 {urlparse(prefix).netloc}"
                plan.append((label, prefix))
            for label, prefix in plan:
                url = StrmRelinkService.compose_link(sample["link"], prefix) if prefix else StrmRelinkService.to_official_link(sample["link"])
                time.sleep(0.5)
                measured = self._relink_service.measure_playback(url)
                routes.append(
                    {
                        "label": label,
                        "prefix": prefix,
                        "display": prefix or OFFICIAL_BASE_URL,
                        "current": prefix == current_prefix,
                        **measured,
                    }
                )
                logger.info(
                    f"ANiStrmHub连通性检测：{label} {prefix or OFFICIAL_BASE_URL} - "
                    + (
                        f"首包{measured['first_byte_ms']}ms，速度{measured['speed_kbps']}KB/s"
                        if measured.get("error") is None
                        else f"不可达({measured['error']})"
                    )
                )

        verdict_level, verdict = self.__judge_routes(routes, bool(accelerator), source_is_official)
        checked_at = datetime.now(tz=pytz.timezone(settings.TZ)).strftime("%Y-%m-%d %H:%M:%S")
        self.save_data(
            "detect_result",
            {
                "checked_at": checked_at,
                "subscriptions": subscriptions,
                "sample_title": (sample or {}).get("title"),
                "routes": routes,
                "verdict": verdict,
                "verdict_level": verdict_level,
            },
        )

        if accelerator:
            expected_route: Optional[str] = StrmRelinkService.describe_route(
                StrmRelinkService.compose_link(f"{OFFICIAL_BASE_URL}/2000-1/sample", accelerator), accelerator
            )
        elif configured_sample:
            expected_route = StrmRelinkService.describe_route(configured_sample["link"])
        else:
            expected_route = None
        distribution = StrmRelinkService.scan_local_distribution(self._storageplace, accelerator, expected_route)
        self.save_data("local_distribution", {"checked_at": checked_at, **distribution})

        ok_subscriptions = sum(1 for row in subscriptions if row["ok"] and row["entries"])
        ok_routes = sum(1 for route in routes if route.get("error") is None)
        summary = (
            f"订阅源可用{ok_subscriptions}/{len(subscriptions)}，播放线路可达{ok_routes}/{len(routes)}，"
            f"本地strm共{distribution.get('total', 0)}个；{verdict}"
        )
        logger.info(f"ANiStrmHub连通性检测完成：{summary}")
        self.__save_task_status("detect", "done", summary)

    @staticmethod
    def __judge_routes(routes: List[Dict[str, Any]], has_accelerator: bool, source_is_official: bool) -> Tuple[str, str]:
        """根据测速结果给出一句可执行的建议，只建议不自动改。其他线路快出30%
        以上才建议更换，避免单次测速的波动导致来回改配置。"""
        if not routes:
            return "error", "订阅源列表中的地址都没有拿到样本直链，无法测试播放线路"
        reachable = [r for r in routes if r.get("error") is None and r.get("speed_kbps")]
        if not reachable:
            return "error", "所有播放线路均不可达，请检查网络"
        best = max(reachable, key=lambda r: r["speed_kbps"])
        current = next((r for r in routes if r.get("current")), None)

        def advice(route: Dict[str, Any]) -> str:
            if route["prefix"]:
                return f"可将加速源改为 {route['prefix']}"
            steps = []
            if has_accelerator:
                steps.append("清空加速源")
            if not source_is_official:
                steps.append("将订阅源改为 ANi 官方源")
            return "可" + "，并".join(steps) + "，直接使用官方直链"

        if not current:
            return "warning", f"当前订阅源未取到样本，无法判断当前线路；最快的是{best['label']}"
        if current.get("error") is not None or not current.get("speed_kbps"):
            return "error", f"当前线路不可达，{advice(best)}"
        if best is not current and best["speed_kbps"] >= current["speed_kbps"] * 1.3:
            ratio = best["speed_kbps"] / current["speed_kbps"]
            return "warning", f"{best['label']}比当前线路快{ratio:.1f}倍，{advice(best)}"
        if current["speed_kbps"] < PLAYBACK_BITRATE_KBPS:
            return "warning", "当前线路已是最快，但速度低于1080P码率，播放可能卡顿"
        return "success", "当前线路速度正常，保持现有配置即可"

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
    def __row(cells: List[Tuple[int, dict]]) -> dict:
        """一行表单控件：cells是(占几列, 控件)的列表，md以上按给定列宽并排，
        窄屏自动堆叠成一列。配置页的每一行都走这个函数，保证列宽/间距一致，
        不会出现一个开关独占一整行、右边大片空白这种排版。"""
        return {
            "component": "VRow",
            "content": [
                {"component": "VCol", "props": {"cols": 12, "md": width}, "content": [component]}
                for width, component in cells
            ],
        }

    @staticmethod
    def __config_card(title: str, rows: List[dict]) -> dict:
        """配置分区卡片：统一用描边卡而不是填色卡，避免几种背景色堆在一起
        显得吵；需要提示"这是会动文件的操作"时才另外用带色卡。"""
        return {
            "component": "VCard",
            "props": {"variant": "outlined", "class": "mb-4"},
            "content": [
                {
                    "component": "VCardTitle",
                    "props": {"class": "text-subtitle-1 font-weight-bold"},
                    "text": title,
                },
                {"component": "VCardText", "content": rows},
            ],
        }

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    self.__config_card(
                        "基本设置",
                        [
                            self.__row(
                                [
                                    (4, {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}),
                                    (4, {"component": "VSwitch", "props": {"model": "onlyonce", "label": "立即运行一次"}}),
                                    (4, {"component": "VSwitch", "props": {"model": "use_proxy", "label": "订阅源走代理"}}),
                                ]
                            ),
                            self.__row(
                                [
                                    (
                                        4,
                                        {
                                            "component": "VTextField",
                                            "props": {
                                                "model": "cron",
                                                "label": "执行周期",
                                                "placeholder": "20 22,23,0,1 * * *",
                                            },
                                        },
                                    ),
                                    (
                                        4,
                                        {
                                            "component": "VTextField",
                                            "props": {
                                                "model": "storageplace",
                                                "label": "Strm存储地址",
                                                "placeholder": "/downloads/strm",
                                            },
                                        },
                                    ),
                                    (
                                        4,
                                        {
                                            "component": "VSelect",
                                            "props": {
                                                "model": "strm_layout",
                                                "label": "strm存放方式",
                                                "items": [
                                                    {"title": "平铺存放", "value": LAYOUT_FLAT},
                                                    {"title": "按剧集分目录", "value": LAYOUT_BY_TITLE},
                                                ],
                                                "hint": "改后运行下方「重建目录结构」",
                                                "persistent-hint": True,
                                            },
                                        },
                                    ),
                                ]
                            ),
                        ],
                    ),
                    self.__config_card(
                        "订阅与加速",
                        [
                            self.__row(
                                [
                                    (
                                        6,
                                        {
                                            "component": "VCombobox",
                                            "props": {
                                                "model": "subscription_source",
                                                "label": "当前订阅源",
                                                "items": self.__subscription_options(),
                                                "placeholder": "选择或输入 RSS 地址",
                                                "hint": "从下方列表选择，也可直接输入地址",
                                                "persistent-hint": True,
                                            },
                                        },
                                    ),
                                    (
                                        6,
                                        {
                                            "component": "VCombobox",
                                            "props": {
                                                "model": "accelerator_prefix",
                                                "label": "当前加速源",
                                                "items": self.__accelerator_options(),
                                                "clearable": True,
                                                "placeholder": "留空不加速",
                                                "hint": "留空=使用订阅源自带线路；填写后改用该加速源",
                                                "persistent-hint": True,
                                            },
                                        },
                                    ),
                                ]
                            ),
                            self.__row(
                                [
                                    (
                                        6,
                                        {
                                            "component": "VTextarea",
                                            "props": {
                                                "model": "subscription_list",
                                                "label": "订阅源列表",
                                                "rows": 3,
                                                "auto-grow": True,
                                                "placeholder": "一行一个 RSS 地址",
                                                "hint": "一行一个，# 开头为注释；可自由增删改",
                                                "persistent-hint": True,
                                            },
                                        },
                                    ),
                                    (
                                        6,
                                        {
                                            "component": "VTextarea",
                                            "props": {
                                                "model": "accelerator_list",
                                                "label": "加速源列表",
                                                "rows": 3,
                                                "auto-grow": True,
                                                "placeholder": "一行一个反代前缀，如 https://pro.pili.cc.cd",
                                                "hint": "一行一个，# 开头为注释；可自由增删改",
                                                "persistent-hint": True,
                                            },
                                        },
                                    ),
                                ]
                            ),
                            self.__row(
                                [
                                    (
                                        6,
                                        {
                                            "component": "VSelect",
                                            "props": {
                                                "model": "season_filter",
                                                "label": "拉取季度筛选",
                                                "items": self.__build_season_options(),
                                                "multiple": True,
                                                "chips": True,
                                                "clearable": True,
                                            },
                                        },
                                    ),
                                    (
                                        6,
                                        {
                                            "component": "div",
                                            "props": {"class": "text-caption", "style": "white-space: pre-line;"},
                                            "text": "首次安装已填入内置地址：pili、op5 镜像（已加速）与 ANi 官方（未加速），"
                                            "失效时直接在列表中删除或替换\n"
                                            "列表改动保存后，下拉框即出现新地址；「连通性检测」会逐个实测列表中的地址",
                                        },
                                    ),
                                ]
                            ),
                        ],
                    ),
                    {
                        "component": "VCard",
                        "props": {"variant": "tonal", "color": "warning", "class": "mb-4"},
                        "content": [
                            {
                                "component": "VCardTitle",
                                "props": {"class": "text-subtitle-1 font-weight-bold"},
                                "text": "维护操作",
                            },
                            {
                                "component": "VCardText",
                                "content": [
                                    self.__row(
                                        [
                                            (3, {"component": "VSwitch", "props": {"model": model, "label": label}})
                                            for model, label in (
                                                ("refresh_subscription_once", "重建直链"),
                                                ("regroup_once", "重建目录结构"),
                                                ("backfill_once", "补全历史剧集"),
                                                ("detect_once", "连通性检测"),
                                            )
                                        ]
                                    ),
                                    {
                                        "component": "div",
                                        "props": {"class": "text-caption", "style": "white-space: pre-line;"},
                                        "text": "手动触发，多选时按顺序依次执行，运行状态见详情页\n"
                                        "重建直链：按当前订阅源与加速源重写全部 strm 链接，实测可达才覆盖\n"
                                        "重建目录结构：按「strm 存放方式」移动文件，不改内容\n"
                                        "补全历史剧集：回溯 RSS 窗口之外的早期集数，串行限流探测\n"
                                        "连通性检测：检查订阅源，并实测各播放线路的首包耗时与下载速度",
                                    },
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "density": "compact",
                            "style": "white-space: pre-line;",
                            "text": "生成的 strm 建议配合「目录监控」转移到媒体库目录，由 MoviePilot 刮削\n"
                            "存放方式选「按剧集分目录」时每部剧一个文件夹，Emby/Jellyfin 刮削更干净\n"
                            "Emby 容器需额外设置小写 http_proxy 环境变量，否则无法提取媒体信息\n"
                            "社区加速源不稳定时，可用 GOST/Nginx 自建反代填入加速源\n"
                            "详细说明：https://github.com/oiloveio/MoviePilot-Plugins",
                        },
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
            "subscription_list": "\n".join(url for url, _ in BUILTIN_SUBSCRIPTIONS),
            "accelerator_list": "\n".join(prefix for prefix, _ in BUILTIN_ACCELERATORS),
            "refresh_subscription_once": False,
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
                "subscription_list": self._subscription_list,
                "accelerator_list": self._accelerator_list,
                "refresh_subscription_once": self._refresh_subscription_once,
                "regroup_once": self._regroup_once,
                "backfill_once": self._backfill_once,
                "detect_once": self._detect_once,
            }
        )

    TASK_LABELS = {
        "task": "订阅同步",
        "refresh_subscription": "重建直链",
        "regroup": "重建目录结构",
        "backfill": "补全历史剧集",
        "detect": "连通性检测",
    }

    @staticmethod
    def __chip(text: str, color: str) -> dict:
        return {"component": "VChip", "props": {"color": color, "size": "small", "class": "ma-1"}, "text": text}

    @staticmethod
    def __format_speed(kbps: float) -> str:
        return f"{kbps / 1024:.1f} MB/s" if kbps >= 1024 else f"{kbps:.0f} KB/s"

    @staticmethod
    def __speed_color(kbps: float) -> str:
        if kbps >= PLAYBACK_BITRATE_KBPS * 2:
            return "success"
        if kbps >= PLAYBACK_BITRATE_KBPS:
            return "warning"
        return "error"

    def __subscription_card(self, detect_result: Dict[str, Any]) -> dict:
        rows = []
        for row in detect_result.get("subscriptions", []):
            if row.get("ok") and row.get("entries"):
                chips = [
                    self.__chip("✅ 可用", "success"),
                    self.__chip(f"{row['entries']} 条", "default"),
                    self.__chip(f"{row['elapsed_ms']:.0f} ms", "default"),
                ]
            elif row.get("ok"):
                chips = [self.__chip("⚠️ RSS无条目", "warning")]
            else:
                chips = [self.__chip(f"❌ {row.get('error') or '抓取失败'}", "error")]
            if row.get("current"):
                chips.append(self.__chip("当前使用", "primary"))
            rows.append(
                self.__row(
                    [
                        (5, {"component": "span", "props": {"class": "text-body-2"}, "text": f"{row.get('label', '')}　{row.get('url', '')}"}),
                        (7, {"component": "div", "content": chips}),
                    ]
                )
            )
        return {
            "component": "VCard",
            "props": {"class": "mb-4"},
            "content": [
                {"component": "VCardTitle", "text": "订阅源"},
                {
                    "component": "VCardSubtitle",
                    "props": {"style": "white-space: normal;"},
                    "text": "只检测订阅列表(RSS)能否获取，决定能否发现新番，与播放速度无关；内置订阅源一并列出供参考",
                },
                {"component": "VCardText", "content": rows or [{"component": "span", "text": "无数据"}]},
            ],
        }

    def __route_card(self, detect_result: Dict[str, Any]) -> dict:
        routes = detect_result.get("routes", [])
        reachable = [r for r in routes if r.get("error") is None and r.get("speed_kbps")]
        best = max(reachable, key=lambda r: r["speed_kbps"]) if reachable else None

        rows = []
        for route in routes:
            chips = []
            if route.get("error") is None and route.get("speed_kbps"):
                chips.append(self.__chip(f"首包 {route['first_byte_ms']:.0f} ms", "default"))
                chips.append(self.__chip(self.__format_speed(route["speed_kbps"]), self.__speed_color(route["speed_kbps"])))
            else:
                chips.append(self.__chip(f"❌ {route.get('error') or '不可达'}", "error"))
            if route.get("current"):
                chips.append(self.__chip("当前使用", "primary"))
            if best is route and len(reachable) > 1:
                chips.append(self.__chip("最快", "success"))
            rows.append(
                self.__row(
                    [
                        (5, {"component": "span", "props": {"class": "text-body-2"}, "text": f"{route.get('label', '')}　{route.get('display') or route.get('prefix') or ''}"}),
                        (7, {"component": "div", "content": chips}),
                    ]
                )
            )

        body: List[dict] = rows or [{"component": "span", "text": "没有拿到样本直链，未测试播放线路"}]
        if detect_result.get("verdict"):
            body.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": detect_result.get("verdict_level") or "info",
                        "variant": "tonal",
                        "density": "compact",
                        "class": "mt-3",
                        "text": detect_result["verdict"],
                    },
                }
            )
        sample_title = detect_result.get("sample_title") or "无"
        return {
            "component": "VCard",
            "props": {"class": "mb-4"},
            "content": [
                {"component": "VCardTitle", "text": f"播放线路测速（{detect_result.get('checked_at', '未知时间')}）"},
                {
                    "component": "VCardSubtitle",
                    "props": {"style": "white-space: normal;"},
                    "text": f"样本：{sample_title}。从 MoviePilot 主机直接访问视频直链(不经过 MP 代理)，内置加速节点一并列出供参考，"
                    f"下载开头 {SPEED_TEST_BYTES // 1024 // 1024}MB；1080P 流畅播放约需 "
                    f"{PLAYBACK_BITRATE_KBPS}KB/s，单次测速存在波动，仅供参考",
                },
                {"component": "VCardText", "content": body},
            ],
        }

    def __distribution_card(self, local_distribution: Dict[str, Any]) -> dict:
        by_category = local_distribution.get("by_category", {})
        total = local_distribution.get("total", 0)
        rows = []
        for category, count in sorted(by_category.items(), key=lambda kv: kv[1], reverse=True):
            percent = f"{count / total * 100:.1f}%" if total else "0%"
            rows.append(
                self.__row(
                    [
                        (6, {"component": "span", "props": {"class": "text-body-2"}, "text": category}),
                        (3, {"component": "span", "text": f"{count} 个"}),
                        (3, {"component": "span", "text": percent}),
                    ]
                )
            )
        body: List[dict] = rows or [{"component": "span", "text": "存储目录下没有strm文件"}]
        mismatched = local_distribution.get("mismatched", 0)
        if mismatched:
            body.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "warning",
                        "variant": "tonal",
                        "density": "compact",
                        "class": "mt-3",
                        "text": f"{mismatched} 个strm的线路与当前加速源配置不一致，运行「重建直链」可统一",
                    },
                }
            )
        return {
            "component": "VCard",
            "content": [
                {
                    "component": "VCardTitle",
                    "text": f"本地strm线路分布（共{total}个，{local_distribution.get('checked_at', '未知时间')}）",
                },
                {"component": "VCardText", "content": body},
            ],
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

        detect_result = self.get_data("detect_result") or {}
        local_distribution = self.get_data("local_distribution") or {}

        if not detect_result and not local_distribution:
            content.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "text": "还没有检测数据。在配置页勾选「连通性检测」运行一次，这里会显示订阅源状态、"
                        "各播放线路的首包耗时与下载速度，以及本地strm的线路分布。",
                    },
                }
            )
            return content

        if detect_result:
            content.append(self.__subscription_card(detect_result))
            content.append(self.__route_card(detect_result))

        if local_distribution:
            content.append(self.__distribution_card(local_distribution))

        return content

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    # 注意：shutdown()默认wait=True，会阻塞等正在跑的job(比如探测/维护
                    # 这类耗时几分钟的一次性任务)跑完才返回。保存配置会先走到这里，
                    # 如果上一次任务还没跑完，保存动作会被卡住——实测踩过的真bug，
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

    def build_direct_request_utils(self) -> RequestUtils:
        """视频直链探测专用，不走MoviePilot代理：strm最终由Emby/Jellyfin/播放器
        直接访问，它们不经过MP的代理。探测必须站在同样的网络视角，否则会出现
        "插件测得通、播放器播不了"的假阳性。"""
        return RequestUtils(ua=settings.USER_AGENT if settings.USER_AGENT else None)

    def fetch_one_source(self, url: str) -> List[Dict[str, str]]:
        """抓取单个数据源，不吞异常，抓取失败会直接抛出，由调用方决定要不要
        区分"RSS连不上"和"RSS通了但没内容"两种情况"""
        return self._fetch_one(url)

    def _fetch_one(self, url: str) -> List[Dict[str, str]]:
        def operation():
            response = self.build_request_utils().get_res(url)
            # 必须用 is None 判断：requests.Response 的布尔值等于 response.ok，
            # 4xx/5xx 响应本身就是 False，用 not response 会把"HTTP 403"误报成"无响应"
            if response is None or response.status_code != 200:
                status = response.status_code if response is not None else "无响应"
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
        """从ANi的完整标题/文件名里切出剧名，"按剧集分目录"存放和"重建目录
        结构"共用这一个函数——解析规则只有这一份，以后规则要改只改
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

    链接模型：ANi 全部直链都是 {线路前缀}/{季度}/{文件名}?d=mp4，其中
    "季度/文件名?d=mp4"这一段(资源路径)在所有镜像、所有加速节点之间完全
    一致。订阅镜像下发的直链往往已经自带一层加速(如 pili 镜像下发的是
    https://pro.pili.cc.cd/resources.ani.rip/...)，所以生成strm时不能在
    RSS原链接上直接叠加速源，否则会叠出两层壳。相关工具：
    1. to_official_link：用资源路径重建官方直链，剥掉所有加速层；
    2. compose_link：生成/改写strm的唯一入口。加速源留空时原样使用订阅源的
       线路，填了加速源时先还原官方直链再套加速源；
    3. build_proxied_url：前缀拼接公式，新地址 = 加速源 + / + 原host + path + query；
    4. describe_route：compose_link的逆向归类，用于本地strm分布统计；
    5. build_season_variant_link/build_episode_variant_link：补全历史剧集的
       候选构造，分别替换季度目录和集数数字，其余部分原样保留。
    """

    SEASON_PATH_RE = re.compile(r"(\d{4}-\d{1,2}/.+)$")

    def __init__(self, request_factory):
        self._request_factory = request_factory

    @classmethod
    def extract_resource_path(cls, url: str) -> Optional[str]:
        match = cls.SEASON_PATH_RE.search(url)
        return match.group(1) if match else None

    @classmethod
    def to_official_link(cls, link: str) -> str:
        """剥掉链接上的所有加速层，用资源路径重建官方直链；识别不出资源路径
        (非ANi格式)时原样返回，不做猜测。"""
        resource_path = cls.extract_resource_path(link)
        if not resource_path:
            return link
        return f"{OFFICIAL_BASE_URL}/{resource_path}"

    @classmethod
    def compose_link(cls, link: str, accelerator: Optional[str]) -> str:
        """生成/改写strm内容的唯一入口。
        - 加速源留空：原样返回，线路就是订阅源给出的线路(镜像源自带加速，
          官方源是官方直链)；
        - 填了加速源：先剥掉链接上已有的所有加速层还原官方直链，再套这个加速源。
          镜像下发的链接本身带一层壳，直接叠会变成两层。"""
        accelerator = (accelerator or "").strip()
        if not accelerator:
            return link
        return cls.build_proxied_url(cls.to_official_link(link), accelerator)

    @classmethod
    def derive_prefix(cls, url: str) -> Optional[str]:
        """订阅源的线路前缀，如 https://pro.pili.cc.cd/resources.ani.rip/ ；
        重建直链时把本地文件的资源路径接到这个前缀上，改成当前订阅源的线路。"""
        resource_path = cls.extract_resource_path(url)
        if not resource_path:
            return None
        return url[: url.index(resource_path)]

    @classmethod
    def route_hops(cls, link: str) -> Optional[List[str]]:
        """把链接的线路前缀拆成逐跳主机列表，如
        https://pro.pili.cc.cd/resources.ani.rip/2026-7/x -> [pro.pili.cc.cd, resources.ani.rip]"""
        resource_path = cls.extract_resource_path(link)
        if not resource_path:
            return None
        parsed = urlparse(link[: link.index(resource_path)])
        if not parsed.netloc:
            return None
        return [parsed.netloc] + [seg for seg in parsed.path.split("/") if seg]

    @classmethod
    def embedded_accelerator(cls, link: str) -> Optional[str]:
        """订阅镜像下发链接自带的那一层加速前缀(如 https://pro.pili.cc.cd)，
        作为测速的候选线路；官方直链或结构不是"单层加速+官方域名"时返回None。"""
        hops = cls.route_hops(link)
        if not hops or len(hops) != 2 or hops[-1] != OFFICIAL_HOST:
            return None
        return f"{urlparse(link).scheme or 'https'}://{hops[0]}"

    @classmethod
    def describe_route(cls, link: str, accelerator: Optional[str] = None) -> str:
        """compose_link的逆向归类：这条strm实际走的是哪条线路。"""
        accelerator = (accelerator or "").strip().rstrip("/")
        if accelerator and link.startswith(accelerator + "/"):
            remainder = link[len(accelerator) + 1:]
            if cls.route_hops("https://" + remainder) == [OFFICIAL_HOST]:
                return f"加速源 {urlparse(accelerator).netloc or accelerator}（当前配置）"
        hops = cls.route_hops(link)
        if not hops:
            return "无法识别"
        if hops == [OFFICIAL_HOST]:
            return "官方直链"
        if hops[-1] != OFFICIAL_HOST:
            return f"其他来源 {hops[0]}"
        if len(hops) == 2:
            return f"加速源 {hops[0]}"
        return "多层套壳 " + " → ".join(hops)

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
    def build_title_variant(original_title: str, new_episode: int) -> Optional[str]:
        """把标题/文件名里的集数换成new_episode，其余原样保留"""
        match = EPISODE_NUM_RE.search(original_title)
        if not match:
            return None
        return f"{original_title[:match.start(2)]}{new_episode}{original_title[match.end(2):]}"

    @staticmethod
    def build_episode_variant_link(original_link: str, new_episode: int) -> Optional[str]:
        """补全历史剧集用：把直链里的集数数字换成new_episode，域名/季度目录/文件名
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
        """补全历史剧集用：把直链里的季度目录换成new_season，其余部分原样保留。
        季度目录是纯ASCII(yyyy-m格式)，不涉及URL编码，直接在原始字符串上
        替换即可，不需要像集数那样先unquote再quote。"""
        match = SEASON_RE.search(link)
        if not match:
            return None
        return f"{link[:match.start(1)]}{new_season}{link[match.end(1):]}"

    @staticmethod
    def season_distance(season_a: str, season_b: str) -> int:
        """两个yyyy-m季度目录之间相差多少个月，补全历史剧集按这个距离由近到远
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
        内容校验失败/异常信息，不能只留一句"不可达"就没了。

        只下载64字节，适合"这条链接存不存在"这类批量校验；衡量线路快慢用
        measure_playback。"""
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
            # 必须用 is None：requests.Response 的布尔值等于 response.ok，4xx/5xx
            # 响应本身就是 False，用 not response 会把"HTTP 403"误报成"无响应"
            if response is None:
                return None, "无响应(连接失败或超时)"
            if response.status_code not in (200, 206):
                return None, f"HTTP {response.status_code}"
            if not self._looks_like_video(response):
                return None, "响应内容不是视频数据(HTTP状态码正常但可能是错误页)"
            return round(elapsed_ms, 1), None
        except Exception as err:
            return None, f"异常:{err}"

    @staticmethod
    def _content_type(response: Any) -> str:
        try:
            return str(response.headers.get("Content-Type", "") or "").split(";")[0].strip().lower()
        except Exception:
            return ""

    @classmethod
    def _looks_like_video(cls, response: Any) -> bool:
        return cls._looks_like_video_bytes(cls._content_type(response), response.content or b"")

    @staticmethod
    def _looks_like_video_bytes(content_type: str, content: bytes) -> bool:
        """两层校验：1. Content-Type声明的类型不是网页/JSON这类格式；
        2. 内容开头不是html/xml/json这类错误页标记。ANi直链固定是mp4容器
        (偏移4字节处是'ftyp')，但不强制匹配魔数，没有错误页特征就放行，
        避免对没见过的容器类型误杀。"""
        if content_type in NON_VIDEO_CONTENT_TYPES:
            return False
        if not content:
            return False
        stripped = content.lstrip()[:20].lower()
        if any(stripped.startswith(marker) for marker in HTML_LIKE_PREFIXES):
            return False
        return True

    def measure_playback(self, url: str) -> Dict[str, Any]:
        """模拟播放器起播：流式下载视频开头一段，得到两个指标——
        首包耗时(从发起请求到收到第一块视频数据，含跳转，决定起播等待)和
        持续下载速度(首包之后的吞吐，决定播放会不会卡)。64字节的连通性探测
        只能回答"通不通"，回答不了"快不快"，测线路必须用这个。

        下载量和时长双重封顶(SPEED_TEST_BYTES / SPEED_TEST_MAX_SECONDS)，
        读够就主动断开，不会下载整集。返回
        {"first_byte_ms": float|None, "speed_kbps": float|None, "error": str|None}。"""
        result: Dict[str, Any] = {"first_byte_ms": None, "speed_kbps": None, "error": None}
        response = None
        try:
            request_utils = self._request_factory()
            request_utils.update_headers({"Range": f"bytes=0-{SPEED_TEST_BYTES - 1}"})
            start = time.monotonic()
            response = request_utils.get_res(url, stream=True)
            if response is None:
                result["error"] = "无响应(连接失败或超时)"
                return result
            if response.status_code not in (200, 206):
                result["error"] = f"HTTP {response.status_code}"
                return result

            received = 0
            first_chunk_bytes = 0
            first_at: Optional[float] = None
            for chunk in response.iter_content(chunk_size=SPEED_TEST_CHUNK_BYTES):
                if not chunk:
                    continue
                now = time.monotonic()
                if first_at is None:
                    first_at = now
                    first_chunk_bytes = len(chunk)
                    if not self._looks_like_video_bytes(self._content_type(response), chunk[:PROBE_RANGE_BYTES]):
                        result["error"] = "响应内容不是视频数据(HTTP状态码正常但可能是错误页)"
                        return result
                received += len(chunk)
                if received >= SPEED_TEST_BYTES or now - start >= SPEED_TEST_MAX_SECONDS:
                    break
            end = time.monotonic()

            if first_at is None:
                result["error"] = "响应体为空"
                return result
            result["first_byte_ms"] = round((first_at - start) * 1000, 1)
            transfer_bytes = received - first_chunk_bytes
            transfer_sec = end - first_at
            if transfer_bytes > 0 and transfer_sec > 0:
                speed = transfer_bytes / 1024 / transfer_sec
            else:
                speed = received / 1024 / max(end - start, 1e-3)
            result["speed_kbps"] = round(speed, 1)
            return result
        except Exception as err:
            result["error"] = f"异常:{err}"
            return result
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

    def _verify_reachable(self, url: str) -> bool:
        latency_ms, _ = self.probe_latency_ms(url)
        return latency_ms is not None

    @staticmethod
    def scan_local_distribution(
        storage_path: str, accelerator: Optional[str], expected_route: Optional[str] = None
    ) -> Dict[str, Any]:
        """扫描本地strm，按实际线路归类(官方直链/加速源X/多层套壳/其他来源)。
        给出expected_route(按当前「订阅源+加速源」新生成的strm应属的类别)时，
        统计有多少个跟它不一致——不一致的可以用「重建直链」一次性统一。"""
        directory = Path(storage_path) if storage_path else None
        if not directory or not directory.exists():
            return {"total": 0, "by_category": {}, "mismatched": 0}

        by_category: Dict[str, int] = {}
        total = 0
        mismatched = 0
        for strm_file in directory.rglob("*.strm"):
            try:
                content = strm_file.read_text(encoding="utf-8").strip()
            except Exception:
                continue
            total += 1
            category = StrmRelinkService.describe_route(content, accelerator)
            by_category[category] = by_category.get(category, 0) + 1
            if expected_route and category != expected_route:
                mismatched += 1

        return {"total": total, "by_category": by_category, "mismatched": mismatched}
