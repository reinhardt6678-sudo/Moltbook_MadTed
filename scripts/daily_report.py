"""每日战报生成（人设文档 §9）。

汇总当天的独白日志 + 战绩，交给 Claude 写成战报体 markdown，存到 reports/daily/。

用法：
    python scripts/daily_report.py               # 生成今天的
    python scripts/daily_report.py --date 2026-08-12
    python scripts/daily_report.py --no-llm      # 只输出原始统计，不调 API
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from brain import Brain  # noqa: E402
from memory import ANGLES, Memory  # noqa: E402

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
MONOLOGUE_DIR = ROOT / "reports" / "monologue"
DAILY_DIR = ROOT / "reports" / "daily"


def _read_monologues(day: str) -> list[dict]:
    path = MONOLOGUE_DIR / f"{day}.jsonl"
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries


def build_stats(day: str, mem: Memory) -> str:
    """把当天的原始数据整理成一段给 LLM 的输入。"""
    monologues = _read_monologues(day)
    battles = mem.battles_on(day)
    restraint = mem.restraint_on(day)

    deliberations = [m for m in monologues if m["kind"] == "deliberate"]
    follow_ups = [m for m in monologues if m["kind"] == "follow_up"]
    engaged = [m for m in deliberations if m.get("verdict") == "出手"]

    lines = [
        f"## 日期：{day}",
        "",
        "### 数据",
        f"- 雷达扫描后进入深度判断的帖子：{len(deliberations)} 条",
        f"- 实际出手（开新杠）：{len(engaged)} 条",
        f"- 追问轮次：{len(follow_ups)} 次",
        f"- 收尾的对线：{len(battles)} 场",
        f"- 忍住了：{restraint['count']} 条，理由分布 {restraint['reasons']}",
        f"- 当前杠力值：{mem.data['state']['gang_power']}（{mem.data['state']['rank']}）"
        f"，距下一级还差 {(mem.data['state']['next_rank_at'] or 0) - mem.data['state']['gang_power']}",
        "",
    ]

    if battles:
        lines.append("### 今日收尾的对线")
        for b in sorted(battles, key=lambda x: x["rounds"], reverse=True):
            angle_name = ANGLES.get(b["angle_used"], b["angle_used"])
            lines.append(
                f"- @{b['opponent']} | {b['rounds']}轮 | {b['outcome']} "
                f"| 用招：{angle_name} | {b['score_delta']:+d}分 | {b['note']}"
            )
        lines.append("")

    conceded = [m for m in follow_ups if m.get("conceded")]
    if conceded:
        lines.append("### 今日认输/翻车")
        for m in conceded:
            lines.append(f"- 对 @{m['opponent']}：{m['thinking'][:200]}")
        lines.append("")

    cold = [b for b in battles if b["outcome"] == "冷场"]
    if cold:
        lines.append("### 今日冷场")
        for b in cold:
            lines.append(f"- @{b['opponent']}（{b['topic_type']}）：{b['note']}")
        lines.append("")

    if engaged:
        lines.append("### 出手理由（内心独白摘录，写日报时可以引用）")
        for m in engaged[:5]:
            lines += [
                f"**《{m.get('scanned', '')}》**",
                f"- 第一反应：{m.get('first_reaction', '')}",
                f"- 为什么是这条：{m.get('why_this_one', '')}",
                f"- 用招：{ANGLES.get(m.get('angle', ''), m.get('angle', ''))} —— {m.get('angle_reason', '')}",
                "",
            ]

    skipped = [m for m in deliberations if m.get("verdict") != "出手"]
    if skipped:
        lines.append("### 划走的帖子（忍住了）")
        for m in skipped[:5]:
            lines.append(f"- {m.get('scanned', '')} → {m.get('why_this_one', '')}")
        lines.append("")

    angle_use = Counter(m.get("angle") for m in engaged if m.get("angle") != "none")
    if angle_use:
        lines.append("### 今日角度使用")
        for angle, n in angle_use.most_common():
            lines.append(f"- {ANGLES.get(angle, angle)}: {n} 次")
        lines.append("")

    sharp, blunt, banned = mem.angle_preference()
    if sharp or blunt or banned:
        lines.append("### 学习状态")
        if sharp:
            lines.append(f"- 利刃：{'、'.join(ANGLES.get(a, a) for a in sharp)}")
        if blunt:
            lines.append(f"- 钝刀：{'、'.join(ANGLES.get(a, a) for a in blunt)}")
        if banned:
            lines.append(f"- 本周禁用：{ANGLES.get(banned, banned)}（占比超 35%）")
        if mem.truce_list:
            lines.append(f"- 免战名单：{'、'.join('@' + n for n in mem.truce_list)}")
        if mem.worthy_rivals:
            lines.append(f"- 硬骨头名单：{'、'.join('@' + n for n in mem.worthy_rivals)}")

    return "\n".join(lines)


def generate(day: str, *, use_llm: bool = True, effort: str = "medium") -> Path:
    mem = Memory()
    stats = build_stats(day, mem)

    if use_llm:
        body = Brain(effort=effort).write_daily_report(stats)
    else:
        body = f"# MadTed 战报 · {day}\n\n（未调用 LLM，以下为原始统计）\n\n{stats}"

    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    path = DAILY_DIR / f"{day}.md"
    path.write_text(body, encoding="utf-8")
    log.info("战报已写入 %s", path)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 MadTed 每日战报")
    parser.add_argument("--date", default=date.today().isoformat(), help="YYYY-MM-DD")
    parser.add_argument("--no-llm", action="store_true", help="只输出原始统计，不调 Claude API")
    parser.add_argument("--effort", default="medium",
                        choices=["low", "medium", "high", "xhigh", "max"])
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    path = generate(args.date, use_llm=not args.no_llm, effort=args.effort)
    print(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
