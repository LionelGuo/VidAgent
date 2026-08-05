"""Sprint 2 硬编码流水线：抓取 → 下载 → ASR → 总结（无 Agent，先跑通）。

这是 Sprint 3 引入 Agent 前的对照基线，也是「无 key 也能验证 ASR、有 key 即端到端」的入口。

用法：
  uv run python -m vidagent.pipeline --task hot_board --limit 1
  uv run python -m vidagent.pipeline --task search --target 人工智能 --limit 1
"""

from __future__ import annotations

import argparse
import asyncio

from vidagent.tools.crawler import search_and_fetch_videos
from vidagent.tools.downloader import download_video
from vidagent.tools.summarizer import extract_and_summarize
from vidagent.utils import storage
from vidagent.utils.logging import setup_logging
from vidagent.utils.timer import Timer

setup_logging()


async def run(platform: str, task: str, target: str | None, date: str | None, limit: int) -> None:
    storage.cleanup_old_files()
    items = await search_and_fetch_videos(platform, task, target, date, limit)
    if not items:
        print("未获取到视频。")
        return

    print(f"\n获取 {len(items)} 个视频，开始逐个「下载 → ASR → 总结」\n")
    for i, it in enumerate(items, 1):
        print(f"===== [{i}/{len(items)}] {it['title']} =====")
        with Timer(f"视频[{i}] 全流程(下载→总结)"):
            d = download_video(it["video_url"], it["video_id"])
            if d["status"] != "success":
                print("  ❌ 下载失败:", d.get("error"))
                continue
            try:
                summary = extract_and_summarize(d["local_path"], it)
                print("\n" + summary + "\n")
            except Exception as e:
                print("  ❌ 总结失败:", e)


def main() -> None:
    ap = argparse.ArgumentParser(description="VidAgent 硬编码流水线（Sprint 2）")
    ap.add_argument("--platform", default="bilibili")
    ap.add_argument("--task", default="hot_board", choices=["hot_board", "search", "user_homepage"])
    ap.add_argument("--target", default=None, help="搜索关键词 或 用户 UID/mid")
    ap.add_argument("--date", default=None, choices=["today"])
    ap.add_argument("--limit", type=int, default=1)
    args = ap.parse_args()
    asyncio.run(run(args.platform, args.task, args.target, args.date, args.limit))


if __name__ == "__main__":
    main()
