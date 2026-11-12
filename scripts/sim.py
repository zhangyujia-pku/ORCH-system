"""仿真模式的旁路采集器：人负责输入与验收，脚本只做准备、采集、复核。

与 exp_runner 的区别（这是刻意的）：
  exp_runner  脚本自己喂 prompt 给 codex、自己跑验收 —— 那是"被脚本驱动的实验"
  sim.py      你（人）在 Codex 里输入需求，Codex 自己执行、自己跑验收；
              脚本只在事后把 token、补丁、复核结果抄下来

三个子命令：
  prep     从题库建一个干净仓库（一格一个），记下基线 sha
  goal     打印这道题的原话（你复制到 Codex 里）
  collect  采集一格：解析会话用量 + 取补丁 + 独立复核 + 追加一行结果

用法：
  python scripts/sim.py prep    --task b3 --arm A
  python scripts/sim.py goal    --task b3
  python scripts/sim.py collect --task b3 --arm A --session runs/exp/sess-A-b3.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
import unicodedata

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.exp_lib import cost_of, load_tasks, score          # noqa: E402
from orch.planner import parse_json_events                       # noqa: E402

RUNS = ROOT / "runs" / "exp"
PRICES = json.loads((ROOT / "config" / "prices.json").read_text(encoding="utf-8"))
H_MODEL = "gpt-6.1-sol"
L_MODEL = "deepseek-flash"          # 仿真模式下 worker 档位；换档就改这一行并记录



# 两条臂的原话必须不同，否则 D 会退化成 A。
# 共同部分：明确工作目录 + 要求它自己验收 + 禁止改判据。
COMMON = """

你现在所在的目录就是这个仓库的根目录（里面有 src/ 与 checks/），只改动这个目录下的文件。
完成后请自己运行验收命令（checks/unit_XX.py 全部跑一遍），把每个单元的结论或退出码贴出来。
不要修改 checks/ 下的文件——那是判定标准，改了也不算数。
"""

# D 臂额外这段：告知编排能力存在，但不强制。
# 不写这段，"Codex 用不用编排"就无从比较——它会像 A 一样直接做。
ORCH_HINT = """

