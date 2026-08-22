"""客户端的端点回退逻辑测试——纯逻辑，不发真实请求。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from moltbook_client import MoltbookClient, MoltbookError, _unwrap_list  # noqa: E402


class FakeAPI:
    """假的 _request：只认某一条路径，其余一律 404。"""

    def __init__(self, working_path: str | None, *, status_for_others: int = 404):
        self.working_path = working_path
        self.status_for_others = status_for_others
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, method, path, *, params=None, json=None, max_retries=4):
        self.calls.append((method, path, json))
        if path == self.working_path:
            return {"id": "c1", "path": path}
        raise MoltbookError(
            f"{method} {path} 失败: {self.status_for_others}", self.status_for_others, ""
        )


@pytest.fixture
def client():
    return MoltbookClient("moltbook_sk_test")


def test_inbox_reads_home_activity(client):
    """收件箱走官方文档写的 /home → activity_on_your_posts。"""
    calls: list[str] = []

    def fake(method, path, *, params=None, json=None, max_retries=4):
        calls.append(path)
        return {"activity_on_your_posts": [{"post_id": "p1"}]}

    client._request = fake

    assert client.get_inbox_activity() == [{"post_id": "p1"}]
    assert calls == ["/home"]


def test_empty_inbox_does_not_trigger_fallback(client):
    """空收件箱是常态，不该被当成失败去白跑 /notifications。"""
    calls: list[str] = []

    def fake(method, path, *, params=None, json=None, max_retries=4):
        calls.append(path)
        return {"activity_on_your_posts": []}

    client._request = fake

    assert client.get_inbox_activity() == []
    assert calls == ["/home"]


def test_inbox_falls_back_when_home_missing(client):
    """/home 不可用时退回 /notifications，而不是让整个跟进阶段瞎掉。"""
    fake = FakeAPI("/notifications")
    fake_result = {"notifications": [{"post_id": "p2"}]}

    def wrapper(method, path, *, params=None, json=None, max_retries=4):
        if path == "/notifications":
            fake.calls.append((method, path, json))
            return fake_result
        return fake(method, path, params=params, json=json)

    client._request = wrapper

    assert client.get_inbox_activity() == [{"post_id": "p2"}]


def test_create_comment_falls_back_past_404(client):
    """第一条候选 404 时要继续试下一条，而不是直接放弃。"""
    fake = FakeAPI("/comments")
    client._request = fake

    result = client.create_comment("p1", "内容")

    assert result["path"] == "/comments"
    # 三条候选都试过了，且顺序是从最可能的那条开始
    assert [path for _, path, _ in fake.calls] == [
        "/posts/p1/comments",
        "/posts/p1/replies",
        "/comments",
    ]


def test_create_comment_sends_right_payload_per_path(client):
    """/comments 需要 body 里带 post_id，子路径写法不需要。"""
    fake = FakeAPI("/posts/p1/comments")
    client._request = fake

    client.create_comment("p1", "内容")

    _, _, payload = fake.calls[0]
    assert payload == {"content": "内容"}

    fake2 = FakeAPI("/comments")
    client2 = MoltbookClient("moltbook_sk_test")
    client2._request = fake2
    client2.create_comment("p1", "内容")
    assert fake2.calls[-1][2] == {"post_id": "p1", "content": "内容"}


def test_working_path_is_remembered(client):
    """确认过的端点要记住，第二次不该再从头试一遍。"""
    fake = FakeAPI("/comments")
    client._request = fake

    client.create_comment("p1", "第一条")
    first_round = len(fake.calls)
    # 清掉评论冷却——本用例测的是端点记忆，不该真等 25 秒
    client._throttle._last_action.clear()
    client.create_comment("p2", "第二条")

    assert first_round == 3          # 第一次逐个试
    assert len(fake.calls) == 4      # 第二次一击即中
    assert fake.calls[-1][1] == "/comments"


def test_non_404_errors_are_not_swallowed(client):
    """401/403 说明鉴权有问题，不该被当成『换条路径试试』。"""
    fake = FakeAPI(None, status_for_others=403)
    client._request = fake

    with pytest.raises(MoltbookError) as exc:
        client.create_comment("p1", "内容")

    assert exc.value.status == 403
    assert len(fake.calls) == 1  # 立刻抛出，不继续试


def test_all_candidates_404_raises(client):
    fake = FakeAPI(None)
    client._request = fake

    with pytest.raises(MoltbookError) as exc:
        client.create_comment("p1", "内容")

    assert exc.value.status == 404
    assert len(fake.calls) == 3


def test_get_replies_accepts_either_key(client):
    """评论列表有的端点叫 comments，有的叫 replies，都要认。"""
    client._request = lambda *a, **kw: {"replies": [{"id": "r1"}]}
    assert client.get_replies("p1") == [{"id": "r1"}]

    client._resolved.clear()
    client._request = lambda *a, **kw: {"comments": [{"id": "c1"}]}
    assert client.get_replies("p1") == [{"id": "c1"}]


def test_unwrap_list_handles_multiple_keys():
    assert _unwrap_list({"comments": [1]}, "comments", "replies") == [1]
    assert _unwrap_list({"replies": [2]}, "comments", "replies") == [2]
    assert _unwrap_list([3], "comments") == [3]
    assert _unwrap_list(None, "comments") == []
    assert _unwrap_list({"没见过的键": [4]}, "comments") == []


def test_dry_run_does_not_remember_paths():
    """空跑没真的碰过服务端，不能把猜的路径当成已确认。"""
    client = MoltbookClient("moltbook_sk_test", dry_run=True)
    client.create_comment("p1", "内容")
    assert client._resolved == {}


def test_dry_run_still_requires_a_key():
    """空跑不是免 key 模式——dry_run 只挡写，读照样发到服务端。

    以前 `not api_key and not dry_run` 把空跑放行了，于是忘配 key 的一轮
    --dry-run 会拿空 Authorization 去打每个 GET，四次重试耗尽后留下一屏
    网络错误。真正的原因（你没配 key）被伪装成了平台故障，而 --dry-run
    的全部意义就是在上线前把这类问题暴露出来。
    """
    with pytest.raises(ValueError, match="MOLTBOOK_API_KEY"):
        MoltbookClient("", dry_run=True)


def test_get_replies_flattens_nested_comments(client):
    """子回复裹在父评论的 replies 里，只取顶层等于把所有冲我来的回复都漏掉。

    线上表现：对手在楼里回了三条、其中一条直接回我，agent 一条都没读到，
    最后把这串记成"对方停止回应，收尾"。
    """
    def fake(method, path, *, params=None, json=None, max_retries=4):
        return {
            "success": True,
            "count": 3,
            "comments": [
                {
                    "id": "c1",
                    "content": "顶层",
                    "replies": [
                        {"id": "c2", "content": "回 c1", "parent_id": "c1",
                         "replies": [{"id": "c3", "content": "回 c2"}]}
                    ],
                }
            ],
        }

    client._request = fake
    got = client.get_replies("p1")

    assert [c["id"] for c in got] == ["c1", "c2", "c3"]
    # 服务端没给 parent_id 的，按树结构补上——摊平之后这是唯一还能分辨回谁的东西
    assert got[2]["parent_id"] == "c2"
    # 摊平之后不再拖着整棵子树
    assert "replies" not in got[1]


def test_create_comment_unwraps_the_envelope(client):
    """POST 的返回体裹了一层信封，不解掉就拿不到自己的评论 id。"""
    from moltbook_client import comment_id

    def fake(method, path, *, params=None, json=None, max_retries=4):
        return {"success": True, "comment": {"id": "my-comment", "content": "x"}}

    client._request = fake

    assert comment_id(client.create_comment("p1", "x")) == "my-comment"


def test_agent_self_name_reads_nested_agent():
    """/agents/status 把名字放在 agent 子对象里。"""
    from moltbook_client import agent_self_name

    assert agent_self_name({"agent": {"name": "madted"}}) == "madted"
    assert agent_self_name({"name": "madted"}) == "madted"
    assert agent_self_name({"success": True}) == ""
