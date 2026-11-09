# 多源 EPG 合并

把多个 XMLTV `.xml.gz` / `.xml` 节目单定时合并，通过 GitHub Pages 提供固定订阅地址。只处理节目单，不包含直播视频，也不能从节目单生成直播线路。

## 快速部署

1. 新建 **Public** GitHub 仓库，例如 `epg-merger`，默认分支使用 `main`。GitHub Free 的 Pages 需要公开仓库。
2. 解压项目，把项目目录内的文件按原路径上传到仓库根目录，**不要再套一层 `epg-merger` 文件夹**。
3. 编辑 `sources.txt`，每行填一个真实 EPG 地址。可混用 `.xml.gz` 和 `.xml`；排在上面的来源优先。默认文件没有真实来源，未填写时会运行失败。
4. 确认 `.github/workflows/update-epg.yml` 也已上传。如果网页上传漏掉隐藏目录，可点 **Add file → Create new file**，文件名填 `.github/workflows/update-epg.yml`，粘贴对应内容。
5. 到仓库 **Settings → Pages → Build and deployment → Source**，选择 **GitHub Actions**。
6. 到 **Actions → Update EPG → Run workflow**，选择 `main` 手动运行。若此前上传触发的运行失败，完成以上配置后重新运行即可。
7. 等待 `build`、`deploy` 都成功，打开 **Settings → Pages** 显示的网址，在其后追加 `epg.xml.gz` 或 `epg.xml`。自定义域名、账户主页仓库等情况下，以该页面显示的基础网址为准。

普通项目仓库的订阅地址示例（需要替换用户名和仓库名；部署成功前不能使用）：

```text
https://你的用户名.github.io/epg-merger/epg.xml.gz
https://你的用户名.github.io/epg-merger/epg.xml
```

优先使用 `.xml.gz` 节省流量；播放器不支持压缩 EPG 时用 `.xml`。把该网址填入播放器的 **EPG/节目单地址**。

`sources.txt` 示例：

```text
# 注释独占一行；下列域名只是占位示例，必须替换
https://example.com/a.xml.gz
https://example.org/b.xml.gz
https://example.net/c.xml
```

含参数的 URL 原样填入，不要在行尾追加注释。不要重复填写同一网址：来源隔离模式会把不同来源编号视为不同的频道空间；需要跨源去重时先配置频道映射。

## 合并规则

| 项目 | 默认行为 |
| --- | --- |
| 频道判断 | 新版工作流使用 `--namespace-channels`，未显式映射的 ID 加 `s1:`、`s2:` 等来源前缀，避免不同来源同 ID 混台 |
| 频道信息 | 相同 ID 保留第一个来源的信息，并补充后续来源的不同显示名称 |
| 节目去重 | 以“映射后的频道 ID + 换算为 UTC 的开始时间”作为唯一键 |
| 节目冲突 | 相同唯一键保留第一个来源的完整节目记录，不拼接字段 |
| 节目补充 | 后续来源中不同开始时间的节目继续加入 |
| 不同起始时间的重叠节目 | 继续保留；本项目不自动判断哪条排期正确 |
| 内容保留 | 保留入选节目的标题、简介、分类等子节点；不自动删减过期节目或限制未来天数 |
| 来源失败 | 新版工作流使用 `--allow-partial`，部分来源失败时发布成功来源，`status.json` 标记 `partial: true`；不会补回失败源的旧数据 |
| 所有来源失败 | 始终失败退出，不发布空节目单 |
| 记录兼容 | 清除时间中已确认的“正在播出”污染；跳过缺少必要字段或零时长的记录；省略非法或早于开始时间的可选结束时间，并记录统计 |

**来源顺序只解决相同开始时间的冲突。** 如果两个来源同一节目相差 5 分钟，两条都会保留；若需要“一个频道完全采用首选源，其他源只在该频道缺失时接管”，需要改用按整频道选择的策略。

默认要求每个来源至少有一条有效节目。单条坏记录不会再让整个源失败，修正和跳过数量会写入日志与 `status.json`。损坏 XML、超限文件或没有任何可用节目仍会判定整个源失败。文件不带频道定义时，会根据节目中的频道 ID 补齐最小频道记录。需要恢复原先的整源严格校验时使用 `--strict`。

## 不同频道 ID 如何统一

XMLTV 的 `<programme channel="...">` 必须与相应的 `<channel id="...">` 对应。直播 M3U 的 `tvg-id` 通常也应与 EPG ID 一致，具体匹配规则取决于播放器。**合并成功不等于所有直播频道都会自动匹配。**

