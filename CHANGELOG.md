# 更新日志 / Changelog

本项目还没有发布版本号，所以按日期记录。条目取自实际合并进 `main` 的提交。
This project has no release versions yet, so entries are dated. They are taken from the commits
actually merged into `main`.

每条先中文后英文。
Each entry gives the Chinese line first, then the English one.

---

## 2026-08-31

- **计分表九档里有五档是死代码，杠力值因此量不出质量** —— `SCORE_TABLE` 列了九种战果，
  收尾那行代码却只会产生四种（`"我认输" if conceded else "多轮激辩"`）。分值最高的
  「对方改口」(+20) 和最低的「硬撑」(-15)、「杠错了」(-10) 从来没发生过一次——15 场
  实战的分布正好印证。杠力值实际上退化成了参与轮数的加权计数：最想奖励的（把人说改口）
  和最想惩罚的（明知理亏还硬撑）都不在分数里，而 §10.1 写明这套分值的设计意图就是
  「让诚实在数值上优于嘴硬」。现在 `FollowUp` 多报三项判定，收尾时按 §10.1 整张表判；
  判定一旦为真就记在讨论串上，对方在第 2 轮改口、对线又走了 4 轮，这场依然算改口。
  「多轮激辩」也照表要求 ≥3 轮，两轮就散场的不再冒充 +10。
  **Five of the nine score-table outcomes were dead code, so gang power measured the wrong thing**
  — `SCORE_TABLE` lists nine outcomes; the closing line could only ever produce four. The highest
  row (opponent moved, +20) and the lowest (stonewalling -15, factual error -10) had never once
  fired, which the 15 recorded battles confirm exactly. Gang power had degenerated into a weighted
  round counter: neither what the persona most wants to reward nor what it most wants to punish was
  in the number, though §10.1 states the whole point of these values is to make honesty score better
  than stubbornness. `FollowUp` now reports three more verdicts, the close is judged against the
  full table, and a verdict sticks to the thread once true — an opponent who moved in round 2 still
  counts even if the argument ran four more rounds. "Multi-round" now also requires the >=3 rounds
  the table asks for.

- **被 moderator 警告之后会继续发，直到有人发现** —— -50 是全表最重的一档，比任何一场
  对线赢回来的都多，代码里却没有任何一处去发现它。收件箱本来每轮都在拉，只是没人看
  那几条。现在警告会在**任何一次发送之前**判掉：记 -50、落下 `memory/halt.json`，
  之后每一轮都不发东西，直到人工 `python scripts/halt.py --clear`。不设自动过期——
  自动解闸等于把"人看过了吗"偷偷答成"看过了"。扣过分的警告 id 记在 `warned_ids` 里，
  否则解闸之后下一轮读到同一条通知会把自己又关回去。
  **A moderator warning kept the agent posting until a human noticed** — -50 is the heaviest row in
  the table, more than any argument can win back, and nothing in the code looked for it. The inbox
  was already being fetched every round; those entries were simply never read. The check now runs
  before any send: it records the -50, writes `memory/halt.json`, and every later round posts
  nothing until someone runs `python scripts/halt.py --clear`. It never expires on its own — an
  automatic lift quietly answers "has a human looked at this?" with "yes". Charged warning ids are
  kept in `warned_ids`, or clearing the gate would let the next round read the same notification and
  lock itself back in.

- **自己那条评论拿了多少票，一直被扔掉** —— radar 会读别人帖子的赞数，却从不读自己
  评论的。评论区本来就整棵拉下来了，那个数字就在手边。代价是所有学习都建立在"对方回没回我"
  这一个很粗的代理指标上：一条被踩到 -20 的评论和一条没人看见的评论，在记忆里长得一模一样，
  都是一条 -2 的冷场，然后 §8.2 的归因去怪角度。现在票数记进战绩，被踩到负分之后的沉默
  归为「姿态型」——§8.2 里本来就有这一类，只是代码从没赋值过——并且不再给那一招记一次
  「无效」。平台没给票数字段时记 None 而不是 0：0 是"没人点赞"，None 是"平台没说"。
  **The score on MadTed's own comments was being thrown away** — radar reads other people's upvotes
  but never its own. The comment tree is already fetched in full; the number was right there. The
  cost was that every learning signal rested on one coarse proxy, "did they reply": a comment
  downvoted to -20 and a comment nobody saw looked identical in memory — both a -2 cold shoulder —
  and §8.2 then blamed the angle. The score is now recorded, silence after a negative score is
  attributed to posture (a category §8.2 always had and code never assigned), and that battle no
  longer marks the angle ineffective. A missing score field records None, not 0: zero means nobody
  upvoted, None means the platform never said.

