"""实验用的公共件：题库、仓库初始化、跑一次 codex、打分。

为什么独立出来：runner 要很短，才能一眼看完它在做什么。
所有"会出错的地方"（git、超时、patch、评分）都收在这里，便于单独修。
"""

from __future__ import annotations

import json
import contextlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orch.planner import parse_json_events, resolve_codex  # noqa: E402
from orch.providers import build as build_worker_provider  # noqa: E402
from orch.workers import load_workers  # noqa: E402

TASKS_DIR = ROOT / "tasks"
RUNS_DIR = ROOT / "runs" / "exp"


# ---------------------------------------------------------------- 题库

@dataclass
class Task:
    id: str
    tier: str                 # S / M / L
    goal: str                 # 给 agent 的原话
    seed: Path                # 初始文件（不含 .git）
    checks: Path              # 权威验收脚本（不放进仓库，防篡改）
    timeout_s: int = 1800
    accept: list[str] = field(default_factory=list)   # 评分时执行的命令

    @staticmethod
    def load(d: Path) -> "Task":
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        return Task(
            id=meta["id"], tier=meta.get("tier", "S"), goal=meta["goal"],
            seed=d / "seed", checks=d / "checks",
            timeout_s=int(meta.get("timeout_s", 1800)),
            accept=meta.get("accept") or [],
        )


def load_tasks(only: list[str] | None = None) -> list[Task]:
    out = []
    for d in sorted(TASKS_DIR.iterdir()):
        if not (d / "meta.json").exists():
            continue
        t = Task.load(d)
        if only and t.id not in only:
            continue
        out.append(t)
    return out


def check_task(t: Task) -> list[str]:
    """题库自检：缺一样就不许进实验。"""
    errs = []
    if not t.seed.is_dir() or not any(t.seed.iterdir()):
        errs.append("seed/ 为空")
    if not t.checks.is_dir() or not any(t.checks.iterdir()):
        errs.append("checks/ 为空")
    if not t.goal.strip():
        errs.append("goal 为空")
    if not t.accept:
        errs.append("meta.json 没有 accept（评分无从判定）")
    if (t.seed / ".git").exists():
        errs.append("seed/ 里含 .git（会变成嵌套仓库）")
    return errs


# ---------------------------------------------------------------- git / 仓库

def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.decode('utf-8', 'replace')[:300]}")
    return r.stdout.decode("utf-8", "replace")


def init_repo(task: Task, dest: Path) -> str:
    """把 seed 拷成一个干净的 git 仓库，返回初始提交 sha。"""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(task.seed, dest)
    for a in (["init", "-q"], ["config", "user.name", "exp"],
              ["config", "user.email", "exp@example.local"], ["add", "-A"],
              ["commit", "-qm", f"init: {task.id}"]):
        git(dest, *a)
    return git(dest, "rev-parse", "HEAD").strip()


def make_patch(repo: Path, base: str, rev: str = "") -> str:
    """base..rev 的 unified diff。rev 为空表示工作区未提交改动。"""
    if rev:
        return git(repo, "diff", base, rev)
    return git(repo, "diff", base)


# ---------------------------------------------------------------- 跑模型


