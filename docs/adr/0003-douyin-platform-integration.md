# ADR-0003: 抖音平台接入方案调研

**日期**: 2026-08-10
**状态**: 已调研，待实施计划确认

## 背景

VidAgent 已完成 YouTube 接入（Sprint 4a），现规划 Sprint 4b：抖音平台。抖音是短视频平台，与 B站/YouTube 存在根本差异：
- 无公开搜索 API（需登录态 + X-Bogus 签名）
- 强反爬机制（数据中心 IP 被封、浏览器指纹检测）
- 短视频元数据模型不同（无"章节"概念，时长通常 < 60s）
- 内容以竖屏为主，多模态总结策略需调整

## 主流开源方案

### 方案 1: f2（Johnserf-Seed/f2）

**仓库**: https://github.com/Johnserf-Seed/f2
**Stars**: ~2.5K | **许可证**: Apache 2.0 | **版本**: v0.0.1.7-pw2

| 能力 | 状态 | 说明 |
|------|------|------|
| 单个视频下载 | ✅ | 无水印，CLI + API |
| 用户主页批量下载 | ✅ | 一键抓取全部作品 |
| 直播录制 + 弹幕 | ✅ | 实时录制 + WSS 弹幕转发 |
| 用户信息查询 | ✅ | 关注/粉丝列表 |
| X-Bogus 签名 | ✅ | v0.0.1.7 已开源完整 ABogus 算法 |
| ID 提取工具 | ✅ | 用户 ID / 作品 ID / 直播间 ID |
| **搜索** | ❌ | 仅 TikTok 支持，抖音不支持 |
| **热门/热榜** | ❌ | 无此功能 |

**安装**: `pip install f2`（已在 pyproject.toml 的 `[project.optional-dependencies] f2` 中声明）

**Cookie 需求**: 公开内容（单个作品、主页作品）无需登录；点赞/收藏等私密内容需要 Cookie。

**优点**:
- 轻量（纯 Python + httpx，无需浏览器）
- 签名算法已开源，无需逆向
- pip 安装即用，CLI 方便调试
- 与 VidAgent 现有 `platforms/` 协议天然契合

**缺点**:
- **不支持搜索** — 这是最大短板
- **不支持热榜** — 但抖音有公开热榜 API（见下方）
- 接口可能随抖音更新而失效（需持续维护）

### 方案 2: MediaCrawler（Anl-cyber/MediaCrawler）★★★★★

**仓库**: https://github.com/Anl-cyber/MediaCrawler
**Stars**: ~40K+ | **许可证**: 开源（需确认） | **维护**: 持续至 2026.3

#### 抖音能力矩阵

| 能力 | 状态 | 实现方式 |
|------|------|---------|
| **关键词搜索** | ✅ **核心能力** | `search_info_by_keyword()` → 抖音 Web 搜索 API + a_bogus 签名 |
| 创作者主页 | ✅ | `get_all_user_aweme_posts()` → 自动翻页 |
| 无水印下载 | ✅ | Playwright 提取下载链接 |
| 评论采集 | ✅ | `batch_get_note_comments()` |
| 多平台 | ✅ | 抖音/小红书/快手/B站/微博/贴吧/知乎，共享抽象层 |

#### 搜索实现原理

```
用户真实 Chrome (CDP :9222)
    │
    ▼
Playwright connect_over_cdp()     ← 复用登录态，无 webdriver 特征
    │
    ▼
page.evaluate("window._webmsxyw(url, data)")  ← 浏览器内执行抖音签名 JS
    │
    ▼
get_a_bogus(uri, query, data, ua, page)  → 生成合法签名的 HTTP 请求
    │
    ▼
抖音 Web 搜索 API 返回结果
```

关键代码路径（`media_platform/douyin/`）：
```
client.py   → DouYinClient.search_info_by_keyword()   搜索 API
core.py     → DouYinCrawler                            主爬虫逻辑
login.py    → DouYinLogin (qrcode/phone/cookie)        登录认证
field.py    → SearchChannelType/SearchSortType         搜索参数枚举
```

**搜索参数**（完整）：
```python
search_info_by_keyword(
    keyword: str,                              # 搜索词
    offset: int = 0,                           # 分页偏移
    search_channel: SearchChannelType,         # 搜索渠道
    sort_type: SearchSortType,                 # 排序 (综合/最新/最多点赞)
    publish_time: PublishTimeType,             # 发布时间筛选
    search_id: str = "",                       # 搜索 ID
)
```

#### 风控对抗

| 层级 | 机制 | 效果 |
|------|------|------|
| 架构层 | CDP 连接真实 Chrome，非 webdriver | 绕过 `navigator.webdriver=true` 检测 |
| 会话层 | 复用浏览器登录态（Cookie + LocalStorage） | 无需每次扫码，Session ~7 天 |
| 签名层 | `page.evaluate()` 调用抖音原生 JS 签名函数 | 无需逆向，签名永不过期 |
| 网络层 | 动态代理 IP 池（商业 API → 验证 → LRU 调度） | 防止 IP 封禁 |
| 行为层 | 随机间隔、模拟滑动、缓动轨迹 | 绕过行为检测 |
| 验证码 | 滑块验证处理（`move_step=3`, `slider_level="hard"`） | 自动过滑块 |

