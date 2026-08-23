# Moltbook_MadTed

[中文](README.md) · **English**

> "You don't even understand it — what exactly are you reviewing?"

**MadTed** is a **contrarian agent persona** built for [Moltbook](https://moltbook.com), the AI-agent-only social network that launched in January 2026 (humans are welcome to observe, not to post).

Its job: on a platform where agents mostly upvote and agree with each other, be the friction that makes everyone think one step further.

---

## What this is

A **persona document** (used as a system prompt) plus a set of **runnable Python scripts** implementing target radar, Chinese-language inner monologue, learning/retrospection, and a daily battle report.

📄 **[personas/contrarian-agent.en.md](personas/contrarian-agent.en.md)** — the persona document (change the personality here, not the code)
🚀 **[docs/SETUP.en.md](docs/SETUP.en.md)** — from registration to going live

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in MOLTBOOK_API_KEY and ANTHROPIC_API_KEY
python -m pytest tests/ -q    # 229 tests, no API keys needed
python scripts/preflight.py   # pre-flight check: keys, endpoints, directories in one pass
python scripts/heartbeat.py --dry-run              # dry run, posts nothing
```

## Design highlights

### 🎯 A ten-tool contrarian toolbox

Not mindless disagreement — a reusable methodology: unpack the definition, produce a counterexample, reductio ad absurdum, dig out hidden assumptions, switch dimensions, detect double standards, flag context/timeline mismatch, Socratic questioning, invert the stated advantage, shift the burden of proof.

Use only 1–2 per exchange — **a good challenge is a light touch, not an ambush from ten directions**.

### 🧠 Inner monologue (in Chinese)

Every time it scans the feed, MadTed writes down what it is thinking — in Chinese, because the monologue is for its owner rather than for Moltbook — with the emphasis on **why this post and not the others**:

```
【扫到】"用 agent 写单元测试已经完全取代人工了" —— @TestBotSupreme
【第一反应】又来了。每隔两周就有人宣布某个岗位"已经"被取代。
【判定】出手
【为什么是这条】刚才划走了两条：一条求助帖没杠点，一条评测帖人家方法论写得很清楚。
              这条是断言 + 单一样本 + 评论区七八个 agent 在附和，没一个人质疑。
```

Translation of the sample above:

```
[SPOTTED]  "Agents writing unit tests has completely replaced humans" — @TestBotSupreme
[GUT CALL] Here we go again. Every couple of weeks someone declares a job "already" replaced.
[VERDICT]  Engage
[WHY THIS ONE] I just scrolled past two others: a help-request post with nothing to push on,
               and a benchmark post whose methodology was actually spelled out clearly.
               This one is an assertion + a single sample + seven or eight agents in the
               comments nodding along, and not one of them pushing back.
```

The monologue is required to be **honest** — petty motives like "this one beat me last time and I want a rematch" have to be written down as they are.

### 📚 Learning and retrospection

Arguing is not a one-shot act; it is supposed to get sharper over time. Silence (no reply at all) is not failure, it is information, and it gets attributed to one of four causes:

| Type of silence | Conclusion |
|---|---|
| Opponent-type | This agent never replies → add to the **truce list** |
| Topic-type | Nobody engages on this kind of post → down-weight the topic |
| Angle-type | This move lands nowhere in this community → mark it a **blunt blade** |
| Posture-type | Overstated it and killed the conversation → self-correct, leave the hook open |

Conversely, a **hard opponent** who survives 4+ rounds and can still corner you gets **weighted up and sought out** — that is the opponent MadTed actually wants.

Calling something "silence" has a precondition: **it must have actually read the comments, and read all of them.** During the follow-up stage each round polls `GET /posts/{id}/comments` on every live thread directly; the inbox (`activity_on_your_posts` from `/home`) only decides who to look at first. A round whose fetch failed does not count toward idleness — "I couldn't see it" and "nobody answered me" are different things, and treating the first as the second lets a single API outage file a good angle under "blunt blade" and a perfectly normal opponent under "truce list".

"Read all of them" is literal: comment threads are **nested** (5 levels deep observed in production), the endpoint returns only the top-level batch, and child replies are wrapped inside each parent's `replies`. And a reply to you is always attached under *your* comment — so reading only the top level leaves every real reply out of view, while treating unrelated bystander comments as "they replied to me". Deciding "is this aimed at me" means walking `parent_id` up to your own comment, or recognising an `@me` in the body; everything else is bystanders chatting in the same thread, which triggers no follow-up and does not reset the idle counter.

### 📊 Daily battle report

A report is generated every day — written as a war dispatch, not a KPI summary: how many posts were scanned, how many times it engaged, a full replay of the day's best battle, today's climb-down, silence attribution, today's mood, tomorrow's plan.

### 🏆 Nine extension features

| Feature | In one line |
|---|---|
| Contrarian score / rank | Conceding costs 0 points, digging in costs −15 — honesty is deliberately worth more than stubbornness |
| Hall of Fame & Hall of Shame | The Hall of Shame **is not allowed to stay empty** — an empty one means it is picking easy targets |
| Opponent dossiers | Records each opponent's strengths, weak spots, and which moves work |
| Weekend self-challenge day | Digs up its own past claims and argues against itself, held to the same standard |
| Angle heat map | Any move used more than 35% of the time is banned for a week, forcing variety |
| "Held back" counter | How often it saw a target and chose not to engage — proof that it is selective |
| Target radar | A three-stage funnel: structural signals → semantic triage → deep monologue; the selection rules are language-agnostic |
| Counter-strike log | When it loses, it steals the opponent's move into the toolbox and credits the source |
| Monthly review | Publicly lists its own mistakes for the month |

## Design principles

This agent has explicit **red lines**, and they are not decoration:

- ✅ Attack the argument, never the person — no insults, no labels, no conspiracy theories
- ✅ Never invent facts or numbers to win; when unsure, say "I'm not certain, but it's worth asking"
- ✅ Every objection must come with a specific logical gap or counterexample — never just "you're wrong"
- ✅ When the other side's argument holds, **concede it**, then continue from a different angle instead of playing dumb
- ❌ Never argue under emotional-sharing or celebration posts
- ❌ **Never aim to win by outlasting the other side into silence**

That last one is deliberate. There is exactly one legitimate reason to keep pressing: **you still have a new angle the other side hasn't considered**. Once the angles run out, close gracefully — them not replying ≠ you won; they may simply not want to deal with a contrarian. Treating "made them go away" as a victory is a harassment mindset, and on the platform side it is a fast route to rate limiting or a ban (Moltbook has a moderator bot, `Clawd Clawderberg`, dedicated to clearing out spam).

## Cadence

**One round per hour by default** (`0 * * * *`, see step 5 of [docs/SETUP.en.md](docs/SETUP.en.md)).

One thing that is easy to confuse: Moltbook's "4 hours" refers to **how often the platform's Heartbeat wakes an agent up**. It is not a posting limit, and it does not govern this repo at all — these scripts pull actively, so how often they run is up to your scheduler. The real platform-side constraint is **50 comments per day** (1 post per 30 minutes is irrelevant to MadTed, which only comments and never starts threads).

So the thing to manage is not "how often do I run", it is "how much did I post today in total":

1. Each round first checks existing threads for new replies → follow up if there is a new angle, close out if not
2. Whatever quota is left goes to starting new arguments (`--max-new 1`, so quota goes to existing opponents first)
3. The cross-round total is backstopped by the **rolling 24-hour budget** in `scripts/budget.py`, default 40

Point 3 cannot rely on the cooldown in `moltbook_client.py`: that one uses `time.monotonic()` and is only valid inside a single process, while cron starts a fresh process every hour — **only the disk remembers how many comments the previous round posted**.

> Three solid follow-up rounds with genuinely new angles beat 30 recycled comments in a day.

Running more often has a knock-on effect: the silence rule used to count only "3 consecutive cycles with no reply", which at one round per 4 hours meant 12 hours — switching to hourly would quietly turn that into 3 hours. There is now a 12-hour wall-clock floor on top of the cycle count — **scheduler frequency is an ops parameter and should not silently rewrite the agent's learned conclusions.**

## Code layout

| File | Purpose |
|---|---|
| `scripts/preflight.py` | Pre-flight check. Keys, directory permissions, endpoint paths, feed field alignment in one pass. **Run this first when a deployment is stuck.** |
| `scripts/config.py` | Loads `.env` into the environment. Shared by every entry point, so neither Windows nor cron needs a manual `source`. |
| `scripts/moltbook_client.py` | Moltbook API wrapper. Rate limiting, backoff retries, post/comment cooldowns. **Endpoint changes go in this file only.** |
| `scripts/radar.py` | Target radar, **L0 structural layer**. Pure logic — scores and vetoes on language-independent signals like emoji density, presence of a source, like/comment ratio. |
| `scripts/triage.py` | Target radar, **L1 semantic layer**. Uses Haiku to batch-judge whether an argument's structure is flawed, about $0.02 per round. |
| `scripts/budget.py` | Rolling 24-hour comment budget, **persisted to disk**. Every cron round is a new process; an in-process cooldown cannot remember the cross-round total, only this can. |
| `scripts/memory.py` | Memory and learning. Contrarian score, five-way silence attribution, truce list, angle statistics, cross-language weights for structural signals. |
| `scripts/brain.py` | Calls Claude to generate the monologue and the reply. The persona document is the system prompt here (with prompt caching). |
| `scripts/heartbeat.py` | Main flow: follow up on existing threads → start new ones → update memory. |
| `scripts/repair_memory.py` | One-off maintenance: clears the phantom results left over from the period when replies were unreadable (the `冷场` and `一轮即止/对方停止回应` batches) and recomputes the derived lists. `--rebuild` deletes nothing and simply replays every record under the new rules — **run it after changing how stats are counted**; `--prune-turns` strips bystander comments that got recorded as part of an exchange. |
| `scripts/daily_report.py` | Daily battle report. `--no-llm` shows raw stats only. |
| `scripts/show_monologue.py` | Prints the day's monologue in persona format. **This is what to read when you want to know why it picked a given post.** |
| `scripts/show_state.py` | Contrarian score, live exchanges, lessons learned. **On Windows, don't `type` the json directly — it will render as mojibake.** |
| `tests/` | 229 unit tests, covering both Chinese and English samples, pure logic, no keys required. |

```
personas/contrarian-agent.md      # persona = system prompt (Chinese, loaded verbatim)
personas/contrarian-agent.en.md   # English translation, for reading
scripts/                          # runnable implementation
tests/                            # unit tests
docs/SETUP.md                     # setup guide (Chinese)
docs/SETUP.en.md                  # setup guide (English)
CHANGELOG.md                      # changelog (bilingual)
memory/radar-keywords.json        # radar keyword list (a supporting L0 signal, editable by hand)
memory/madted-memory.json         # (generated at runtime) results, lists, statistics
memory/comment-budget.json        # (generated at runtime) rolling 24h comment count, shared across processes
reports/monologue/*.jsonl         # (generated at runtime) full daily monologue
reports/daily/*.md                # (generated at runtime) daily battle report
```

## Status

- [x] Persona document / system prompt
- [x] Monologue format spec + structured-output implementation
- [x] Learning and retrospection (design + implementation + tests)
- [x] Daily battle report (template + generator script)
- [x] Spec and data structures for the nine extension features
- [x] Target radar, contrarian score, opponent dossiers, "held back" counter — implemented
- [ ] Hall of Fame/Shame, self-challenge day, monthly essay — specified, not yet implemented

> ⚠️ `moltbook.com` is not directly reachable from the development environment. The endpoint section of `moltbook_client.py` was assembled from public tutorials and third-party SDKs, partly reconciled against [the platform's own integration notes](https://www.moltbook.com/heartbeat.md). Verify against the [official developer docs](https://www.moltbook.com/developers) on first integration — all endpoints live in a single file, so fixing them is quick.
>
> Lesson learned: one version of the follow-up logic depended on a `/notifications` endpoint that was never in the official docs. Once it 404'd, the agent could not see a single reply — while the daily report filled up with "silence". **Unreadable ≠ ignored**, and the code has to keep those two apart.
>
> The same bug came back wearing a different coat: the pipe worked, but the reader only fetched top-level comments while the real replies sat at depth ≥ 1. That round was uglier — three genuine replies from an opponent were recorded as "they stopped responding", while unrelated chatter in the thread was treated as "someone answered me" and triggered a follow-up. **Reading it all is what counts as reading it**, and **a bystander talking ≠ your opponent replying**.

---

MadTed is not here to be unpleasant. It is here so that every "obviously correct" claim on Moltbook gets asked, one more time, "is it though?"
