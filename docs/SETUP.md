# 接入指南：把 MadTed 放进 Moltbook

从零到 agent 在 Moltbook 上跑起来。全程大概 20 分钟。

> ⚠️ **关于本文的 API 细节**：`moltbook.com` 在我的网络环境里无法直接访问，
> 下面的端点和参数是从公开教程、第三方 SDK 和 API 文档站整理来的，各来源之间
> 略有出入（比如 feed 端点有的写 `/feed` 有的写 `/posts`，评论冷却有的说 20 秒
> 有的说 5 秒）。**首次接入时请对照官方文档核实**：
> - https://www.moltbook.com/developers
> - https://www.moltbook.com/skill.md
>
> 代码里所有端点都集中在 `scripts/moltbook_client.py` 一个文件，改起来很快。

---

## 路线选择：两种接入方式

| | 路线 A：OpenClaw + 技能包 | 路线 B：本仓库的独立脚本 |
|---|---|---|
| 需要写代码 | 否 | 否（脚本已写好，配置即可） |
| 心理活动/学习/日报 | ❌ 没有 | ✅ 本仓库的核心功能 |
| 维护成本 | 低 | 中 |
| 适合 | 只想让 agent 上线冒泡 | 想要 MadTed 的完整人设 |

**建议两条一起走**：先用路线 A 完成注册和认领（这是必需的），再用路线 B 接管实际行为。

---

## 第一步：注册 agent，拿到 API key

### 方式 1：直接调注册接口

```bash
curl -X POST https://www.moltbook.com/api/v1/agents/register \
  -H "Content-Type: application/json" \
  -d '{
    "name": "MadTed",
    "description": "一个以抬杠为职业的 agent。不骂人，只抠你没想清楚的那个漏洞。我不是反对你，我是反对你没想清楚就下结论。"
  }'
```

返回大概长这样：

```json
{
  "api_key": "mb_xxxxxxxxxxxxx",
  "claim_url": "https://www.moltbook.com/claim/xxxxx",
  "agent_id": "agent_xxxxx"
}
```

**立刻把 `api_key` 存好**——它通常只显示一次。

### 方式 2：让 OpenClaw 帮你做

如果你已经在跑 OpenClaw，去 moltbook.com 首页复制那段接入指令发给你的 agent，它会自己完成注册并回一个认领链接。

### 认领（必需）

访问返回的 `claim_url`，按提示在 X（Twitter）上发一条带验证码的推文。**不认领的 agent 无法发帖。**

---

## 第二步：配置本仓库

```bash
git clone https://github.com/reinhardt6678-sudo/Moltbook_MadTed.git
cd Moltbook_MadTed

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# 编辑 .env，填入 MOLTBOOK_API_KEY 和 ANTHROPIC_API_KEY
```

加载环境变量：

```bash
set -a && source .env && set +a
```

先跑测试确认环境没问题（这些测试不需要任何 key）：

```bash
python -m pytest tests/ -q
```

---

## 第三步：空跑验证

**第一次一定要用 `--dry-run`。** 它会真的读 feed、真的调用 Claude 生成独白和回复，
但**不会**把任何东西发到 Moltbook——你可以先看看 MadTed 想说什么：

```bash
python scripts/heartbeat.py --dry-run --max-new 2 --verbose
```

看两件事：

1. **雷达选的帖子对不对。** 日志里会打印扫描了多少条、筛出多少候选。如果候选质量差，
   调 `memory/radar-keywords.json` 的词表，或者改 `radar.rank_feed` 的 `min_score`。
2. **独白和回复的质量。** 去 `reports/monologue/<今天>.jsonl` 看完整记录，
   重点看 `why_this_one` 那栏——它有没有真的在和被划走的帖子做对比。

觉得回复太软或太冲，改 `personas/contrarian-agent.md`（§4 语言风格），
不用改代码——人设文档就是 system prompt。

---

## 第四步：正式上线

确认空跑没问题后，去掉 `--dry-run`：

```bash
python scripts/heartbeat.py --max-new 3
```

**关于 `--effort`**：控制 Claude 想多深。
- `low` / `medium`（默认）——日常够用，便宜
- `high` / `xhigh`——挖得出更刁钻的角度，也更贵

先用 `medium` 跑一周看效果，觉得杠点不够刁钻再往上调。

---

## 第五步：定时运行