#### 系统要求

| 依赖 | 说明 |
|------|------|
| Python ≥ 3.10 | 项目语言 |
| Playwright | `pip install playwright && playwright install chromium` |
| Chrome 浏览器 | 本机已安装的真实 Chrome（非 headless） |
| CDP 端口 9222 | 启动 Chrome 时加 `--remote-debugging-port=9222` |
| Node.js ≥ 16 | Playwright 依赖 |

#### 运行方式

```bash
# 1. 启动 Chrome 调试模式
google-chrome --remote-debugging-port=9222

# 2. 扫码登录抖音（首次，Session 缓存 7 天）
uv run python main.py --platform douyin --lt qrcode --type search --keyword "关键词"

# 3. 后续复用登录态，无需再次扫码
uv run python main.py --platform douyin --lt cookie --type search --keyword "关键词"
```

#### 与 VidAgent 集成的可行性

| 维度 | 评估 |
|------|------|
| 搜索能力 | ✅ 完整支持，参数丰富 |
| 本机调试 | ✅ 用户本机有 Chrome，可开 CDP |
| 服务器部署 | ❌ 需要 Chrome + 桌面环境（但用户说本机优先） |
| 稳定性 | ⚠️ 依赖抖音接口变化，需跟随 MediaCrawler 更新 |
| 代码复用 | ⚠️ 需提取 DouYinClient 核心逻辑，避免引入整个 MediaCrawler 项目 |
| 维护成本 | ⭐⭐ 中等（MediaCrawler 社区活跃，接口更新有人跟） |

**结论：MediaCrawler 是目前唯一能补齐抖音搜索的成熟开源方案。**

### 方案 3: 第三方 API 服务

