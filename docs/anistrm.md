- [1. ANi-Strm插件](#MoviePilot-x-ANi-Strm)
    - [2023-10秋 刮削效果](#2023-10秋-刮削效果)
    - [注意事项](#注意事项)
    - [Todo](#Todo)
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
