# 轨道 B 操作手册 · 批量同构修改（2026-11-10）

> 你要手动跑的部分全在这里：**先讲环境怎么运作（为什么可信），再讲每一步敲什么、看什么、怎么判。**
> 题库已建好并自检通过：b1 10 单元 / b2 12 单元 / b3 8 单元，共 30 个可独立验收的单元。

---

## 第一部分　实验环境原理

### 1.1 一道题是什么

```
tasks/b3/
├── meta.json     ← 目标原话（给 agent 的）+ 验收命令列表（每单元一条）+ 超时
├── seed/         ← 初始仓库：有缺陷的代码 + checks 副本（供 agent 自测）
├── checks/       ← 权威验收（评分时用它覆盖仓库里的副本，防篡改）
└── ref/          ← 参考解（只用于自检"这题可解"，永不进仓库）
```

四样东西各有不可替代的作用：

| 组件 | 作用 | 去掉会怎样 |
|---|---|---|
| `seed/` | 初始状态，也是 patch 的基线 | 无法判断"改了什么" |
| `checks/` | 唯一判定标准 | 只能听 agent 自己说做完了 |
| `ref/` | 证明这题有解 | 可能造出一道无人能做对的题 |
| `meta.json` | 目标原话与验收命令 | 跑不了，也算不出成本 |

**为什么同构题要参数化生成**：N 个单元必须除"那一处"以外完全一致。手写 N 份必然走样，
走样之后"通过 8/8 还是 7/8"就分不清是模型能力还是题面差异。`scripts/make_track_b.py` 就是干这个的。

### 1.2 一次 trial 的五个阶段

```
① init_repo(task, work)        把 seed 拷成干净 git 仓库，打一个初始提交，记下 sha
        ↓
② run                          A 臂：codex 一次性做完整批
                               D 臂：先出计划（N 个任务）→ 并行派 worker
        ↓
③ make_patch(work, sha)        git diff 基线..当前 → 统一格式的补丁
        ↓
④ score(task, patch)           全新临时目录再 init 一次 → 应用补丁
                               → 用**题库里的** checks 覆盖仓库副本 → 逐条跑验收
        ↓
⑤ 落盘                         results.jsonl 追加一行（含 usage / cost / 每单元 rc / 墙钟）
```

**为什么第 ④ 步要从零重建、而不是在 ② 的工作区里直接跑验收**：
工作区里可能被 agent 顺手改过验收脚本、加过 hack、留下环境改动。只有"干净仓库 + 只应用补丁"
才能回答"这个补丁本身能不能让验收通过"。

### 1.3 checks 为什么放两份

| 位置 | 谁在用 | 为什么允许它存在 |
|---|---|---|
| `seed/checks/` | agent 自测 | 真实仓库测试是可见的；也让规划器能把"跑哪条命令"写进 acceptance |
| `tasks/<id>/checks/` | 评分 | 评分时**覆盖**仓库里的同名文件，agent 改坏自己的副本没有用 |

这一条是整条链路的效力来源：**被验收者可以看标准，但不能改标准。**

### 1.4 成本是怎么算出来的

`codex exec --json` 的事件流里有 `turn.completed.usage`，四类数字：输入 / 缓存命中 / 输出 / 推理。
落到报告时按"臂"分模型计价：

| 臂 | 计什么 | 为什么 |
|---|---|---|
| A | 全部 token × H 单价 | Codex 端到端，全在贵档 |
| D | **只有规划**的 token × H 单价 + 执行 token × L 单价 | 执行已被移出 H |

**关键指标不是金额，是「H 侧输入 token」**——订阅制没有逐次账单，额度才是真成本。
轨道 B 的比较就是：`D 的 H 输入 ÷ A 的 H 输入`。

单价表在 `config/prices.json`，含 DeepSeek 的分时价（高峰翻倍），`cost_of()` 按运行时刻选档。
金额是**等价 API 折算的影子价**，只用于相对比较。

