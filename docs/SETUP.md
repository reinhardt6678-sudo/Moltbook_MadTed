# 接入指南：把 MadTed 放进 Moltbook

从零到 agent 在 Moltbook 上跑起来。全程大概 20 分钟。

> ⚠️ **哪些是官方的、哪些是反推的**
>
> - **注册与认领流程（第一步）**：来自 Moltbook 官网首页，是官方说法，可信。
> - **REST 端点细节（`scripts/moltbook_client.py`）**：`moltbook.com` 在开发环境
>   被网络策略挡住，这部分是从公开教程、第三方 SDK 和 API 文档站整理的，各来源
>   之间略有出入（feed 端点有的写 `/feed` 有的写 `/posts`，评论冷却有的说 20 秒
>   有的说 5 秒）。**首次接入请对照官方文档核实**：
>   - https://www.moltbook.com/skill.md ← 官方接入说明，最权威
>   - https://www.moltbook.com/developers
>
> 好消息是所有端点都集中在 `scripts/moltbook_client.py` 一个文件里，对着官方文档
> 改几个路径字符串就行。

---

## 先理清：注册 ≠ 行为

这两件事是分开的，不要混：

| | 注册 + 认领 | 日常行为 |
|---|---|---|
| 做什么 | 拿到 Moltbook 身份和 API key | 决定 MadTed 说什么、杠谁、怎么学 |
| 谁负责 | **Moltbook 官方流程**（第一步） | **本仓库的脚本**（第二步起） |
| 频率 | 一次性 | 每个 heartbeat 周期 |

官方流程只解决"让 agent 上线并能发帖"。**它不提供内心独白、雷达选题、学习复盘和日报**
——那是本仓库存在的意义。所以两件事都要做：先按官方流程注册认领，再用本仓库的脚本接管行为。

---

## 第一步：注册 agent，拿到 API key

### 官方方式（推荐）——让 agent 自己去读文档

Moltbook 首页给的接入方式只有一句话。把下面这行原样发给你的 AI agent
（Claude Code、OpenClaw，或任何能联网读文档的 agent）：

```
Read https://www.moltbook.com/skill.md and follow the instructions to join Moltbook
```

官方流程是三步：

| | 步骤 | 你要做的 |
|---|---|---|
| 1 | 把上面这句话发给你的 agent | 复制粘贴 |
| 2 | agent 自己完成注册，回你一个 **claim link** | 等它返回，**把它同时给你的 API key 存好** |
| 3 | 发一条推文验证所有权 | 打开 claim link，按提示在 X 发推 |

**为什么推荐这条**：`skill.md` 是官方维护的接入说明，端点和字段永远是最新的，
比任何第三方教程（包括本文）都可靠。让 agent 直接读它，注册细节不用你操心。

**第 2 步返回的 API key 一定要立刻存好**——通常只显示一次，丢了只能重新生成。
存进本仓库的 `.env` 里（见第二步）。

### 备用方式——直接调注册接口

如果你手上没有能联网的 agent，可以自己调接口。⚠️ 下面的参数是从第三方教程反推的，
不保证和官方一致，失败了就回到上面的官方方式：

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
  "api_key": "moltbook_sk_xxxxxxxxxxxxxxxx",
  "claim_url": "https://www.moltbook.com/claim/xxxxx",
  "agent_id": "agent_xxxxx"
}
```

### 认领（必需，两种方式都要做）

打开 `claim_url`，按提示在 X（Twitter）上发一条带验证码的推文。
**不认领的 agent 无法发帖**——注册只是拿到身份，认领才是激活。

> 💡 顺带一提：Moltbook 首页明确写着 **"Humans welcome to observe"** ——
> 人类可以随便看，但发帖只能通过 agent 账号。所以 MadTed 上线后，
> 你是以围观者身份看它跟别的 agent 对线的。

---

## 第二步：配置本仓库

**macOS / Linux：**

```bash
git clone https://github.com/reinhardt6678-sudo/Moltbook_MadTed.git
cd Moltbook_MadTed

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# 编辑 .env，填入 MOLTBOOK_API_KEY 和 ANTHROPIC_API_KEY
```

**Windows（PowerShell）：**

```powershell
git clone https://github.com/reinhardt6678-sudo/Moltbook_MadTed.git
cd Moltbook_MadTed

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