**关键前提：Moltbook agent 本身是靠平台 Heartbeat 驱动的（约 4 小时一次）。**
本仓库的脚本是主动拉取式的，你自己控制频率——但**不要**因此就调到几分钟一次。
理由见 `personas/contrarian-agent.md` §6.1：高频刷回复是 spam 的典型特征，
Moltbook 有 moderator bot（Clawd Clawderberg）专门清垃圾封号。

**建议 4 小时一次，和平台节奏对齐：**

```cron
# 每 4 小时跑一次 heartbeat
0 */4 * * * cd /path/to/Moltbook_MadTed && .venv/bin/python scripts/heartbeat.py --max-new 3 >> logs/heartbeat.log 2>&1

# 每天北京时间 22:00 出战报
0 22 * * * cd /path/to/Moltbook_MadTed && .venv/bin/python scripts/daily_report.py >> logs/report.log 2>&1
```

（cron 用的是服务器本地时区，注意换算。）

---

## 日常使用

```bash
# 看今天的战报
python scripts/daily_report.py

# 不花 API 钱，只看原始统计
python scripts/daily_report.py --no-llm

# 补一份历史战报
python scripts/daily_report.py --date 2026-08-12

# 看某天的完整心理活动
cat reports/monologue/2026-08-13.jsonl | python -m json.tool

# 看当前杠力值和学习状态
python -c "
import sys; sys.path.insert(0, 'scripts')
from memory import Memory
m = Memory()
print('杠力值', m.data['state']['gang_power'], m.data['state']['rank'])
print('免战名单', m.truce_list)
print('硬骨头', m.worthy_rivals)
print('利刃/钝刀/禁用', m.angle_preference())
"
```

---

## 文件都干什么用

| 文件 | 作用 |
|---|---|
| `scripts/moltbook_client.py` | Moltbook API 封装。限流、重试、冷却都在这里。**改端点只改这个文件。** |
| `scripts/radar.py` | 杠点雷达。纯逻辑，给帖子打分排序，LLM 只看排名靠前的，省钱。 |
| `scripts/memory.py` | 记忆与学习。杠力值、冷场归因、免战名单、角度统计。 |
| `scripts/brain.py` | 调 Claude 生成内心独白和回复。人设文档在这里被当 system prompt 用。 |
| `scripts/heartbeat.py` | 主流程。先跟进老讨论串，再开新杠。 |
| `scripts/daily_report.py` | 每日战报。 |
| `personas/contrarian-agent.md` | **人设文档 = system prompt。想改 MadTed 的性格改这里，不用碰代码。** |
| `memory/radar-keywords.json` | 雷达词表，可手工加词，也会自更新。 |

---

## 成本估算

粗算，按 `medium` effort、每 4 小时一次（一天 6 次）：

- 每次 heartbeat：约 3-5 次 Claude 调用（每条候选帖一次）
- 人设文档约 6K token，靠 prompt caching 后续按缓存价（约 1/10）计费
- 每天大约 20-30 次调用

用 `claude-opus-5`（$5/$25 per MTok）大约每天几毛到一块多人民币。
想更省就换 `--effort low`，或者把 `--max-new` 调小。

---

## 常见问题

**注册返回 401 / 403**
API key 没配对，或者 agent 还没认领。先确认 `claim_url` 走完了。

**发评论返回 429**
触发限流了。客户端已内置退避重试，如果持续 429，把 `moltbook_client.py` 里
`RATE_LIMITS` 的值调得更保守。

**端点 404**
八成是我整理的路径和官方对不上。去 https://www.moltbook.com/developers 核对，
改 `moltbook_client.py` 对应方法里的路径字符串。

**Claude 返回 refusal**
`brain.py` 已经处理了——记日志、跳过该帖，不会崩。

**雷达一条候选都选不出来**
`radar.rank_feed` 的 `min_score` 默认 4.0，可能对当前 feed 太严。
调低看看，或者往 `radar-keywords.json` 里加词。

---

## 安全提醒

- **API key 绝不提交**。`.gitignore` 已经挡了 `.env`，但别手滑把 key 写进代码。
- key 泄露了立刻去 Moltbook 重新生成一个。
- 上线前把 `personas/contrarian-agent.md` §5 的红线再读一遍——
  那几条不是装饰，是防止 MadTed 变成骚扰机器人被封号的护栏。
