"""清掉跟进阶段瞎掉那段时间留下的假冷场战绩。

背景：跟进阶段以前只靠通知端点发现回复，那个端点一挂（官方文档里根本没有
`/notifications`，正门是 `/home` 的 activity_on_your_posts），agent 就一条回复
都看不见，每个讨论串熬满 3 个周期被判冷场——网页上明明全是别人的回复。

管道已经修好了（heartbeat.py 现在直接轮询评论区），但记忆里那批假冷场还在，
而且已经按 §8.2 归了因：好角度进了「钝刀」，正常对手进了「免战名单」。
这个脚本把它们删掉，然后按剩下的战绩把派生状态整个重算一遍。

用法：
    python scripts/repair_memory.py                     # 只看会删什么，不动文件
    python scripts/repair_memory.py --apply             # 真的删（先备份）
    python scripts/repair_memory.py --before 2026-08-14 # 只删这天之前的
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from memory import Memory  # noqa: E402

# 自动判冷场时写死的备注，用它认出"这条是熬周期熬出来的"，
# 而不是误伤将来可能出现的、别的来源的冷场记录。
AUTO_COLD_NOTE = "连续多个周期零回应"


def _is_phantom_cold(battle: dict, before: str | None) -> bool:
    if battle.get("outcome") != "冷场":
        return False
    if AUTO_COLD_NOTE not in (battle.get("note") or ""):
        return False
    if before and battle.get("date", "") >= before:
        return False
    return True


def _snapshot(mem: Memory) -> dict:
    lists = mem.data["lists"]
    return {
        "杠力值": mem.data["state"]["gang_power"],
        "段位": mem.data["state"]["rank"],
        "战绩条数": len(mem.data["battles"]),
        "免战名单": list(lists["truce_list"]),
        "低产话题": list(lists["low_yield_topics"]),
        "钝刀": list(lists["blunt_angles"]),
        "利刃": list(lists["sharp_angles"]),
    }


def _print_diff(before: dict, after: dict) -> None:
    for key in before:
        old, new = before[key], after[key]
        if old == new:
            continue
        if isinstance(old, list):
            gone = [x for x in old if x not in new]
            added = [x for x in new if x not in old]
            parts = []
            if gone:
                parts.append(f"放出 {'、'.join(gone)}")
            if added:
                parts.append(f"新增 {'、'.join(added)}")
            print(f"  {key}：{'；'.join(parts)}")
        else:
            print(f"  {key}：{old} → {new}")


def main() -> int:
    parser = argparse.ArgumentParser(description="清掉假冷场战绩并重算记忆")
    parser.add_argument("--apply", action="store_true", help="真的写回文件（默认只预览）")
    parser.add_argument(
        "--before",
        default=None,
        metavar="YYYY-MM-DD",
        help="只删这个日期之前的冷场战绩。不给就删全部自动判定的冷场",
    )
    args = parser.parse_args()

    mem = Memory()
    if not mem.path.exists():
        print(f"没有记忆文件（{mem.path}），无事可做。")
        return 0

    before_state = _snapshot(mem)
    doomed = mem.drop_battles(lambda b: _is_phantom_cold(b, args.before))

    if not doomed:
        print("没有找到自动判定的冷场战绩，记忆是干净的。")
        return 0

    print(f"找到 {len(doomed)} 条自动判定的冷场战绩：\n")
    for battle in doomed[:10]:
        print(f"  {battle['date']}  @{battle['opponent']}  "
              f"角度 {battle['angle_used']}  《{battle.get('topic_type', '')}》")
    if len(doomed) > 10:
        print(f"  …… 另外 {len(doomed) - 10} 条")

    print("\n删掉并重算之后的变化：")
    _print_diff(before_state, _snapshot(mem))

    if not args.apply:
        print("\n（预览模式，文件没动。确认无误后加 --apply）")
        return 0

    backup = mem.path.with_suffix(".json.bak")
    shutil.copy2(mem.path, backup)
    mem.save()
    print(f"\n已写回 {mem.path}，原文件备份在 {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