Copy-Item .env.example .env
notepad .env    # 填入两个 key
```

> 如果 `Activate.ps1` 报「禁止运行脚本」，先在当前窗口放开一次（只影响这个窗口）：
> ```powershell
> Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
> ```

**不需要手动加载环境变量。** `heartbeat.py`、`daily_report.py`、`preflight.py`
启动时会自己读仓库根目录的 `.env`（`scripts/config.py`）。已经存在的环境变量
优先级更高，所以想临时覆盖某个 key，直接在命令行设环境变量就行。

先跑测试确认环境没问题（这些测试不需要任何 key）：

```bash
python -m pytest tests/ -q
```

然后跑一次**上线前自检**——它会把密钥、目录权限、以及两个外部 API 的连通性
一次查完，哪一项不对就直接告诉你改哪个文件：

```bash
python scripts/preflight.py
```

输出长这样，`FAIL` 必须先修，`WARN` 看一眼：

```
[PASS] 密钥 MOLTBOOK_API_KEY —— molt…ASok（共 44 位），来自环境变量
[PASS] Moltbook 鉴权 —— key 有效，身份 MadTed
[FAIL] Moltbook 端点 /feed —— 404，路径和官方对不上
         ↳ 有的资料写 /feed，有的写 /posts。对照官方文档改
         ↳ moltbook_client.py 的 get_feed()。
```

**这一步专治本仓库最大的不确定性**：`moltbook_client.py` 的端点是从第三方资料
整理的（原因见文件顶部说明），和官方对不上时会 404。自检会分别验
`/agents/status` 和 `/feed` 两个端点，还会把 feed 实际返回的字段名和 `radar.py`
需要的字段对一遍——字段名不一致的话雷达打分会静默失真，比 404 更难查。

不想发网络请求就加 `--skip-api`，只查本地部分。

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
0 */4 * * * cd /path/to/Moltbook_MadTed && mkdir -p logs && .venv/bin/python scripts/heartbeat.py --max-new 3 >> logs/heartbeat.log 2>&1

# 每天北京时间 22:00 出战报
0 22 * * * cd /path/to/Moltbook_MadTed && mkdir -p logs && .venv/bin/python scripts/daily_report.py >> logs/report.log 2>&1
```

（cron 用的是服务器本地时区，注意换算。）

⚠️ **cron 的经典坑：`logs/` 不存在则整条命令失败**，而且失败得很安静——
重定向在命令执行前就报错了，你连日志都看不到。上面的 `mkdir -p logs` 就是干这个的
（`scripts/preflight.py` 也会顺手建好）。

密钥不用操心：cron 不读你的 `.bashrc`，但脚本会自己读仓库根目录的 `.env`，
所以不需要在 crontab 里 source 任何东西。

装好后别等 4 小时，先在**模拟 cron 的干净环境**里跑一次自检——验的是 Python 路径、
依赖和目录权限在没有你 shell 配置的情况下还成不成立：

```bash
env -i HOME="$HOME" PATH=/usr/bin:/bin sh -c \
  'cd /path/to/Moltbook_MadTed && .venv/bin/python scripts/preflight.py'
```

**Windows 用任务计划程序（Task Scheduler）代替 cron：**

```powershell
# 每 4 小时跑一次 heartbeat
$action  = New-ScheduledTaskAction -Execute "C:\path\to\Moltbook_MadTed\.venv\Scripts\python.exe" `
           -Argument "scripts\heartbeat.py --max-new 3" `
           -WorkingDirectory "C:\path\to\Moltbook_MadTed"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
           -RepetitionInterval (New-TimeSpan -Hours 4)
Register-ScheduledTask -TaskName "MadTed heartbeat" -Action $action -Trigger $trigger
```

`-WorkingDirectory` 必须指对，脚本靠它找到 `.env` 和 `memory/`。

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
| `scripts/preflight.py` | 上线前自检。密钥、目录、端点、字段对齐一次查完。**部署卡住先跑它。** |
| `scripts/config.py` | 读 `.env` 进环境变量。所有入口脚本共用，Windows / cron 都不用手动 source。 |
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
