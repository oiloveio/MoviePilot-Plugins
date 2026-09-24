# MoviePilot-Plugins

oiloveio 维护的 [MoviePilot](https://github.com/jxxghp/MoviePilot) 第三方插件仓库，不限于 ANi 相关插件。

## 插件列表

| 插件 | 说明 |
|---|---|
| [ANiStrmHub](./plugins.v3/anistrmhub/README.md) | 抓取 ANi 的 RSS 订阅源生成 strm 文件，由 MoviePilot 刮削入库、Emby/Jellyfin 直连播放。支持反代加速源、按剧集分目录存放、补全历史剧集、连通性检测 |

## 仓库结构

按 [MoviePilot 插件开发指南（V3）](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/Plugin_Development.md) 组织，同时按 [V2 插件开发指南](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/V2_Plugin_Development.md) 为需要的插件提供 V2 兼容实现：

```text
MoviePilot-Plugins/
├── plugins.v3/
│   └── <plugin_id_lower>/       # V3 实现，用 app.sdk.* 稳定出口
│       ├── __init__.py
│       ├── README.md
│       └── ...
├── plugins.v2/
│   └── <plugin_id_lower>/       # V2 兼容实现（按需），除 import 外与 V3 逐字节一致
│       └── __init__.py
├── tests/
│   └── v3/
│       └── <plugin_id_lower>/
│           └── test_plugin.py
├── icons/
├── package.v3.json
└── package.v2.json
```

新插件统一按这套结构添加：V3 实现放 `plugins.v3/`，索引写入 `package.v3.json`；需要兼容 V2 宿主时（V2 没有 `app.sdk.*`，无法与 V3 共用源码）再单独建 `plugins.v2/` 与 `package.v2.json`。

## ANiStrmHub 与上游 honue/ANi-Strm 的区别

- 数据源由单一目录扫描 API（被限流/封锁后插件完全失效）改为 RSS 订阅源，内置已加速镜像与官方源供选择，也支持自定义地址
- 新增加速源：给官方直链整体套一层反代前缀，内置两个加速节点可选，每次拉取前自动验证可用性
- 新增本地 strm 维护：重建直链、重建目录结构
- 新增补全历史剧集：回溯找回 RSS 滚动窗口之外的老集数，支持跨季度目录尝试
- 新增连通性检测：订阅源与播放线路分开检测，用真实视频直链实测各线路的首包耗时与下载速度；状态码 + 响应内容双重校验，避免把返回 200 的错误页判定为可达
- strm 可按剧集分目录存放，每部剧一个文件夹
- 提供 V2/V3 双实现与单元测试

详细说明见[插件文档](./plugins.v3/anistrmhub/README.md)。

## 致谢

- [jxxghp](https://github.com/jxxghp) —— 开发并开源 MoviePilot 本体
- [honue](https://github.com/honue) —— 开源 ANi-Strm 插件与整套插件仓库结构，本仓库 fork 自 [honue/MoviePilot-Plugins](https://github.com/honue/MoviePilot-Plugins/)

上游仓库：[![Stargazers over time](https://starchart.cc/honue/MoviePilot-Plugins.svg?background=%23FFFFFF&axis=%23333333&line=%2363beff)](https://starchart.cc/honue/MoviePilot-Plugins)
