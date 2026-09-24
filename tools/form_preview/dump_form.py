"""导出插件配置页 JSON，供 index.html 预览。

    .venv/bin/python tools/form_preview/dump_form.py [配置JSON]
    .venv/bin/python -m http.server 8766 -d tools/form_preview   # 浏览器打开 http://localhost:8766

index.html 按 MoviePilot-Frontend src/components/render/FormRender.vue 逐行移植了
渲染逻辑（show 表达式、on* 事件函数、{{ }} 动态属性），用 Vuetify 3.7.3 渲染，
可以在不部署 MoviePilot 的情况下验证配置页的显示与交互。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "v3" / "anistrmhub"))
import conftest  # noqa: E402,F401  注入 app.* 替身模块

from app.plugins.anistrmhub import ANiStrmHub  # noqa: E402

config = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
plugin = ANiStrmHub()
plugin.init_plugin(config)
form, model = plugin.get_form()
model.update(plugin.get_config() or config)
out = Path(__file__).with_name("form.json")
out.write_text(json.dumps({"form": form, "model": model}, ensure_ascii=False), encoding="utf-8")
print(f"已导出 {out.relative_to(ROOT)}")
