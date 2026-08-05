"""B站 数据采集 CLI（Sprint 1 验证入口）。

示例：
  uv run python scripts/crawl_cli.py --task hot_board --limit 5
  uv run python scripts/crawl_cli.py --task search --target 机器学习 --limit 5 --date today
  uv run python scripts/crawl_cli.py --task user_homepage --target 2 --limit 5   # 需 BILI_COOKIE
  uv run python scripts/crawl_cli.py --task hot_board --limit 1 --download
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime

from vidagent.tools.crawler import search_and_fetch_videos
from vidagent.tools.downloader import download_video
from vidagent.utils import storage
from vidagent.utils.audio import extract_audio
from vidagent.utils.logging import setup_logging


def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"


async def run(args) -> list[dict]:
    items = await search_and_fetch_videos(
        platform=args.platform,
        task_type=args.task,
        target_id=args.target,
        date_filter=args.date,
        limit=args.limit,
    )

    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return items
    if not items:
        print("（无结果）")
        return items

    print(f"\n=== {args.platform} / {args.task} —— 共 {len(items)} 条 ===")
    for i, it in enumerate(items, 1):
        print(f"\n[{i}] {it['title']}")
        print(
            f"    UP: {it['author']}  | 播放: {it['view_count']}  "
            f"| 时长: {it.get('duration_text', '-')}  | 发布: {fmt_ts(it['publish_time'])}"
        )
        print(f"    {it['video_url']}")
        if it["desc"] and it["desc"] != "-":
            print(f"    简介: {it['desc'][:80]}")

    if args.download:
        print("\n--- 开始下载 + 抽音 ---")
        for i, it in enumerate(items, 1):
            d = download_video(it["video_url"], it["video_id"])
            if d["status"] != "success":
                print(f"  [{i}] 下载失败: {d.get('error')}")
                continue
            try:
                mp3 = extract_audio(d["local_path"])
                print(f"  [{i}] OK  mp4={d['local_path']}  mp3={mp3}")
            except Exception as e:
                print(f"  [{i}] 抽音失败（可走降级总结）: {e}")
    return items


def main() -> None:
    ap = argparse.ArgumentParser(description="VidAgent 采集 CLI（Sprint 1）")
    ap.add_argument("--platform", default="bilibili")
    ap.add_argument(
        "--task", default="hot_board", choices=["hot_board", "search", "user_homepage"]
    )
    ap.add_argument("--target", default=None, help="搜索关键词 或 用户 UID/mid")
    ap.add_argument("--date", default=None, choices=["today"], help="时间过滤")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--download", action="store_true", help="下载视频并抽取音频")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出元数据")
    args = ap.parse_args()

    setup_logging()
    storage.cleanup_old_files()  # 任务开始前清理（文档 §5.3）
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