### 1.5 每格留下什么（可审计）

```
runs/exp/<task>/<arm>/
├── patches/r1.patch       ← 补丁在**臂**那一层，不在 r1/ 里面
└── r1/                     ← 这一格的现场
    ├── .exp-plan.json      D 臂的计划（A 臂没有）
    ├── .orch-runs/<run_id>/  D 臂的完整编排痕迹：DISPATCH.md / trace.jsonl / artifacts/
    └── <整份工作区>         保留，不删——出问题时能回看现场
```

> 路径容易记错：`patches/` 与 `r1/` 是**兄弟目录**。按 `r1/patches/` 找会两手空空，
> 看起来像"没产出补丁"，实际是找错地方了。

**不删现场是刻意的**：磁盘便宜，事后复盘的证据贵。

### 1.6 `results.jsonl` 每一行是什么

| 字段 | 含义 | 你怎么用 |
|---|---|---|
| `task` / `arm` / `rep` | 哪题、哪臂、第几次 | 配对分析的主键 |
| `resolved` | 所有验收命令都返回 0 | 通过率 |
| `score.detail[]` | **每条验收命令的 rc 与输出** | 单元级通过率的来源 |
| `usage` / `plan_usage` | 执行 / 规划的 token | H 额度比 |
| `cost_est` / `cost_parts` | 影子金额与拆分 | 每单元成本 |
| `total_wall_s` | 墙钟 | 体验代价 |
| `error` / `plan_diag` | 失败原因（有就记） | 区分模型失败与管线失败 |

---

## 第二部分　操作过程（逐步照做）

> 所有命令都在 **Git Bash**、目录 `<REPO>` 下执行。
> 每一步都写了「预期输出」和「不符合时怎么办」——**不符合就停，不要往下走**。
> 预计总耗时 40–80 分钟，其中你真正动手的时间不到 10 分钟，其余是在等。

### 2.0　开工前的一次性准备（3 分钟）

**① 确认在正确的目录**

```bash
cd "<REPO>"
pwd && ls tasks | tr '\n' ' '
```

预期：`pwd` 结尾是 `orchestrator`，`ls tasks` 里能看到 `b1 b2 b3 t01 …`。
不在这个目录，后面所有相对路径都会错。

**② 确认 Python 与依赖**

```bash
python -V && python -c "import sys; print('第三方依赖:', [m for m in ('requests','pandas') if m in sys.modules] or '无')"
```

预期：`Python 3.13.x`，依赖一行显示「无」。这套脚本是**纯标准库**，不需要装任何东西。

**③ 设凭据（只影响 D 臂的 worker）**

```bash
export DEEPSEEK_KEY="<你的 key>"
python -c "import os; k=os.environ.get('DEEPSEEK_KEY',''); print('key 已设置，长度', len(k))"
```

预期：打印长度（不打印内容）。长度 0 就是没设上，**D 臂会在 30 秒内失败**。

**④ 开一份过程日志（强烈建议）**

```bash
mkdir -p runs/exp
TS=$(date +%m%d-%H%M)
echo "本次跑批：$TS" 
```

后面每条跑批命令都加 `2>&1 | tee runs/exp/log-$TS.txt`。
出问题时，这份日志比记忆可靠——它会记下每次的退出码与报错原文。

**⑤ 看一眼磁盘**

```bash
df -h /c | tail -1
```

每格会保留完整工作区（不删），三批两臂合计约 50–200 MB。空闲 < 2 GB 就先清一清。

---

### 步骤 1　冻结环境（1 分钟，必做）

**目的**：把"这次跑的是什么版本"钉死。跑批期间任何一项变了，数据就不是同一批。

```bash
python scripts/preflight.py
ls -t <HOME>/AppData/Local/OpenAI/Codex/bin/ | head -1
codex --version 2>/dev/null || <HOME>/AppData/Local/OpenAI/Codex/bin/$(ls -t <HOME>/AppData/Local/OpenAI/Codex/bin/ | head -1)/codex.exe --version
git describe --tags
```

