### MoviePilot-Plugins（ANi-Strm 专项 Fork）

本仓库 fork 自 [honue/MoviePilot-Plugins](https://github.com/honue/MoviePilot-Plugins/)，感谢原作者 [honue](https://github.com/honue) 开源的 ANi-Strm 插件与整套 MoviePilot 插件体系。

本 fork 只保留并深度改造了 **ANi-Strm** 一个插件，其余插件请前往上游仓库获取。

- [ANi-Strm插件](./docs/anistrm.md)

> 多源聚合抓取 ANi 新番资源直链，自动去重、轮询多个镜像容灾，生成 strm 文件，由 mp 刮削入库，媒体服务器直连播放

### 与上游的区别

- 数据源从单一的目录扫描 API，改为多个 RSS 镜像聚合抓取，任意镜像失效自动切换
- 数据源列表可在插件页面直接填写、增删、注释禁用，无需改代码
- 跨镜像按标题去重，避免同一集重复生成 strm

### 如果对你有所帮助⭐

上游仓库：[![Stargazers over time](https://starchart.cc/honue/MoviePilot-Plugins.svg?background=%23FFFFFF&axis=%23333333&line=%2363beff)](https://starchart.cc/honue/MoviePilot-Plugins)
