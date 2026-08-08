"""Profile Agno agent to find where time is spent."""
import asyncio, sys, time, logging
sys.path.insert(0, 'src')
logging.basicConfig(level=logging.ERROR)

from vidagent.agent import build_agent

async def profile():
    agent = build_agent()
    prompt = "写一篇800字的文章介绍人工智能发展历史"

    print("=== Profile: agent.arun() ===")
    t0 = time.perf_counter()
    events = []
    ttft = None
    first_content = None

    async for ev in agent.arun(prompt, stream=True, stream_events=True):
        now = time.perf_counter()
        if ttft is None:
            ttft = now - t0
        etype = type(ev).__name__
        content = getattr(ev, "content", "") or ""
        if content and first_content is None:
            first_content = now - t0
        events.append((now - t0, etype, len(content)))

    total = time.perf_counter() - t0

    # Analyze event timeline
    print(f"Total time: {total:.1f}s")
    print(f"First event (any): {ttft:.3f}s ({events[0][1] if events else 'N/A'})")
    print(f"First content event: {first_content:.3f}s" if first_content else "No content")
    print(f"Total events: {len(events)}")

    # Group by event type
    from collections import Counter
    types = Counter(e[1] for e in events)
    print(f"\nEvent types:")
    for t, c in types.most_common():
        print(f"  {t}: {c}")

    # Find gaps > 100ms between events
    print(f"\nGaps > 100ms:")
    for i in range(1, len(events)):
        gap = events[i][0] - events[i-1][0]
        if gap > 0.1:
            print(f"  {events[i-1][0]:.2f}s → {events[i][0]:.2f}s: {gap:.2f}s gap "
                  f"({events[i-1][1]} → {events[i][1]})")

    # Content stats
    content_events = [e for e in events if e[1] == "RunContentEvent"]
    total_chars = sum(e[2] for e in content_events)
    print(f"\nContent: {total_chars} chars in {len(content_events)} events")
    if content_events:
        print(f"Avg chars/event: {total_chars/len(content_events):.1f}")

asyncio.run(profile())
