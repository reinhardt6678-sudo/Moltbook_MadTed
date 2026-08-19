"""清掉读不到回复那段时间留下的假战绩。

同一个毛病犯过两次，这个脚本收拾的是两次的残留：

1. **通知端点当唯一入口**：那个端点一挂（官方文档里根本没有 `/notifications`，
   正门是 `/home` 的 activity_on_your_posts），agent 一条回复都看不见，
   每个讨论串熬满 3 个周期被判「冷场」——网页上明明全是别人的回复。
2. **只读顶层评论**：评论区是有层级的，别人回我一定是挂在我那条评论下面
   （depth≥1），而 reader 只取顶层那批。于是真回复全在视野之外，
   楼里旁人的顶层发言反倒被当成"对方回我了"。这批串收尾时记的是
   「一轮即止 / 对方停止回应」——线上回头核对，对手其实回了 2~3 条。

两批记录都已经按 §8.2 归过因：好角度进了「钝刀」，正常对手进了「免战名单」。
光修管道不够，这些结论会一直留在记忆里继续影响选题。

用法：
    python scripts/repair_memory.py                     # 只看会删什么，不动文件
    python scripts/repair_memory.py --apply             # 真的删（先备份）
    python scripts/repair_memory.py --before 2026-08-14 # 只删这天之前的
    python scripts/repair_memory.py --rebuild --apply   # 不删战绩，只重算派生状态
    python scripts/repair_memory.py --prune-turns --apply  # 清掉对线记录里的旁人发言

`--before` 在管道修好之后要带上：「对方停止回应」从此是个有观测基础的判断，
新记录不该跟着这批老账一起删。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import force_utf8_stdio  # noqa: E402
from memory import Memory  # noqa: E402

# 自动判定时写死的备注，用它认出"这条是熬周期熬出来的"，
# 而不是误伤将来可能出现的、别的来源的记录。
AUTO_COLD_NOTE = "连续多个周期零回应"
BLIND_READER_NOTE = "对方停止回应，收尾"

THREADS_PATH = Path(__file__).resolve().parent.parent / "memory" / "active-threads.json"


def _is_phantom_cold(battle: dict, before: str | None) -> bool:
    if battle.get("outcome") != "冷场":
        return False
    if AUTO_COLD_NOTE not in (battle.get("note") or ""):
        return False
    if before and battle.get("date", "") >= before:
        return False
    return True


def _is_blind_reader_verdict(battle: dict, before: str | None) -> bool:
    """reader 只看得见顶层评论那段时间判出来的「对方停止回应」。

    当时那个判断没有观测基础：真回复挂在我的评论底下，reader 根本没取。
    留着它，"这个角度不管用"「这个对手不接招」这些结论就一直是错的。
    """
    if battle.get("outcome") != "一轮即止":
        return False
    if BLIND_READER_NOTE not in (battle.get("note") or ""):
        return False
    if before and battle.get("date", "") >= before:
        return False
    return True


def _is_phantom(battle: dict, before: str | None) -> bool:
    return _is_phantom_cold(battle, before) or _is_blind_reader_verdict(battle, before)


def prune_bystander_turns(threads: dict) -> list[tuple[str, int]]:
    """把进行中的讨论串里那些"旁人发言"从对线记录里清出去。

    那批 turns 是 reader 瞎的时候攒的：顶层评论不管谁说的、回给谁的，
    全被当成对方回我记了进来（单串见过 99 条）。留着它们，下一轮追问喂给
    大脑的 transcript 还是花的——它会以为这一楼的人都在跟自己对线。

    只动没关掉的串：已经收尾的留着当历史，不会再喂给大脑。
    seen_reply_ids 一起清掉——里面记的全是旁人的评论 id，而真回复
    当时压根没被读到过，清了不会漏掉任何已经答过的东西。
    """
    pruned: list[tuple[str, int]] = []
    for post_id, thread in threads.items():
        if thread.get("closed"):
            continue
        turns = thread.get("turns", [])
        kept = [t for t in turns if t.get("role") == "self"]
        if len(kept) == len(turns):
            continue
        pruned.append((post_id, len(turns) - len(kept)))
        thread["turns"] = kept
        thread["seen_reply_ids"] = []
    return pruned


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
        "记过的招": sorted(mem.data["angle_stats"]),
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


def _prune_turns(*, apply: bool, path: Path = THREADS_PATH) -> int:
    if not path.exists():
        print(f"没有讨论串文件（{path}），无事可做。")
        return 0
    threads = json.loads(path.read_text(encoding="utf-8"))
    pruned = prune_bystander_turns(threads)
    if not pruned:
        print("进行中的讨论串里没有旁人发言，对线记录是干净的。")
        return 0

    total = sum(count for _, count in pruned)
    print(f"{len(pruned)} 个进行中的讨论串里有 {total} 条旁人发言被当成了对线记录：")
    print()
    for post_id, count in pruned:
        print(f"  {post_id[:8]}  清掉 {count} 条")

    if not apply:
        print()
        print("（预览模式，文件没动。确认无误后加 --apply）")
        return 0
    backup = path.with_suffix(".json.bak")
    shutil.copy2(path, backup)
    path.write_text(
        json.dumps(threads, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print()
    print(f"已写回 {path}，原文件备份在 {backup}")
    return 0


def main() -> int:
    force_utf8_stdio()
    parser = argparse.ArgumentParser(description="清掉假冷场战绩并重算记忆")
    parser.add_argument("--apply", action="store_true", help="真的写回文件（默认只预览）")
    parser.add_argument(
        "--before",
        default=None,
        metavar="YYYY-MM-DD",
        help="只删这个日期之前的冷场战绩。不给就删全部自动判定的冷场",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="一条战绩都不删，只按现有战绩把派生状态重算一遍（改了统计口径之后用）",
    )
    parser.add_argument(
        "--prune-turns",
        action="store_true",
        help="清掉进行中讨论串的对线记录里那些旁人发言（reader 瞎的时候攒的）",
    )
    args = parser.parse_args()

    if args.prune_turns:
        return _prune_turns(apply=args.apply)

    mem = Memory()
    if not mem.path.exists():
        print(f"没有记忆文件（{mem.path}），无事可做。")
        return 0

    before_state = _snapshot(mem)

    if args.rebuild:
        # 统计口径改了之后，光有新代码不够：angle_stats 里的旧账是按老口径
        # 攒出来的，不重放一遍就会一直留着（比如 "3.10+3.4" 那个复合桶）。
        mem.rebuild_derived_state()
        print("按现有战绩重算派生状态：\n")
        after = _snapshot(mem)
        if before_state == after:
            print("  （没有变化，派生状态本来就是对的）")
        else:
            _print_diff(before_state, after)
        if not args.apply:
            print("\n（预览模式，文件没动。确认无误后加 --apply）")
            return 0
        backup = mem.path.with_suffix(".json.bak")
        shutil.copy2(mem.path, backup)
        mem.save()
        print(f"\n已写回 {mem.path}，原文件备份在 {backup}")
        return 0

    doomed = mem.drop_battles(lambda b: _is_phantom(b, args.before))

    if not doomed:
        print("没有找到自动判定的冷场战绩，记忆是干净的。")
        return 0

    print(f"找到 {len(doomed)} 条自动判定的冷场战绩：\n")
    for battle in doomed[:10]:
        print(f"  {battle['date']}  {battle['outcome']}  @{battle['opponent']}  "
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