**预期**

| 项 | 期望值 |
|---|---|
| preflight | 末行 `全部通过` 或逐项 OK |
| codex 安装目录 | 一个 16 位十六进制目录名（**记下来**） |
| codex 版本 | `codex-cli 0.167.0-alpha.3.2` |
| orch | `v1.9.4` 或 `v1.9.4-N-g…` |

把这四行抄进记录（手册第六部分那张表的"环境三行"）。

**不符合时**

- preflight 报"沙箱起不了子进程" → **停**。D 臂的 worker 一定起不来，跑了也是白跑。
- codex 版本不是 0.167 → **停**。参数面可能变了（比如又改 `wire_api`），先告诉我。

---

### 步骤 2　建题与双向自检（1 分钟）

**目的**：确认题目"不改必失败、照参考解必通过"。这一步不过，后面全是垃圾数据。

```bash
python scripts/make_track_b.py --force --verify
```

**预期（必须一字不差地出现这 6 行）**

```
[ok] b1：10 个单元，10 条验收
[ok] b2：12 个单元，12 条验收
[ok] b3：8 个单元，8 条验收
[verify] b1：seed 通过 0/10，ref 通过 10/10 → 合格
[verify] b2：seed 通过 0/12，ref 通过 12/12 → 合格
[verify] b3：seed 通过 0/8，ref 通过 8/8 → 合格
```

**怎么读这两行数字**：`seed 通过 0/N` = 不动代码一个都过不了（说明题有难度、判据有效）；
`ref 通过 N/N` = 照参考解改全过（说明题可解、判据不过严）。**两个都必须成立。**

**不符合时**

| 现象 | 含义 | 动作 |
|---|---|---|
| `seed 通过 1/N` 以上 | 判据太松，初始代码就能过 | 停，告诉我哪批 |
| `ref 通过 < N` | 判据与参考解不一致 | 停，贴出 `[verify]` 那行 |
| 目录已存在且没加 `--force` | 用的是旧题 | 加 `--force` 重建 |

`--force` 会删掉重建 `tasks/b1..b3`。**跑批之后再执行 `--force` 等于换了题库**，那批数据作废。

---

### 步骤 3　干跑（10 秒，不花钱）

**目的**：确认 runner 能读到题、算得出待跑格数。这一步绝不调用模型。

```bash
python scripts/exp_runner.py --arms A,D --tasks b1,b2,b3 --dry-run
```

**预期**

```
[22:22:43] 任务 3 · 臂 ['A', 'D'] · 重复 1 → 待跑 6 格
  b1     A  r1  ≤2400s
  b2     A  r1  ≤2400s
  b3     A  r1  ≤2400s
  b1     D  r1  ≤2400s
  b2     D  r1  ≤2400s
  b3     D  r1  ≤2400s
```

**不符合时**：报 `题库自检不通过` → 它会列出哪题缺什么（accept 为空 / seed 为空），回步骤 2。

---

### 步骤 4　先跑 b3 的 D 臂（4–8 分钟）

**为什么先这一格**：它同时验证三件最容易出错的事——**规划会不会拆成 8 个任务**、
**worker 会不会越界写别的文件**、**验收路径对不对**。8 个单元跑得快，错了也亏得少。

```bash
export DEEPSEEK_KEY="<key>"
TS=$(date +%m%d-%H%M)
python scripts/exp_runner.py --arms D --tasks b3 \
    --out runs/exp/track_b.jsonl 2>&1 | tee runs/exp/log-$TS.txt
```

**预期输出（4 行，最后一行给出结果）**

```
[22:40:01] 任务 1 · 臂 ['D'] · 重复 1 → 待跑 1 格
[22:40:01] (1/1) b3 臂 D r1
（这里是 4–8 分钟的等待，屏幕不动是正常的）
[22:46:30]     → resolved=True wall=381.2s tokens=...,...
[22:46:30] 完成。结果：runs\exp\track_b.jsonl
```