@contextlib.contextmanager
def orch_hidden():
    """跑 A/B 臂时把 orch 命令藏起来，并**校验确实藏住了**。

    为什么必须机制化：A 的定义是「Codex 单干」，但只要 orch 可达，它就可能自己调用
    （E22：A 臂自己派了 8 个 worker，成本 3.5 倍；E26：09:00 那批 12 个 A 格全是 orch 跑的）。

    为什么不能只 rename 到固定名：上一次被中断会留下 `orch.hidden`，
    下次 rename 到同名目标在 Windows 上直接失败（目标已存在），
    而原来的代码 `except OSError: pass`——**静默跳过，orch 依然可见**，
    结果整批 A 臂都在偷偷编排，而且记录里看不出异常。
    所以这里改成：换到唯一目录 + 事后用 shutil.which 校验 + 把结果交给调用方。
    """
    home = Path.home() / "bin"
    # 清掉上次中断留下的空 stash 目录（不影响运行，只是别让 ~/bin 越积越乱）
    for old in home.glob(".orchstash-*"):
        try:
            old.rmdir()
        except OSError:
            pass
    stash = home / f".orchstash-{os.getpid()}-{int(time.time())}"
    moved = []
    try:
        stash.mkdir(parents=True, exist_ok=True)
    except OSError:
        stash = None

    if stash is not None:
        # ① 命令垫片
        for name in ("orch", "orch.cmd", "orch.py"):
            src = home / name
            if not src.exists():
                continue
            try:
                src.rename(stash / name)
                moved.append((src, stash / name))
            except OSError as exc:
                print(f"[warn] 藏不起 {src}：{exc}")

        # ② 技能目录。只藏命令是不够的：SKILL.md 里写了「没装就跑 install_cli.py」，
        #    模型会照着把命令重新装回来（实测：藏了 shim，它仍然装回去并派了 worker）。
        skill = Path.home() / ".codex" / "skills" / "orch"
        if skill.exists():
            try:
                skill.rename(stash / "skill-orch")
                moved.append((skill, stash / "skill-orch"))
            except OSError as exc:
                print(f"[warn] 藏不起技能目录 {skill}：{exc}")

    # 关键一步：不假设成功，实际问一次系统
    still = shutil.which("orch")
    skill_left = (Path.home() / ".codex" / "skills" / "orch").exists()
    try:
        yield still is None and not skill_left
    finally:
        for src, dst in moved:
            try:
                dst.rename(src)
            except OSError:
                pass
        if stash is not None:
            try:
                stash.rmdir()
            except OSError:
                pass

def codex_argv(arm: str, work: Path, sandbox: str, l_worker: str = "codex-ds-flash") -> list[str]:
    """A/B 臂的 argv：A 用 codex 默认模型（订阅强档），B 用最便宜档。"""
    if arm == "A":
        argv = [resolve_codex(), "exec", "--json", "--skip-git-repo-check"]
        if sandbox == "bypass":
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            argv += ["--sandbox", sandbox]
        return argv
    ws, _ = load_workers(str(ROOT / "workers.json"))
    if l_worker not in ws:
        raise RuntimeError(f"workers.json 里没有 {l_worker}")
    prov, why = build_worker_provider(ws[l_worker], sandbox=sandbox)
    if prov is None:
        raise RuntimeError(f"造不出 L 档 provider：{why}")
    return [a.replace("{cwd}", str(work)) for a in prov.argv if a != "{prompt}"]


def kill_tree(pid: int) -> None:
    """连子进程一起杀。

    真实踩过：20 分钟超时后父进程被 kill，两个 codex.exe 还挂着；
    后续批次被它们拖住，看起来像"某臂无故卡死"。
    """
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True)
    else:
        try:
            os.killpg(os.getpgid(pid), 9)
        except OSError:
            pass


