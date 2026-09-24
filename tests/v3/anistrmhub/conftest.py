"""测试环境引导。

在 MoviePilot 宿主虚拟环境中运行时直接使用真实的 app.* 模块；在宿主之外
（本地开发、CI）运行时，注入最小化的 app.* 替身模块，再把
plugins.v3/anistrmhub 挂载为 app.plugins.anistrmhub，使测试可以独立运行：

    python -m venv .venv
    .venv/bin/pip install pytest apscheduler pytz fastapi httpx requests
    .venv/bin/python -m pytest tests/v3/anistrmhub
"""
import importlib.util
import logging
import sys
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins.v3" / "anistrmhub"


def _install_stubs() -> None:
    class _PluginBase:
        def __init__(self):
            self._data = {}

        def get_data(self, key):
            return self._data.get(key)

        def save_data(self, key, value):
            self._data[key] = value

        def update_config(self, config):
            self._config = config

        def get_config(self):
            return getattr(self, "_config", None)

    class _Settings:
        TZ = "Asia/Shanghai"
        USER_AGENT = "stub-ua"
        PROXY = None

    class RequestUtils:
        def __init__(self, ua=None, proxies=None, timeout=None, **kwargs):
            self.ua = ua
            self.proxies = proxies
            self.timeout = timeout

        def update_headers(self, headers):
            pass

        def get_res(self, url, **kwargs):
            return None

    modules = {
        "app": types.ModuleType("app"),
        "app.plugins": types.ModuleType("app.plugins"),
        "app.sdk": types.ModuleType("app.sdk"),
        "app.sdk.config": types.ModuleType("app.sdk.config"),
        "app.sdk.logging": types.ModuleType("app.sdk.logging"),
        "app.sdk.network": types.ModuleType("app.sdk.network"),
    }
    modules["app"].__path__ = []
    modules["app.plugins"].__path__ = [str(PLUGIN_DIR.parent)]
    modules["app.sdk"].__path__ = []
    modules["app.plugins"]._PluginBase = _PluginBase
    modules["app.sdk.config"].settings = _Settings()
    modules["app.sdk.logging"].logger = logging.getLogger("anistrmhub-test")
    modules["app.sdk.network"].RequestUtils = RequestUtils
    sys.modules.update(modules)

    spec = importlib.util.spec_from_file_location(
        "app.plugins.anistrmhub", PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["app.plugins.anistrmhub"] = module
    spec.loader.exec_module(module)


try:
    import app.plugins  # noqa: F401  MoviePilot 宿主环境
except ImportError:
    _install_stubs()