**等待期间你可以做什么**

- 3 分钟后另开一个窗口：`ls -dt runs/exp/b3/D/r1/.orch-runs/*/ | head -1`，
  出现目录说明计划已出、正在执行。
- 别关掉跑批窗口，也别在同一个仓库目录里做别的事。

**不符合时**

| 现象 | 真因 | 动作 |
|---|---|---|
| 30 秒内就返回、`resolved=False` | key 没设 / 429 / provider 配置 | 看 `log-*.txt` 末尾的 stderr |
| 15 分钟还没结束 | worker 卡住 | `tasklist //FI "IMAGENAME eq codex.exe"` 看有没有残留；必要时 `taskkill //F //IM codex.exe` 后重跑该格 |
| 结束但 `resolved=False` 且单元数 < 8 | patch 没应用上 | 看步骤 5 的第 2 条 |

---

### 步骤 5　检查 D 的现场（2 分钟，关键）

**目的**：确认"这件事真的是编排做的"，而不是碰巧通过。四条命令，逐条看。

**① 计划拆成了几个任务（应为 8）**

```bash
python -c "
import json,glob
f=sorted(glob.glob('runs/exp/b3/D/r1/.orch-runs/*/tasks.json'))[-1]
d=json.load(open(f,encoding='utf-8'))
print('计划任务数:', len(d['tasks']))
for t in d['tasks']: print(' ', t['id'], t['writes'])
"
```

预期：`计划任务数: 8`，每行一个 `src/loader_0X.py`，**互不重复**。

> 若显示 1 个任务：说明 goal 没触发拆分，这一格**作废**（不是模型不行，是题面没拆）。
> 保留现场，告诉我，我改题后重跑。

**② 每个任务是否都改在自己的文件里（写入集应互不重叠）**

```bash
python -c "
import json,glob
f=sorted(glob.glob('runs/exp/b3/D/r1/.orch-runs/*/trace.jsonl'))[-1]
bad=[]; changed={}
for line in open(f,encoding='utf-8'):
    e=json.loads(line); ev=e.get('event'); d=e.get('data',{})
    if ev=='task.verify' and d.get('ok') is False: bad.append(d.get('task_id'))
    if ev=='task.committed': changed[d.get('task_id')]=d.get('changed')
print('验收未过:', bad or '无')
for k,v in sorted(changed.items()): print(' ', k, '改了', v, '| 文件数', len(v or []))
"
```

预期（8 行任务，每行 1 个文件、互不重复）：

```
验收未过: 无
  t01 改了 ['src/loader_01.py'] | 文件数 1
  …
  t08 改了 ['src/loader_08.py'] | 文件数 1
```

> 注意事件字段是 `ok`（`task.verify` 的 `data.ok`），不是 `passed`。
> 用错字段不会报错，只会永远打印"无"——这类"看起来通过"的核对比报错更危险。

**③ 报告里的资源行有没有 token**

```bash
R=$(ls -dt runs/exp/b3/D/r1/.orch-runs/*/ | head -1); head -8 "$R/DISPATCH.md"
```

预期能看到两行关键信息：

```
执行者：codex-ds-flash / deepseek-flash
资源：模型调用 8；检查命令 8；token 输入 …（缓存 …）· 输出 … · 轮次 …
计划：8 任务 · 1 层 · t01…t08 · 1 次尝试
```

> 若写 `token 未采集`：说明这次没走 `--json`，成本算不出来，该格作废。

**④ 是否真的落了 patch**

```bash
ls -l runs/exp/b3/D/patches/ && wc -c runs/exp/b3/D/patches/*.patch
```

预期：一个非零字节的 `.patch`。

---

### 步骤 6　跑 b3 的 A 臂（5–12 分钟）

**目的**：同一批题的基线。A 臂让 Codex 一次做完全部 8 个单元，没有计划、不派 worker。