这个仓库的任务量不小，你可以使用已安装的 orch 编排能力来拆分与并行执行
（命令 `orch --help`，或 `python -m orch`；典型用法是先写计划再 dispatch）。
用不用由你判断：如果认为直接做更合适，也可以直接做，但请在结尾说明你的选择理由。
"""


def goal_text(task, arm: str = "A") -> str:
    arm = (arm or "A").upper()
    body = task.goal + COMMON
    if arm in ("C", "D"):
        body += ORCH_HINT
    return body

def git(repo: pathlib.Path, *args: str) -> str:
    # 一律按 UTF-8 解码：中文 Windows 默认 gbk，读 UTF-8 的 diff 会直接抛解码错。
    r = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip())
    return r.stdout.strip()


def cell_of(task: str, arm: str, rep: int) -> pathlib.Path:
    return RUNS / task / arm.upper() / f"r{rep}"


# ---------------------------------------------------------------- prep


def _ensure_exclude(cell: pathlib.Path) -> None:
    """把实验专用文件挡在版本控制外。

    否则 Codex 一旦 `git add -A`，GOAL.txt 与 .sim-base 会进补丁，
    评分时被当成"多出来的改动"。已存在的格子也要补一次（历史遗留）。
    """
    exclude = cell / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    with exclude.open("a", encoding="utf-8") as f:
        for name in ("GOAL.txt", ".sim-base"):
            if name not in text:
                f.write(f"\n{name}\n")


# ---------------------------------------------------------------- 客户端会话

SESSIONS = pathlib.Path.home() / ".codex" / "sessions"



def _last_assistant(path: pathlib.Path) -> str:
    """取会话里最后一条助手消息的正文（用于记录"它自己怎么说"）。"""
    text = ""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("type") != "response_item":
                    continue
                pl = e.get("payload") or {}
                if pl.get("type") != "message" or pl.get("role") != "assistant":
                    continue
                parts = []
                for c in pl.get("content") or []:
                    if isinstance(c, dict) and c.get("text"):
                        parts.append(c["text"])
                if parts:
                    text = "\n".join(parts)
    except OSError:
        pass
    return text

def recent_rollouts(limit: int = 12) -> list[dict]:
    """最近的客户端会话（rollout）。

    为什么需要它：客户端跑批拿不到 `--json` 事件流，但每次会话都会落一份
    rollout-*.jsonl，里面有 session_meta.cwd 与累计 token。按这个取数，
    客户端跑出来的格子也能算成本。
    """
    out = []
    if not SESSIONS.exists():
        return out
    files = sorted(SESSIONS.rglob("rollout-*.jsonl"),
                   key=lambda x: x.stat().st_mtime, reverse=True)[:limit * 3]
    for f in files:
        cwd, usage, turns = "", {}, 0
        try:
            with f.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    t = e.get("type")
                    pl = e.get("payload") or {}
                    if t == "session_meta" and not cwd:
                        cwd = str(pl.get("cwd") or "")
                    elif t == "turn_context":
                        turns += 1
                    elif t == "event_msg" and pl.get("type") == "token_count":
                        u = (pl.get("info") or {}).get("total_token_usage") or {}
                        if u:
                            usage = u
        except OSError:
            continue
        out.append({"path": f, "mtime": f.stat().st_mtime, "cwd": cwd,
                    "turns": turns, "usage": usage, "message": _last_assistant(f)})
        if len(out) >= limit:
            break
    return out


def find_rollout(cell: pathlib.Path, arg: str) -> dict | None:
    """支持三种写法：绝对路径 / 序号（sessions 列表里的）/ auto（按 cwd 匹配）。"""
    rows = recent_rollouts(30)
    if not rows:
        return None
    if arg and arg not in ("auto",):
        p = pathlib.Path(arg)
        if p.exists():
            for r in rows:
                if r["path"] == p:
                    return r
            return {"path": p, "mtime": p.stat().st_mtime, "cwd": "", "turns": 0, "usage": {}}
        if arg.isdigit():
            i = int(arg)
            if 1 <= i <= len(rows):
                return rows[i - 1]
        return None
    want = str(cell).lower()
    for r in rows:                       # 先按 cwd 精确匹配这一格
        if r["cwd"].lower() == want:
            return r
    return None

def cmd_prep(args) -> int:
    task = {t.id: t for t in load_tasks([args.task])}[args.task]
    cell = cell_of(args.task, args.arm, args.rep)

    # --goal-only 要放在"已存在就跳过"之前判断，否则永远走不到。
    if getattr(args, "goal_only", False):
        if not (cell / ".sim-base").exists():
            print(f"[✗] {cell} 还不是一格实验仓库；先不带 --goal-only 跑一次。")
            return 2
        (cell / "GOAL.txt").write_text(goal_text(task, args.arm), encoding="utf-8")
        _ensure_exclude(cell)
        print(f"[ok] 只更新了原话：{cell / 'GOAL.txt'}（仓库未改动）")
        return 0

    if cell.exists() and any(cell.iterdir()):
        if not args.force:
            base = (cell / ".sim-base").read_text().strip() if (cell / ".sim-base").exists() else "未记录"
            print(f"[skip] {cell} 已存在（--force 重建）")
            print(f"       基线 sha：{base}")
            print("       只想刷新原话：加 --goal-only")
            return 0
        # 不删：改名归档，保留现场（宿主的批量删除保护也会拦 rmtree）
        try:
            cell.rename(cell.with_name(f"{cell.name}.old{int(time.time())}"))
        except PermissionError:
            print(f"[✗] {cell} 被占用（通常是 Codex 窗口/进程还开着它）。")
            print("    关掉那个会话再试；或：prep --force --goal-only 只重写 GOAL.txt，不动仓库。")
            return 3

    cell.parent.mkdir(parents=True, exist_ok=True)
    cell.mkdir(parents=True, exist_ok=True)

    # 从 seed 铺文件（不复制 .git），再自己 init
    for item in task.seed.iterdir():
        if item.name == ".git":
            continue
        dst = cell / item.name
        if item.is_dir():
            import shutil
            shutil.copytree(item, dst)
        else:
            import shutil
            shutil.copy2(item, dst)

    for a in (["init", "-q"], ["config", "user.name", "sim"],
              ["config", "user.email", "sim@example.local"], ["add", "-A"],
              ["commit", "-qm", f"init: {task.id}"]):
        git(cell, *a)
    _ensure_exclude(cell)
    base = git(cell, "rev-parse", "HEAD")
    (cell / ".sim-base").write_text(base, encoding="utf-8")
    (cell / "GOAL.txt").write_text(goal_text(task, args.arm), encoding="utf-8")
    print(f"[ok] 仓库就绪：{cell}")
    print(f"     基线 {base[:8]} · {len(list(cell.rglob('*.py')))} 个 py 文件 · 验收 {len(task.accept)} 条")
    print("\n下一步：在该目录里启动 Codex，把 GOAL.txt 的内容发给它。")
    return 0


# ---------------------------------------------------------------- goal

def cmd_goal(args) -> int:
    task = {t.id: t for t in load_tasks([args.task])}[args.task]
    print(goal_text(task, args.arm))
    print("# ---- 以上为原文（同样写在 <格子>/GOAL.txt 里）----")
    print("# A 臂：直接发；D 臂：原样发，让 Codex 自己决定是否调用 orch。")
    return 0


# ---------------------------------------------------------------- collect

def read_session(path: pathlib.Path) -> dict:
    """解析 codex exec --json 的会话：最终消息 + 用量 + 轮次。"""
    text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    # 终端里 `>` / `Out-File -Encoding utf8` 会带 BOM（PS 5.1 的行为），
    # 不去掉的话第一行不是以 "{" 开头，会被当成噪声跳过。
    text = text.lstrip("\ufeff")
    message, usage = parse_json_events(text)
    return {"message": message, "usage": usage or {}, "turns": (usage or {}).get("turns", 0),
            "raw_lines": len([1 for x in text.splitlines() if x.strip()])}


def orch_usage(cell: pathlib.Path) -> dict:
    """D 臂：worker 的用量在 .orch-runs 的 trace 里，单独记一栏。"""
    runs = cell / ".orch-runs"
    if not runs.is_dir():
        return {}
    latest = max(runs.glob("*"), key=lambda p: p.stat().st_mtime, default=None)
    if latest is None:
        return {}
    usage = {}
    for line in (latest / "trace.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("event") == "run.usage":
            usage = e.get("data") or {}
    # trace 里叫 tokens_in / tokens_cached / tokens_out，而计价函数吃
    # input_tokens / cached_input_tokens / output_tokens。不转换的话
    # cost_of 找不到键、直接返回 None 或 0——现象是"编排的 worker 不花钱"，
    # 整批成本被严重低估（踩过）。
    if usage:
        usage = {
            "input_tokens": usage.get("tokens_in", 0),
            "cached_input_tokens": usage.get("tokens_cached", 0),
            "output_tokens": usage.get("tokens_out", 0),
            "reasoning_output_tokens": usage.get("tokens_reasoning", 0),
            "turns": usage.get("turns", 0),
        }
    return usage


def claimed_verdict(message: str) -> str:
    """从 Codex 的最后一段话里看它自己怎么报验收结果。

    只记录、不采信——真正的判定用下面 score() 独立跑一遍。
    "它说通过"和"实际通过"不一致，本身是实验要看的东西。
    """
    if not message:
        return "未汇报"
    tail = message[-800:]
    # 先判否定，再判肯定——否则"未通过"会被"通过"命中。
    # 不要用裸"失败"：需求原文里就有"三次失败就抛出"，会误判。
    if any(k in tail for k in ("验收未通过", "不通过", "不合格", "存在失败", "FAILED")):
        return "自称未通过"
    if any(k in tail for k in ("全部合格", "全部通过", "均通过", "所有验收通过",
                               "8/8", "12/12", "10/10")):
        return "自称通过"
    return "未表态"



def orch_status(cell: pathlib.Path) -> tuple[str, str]:
    """读最新一次 orch 报告的首行，判断是"执行成功"还是"执行失败"。

    为什么必须记：编排失败、模型随后手工补完，最终验收也可能是 8/8——
    但那是**混合链路**，不能算"编排做成了"。这一列不记，事后分不清。
    """
    runs = cell / ".orch-runs"
    if not runs.is_dir():
        return "无编排", ""
    latest = max(runs.glob("*"), key=lambda p: p.stat().st_mtime, default=None)
    if latest is None:
        return "无编排", ""
    dis = latest / "DISPATCH.md"
    if not dis.exists():
        return "无报告", str(latest)
    first = dis.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    if "执行成功" in first:
        return "成功", str(latest)
    if "执行失败" in first:
        return "失败", str(latest)
    return first.strip()[:40], str(latest)

def cmd_collect(args) -> int:
    # 先对表：题库必须还是冻结时那一把，否则这一格写进去就会污染整批数据。
    from scripts.freeze_taskset import verify_hash
    ok, info = verify_hash([args.task])
    if not ok:
        print(f"[✗] 题库已变：{info}")
        print("    按规则：改动题库要升版本并声明旧数据作废。当前不采集。")
        return 3
    frozen = json.loads((ROOT / "tasks" / "FREEZE.json").read_text(encoding="utf-8"))

    task = {t.id: t for t in load_tasks([args.task])}[args.task]
    cell = cell_of(args.task, args.arm, args.rep)
    if not (cell / ".sim-base").exists():
        print(f"[✗] {cell} 不是一格实验仓库（缺 .sim-base）。先用 sim.py prep 建。")
        return 2
    base = (cell / ".sim-base").read_text(encoding="utf-8").strip()

    start = time.time()
    # 1) 补丁：基线..当前工作区（含已提交与未提交）
    patch = git(cell, "diff", base)
    patch = patch + "\n" if patch and not patch.endswith("\n") else patch
    pdir = RUNS / args.task / args.arm.upper() / "patches"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / f"r{args.rep}.patch").write_text(patch, encoding="utf-8")

    # 2) 独立复核：干净仓库 + 只应用补丁 + 题库 checks
    result = score(task, patch)
    detail = result.get("detail") if isinstance(result.get("detail"), list) else []
    units_ok = sum(1 for d in detail if d.get("rc") == 0)

    # 3) 会话用量
    roll = None
    if getattr(args, "rollout", ""):
        roll = find_rollout(cell, args.rollout)
        if roll is None:
            print(f"[✗] 找不到会话：{args.rollout}。先跑 `python scripts/sim.py sessions` 看序号。")
            return 4

    if args.session:
        sess = read_session(pathlib.Path(args.session))
        channel = "cli"
    elif roll:
        sess = {"message": roll.get("message", ""), "usage": roll["usage"],
                "turns": roll["turns"], "raw_lines": 0}
        channel = "client"
    else:
        sess = {"message": "", "usage": {}, "turns": 0, "raw_lines": 0}
        channel = "unknown"
    ses_msg = sess.get("message") or ""

    o_status, o_dir = orch_status(cell)

    # A 臂的体检：出现 .orch-runs 就说明它偷偷用了编排，这一格不再是 A。
    contaminated = False
    if args.arm.upper() == "A" and (cell / ".orch-runs").exists():
        contaminated = True
        print("[✗] 这一格出现了 .orch-runs —— A 臂被编排污染了，不能当作\"Codex 单干\"。")
        print("    原因通常是 orch 技能没停用。这一格只作废，不计入结果。")
    worker = orch_usage(cell)

    # 4) 成本：H 侧（会话）+ L 侧（D 的 worker）；拿不到就 None
    h_cost = cost_of(sess["usage"] or {}, H_MODEL, PRICES)
    l_cost = cost_of(worker or {}, L_MODEL, PRICES) if worker else 0.0
    total = None if (h_cost is None and not worker) else (h_cost or 0.0) + (l_cost or 0.0)

    row = {
        "task": args.task, "arm": args.arm.upper(), "rep": args.rep,
        "mode": "sim",
        "taskset": {"version": frozen["version"], "hash": frozen["total_hash"]},
        "started": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(start)),
        "usage": sess["usage"], "turns": sess["turns"],
        "worker_usage": worker,
        "resolved": result.get("resolved", False),
        "score": result,
        "units": len(detail), "units_ok": units_ok,
        "claimed": claimed_verdict(sess["message"]),
        "patch_bytes": len(patch.encode("utf-8")),
        "cost_est": None if total is None else round(total, 6),
        "cost_parts": {"h": h_cost, "worker": l_cost},
        "session": args.session or "",
        "channel": channel,
        "skill": getattr(args, "skill", "") or "未声明",
        # 控制变量：orch 命令当时在不在 PATH 上。
        # 靠"记得停用技能"是不可靠的——实测 CLI 也会直接调 orch 命令。
        "orch_available": bool(shutil.which("orch")),
        "contaminated": contaminated,
        # 条件不成立 ≠ 模型没做对。resolved 保留实测值，另设 invalid 标记，
        # 汇总时排除。之前是在写盘之后才改 resolved，导致记录里仍写着 True。
        "invalid": contaminated,
        "invalid_reason": ("A 臂出现 .orch-runs：编排介入，不再是'Codex 单干'"
                           if contaminated else ""),
        "orch_status": o_status,
        "orch_run": o_dir,
        "rollout": str(roll["path"]) if roll else "",
        "rollout_cwd": (roll or {}).get("cwd", ""),
        "note": args.note or "",
    }
    out = pathlib.Path(args.results)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

    if contaminated:
        print(f"任务 {row['task']} 臂 {row['arm']}：[作废] 编排污染，条件不成立（记录保留，汇总时排除）")
    print(f"任务 {row['task']} 臂 {row['arm']}："
          f"单元 {units_ok}/{len(detail) or '?'} · resolved {row['resolved']}")
    print(f"  Codex 自报：{row['claimed']}")
    print(f"  H 输入 {sess['usage'].get('input_tokens', 0):,}（缓存 "
          f"{sess['usage'].get('cached_input_tokens', 0):,}）· 输出 "
          f"{sess['usage'].get('output_tokens', 0):,} · 轮次 {sess['turns']}")
    if worker:
        print(f"  worker 输入 {worker.get('tokens_in', 0):,} · 输出 {worker.get('tokens_out', 0):,}")
    if row["patch_bytes"] < 200:
        print("  ↳ [注意] 补丁几乎为空：大概率是 Codex 的工作目录不在这一格"
              "（客户端要先打开该文件夹）。这一格不能计入结果。")
    if row["arm"] == "D":
        print(f"  编排状态：{o_status}")
        if o_status == "失败" and row["resolved"]:
            print("  ↳ [注意] 编排失败但验收通过——多半是模型随后手工补的，"
                  "属于混合链路；建议在记录里标注，别当\"编排做成了\"。")
    print(f"  补丁 {row['patch_bytes']} 字节 · 估算成本 "
          f"{'未配价' if row['cost_est'] is None else f'${row["cost_est"]:.4f}'}")
    print(f"  已追加 → {out}")
    # 乱码兜底：如果需求是用错误编码读进去的，模型收到的是乱码，这次结果不该算数。
    if ses_msg.count("\ufffd") > 5:
        print("  ↳ [注意] 会话里出现大量乱码字符（\ufffd），输入编码可能错了；这次结果存疑。")
    if row["claimed"] == "自称通过" and not row["resolved"]:
        print("  ↳ [注意] 自报通过但独立复核未过——这正是实验要记录的分歧，不要改判。")
    return 0



def _arm(v: str) -> str:
    """--arm 只接受 A/B/C/D。大小写都行，写错时把可选值列出来。"""
    u = v.strip().upper()
    if u not in ("A", "B", "C", "D"):
        raise argparse.ArgumentTypeError(f"臂只能是 A / B / C / D，收到 {v!r}")
    return u


def _task(v: str) -> str:
    """--task 校验：题库里没有就直接说有哪些。"""
    ids = sorted(x.name for x in (ROOT / "tasks").iterdir()
                 if (x / "meta.json").exists())
    if v not in ids:
        raise argparse.ArgumentTypeError(f"没有这道题：{v!r}；可用：{', '.join(ids) or '（题库为空）'}")
    return v


def cmd_print(args) -> int:
    """打印"你该敲的那条命令"，不执行。

    为什么要有这个：codex 不在 PATH 上，路径在 AppData 下还带哈希目录；
    直接手打容易错。脚本只负责把路径和引号拼对，模型仍由你自己启动。
    """
    from orch.planner import resolve_codex
    cell = cell_of(args.task, args.arm, args.rep)
    goal = cell / "GOAL.txt"
    out = RUNS / f"sess-{args.arm.upper()}-{args.task}-r{args.rep}.jsonl"
    exe = resolve_codex()
    # 用分隔线而不是空行：某些终端会把空行折叠掉，导致两行命令粘在一起。
    sep = "-" * 66
    print(sep)
    print("# ① 进入这一格")
    print(f'cd "{cell}"')
    print(sep)
    print("# ② 把需求喂给 Codex（stdin 传入，避免引号问题），事件流写进会话文件")
    print(f'Get-Content "{goal}" -Raw -Encoding utf8 | & "{exe}" exec --json '
          f'--dangerously-bypass-approvals-and-sandbox -C . | Out-File -FilePath "{out}" -Encoding utf8')
    print(sep)
    print("# ③ 回到仓库根，采集（取补丁 + 独立复核）")
    print(f'cd "{ROOT}"')
    print(f'python scripts/sim.py collect --task {args.task} --arm {args.arm.upper()} '
          f'--session "{out}"')
    print(sep)
    print(f"# 会话文件：{out}")
    print("# 客户端窗口跑也可以（更贴近真实使用），但拿不到 token，collect 时不要给 --session。")
    return 0


def cmd_sessions(args) -> int:
    rows = recent_rollouts(args.limit)
    if not rows:
        print("没有找到客户端会话（~/.codex/sessions）。")
        return 1
    print(f"{'#':<3}{'时间':<10}{'轮次':>4}{'输入':>12}{'缓存':>12}{'输出':>8}  cwd")
    for i, r in enumerate(rows, 1):
        u = r["usage"] or {}
        print(f"{i:<3}{time.strftime('%H:%M:%S', time.localtime(r['mtime'])):<10}"
              f"{r['turns']:>4}{u.get('input_tokens', 0):>12,}"
              f"{u.get('cached_input_tokens', 0):>12,}{u.get('output_tokens', 0):>8,}"
              f"  {r['cwd'][:58]}")
    print("\n用序号取数：python scripts/sim.py collect --task b3 --arm A --rollout <#>")
    return 0


def cmd_discard(args) -> int:
    """把某一格标记为作废，但保留记录。

    为什么不删行：删掉就再也说不清"跑过几次、为什么废"。标记作废既留住证据，
    又不会污染汇总（report 只算 invalid != true 的行）。
    """
    path = pathlib.Path(args.results)
    if not path.exists():
        print(f"（{path} 不存在）")
        return 1
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    hit = None
    for r in reversed(rows):
        if (r.get("task") == args.task and r.get("arm") == args.arm.upper()
                and r.get("rep", 1) == args.rep and not r.get("invalid")):
            hit = r
            break
    if hit is None:
        print("没有找到可作废的记录（可能还没采集，或已经标过）。")
        return 1
    hit["invalid"] = True
    hit["invalid_reason"] = args.reason
    hit["invalid_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")
    print(f"已标记作废：{args.task} {args.arm.upper()} r{args.rep}"
          f"（{hit.get('units_ok')}/{hit.get('units')}）· 原因：{args.reason}")
    print("记录保留，汇总时不计入。")
    return 0



def pad(text: str, width: int, right: bool = False) -> str:
    """按显示宽度补齐（中文占两格）。"""
    w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    fill = " " * max(0, width - w)
    return fill + text if right else text + fill


def cmd_summary(args) -> int:
    """按金额口径汇总，并按"保本单元数"给预判。

    为什么不能用 token 比：A 用的是 H（$2/百万输入），worker 用的是 L
    （$0.15/百万，缓存 $0.003）。同样一件事，L 的 token 计数本来就是 H 的 2 倍多，
    单价又差 13 倍——拿原始 token 相除，得到的是"两种货币的汇率"，不是成本。
    实测教训：b3 上 token 差 19.6 倍，折成钱只差 -45%（worker 更便宜）。
    """
    path = pathlib.Path(args.results)
    if not path.exists():
        print(f"（{path} 不存在）")
        return 1
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip() and not json.loads(l).get("invalid")]
    if not rows:
        print("（没有有效记录）")
        return 1

    by: dict[str, dict] = {}
    for r in rows:
        by.setdefault(r["task"], {})[r["arm"]] = r

    print("每单元成本（金额口径 · 美元）")
    hdr = (pad("题", 6) + pad("N", 5, True) + pad("A/单元", 12, True)
           + pad("worker/单元", 16, True) + pad("计划", 12, True)
           + pad("保本N", 10, True) + pad("D/A", 9, True) + "  预判")
    print(hdr)
    print("-" * 84)

    for task in sorted(by):
        cell = by[task]
        a, d = cell.get("A"), cell.get("D")
        n = (a or d or {}).get("units") or 8

        a_per = (a.get("cost_est") / n) if (a and a.get("cost_est")) else None

        # D 的"执行成本"只看 worker；"计划"是 H 那一侧。
        # 若这一格根本没编排（worker=0），H 的消耗是它自己干活，不是计划费，不能混算。
        d_worker = d_plan = None
        d_ran = False
        if d:
            parts = d.get("cost_parts") or {}
            d_ran = (parts.get("worker") or 0) > 0
            d_plan = parts.get("h")
            if d_ran:
                d_worker = parts["worker"] / n

        bn, verdict = None, ""
        if not d:
            verdict = "只跑了 A"
        elif not d_ran:
            verdict = "D 未编排（算不出）"
        elif a_per is None or d_worker is None or d_plan is None:
            verdict = "缺数据"
        else:
            save = a_per - d_worker
            if save <= 0:
                verdict = "执行就亏，别跑"
            else:
                bn = d_plan / save
                verdict = (f"N≥{bn:.0f} 才保本" if bn > 20 else "值得一跑")

        ratio = None
        if d and a and d.get("cost_est") and a.get("cost_est") and d_ran:
            ratio = d["cost_est"] / a["cost_est"]

        print(pad(task, 6) + pad(str(n), 5, True)
              + pad("—" if a_per is None else f"{a_per:.5f}", 12, True)
              + pad("—" if d_worker is None else f"{d_worker:.5f}", 16, True)
              + pad("—" if d_plan is None else f"{d_plan:.5f}", 12, True)
              + pad("—" if bn is None else f"{bn:.1f}", 10, True)
              + pad("—" if ratio is None else f"{ratio:.2f}x", 9, True)
              + "  " + verdict)

    # 历史编排样本（唯一一次真的派了 worker 的运行），作为阈值来源展示
    ref = RUNS / "ref-orch-b3.json"
    if ref.exists():
        r = json.loads(ref.read_text(encoding="utf-8"))
        c = r["cost"]
        print(f"\n参考样本：{r['task']} 强制编排（{r['workers_attempted']} 个 worker / "
              f"{r['units_committed']} 个落库）")
        print(f"  worker 合计 ${c['worker_total']:.4f} · 按尝试每单元 ${c['worker_per_attempt']:.5f}"
              f" · 按落库每单元 ${c['worker_per_committed']:.5f}")
        print(f"  计划一次 ${c['plan_total']:.4f}（H 侧）")
        ua = (by.get(r["task"], {}).get("A") or {}).get("usage") or {}
        if ua.get("input_tokens"):
            print(f"  对照 token：worker 每单元 {r['worker_usage']['input_tokens']//r['workers_attempted']:,}"
                  f" vs A 每单元 {ua['input_tokens']//max(r['units_declared'],1):,}"
                  f" → 差 {r['worker_usage']['input_tokens']//r['workers_attempted']/(ua['input_tokens']//max(r['units_declared'],1)):.1f}×"
                  f"，但折价后只有 {c['worker_per_attempt']/(by[r['task']]['A']['cost_est']/r['units_declared'])-1:+.0%}")
            # 订阅视角：H 的实际成本不是 $2/百万（包月已付），真正稀缺的是额度。
            # 这一栏与金额无关，且只拿 H 跟 H 比，不存在汇率问题。
            h_share = r["plan_usage"]["input_tokens"] / ua["input_tokens"]
            print(f"  订阅视角：D 的 H 额度只用了 A 的 {h_share:.0%}"
                  f"（计划 {r['plan_usage']['input_tokens']:,} vs A {ua['input_tokens']:,}，同模型相比）")

    print("\n判据：预判为「值得一跑」才跑 D；「执行就亏」或保本 N 远超实际批量 → 不跑。")
    print("注意：token 数不能跨模型比。A 是 H，worker 是 L，单价差 13 倍、记账口径也差 2 倍多。")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        epilog="例：python scripts/sim.py prep --task b3 --arm A"
               "  |  python scripts/sim.py collect --task b3 --arm A --session runs/exp/sess.jsonl")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("prep", help="建一格实验仓库")
    p1.add_argument("--task", required=True, type=_task)
    p1.add_argument("--arm", required=True, type=_arm, help="A / B / C / D")
    p1.add_argument("--rep", type=int, default=1)
    p1.add_argument("--force", action="store_true")
    p1.add_argument("--goal-only", dest="goal_only", action="store_true",
                    help="只重写 GOAL.txt，不动仓库（目录被占用时用）")
    p1.set_defaults(fn=cmd_prep)

    p2 = sub.add_parser("goal", help="打印这道题的原话")
    p2.add_argument("--task", required=True, type=_task)
    p2.add_argument("--arm", default="A", type=_arm, help="A 臂与 D 臂的原话不同")
    p2.set_defaults(fn=cmd_goal)

    p7 = sub.add_parser("summary", help="按新判定规则汇总（先判该不该编排）")
    p7.add_argument("--results", default=str(RUNS / "sim.jsonl"))
    p7.set_defaults(fn=cmd_summary)

    p6 = sub.add_parser("discard", help="把某一格标记作废（保留记录）")
    p6.add_argument("--task", required=True, type=_task)
    p6.add_argument("--arm", required=True, type=_arm)
    p6.add_argument("--rep", type=int, default=1)
    p6.add_argument("--reason", required=True)
    p6.add_argument("--results", default=str(RUNS / "sim.jsonl"))
    p6.set_defaults(fn=cmd_discard)

    p5 = sub.add_parser("sessions", help="列出最近的客户端会话（取 token 用）")
    p5.add_argument("--limit", type=int, default=12)
    p5.set_defaults(fn=cmd_sessions)

    p4 = sub.add_parser("cmd", help="打印这一格该敲的命令（不执行）")
    p4.add_argument("--task", required=True, type=_task)
    p4.add_argument("--arm", required=True, type=_arm)
    p4.add_argument("--rep", type=int, default=1)
    p4.set_defaults(fn=cmd_print)

    p3 = sub.add_parser("collect", help="采集一格")
    p3.add_argument("--task", required=True, type=_task)
    p3.add_argument("--arm", required=True, type=_arm, help="A / B / C / D")
    p3.add_argument("--rep", type=int, default=1)
    p3.add_argument("--session", default="", help="codex exec --json 的输出文件")
    p3.add_argument("--rollout", default="",
                    help="客户端会话：序号（先跑 sessions 看）或路径；auto=按 cwd 匹配本格")
    p3.add_argument("--skill", choices=["on", "off"], default="",
                    help="这一臂是否启用了 orch 技能（实验条件，必须如实记）")
    p3.add_argument("--results", default=str(RUNS / "sim.jsonl"))
    p3.add_argument("--note", default="")
    p3.set_defaults(fn=cmd_collect)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
