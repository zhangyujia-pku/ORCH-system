# 手动跑一遍（确认当前进度）

> **路径说明**：下文出现的 `<HOME>//...` 是作者本机示例，请替换成你自己的；
> `<hash>` 是 Codex 安装目录里的随机段。命令本身与路径无关。
>
> 第四步当前操作：执行前准备固定检查和执行策略；恢复沿用同一运行、参数与配置，资源不清零。详细限制见 [第四步说明](docs/第四步_恢复资源与异构闭环.md)。
>
> ```powershell
> python -m orch run RUN_ID --workers workers.json --execution examples/execution.example.json
> python -m orch cancel RUN_ID
> python -m orch answer RUN_ID t01 "用户回答的原话"
> python -m orch run RUN_ID --workers workers.json --execution examples/execution.example.json --resume
> ```
>
> `RUN_ID` 需替换。`--verifier`、`--verification`、`--timeout` 等也须与原执行一致。遇到配置变化、不确定合并或未确认停止的子进程时会阻断恢复。两个真实模型后端当前尚未联合验收，不能把下文历史成功经历当成本机当前可用证明。

> 第三步后的执行入口需要固定整体验收 JSON（`--verification <路径>` 或运行目录 `verification.json`）。配置格式与准备方式见 `docs/第三步_可信验收与整合.md`。
> 下文保留 M2 的历史走通流程；未提供检查配置的旧 run 命令现在会被阻断。验收应进入报告中的独立整合目录，原目标目录不会自动变成合并结果。

全程在 **cmd** 里操作，**不要用 PyCharm 的运行按钮**（工作目录会不对）。
每一段都可以直接复制。

---

## 0. 前置：git 和依赖（只做一次）

### 0.1 git —— 你的 cmd 里没有

先验：

```bat
git --version
```

如果报「不是内部或外部命令」，用下面这条**临时**补上（只对当前窗口有效，后续命令都在这个窗口跑）：

```bat
set PATH=%PATH%;<PortableGit 的 mingw64/bin 目录>
git --version
```

想永久生效，开 PowerShell 跑一次（**别用 setx**，它会把长 PATH 截断）：

```powershell
$p = [Environment]::GetEnvironmentVariable('Path','User')
SetEnvironmentVariable('Path', $p + ';<PortableGit 的 mingw64/bin 目录>', 'User')
```

然后重开 cmd。

> 没有 git 时 `orch run` 和 `sandbox_check.py` 都会直接报错退出——这不是 bug，是它在提醒你缺前置条件。

### 0.2 Python 依赖

你 cmd 里的 `python` 是 `D:\应用数据\Python\Python313`，它没装 pytest。
不装的话，验收器跑 `python -m pytest` 会失败，任务会被判"未通过"。

```bat
python -c "import sys;print(sys.executable)"
python -m pip install pytest flask
```

第二行要能打印出 `pytest` 和 `flask` 的版本。

---

## 1. 进目录 + 确认 codex 在 PATH

```bat
cd /d <orch 仓库目录>
where codex
```

`where codex` 有输出就不用管。没有的话，每次开新窗口前先设：

```bat
set CODEX_BIN=<你的 codex 可执行文件路径>
```

---

## 2. 沙箱体检（约 2 分钟）

```bat
python scripts\sandbox_check.py --timeout 90
```

**看到这个才算通过：**

```
  PASS  W 写工作目录      27s · 写入成功
  PASS  G git 能跑        29s · 拿到 HEAD ced9330（来自文件）
  PASS  E 越界写（负向）  32s · 已执行且被拦住

结论：修好了。能写、git 不卡、写不出工作目录。
```

`G` 那行必须出现一个**十六进制哈希**。没出现就说明 git 没真跑成——不是"还行"。
`E` 必须是"已执行且被拦住"，不是"没留下 marker"。

---

## 3. 单元测试（约 20 秒）

```bat
python -m unittest discover -s tests -t .
```

期望最后两行：

```
Ran <当前测试数> tests in ...
OK
```

---

## 4. 把 demo 仓库拉回干净基线

上一次跑完，它停在 `orch/run-...` 分支上了。

```bat
cd demo\tasks_app
git checkout master
git --no-pager log --oneline -1
cd ..\..
```