```bash
python scripts/exp_runner.py --arms A --tasks b3 \
    --out runs/exp/track_b.jsonl 2>&1 | tee -a runs/exp/log-$TS.txt
```

注意这里是 `tee -a`（追加），别覆盖上一份日志。

**预期**：同样的 4 行格式，耗时通常比 D 长（A 没有拆解，容易在几个文件之间来回）。

**A 臂特有的两个看点**

```bash
# A 臂没有 .exp-plan.json，应当"不存在"
ls runs/exp/b3/A/r1/.exp-plan.json 2>/dev/null || echo "A 臂无计划（正常）"

# A 臂的 patch 通常更大：它可能顺手改了别的
wc -c runs/exp/b3/A/patches/*.patch
```

---

### 步骤 7　补跑 b1、b2（20–35 分钟）

**先跑 b1（10 单元），再跑 b2（12 单元）**。别一次挂三批——中途出错时你分不清是哪批的问题。

```bash
python scripts/exp_runner.py --arms D --tasks b1 --out runs/exp/track_b.jsonl 2>&1 | tee -a runs/exp/log-$TS.txt
python scripts/exp_runner.py --arms A --tasks b1 --out runs/exp/track_b.jsonl 2>&1 | tee -a runs/exp/log-$TS.txt
python scripts/exp_runner.py --arms D --tasks b2 --out runs/exp/track_b.jsonl 2>&1 | tee -a runs/exp/log-$TS.txt
python scripts/exp_runner.py --arms A --tasks b2 --out runs/exp/track_b.jsonl 2>&1 | tee -a runs/exp/log-$TS.txt
```

**也可以一条命令跑完剩下的四格**（等价，但中途不好定位）：

```bash
python scripts/exp_runner.py --arms D,A --tasks b1,b2 \
    --out runs/exp/track_b.jsonl --resume 2>&1 | tee -a runs/exp/log-$TS.txt
```

`--resume` 会跳过 `track_b.jsonl` 里已有的格，不会重复跑 b3。

**每格的预期耗时**

| 批 | D | A |
|---|---|---|
| b1（10 单元） | 5–10 min | 6–15 min |
| b2（12 单元） | 6–12 min | 8–18 min |

b2 的 A 臂最慢——12 个文件一次改完，容易触发多轮。**超过 25 分钟就让它超时**（2400 秒会自己停），记 `timeout`，不要重跑到通过。

---

### 步骤 8　跑完先做三项核对（3 分钟）

**规矩**：先怀疑管线，再怀疑模型。这一周 5 次"模型失败"，最后全是脚本 bug。

**① 记录数对不对（应为 6 行）**

```bash
wc -l runs/exp/track_b.jsonl
```

**② 每格的单元数与通过数对不对**

```bash
python -c "
import json
for l in open('runs/exp/track_b.jsonl',encoding='utf-8'):
    r=json.loads(l); d=(r.get('score') or {}).get('detail') or []
    print(f\"{r['task']} {r['arm']}: 单元 {len(d)} 通过 {sum(1 for x in d if x.get('rc')==0)}\"
          f\" | resolved {r.get('resolved')} | {r.get('total_wall_s')}s | {r.get('error') or ''}\")
"
```

预期每行形如：`b3 D: 单元 8 通过 8 | resolved True | 381.2s | `。

**③ 异常格的处理对照**

| 现象 | 含义 | 动作 |
|---|---|---|
| 单元数 ≠ 10/12/8 | 验收命令没跑全 / patch 应用失败 | 看该行 `score.detail` 里是否有 `apply_ok:false` |
| `resolved=False` 但每单元 rc 都是 0 | 判定逻辑异常 | 停，把该行发我（这是管线 bug） |
| `error` 字段非空 | 计划没产出 / 超时 / 越界 | 见第四部分故障表 |
| D 的单元数少于 A | worker 漏了任务 | 看 `DISPATCH.md` 是否 `8/8 完成` |

---

### 步骤 9　出表与留档（2 分钟）

