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
    plugin_version = "3.0.0"
    plugin_author = "honue,oiloveio"
    author_url = "https://github.com/honue"
    plugin_config_prefix = "anistrm_"
    plugin_order = 15
    auth_level = 2

    _enabled = False
    _use_proxy = True
    _cron = None
    _onlyonce = False
    _storageplace = None
    _rss_sources = DEFAULT_RSS_SOURCES
    _scheduler: Optional[BackgroundScheduler] = None

    def __init__(self):
        super().__init__()
        self._client = AniRssAggregator()
        self._strm_service = StrmFileService()

    def init_plugin(self, config: dict = None):
        self.stop_service()

        config = config or {}
        self._enabled = config.get("enabled", False)
        use_proxy = config.get("use_proxy")
        self._use_proxy = True if use_proxy is None else use_proxy
        self._cron = config.get("cron") or "20 22,23,0,1 * * *"
        self._onlyonce = config.get("onlyonce", False)
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

        if not (self._enabled or self._onlyonce):
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
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
            "rss_sources": DEFAULT_RSS_SOURCES,
            "cron": "20 22,23,0,1 * * *",
        }

    def __update_config(self):
        self.update_config(
            {
                "onlyonce": self._onlyonce,
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
            response = RequestUtils(
                ua=settings.USER_AGENT if settings.USER_AGENT else None,
                proxies=settings.PROXY if self._use_proxy and settings.PROXY else None,
            ).get_res(url)
            if not response or response.status_code != 200:
                status = response.status_code if response else "无响应"
                raise ValueError(f"HTTP状态异常：{status}")
            return self._parse_rss(response.text)

        return self._with_retry(operation, tries=2, delay=3)

    @staticmethod
    def _parse_rss(xml_text: str) -> List[Dict[str, str]]:
        try:
            root = ET.fromstring(xml_text)
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
