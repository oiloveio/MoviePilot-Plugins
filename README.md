### MoviePilot-Plugins（ANiStrmHub 专项 Fork）

本仓库是给 [MoviePilot](https://github.com/jxxghp/MoviePilot) 用的第三方插件仓库，感谢 [jxxghp](https://github.com/jxxghp) 开发并开源 MoviePilot 本体。

本仓库 fork 自 [honue/MoviePilot-Plugins](https://github.com/honue/MoviePilot-Plugins/)，感谢原作者 [honue](https://github.com/honue) 开源的 ANi-Strm 插件与整套 MoviePilot 插件体系，本 fork 是在他的实现基础上做的。

本 fork 只保留并深度改造了原来的 **ANi-Strm** 一个插件，其余插件请前往上游仓库获取。插件已改名为 **ANiStrmHub**（插件 ID/类名/目录名同步改），避免与 honue 原版及其他 fork（ANiStrmPlus、ANiStrmPro 等）撞插件 ID，装的时候认这个新名字。

- [ANiStrmHub插件](./plugins.v3/anistrmhub/README.md)

> 多源聚合抓取 ANi 新番资源直链，自动去重、轮询多个镜像容灾，生成 strm 文件，由 mp 刮削入库，媒体服务器直连播放

### 仓库结构

按 [MoviePilot 插件开发指南（V3）](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/Plugin_Development.md) 组织，同时按 [V2 插件开发指南](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/V2_Plugin_Development.md) 提供 V2 兼容实现：

```text
MoviePilot-Plugins/
├── plugins.v3/
│   └── anistrmhub/          # V3实现，用app.sdk.*稳定出口
│       ├── __init__.py
│       ├── README.md
│       └── img/
├── plugins.v2/
│   └── anistrmhub/          # V2兼容实现，只有import路径不同，业务逻辑与V3版本逐字节一致
│       └── __init__.py
├── tests/
│   └── v3/
│       └── anistrmhub/
│           └── test_plugin.py
├── package.v3.json
└── package.v2.json
```

V2宿主没有`app.sdk.*`这套V3才有的稳定出口，两份实现没法共用同一份源码，只能各自维护——改业务逻辑时两个`__init__.py`要同步改，`plugins.v2/anistrmhub/__init__.py`开头写了这条约束。

### 与上游的区别

- 数据源从单一的目录扫描 API，改为多个 RSS 镜像聚合抓取，任意镜像失效自动切换
- 数据源列表可在插件页面直接填写、增删、注释禁用，无需改代码
- 跨镜像按标题去重，避免同一集重复生成 strm
- 新增「修复本地失效链接」：数据源失效后，一键批量修复本地已经生成的 strm 文件（标题匹配 + 路径迁移兜底），不用删了重生成
- 新增「探测数据源健康度」「按来源统计本地strm分布」「一键切换到指定来源」
- 借鉴 [ANiStrmPlus](https://github.com/MangMax/MoviePilot-Plugins)（按季度分子目录）和 [ANiStrmPro](https://github.com/shanhai2333/MoviePilot-Plugins)（文件名清洗、黑名单过滤、字幕文件过滤）两个同类 fork 的可取之处
- 插件按 V3 标准迁移到 `plugins.v3/`，导入统一走 `app.sdk.*`；同时提供 `plugins.v2/` 兼容实现覆盖仍在用 V2 宿主的用户

### 如果对你有所帮助⭐

上游仓库：[![Stargazers over time](https://starchart.cc/honue/MoviePilot-Plugins.svg?background=%23FFFFFF&axis=%23333333&line=%2363beff)](https://starchart.cc/honue/MoviePilot-Plugins)
