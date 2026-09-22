### MoviePilot-Plugins

oiloveio 维护的 [MoviePilot](https://github.com/jxxghp/MoviePilot) 第三方插件仓库。仓库结构参考并 fork 自 [honue/MoviePilot-Plugins](https://github.com/honue/MoviePilot-Plugins/)，感谢 [jxxghp](https://github.com/jxxghp) 开发并开源 MoviePilot 本体，感谢 [honue](https://github.com/honue) 开源 ANi-Strm 插件与整套插件仓库结构。

本仓库不限于 ANi 相关插件，目前收录：

- [ANiStrmHub插件](./plugins.v3/anistrmhub/README.md)

> 多源聚合抓取 ANi 新番资源直链，自动去重、轮询多个镜像容灾，生成 strm 文件，由 mp 刮削入库，媒体服务器直连播放。fork 自 honue 的 ANi-Strm，改名避免与原版及其他 fork（ANiStrmPlus、ANiStrmPro）撞插件 ID

后续新增插件会持续加到这个列表里。

### 仓库结构

按 [MoviePilot 插件开发指南（V3）](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/Plugin_Development.md) 组织，同时按 [V2 插件开发指南](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/V2_Plugin_Development.md) 为需要的插件提供 V2 兼容实现：

```text
MoviePilot-Plugins/
├── plugins.v3/
│   └── <plugin_id_lower>/       # V3实现，用app.sdk.*稳定出口
│       ├── __init__.py
│       ├── README.md
│       └── ...
├── plugins.v2/
│   └── <plugin_id_lower>/       # V2兼容实现(按需)，只有import路径不同，业务逻辑与V3版本逐字节一致
│       └── __init__.py
├── tests/
│   └── v3/
│       └── <plugin_id_lower>/
│           └── test_plugin.py
├── icons/
├── package.v3.json
└── package.v2.json
```

新插件统一往这套结构里加：V3实现放 `plugins.v3/`，索引写 `package.v3.json`；如果要兼容 V2 宿主（V2 没有 `app.sdk.*`，没法跟 V3 共用源码），再单独建 `plugins.v2/` 和 `package.v2.json`。

### ANiStrmHub 与上游 honue/ANi-Strm 的区别

- 数据源从单一的目录扫描 API，改为多个 RSS 镜像聚合抓取，任意镜像失效自动切换
- 数据源列表可在插件页面直接填写、增删、注释禁用，无需改代码
- 跨镜像按标题去重，避免同一集重复生成 strm
- 新增「修复本地失效链接」：数据源失效后，一键批量修复本地已经生成的 strm 文件（标题匹配 + 路径迁移兜底），不用删了重生成
- 新增「探测数据源健康度」「按来源统计本地strm分布」「一键切换到指定来源」
- 借鉴 [ANiStrmPlus](https://github.com/MangMax/MoviePilot-Plugins)（按季度分子目录）和 [ANiStrmPro](https://github.com/shanhai2333/MoviePilot-Plugins)（文件名清洗、黑名单过滤、字幕文件过滤）两个同类 fork 的可取之处

详细说明见 [插件自带文档](./plugins.v3/anistrmhub/README.md)。

### 如果对你有所帮助⭐

上游仓库：[![Stargazers over time](https://starchart.cc/honue/MoviePilot-Plugins.svg?background=%23FFFFFF&axis=%23333333&line=%2363beff)](https://starchart.cc/honue/MoviePilot-Plugins)
