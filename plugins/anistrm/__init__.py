import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.utils.http import RequestUtils

DEFAULT_RSS_SOURCES = """https://api.pili.cc.cd/ani-download.xml
https://aniapi.op5.de5.net/ani-download.xml
https://api.ani.rip/ani-download.xml
# https://aniapi.v300.eu.org/ani-download.xml  RSS能拉到,但视频直链域名proi.v300.eu.org被Cloudflare挑战拦截,实测无法取到视频数据,已禁用
# https://aniapi.td.ee/ani-download.xml  RSS能拉到,但视频直链域名ani.td.ee返回"temporarily rate limited",实测无法取到视频数据,已禁用
# http://open.ani.rip/ani-download.xml  当前403(Cloudflare拦截)，恢复后可去掉#启用
# https://openani.an-i.workers.dev/ani-download.xml  当前429(限流)，恢复后可去掉#启用"""


class ANiStrm(_PluginBase):
    plugin_name = "ANiStrm"
    plugin_desc = "多源聚合抓取ANi新番资源，自动去重轮询多个镜像，生成strm文件，mp刮削入库，媒体服务器直连播放"
    plugin_icon = "https://raw.githubusercontent.com/honue/MoviePilot-Plugins/main/icons/anistrm.png"
    plugin_version = "3.1.0"
    plugin_author = "honue,oiloveio"
    author_url = "https://github.com/honue"
    plugin_config_prefix = "anistrm_"
    plugin_order = 15
    auth_level = 2

    _enabled = False
    _use_proxy = True
    _cron = None
    _onlyonce = False
    _relink_once = False
    _storageplace = None
    _rss_sources = DEFAULT_RSS_SOURCES
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
        self._relink_once = config.get("relink_once", False)
        self._storageplace = config.get("storageplace") or "/downloads/strm"
        self._rss_sources = config.get("rss_sources")
        if not self._rss_sources:
            self._rss_sources = DEFAULT_RSS_SOURCES
        self._client.set_use_proxy(self._use_proxy)
        self._client.set_sources(self._rss_sources)
        logger.info(
            f"ANi-Strm配置加载：enabled={self._enabled}, onlyonce={self._onlyonce}, "
            f"use_proxy={self._use_proxy}, storage={self._storageplace}, "
            f"数据源数={len(self._client.get_source_urls())}"
        )

        if not (self._enabled or self._onlyonce or self._relink_once):
            logger.info("ANi-Strm未启用且未触发立即运行，跳过任务注册")
            return

        self._scheduler = BackgroundScheduler(timezone=settings.TZ)

        if self._enabled and self._cron:
            try:
                self._scheduler.add_job(
                    func=self.__task,
                    trigger=CronTrigger.from_crontab(self._cron),
                    name="ANiStrm文件创建",
                )
                logger.info(f"ANi-Strm定时任务创建成功：{self._cron}")
            except Exception as err:
                logger.error(f"定时任务配置错误：{err}")

        if self._onlyonce:
            logger.info("ANi-Strm服务启动，立即运行一次")
            self._scheduler.add_job(
                func=self.__task,
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="ANiStrm文件创建",
            )
            self._onlyonce = False

        if self._relink_once:
            logger.info("ANi-Strm服务启动，立即修复本地已存在的失效链接")
            self._scheduler.add_job(
                func=self.__relink_task,
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="ANiStrm修复失效链接",
            )
            self._relink_once = False

        self.__update_config()

        if self._scheduler.get_jobs():
            self._scheduler.print_jobs()
            self._scheduler.start()

    def __task(self):
        source_urls = self._client.get_source_urls()
        if not source_urls:
            logger.info("未配置任何数据源，任务结束")
            return

        logger.info(f"ANi-Strm任务开始：数据源数={len(source_urls)}，storage={self._storageplace}")

        entries, source_stats = self._client.fetch_all_entries()
        logger.info(
            "ANi-Strm数据源抓取结果："
            + "; ".join(f"{url}={stat}" for url, stat in source_stats.items())
        )

        if not entries:
            logger.warning("ANi-Strm所有数据源均不可用或无内容，本次任务结束")
            return

        total_created = 0
        total_exists = 0
        total_failed = 0
        for entry in entries:
            status = self._strm_service.touch_strm_file(
                storage_path=self._storageplace,
                file_name=entry["title"],
                file_url=entry["link"],
            )
            if status == "created":
                total_created += 1
            elif status == "exists":
                total_exists += 1
            else:
                total_failed += 1

        logger.info(
            f"ANi-Strm任务完成：去重后条目数={len(entries)}，"
            f"新增={total_created}，跳过={total_exists}，失败={total_failed}"
        )

    def __relink_task(self):
        entries, source_stats = self._client.fetch_all_entries()
        logger.info(
            "ANi-Strm修复链接：数据源抓取结果："
            + "; ".join(f"{url}={stat}" for url, stat in source_stats.items())
        )
        if not entries:
            logger.warning("ANi-Strm修复链接：所有数据源均不可用，无法作为迁移参照，任务结束")
            return

        title_map = {entry["title"]: entry["link"] for entry in entries}
        reference_link = entries[0]["link"]

        stats = self._relink_service.relink_existing(
            storage_path=self._storageplace,
            title_map=title_map,
            reference_link=reference_link,
        )
        logger.info(
            "ANi-Strm修复链接完成："
            + "，".join(f"{k}={v}" for k, v in stats.items())
        )

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "use_proxy", "label": "使用代理"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "onlyonce", "label": "立即运行一次"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "relink_once",
                                            "label": "修复本地失效链接",
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
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "rss_sources",
                                            "label": "数据源列表（一行一个RSS地址）",
                                            "rows": 8,
                                            "placeholder": DEFAULT_RSS_SOURCES,
                                            "hint": "按行顺序依次轮询抓取，抓取失败的源自动跳过不影响其他源；"
                                            "多个源抓到同一集时，strm里写入排序靠前的源给出的直链地址，"
                                            "所以顺序也是直链域名的优先级——国内能直连的镜像建议放前面，"
                                            "官方裸域名(api.ani.rip/open.ani.rip)不走代理很可能连不通，建议放最后兜底；"
                                            "行首加 # 可临时禁用某个源而不删除",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            }
                        ],
                    },
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
                                            "text": "多源聚合抓取ANi的RSS（ani-download.xml），生成strm文件\n"
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
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "warning",
                                            "variant": "tonal",
                                            "text": "「修复本地失效链接」：勾选后立即扫描Strm存储地址下所有已存在的.strm文件，"
                                            "重新按当前数据源列表解析直链——标题还在RSS窗口内的直接换成最新直链；"
                                            "不在窗口内的老集数，会从旧链接里提取季度/文件名部分，换上当前排序第一个"
                                            "可用源的域名前缀，实际探测确认能连通后才覆盖写入，探测不通过的保留原文件不动。"
                                            "跑完在日志里看统计结果。",
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
            "relink_once": False,
            "storageplace": "/downloads/strm",
            "rss_sources": DEFAULT_RSS_SOURCES,
            "cron": "20 22,23,0,1 * * *",
        }

    def __update_config(self):
        self.update_config(
            {
                "onlyonce": self._onlyonce,
                "relink_once": self._relink_once,
                "cron": self._cron,
                "enabled": self._enabled,
                "use_proxy": self._use_proxy,
                "storageplace": self._storageplace,
                "rss_sources": self._rss_sources,
            }
        )

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as err:
            logger.error(f"退出插件失败：{err}")