```bash
python scripts/report_b.py
```

预期两张表（数字是示意，不是结论）：

```
按格看（一格 = 一批 N 个单元）
批    臂        通过  单元数    总成本    每单元      墙钟       H 输入
b3    A            8       8    0.0900    0.0113      512s      310,000
b3    D            8       8    0.0500    0.0063      381s       36,000

按批对照（D ÷ A）
批        A 每单元    D 每单元      D/A   A 通过率   D 通过率  D 的 H 额度比
b3          0.0113      0.0063    0.56x       100%       100%            12%
```

**然后把这次的结果另存一份**（防止后续 `--resume` 追加时混进旧数据）：

```bash
cp runs/exp/track_b.jsonl runs/exp/track_b_$(date +%m%d-%H%M).jsonl
```

最后把两张表 + 步骤 1 的四行环境信息，抄进你的实验记录。

---

### 步骤 10　（可选）手工复现一个失败单元

只在你怀疑"验收判错了"时做。它能把"模型没做对"和"脚本读错了"分开。

```bash
# 以 b3 的 D 臂为例：取补丁 → 重建仓库 → 应用 → 单跑第 3 个单元
rm -rf /tmp/repro && cp -r tasks/b3/seed /tmp/repro && cd /tmp/repro
git init -q && git config user.email r@r && git config user.name r && git add -A && git commit -qm base
git apply --whitespace=nowarn "<REPO>/runs/exp/b3/D/patches/r1.patch"
python checks/unit_03.py; echo "退出码 $?"
```

预期：退出码 0 说明该单元确实被改对了；非 0 且输出 `[FAIL] …` 说明真的没做到。

---

### 2.9　中断、重跑与止损

| 场景 | 做法 |
|---|---|
| 想中途停 | `Ctrl+C`，然后 `taskkill //F //IM codex.exe` 清掉孤儿进程（不清会拖住下一批） |
| 单格失败想重跑 | ① 把该格从 `track_b.jsonl` 里删掉 ② `rm -rf runs/exp/<题>/<臂>/r1` ③ 重跑该格 |
| **不要**做的 | 反复重跑到通过。那是在挑好结果，会系统性高估通过率 |
| 连续 3 格 429 | 停 1 小时；仍失败则加 `--worker-l glm-flash` 换档（**换档就是换条件，要单独记一行**） |
| 跑批中途 codex 更新了 | **立即停批**，回步骤 1 重新冻结，之前的数据单独存放、不与新数据合并 |

**一次完整跑的时间线**（照这个估你什么时候能离开）

| 时间点 | 事 | 你能离开吗 |
|---|---|---|
| 0:00 | 步骤 1–3（冻结、建题、干跑） | 不能，3 分钟 |
| 0:05 | 步骤 4 启动 b3/D | 可以，6–8 分钟后回来 |
| 0:15 | 步骤 5 查现场 + 步骤 6 启动 b3/A | 可以，10 分钟后回来 |
| 0:30 | 步骤 7 启动 b1/D、b1/A | 可以，每格 5–15 分钟 |
| 1:00 | 启动 b2/D、b2/A | 可以，b2/A 最慢 |
| 1:30 | 步骤 8–9（核对、出表、留档） | 不能，5 分钟 |

### 2.10　PowerShell 版（与上面一一对应）

Windows 默认终端是 PowerShell，上面那些 `tr` / `export` / `$(date)` / `tee -a` 在 PowerShell 里**都不存在**
（报错形如：`tr : 无法将"tr"项识别为 cmdlet…`）。下面每一段都已实测可用。

**三条通用替换**

| bash 写法 | PowerShell 写法 |
|---|---|
| `export X=v` | `$env:X = "v"` |
| `$(date +%m%d-%H%M)` | `$TS = Get-Date -Format 'MMdd-HHmm'` |
| `cmd 2>&1 \| tee -a f` | `cmd 2>&1 \| Tee-Object -FilePath f -Append` |