新版工作流默认启用来源隔离。例如两个来源中原始 ID 都是 `2`，输出分别为 `s1:2` 和 `s2:2`，避免 CCTV2 与山东卫视被拼成一个频道。频道显示名称保持不变。依赖 ID 匹配的播放器需要使用新 ID；依赖名称匹配的播放器可能仍能匹配，具体取决于播放器。不同来源的同一频道也先保留为不同 ID，确认相同后再用映射合并。

例如：第一个源使用 `CCTV1`，第二个源使用 `cctv1.cn`，确认是同一频道后，把 `aliases.json` 改成：

```json
{
  "global": {
    "CCTV1": "CCTV1"
  },
  "sources": {
    "2": {
      "cctv1.cn": "CCTV1"
    }
  }
}
```

`"2"` 代表 `sources.txt` 中第二个有效网址，空行和注释不计数。调整来源顺序时也要调整映射编号。

全局映射写入 `global`；来源专用映射覆盖同名的全局规则。映射只应用一次，不沿着 A→B→C 递归转换。频道定义、节目频道引用及 `previously-shown` 中的频道引用会同步改写。

显式映射的目标 ID 不加来源前缀。上面的自映射 `CCTV1 → CCTV1` 让第一个源也输出统一的 `CCTV1`，与第二个源合并。如果你希望全部保留原始 ID，可去掉工作流中的 `--namespace-channels`，但需要自行确认来源间不存在“同 ID、不同频道”的冲突。

**源内部重复 ID 是另一类问题。** 如果一个源把 `C` 和 `Channel[V]` 都定义成同一个 ID，则它的节目记录已经无法仅凭 ID 可靠区分。日志会报告这类 ID/名称歧义；来源前缀不能修复源内部的错误，必须核查上游。不同名称也可能只是合法别名，因此警告只提示核查，不擅自拆分或猜测归属。

反过来，如果两个源碰巧用了同一个 ID，但实际代表不同频道，也必须用来源专用映射拆开，否则会被误合并。不要只凭“名字差不多”就映射。

## 定时更新与维护

工作流默认：

```yaml
schedule:
  - cron: '17 */6 * * *'
```

表示每天 UTC **00:17、06:17、12:17、18:17** 尝试更新。改成 `'17 */4 * * *'` 为每 4 小时一次。配置文件改动和手动运行也会触发更新。若默认分支不是 `main`，需修改工作流的 `push.branches`；定时工作流必须在默认分支中。

GitHub 定时任务可能延迟，高负载时甚至可能丢弃排队任务，不能当作准点调度服务。公开仓库若 60 天没有仓库活动，定时任务可能自动停用；需要检查 Actions 并重新启用。本项目生成文件直接部署到 Pages，不自动提交回仓库，也不额外制造保活提交。

生成物不会出现在仓库代码页面，因此不存在本项目的 `raw.githubusercontent.com/.../epg.xml.gz` 地址。订阅请使用 **Pages 地址**。输出通过 Pages 部署，避免每次更新都把大型节目单写入 Git 历史。

首页显示本次节目数量及 UTC 更新时间，并在部分来源失败时直接提示。`status.json` 提供来源编号、成功状态、数量和去重统计。工作流默认允许部分源失败，请检查 `partial` 和各来源的 `ok`。所有源失败，或移除 `--allow-partial` 后任一源失败时，不会发布，线上状态文件也仍是上次成功版本；**查本次失败请看 Actions 日志**。

## 本地运行

