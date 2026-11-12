"""批量跑实验：四臂 × 题库。一条命令，断了能续。

分工：这个脚本负责"跑和记"，不负责"判断"。判定标准全在题库的 checks 里，
样本量、排除、结论由人定。

用法：
    python scripts/exp_runner.py --arms A,B,C,D --tasks t01,t02 --dry-run
    python scripts/exp_runner.py --arms A,B,C,D --tasks t01,t02,t03
    python scripts/exp_runner.py --arms A,B --out runs/exp/results.jsonl   # 全题库

臂的定义：
    A  H 端到端（codex 默认模型，一次 exec，自己看仓库自己做）
    B  L 端到端（同一外壳，换便宜模型）
    C  H 规划 + H 执行（orch，外部计划）
    D  H 规划 + L 执行（orch，外部计划）
C/D 的计划由 H 现场生成，各自生成一份——不借用，否则两臂的规划成本对不上。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 实验里绝不开对照组 A：那会让 D 的每次运行额外多跑一次 A，
# 成本和时间全被污染。真实使用（Codex 客户端）默认是开的。
import os as _os
_os.environ.setdefault("ORCH_CONTROL_ARM", "off")
sys.path.insert(0, str(ROOT))

from scripts.exp_lib import (RUNS_DIR, check_task, codex_argv, cost_of,  # noqa: E402
                             git, init_repo, load_tasks, make_patch, orch_hidden,
                             run_codex, score)

RESULTS = RUNS_DIR / "results.jsonl"
PRICES_FILE = ROOT / "config" / "prices.json"

PLAN_PROMPT = """你是规划器。把下面这个需求拆成可独立执行的任务。

需求：{goal}

仓库现有文件：
{files}

只输出一个 JSON 对象，不要任何解释文字：
{{"tasks": [{{"id": "t01", "goal": "这一件事做什么（一句）",
  "reads": ["要读的文件"], "writes": ["这个任务会写的文件"],
  "acceptance": ["一条可用命令判定的判据，例如 python checks/x.py 退出码 0"]}}]}}

规则：
- 任务之间不要写同一个文件；有先后依赖就把后一步放进下一层也用同一份 tasks 列表（系统会按 writes 自动分层）。
- acceptance 必须是命令能判的；写成"文件断言：路径包含 XX"也可以。
- **每条 acceptance 只能验本任务自己写的文件**。若仓库里有 checks/check_a.py 这类按文件分的验收，
  就引用对应的那一个；不要用"跑全部测试"的命令当单个任务的判据——那样任何一个任务单独跑都过不了。
- **goal 必须是一句话、一个动词**：不许出现「；」「然后」「同时」「以及」这类并列标记。
  同一件事的多条约束写进 acceptance 或留在需求原文里，不要堆进 goal。
- **一个可独立交付的文件 = 一个任务**：需求提到 6 份文件就产出 6 个任务，
  不要把多份文件塞进同一个任务（那样就退化成"一个 worker 干完所有事"，失去并行意义）。
  只有文件之间必须共享同一处修改时，才允许合并。