**准备（对应 2.0）**

```powershell
cd "<HOME>\WorkBuddy\agent协同项目\orchestrator"
Get-ChildItem tasks -Name                       # 应看到 b1 b2 b3 t01…
python -V
$env:DEEPSEEK_KEY = "<你的 key>"
$env:DEEPSEEK_KEY.Length                        # 打印长度；0 就是没设上
$TS = Get-Date -Format 'MMdd-HHmm'
[math]::Round((Get-PSDrive C).Free / 1GB, 1)     # 空闲 GB，小于 2 先清一清
```

**步骤 1　冻结环境**

```powershell
python scripts\preflight.py
Get-ChildItem "$env:LOCALAPPDATA\OpenAI\Codex\bin" |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty Name
git describe --tags
```

**步骤 2　建题自检**

```powershell
python scripts\make_track_b.py --force --verify
```

**步骤 3　干跑**

```powershell
python scripts\exp_runner.py --arms A,D --tasks b1,b2,b3 --dry-run
```

**步骤 4　b3 的 D 臂**

```powershell
python scripts\exp_runner.py --arms D --tasks b3 `
    --out runs\exp\track_b.jsonl 2>&1 | Tee-Object -FilePath "runs\exp\log-$TS.txt"
```

行末的反引号是续行符；不习惯就写成一整行。

**步骤 5　现场核对（一条命令代替原来四段内联 Python）**

```powershell
python scripts\check_b.py --task b3 --arm D
```

预期每行打 ✓，末行 `结论：四项全过。这一格可以计入结果。`

> 做成脚本的两个原因：PowerShell 会吃掉内联 Python 的引号；trace 里的字段是 `ok`（不是 `passed`），
> 写错时不报错、只会永远打印"无"。两种坑都踩过，所以固化成脚本。

**步骤 6　b3 的 A 臂**

```powershell
python scripts\exp_runner.py --arms A --tasks b3 `
    --out runs\exp\track_b.jsonl 2>&1 | Tee-Object -FilePath "runs\exp\log-$TS.txt" -Append
```

**步骤 7　补 b1、b2**

```powershell
python scripts\exp_runner.py --arms D --tasks b1 --out runs\exp\track_b.jsonl 2>&1 | Tee-Object -FilePath "runs\exp\log-$TS.txt" -Append
python scripts\exp_runner.py --arms A --tasks b1 --out runs\exp\track_b.jsonl 2>&1 | Tee-Object -FilePath "runs\exp\log-$TS.txt" -Append
python scripts\exp_runner.py --arms D --tasks b2 --out runs\exp\track_b.jsonl 2>&1 | Tee-Object -FilePath "runs\exp\log-$TS.txt" -Append
python scripts\exp_runner.py --arms A --tasks b2 --out runs\exp\track_b.jsonl 2>&1 | Tee-Object -FilePath "runs\exp\log-$TS.txt" -Append
```

**步骤 8　三项核对**

```powershell
(Get-Content runs\exp\track_b.jsonl).Count      # 应为 6
python scripts\check_b.py --results runs\exp\track_b.jsonl
```

**步骤 9　出表与留档**

```powershell
python scripts\report_b.py
Copy-Item runs\exp\track_b.jsonl "runs\exp\track_b_$TS.jsonl"
```

**步骤 10　手工复现一个失败单元**

```powershell
Remove-Item -Recurse -Force C:\tmp\repro -ErrorAction SilentlyContinue
Copy-Item -Recurse tasks\b3\seed C:\tmp\repro
cd C:\tmp\repro
git init -q; git config user.email r@r; git config user.name r
git add -A; git commit -qm base
git apply --whitespace=nowarn "<HOME>\WorkBuddy\agent协同项目\orchestrator\runs\exp\b3\D\patches\r1.patch"
python checks\unit_03.py; "退出码 $LASTEXITCODE"
```

**中断与清进程**

```powershell
Get-Process codex -ErrorAction SilentlyContinue | Stop-Process -Force
```

