"""入口脚本的两道保险：main 确实存在，输出重定向后 emoji 不炸。

这两条都是从同一类事故里长出来的——**只在无人值守那次运行里翻车**：

- `preflight.py` 曾经把整个 main 的函数体缩进进了上一个函数，
  `sys.exit(main())` 引用了一个不存在的名字。import 阶段完全正常，
  只有真的敲下命令才会 NameError，而这个脚本恰恰是"上线前查一遍"用的。
- 独白/状态脚本打 ⚔️🔁 这类字符。Windows 控制台走 UTF-16 API 没事，
  可 cron 里 `>> logs/heartbeat.log` 一重定向就退回 GBK，UnicodeEncodeError。

所以下面两组测试都不测"逻辑对不对"，只测"这脚本还能不能跑起来"。
"""

from __future__ import annotations

import runpy
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from config import force_utf8_stdio  # noqa: E402

# 每个能直接 `python scripts/xxx.py` 跑的入口
ENTRY_SCRIPTS = [
    "heartbeat",
    "daily_report",
    "preflight",
    "show_state",
    "show_monologue",
    "repair_memory",
]


# ---------- main 得真的存在 ----------


@pytest.mark.parametrize("name", ENTRY_SCRIPTS)
def test_entry_script_exposes_main(name):
    """`sys.exit(main())` 里的 main 必须是模块级的可调用对象。

    缩进错位会让 main 变成上一个函数的函数体，模块照样 import 得动，
    只有运行时才炸——这条就是拿来在 CI 里提前炸的。
    """
    module = __import__(name)
    main = getattr(module, "main", None)
    assert callable(main), f"{name}.py 没有模块级的 main()，命令行入口是坏的"


@pytest.mark.parametrize("name", ENTRY_SCRIPTS)
def test_entry_script_help_runs(name):
    """`--help` 能跑通，等于把 import 到 main 的整条路径走了一遍，且不发任何请求。"""
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / f"{name}.py"), "--help"],
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")


# ---------- 输出重定向后 emoji 不能炸 ----------


class _Recorder:
    """冒充一个 TextIOWrapper，只记下 reconfigure 收到了什么。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def reconfigure(self, **kwargs) -> None:
        self.calls.append(kwargs)


def test_force_utf8_stdio_reconfigures_both_streams(monkeypatch):
    out, err = _Recorder(), _Recorder()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    force_utf8_stdio()

    for stream in (out, err):
        assert stream.calls == [{"encoding": "utf-8", "errors": "replace"}]


def test_force_utf8_stdio_tolerates_streams_without_reconfigure(monkeypatch):
    """pytest / 某些 CI 会把 stdout 换成没有 reconfigure 的替身，不能因此崩。"""
    monkeypatch.setattr(sys, "stdout", object())
    monkeypatch.setattr(sys, "stderr", object())

    force_utf8_stdio()  # 不抛异常即可


@pytest.mark.parametrize("name", ["show_monologue", "show_state"])
def test_script_prints_emoji_to_a_pipe(name):
    """管道 = 非 tty，正是 cron 重定向的场景：GBK 机器上这里曾经必崩。"""
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / f"{name}.py"), "--help"],
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0
    assert b"UnicodeEncodeError" not in result.stderr


def test_emoji_reaches_a_redirected_stdout(tmp_path):
    """真正的端到端：子进程里调 force_utf8_stdio，再往重定向的 stdout 打 emoji。

    不 mock 编码，直接让子进程继承一个文件句柄——这样 locale 退化是真的发生了。
    """
    script = tmp_path / "emit.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "from config import force_utf8_stdio\n"
        "force_utf8_stdio()\n"
        "print('\\u2694\\ufe0f \\U0001f501 \\U0001f440 抬杠')\n",
        encoding="utf-8",
    )
    out = tmp_path / "out.log"
    with out.open("wb") as handle:
        result = subprocess.run(
            [sys.executable, str(script)], stdout=handle, stderr=subprocess.PIPE, timeout=60
        )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert out.read_text(encoding="utf-8").strip() == "⚔️ 🔁 👀 抬杠"


def test_lone_surrogate_does_not_crash(tmp_path):
    """feed 里真出现落单代理对时，UTF-8 也编不出来——必须被 replace 掉而不是崩。"""
    script = tmp_path / "emit_surrogate.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "from config import force_utf8_stdio\n"
        "force_utf8_stdio()\n"
        "print('before \\ud83d after')\n",
        encoding="utf-8",
    )
    out = tmp_path / "out.log"
    with out.open("wb") as handle:
        result = subprocess.run(
            [sys.executable, str(script)], stdout=handle, stderr=subprocess.PIPE, timeout=60
        )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    text = out.read_text(encoding="utf-8")
    assert "before" in text and "after" in text