Python 3.10 及以上，工作流使用 Python 3.12。在项目根目录执行：

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python merge_epg.py --namespace-channels --allow-partial
```

结果写入 `public/`：

```text
epg.xml
epg.xml.gz
status.json
index.html
```

工作流默认容许部分源失败，对应命令：

```bash
python merge_epg.py --namespace-channels --allow-partial
```

这样会用本次成功的来源生成节目单，失败来源独有的频道和节目会缺失，**不会自动缓存补回失败源的旧数据**。如需“所有源都成功才发布”，从工作流中去掉 `--allow-partial`。

## 已知边界

### 失败日志排查

新版脚本会显示下载字节数、gzip/plain 类型、失败阶段及受控的具体原因。例如：

```text
Source 1: downloaded 12345 bytes (gzip)
Source 1: cleaned 0 timestamps; omitted 1 stop times; skipped 0 programmes and 0 channels
Source 1: warning: programme #25: omitted stop earlier than start
```

这说明下载成功，第 25 条节目的结束时间错误，已去掉可选结束时间并保留节目。节目序号按源文件中 `<programme>` 出现的顺序计数，与频道序号无关。若使用 `--strict`，同样的问题会导致该源失败。

| 日志关键字 | 含义及下一步 |
| --- | --- |
| `during download` | 检查 URL 是否为完整直链、是否被加上引号或行尾注释、是否允许 GitHub 运行器访问 |
| `HTTP 403` / `HTTP 404` | 服务器拒绝访问 / 地址不存在，核实来源访问限制或路径 |
| `exceeds size limit` | 超过代码中的下载或解压大小限制 |
| `root is not <tv>` | 内容不是普通 XMLTV，可能是网页、错误页或带有不支持的 XML 命名空间 |
| `XML is malformed at line` | XML 结构损坏或下载内容不完整，日志给出行列位置 |
| `missing or empty title` | 指定节目的标题缺失或为空；默认跳过这条记录 |
| `start` / `stop` 后接时间错误 | 对应字段缺失、格式不支持，或日期时间非法 |
| `zero or negative duration` | 节目结束时间等于或早于开始时间；默认跳过零时长记录，负时长则省略结束时间 |
| `no usable <programme> records` | 文档没有可用的节目记录 |

日志不打印来源 URL、原始节目内容或任意第三方异常消息。若所有源仍失败，`--allow-partial` 也无法生成节目单；需要先解决源内容或格式问题。

兼容处理只删除已观察到的“正在播出”时间污染，不随意剥离未知文字或猜测节目开始时间。结束时间错误时使用 XMLTV 允许的省略 `stop` 方式，播放器能否按后续节目推断结束时间取决于其实现，不保证保留源中原本想表达的时长。

已用用户提供的四个真实文件验证：`x.xml.gz`、`t.xml.gz`、`seven-days.xml.gz`、`t (1).xml.gz`。隔离来源后得到 636 个频道、186348 条节目，去除 3077 条同频道同开始时间的重复记录；清理 352 个时间字段，省略 130 个错误结束时间，跳过 50 条空标题或零时长节目。该统计是这批静态样本的结果，不代表实时来源每次更新的数量。

### 大小和格式

- 单源下载上限 64 MiB，单源解压后上限 256 MiB；内存占用会高于文件大小。面向个人中小规模聚合，大型全世界频道库应改用流式解析及数据库去重。
- 下载每源最多尝试 3 次，网络读取超时 45 秒；工作流构建任务最长 25 分钟。来源过多、过慢时要相应调整。
- 自动识别一层 gzip；下载不依赖 URL 后缀，拒绝 HTML 错误页和不完整 XML。实体扩展由 `defusedxml` 禁止。
- 节目时间支持 `YYYYMMDD`、`YYYYMMDDhh`、`YYYYMMDDhhmm`、`YYYYMMDDhhmmss`，配合 `+0800` 等数字时区，或 UTC/GMT/Z。缺少时区按 XMLTV 规范视为 UTC；上游实际是北京时间却省略时区时，需修正上游。CST/BST 等名称时区在本项目中拒绝解析，避免猜测。XMLTV 允许更低精度日期，但本项目的节目排期要求至少精确到日。
- 开始时间相同但由 `clumpidx` 区分的多个片段会按同一节目去重；需要这种 XMLTV 扩展时应调整唯一键。
- 默认保留源中全部节目，因此坏源即使 XML 格式正确，也可能提供过期或错误排期；请关注节目日期与来源质量。
- GitHub Pages 有容量、带宽和服务限制，且不保证所有网络环境可达；高流量公开 EPG 服务应使用适合的对象存储或服务器。
- 公开仓库中的 `sources.txt` 和 `aliases.json` 人人可见。不要填写私人令牌或账号密码。输出节目单本身也是公开的。日志和状态文件仅列来源编号，不输出源 URL。

## 参考规范和文档

- [XMLTV DTD](https://github.com/XMLTV/xmltv/blob/master/xmltv.dtd)
- [GitHub Pages 自定义 Actions 工作流](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)
- [GitHub Actions 定时事件](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [GitHub Pages 限制](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits)

本项目包已通过 25 项自动化测试，以及上传的四个真实节目单文件的完整合并验证。从发布 ZIP 独立解压后，还通过了真实本地 HTTP 下载、CLI 合并、输出引用和时间校验、HTTP 读取、失败保留旧结果与显式 CCTV1 跨源映射验证。

已读取公开仓库中配置的四个真实网址进行实时下载。验证时前三个源成功，第四个源返回 HTTP 200，但 gzip CRC 校验失败；它与先前上传的有效静态文件不是同一份响应。新版工作流因此增加 `--allow-partial`，跳过损坏源并发布成功来源；不会绕过 gzip 完整性校验。实时源的内容和可用性会变化，统计以每次 Actions 日志和状态文件为准。尚未替你修改远程仓库或完成 GitHub Pages 线上部署，也未验证具体播放器的匹配和显示。

这次实时运行正常退出，生成 510 个频道 ID、148074 条节目，去除 2935 条重复记录，状态为 `partial: true`。上述四文件全部成功的 636/186348 统计与这次实时三源成功的 510/148074 统计属于两组不同输入，不能混为一谈。
