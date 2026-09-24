import ipaddress
import json
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote, unquote, urlparse, urlunparse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from fastapi import Body, Request
from fastapi.responses import Response, StreamingResponse
from requests import Session
from requests.adapters import HTTPAdapter

from app.plugins import _PluginBase
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.network import RequestUtils

# 内置订阅源：只作为首次安装时「订阅源列表」的初始内容。列表完全由用户维护，
# 可增删——内置地址日后失效时用户直接删掉、换成新地址即可，不依赖插件更新。
# 用哪个由用户选，插件不在后台自动切换。订阅源分两类：社区镜像下发的视频直链
# 已经自带镜像方的加速；ANi 官方下发的是未加速的官方直链，需要自己配置加速源。
BUILTIN_SUBSCRIPTIONS: Tuple[Tuple[str, str], ...] = (
    ("https://api.ani.rip/ani-download.xml", "ANi 官方"),
    ("https://api.pili.cc.cd/ani-download.xml", "pili 镜像"),
    ("https://aniapi.op5.de5.net/ani-download.xml", "op5 镜像"),
)
# 默认给新用户已加速的镜像，开箱即用，不需要再配置加速源
DEFAULT_SUBSCRIPTION_SOURCE = "https://api.pili.cc.cd/ani-download.xml"
# 内置加速源：同样只作为「加速源列表」的初始内容。这两个就是上面两个镜像
# 自带加速所用的反代节点，也可以单独套在官方订阅源的视频直链上使用。
BUILTIN_ACCELERATORS: Tuple[Tuple[str, str], ...] = (
    ("https://pro.pili.cc.cd", "pili 节点"),
    ("https://pro.op5.de5.net", "op5 节点"),
)
# 内置地址在配置页上显示的注释标签
BUILTIN_TAGS: Dict[str, Tuple[str, ...]] = {
    "https://api.ani.rip/ani-download.xml": ("视频链接未加速",),
    "https://api.pili.cc.cd/ani-download.xml": ("视频链接已加速",),
    "https://aniapi.op5.de5.net/ani-download.xml": ("视频链接已加速",),
}
# 本地中转：插件在 MoviePilot 内提供的视频转发接口。媒体服务器或播放设备本身
# 不走代理、访问不了需要翻墙的视频链接时，strm 改为指向这个局域网地址，由
# MoviePilot 经用户的代理拉取视频再原样转发。路径固定为 MoviePilot 插件接口的
# 注册规则：/api/v1/plugin/{插件ID}{get_api 的 path}
RELAY_API_PATH = "/api/v1/plugin/ANiStrmHub/relay"
RELAY_CHUNK_BYTES = 256 * 1024
RELAY_TIMEOUT_SECONDS = 30
# 经代理每新建一条加密连接的开销可能达到数秒(实测某局域网代理约 5.6 秒)，而播放
# 一个视频要先连 resources.ani.rip 再被跳转到 workers.dev，首包因此翻倍到 12 秒以上，
# 每次拖动进度都要再等一遍。两项优化：记住跳转后的最终地址，之后直接访问，省掉一跳；
# 中转内部用长连接会话，拖动与连续读取复用已建立的连接(实测首包降到约 0.2 秒)。
RELAY_REDIRECT_CACHE_SECONDS = 6 * 3600
# 探测本地中转时的等待时间：中转冷启动要经代理新建两条连接，实测某局域网代理可达
# 18 秒，默认的 20 秒很容易误判为不可达，导致订阅同步跳过本轮
RELAY_PROBE_TIMEOUT_SECONDS = 60
RELAY_REDIRECT_CACHE_LIMIT = 1000
# 播放器拖动进度靠 Range 请求，必须原样转发；响应头只透传与视频传输相关的几项
RELAY_REQUEST_HEADERS = ("If-Range",)
# 中转密钥：URL 安全字符。配置页「重置密钥」在浏览器里直接生成，保存时由后端校验
RELAY_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{12,64}$")
RELAY_TOKEN_JS = (
    "function() { const c = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'; "
    "const b = new Uint8Array(16); crypto.getRandomValues(b); "
    "relay_token = Array.from(b, x => c[x % c.length]).join(''); }"
)
CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+)")
RELAY_RESPONSE_HEADERS = (
    "Content-Type",
    "Content-Length",
    "Content-Range",
    "Accept-Ranges",
    "Last-Modified",
    "ETag",
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
    plugin_desc = "开箱即用的ANi新番strm生成：内置已加速订阅源，也可选官方源自配加速或本地中转；mp刮削入库，媒体服务器直连播放"
    plugin_icon = "https://raw.githubusercontent.com/oiloveio/MoviePilot-Plugins/main/icons/anistrmhub.png"
    plugin_version = "0.13.1"
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
    _subscription_list: List[str] = [url for url, _ in BUILTIN_SUBSCRIPTIONS]
    _accelerator_list: List[str] = [prefix for prefix, _ in BUILTIN_ACCELERATORS]
    _relay_enabled = False
    _relay_address = ""
    _relay_proxy = ""
    _relay_token = ""

    _scheduler: Optional[BackgroundScheduler] = None

    def __init__(self):
        super().__init__()
        self._relay_session: Optional[Session] = None
        # 维护任务(重建直链/重建目录结构/补全历史剧集/连通性检测)同一时间只运行一个：
        # 它们扫描并改写同一批 strm，并发会互相看到对方写到一半的中间状态
        self._maintenance_lock = threading.Lock()
        self._relay_redirects: Dict[str, Tuple[str, float]] = {}
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

        # 首次安装(配置里还没有这个键)用默认订阅源；用户清空保存时按列表第一条，
        # 见__active_subscription。清空时前端存的是null，不能和"键不存在"混为一谈
        if "subscription_source" in config:
            self._subscription_source = (config.get("subscription_source") or "").strip()
        else:
            self._subscription_source = DEFAULT_SUBSCRIPTION_SOURCE
        self._accelerator_prefix = (config.get("accelerator_prefix") or "").strip().rstrip("/")
        # 列表同理：只有键不存在(首次安装/从旧版本升级)才填入内置地址；用户删光后
        # 保存的是空列表，尊重用户的选择，不再自动补回
        if "subscription_list" in config:
            self._subscription_list = self.parse_url_lines(config.get("subscription_list"))
        else:
            self._subscription_list = [url for url, _ in BUILTIN_SUBSCRIPTIONS]
        if "accelerator_list" in config:
            self._accelerator_list = self.parse_url_lines(config.get("accelerator_list"), strip_slash=True)
        else:
            self._accelerator_list = [prefix for prefix, _ in BUILTIN_ACCELERATORS]
        self._relay_enabled = bool(config.get("relay_enabled", False))
        self._relay_address = (config.get("relay_address") or "").strip().rstrip("/")
        self._relay_proxy = (config.get("relay_proxy") or "").strip()
        # 中转密钥：写进 strm 链接，没有正确密钥的请求一律拒绝。清空后保存即重新生成
        self._relay_token = (config.get("relay_token") or "").strip()
        token_generated = not RELAY_TOKEN_RE.match(self._relay_token)
        if token_generated:
            self._relay_token = secrets.token_urlsafe(12)

        # 在下拉框里直接输入的新地址，保存时并入列表，之后一直保留，直到用户删除
        lists_changed = token_generated or "subscription_list" not in config or "accelerator_list" not in config
        # 中转地址由插件维护：启用后作为一条加速源加入列表；中转地址或密钥变化后，
        # 列表里旧的中转地址被替换，正在使用旧地址时自动切到新地址(否则新生成的
        # strm 会指向已失效的密钥)。已有 strm 需在详情页重建直链更新
        relay_prefix = self.relay_prefix()
        if relay_prefix:
            kept = [url for url in self._accelerator_list if RELAY_API_PATH not in url or url == relay_prefix]
            if relay_prefix not in kept:
                kept.append(relay_prefix)
            if kept != self._accelerator_list:
                self._accelerator_list = kept
                lists_changed = True
            if RELAY_API_PATH in self._accelerator_prefix and self._accelerator_prefix != relay_prefix:
                self._accelerator_prefix = relay_prefix
                lists_changed = True
        if self._subscription_source and self._subscription_source not in self._subscription_list:
            self._subscription_list.append(self._subscription_source)
            lists_changed = True
        if self._accelerator_prefix and self._accelerator_prefix not in self._accelerator_list:
            self._accelerator_list.append(self._accelerator_prefix)
            lists_changed = True


        self._client.set_use_proxy(self._use_proxy)
        logger.info(
            f"ANiStrmHub配置加载：enabled={self._enabled}, onlyonce={self._onlyonce}, "
            f"use_proxy={self._use_proxy}, storage={self._storageplace}, "
            f"订阅源={self._subscription_source}, 加速源={self._accelerator_prefix or '(不加速)'}"
        )

        if not (self._enabled or self._onlyonce):
            logger.info("ANiStrmHub未启用且未触发立即运行，跳过任务注册")
            if lists_changed:
                # 未启用时也要把并入的新地址写回配置，否则只存在于内存，下次保存就丢了
                self.__update_config()
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

        self.__update_config()

        if self._scheduler.get_jobs():
            self._scheduler.print_jobs()
            self._scheduler.start()

    # 详情页按钮可执行的操作：接口名 -> (任务状态键, 显示名, 执行函数名)
    ACTIONS = {
        "sync": ("task", "订阅同步", "_ANiStrmHub__task"),
        "rebuild": ("refresh_subscription", "重建直链", "_ANiStrmHub__refresh_subscription_task"),
        "regroup": ("regroup", "重建目录结构", "_ANiStrmHub__regroup_local_strm_task"),
        "backfill": ("backfill", "补全历史剧集", "_ANiStrmHub__backfill_task"),
        "detect": ("detect", "连通性检测", "_ANiStrmHub__detect_task"),
    }

    def run_action(self, name: str, payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
        """详情页按钮调用的操作接口(需 MoviePilot 登录)。任务在后台线程执行，接口立即
        返回，详情页刷新后在「任务运行状态」查看进度与结果。

        重建直链带参数 accelerator：先把它设为当前加速源再重建，保证已有 strm 与之后
        新生成的 strm 走同一条线路。空字符串表示不加速(使用订阅源原始链接)。"""
        if name not in self.ACTIONS:
            return {"success": False, "message": f"未知操作：{name}"}
        task_key, label, method = self.ACTIONS[name]
        if name == "rebuild":
            accelerator = str((payload or {}).get("accelerator") or "").strip().rstrip("/")
            allowed = set(self._accelerator_list) | {self.relay_prefix() or ""} | {""}
            if accelerator not in allowed:
                return {"success": False, "message": "目标线路不在加速源列表中，请刷新页面后重试"}
            self._accelerator_prefix = accelerator
            self.__update_config()
            logger.info(f"ANiStrmHub重建直链：目标线路设为 {accelerator or '不加速（订阅源原始链接）'}")
        started, message = self.start_maintenance(task_key, label, getattr(self, method))
        return {"success": started, "message": message}

    def start_maintenance(self, task_key: str, label: str, func) -> Tuple[bool, str]:
        """在后台线程执行一个维护任务；已有任务在运行时拒绝，不排队也不并发"""
        if not self._maintenance_lock.acquire(blocking=False):
            return False, "已有维护任务在运行，请等它完成后再试"
        self.__save_task_status(task_key, "running", "已启动")

        def worker():
            try:
                func()
            except Exception as err:
                logger.error(f"ANiStrmHub{label}：任务异常终止 - {err}")
                self.__save_task_status(task_key, "done", f"任务异常终止：{err}")
            finally:
                self._maintenance_lock.release()

        threading.Thread(target=worker, name=f"ANiStrmHub-{task_key}", daemon=True).start()
        return True, f"{label}已开始执行"

    @staticmethod
    def parse_url_lines(value: Any, strip_slash: bool = False) -> List[str]:
        """规整用户维护的地址列表：接受列表或多行文本，# 开头为注释，忽略空行和
        非http(s)开头的项，去重并保持用户的顺序。加速源是拼接前缀，strip_slash=True
        去掉末尾的/，避免拼出双斜杠。"""
        if isinstance(value, (list, tuple)):
            lines = [str(item) for item in value if item is not None]
        else:
            lines = str(value or "").splitlines()
        urls: List[str] = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if not line.lower().startswith(("http://", "https://")):
                logger.warning(f"ANiStrmHub：地址列表中忽略无效行（需以http://或https://开头）：{line}")
                continue
            urls.append(line.rstrip("/") if strip_slash else line)
        return list(dict.fromkeys(urls))

    @staticmethod
    def short_url(url: str) -> str:
        """界面上展示用的地址：本地中转只显示 协议+域名(接口路径与密钥对用户没有意义，
        而且会把一行撑到换行)，其余原样"""
        if RELAY_API_PATH in url:
            parsed = urlparse(url)
            return f"{parsed.scheme}://{parsed.netloc}"
        return url

    @staticmethod
    def __display_name(url: str, builtin: Tuple[Tuple[str, str], ...]) -> str:
        """内置地址显示内置名称，本地中转显示「本地中转」，自定义地址显示域名"""
        if RELAY_API_PATH in url:
            return f"本地中转 {urlparse(url).netloc}"
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
                f"下次定时运行会重试；可在详情页运行「连通性检测」对比线路后更换加速源"
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

        # 重建完刷新本地线路分布，详情页「维护操作」马上能看到结果
        expected_route = StrmRelinkService.describe_route(
            StrmRelinkService.compose_link(entries[0]["link"], accelerator), accelerator
        )
        distribution = StrmRelinkService.scan_local_distribution(self._storageplace, accelerator, expected_route)
        checked_at = datetime.now(tz=pytz.timezone(settings.TZ)).strftime("%Y-%m-%d %H:%M:%S")
        self.save_data("local_distribution", {"checked_at": checked_at, **distribution})

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
            # 本地中转显式列入：不依赖它是否已被写进加速源列表(用户反馈过检测结果里看不到中转)
            relay_prefix = self.relay_prefix()
            for prefix in dict.fromkeys(
                ([accelerator] if accelerator else []) + ([relay_prefix] if relay_prefix else []) + listed + extra_nodes
            ):
                if any(prefix == existing for _, existing in plan):
                    continue
                if RELAY_API_PATH in prefix:
                    label = "本地中转"
                elif prefix in listed or prefix == accelerator:
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
                        "display": self.short_url(prefix) if prefix else OFFICIAL_BASE_URL,
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
        direct_egress = self.__direct_egress()
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
                "direct_egress": direct_egress,
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

    def __direct_egress(self) -> Optional[Dict[str, str]]:
        """查询 MoviePilot 直连(不经任何代理)时的出口 IP 与所属国家。出口不在国内时，
        说明所在网络本身已被代理(如透明代理、路由器翻墙)，「官方直链」测得可达并不代表
        未翻墙的设备也能访问——用户实测时对此产生过疑问"""
        # 多个查询服务依次尝试：单个服务可能限流(实测 ipinfo.io 返回过 429)
        services = (
            ("https://api.ip.sb/geoip", "ip", "country_code", "organization"),
            ("https://ipinfo.io/json", "ip", "country", "org"),
            ("http://ip-api.com/json", "query", "countryCode", "isp"),
        )
        for url, ip_key, country_key, org_key in services:
            try:
                response = self._client.build_direct_request_utils(timeout=10).get_res(url)
                if response is None or response.status_code != 200:
                    continue
                data = response.json()
                if data.get(ip_key):
                    return {
                        "ip": str(data.get(ip_key) or ""),
                        "country": str(data.get(country_key) or "").upper(),
                        "org": str(data.get(org_key) or ""),
                    }
            except Exception as err:
                logger.debug(f"ANiStrmHub连通性检测：查询直连出口失败 {url} - {err}")
        return None

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
        """启用本地中转时注册视频转发接口。

        接口免登录(allow_anonymous)：strm 链接由媒体服务器、播放器在后台直接访问，
        无法交互登录，凭证只能写在链接里。写 MoviePilot 账号密码或 API 令牌等于把
        MoviePilot 的完整权限写进每个 strm 文件，所以改用插件专用的中转密钥：只能
        用来转发 ANi 官方视频，泄露后重置即作废。见 relay_video。"""
        apis: List[Dict[str, Any]] = [
            {
                "path": "/action/{name}",
                "endpoint": self.run_action,
                "methods": ["POST"],
                # 详情页按钮经 MoviePilot 前端调用，携带登录令牌；未登录无法调用
                "auth": "bear",
                "summary": "执行维护操作",
            }
        ]
        if not self._relay_enabled:
            return apis
        return apis + [
            {
                "path": "/relay/{path:path}",
                "endpoint": self.relay_video,
                "methods": ["GET", "HEAD"],
                "allow_anonymous": True,
                "summary": "本地中转：经代理转发 ANi 视频",
            }
        ]

    def relay_prefix(self) -> Optional[str]:
        """本地中转作为加速源时的前缀。一个 strm 只能写一个地址，所以由「strm 访问地址」
        决定链接形态：
        - 局域网地址：http://192.168.1.10:3000/api/v1/plugin/ANiStrmHub/relay
          不带任何凭证，只有局域网设备能用；
        - 公网地址：https://mp.example.com/api/v1/plugin/ANiStrmHub/relay/{密钥}
          自动带密钥，内外网都能直接播放。
        未启用或未填写 strm 访问地址时返回 None"""
        address = (self._relay_address or "").strip().rstrip("/")
        if not self._relay_enabled or not address.lower().startswith(("http://", "https://")):
            return None
        if self.relay_address_is_lan():
            return f"{address}{RELAY_API_PATH}"
        if not self._relay_token:
            return None
        return f"{address}{RELAY_API_PATH}/{self._relay_token}"

    def relay_address_is_lan(self) -> bool:
        return self.is_lan_host(urlparse(self._relay_address or "").hostname)

    @staticmethod
    def is_lan_address(host: Optional[str]) -> bool:
        """IP 地址是否属于局域网/本机"""
        try:
            address = ipaddress.ip_address((host or "").strip().strip("[]"))
        except ValueError:
            return False
        if getattr(address, "ipv4_mapped", None):
            address = address.ipv4_mapped
        return address.is_private or address.is_loopback or address.is_link_local

    @classmethod
    def is_lan_host(cls, host: Optional[str]) -> bool:
        """访问地址的主机部分是否是局域网地址：局域网 IP、localhost，或不带点的
        局域网主机名(如 nas)。域名一律视为公网访问地址。"""
        host = (host or "").strip().strip("[]").lower()
        if not host:
            return False
        if cls.is_lan_address(host):
            return True
        return host == "localhost" or "." not in host

    @classmethod
    def request_is_lan(cls, request: Request) -> Tuple[bool, str]:
        """判断请求是否来自局域网，返回(是否局域网, 判定依据)。

        只看来源 IP 不可靠：MoviePilot 前面有反向代理(公网域名访问)时，外网请求到达
        这里的来源地址是反向代理自己的局域网地址——0.11.0 就因此把外网请求当成局域网
        放行。所以同时满足两条才算局域网：
        1. 访问地址(Host)是局域网地址。经 Lucky 等反向代理访问时，Host 是公网域名
           (已实测：外网访问时 MoviePilot 看到的 Host 为 mp.example.com:8443)；
        2. 整条转发链(直接连接方、X-Forwarded-For、X-Real-IP)都是局域网地址。"""
        raw_host = request.headers.get("host") or ""
        host = urlparse(f"//{raw_host}").hostname or raw_host
        if not cls.is_lan_host(host):
            return False, f"访问地址 {raw_host or '未知'}"
        chain = [request.client.host if request.client else ""]
        chain += [hop.strip() for hop in (request.headers.get("X-Forwarded-For") or "").split(",")]
        chain.append(request.headers.get("X-Real-IP") or "")
        for hop in (hop for hop in chain if hop):
            if not cls.is_lan_address(hop):
                return False, f"来源 {hop}"
        return True, f"访问地址 {raw_host}"

    def __relay_proxies(self) -> Optional[Dict[str, str]]:
        """中转上游代理：填写了就用填写的，留空使用 MoviePilot 的代理设置"""
        custom = (self._relay_proxy or "").strip()
        if custom:
            return {"http": custom, "https": custom}
        return settings.PROXY or None

    @staticmethod
    def relay_target(raw_path: str, query: str) -> Optional[str]:
        """由中转请求的路径还原上游视频地址。只接受 resources.ani.rip 下
        "季度/文件名" 结构的路径，其余一律拒绝，保证接口不会被当成通用代理。
        raw_path 是未解码的原始路径段，直接拼回去，避免解码再编码改变文件名。"""
        remainder = (raw_path or "").lstrip("/")
        if not remainder.startswith(f"{OFFICIAL_HOST}/"):
            return None
        if ".." in remainder or not StrmRelinkService.extract_resource_path("/" + remainder):
            return None
        target = f"https://{remainder}"
        return f"{target}?{query}" if query else target

    def __relay_session_instance(self) -> Session:
        """中转专用的长连接会话：经代理建立的连接在请求之间复用"""
        if self._relay_session is None:
            session = Session()
            adapter = HTTPAdapter(pool_connections=4, pool_maxsize=16)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            self._relay_session = session
        return self._relay_session

    def __relay_get(self, url: str, headers: Dict[str, str]):
        request_utils = RequestUtils(
            ua=settings.USER_AGENT if settings.USER_AGENT else None,
            proxies=self.__relay_proxies(),
            session=self.__relay_session_instance(),
            timeout=RELAY_TIMEOUT_SECONDS,
        )
        request_utils.update_headers(headers)
        return request_utils.get_res(url, stream=True)

    def __fetch_upstream(self, target: str, headers: Dict[str, str]):
        """向上游请求视频。命中跳转缓存时直接访问最终地址；最终地址失效(报错或
        4xx/5xx)时丢弃缓存，回到原始地址重新跳转一次。"""
        cached = self._relay_redirects.get(target)
        if cached and time.time() - cached[1] < RELAY_REDIRECT_CACHE_SECONDS:
            response = self.__relay_get(cached[0], headers)
            # 416(请求范围超出文件大小)是最终地址给出的正常应答，不代表缓存失效；
            # 当成失效会绕回原始地址再跳转一次，白白多等一次建连
            if response is not None and (response.status_code < 400 or response.status_code == 416):
                return response
            if response is not None:
                response.close()
            self._relay_redirects.pop(target, None)

        response = self.__relay_get(target, headers)
        if response is not None and response.status_code < 400 and getattr(response, "history", None):
            final_url = getattr(response, "url", "") or ""
            if final_url.startswith("https://") and urlparse(final_url).netloc != OFFICIAL_HOST:
                if len(self._relay_redirects) >= RELAY_REDIRECT_CACHE_LIMIT:
                    self._relay_redirects.clear()
                self._relay_redirects[target] = (final_url, time.time())
        return response

    @staticmethod
    def __deny(message: str, status_code: int = 403) -> Response:
        # 明确声明 UTF-8：没有字符集声明时，部分浏览器和播放器会把中文提示显示成乱码
        return Response(status_code=status_code, content=message, media_type="text/plain; charset=utf-8")

    def relay_video(self, request: Request, path: str):
        """本地中转接口：媒体服务器请求局域网地址，MoviePilot 经代理向上游拉取
        视频并流式转发。Range 原样转发以支持拖动进度；上游的跳转(resources.ani.rip
        会跳到 workers.dev)在 MoviePilot 这一侧经代理完成，媒体服务器无感知。"""
        raw_path = request.scope.get("raw_path")
        raw_path = raw_path.decode("latin-1") if isinstance(raw_path, bytes) else ""
        marker = f"{RELAY_API_PATH}/"
        if marker in raw_path:
            raw_path = raw_path.split(marker, 1)[1]
        else:
            raw_path = quote(path, safe="/")

        # 访问规则：带正确密钥的请求任何来源都放行；不带密钥的请求只在确认来自局域网时
        # 放行，判定方式见 request_is_lan
        first_segment, _, rest = raw_path.partition("/")
        if self._relay_token and secrets.compare_digest(first_segment, self._relay_token):
            raw_path = rest
        elif first_segment != OFFICIAL_HOST and RELAY_TOKEN_RE.match(first_segment):
            logger.warning("ANiStrmHub本地中转：拒绝密钥已失效的请求")
            return self.__deny("拒绝访问：中转链接的密钥已失效（可能已重置密钥）。请在插件详情页点击「本地中转」重建直链，更新 strm")
        else:
            from_lan, basis = self.request_is_lan(request)
            if not from_lan:
                logger.warning(f"ANiStrmHub本地中转：拒绝外网的无密钥请求（{basis}）")
                if self.relay_address_is_lan():
                    return self.__deny(
                        "拒绝访问：该中转链接只能在局域网内使用。需要在外网播放，请把插件的「strm 访问地址」改为公网地址，"
                        "再在插件详情页点击「本地中转」重建直链"
                    )
                return self.__deny(
                    "拒绝访问：该中转链接缺少密钥，外网无法使用。请在插件详情页点击「本地中转」重建直链，strm 会换成带密钥的新链接"
                )

        target = self.relay_target(raw_path, request.url.query)
        if not target:
            return self.__deny("拒绝访问：本地中转只转发 ANi 官方视频地址")

        # 上游(Cloudflare 后的对象存储)对 Range 请求只返回 Content-Range，不带
        # Content-Length/Accept-Ranges，媒体服务器因此拿不到文件大小。所以上游一律按
        # Range 请求：客户端没带 Range 时取整个文件(bytes=0-)，HEAD 只取 1 字节，
        # 再由 Content-Range 算出长度与文件总大小回给客户端。
        client_range = request.headers.get("Range")
        is_head = request.method == "HEAD"
        forward = {name: request.headers[name] for name in RELAY_REQUEST_HEADERS if request.headers.get(name)}
        forward["Range"] = client_range or ("bytes=0-0" if is_head else "bytes=0-")
        upstream = self.__fetch_upstream(target, forward)
        if upstream is None:
            logger.warning(f"ANiStrmHub本地中转：上游无响应（检查中转代理是否可用）{target}")
            return self.__deny("上游无响应，请检查插件的「中转代理」是否可用", status_code=502)

        headers = {name: upstream.headers[name] for name in RELAY_RESPONSE_HEADERS if upstream.headers.get(name)}
        status_code = upstream.status_code
        content_range = CONTENT_RANGE_RE.match(upstream.headers.get("Content-Range") or "")
        if status_code == 206 and content_range:
            start, end, total = (int(value) for value in content_range.groups())
            headers["Accept-Ranges"] = "bytes"
            if client_range:
                headers["Content-Length"] = str(end - start + 1)
            else:
                # 客户端要的是整个文件：按 200 回整个文件，长度为文件总大小
                status_code = 200
                headers.pop("Content-Range", None)
                headers["Content-Length"] = str(total)
        if upstream.headers.get("Content-Encoding"):
            # iter_content 会解压，长度与 Content-Length 对不上，交给框架按分块传输
            headers.pop("Content-Length", None)
        if is_head or status_code >= 400:
            # 先读完剩余内容(HEAD 只取了 1 字节)再关闭，连接才会放回复用池；
            # 直接关闭会丢弃连接，紧接着的起播请求又要重新经代理建连
            try:
                upstream.content
            except Exception:
                pass
            upstream.close()
            return Response(status_code=status_code, headers=headers)

        def body():
            try:
                for chunk in upstream.iter_content(chunk_size=RELAY_CHUNK_BYTES):
                    if chunk:
                        yield chunk
            finally:
                upstream.close()

        return StreamingResponse(body(), status_code=status_code, headers=headers)

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

    @staticmethod
    def __notice(text: str) -> dict:
        """分区顶部的功能说明：用浅色提示条，比输入框下方的小字 hint 醒目"""
        return {
            "component": "VAlert",
            "props": {"type": "info", "variant": "tonal", "density": "compact", "class": "mb-4", "text": text},
        }

    @staticmethod
    def __source_notes(url: str, builtin: Tuple[Tuple[str, str], ...]) -> List[Tuple[str, str]]:
        """地址的注释标签：(文字, 颜色)。内置地址显示「内置」+名称+说明，其余显示「自定义」"""
        names = dict(builtin)
        if RELAY_API_PATH in url:
            mode = "局域网免密钥" if ANiStrmHub.is_lan_host(urlparse(url).hostname) else "带密钥"
            return [("本地中转", "info"), (mode, "default")]
        if url not in names:
            return [("自定义", "secondary")]
        notes = [("内置", "primary"), (names[url], "default")]
        for tag in BUILTIN_TAGS.get(url, ()):
            notes.append((tag, "success" if "已加速" in tag else "default"))
        return notes

    @classmethod
    def __source_picker(
        cls,
        model_key: str,
        list_key: str,
        label: str,
        placeholder: str,
        builtin: Tuple[Tuple[str, str], ...],
    ) -> dict:
        """当前订阅源/加速源的下拉框。选项来自下方的地址列表，并随列表的删除实时
        变化(items 用前端表达式从 model 计算)；每个选项第二行显示注释标签的文字。
        title 与 value 都是地址本身、return-object 关闭，保证存进配置的始终是纯
        地址字符串——已在 Vuetify 3.7.3 下实测选择与手动输入两种情况。"""
        notes = {url: " · ".join(text for text, _ in cls.__source_notes(url, builtin)) for url, _ in builtin}
        items_expr = (
            "{{ (%s || []).map(u => ({ title: u, value: u, subtitle: (%s)[u] || "
            "(u.includes(%s) ? '本地中转' : '自定义') })) }}"
            % (list_key, json.dumps(notes, ensure_ascii=False), json.dumps(RELAY_API_PATH))
        )
        props: Dict[str, Any] = {
            "model": model_key,
            "label": label,
            "items": items_expr,
            "item-props": True,
            "return-object": False,
            "placeholder": placeholder,
            "clearable": True,
        }
        return {"component": "VCombobox", "props": props}

    @classmethod
    def __source_manager(
        cls,
        urls: List[str],
        list_key: str,
        current_key: str,
        builtin: Tuple[Tuple[str, str], ...],
    ) -> List[dict]:
        """下拉框下方的地址管理列表：每行一个已保存的地址，带注释标签、「当前使用」
        标记、「设为当前」和删除按钮。

        插件配置页是后端下发的 JSON 组件树，下拉菜单的每个选项拿不到自己的数据，
        放不了删除按钮，所以管理操作放在这个列表里。按钮用前端事件函数直接改
        model：删除即从列表数组移除(行随之隐藏，下拉选项同步消失)，保存后生效。"""
        rows: List[dict] = [
            {
                "component": "div",
                "props": {"class": "text-subtitle-2 mt-2 mb-1"},
                "text": "已保存的地址",
            }
        ]
        if not urls:
            rows.append({"component": "div", "props": {"class": "text-body-2 text-medium-emphasis"}, "text": "暂无地址"})
        for url in urls:
            literal = json.dumps(url)
            chips = [
                {
                    "component": "VChip",
                    "props": {"size": "x-small", "color": color, "variant": "tonal", "label": True, "class": "me-1"},
                    "text": text,
                }
                for text, color in cls.__source_notes(url, builtin)
            ]
            chips.append(
                {
                    "component": "VChip",
                    "props": {
                        "size": "x-small",
                        "color": "success",
                        "variant": "flat",
                        "label": True,
                        "class": "me-1",
                        "show": "{{ %s === %s }}" % (current_key, literal),
                    },
                    "text": "当前使用",
                }
            )
            # 外层只负责显示/隐藏，不能带 style：FormRender 处理 show 时往 parsedProps.style
            # 上设置 display——style 是字符串时直接失效；是对象时会被就地修改，Vue 比较
            # 前后引用相同而跳过更新，删除后行依然显示。样式全部放在内层。
            rows.append(
                {
                    "component": "div",
                    "props": {"show": "{{ (%s || []).includes(%s) }}" % (list_key, literal)},
                    "content": [
                        {
                            # 单行排布：地址过长时截断显示省略号，标签与按钮不换行
                            "component": "div",
                            "props": {
                                "class": "d-flex align-center py-1",
                                "style": "gap: 6px; border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));",
                            },
                            "content": [
                        {
                            "component": "span",
                            "props": {
                                "class": "text-body-2 text-truncate",
                                "style": "min-width: 0; flex: 1 1 auto;",
                                "title": url,
                            },
                            "text": cls.short_url(url),
                        },
                        {"component": "div", "props": {"class": "d-flex flex-shrink-0 align-center"}, "content": chips},
                        {
                            "component": "VBtn",
                            "props": {
                                "size": "small",
                                "variant": "text",
                                "color": "primary",
                                "show": "{{ %s !== %s }}" % (current_key, literal),
                                "onClick": "function() { %s = %s; }" % (current_key, literal),
                            },
                            "text": "设为当前",
                        },
                        {
                            # 图标必须作为子组件放进按钮：MoviePilot 的 FormRender 总会传入默认插槽，
                            # 会盖掉 VBtn 的 icon 属性，导致按钮显示为空白
                            "component": "VBtn",
                            "props": {
                                "icon": True,
                                "size": "small",
                                "variant": "text",
                                "color": "error",
                                "title": "删除",
                                "onClick": (
                                    "function() { const i = (%(l)s || []).indexOf(%(u)s); if (i > -1) %(l)s.splice(i, 1); "
                                    "if (%(c)s === %(u)s) %(c)s = ''; }" % {"l": list_key, "u": literal, "c": current_key}
                                ),
                            },
                            "content": [{"component": "VIcon", "props": {"icon": "mdi-delete-outline", "size": "small"}}],
                        },
                            ],
                        }
                    ],
                }
            )
        rows.append(
            {
                "component": "div",
                "props": {"class": "text-caption text-medium-emphasis mt-1"},
                "text": "删除后点击保存生效；内置地址删除后不会自动恢复，需要时可在下拉框重新输入",
            }
        )
        return rows

    def __relay_section(self) -> dict:
        """「本地中转」设置卡片：一行填地址与代理，一行管理密钥，底部一句状态"""
        relay_prefix = self.relay_prefix()
        is_lan = self.relay_address_is_lan()
        if not self._relay_enabled:
            status_type, status = "info", "未启用。启用并填写 strm 访问地址、保存后，本地中转会作为一条线路加入加速源列表"
        elif not relay_prefix:
            status_type, status = "warning", "请填写 strm 访问地址（以 http:// 或 https:// 开头）后保存"
        else:
            mode = "局域网地址：链接不带密钥，仅家里的设备可播放" if is_lan else "公网地址：链接自动附带密钥，在家和在外都能播放"
            using = (self._accelerator_prefix or "").rstrip("/") == relay_prefix
            tail = "已是当前加速源" if using else "在插件详情页点击「本地中转」即可把全部 strm 改为经中转播放"
            status_type, status = ("success" if using else "info"), f"{mode}；{tail}"
        return self.__config_card(
            "本地中转",
            [
                self.__notice(
                    "为不走代理的媒体服务器和播放器提供视频转发：strm 指向 MoviePilot，由 MoviePilot 经你的代理拉取视频"
                    "再转发，支持拖动进度，播放设备无需任何代理设置。只转发 ANi 视频，使用期间请保持插件启用。"
                ),
                self.__row(
                    [
                        (3, {"component": "VSwitch", "props": {"model": "relay_enabled", "label": "启用本地中转"}}),
                        (
                            5,
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": "relay_address",
                                    "label": "strm 访问地址",
                                    "placeholder": "https://mp.example.com:8443",
                                    "hint": "播放设备访问 MoviePilot 的地址。填公网地址在家和在外都能播放；只在家里看可填局域网地址",
                                    "persistent-hint": True,
                                },
                            },
                        ),
                        (
                            4,
                            {
                                "component": "VTextField",
                                "props": {
                                    "model": "relay_proxy",
                                    "label": "中转代理",
                                    "placeholder": "留空使用 MoviePilot 的代理设置",
                                    "hint": "如 http://192.168.1.2:7890",
                                    "persistent-hint": True,
                                },
                            },
                        ),
                    ]
                ),
                self.__row(
                    [
                        (3, {"component": "div"}),
                        (
                            5,
                            {
                                # 只读输入框绑定密钥，右侧刷新图标即「重置密钥」：在浏览器里直接生成新
                                # 密钥并显示出来，保存后生效。图标用 append-inner-icon 属性而不是插槽内容，
                                # 不受 FormRender 默认插槽的影响
                                "component": "VTextField",
                                "props": {
                                    "model": "relay_token",
                                    "label": "中转密钥",
                                    "readonly": True,
                                    "append-inner-icon": "mdi-refresh",
                                    "onClick:appendInner": RELAY_TOKEN_JS,
                                    "hint": "公网地址时写入 strm 链接。点击右侧图标重置，保存后到详情页重建直链，旧链接失效",
                                    "persistent-hint": True,
                                },
                            },
                        ),
                    ]
                ),
                {
                    "component": "VAlert",
                    "props": {"type": status_type, "variant": "tonal", "density": "compact", "class": "mt-3", "text": status},
                },
            ],
        )

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
                                                "hint": "改后在详情页运行「重建目录结构」",
                                                "persistent-hint": True,
                                            },
                                        },
                                    ),
                                ]
                            ),
                        ],
                    ),
                    self.__config_card(
                        "订阅源",
                        [
                            self.__notice(
                                "订阅源提供 ANi 新番列表（RSS），决定能发现哪些新番。"
                                "从下拉框选择当前使用的订阅源；输入新地址并保存即可添加，添加的地址会保留在下方列表中，可随时删除。"
                            ),
                            self.__row(
                                [
                                    (
                                        8,
                                        self.__source_picker(
                                            "subscription_source", "subscription_list", "当前订阅源", "选择或输入 RSS 地址",
                                            BUILTIN_SUBSCRIPTIONS,
                                        ),
                                    ),
                                    (
                                        4,
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
                                ]
                            ),
                            *self.__source_manager(
                                self._subscription_list, "subscription_list", "subscription_source", BUILTIN_SUBSCRIPTIONS
                            ),
                        ],
                    ),
                    self.__config_card(
                        "加速源",
                        [
                            self.__notice(
                                "加速源只作用于 strm 中的视频播放链接，用于提升 Emby/Jellyfin/飞牛影视 等媒体服务器的播放速度，"
                                "不影响订阅源的获取。留空则直接使用订阅源给出的视频链接；"
                                "当前订阅源已是「视频链接已加速」的镜像时，通常无需再配置。"
                            ),
                            self.__row(
                                [
                                    (
                                        8,
                                        self.__source_picker(
                                            "accelerator_prefix", "accelerator_list", "当前加速源", "留空则不加速",
                                            BUILTIN_ACCELERATORS,
                                        ),
                                    ),
                                ]
                            ),
                            *self.__source_manager(
                                self._accelerator_list, "accelerator_list", "accelerator_prefix", BUILTIN_ACCELERATORS
                            ),
                        ],
                    ),
                    self.__relay_section(),
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "warning",
                            "variant": "tonal",
                            "density": "compact",
                            "class": "mb-4",
                            "text": "维护操作（重建直链、重建目录结构、补全历史剧集、连通性检测）在插件详情页点击按钮执行，"
                            "执行前可看到本地 strm 的线路分布",
                        },
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "density": "compact",
                            "style": "white-space: pre-line;",
                            "text": "生成的 strm 建议配合 MoviePilot 的「目录监控」与「媒体整理」，刮削后整理到媒体库目录",
                        },
                        "content": [
                            {
                                "component": "div",
                                "props": {"class": "mt-1"},
                                "content": [
                                    {"component": "span", "text": "详细说明："},
                                    {
                                        "component": "a",
                                        "props": {
                                            "href": "https://github.com/oiloveio/MoviePilot-Plugins",
                                            "target": "_blank",
                                            "rel": "noopener",
                                        },
                                        "text": "github.com/oiloveio/MoviePilot-Plugins",
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
            "subscription_list": [url for url, _ in BUILTIN_SUBSCRIPTIONS],
            "accelerator_list": [prefix for prefix, _ in BUILTIN_ACCELERATORS],
            "relay_enabled": False,
            "relay_address": "",
            "relay_proxy": "",
            "relay_token": "",
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
                "relay_enabled": self._relay_enabled,
                "relay_address": self._relay_address,
                "relay_proxy": self._relay_proxy,
                "relay_token": self._relay_token,
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
        egress = detect_result.get("direct_egress") or {}
        if egress.get("ip"):
            abroad = egress.get("country") and egress.get("country") != "CN"
            body.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "warning" if abroad else "info",
                        "variant": "tonal",
                        "density": "compact",
                        "class": "mt-3",
                        "text": f"MoviePilot 直连出口：{egress['ip']}（{egress.get('country') or '未知'} {egress.get('org') or ''}）"
                        + (
                            "。出口不在国内，说明所在网络本身已被代理（如透明代理、路由器翻墙），"
                            "「官方直链」可达不代表未翻墙的设备也能访问"
                            if abroad
                            else ""
                        ),
                    },
                }
            )
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
                        "text": f"{mismatched} 个strm的线路与当前加速源配置不一致，在上方「维护操作」点击目标线路即可统一",
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

    @staticmethod
    def __action_button(text: str, action: str, params: Optional[Dict[str, Any]] = None, **props) -> dict:
        """详情页按钮：MoviePilot 的 PageRender 用 events 描述点击调用的接口(自动携带
        登录令牌)，执行完自动刷新详情页。按钮文字写在 text 字段"""
        return {
            "component": "VBtn",
            # text-none：Vuetify 按钮默认把英文转成大写(pili → PILI)
            "props": {"class": "me-2 mb-2 text-none", **props},
            "text": text,
            "events": {
                "click": {"api": f"plugin/ANiStrmHub/action/{action}", "method": "post", "params": params or {}}
            },
        }

    def __action_card(self, local_distribution: Dict[str, Any]) -> dict:
        """详情页顶部的维护操作区：先看本地 strm 的线路分布，再点目标线路执行重建"""
        busy = self._maintenance_lock.locked()
        current = (self._accelerator_prefix or "").rstrip("/")
        options: List[Tuple[str, str]] = [("", "订阅源原始链接（不加速）")]
        relay_prefix = self.relay_prefix()
        for prefix in dict.fromkeys(self._accelerator_list + ([relay_prefix] if relay_prefix else [])):
            options.append((prefix, self.__display_name(prefix, BUILTIN_ACCELERATORS)))

        route_buttons = [
            self.__action_button(
                f"{label}（当前）" if prefix == current else label,
                "rebuild",
                {"accelerator": prefix},
                color="primary",
                variant="flat" if prefix == current else "tonal",
                disabled=busy,
            )
            for prefix, label in options
        ]

        by_category = local_distribution.get("by_category") or {}
        if by_category:
            distribution: List[dict] = [
                {
                    "component": "VChip",
                    "props": {"size": "small", "variant": "tonal", "label": True, "class": "me-2 mb-2"},
                    "text": f"{category} · {count} 个",
                }
                for category, count in sorted(by_category.items(), key=lambda kv: kv[1], reverse=True)
            ]
        else:
            distribution = [
                {
                    "component": "span",
                    "props": {"class": "text-body-2 text-medium-emphasis"},
                    "text": "尚无统计，运行一次连通性检测或重建直链后显示",
                }
            ]

        other_buttons = [
            self.__action_button("立即订阅同步", "sync", variant="tonal", disabled=busy),
            self.__action_button("重建目录结构", "regroup", variant="tonal", disabled=busy),
            self.__action_button("补全历史剧集", "backfill", variant="tonal", disabled=busy),
            self.__action_button("连通性检测", "detect", variant="tonal", disabled=busy),
        ]
        body: List[dict] = []
        if busy:
            body.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "density": "compact",
                        "class": "mb-3",
                        "text": "有维护任务正在后台运行，完成前按钮不可用；稍后刷新本页查看进度",
                    },
                }
            )
        body += [
            {"component": "div", "props": {"class": "text-subtitle-2 mb-1"}, "text": "重建直链"},
            {
                "component": "div",
                "props": {"class": "text-body-2 text-medium-emphasis mb-2"},
                "text": "点击目标线路，即把全部 strm 改为经该线路播放，并设为当前加速源，之后新生成的 strm 也走这条线路。"
                "逐个实测可达才覆盖，不可达的保留原样。",
            },
            {"component": "div", "props": {"class": "text-caption mb-1"}, "text": "本地 strm 当前线路分布"},
            {"component": "div", "props": {"class": "d-flex flex-wrap mb-2"}, "content": distribution},
            {"component": "div", "props": {"class": "text-caption mb-1"}, "text": "重建为"},
            {"component": "div", "props": {"class": "d-flex flex-wrap"}, "content": route_buttons},
            {"component": "VDivider", "props": {"class": "my-3"}},
            {"component": "div", "props": {"class": "text-subtitle-2 mb-2"}, "text": "其他操作"},
            {"component": "div", "props": {"class": "d-flex flex-wrap"}, "content": other_buttons},
            {
                "component": "div",
                "props": {"class": "text-caption text-medium-emphasis mt-1", "style": "white-space: pre-line;"},
                "text": "重建目录结构：按配置页的「strm 存放方式」移动文件，不改内容\n"
                "补全历史剧集：回溯 RSS 窗口之外的早期集数，串行限流探测\n"
                "连通性检测：检查订阅源，并实测各播放线路的首包耗时与下载速度\n"
                "任务在后台执行，同一时间只运行一个；完成后刷新本页查看结果",
            },
        ]
        return {
            "component": "VCard",
            "props": {"class": "mb-4"},
            "content": [
                {"component": "VCardTitle", "text": "维护操作"},
                {"component": "VCardText", "content": body},
            ],
        }

    def get_page(self) -> List[dict]:
        local_distribution = self.get_data("local_distribution") or {}
        if not local_distribution and self._storageplace:
            # 还没有统计过：当场扫一次本地 strm(只读文件，不发网络请求)，让维护操作区直接看到分布
            local_distribution = StrmRelinkService.scan_local_distribution(self._storageplace, self._accelerator_prefix)
        content: List[dict] = [self.__action_card(local_distribution)]

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
                        "text": "还没有检测数据。点击上方「连通性检测」运行一次，这里会显示订阅源状态、"
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
            if self._relay_session is not None:
                self._relay_session.close()
                self._relay_session = None
        except Exception as err:
            logger.error(f"退出插件失败：{err}")