- **空跑和真跑之间没有"我先看一眼"这一档** —— 只有全不发和全直接发两个极端，而输出是
  公开且撤不回的。新增 `--queue`：照常选题、照常写回复，但攒进 `memory/pending.jsonl`，
  由 `python scripts/approve.py` 逐条过目，点头的才发。和空跑的区别是状态**推迟**而不是
  丢掉——批准那一刻才落讨论串、战绩和额度，所以批准之后 MadTed 知道自己参与过这条串，
  下一轮会正常跟进。排队中的帖子本轮不再重复深挖（L2 是最贵的一层），独白存档里打
  `pending` 标记，不进战报统计。
  **There was no "let me look first" setting between a dry run and a live one** — only send nothing
  or send everything, for output that is public and cannot be taken back. `--queue` picks and writes
  as usual but parks the drafts in `memory/pending.jsonl`; `python scripts/approve.py` walks through
  them and only what you approve goes out. Unlike a dry run the state is deferred, not discarded:
  the thread, the battle and the budget land at approval time, so an approved draft leaves MadTed
  knowing it took part and following up normally next round. A post with a draft waiting is not
  deliberated over again (L2 is the expensive layer), and queued monologues carry a `pending` marker
  so they stay out of the daily report.

## 2026-08-24

- **空跑不再往盘上写任何东西** —— 空跑不发评论，却照样把"这条我回过了"和扣掉的杠力值写进了
  记忆和讨论串存档，于是下一次真跑把它当成已经答过、直接跳过，那条评论永远发不出去。现在
  `Memory` 和讨论串存档跟 `CommentBudget` 一样认 `dry_run`。独白照存——那是空跑的主要产出——
  但打上标记，不再计进战报的出手数和追问数。
  **Stop the dry run from leaving anything on disk** — it posted no comment, yet still wrote "I
  answered this one" and the docked gang power into memory and the thread archive, so the next live
  run read that back as already answered, skipped the post, and the comment never went out at all.
  `Memory` and the thread archive now honour `dry_run` the way `CommentBudget` already did. The
  monologue is still archived — that is the main output of a dry run — but it is marked, and no
  longer counts toward the daily report's engagement and follow-up totals.

## 2026-08-22

- **删掉两份和代码已经对不上的上手文档** —— 它们描述的流程早就变了，留着比没有更误导人。
  **Delete two onboarding docs that outlived the code they described** — the flow they documented had
  since changed, and keeping them was more misleading than not having them.
- **空跑遇到缺失的 key 时直接失败，而不是伪装成网络故障** —— 缺 key 是配置问题，报成网络问题会
  让人往完全错误的方向排查。
  **Make the dry run fail on a missing key instead of faking a network outage** — a missing key is a
  configuration problem, and reporting it as a network problem sends you debugging in the wrong
  direction entirely.

## 2026-08-18

- **读完整棵评论树，并且只把冲着自己来的回复算数** —— 评论区是有层级的（线上见过 5 层深），
  只读顶层等于把真回复全留在视野外，同时把楼里旁人的发言当成"对方回我了"。
  **Read the whole comment tree, and only count replies aimed at me** — comment threads are nested
  (5 levels deep observed in production). Reading only the top level leaves every real reply out of
  view while treating unrelated bystander chatter as "they replied to me".

## 2026-08-17

- **给 L1 粗筛调用加上墙上时间上限，并按上限决定批量大小** —— 免得一次慢调用把整轮拖死。
  **Bound the L1 triage call by wall clock, and size the batch to fit** — so one slow call cannot
  stall the entire round.
- **不让被截断的模型输出发到 Moltbook 上去** —— 截断的回复发出去就是自己打自己脸。
  **Stop truncated model output from reaching Moltbook** — posting a half-finished reply undoes the
  whole point.

## 2026-08-16

- **修两个只在无人值守那次运行里才会触发的 bug** —— 手动跑一切正常，只有 cron 那次翻车，
  这类问题最难查。
  **Fix two bugs that only ever fire in the unattended run** — everything works by hand and only the
  cron run breaks, which is the hardest class of bug to catch.
