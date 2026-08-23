# 更新日志 / Changelog

本项目还没有发布版本号，所以按日期记录。条目取自实际合并进 `main` 的提交。
This project has no release versions yet, so entries are dated. They are taken from the commits
actually merged into `main`.

每条先中文后英文。
Each entry gives the Chinese line first, then the English one.

---

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
