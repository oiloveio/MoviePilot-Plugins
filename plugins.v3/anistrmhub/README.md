- [1. ANiStrmHub插件](#MoviePilot-x-ANi-Strm)
    - [2023-10秋 刮削效果](#2023-10秋-刮削效果)
    - [注意事项](#注意事项)
    - [Todo](#Todo)

## 改名说明

本插件原名 ANi-Strm，是 honue 原版的 fork。为避免和 honue 原版及其他 fork（ANiStrmPlus、ANiStrmPro 等）撞插件 ID，从 v3.4.0 起插件 ID/类名/目录名统一改为 **ANiStrmHub**。已安装旧版 ANiStrm 的需要按新 ID 重新安装（视为不同插件，配置不会自动迁移）。

## V3 插件规范说明

本插件按 [MoviePilot 插件开发指南（V3）](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/Plugin_Development.md) 组织：插件目录 `plugins.v3/anistrmhub/`，导入统一走 `app.sdk.*` 稳定出口（`app.sdk.config`/`app.sdk.logging`/`app.sdk.network`），不使用 `app.core.*`/`app.utils.*`/`app.log` 等旧路径。索引信息见仓库根目录 `package.v3.json`。

## 2023-10秋 刮削效果

<div align="center">
	<img src="./img/embyani.png">
</div>


## MoviePilot x ANi-Strm

建议配合目录监控使用，strm文件创建在你插件填写的地址 如/downloads/strm

通过目录监控插件转移到link媒体库文件夹 如/downloads/link/strm，mp会完成刮削 这样也避免了污染正常视频文件的媒体库

```
/downloads/strm:/downloads/link/strm#copy
```

<div align="center">
	<img src="./img/link.png" width="200px">
</div>

不开启一次性创建全部，则每次运行会创建ani最新季度的top15个文件。

<div align="center">
	<img src="./img/pic1.png">
</div>

> 非常感谢 https://aniopen.an-i.workers.dev TG:[Channel_ANi](https://t.me/channel_ani)

## v3.0.0 改动说明（本 fork）

原版依赖单一的目录扫描接口（`openani.an-i.workers.dev` 等），一旦该域名被限流/封锁（429/403），插件即完全失效。

本 fork 改为**多 RSS 镜像聚合**：

- 数据源改成解析官方 `ani-download.xml` 格式的 RSS（含 `title`/`link`/`pubDate`/`anime:size`），不再依赖目录扫描 API
- 插件页面新增「数据源列表」文本框，一行一个 RSS 地址，按顺序轮询；某个源请求失败自动跳过，不影响其他源
- 行首加 `#` 可临时禁用某个源而不删除，方便官方域名恢复后重新启用
- 多个镜像抓到同一集时按标题去重，**strm 里写入的是排序靠前的源给出的直链地址**——顺序即优先级

### 实测结果（非常重要，RSS 能拉到 ≠ 视频能播）

只测 RSS 端点返回 200 是不够的：RSS 里的下载直链和 RSS 本身经常不是同一个域名，直链域名可能单独被拦截/限流。逐个用 `Range` 请求实际视频直链验证（curl 模拟播放器分段拖动播放）后：

  | 地址 | RSS 状态 | 视频直链实测 | 结论 |
  |---|---|---|---|
  | `https://api.pili.cc.cd/ani-download.xml` | 200 | `pro.pili.cc.cd` 返回206 Partial Content + 正确 mp4 数据 | ✅ 默认启用 |
  | `https://aniapi.op5.de5.net/ani-download.xml` | 200 | `pro.op5.de5.net` 返回206 Partial Content + 正确 mp4 数据 | ✅ 默认启用 |
  | `https://api.ani.rip/ani-download.xml` | 200 | `resources.ani.rip` 返回206 + 正确 mp4 数据（测试环境非大陆网络，国内是否直连未知） | ✅ 默认启用，排最后兜底 |
  | `https://aniapi.v300.eu.org/ani-download.xml` | 200 | 直链域名 `proi.v300.eu.org` 返回 Cloudflare **"Just a moment..."** JS 挑战页（403），程序化请求过不去 | ❌ 默认禁用 |
  | `https://aniapi.td.ee/ani-download.xml` | 200 | 直链域名 `ani.td.ee` 返回 **"This website has been temporarily rate limited"**，等待重试仍限流 | ❌ 默认禁用 |
  | `http://open.ani.rip/ani-download.xml` | 403（Cloudflare 拦截） | — | ❌ 默认禁用 |
  | `https://openani.an-i.workers.dev/ani-download.xml` | 429（限流） | — | ❌ 默认禁用 |

  被禁用的源都保留在默认列表里（行首 `#`），状态恢复后手动去掉 `#` 即可重新启用。

  **踩坑记录**：官方 RSS（`api.ani.rip`）的条目直链域名是裸的 `resources.ani.rip`，而各能用的社区镜像早就把这个域名替换成了自己的反代域名。所以务必把实测确认可直连的镜像排在前面，官方源放最后只作兜底。

  **另一个踩坑**：`touch_strm_file` 早期版本曾把 RSS link 里的 `?d=mp4` 参数改写成 `.mp4` 后缀，实测这个改写会导致目标反代站点路由不到资源（404）。已经改回直接写 RSS 原始 link，不做任何后缀改写。

### 数据源失效了，本地已经生成的一批 strm 怎么办

不用删了重新生成。插件页面勾选「修复本地失效链接」，立即运行一次：

1. **标题精确匹配**：strm 文件名（去掉 `.strm`）就是当初 RSS 的 `title`。如果这一集还在当前 RSS 的滚动窗口内（RSS 只保留近期约 50~60 条），直接换成当前源给出的最新直链，最准确。
2. **路径迁移兜底**：标题不在当前 RSS 窗口内的老集数（比如失效前很久生成的 strm）——实测发现 ANi 各镜像的直链结构里，`{季度}/{文件名}?d=mp4` 这一段在所有镜像之间是完全一致的（比如 `2026-7/[ANi]...mp4?d=mp4`），只有域名前缀不同（有的镜像多一段 `resources.ani.rip/`，比如 `td.ee` 就没有）。所以从旧链接里提取出这一段，换上当前数据源列表里排第一、且成功抓到内容的源的域名前缀，拼出候选新链接；**写入前会用 Range 请求实际探测一次确认能连通才覆盖**，探测不通过的保留原文件不动，不会瞎改出一堆死链接。
3. 完全无法从旧内容里识别出季度路径的（比如根本不是 ANi 家族的链接），原样跳过不动。

跑完在日志里看统计（精确匹配更新/路径迁移成功/迁移失败保留原文件/无法识别）。

**这个功能开发时联调踩过一个坑**：探测候选链接用的 `get_res(url, headers={"Range": "bytes=0-0"})` 一开始直接传了个只含 `Range` 的 `headers` 字典——MoviePilot 的 `RequestUtils.get_res` 对显式传入的 `headers` 是**整体替换**，不是合并，导致探测请求丢了默认的 `User-Agent`，裸请求直接被 Cloudflare 当可疑流量拦成 403，好端端能连通的链接被误判成"不可达"。改成先用 `update_headers()` 把 Range 头合并进去再请求，才是对的。

### 探测数据源健康度 / 按来源查看本地strm分布 / 一键切换到指定来源

三个能力对应插件页面两个开关一个下拉：

- **「探测各数据源播放健康度」**：勾选立即运行一次，对每个已启用的数据源各抓一条样本，判断 RSS 通不通、视频直链域名真的连不连得上（不是只看 RSS 状态码），顺带扫一遍本地 strm 按当前指向的域名分类计数。结果存起来，不在页面里实时刷新。
- **详情页**（插件卡片的"详情"/查看页，不是配置表单）：展示上面探测的结果——每个数据源的健康状态（可播放/RSS通但连不上视频/不可用）、本地 strm 按域名的数量分布和占比。数据来自最近一次探测缓存，不是实时扫描；要看最新状态就重新勾一次探测开关。
- **「一键切换到指定来源」**（配合"一键切换到指定来源"下拉）：选一个数据源、勾开关、立即运行一次，不管本地 strm 现在能不能播，强制全部统一改写成这个源——逻辑复用"修复本地失效链接"那套标题匹配+路径迁移，只是参照源从"当前排序第一个可用的"变成"用户指定的这一个"。用于主动决定"以后全都走这个镜像"，而不是等失效了才被动修。

**为什么详情页是"缓存快照"不是"实时仪表盘"**：MoviePilot 插件的 `get_page()` 和 `get_form()` 一样是静态 Vuetify 组件树，每次打开页面重新渲染，但页面内部按钮没有内置"点击调 API 刷新"的机制（那需要走完全独立的 Vue 自定义组件打包，`get_render_mode` 才能声明为 `vue` 模式）。所以探测这个"动作"和详情页这个"展示"是分开的两步：先勾开关探测，再打开详情页看结果，不是点一个按钮就地刷新。

## 注意事项

**已解决**  ~~**已定位问题 疑似ffprobe命令读取网络视频的媒体信息时，给容器设定的代理，命令执行不生效**~~
> /bin/ffprobe -i "https://resources.ani.rip/2023-10/[ANi] 葬送的芙莉蓮 - 02 [1080P][Baha][WEB-DL][AAC AVC][CHT]
> .mp4?d=true" -threads 0 -v info -print_format json -show_streams -show_chapters -show_format -show_data

**emby容器代理设置**

❗ ❗ ❗ **环境变量必须多设置一条，键为小写的http_proxy的代理**

[ffprobe源码](https://github.com/FFmpeg/FFmpeg/blob/master/libavformat/http.c#L218C48-L218C48) 使用getenv_utf8("
http_proxy") 对大小写敏感。

emby docker-compose env

```yaml
- 'http_proxy=http://127.0.0.1:7890'
- 'HTTP_PROXY=http://127.0.0.1:7890'
- 'HTTPS_PROXY=http://127.0.0.1:7890'
```
另外clash 这两个域名记得设置代理规则
```
resources.ani.rip
aniopen.an-i.workers.dev
```

## Todo:

- [x] ~~网页、fileball 无法播放的问题，看看能不能解决，或者有无更好的源代替~~。
- [x] 更新获取最新方法，避免跨季度番剧漏抓
- [x] 排查是否存在bug，优化使用
