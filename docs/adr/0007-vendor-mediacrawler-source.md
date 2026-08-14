# Vendor MediaCrawler 源码入仓（非外部依赖）

VidAgent 的抖音/小红书/快手三平台依赖外部 MediaCrawler（需自带 `.venv`，含 execjs/xhshow/tenacity）。
此前以 `~/Code/MediaCrawler` 外部路径 + `sys.path` 注入其 `.venv` 的方式接入，对开源新用户是隐形的
硬依赖——配错即裸 `ModuleNotFoundError`，且默认路径是开发机约定。决定**将 MediaCrawler 源码作为
仓库的一部分直接提交**（`vendor/MediaCrawler/`，排除 `.venv/docs/data/tests/webui/.git`），并将其
所需 Python 依赖精选收敛进 VidAgent 的 `[douyin]` extra——**彻底干掉 MediaCrawler 独立 `.venv`**，
使 `git clone + uv sync` 即得全部，服务于「非专业用户傻瓜式部署」的开源目标。

## 考虑的方案

- **Vendor 源码 + 依赖收敛（采纳）**：源码本身仅 ~10-15M（727M 体积几乎全在 `.venv`，不入库）。
  一次 clone + 一次 `uv sync` 全有，部署步骤最少。MediaCrawler《非商业学习许可 1.1》明确授予
  「为非商业学习目的使用、复制、修改、合并」——本项目完全非商用，vendoring 许可干净（保留其
  `LICENSE` + 归属）。
- **git submodule（拒绝）**：版本锁更纯，但 `git clone --recursive` 对小白是劝退点；且 MediaCrawler
  API 频繁变动，pin 维护负担大。
- **脚本 clone 外部（拒绝）**：多一条命令、多一个失败点；不如直接入仓省心。

## 后果

- MediaCrawler 成为**冻结的 fork**：不再随上游自动更新。但 VidAgent 本已与上游分叉（快手
  page-listen、小红书签名均为自定义实现），实际早已是冻结态，代价为零；需上游修复时手动 resync。
- MC 依赖收敛进 `[douyin]` extra 后须注意**版本兼容**：MC 钉 `fastapi==0.110.2` / `uvicorn==0.29.0`，
  与 VidAgent 的 `fastapi>=0.115` / `uvicorn>=0.30` 冲突——不能整份塞 MC 的 `requirements.txt`，
  须精选兼容子集。**实测修正**：MC 每个平台的 `__init__→core→store` 链在导入期强制加载大量「分析与数据库」
  依赖（opencv / matplotlib / jieba / wordcloud / sqlalchemy / motor / aiomysql / redis / aiosqlite / asyncmy 等，
  VidAgent 功能上不用但导入即触发）——静态追踪曾低估（误判 redis/aiosqlite/asyncmy 为惰性），最终清单以
  `pyproject.toml` 的 `[douyin]` extra 为准、由导入冒烟实测确定。这是「vendor 不改源码」的代价（即「wordcloud 税」），
  但被隔离在 `[douyin]` extra 内——只用 B站/YouTube 的用户（`[server]` extra）不受影响。真正可丢弃的仅
  fastapi/uvicorn/typer/pandas/parsel/requests/cryptography/alembic/pytest 等（MC 自身服务/CLI/其他平台/测试用）。
- `_cdp_browser.py` 的 `sys.path` 注入从「MC root + 其 `.venv` site-packages」简化为「仅 vendor root」；
  `chdir` 到 vendor root 保留（`libs/douyin.js` 的 cwd 依赖）。
- `vendor/MediaCrawler/LICENSE` 保留；根目录加 `NOTICE` 声明该子树沿用《非商业学习许可 1.1》，
  VidAgent 自有代码仍为 MIT。
