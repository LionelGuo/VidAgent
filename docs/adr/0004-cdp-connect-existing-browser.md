# ADR-0004: 抖音/小红书平台客户端通过 CDP 连接用户现有浏览器

**日期**: 2026-08-13
**状态**: 已接受 (accepted)

## 背景

VidAgent 的抖音/小红书能力复用 MediaCrawler 仓库的客户端（`DouYinClient` / `XiaoHongShuClient`）：
- 签名（a_bogus / x-s、x-t）与请求参数依赖浏览器状态：抖音的 msToken 取自页面 `localStorage["xmst"]`（每个请求前 `page.evaluate` 读取），小红书依赖浏览器 cookie 中的 `a1` 与登录态 `web_session`
- 未登录或指纹异常时平台风控直接拒绝请求（空响应 / "blocked" / 412）
- 因此「有登录态的浏览器页面」是这两个平台能力的硬性前提

早期实现（ADR-0003 落地时）在 WSL 中自行启动 Playwright Chromium 并轮询扫码登录，实践中链路不稳定：
- 自启浏览器无登录态 → 搜索被风控拒绝
- 页面失效时 `page.evaluate` 无超时挂起，导致下载请求卡死
- 每次启动需重新扫码（登录态持久化依赖 `browser_data/` 目录，路径受进程 CWD 影响）

用户实测 MediaCrawler 原生的 CDP 模式（Windows 打开 Chrome → WSL 中脚本连接）稳定可用。

## 决策

**平台客户端（抖音、小红书）通过 CDP 连接用户现有浏览器（Windows Chrome 的 `:9222` 调试端口），复用其登录态与页面环境，不自行启动浏览器。**

具体约束：

1. **连接方式**：复用 MediaCrawler `CDPBrowserManager.launch_and_connect`（`CDP_CONNECT_EXISTING=True`），Playwright `connect_over_cdp("ws://localhost:9222/devtools/browser")`；依赖 WSL2 localhost 转发到达 Windows 侧调试端口
2. **失败即报错**：CDP 端口不可达（等待 15s）→ 返回可操作的错误信息（提示开启远程调试 / 接受连接确认框），**不降级**到自启浏览器——无登录态的降级路径注定被风控拒绝
3. **登录态缺失**：在 CDP 页面上打开登录弹窗引导用户扫码（最多 120s），登录态由用户浏览器自身持久化，一次扫码长期有效
4. **串行化**：`asyncio.Lock` 包住所有 client 方法调用——MediaCrawler 按单爬虫设计（共享单 page，每请求 evaluate）
5. **绝不关闭用户浏览器**：不调用 `CDPBrowserManager.cleanup()` / `context.close()`，仅关闭 VidAgent 自己打开的标签页（`invalidate_page`）
6. **cwd 收敛**：MediaCrawler 唯一导入期 cwd 依赖（`douyin/help.py` 模块级编译 `libs/douyin.js`）通过「导入期 chdir 一次 + 立即切回」解决，运行时零 chdir，避免多线程竞态
7. **CDP 专用常驻事件循环**：Playwright 异步对象绑定创建时的事件循环；平台 `download()` 是同步入口（每次调用若用 `asyncio.run` 会新建临时循环），跨调用复用模块级 Playwright 单例会在已关闭的循环上调度回调（实测 `RuntimeError: Event loop is closed`）。因此 `_cdp_browser.py` 维护一个后台守护线程跑常驻事件循环，所有 Playwright 操作通过 `run_on_cdp_loop`（同步入口）/ `run_on_cdp_loop_async`（异步入口，`run_coroutine_threadsafe` + `wrap_future`）提交执行

## 替代方案

### 方案 A：自启持久化 Chromium + 扫码登录（原实现）
- 优点：无外部依赖，服务器环境可跑
- 拒绝原因：无登录态 → 风控拒绝；每次启动重新扫码；page 挂起风险；实践已证明不稳定

### 方案 B：纯 HTTP 请求 + 自行维护 cookie
- 拒绝原因：抖音 msToken 生命周期短且需要浏览器指纹环境；小红书 a1/web_session 依赖真实浏览器获取；维护成本高

### 方案 C：CDP 自动启动新 Chrome 进程（`CDP_CONNECT_EXISTING=False`）
- 拒绝原因：WSL 中 `detect_browser_paths` 只扫 Linux 路径，找不到 Windows Chrome；本地 Linux Chrome 无登录态，与方案 A 同病

## 影响

- **部署约束**：抖音/小红书能力仅在有 Windows Chrome + 远程调试开启的本地环境可用（即当前开发机拓扑）；远程服务器部署时这两个平台不可用（B站/YouTube 不受影响）
- **风险**：依赖 WSL2 localhost 转发与 Chrome 136+ 的 `ws://localhost:{port}/devtools/browser` 直连协议（MediaCrawler 已实现 `/json/version` 兜底）
- 文件：`src/vidagent/tools/platforms/{douyin,xiaohongshu}.py`、`src/vidagent/tools/platforms/_cdp_browser.py`