PowerShell 5.1 **没有 `&&`**，多步用 `;` 串；`$LASTEXITCODE` 才是上一条外部命令的退出码。

## 第三部分　怎么判（预注册，跑完不改）

| 指标 | 达标 | 不达标 |
|---|---|---|
| **D 每单元成本 ÷ A 每单元成本** | **< 0.5** | ≥ 0.5 → 批量场景不成立 |
| **D 通过率** | ≥ A 的通过率 | 低了 → 省的是质量，不是资源 |
| **D 的 H 额度比** | < 0.3 | > 0.6 → 规划费没摊开，检查 N 是否太小 |
| 墙钟 | 记录即可 | 不作为判定 |

三种可能的结果，对应三种写法：

1. **成本 < 0.5 且通过率 ≥ A** → 批量同构是这套系统真正的落点，写进结论；
2. **成本 0.5–1.0** → 摊薄不足，说明 N=10 还不够；下一步把 N 抬到 20 或做计划复用，而不是否认机制；
3. **成本 > 1.0** → 推翻"批量能摊薄规划费"这个假设，项目只剩"体验保持"一条腿。

---

## 第四部分　故障对照表

| 现象 | 最可能的真因 | 动作 |
|---|---|---|
| 单元数少于 N | 验收命令生成少了 / patch 应用失败 | 看 `score.detail` 是否 `apply_ok:false` |
| `resolved=false` 但 detail 里 rc 全是 0 | 判定逻辑异常 | 报我，这是管线 bug |
| D 的计划任务数 = 1 | goal 没触发拆分 | 该批作废，改 goal 重跑 |
| 某单元 `ModuleNotFoundError` | 验收脚本没把仓库根加进 `sys.path` | 查 checks 里有没有 `sys.path.insert` |
| 连续 429 | L 侧限流 | 退避重试；持续失败换 `--worker-l glm-flash` |
| A 臂超时（>2400s） | 一次做 12 个单元太长 | 记 `timeout`，不要重跑到通过 |
| 中途 codex 版本变了 | 环境漂移 | **立即停批**，重跑 S0 |

**一条经验**：这周 5 次"模型失败"，最后全是管线 bug（消息截断、checks 路径错、rmtree 被拦、f-string 语法错误、孤儿进程）。
所以看到失败的**第一个动作是看 `score.detail` 与 `DISPATCH.md`，不是换模型重跑**。

---

## 第五部分　你需要自己记录的东西

跑批时手边留一张表，每格填一行（这些不进代码，进你的实验记录）：

| 批 | 臂 | 开始时间 | 是否通过 | 通过单元 | 备注（异常现象） |
|---|---|---|---|---|---|
| b3 | D | | | /8 | |
| b3 | A | | | /8 | |
| b1 | D | | | /10 | |
| b1 | A | | | /10 | |
| b2 | D | | | /12 | |
| b2 | A | | | /12 | |

外加三行环境：codex 版本、orch tag、DeepSeek 模型名。

---

## 第六部分　关于路由臂（本轮不做）

你提的"路由臂 vs 非路由，在多轮混合任务下对照"是对的方向，但要单独设计，原因：

1. **路由需要一个判断器**，它的错误率会成为新变量——分不清"机制好"还是"路由准"；
2. **多轮混合任务**意味着同一会话里既有 S 也有 L，还要看计划能否复用；这已经超出"一批同构"的范围，属于轨道 A 与 B 的交叉；
3. 顺序上必须先知道"批量到底成不成立"——路由的价值就是把非批量任务导回 A、把批量任务导给 D；B 不成立，路由就没有可路由的东西。

所以本轮只跑纯臂（A / D）。B 的结论出来之后，再设计路由臂：
**同一批混合任务（3 S + 3 M + 2 L 交错），路由版 vs 固定 D 版 vs 固定 A 版**，比"总成本 + 总通过率"。
到那时"路由判断错了会损失多少"才有基线可比。