def run_codex(argv: list[str], work: Path, prompt: str, timeout: int) -> dict:
    """跑一次 codex exec，返回 {usage, turns, wall_s, ok, stderr, message}。"""
    t0 = time.time()
    proc = subprocess.Popen(argv, cwd=str(work), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, stdin=subprocess.PIPE,
                            start_new_session=os.name != "nt",
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
    try:
        out_b, err_b = proc.communicate(prompt.encode("utf-8"), timeout=timeout)
        out, err = out_b.decode("utf-8", "replace"), err_b.decode("utf-8", "replace")
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        kill_tree(proc.pid)
        try:
            out_b, err_b = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            out_b, err_b = b"", b""
        out = out_b.decode("utf-8", "replace")
        err = f"timeout after {timeout}s"
        rc = -1
    msg, usage = parse_json_events(out)
    turns = sum(1 for ln in out.splitlines() if '"turn.completed"' in ln)
    # message 不截断：上层要靠它解析计划 JSON。
    # 曾经截到 800 字，4 个任务的计划被切断 → 解析失败 → 被误判成"模型写不出计划"。
    return {"usage": usage, "turns": turns, "wall_s": round(time.time() - t0, 1),
            "exit_code": rc, "stderr": err[-500:], "message": msg}


# ---------------------------------------------------------------- 评分

def score(task: Task, patch: str, timeout: int = 300) -> dict:
    """把 patch 打到一个全新仓库上跑权威 checks。副作用为零：临时目录。"""
    tmp = Path(tempfile.mkdtemp(prefix="exp-score-"))
    try:
        repo = tmp / "repo"
        init_repo(task, repo)
        if patch.strip():
            pf = tmp / "p.patch"
            pf.write_text(patch, encoding="utf-8")
            r = subprocess.run(["git", "apply", "--whitespace=nowarn", str(pf)],
                               cwd=str(repo), capture_output=True)
            if r.returncode != 0:
                return {"apply_ok": False, "resolved": False,
                        "detail": r.stderr.decode("utf-8", "replace")[-300:]}
        # 权威 checks 从题库目录拷，不用仓库里的副本（防 worker 改写验收）
        for f in task.checks.rglob("*"):
            if f.is_file():
                # 必须落在 repo/checks/ 下：accept 里的路径是 "python checks/xxx.py"。
                # 曾直接拷到仓库根，导致评分时"找不到文件"，看起来像模型没做完。
                dst = repo / "checks" / f.relative_to(task.checks)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst)
        outs, ok = [], True
        for cmd in task.accept:
            r = subprocess.run(cmd, cwd=str(repo), shell=True, capture_output=True,
                               timeout=timeout)
            outs.append({"cmd": cmd, "rc": r.returncode,
                         "out": (r.stdout + r.stderr).decode("utf-8", "replace")[-400:]})
            ok = ok and r.returncode == 0
        return {"apply_ok": True, "resolved": bool(ok), "detail": outs}
    except Exception as e:                                  # noqa: BLE001
        return {"apply_ok": False, "resolved": False, "detail": f"{type(e).__name__}: {e}"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _is_peak(ts: float | None, windows: list) -> bool:
    """按 UTC 判断是否落在高峰时段（DeepSeek 的高峰是 UTC 周一到周五的固定窗口）。"""
    import datetime as _dt
    t = _dt.datetime.fromtimestamp(ts or time.time(), _dt.timezone.utc)
    if t.weekday() >= 5:
        return False
    return any(int(a) <= t.hour < int(b) for a, b in windows)


def cost_of(usage: dict, model: str, prices: dict, ts: float | None = None) -> float | None:
    """token → 估算金额。

    - 拿不到用量或没有单价：返回 None（不写 0，否则"没测到"会变成"不花钱"）
    - 分时计价的模型（deepseek-flash）按运行时刻的 UTC 时段选档；
      高峰/空闲差一倍，混在一起算会把成本算错 30% 以上。
    """
    if not usage:
        return None
    spec = (prices.get("models") or {}).get(model)
    if not spec:
        return None
    if "offpeak" in spec:
        peak = _is_peak(ts, spec.get("peak_utc_hours") or [])
        rate = spec["peak"] if peak else spec["offpeak"]
    else:
        rate = spec
    pin, pout = rate.get("input_per_mtok"), rate.get("output_per_mtok")
    if pin is None or pout is None:
        return None
    pcache = rate.get("cached_input_per_mtok", pin)
    cached = usage.get("cached_input_tokens", 0)
    fresh = max(0, usage.get("input_tokens", 0) - cached)
    return round((fresh * pin + cached * pcache + usage.get("output_tokens", 0) * pout) / 1e6, 6)
