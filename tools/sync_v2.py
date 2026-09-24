"""由 plugins.v3 生成 plugins.v2 兼容副本。

V2 宿主没有 app.sdk.* 稳定出口，只能用 app.core.config / app.log / app.utils.http
这几个旧路径，因此两份实现只有这 3 行 import 不同，其余代码必须完全一致。
修改业务逻辑时只改 V3，再运行本脚本生成 V2：

    python tools/sync_v2.py          # 写入 V2
    python tools/sync_v2.py --check  # 只检查，不一致时退出码为 1（供测试/CI 使用）
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGINS = ("anistrmhub",)
IMPORT_MAP = {
    "from app.sdk.config import settings": "from app.core.config import settings",
    "from app.sdk.logging import logger": "from app.log import logger",
    "from app.sdk.network import RequestUtils": "from app.utils.http import RequestUtils",
}
HEADER = (
    "# 本文件由 tools/sync_v2.py 从 plugins.v3/{name}/__init__.py 生成，请勿直接修改。\n"
    "# V2 宿主没有 app.sdk.* 稳定出口，两份实现只有 settings/logger/RequestUtils 三行 import 不同。\n"
)


def render_v2(name: str) -> str:
    source = (ROOT / "plugins.v3" / name / "__init__.py").read_text(encoding="utf-8")
    for v3_import, v2_import in IMPORT_MAP.items():
        if source.count(v3_import) != 1:
            raise SystemExit(f"{name}: 未找到唯一的 `{v3_import}`，请检查 V3 的 import")
        source = source.replace(v3_import, v2_import)
    return HEADER.format(name=name) + source


def main(check: bool) -> int:
    stale = []
    for name in PLUGINS:
        target = ROOT / "plugins.v2" / name / "__init__.py"
        expected = render_v2(name)
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        if current == expected:
            continue
        if check:
            stale.append(str(target.relative_to(ROOT)))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(expected, encoding="utf-8")
            print(f"已生成 {target.relative_to(ROOT)}")
    if stale:
        print("以下 V2 副本与 V3 不一致，请运行 python tools/sync_v2.py：\n  " + "\n  ".join(stale))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(check="--check" in sys.argv[1:]))