- **修 L1 粗筛静默截断、然后把它报成解析错误的问题** —— 报错报错了地方，等于把线索指反。
  **Fix L1 triage silently truncating, then reporting it as a parse error** — the wrong error message
  points the investigation the wrong way.
- **把 README 里引用的测试数量同步成实际数字。**
  **Bump the test count README quotes to match.**

## 2026-08-15

- **改成每小时一轮，并用落盘的评论额度兜住** —— 进程内的冷却挡不住跨轮总量：cron 每小时拉起
  的是新进程，上一轮发过多少条只有磁盘记得住。同时给"冷场"判定加了 12 小时墙上时间下限，
  免得定时器频率顺手改掉 agent 的学习结论。
  **Switch to hourly cadence, guard it with a persisted comment budget** — an in-process cooldown
  cannot cap the cross-round total: cron starts a fresh process every hour, and only the disk
  remembers how many comments the previous round posted. A 12-hour wall-clock floor was added to the
  silence rule at the same time, so that scheduler frequency does not silently rewrite the agent's
  learned conclusions.

## 2026-08-14

- **修跟进阶段根本看不到回复、却把它们记成冷场的问题** —— 读不到 ≠ 没人理，这两件事在代码里
  必须分开。
  **Fix follow-up stage never seeing replies, then logging them as cold** — unreadable ≠ ignored, and
  the code has to keep those two apart.
- **让选题不挑语言：L0 结构层 + L1 语义粗筛** —— 关键词表本质上是拿 grep 模拟语义理解，每支持
  一种语言就要重写一遍。改成 L0 只用语言无关的结构信号（emoji 密度、有无出处、赞评比），
  L1 用描述论证结构缺陷的 rubric，一次写完全语言通用。
  **Make post selection language-agnostic: L0 structural layer + L1 semantic triage** — a keyword
  list is fundamentally grep pretending to be semantic understanding, and every extra language means
  rewriting it. L0 now uses only language-independent structural signals (emoji density, presence of
  a source, like/comment ratio), and L1 uses a rubric describing structural flaws in an argument,
  written once and valid in every language.

## 2026-08-13

初始构建日：人设、可运行实现、部署链路一次成型。
The initial build day: persona, runnable implementation and deployment path all landed.

- **加入 MadTed 抬杠 agent 人设**，随后调整为"追求新角度而非耗到对方沉默"，并把节奏对齐
  Heartbeat。
  **Add the MadTed contrarian agent persona**, then tune it to pursue new angles over attrition and
  align its cadence to Heartbeat.
- **补上内心独白、学习记忆、每日战报，以及可选功能构想**；随后把九项扩展功能连同数据结构
  正式定稿，并加上项目 README。
  **Add the inner monologue, learning memory, daily report and optional feature ideas**; then
  formalize the nine extension features with a data schema and add the project README.
- **实现可运行的 agent**：API 客户端、杠点雷达、记忆、大脑、报告。
  **Implement the runnable agent**: API client, radar, memory, brain, reports.
- **补上部署前自检并修好 cron 的日志目录问题** —— `logs/` 不存在会让整条命令静默失败。
  **Add the deployment preflight check and fix the cron logging setup** — a missing `logs/` makes the
  whole command fail silently.
- **自动加载 `.env`，让 Windows 和 cron 都不用手动 source。**
  **Load `.env` automatically so Windows and cron both work.**
- **按官网首页补上官方接入流程文档**；**更正文档里的 Moltbook key 格式并改进掩码显示。**
  **Document the official Moltbook onboarding flow from the homepage**; **correct the documented
  Moltbook key format and improve masking.**
- **读取 `submolt_name`，让按社区过滤真的生效。**
  **Read `submolt_name` so community-based filtering actually works.**
- **修评论端点 404 并压低运行成本。**
  **Fix the comment endpoint 404 and cut running cost.**
- **加入独白查看器和状态查看器**，并修掉在 Windows 上会乱码的记忆检查文档命令。
  **Add the monologue viewer and the state viewer**, and fix the memory-inspection doc commands that
  were broken on Windows.
- **收紧 `.gitignore`，防止密钥文件被提交。**
  **Harden `.gitignore` for secret files.**
- **正式部署 agent MadTed。**
  **Deploy agent MadTed.**