class AniRssAggregator:
    """按URL抓取并解析单个ANi RSS源(ani-download.xml格式)。不持有"数据源
    列表"这个状态——具体拉哪个源由调用方(ANiStrmHub)决定，这里只负责
    "给一个URL，抓取解析成条目列表"这一件事。"""

    def __init__(self, use_proxy: bool = False):
        self._use_proxy = use_proxy
        self._direct_session: Optional[Session] = None

    def set_use_proxy(self, use_proxy: bool):
        self._use_proxy = use_proxy

    def build_request_utils(self) -> RequestUtils:
        return RequestUtils(
            ua=settings.USER_AGENT if settings.USER_AGENT else None,
            proxies=settings.PROXY if self._use_proxy and settings.PROXY else None,
        )

    def build_direct_request_utils(self, timeout: Optional[int] = None) -> RequestUtils:
        """视频直链探测专用，不走MoviePilot代理：strm最终由Emby/Jellyfin/播放器
        直接访问，它们不经过MP的代理。探测必须站在同样的网络视角，否则会出现
        "插件测得通、播放器播不了"的假阳性。

        只是不传 proxies 还不够：requests 默认会读取环境变量 HTTP_PROXY/HTTPS_PROXY，
        MoviePilot 容器常用这两个变量配置代理(MoviePilot 自己也会读取它们作为代理
        设置)，结果"直连"测试实际走了代理。必须用 trust_env=False 的会话明确忽略。"""
        if self._direct_session is None:
            session = Session()
            session.trust_env = False
            self._direct_session = session
        return RequestUtils(
            ua=settings.USER_AGENT if settings.USER_AGENT else None, session=self._direct_session, timeout=timeout
        )

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
        # 本地中转的前缀本身带路径(/api/v1/plugin/...)，要先于按主机逐跳拆分识别，
        # 否则会被误判为多层套壳
        relay_index = link.find(RELAY_API_PATH + "/")
        if relay_index > 0:
            label = f"本地中转 {urlparse(link[:relay_index]).netloc}"
            if RELAY_API_PATH in accelerator and link.startswith(accelerator + "/"):
                return f"{label}（当前配置）"
            return label
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

    def _request_utils_for(self, url: str):
        """本地中转线路放宽等待时间，其余线路用默认值"""
        if RELAY_API_PATH in url:
            return self._request_factory(timeout=RELAY_PROBE_TIMEOUT_SECONDS)
        return self._request_factory()

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
            request_utils = self._request_utils_for(url)
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
            request_utils = self._request_utils_for(url)
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
                # 时长上限从收到首包开始算：冷启动首包慢(实测中转可达 13 秒)时，从发起请求
                # 算会在首包一到就停止，只读到一小块，速度被严重低估(实测误报 4.8KB/s)
                if received >= SPEED_TEST_BYTES or now - first_at >= SPEED_TEST_MAX_SECONDS:
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
        统计有多少个跟它不一致——不一致的可以在详情页重建直链一次性统一。"""
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