| 服务 | 能力 | 价格 |
|------|------|------|
| [TikHub API](https://github.com/TikHub/TikHub-API-Python-SDK) | 400+ 抖音端点（搜索/热榜/详情/评论/直播） | 订阅制 |
| [SocialDataX](https://github.com/DevinChen2014/douyin-mcp) | MCP 协议，支持 Claude 直接调用；搜索/热榜/创作者/评论 | $4.99/1K items |
| [Apify Douyin Scraper](https://apify.com/bovi/douyin-scraper) | hot_search（免费无登录）/ search / user / comments | 按量计费 |

**优点**: 开箱即用、无维护成本、IP 代理内置
**缺点**: 有成本、外部依赖、数据出境风险

### 方案 4: 抖音公开热榜 API（无需登录）

```
GET https://www.douyin.com/aweme/v1/web/hot/search/list/
```

返回约 50 条实时热搜词，含热度值、视频数、封面图。**无需登录、无需 API Key**。这是少数不需要反爬的抖音公开接口。

## 推荐方案：三件套 — F2 下载 + 公开 API 热榜 + MediaCrawler 搜索

综合分析，**三个能力分别由三个方案负责**，不存在单一方案全覆盖：

```
抖音 Platform 实现
├── 热榜      → 公开 API（免费，免登录，已验证 ✅）
├── 下载      → f2 DouyinHandler（免费，pip 安装，无水印）
├── 搜索      → MediaCrawler DouYinClient（需要本地 Chrome + CDP）
└── 创作者    → MediaCrawler（复用搜索链路的登录态）
```

### 能力对比：f2 vs MediaCrawler

| | f2 | MediaCrawler |
|------|-----|-------------|
| 安装 | `pip install f2` | 克隆项目 + Playwright + Chrome |
| 下载 | ✅ 主力 | ✅ 也行 |
| 热榜 | ❌ | ❌（热榜走公开 API） |
| 搜索 | ❌ | ✅ **唯一方案** |
| 创作者 | ✅ | ✅ |
| 签名机制 | X-Bogus（Python 实现） | a_bogus（浏览器内 JS 执行） |
| 登录需求 | 公开内容无需 | **搜索必须登录**（CDP 复用浏览器 Session） |
| 服务器部署 | ✅ headless 友好 | ❌ 需要 Chrome 图形环境 |
| 维护风险 | 签名算法需手动更新 | 签名由抖音自己维护（在浏览器内跑） |

### 具体实施

#### Step 1: 抖音热榜（最简单，先行）

使用公开 API，无需任何依赖：
```python
# GET https://www.douyin.com/aweme/v1/web/hot/search/list/
# 返回: {data: {word_list: [{word, hot_value, video_count, ...}]}}
```

这是一个**热搜词列表**，非直接视频列表。需从热搜词中选取关键词，再通过搜索获取视频——但搜索不可用时会成为死胡同。
**应对**：热榜本身作为信息源，Agent 可以直接基于热搜词回答"今天抖音热什么"，不需下载视频。

#### Step 2: 抖音下载（核心能力）

```python
from f2.apps.douyin.handler import DouyinHandler

# 单个视频下载
handler = DouyinHandler(cookie=cookie_str)
await handler.fetch_one_video(aweme_id)

# 用户主页批量下载
await handler.fetch_user_post_videos(user_id)
```

f2 使用异步 API（httpx + asyncio），与 VidAgent 现有架构兼容。

VidAgent 中集成：
- `DouyinPlatform.download()` → 封装 f2 `fetch_one_video()`
- 视频元数据通过 f2 的接口返回，映射到统一 `normalize()` schema

#### Step 3: 抖音搜索（最大挑战）

**问题**: f2 不支持抖音搜索；MediaCrawler 太重。

**选项**:
| 选项 | 说明 | 推荐度 |
|------|------|--------|
| A. SocialDataX API | 按月付费，MCP 协议，开箱即用 | ⭐⭐⭐ (最快) |
| B. TikHub API | 端点最全，400+ 接口 | ⭐⭐ |
| C. MediaCrawler Playwright | 免费但需桌面环境 | ⭐ (不适合 headless) |
| D. 放弃搜索 | 只做热榜+下载 | ⭐⭐ (最稳) |

**建议**: 先用选项 D（热榜 + 下载）跑通核心链路；搜索按需接入 SocialDataX。

#### Step 4: 创作者视频

f2 支持 `fetch_user_post_videos()` — 可直接用。需用户 ID（可从分享链接提取）。

### 元数据映射

抖音 → VidAgent 统一 Schema：

| 抖音字段 | 统一 Schema | 说明 |
|---------|------------|------|
| aweme_id | video_id | 作品 ID（19 位数字） |
| desc | title | 视频描述（抖音无独立标题） |
| desc | desc | 同上，截断 |
| create_time | publish_time | unix 时间戳 |
| duration | duration | 毫秒 → 秒 |
| duration | duration_text | 秒 → MM:SS |
| share_url | video_url | 分享链接 |
| "douyin" | platform | 硬编码 |
| author.nickname | author | 作者昵称 |
| author.uid | author_id | 作者 ID |
| statistics.digg_count | view_count | 点赞数（抖音无播放量公开字段） |

### 总结管线调整

抖音短视频（< 60s）与 B站/YouTube 长视频（5-30min）的总结策略不同：

| | B站/YouTube | 抖音 |
|------|------------|------|
| 帧抽取 | 4-16 帧均匀采样 | 2-4 帧（视频短，封面+中间帧即可） |
| 音频 | 完整音频送 Whisper/Omni | 已有背景音乐为主，语音信息少 |
| 章节 | 有意义 | **无意义**（短视频无章节概念，跳过） |
| 总结 | 结构化的核心观点+梳理 | 一句话概括 + 标签 |

建议：在 `extract_and_summarize` / `_run_one` 中根据 `platform` 参数自动调整策略。

## 实施优先级（更新）

| 优先级 | 功能 | 方案 | 复杂度 | 说明 |
|--------|------|------|--------|------|
| P0 | 公开热榜 API | 原生 HTTP | ⭐ | 已验证可用，直接封装 |
| P1 | F2 下载 + normalize | f2 库 | ⭐⭐ | pip 安装即用 |
| P1 | MediaCrawler 搜索集成 | 提取 DouYinClient | ⭐⭐⭐⭐ | 需要 Chrome CDP |
| P2 | 创作者主页 | MediaCrawler | ⭐⭐ | 复用搜索链路的登录态 |
| P3 | 短视频总结策略 | 逻辑调整 | ⭐ | platform=="douyin" 时跳章节+减帧 |

### P1 搜索集成的具体步骤

1. **验证 Chrome CDP 可用**：启动 Chrome `--remote-debugging-port=9222`，确认 MediaCrawler 能连接
2. **提取 DouYinClient 核心代码**：从 MediaCrawler 中提取 `client.py`（搜索）+ `login.py`（Cookie 管理），作为 VidAgent 的独立子模块，不引入整个 MediaCrawler 项目
3. **实现 DouyinPlatform.search()**：调用提取后的 DouYinClient，映射到统一 normalize() schema
4. **Cookie 管理**：首次扫码登录 → 缓存 Session → 后续自动复用（~7 天过期）

**核心提取的文件**（`vidagent/tools/platforms/douyin/`）：
```
douyin/
├── __init__.py        # DouyinPlatform 注册
├── client.py          # 提取自 MediaCrawler — 搜索 API + a_bogus 签名
├── login.py           # 提取自 MediaCrawler — Cookie 登录/缓存
├── normalize.py       # 抖音 → 统一 schema 映射
└── download.py        # f2 下载封装
```

## 参考资料

- [f2 GitHub](https://github.com/Johnserf-Seed/f2) — Apache 2.0，2.5K stars
- [f2 官方文档](https://f2.wiki/)
- [MediaCrawler GitHub](https://github.com/Anl-cyber/MediaCrawler)
- [抖音公开热榜 API](https://www.douyin.com/aweme/v1/web/hot/search/list/)
- [TikHub API Python SDK](https://github.com/TikHub/TikHub-API-Python-SDK)
- [SocialDataX Douyin MCP](https://github.com/DevinChen2014/douyin-mcp)
- [Apify Douyin Scraper](https://apify.com/bovi/douyin-scraper)
- [hots - 多平台热搜聚合](https://github.com/turbo-uid/hots)
