"""配置加载：把 .env 读进环境变量。

放在这里让所有入口脚本（heartbeat / daily_report / preflight）共用一份逻辑。

为什么不用 python-dotenv：只为读几行 KEY=VALUE 多装一个依赖不划算，
而且这样 Windows / cron / CI 三种环境的行为完全一致——都不需要先 source。

优先级：已存在的环境变量 > .env 文件。所以临时覆盖某个 key 直接在命令行
设环境变量即可，不用改文件。
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = ROOT / ".env"


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
