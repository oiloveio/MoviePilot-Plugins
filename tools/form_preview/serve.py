"""本地预览服务：真实运行插件代码，渲染配置页与详情页，详情页按钮真实调用插件接口。

    .venv/bin/python tools/form_preview/serve.py '<插件配置JSON>' [端口]
    浏览器打开 http://localhost:端口/          配置页（FormRender）
             http://localhost:端口/page.html  详情页（PageRender，按钮可点击执行）

网络请求走真实网络（requests），行为与 MoviePilot 的 RequestUtils 一致：有 session
用 session，否则 requests.request。插件接口按 MoviePilot 的规则挂载在
/api/v1/plugin/ANiStrmHub{path}；本地预览不做登录校验。
"""
import json
import logging
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "v3" / "anistrmhub"))
warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
import conftest  # noqa: E402,F401  注入 app.* 替身模块

import requests  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402

import app.plugins.anistrmhub as module  # noqa: E402


class RealRequestUtils:
    def __init__(self, ua=None, proxies=None, session=None, timeout=None, **kwargs):
        self.headers = {"User-Agent": ua or "Mozilla/5.0"}
        self.proxies, self.session, self.timeout = proxies, session, timeout or 20

    def update_headers(self, headers):
        self.headers.update(headers)

    def get_res(self, url, **kwargs):
        kwargs.setdefault("headers", self.headers)
        kwargs.setdefault("proxies", self.proxies)
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("verify", False)
        try:
            return (self.session.request if self.session else requests.request)("get", url, **kwargs)
        except requests.exceptions.RequestException as err:
            logging.warning(f"[请求异常] {err.__class__.__name__}: {str(err)[:120]}")
            return None


module.RequestUtils = RealRequestUtils
config = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
port = int(sys.argv[2]) if len(sys.argv) > 2 else 8766
plugin = module.ANiStrmHub()
plugin.init_plugin(config)
if "MP_PROXY" in config:
    module.settings.PROXY = {"http": config["MP_PROXY"], "https": config["MP_PROXY"]}

app = FastAPI()
here = Path(__file__).parent


@app.get("/")
def form_page():
    return FileResponse(here / "index.html")


@app.get("/page.html")
def detail_page():
    return FileResponse(here / "page.html")


@app.get("/form.json")
def form_json():
    form, model = plugin.get_form()
    model.update(plugin.get_config() or config)
    return JSONResponse({"form": form, "model": model})


@app.get("/api/v1/plugin/page/ANiStrmHub")
def page_json():
    return JSONResponse(plugin.get_page())


for api in plugin.get_api():
    api = dict(api)
    api.pop("allow_anonymous", None)
    api.pop("auth", None)
    api["path"] = f"/api/v1/plugin/ANiStrmHub{api['path']}"
    app.add_api_route(**api)

uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
