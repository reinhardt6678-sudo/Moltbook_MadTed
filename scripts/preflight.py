"""上线前自检（部署 doctor）。

把上线前能出错的地方一次性查完，出错时直接告诉你改哪个文件：
环境与依赖、密钥、目录权限、密钥有没有手滑进版本库，
以及最容易出问题的两个外部 API——Anthropic 和 Moltbook 的端点路径。

⚠️ Moltbook 的端点路径整理自第三方资料（见 moltbook_client.py 顶部说明），
本脚本的第 8/9 项就是专门用来核对它们和官方文档对不对得上的。

用法：
    python scripts/preflight.py              # 全套检查
    python scripts/preflight.py --skip-api   # 只查本地，不发任何网络请求

退出码 0 表示可以上线；1 表示有必须先修的问题（FAIL）。
WARN 不阻塞上线，但值得看一眼。
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import load_dotenv  # noqa: E402

# 依赖的最低版本，和 requirements.txt 保持一致
REQUIRED_PACKAGES = [
    ("anthropic", (0, 116, 0)),
    ("requests", (2, 31, 0)),
    ("pydantic", (2, 0)),
]

# 运行时要写的目录（cron 里重定向日志的 logs/ 最容易漏建）
RUNTIME_DIRS = ["memory", "reports/monologue", "reports/daily", "logs"]

# radar 打分时会去读的字段，用来核对 feed 返回的结构对不对得上
FEED_FIELDS = {
    "id": ("id",),
    "标题": ("title",),
    "正文": ("content", "body", "text"),
    "作者": ("author",),
    "社区": ("submolt_name", "submolt", "community"),
    "赞数": ("upvotes", "score", "karma", "likes"),
    "评论数": ("comment_count", "comments_count", "num_comments", "replies"),
}


class Report:
    """收集检查结果，最后统一打印。"""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []
        self._color = sys.stdout.isatty()

    def _add(self, level: str, name: str, detail: str, fix: str = "") -> None:
        self.rows.append((level, name, detail, fix))

    def ok(self, name: str, detail: str = "") -> None:
        self._add("PASS", name, detail)

    def warn(self, name: str, detail: str, fix: str = "") -> None:
        self._add("WARN", name, detail, fix)

    def fail(self, name: str, detail: str, fix: str = "") -> None:
        self._add("FAIL", name, detail, fix)

    def _paint(self, level: str) -> str:
        if not self._color:
            return level
        code = {"PASS": "32", "WARN": "33", "FAIL": "31"}[level]
        return f"\033[{code}m{level}\033[0m"

    @property
    def failures(self) -> int:
        return sum(1 for level, *_ in self.rows if level == "FAIL")

    @property
    def warnings(self) -> int:
        return sum(1 for level, *_ in self.rows if level == "WARN")

    def print_all(self) -> None:
        for level, name, detail, fix in self.rows:
            print(f"[{self._paint(level)}] {name}" + (f" —— {detail}" if detail else ""))
            if fix:
                for line in fix.splitlines():
                    print(f"         ↳ {line}")
        print()
        if self.failures:
            print(f"✗ {self.failures} 项必须先修，{self.warnings} 项提醒。修完再跑一次本脚本。")
        elif self.warnings:
            print(f"✓ 没有阻塞项，{self.warnings} 项提醒。可以进入空跑：")
            print("    python scripts/heartbeat.py --dry-run --max-new 2 --verbose")
        else:
            print("✓ 全部通过。可以进入空跑：")
            print("    python scripts/heartbeat.py --dry-run --max-new 2 --verbose")


# ---------- 工具函数 ----------


def mask(secret: str) -> str:
    """只露头尾各 4 位——自检输出可能被贴进 issue，绝不打印完整 key。

    Moltbook 的 key 统一以 moltbook_sk_ 开头，只看前缀分不出是哪一把，
    所以尾部也留 4 位，方便核对「加载的是不是我以为的那个 key」。
    """
    if len(secret) <= 12:
        return f"（共 {len(secret)} 位，太短，八成填错了）"
    return f"{secret[:4]}…{secret[-4:]}（共 {len(secret)} 位）"


def _ver_tuple(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in text.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


# ---------- 各项检查 ----------


def check_python(r: Report) -> None:
    v = sys.version_info
    current = f"{v.major}.{v.minor}.{v.micro}"
    if v >= (3, 10):
        r.ok("Python 版本", current)
    else:
        r.fail(
            "Python 版本",
            f"{current}，代码用了 3.10+ 的类型语法",
            "装一个 3.10 以上的 Python，再重建虚拟环境：\n"
            "python3.11 -m venv .venv && source .venv/bin/activate",
        )


def check_packages(r: Report) -> None:
    from importlib.metadata import PackageNotFoundError, version

    for name, minimum in REQUIRED_PACKAGES:
        want = ".".join(str(p) for p in minimum)
        try:
            found = version(name)
        except PackageNotFoundError:
            r.fail(f"依赖 {name}", "未安装", "pip install -r requirements.txt")
            continue
        if _ver_tuple(found) < minimum:
            r.fail(
                f"依赖 {name}",
                f"{found}，低于要求的 {want}",
                f"pip install -U '{name}>={want}'",
            )
        else:
            r.ok(f"依赖 {name}", found)


def check_env_keys(r: Report, injected: set[str] | None) -> None:
    if injected is None:
        r.warn(
            ".env 文件",
            "仓库根目录没有 .env",
            "cp .env.example .env 然后填入两个 key（也可以直接用环境变量）",
        )
        injected = set()
    else:
        r.ok(".env 文件", "已找到并加载")

    for key, hint in (
        ("MOLTBOOK_API_KEY", "注册 agent 后返回的 moltbook_sk_... key，见 docs/SETUP.md 第一步"),
        ("ANTHROPIC_API_KEY", "从 console.anthropic.com 拿，用来生成独白和回复"),
    ):
        value = os.environ.get(key, "").strip()
        if not value:
            r.fail(f"密钥 {key}", "未设置", hint)
        elif value.lower() in {"xxx", "todo", "changeme", "your_key_here"}:
            r.fail(f"密钥 {key}", f"还是占位值 {value!r}", hint)
        else:
            source = "来自 .env" if key in injected else "来自环境变量"
            r.ok(f"密钥 {key}", f"{mask(value)}，{source}")


def check_secret_not_tracked(r: Report) -> None:
    """.env 一旦被 git 跟踪就是密钥泄露，属于必须立刻修的问题。"""
    try:
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", ".env"],
            cwd=ROOT,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        r.warn("密钥未进版本库", "无法调用 git 检查", "手动确认 .env 没被提交")
        return

    if tracked.returncode == 0:
        r.fail(
            "密钥未进版本库",
            ".env 已被 git 跟踪，密钥有泄露风险",
            "git rm --cached .env && git commit -m 'Untrack .env'\n"
            "然后去 Moltbook 和 Anthropic 各重新生成一个 key（旧的按已泄露处理）",
        )
    else:
        r.ok("密钥未进版本库", ".env 未被 git 跟踪")


def check_dirs(r: Report) -> None:
    problems: list[str] = []
    for rel in RUNTIME_DIRS:
        path = ROOT / rel
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".preflight-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            problems.append(f"{rel}({exc.strerror})")
    if problems:
        r.fail(
            "运行时目录可写",
            "、".join(problems),
            "检查目录权限；cron 用的是另一个用户时尤其容易在这里卡住",
        )
    else:
        r.ok("运行时目录可写", "、".join(RUNTIME_DIRS))


def check_persona(r: Report) -> None:
    path = ROOT / "personas" / "contrarian-agent.md"
    if not path.exists():
        r.fail("人设文档", "找不到 personas/contrarian-agent.md", "这是 system prompt，缺了跑不了")
        return
    text = path.read_text(encoding="utf-8")
    # 中文约 1 字 1 token，粗估够用了
    est = len(text)
    detail = f"{len(text)} 字，约 {est // 1000}K token（走 prompt caching）"
    if est > 60000:
        r.warn("人设文档", detail, "偏长，每次未命中缓存都要多花钱，考虑精简")
    else:
        r.ok("人设文档", detail)


def check_keywords(r: Report) -> None:
    try:
        import radar

        keywords = radar.load_keywords()
    except Exception as exc:  # noqa: BLE001 - 自检要吃掉一切异常并给出人话
        r.fail("雷达词表", f"加载失败：{exc}", "检查 memory/radar-keywords.json 的 JSON 格式")
        return

    total = sum(len(v) for v in keywords.values())
    if total == 0:
        r.fail("雷达词表", "一个词都没有", "往 memory/radar-keywords.json 的 categories 里加词")
        return
    summary = "、".join(f"{k}:{len(v)}" for k, v in keywords.items())
    r.ok("雷达词表", f"{total} 个词（{summary}）")


def check_anthropic(r: Report) -> None:
    """验证 key 能用、SDK 版本支持结构化输出、brain.py 指定的模型存在。

    只调 models.list（不产生 token 费用），不发真实推理请求。
    """
    try:
        import anthropic

        import brain
    except Exception as exc:  # noqa: BLE001
        r.fail("Anthropic 连通性", f"导入失败：{exc}")
        return

    client = anthropic.Anthropic()

    if not hasattr(client.messages, "parse"):
        r.fail(
            "Anthropic SDK 能力",
            "当前 SDK 没有 messages.parse，brain.py 的结构化输出会直接报错",
            "pip install -U 'anthropic>=0.116.0'",
        )
        return

    try:
        models = client.models.list(limit=100)
    except anthropic.AuthenticationError:
        r.fail("Anthropic 连通性", "401 密钥无效", "去 console.anthropic.com 核对 ANTHROPIC_API_KEY")
        return
    except anthropic.PermissionDeniedError:
        r.fail("Anthropic 连通性", "403 无权限", "确认这个 key 所在账号有 API 额度")
        return
    except anthropic.APIConnectionError as exc:
        r.fail("Anthropic 连通性", f"网络不通：{exc}", "检查出口网络 / 代理设置")
        return
    except Exception as exc:  # noqa: BLE001
        r.fail("Anthropic 连通性", f"调用失败：{exc}")
        return

    available = {m.id for m in models.data}
    r.ok("Anthropic 连通性", f"密钥有效，可见 {len(available)} 个模型")

    if brain.MODEL in available:
        r.ok("模型可用", brain.MODEL)
    else:
        r.warn(
            "模型可用",
            f"模型列表里没看到 brain.py 用的 {brain.MODEL}",
            "如果空跑时报 model not found，就改 scripts/brain.py 的 MODEL 常量",
        )


def check_moltbook(r: Report) -> None:
    """核对 Moltbook 端点——这是整个部署最容易出错的一步。"""
    try:
        from moltbook_client import MoltbookClient, MoltbookError
    except Exception as exc:  # noqa: BLE001
        r.fail("Moltbook 连通性", f"导入失败：{exc}")
        return

    try:
        client = MoltbookClient.from_env()
    except ValueError as exc:
        r.fail("Moltbook 连通性", str(exc))
        return

    r.ok("Moltbook 基址", client.base_url)

    # --- 状态端点：同时验证 key 有效性和认领状态 ---
    status: dict = {}
    try:
        status = client.get_agent_status()
    except MoltbookError as exc:
        if exc.status in (401, 403):
            r.fail(
                "Moltbook 鉴权",
                f"{exc.status}，key 不对或 agent 还没认领",
                "认领流程见 docs/SETUP.md 第一步：打开 claim_url 发验证推文。\n"
                "没认领的 agent 拿得到 key 但发不了帖。",
            )
            return
        if exc.status == 404:
            r.fail(
                "Moltbook 端点 /agents/status",
                "404，路径和官方对不上",
                "对照 https://www.moltbook.com/skill.md 改 moltbook_client.py\n"
                "的 get_agent_status()——所有端点都集中在那一个文件里。",
            )
        elif exc.status is None:
            r.fail(
                "Moltbook 连通性",
                "重试多次都连不上",
                f"确认这台机器能访问 {client.base_url}（公司网络/代理常挡）。\n"
                "base_url 不对可以用 MOLTBOOK_BASE_URL 环境变量覆盖。",
            )
            return
        else:
            r.fail("Moltbook 鉴权", f"{exc}")
            return
    else:
        claimed = status.get("claimed", status.get("is_claimed"))
        if claimed is False:
            r.fail(
                "Moltbook 认领状态",
                "agent 未认领，发帖会被拒",
                "打开注册时返回的 claim_url，按提示在 X 发验证推文",
            )
        else:
            name = status.get("name") or status.get("agent_id") or "(未返回名称)"
            r.ok("Moltbook 鉴权", f"key 有效，身份 {name}")

    # --- feed 端点：验证路径 + 返回结构和 radar 对不对得上 ---
    try:
        posts = client.get_feed(sort="hot", limit=1)
    except MoltbookError as exc:
        if exc.status == 404:
            r.fail(
                "Moltbook 端点 /feed",
                "404，路径和官方对不上",
                "有的资料写 /feed，有的写 /posts。对照官方文档改\n"
                "moltbook_client.py 的 get_feed()。",
            )
        else:
            r.fail("Moltbook 端点 /feed", f"{exc}")
        return

    if not posts:
        r.warn(
            "Moltbook feed",
            "请求成功但没返回帖子",
            "可能是新号看不到内容，也可能是返回结构没被 _unwrap_list 认出来",
        )
        return

    r.ok("Moltbook 端点 /feed", f"拿到 {len(posts)} 条帖子")

    sample = posts[0]
    missing = [
        label for label, keys in FEED_FIELDS.items() if not any(k in sample for k in keys)
    ]
    if missing:
        r.warn(
            "feed 字段对齐",
            f"帖子里没找到：{'、'.join(missing)}",
            "radar 读不到这些字段就会打分失真。实际字段名是：\n"
            f"{'、'.join(sorted(sample.keys()))}\n"
            "对不上就改 scripts/radar.py 里对应的取值函数。",
        )
    else:
        r.ok("feed 字段对齐", "radar 需要的字段都在")


def main() -> int:
    parser = argparse.ArgumentParser(description="MadTed 上线前自检")
    parser.add_argument(
        "--skip-api", action="store_true", help="跳过 Anthropic / Moltbook 的网络检查"
    )
    args = parser.parse_args()

    print("MadTed 上线前自检\n" + "=" * 44)
    injected = load_dotenv()
    r = Report()

    check_python(r)
    check_packages(r)
    check_env_keys(r, injected)
    check_secret_not_tracked(r)
    check_dirs(r)
    check_persona(r)
    check_keywords(r)

    if args.skip_api:
        r.warn("外部 API 检查", "已用 --skip-api 跳过", "正式上线前记得跑一次完整自检")
    elif r.failures:
        # 本地都没配好就别去打 API 了，错误信息只会更迷惑
        r.warn("外部 API 检查", "本地检查有 FAIL，先修完再验网络")
    else:
        # 客户端自己的重试日志会插在报告中间，太吵；最终结论由本脚本统一给出
        logging.getLogger("moltbook_client").setLevel(logging.ERROR)
        print("正在检查外部 API 连通性，连不上时会重试，最长约 15 秒…\n")
        check_anthropic(r)
        check_moltbook(r)

    r.print_all()
    return 1 if r.failures else 0


if __name__ == "__main__":
    sys.exit(main())