- 不要输出 markdown 代码块。"""


def now() -> str:
    return time.strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"[{now()}] {msg}", flush=True)


def key_of(r: dict) -> str:
    return f"{r['task']}|{r['arm']}|{r.get('rep', 1)}"


def load_done(path: Path) -> set[str]:
    if not path.exists():
        return set()
    done = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            done.add(key_of(json.loads(line)))
        except ValueError:
            continue
    return done


def append(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def extract_json(text: str) -> dict | None:
    """模型爱在 JSON 外面包解释或代码块，也可能直接给数组，尽量捞出来。"""
    def norm(doc):
        if isinstance(doc, list):
            return {"tasks": doc}
        if isinstance(doc, dict) and doc.get("tasks"):
            return doc
        return None

    raw = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        doc = norm(json.loads(raw))
        if doc:
            return doc
    except ValueError:
        pass
    # 从长到短试括号块，避免贪婪匹配把两段 JSON 粘在一起
    for m in re.finditer(r"\{.*\}|\[.*\]", text, re.S):
        try:
            doc = norm(json.loads(m.group(0)))
            if doc:
                return doc
        except ValueError:
            continue
    return None


def plan_for(task, work: Path, timeout: int) -> tuple[dict | None, dict, float, str]:
    """让 H 写一份计划，并用 orch 的校验器验一遍（不通过就带着错误重写一次）。"""
    files = "\n".join("- " + str(p.relative_to(work)).replace("\\", "/")
                      for p in sorted(work.rglob("*")) if p.is_file()
                      and ".git" not in p.parts)[:3000]
    prompt = PLAN_PROMPT.format(goal=task.goal, files=files)
    usage_total, wall, last_err = {}, 0.0, ""
    plan = None
    diag = ""
    for attempt in (1, 2):
        r = run_codex(codex_argv("A", work, "bypass"), work, prompt, min(timeout, 300))
        wall += r["wall_s"]
        for k, v in (r["usage"] or {}).items():
            if isinstance(v, int):
                usage_total[k] = usage_total.get(k, 0) + v
        doc = extract_json(r["message"] or "")
        if not doc or not doc.get("tasks"):
            last_err = "没有解析出含 tasks 的 JSON"
            diag = (f"rc={r['exit_code']} msg_len={len(r['message'] or '')} "
                    f"head={(r['message'] or '')[:200]!r} err={r['stderr'][:200]}")
            prompt = prompt + f"\n\n上一次输出无法解析（{last_err}）。只输出 JSON。"
            continue
        pf = work / ".exp-plan.json"
        pf.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        v = subprocess.run([sys.executable, "-m", "orch", "validate", str(pf),
                            "--repo", str(work)],
                           cwd=str(ROOT), capture_output=True)
        if v.returncode == 0:
            plan = doc
            break
        last_err = (v.stdout + v.stderr).decode("utf-8", "replace")[-600:]
        diag = f"validate rc={v.returncode} {last_err[:300]}"
        prompt = prompt + f"\n\n上一次计划被校验拒绝，原因如下，修好再输出：\n{last_err}"
    return plan, usage_total, round(wall, 1), diag


def dispatch(task, work: Path, plan_path: Path, provider: str, timeout: int) -> dict:
    """让 orch 执行外部计划。provider 决定 worker 档位（codex=H，deepseek=L）。"""
    cmd = [sys.executable, "-m", "orch.dispatch", task.goal,
           "--repo", str(work), "--plan", str(plan_path),
           "--mode", "foreground", "--no-notify", "--timeout", str(timeout),
           "--provider", provider]
    proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
    try:
        o, e = proc.communicate(timeout=timeout + 120)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        from scripts.exp_lib import kill_tree
        kill_tree(proc.pid)
        try:
            o, e = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            o, e = b"", b""
        rc = -1
    out = (o + e).decode("utf-8", "replace")
    return {"exit_code": rc, "out": out[-1500:]}


def orch_usage(run_dir: Path) -> tuple[dict, str]:
    """读 orch 那次运行的汇总用量与合并分支。"""
    usage, branch = {}, ""
    trace = run_dir / "trace.jsonl"
    if not trace.exists():
        return usage, branch
    for line in trace.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("event") == "run.usage":
            d = e.get("data", {})
            usage = {"input_tokens": d.get("tokens_in", 0),
                     "cached_input_tokens": d.get("tokens_cached", 0),
                     "output_tokens": d.get("tokens_out", 0),
                     "turns": d.get("turns", 0)}
        elif e.get("event") == "run.end":
            branch = e.get("data", {}).get("merged", "") or branch
    return usage, branch


def merge_usage(a: dict, b: dict) -> dict:
    out = dict(a or {})
    for k, v in (b or {}).items():
        if isinstance(v, int):
            out[k] = out.get(k, 0) + v
    return out


def run_one(task, arm: str, rep: int, keep: bool, l_worker: str = "codex-ds-flash") -> dict:
    t0 = time.time()
    cell = RUNS_DIR / task.id / arm
    work = cell / f"r{rep}"
    os.makedirs(work.parent, exist_ok=True)
    if work.exists():
        # 不删：宿主的批量删除保护会拦下 rmtree，整批任务会因此中断。
        # 改名归档，既保住现场，也不影响 resume（续跑看的是 results.jsonl）。
        work.rename(work.with_name(f"{work.name}.old{int(time.time())}"))
    base = init_repo(task, work)

    rec = {"task": task.id, "tier": task.tier, "arm": arm, "rep": rep,
           "base": base, "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "l_worker": l_worker}   # 先记档位，finish() 据此选单价
    plan_usage: dict = {}
    branch = ""

    try:
        if arm in ("A", "B"):
            # A/B 的定义是"不编排"，所以必须让 orch 不可达，否则它可能自己调用。
            with orch_hidden() as hidden:
                if not hidden:
                    raise RuntimeError("orch 仍然可见，A/B 条件不成立——先清理 ~/bin 下的残留")
                r = run_codex(codex_argv(arm, work, "bypass", l_worker), work,
                              task.goal, task.timeout_s)
            rec["orch_hidden"] = True
            rec.update({"usage": r["usage"], "turns": r["turns"],
                        "wall_s": r["wall_s"], "model_exit": r["exit_code"],
                        "stderr": r["stderr"], "message": r["message"]})
            patch = make_patch(work, base)
            # 双保险：即使命令藏住了，只要真跑出了 .orch-runs，这一格也不能算 A/B。
            if (work / ".orch-runs").exists():
                rec["contaminated"] = True
                rec["invalid"] = True
                rec["invalid_reason"] = "A/B 臂出现 .orch-runs：orch 介入，条件不成立"
        else:
            plan, plan_usage, plan_wall, plan_diag = plan_for(task, work, task.timeout_s)
            if plan is None:
                rec.update({"error": "计划未产出或始终未通过校验",
                            "plan_usage": plan_usage, "plan_wall_s": plan_wall,
                            "plan_diag": plan_diag})
                rec["score"] = {"apply_ok": False, "resolved": False, "detail": "no plan"}
                return finish(rec, t0, work, keep)
            pf = work / ".exp-plan.json"
            provider = "codex" if arm == "C" else "deepseek"
            if arm == "D2":
                # D2 = D + 一次返工：先注入一条写错的判据（指向不存在的脚本），
                # 复现"判据写错 → rc=2"这种最常见的失败，再看返工一次的代价。
                bad = json.loads(json.dumps(plan))
                first = bad["tasks"][0]
                if first.get("acceptance"):
                    first["acceptance"][0] = first["acceptance"][0].replace(".py", "_typo.py", 1)
                pfb = work / ".exp-plan-bad.json"
                pfb.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
                d = dispatch(task, work, pfb, provider, task.timeout_s)
                run_dir = max((work / ".orch-runs").glob("*"),
                              key=lambda p: p.stat().st_mtime, default=None)
                usage1, branch1 = orch_usage(run_dir) if run_dir else ({}, "")
                patch1 = make_patch(work, base, branch1) if branch1 else ""
                sc1 = score(task, patch1)
                rec.update({"first_pass_resolved": sc1["resolved"],
                            "first_pass_cost_parts": True,
                            "usage_first": usage1,
                            "dispatch_exit": d["exit_code"]})
                if sc1["resolved"]:
                    patch = patch1
                    usage, branch = usage1, branch1
                    rec["retry"] = "未触发（第一轮就过）"
                else:
                    d2 = dispatch(task, work, pf, provider, task.timeout_s)
                    run2 = max((work / ".orch-runs").glob("*"),
                               key=lambda p: p.stat().st_mtime, default=None)
                    usage2, branch = orch_usage(run2) if run2 else ({}, "")
                    usage = {k: (usage1.get(k, 0) + usage2.get(k, 0)) for k in
                             set(usage1) | set(usage2) if isinstance(usage1.get(k, 0), int)
                             and isinstance(usage2.get(k, 0), int)}
                    usage["turns"] = usage1.get("turns", 0) + usage2.get("turns", 0)
                    patch = make_patch(work, base, branch) if branch else ""
                    rec["retry"] = {"triggered": True, "usage_second": usage2,
                                    "wall_second_s": round(d2.get("wall_s", 0), 1),
                                    "second_branch": branch}
                    rec["second_run_dir"] = str(run2) if run2 else ""
                rec.update({"usage": usage, "turns": usage.get("turns", 0),
                            "plan_usage": plan_usage, "plan_wall_s": plan_wall,
                            "dispatch_out": d["out"],
                            "run_dir": str(run_dir) if run_dir else ""})
                patch_dir = cell / "patches"
                patch_dir.mkdir(parents=True, exist_ok=True)
                (patch_dir / f"r{rep}.patch").write_text(patch, encoding="utf-8")
                rec["patch_bytes"] = len(patch.encode("utf-8"))
                rec["score"] = score(task, patch)
                rec["resolved"] = rec["score"]["resolved"]
                return finish(rec, t0, work, keep)
            d = dispatch(task, work, pf, provider, task.timeout_s)
            run_dir = max((work / ".orch-runs").glob("*"), key=lambda p: p.stat().st_mtime,
                          default=None)
            usage, branch = orch_usage(run_dir) if run_dir else ({}, "")
            rec.update({"usage": usage, "turns": usage.get("turns", 0),
                        "plan_usage": plan_usage, "plan_wall_s": plan_wall,
                        "dispatch_exit": d["exit_code"], "dispatch_out": d["out"],
                        "run_dir": str(run_dir) if run_dir else ""})
            patch = make_patch(work, base, branch) if branch else ""

        patch_dir = cell / "patches"
        patch_dir.mkdir(parents=True, exist_ok=True)
        (patch_dir / f"r{rep}.patch").write_text(patch, encoding="utf-8")
        rec["patch_bytes"] = len(patch.encode("utf-8"))
        rec["score"] = score(task, patch)
        rec["resolved"] = rec["score"]["resolved"]
    except Exception as e:                                   # noqa: BLE001
        rec.update({"error": f"{type(e).__name__}: {e}", "resolved": False,
                    "score": {"apply_ok": False, "resolved": False, "detail": str(e)[:300]}})
    return finish(rec, t0, work, keep)


H_MODEL, L_MODEL = "gpt-6.1-sol", "deepseek-flash"


def l_model_name(l_worker: str) -> str:
    """--worker-l 指定的档位对应哪个模型名。

    必须是动态的：写死 L_MODEL 的话，换成 glm 之后成本仍按 deepseek 单价算，
    数字看着正常、其实全是错的（曾把 glm 的三格记成 deepseek-flash）。
    """
    try:
        from orch.workers import load_workers
        ws, _ = load_workers(str(ROOT / "workers.json"))
        if l_worker in ws and ws[l_worker].model:
            return ws[l_worker].model
    except Exception:
        pass
    return L_MODEL


def finish(rec: dict, t0: float, work: Path, keep: bool) -> dict:
    rec["total_wall_s"] = round(time.time() - t0, 1)
    rec["usage_all"] = merge_usage(rec.get("usage"), rec.get("plan_usage"))
    # 分模型计价：规划永远是 H；执行按臂取。合并成一个 token 数再乘单价是错的。
    price = prices()
    ts = t0
    worker_model = H_MODEL if rec["arm"] in ("A", "C") else l_model_name(
        rec.get("l_worker") or "codex-ds-flash")
    c_plan = cost_of(rec.get("plan_usage") or {}, H_MODEL, price, ts) or 0.0
    c_work = cost_of(rec.get("usage") or {}, worker_model, price, ts)
    rec["cost_est"] = None if c_work is None else round(c_plan + c_work, 6)
    rec["cost_parts"] = {"plan": c_plan, "worker": c_work, "worker_model": worker_model}
    rec["l_worker"] = rec.get("l_worker") or "codex-ds-flash"
    # 一律保留现场：① 宿主的批量删除保护会拦 rmtree，整批会中断；
    # ② 审计时能回看每个 trial 的仓库状态，比省那点磁盘重要。
    if not keep:
        rec["workdir"] = str(work)
    return rec


def prices() -> dict:
    try:
        return json.loads(PRICES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="四臂批量实验 runner")
    ap.add_argument("--arms", default="A,B,C,D")
    ap.add_argument("--tasks", default="", help="逗号分隔；空=全题库")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--out", default=str(RESULTS))
    ap.add_argument("--resume", action="store_true", help="跳过已有结果的格子")
    ap.add_argument("--keep", action="store_true", help="保留工作目录")
    ap.add_argument("--worker-l", default="codex-ds-flash",
                    help="B/D 臂用哪个 L 档（workers.json 里的名字，如 glm-flash）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    tasks = load_tasks([t.strip() for t in args.tasks.split(",") if t.strip()])
    if not tasks:
        print("没有可用任务。先放 tasks/<id>/{meta.json,seed/,checks/}", file=sys.stderr)
        return 2
    bad = {t.id: check_task(t) for t in tasks}
    bad = {k: v for k, v in bad.items() if v}
    if bad:
        print("题库自检不通过：", file=sys.stderr)
        for k, v in bad.items():
            print(f"  {k}: {'；'.join(v)}", file=sys.stderr)
        return 2

    arms = [a.strip().upper() for a in args.arms.split(",") if a.strip()]
    out = Path(args.out)
    done = load_done(out) if args.resume else set()

    todo = [(t, a, r) for a in arms for t in tasks for r in range(1, args.reps + 1)
            if not (args.resume and key_of({"task": t.id, "arm": a, "rep": r}) in done)]
    log(f"任务 {len(tasks)} · 臂 {arms} · 重复 {args.reps} → 待跑 {len(todo)} 格")
    if args.dry_run:
        for t, a, r in todo:
            print(f"  {t.id:<6} {a}  r{r}  ≤{t.timeout_s}s")
        return 0

    for i, (t, a, r) in enumerate(todo, 1):
        log(f"({i}/{len(todo)}) {t.id} 臂 {a} r{r}")
        rec = run_one(t, a, r, args.keep, args.worker_l)
        append(out, rec)
        log(f"    → resolved={rec.get('resolved')} wall={rec.get('total_wall_s')}s "
            f"tokens={rec.get('usage_all', {}).get('input_tokens', 0):,}"
            f"/{rec.get('usage_all', {}).get('output_tokens', 0):,}"
            + (f" err={rec['error'][:60]}" if rec.get("error") else ""))
    log(f"完成。结果：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