class AniRssAggregator:
    """解析rss_sources配置的多个ANi RSS镜像，按顺序轮询抓取并跨源去重"""

    def __init__(self, use_proxy: bool = False, sources_text: str = DEFAULT_RSS_SOURCES):
        self._use_proxy = use_proxy
        self._sources_text = sources_text

    def set_use_proxy(self, use_proxy: bool):
        self._use_proxy = use_proxy

    def set_sources(self, sources_text: str):
        self._sources_text = sources_text or ""

    def build_request_utils(self) -> RequestUtils:
        return RequestUtils(
            ua=settings.USER_AGENT if settings.USER_AGENT else None,
            proxies=settings.PROXY if self._use_proxy and settings.PROXY else None,
        )

    def get_source_urls(self) -> List[str]:
        urls = []
        for raw_line in (self._sources_text or "").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            urls.append(line)
        return list(dict.fromkeys(urls))

    def fetch_all_entries(self) -> Tuple[List[Dict[str, str]], Dict[str, str]]:
        seen_titles: Dict[str, bool] = {}
        merged: List[Dict[str, str]] = []
        source_stats: Dict[str, str] = {}

        for url in self.get_source_urls():
            try:
                entries = self._fetch_one(url)
            except Exception as err:
                logger.warning(f"ANi-Strm数据源抓取失败，跳过：{url} - {err}")
                source_stats[url] = "失败"
                continue

            source_stats[url] = f"{len(entries)}条"
            for entry in entries:
                title = entry["title"]
                if title in seen_titles:
                    continue
                seen_titles[title] = True
                merged.append(entry)

        return merged, source_stats

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
    def touch_strm_file(self, storage_path: str, file_name: str, file_url: str) -> str:
        if not storage_path:
            logger.error("创建strm源文件失败：未配置存储目录")
            return "failed"

        safe_name = file_name.replace("/", "_").replace("\\", "_")
        # RSS里的link本身就是可直接请求的直链(通常带?d=mp4参数)，不要对其做后缀改写，
        # 改写会导致目标站点路由不到实际资源(404)——已实测踩过这个坑
        src_url = file_url

        directory = Path(storage_path)
        file_path = directory / f"{safe_name}.strm"
        if file_path.exists():
            logger.debug(f"ANi-Strm跳过已存在文件：{file_path.name}")
            return "exists"

        try:
            directory.mkdir(parents=True, exist_ok=True)
            file_path.write_text(src_url, encoding="utf-8")
            logger.debug(f"ANi-Strm创建成功：{file_path.name}")
            return "created"
        except Exception as err:
            logger.error(f"创建strm源文件失败：{file_path.name} - {err}")
            return "failed"


