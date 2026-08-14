"""把当天的内心独白按人设 §7.1 的格式打出来。

独白存成 JSONL（一行一条），直接 cat 出来是一大坨转义文本没法看。
这个脚本按【扫到】【第一反应】…的排版还原，重点突出【为什么是这条】——
那栏是给主人看的核心，交代它为什么在几十条帖子里挑中这一条。

用法：
    python scripts/show_monologue.py              # 今天的
    python scripts/show_monologue.py --date 2026-08-12
    python scripts/show_monologue.py --only 出手   # 只看出手的，跳过划走
    python scripts/show_monologue.py --list        # 列出有记录的日期
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from memory import ANGLES  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MONOLOGUE_DIR = ROOT / "reports" / "monologue"


def _angle_name(code: str) -> str:
    """把角度编号翻成招式名，如 3.4 → 3.4 挖隐藏假设。"""
    if not code or code == "none":
        return "（未出手）"
    return f"{code} {ANGLES.get(code, '未知招式')}"


def _load(day: str) -> list[dict]:
    path = MONOLOGUE_DIR / f"{day}.jsonl"
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries


def _print_deliberate(e: dict) -> None:
    verdict = e.get("verdict", "?")
    mark = {"出手": "⚔️", "划走": "👋", "观望": "👀"}.get(verdict, "・")

    print(f"{mark} 【扫到】{e.get('scanned', '')}")
    print(f"   【第一反应】{e.get('first_reaction', '')}")
    print(f"   【杠点扫描】{e.get('weak_points', '')}")
    print(f"   【判定】{verdict}")
    print(f"   【为什么是这条】{e.get('why_this_one', '')}")

    if verdict == "出手":
        print(f"   【用的角度】{_angle_name(e.get('angle', ''))}"
              f" —— {e.get('angle_reason', '')}")
        print(f"   【预判】{e.get('prediction', '')}")
        lang = e.get("post_language", "")
        suffix = f"（{ {'en': '英文', 'zh': '中文'}.get(lang, lang) }）" if lang else ""
        print(f"   【发出去的话】{suffix}{e.get('reply', '')}")
    elif e.get("restraint_reason"):
        print(f"   【忍住的理由】{e['restraint_reason']}")

    score = e.get("radar_score")
    if score is not None:
        reasons = "；".join(e.get("radar_reasons", [])) or "无"
        print(f"   （L0 结构层 {score:.1f} 分：{reasons}）")

    triage = e.get("triage")
    if triage:
        print(
            f"   （L1 粗筛 {triage.get('hook_score', '?')}/10，"
            f"缺陷 {triage.get('defect', '?')}：{triage.get('note', '')}）"
        )


def _print_follow_up(e: dict) -> None:
    print(f"🔁 【追问】第 {e.get('round', '?')} 轮 —— @{e.get('opponent', '?')}")
    print(f"   【心里怎么想】{e.get('thinking', '')}")
    print(f"   【还有新角度吗】{'有' if e.get('has_new_angle') else '没有了，该收尾'}")
    if e.get("conceded"):
        print("   【认输】对方把我提的漏洞堵住了，认了")
    print(f"   【用的角度】{_angle_name(e.get('angle', ''))}")
    print(f"   【发出去的话】{e.get('reply', '')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="查看 MadTed 的内心独白")
    parser.add_argument("--date", default=date.today().isoformat(), help="YYYY-MM-DD")
    parser.add_argument(
        "--only",
        choices=["出手", "划走", "观望"],
        help="只看某一类判定",
    )
    parser.add_argument("--list", action="store_true", help="列出所有有记录的日期")
    args = parser.parse_args()

    if args.list:
        days = sorted(p.stem for p in MONOLOGUE_DIR.glob("*.jsonl"))
        if not days:
            print("还没有任何独白记录。跑一次 heartbeat 就有了。")
            return 0
        print("有记录的日期：")
        for day in days:
            print(f"  {day}（{len(_load(day))} 条）")
        return 0

    entries = _load(args.date)
    if not entries:
        print(f"{args.date} 没有独白记录。")
        print("跑一次 python scripts/heartbeat.py 就会生成。")
        return 0

    engaged = sum(1 for e in entries if e.get("verdict") == "出手")
    skipped = sum(1 for e in entries if e.get("verdict") == "划走")
    follow_ups = sum(1 for e in entries if e.get("kind") == "follow_up")

    print(f"MadTed 内心独白 · {args.date}")
    print("=" * 60)
    print(f"深挖 {len(entries) - follow_ups} 条 | 出手 {engaged} | 划走 {skipped} "
          f"| 追问 {follow_ups} 轮")
    print()

    for e in entries:
        if args.only and e.get("verdict") != args.only:
            continue
        if e.get("kind") == "follow_up":
            if args.only:
                continue
            _print_follow_up(e)
        else:
            _print_deliberate(e)
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
