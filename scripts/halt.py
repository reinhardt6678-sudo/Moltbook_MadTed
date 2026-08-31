"""停机闸：踩到平台红线之后，把 MadTed 按住，等人来看一眼。

为什么要有这个东西——人设 §10.1 里"被 moderator 警告"是 -50 分，比任何一场
对线赢回来的都多，而 §6.1 那条注释说得更直白：连续顶格发是 spam 的典型特征，
Moltbook 的 moderator bot 是会封号的。也就是说，这是全项目**唯一**一个
"再跑一轮的期望收益是负的"的状态。其余所有异常（拉不到 feed、模型截断、
额度耗尽）都可以靠下一轮自愈，只有这个不行：被警告之后继续发，只会让情况变坏。

闸是**一个文件**，不是进程内的开关：cron 每小时拉起一个新进程，进程里的任何
标志位都跟着上一轮一起消失了，只有磁盘记得住"上次被警告过"。和 budget.py
用文件记额度是同一个理由。

解闸必须人工——`python scripts/halt.py --clear`。不设自动过期：自动解闸等于
把"人看过了吗"这个问题偷偷回答成"看过了"，而这正是需要人判断的那一次。

用法：
    python scripts/halt.py            # 看当前状态
    python scripts/halt.py --clear    # 人工解闸
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import force_utf8_stdio  # noqa: E402

log = logging.getLogger(__name__)

HALT_PATH = Path(__file__).resolve().parent.parent / "memory" / "halt.json"


def active(path: Path | str | None = None) -> dict | None:
    """当前是否处于停机状态，是的话返回闸的内容。

    文件读坏了也当成"停着"：这个闸的默认方向必须是拦住。读不出内容说明
    盘上的状态不可知，而不可知的时候继续发帖，正是这个闸要防的事。
    """
    path = Path(path or HALT_PATH)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.error("停机闸文件读不了（%s），按停机处理：%s", path, exc)
        return {"reason": f"闸文件损坏：{exc}", "raised_at": "", "warning_ids": []}
    return data if isinstance(data, dict) else {"reason": str(data)[:200]}


def raise_halt(
    reason: str,
    *,
    warning_ids: list[str] | None = None,
    path: Path | str | None = None,
    dry_run: bool = False,
) -> None:
    """落闸。

    `dry_run=True` 时只喊、不写盘——和 Memory / CommentBudget 同一个约定。
    这里不写也不会漏掉警告：通知在收件箱里不会因为我们空跑一次就消失，
    下一次真跑照样读得到，照样落闸。
    """
    payload = {
        "reason": reason,
        "raised_at": datetime.now(timezone.utc).isoformat(),
        "warning_ids": warning_ids or [],
    }
    if dry_run:
        log.error("[dry-run] 本该落停机闸：%s", reason)
        return
    path = Path(path or HALT_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.error("已落停机闸（%s）：%s", path, reason)


def clear(path: Path | str | None = None) -> bool:
    """解闸，返回之前是不是真的停着。"""
    path = Path(path or HALT_PATH)
    if not path.exists():
        return False
    path.unlink()
    return True


def describe(state: dict) -> str:
    ids = "、".join(state.get("warning_ids") or []) or "（无 id）"
    return (
        f"落闸时间：{state.get('raised_at') or '未知'}\n"
        f"原因：{state.get('reason') or '未记录'}\n"
        f"相关通知：{ids}"
    )


def main() -> None:
    force_utf8_stdio()
    parser = argparse.ArgumentParser(description="MadTed 的停机闸")
    parser.add_argument("--clear", action="store_true", help="人工解闸，之后 heartbeat 才会继续跑")
    args = parser.parse_args()

    if args.clear:
        if clear():
            print("已解闸。下一轮 heartbeat 会正常跑。")
        else:
            print("本来就没停机，什么都没做。")
        return

    state = active()
    if state is None:
        print("未停机。")
        return
    print("⛔ 已停机——heartbeat 不会发任何东西。\n")
    print(describe(state))
    print("\n确认处理完之后：python scripts/halt.py --clear")


if __name__ == "__main__":
    main()
