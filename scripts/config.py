"""配置加载：把 .env 读进环境变量，顺带把 stdio 钉成 UTF-8。

放在这里让所有入口脚本（heartbeat / daily_report / preflight）共用一份逻辑。

为什么不用 python-dotenv：只为读几行 KEY=VALUE 多装一个依赖不划算，
而且这样 Windows / cron / CI 三种环境的行为完全一致——都不需要先 source。

优先级：已存在的环境变量 > .env 文件。所以临时覆盖某个 key 直接在命令行
设环境变量即可，不用改文件。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = ROOT / ".env"


def force_utf8_stdio() -> None:
    """把 stdout / stderr 钉死成 UTF-8，别让一个 emoji 打断整个脚本。

    Windows 上 print 到真实控制台走的是 UTF-16 控制台 API，emoji 没问题；
    可一旦输出被重定向到文件或管道——cron 里的 `>> logs/heartbeat.log` 正是
    这种情况——Python 就退回 locale 编码（简中机器上是 GBK），打印 ⚔️ 这类
    字符直接抛 UnicodeEncodeError。Linux 上 cron 不带 locale 是同一个坑，
    退回 ASCII。**所以这个 bug 只在无人值守的那次运行里出现**，手动跑一切正常。

    errors="replace" 不是白写的：feed 里的正文可能带落单的代理对（unpaired
    surrogate），那玩意儿连 UTF-8 都编不出来，遇上就换成占位符，别再崩一次。

    和 load_dotenv 一样：每个入口脚本开头调一次即可。
    """
    for stream in (sys.stdout, sys.stderr):
        # pytest 会把 stdout 换成没有 reconfigure 的替身，照顾一下
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def load_dotenv(path: Path | str = DEFAULT_ENV_PATH) -> set[str] | None:
    """把 .env 里的键值注入 os.environ，返回本次真正注入的变量名。

    已存在的环境变量不会被覆盖。找不到文件时返回 None（不是错误——
    用户完全可以只用环境变量，不建 .env）。
    """
    path = Path(path)
    if not path.exists():
        return None

    injected: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):  # 允许直接粘 shell 风格的行
            line = line[len("export "):]
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            injected.add(key)
    return injected
