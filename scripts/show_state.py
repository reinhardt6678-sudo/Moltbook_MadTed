"""一屏看完 MadTed 当前状态：杠力值、进行中的对线、学到的东西。

为什么要这个脚本：记忆和讨论串都存成 UTF-8 的 JSON，而 Windows PowerShell
的 `type` / `Get-Content` 默认按系统 ANSI 代码页（简体中文机器上是 GBK）读，
中文会显示成乱码——文件没坏，是读的方式不对。用 Python 读就没这问题。

用法：
    python scripts/show_state.py            # 全部
    python scripts/show_state.py --threads  # 只看进行中的对线
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import heartbeat  # noqa: E402
from budget import CommentBudget  # noqa: E402
from config import force_utf8_stdio, load_dotenv  # noqa: E402
from memory import ANGLES, RANKS, Memory  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
THREADS_PATH = ROOT / "memory" / "active-threads.json"


def _angle_name(code: str) -> str:
    if not code or code == "none":
        return "无"
    return f"{code} {ANGLES.get(code, '未知招式')}"


def _angles(codes: list[str]) -> str:
    return "、".join(_angle_name(c) for c in codes) or "暂无数据"


def _load_threads() -> dict:
    if not THREADS_PATH.exists():
        return {}
    return json.loads(THREADS_PATH.read_text(encoding="utf-8"))


def show_rank(mem: Memory) -> None:
    state = mem.data["state"]
    power, rank = state["gang_power"], state["rank"]
    print(f"杠力值 {power} —— {rank}")

    nxt = next((floor for floor, _ in RANKS if floor > power), None)
    if nxt is not None:
        name = next(n for f, n in RANKS if f == nxt)
        print(f"距「{name}」还差 {nxt - power} 分")

    battles = mem.data["battles"]
    print(f"累计收尾的对线：{len(battles)} 场")
    if not battles:
        print("  （战绩只在讨论串收尾时才记——刚出手的还在进行中，不算数）")
    print()


def show_threads() -> None:
    threads = _load_threads()
    live = {k: v for k, v in threads.items() if not v.get("closed")}
    closed = len(threads) - len(live)

    print(f"进行中的对线：{len(live)} 个（已收尾 {closed} 个）")
    if not live:
        print("  （还没开杠，或者都收尾了）")
        print()
        return

    for post_id, t in live.items():
        print()
        print(f"  《{t['title']}》")
        print(f"    对手：@{t['opponent']}　已来回 {t['rounds']} 轮")
        print(f"    用过的角度：{_angles(t.get('used_angles', []))}")
        idle = t.get("idle_cycles", 0)
        if idle:
            quiet = heartbeat._quiet_hours(t)
            elapsed = f"，已冷 {quiet:.0f} 小时" if quiet is not None else ""
            print(
                f"    ⚠️ 连续 {idle} 个周期没等到回应{elapsed}"
                f"（要同时满 3 个周期且冷够 {heartbeat.cold_after_hours():.0f} 小时才判冷场）"
            )
        last = t["turns"][-1] if t.get("turns") else None
        if last:
            who = "我" if last["role"] == "self" else f"@{t['opponent']}"
            text = last["text"].replace("\n", " ")
            if len(text) > 100:
                text = text[:100] + "…"
            print(f"    最后一句（{who}）：{text}")
        print(f"    帖子 id：{post_id}")
    print()


def show_budget() -> None:
    """还能发几条评论。每小时一轮的时候这行比杠力值更常看。"""
    budget = CommentBudget()
    print(budget.summary())
    if budget.remaining > 0:
        print(f"  还能发 {budget.remaining} 条（出手 + 追问合计）")
    print()


def show_learning(mem: Memory) -> None:
    sharp, blunt, banned = mem.angle_preference()
    print("学到的东西")
    print(f"  利刃（有效率高）：{_angles(sharp)}")
    print(f"  钝刀（没人接）：　{_angles(blunt)}")
    print(f"  本周禁用：　　　　{_angle_name(banned) if banned else '无'}")
    print(f"  免战名单：　　　　{'、'.join(mem.truce_list) or '暂无'}")
    print(f"  硬骨头：　　　　　{'、'.join(mem.worthy_rivals) or '暂无'}")
    print(f"  低产话题：　　　　{'、'.join(mem.low_yield_topics) or '暂无'}")
    print()

    stats = mem.data["angle_stats"]
    if stats:
        print("各招使用情况")
        for code, s in sorted(stats.items(), key=lambda kv: -kv[1].get("used", 0)):
            used, eff = s.get("used", 0), s.get("effective", 0)
            rate = f"{eff / used:.0%}" if used else "—"
            print(f"  {_angle_name(code):<16} 用了 {used:>3} 次，有效 {eff:>3} 次（{rate}）")
        print()

    log = mem.data["restraint_log"]
    if log:
        latest = log[-1]
        reasons = "、".join(f"{k} {v}" for k, v in latest.get("reasons", {}).items())
        print(f"最近一次「忍住了」：{latest.get('date')} 共 {latest.get('count', 0)} 条")
        if reasons:
            print(f"  理由分布：{reasons}")
        print()


def main() -> int:
    force_utf8_stdio()
    parser = argparse.ArgumentParser(description="查看 MadTed 当前状态")
    parser.add_argument("--threads", action="store_true", help="只看进行中的对线")
    args = parser.parse_args()

    # 额度上限和冷场阈值都可以写在 .env 里，这里显示的数得和 heartbeat 实际用的一致
    load_dotenv()
    mem = Memory()

    print("MadTed 当前状态")
    print("=" * 60)

    if args.threads:
        show_threads()
        return 0

    show_rank(mem)
    show_budget()
    show_threads()
    show_learning(mem)
    return 0


if __name__ == "__main__":
    sys.exit(main())