class StrmRelinkService:
    """修复本地已生成strm文件里失效的直链

    两种迁移方式：
    1. 标题精确匹配：strm文件名(去掉.strm)就是当年RSS的title，如果这一集还在
       当前RSS的滚动窗口内，直接用最新抓到的link覆盖，最准确。
    2. 路径迁移兜底：ANi各镜像的直链结构是 {前缀}/{季度}/{文件名}?d=mp4，其中
       "季度/文件名?d=mp4"这一段实测在所有镜像间完全一致，只有前缀（域名，
       以及是否带resources.ani.rip中间路径）不同。所以标题不在当前RSS窗口内的
       老集数（比如失效前很久生成的strm），可以从旧链接里提取这一段，换上
       当前配置里第一个成功抓到内容的源的前缀，拼出候选新链接；写入前会用
       Range请求实际探测一次，确认能连通才采用，避免写入猜测性的死链接。
    """

    SEASON_PATH_RE = re.compile(r"(\d{4}-\d{1,2}/.+)$")

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

    def _verify_reachable(self, url: str) -> bool:
        try:
            # 注意：get_res(url, headers=...)里的headers会整体替换掉RequestUtils构造时
            # 设置的默认header(包括UA)，不是合并。必须用update_headers()把Range头合并
            # 进去，否则探测请求会变成没有UA的裸请求，容易被目标站点当可疑流量拦截(403)，
            # 导致本来可达的链接被误判为不可达——这个坑已经在联调时实测踩过。
            request_utils = self._request_factory()
            request_utils.update_headers({"Range": "bytes=0-0"})
            response = request_utils.get_res(url)
            return bool(response) and response.status_code in (200, 206)
        except Exception:
            return False

    def relink_existing(
        self,
        storage_path: str,
        title_map: Dict[str, str],
        reference_link: Optional[str],
    ) -> Dict[str, int]:
        stats = {
            "标题精确匹配更新": 0,
            "无需更新": 0,
            "路径迁移成功": 0,
            "路径迁移失败(保留原文件)": 0,
            "无法识别(保留原文件)": 0,
        }

        directory = Path(storage_path)
        if not directory.exists():
            logger.warning(f"ANi-Strm修复链接：目录不存在，跳过 {storage_path}")
            return stats

        reference_prefix = self.derive_prefix(reference_link) if reference_link else None

        for strm_file in sorted(directory.rglob("*.strm")):
            title = strm_file.stem
            try:
                old_content = strm_file.read_text(encoding="utf-8").strip()
            except Exception as err:
                logger.warning(f"ANi-Strm修复链接：读取失败，跳过 {strm_file.name} - {err}")
                stats["无法识别(保留原文件)"] += 1
                continue

            new_link = title_map.get(title)
            if new_link:
                if new_link != old_content:
                    strm_file.write_text(new_link, encoding="utf-8")
                    stats["标题精确匹配更新"] += 1
                    logger.info(f"ANi-Strm修复链接：标题精确匹配更新 {strm_file.name}")
                else:
                    stats["无需更新"] += 1
                continue

            if not reference_prefix:
                stats["无法识别(保留原文件)"] += 1
                continue

            old_resource_path = self.extract_resource_path(old_content)
            if not old_resource_path:
                logger.warning(f"ANi-Strm修复链接：无法从旧链接提取季度路径，跳过 {strm_file.name}")
                stats["无法识别(保留原文件)"] += 1
                continue

            candidate = reference_prefix + old_resource_path
            if candidate == old_content:
                stats["无需更新"] += 1
                continue

            time.sleep(0.3)
            if self._verify_reachable(candidate):
                strm_file.write_text(candidate, encoding="utf-8")
                stats["路径迁移成功"] += 1
                logger.info(f"ANi-Strm修复链接：路径迁移成功 {strm_file.name}")
            else:
                logger.warning(f"ANi-Strm修复链接：候选链接探测不可达，保留原文件 {strm_file.name}")
                stats["路径迁移失败(保留原文件)"] += 1

        return stats