---

## 5. 提交一条需求

```bat
python -m orch intake "做三件互不相关的事：1) 给 storage.py 加持久化，每次写操作后把数据存到 tasks.json，启动时读回来；2) 给 cli.py 加 --verbose 选项，打印每次操作的耗时；3) 在 README 里加一节「数据文件」，说明 tasks.json 的位置和格式。" --repo demo/tasks_app --max-agents 3 --budget 6
```

会打印一个 run id，长这样：`20261106-024500-ab12`。**记下来。**

---

## 6. 规划（1–2 分钟）

```bat
python -m orch plan --provider codex --timeout 240
```

期望：

```
  PASS  attempt 1
  3 tasks · 1 layers · ... · t01,t02,t03
```

如果出现 `FAIL attempt 1` 然后 `PASS attempt 2`，也正常——那是校验器把不合格的判据打回去重写了。
多次校验仍未通过时停止规划，报告尚未执行；后端调用错误也可能提前停止。不会自动启动单 agent。

---

## 7. 看一眼计划（10 秒，这一步别跳）

```bat
python -m orch show
```

（不用手拼路径。想连 SPEC 一起看：`python -m orch show --spec`）

看三件事：

- 每个任务的 `writes` 是不是互不重叠
- 每个 `acceptance` 是不是能执行（有 `pytest ... 退出码 0` 或者 `包含 \`某字符串\``）
- 有没有莫名其妙的 `"**"`

---

## 8. 执行（2–3 分钟）

```bat
python -m orch run --provider codex --verifier codex --max-agents 3 --timeout 420
```

期望：

```
  DONE   t02  ...  2 文件
  DONE   t01  ...  2 文件
  DONE   t03  ...  1 文件

  合并到 orch/run-<run_id>
    ok   ...
    ok   ...
    ok   ...

  3/3 任务完成 · OK
```

---

## 9. 验证产物真的能用

```bat
cd demo\tasks_app
git --no-pager log --oneline -6
python -m pytest -q
```

> `git log` 直接跑可能报 `terminals database is inaccessible`——那是分页器找不到 terminfo，
> 不是仓库坏了。加 `--no-pager`，或先 `set GIT_PAGER=cat`。

要看的是：

- 有三条 `orch: merge ...` 的合并提交
- `pytest` 全部通过，**用例数应该比原来多**（worker 自己补了测试）
- 原来 4 个用例，跑完应该 ≥ 4

想看得更细：

```bat
git diff --stat master HEAD
type README.md
```

---

## 10. 看这一趟的账

```bat
cd ..\..
python -m orch status
```

会打印事件计数（`run.start` / `task.start` / `task.committed` / `task.verify` / `merge.result` / `run.end`）。

---

## 出问题对照表

| 症状 | 原因 | 怎么办 |
|---|---|---|
| `ModuleNotFoundError: No module named 'pytest'` | 用的 python 不对 | `python -m pip install pytest flask`，或 `python -c "import sys;print(sys.executable)"` 看是哪个 |
| `找不到可执行文件：codex` | 不在 PATH 上 | `set CODEX_BIN=...`（见第 1 步） |
| `branch already exists` | 旧 worktree 占着分支名 | 已按 run 隔离；仍出现就 `git worktree prune` |
| `3 次尝试均未通过` | 计划本身不合格 | 看 `runs\<id>\tasks.attempt1.raw.txt`，里面是模型原始输出 |
| 某个任务 `FAIL ... 越界 1 文件` | worker 改了 `writes` 之外的文件 | 这是设计：越界一律废掉，看 `runs\<id>\artifacts\<task>-a1.diff` |
| 某个任务 `未过 N 条` | 验收没过 | 看 `runs\<id>\artifacts\<task>-a1.verify.json` |
| 体检 `E` 说"没留下 marker" | 命令没跑起来 | 不是通过，是失败；回到沙箱那节 |

---

## 一句话判断"这次跑得好不好"

不是看它有没有"成功"，是看这四行：

1. 三个 `writes` 互不重叠 → 拆解合理
2. `3/3 任务完成` → 执行+验收闭环
3. `pytest` 用例数变多且全过 → 产出是真的
4. `越界 0 次` → 隔离有效
