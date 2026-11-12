"""出入口：把一句话从对话窗口送进引擎，再把产物送回**同一个窗口**。

为什么单独一层
--------------
`intake` 只负责落原话，`plan`/`run` 只负责编译和执行。这条链原本是断的：
发起之后没人负责把结果送回去，用户只能自己在终端敲 `orch status`。

这一层补的就是那两段：

    窗口里说一句话
       ↓  dispatch()  同步只做 intake，毫秒级返回 run_id
    脱离进程树的后台进程
       ↓  plan → validate → run → verify → merge（5–15 分钟）
    回到原来那个窗口
       ↓  `codex queue --thread <T> --message <报告>`

出口用的是 Codex 自带的消息队列（实测 2026-11-07，codex-cli 0.167.0-alpha.3.2）：
消息落进 `~/.codex/queue_1.sqlite` 的 `queued_items`，桌面端自己消费并立刻起一轮
（会话记录里 `turn_trigger: "queue"`）。不需要 daemon 常驻，也不需要 `--resume`。
桌面端没开时消息留在表里，下次打开再消费，不会丢。

一个必须守住的边界
------------------
后台进程用 DETACHED_PROCESS 起，**绝不能挂在 Codex 的进程树里**——否则那个 turn
一结束就被连带杀掉，整套东西只在对话窗口开着的那几秒里有效。

另外：回传的消息是以「用户消息」身份进入会话的，模型会真的再跑一轮。所以报告末尾
必须写死「原样汇报、不要动文件」，否则它会以为这是新任务又去改代码。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import thread as thread_mod
from .intake import intake

# Windows：脱离控制台 + 独立进程组。两个都要，缺一个都可能被父进程组带走。
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200

# 单条回传消息的上限。报告正文再长也不该把整个 trace 灌进对话。
_MAX_MESSAGE = 2500

_REPORT_NAME = "DISPATCH.md"


@dataclass
class Dispatch:
    run_id: str
    run_dir: str
    thread_id: str = ""
    pid: int = 0
    detached: bool = True
    plan_only: bool = False
    note: str = ""        # 调度说明（为什么选阻塞/投递）

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "run_dir": self.run_dir,
            "thread_id": self.thread_id,
            "pid": self.pid,
            "detached": self.detached,
            "plan_only": self.plan_only,
        }


class _Log:
    """后台进程没有终端，日志必须落盘，否则失败时什么都看不到。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def __call__(self, msg: str) -> None:
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
        print(line, flush=True)
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def find_codex_bin() -> str | None:
    """找 codex.exe。桌面端把它放在按版本哈希命名的目录里，不能写死。"""
    env = os.environ.get("CODEX_BIN")
    if env and Path(env).is_file():
        return env

    local = os.environ.get("LOCALAPPDATA")
    if local:
        base = Path(local) / "OpenAI" / "Codex" / "bin"
        if base.exists():
            # 注意：同级目录里还有只有 rg.exe 的条目，必须按 codex.exe 过滤
            cands = [p for p in base.glob("*/codex.exe") if p.is_file()]
            cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            if cands:
                return str(cands[0])

    return shutil.which("codex") or shutil.which("codex.exe")


def send_to_thread(thread_id: str, text: str, timeout: float = 60.0) -> tuple[bool, str]:
    """把一段文本投回指定会话。

    不加 `--resume`：桌面端自己订阅队列，实测 4.4 秒就起了一轮。
    加了反而会去连一个可能没启动的 app-server daemon，只换来一条 warning。
    """
    exe = find_codex_bin()
    if not exe:
        return False, "找不到 codex.exe（用 CODEX_BIN 指定）"
    if not thread_id:
        return False, "没有 thread_id"

    body = text
    if len(body) > _MAX_MESSAGE:
        body = body[:_MAX_MESSAGE] + f"\n…（已截断，完整报告见 {_REPORT_NAME}）"

    try:
        r = subprocess.run(
            [exe, "queue", "--thread", thread_id, "--message", body],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
    except Exception as e:  # 超时、找不到可执行文件都算投递失败，不抛
        return False, f"{type(e).__name__}: {e}"

    out = ((r.stdout or "") + (r.stderr or "")).strip()
    return r.returncode == 0, out


def _state_root() -> Path:
    """常驻宿主的家。和 runs/ 分开：一个是运行产物，一个是进程间通信。"""
    env = os.environ.get("ORCH_HOME")
    return Path(env) if env else Path.home() / ".orch"


def _queue_dir() -> Path:
    d = _state_root() / "queue"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pid_file() -> Path:
    return _state_root() / "serve.pid"


def pid_alive(pid: int) -> bool:
    """只读检查进程在不在。不做任何信号操作。"""
    if not pid or pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = wintypes.DWORD()
        k32.GetExitCodeProcess(h, ctypes.byref(code))
        return code.value == 259  # STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def serve_pid() -> int:
    p = _pid_file()
    if not p.exists():
        return 0
    try:
        pid = int(p.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0
    return pid if pid_alive(pid) else 0


def _exclude_runs_dir(repo: Path, runs_root: Path) -> None:
    """运行目录若落在仓库内，写进 .git/info/exclude。

    不写的话工作区永远是"脏"的（.orch-runs/ 是未跟踪目录），
    于是 `orch apply` 每次都被自己的运行记录挡住。
    用 info/exclude 而不是 .gitignore：后者会被提交，污染用户的仓库。
    """
    try:
        rel = runs_root.resolve().relative_to(repo.resolve())
    except (ValueError, OSError):
        return
    exclude = repo / ".git" / "info" / "exclude"
    marker = f"/{rel.as_posix().rstrip('/')}/"
    try:
        old = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if marker in old:
            return
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a", encoding="utf-8") as fh:
            fh.write(f"\n# orch 运行目录（不进版本库）\n{marker}\n")
    except OSError:
        pass          # 排除失败不该阻断运行；apply 时还能用 --force


def serve(poll: float = 3.0, once: bool = False, runs_dir: str | Path = "runs") -> int:
    from .control import RunLock
    try:
        with RunLock(_state_root(), "serve.lock"):
            return _serve(poll, once, runs_dir)
    except ValueError as e:
        print(str(e))
        return 4


def _recover_claims(qdir: Path, log):
    from .control import atomic_json
    for claimed in qdir.glob("*.running"):
        try:
            job = json.loads(claimed.read_text(encoding="utf-8"))
            directory = Path(job.get("runs_dir", "runs")) / job["run_id"]
            job["resume"] = (directory / "runtime.json").exists()
            target = claimed.with_suffix(".json")
            if target.exists():
                log(f"保留重复认领记录：{claimed.name}")
                continue
            atomic_json(target, job)
            claimed.unlink()
            log(f"恢复认领 {job['run_id']} resume={job['resume']}")
        except (OSError, ValueError, KeyError) as e:
            log(f"无法恢复 {claimed.name}：{e}")


def _serve(poll: float = 3.0, once: bool = False, runs_dir: str | Path = "runs") -> int:
    """常驻宿主：轮询队列，跑完把结果投回窗口。

    为什么必须有这个东西
    --------------------
    实测（2026-11-07）：从 Codex / 宿主的命令里 fork 出去的后台进程，会在命令
    结束时被父级 Job Object 一并清理；`CREATE_BREAKAWAY_FROM_JOB` 被拒
    （WinError 5），`schtasks` 在程序黑名单里。也就是说 **任何依附于那一条命令
    的进程都活不过它**，而一次 run 要 5–15 分钟。

    所以执行者必须是一个从别处启动、不挂在任何对话上的常驻进程。它只做两件事：
    消费 queue/ 里的任务、把报告投回去。
    """
    qdir = _queue_dir()
    log_path = _state_root() / "serve.log"
    log = _Log(log_path)
    _pid_file().write_text(str(os.getpid()), encoding="utf-8")
    log(f"serve start  pid={os.getpid()}  queue={qdir}  poll={poll}s")
    _recover_claims(qdir, log)

    try:
        while True:
            jobs = sorted(qdir.glob("*.json"))
            for job_file in jobs:
                # 先改名认领，避免两个 serve 抢同一条
                try:
                    claimed = job_file.with_suffix(".running")
                    job_file.rename(claimed)
                except OSError:
                    continue
                try:
                    job = json.loads(claimed.read_text(encoding="utf-8"))
                except Exception as e:
                    log(f"跳过坏任务 {claimed.name}: {e}")
                    claimed.unlink(missing_ok=True)
                    continue

                log(f"取到 {job.get('run_id')}  thread={job.get('thread_id') or '-'}")
                # 每个任务用**独立子进程**跑，不在宿主内存里直接执行。
                #
                # 原因：宿主是长驻的，一旦引擎代码在运行期间被改（2026-11-07 01:08
                # 有人给 verifier 加了 _OUT_SPEC，01:09 就炸 ImportError），内存里的
                # contract 与磁盘上的 verifier 就对不上了。
                # 子进程每次重新加载模块，既拿到最新代码，也让单个任务崩掉不带走宿主。
                try:
                    code = _run_in_child(job, runs_dir, log)
                    suffix = ".done" if code == 0 else ".failed"
                    metadata = Path(job.get("runs_dir", runs_dir)) / job["run_id"] / "dispatch.json"
                    if metadata.exists() and json.loads(metadata.read_text(encoding="utf-8")).get("state") == "waiting_input":
                        suffix = ".waiting"
                    claimed.replace(claimed.with_suffix(suffix))
                except Exception as e:
                    log(f"任务 {job.get('run_id')} 崩了：{type(e).__name__}: {e}")
                    if claimed.exists():
                        claimed.replace(claimed.with_suffix(".failed"))

            if once:
                log("serve 单次模式结束")
                return 0
            time.sleep(poll)
    except KeyboardInterrupt:
        log("serve 收到中断，退出")
        return 0
    finally:
        _pid_file().unlink(missing_ok=True)


def _run_in_child(job: dict, default_runs_dir: str | Path, log: _Log) -> int:
    """在子进程里执行一个任务。argv 与 dispatch 入队时保持同构。"""
    root = Path(__file__).resolve().parent.parent
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONUNBUFFERED"] = "1"

    argv = [
        sys.executable, "-m", "orch.dispatch",
        "--job", job["run_id"],
        "--runs-dir", str(job.get("runs_dir") or Path(default_runs_dir).resolve()),
        "--repo", str(job.get("repo") or "."),
        "--provider", job.get("provider", "codex"),
        "--verifier", job.get("verifier", "codex"),
        "--timeout", str(int(job.get("timeout") or 900)),
    ]
    if job.get("thread_id"):
        argv += ["--thread", job["thread_id"]]
    if job.get("nonce"):
        argv += ["--nonce", job["nonce"]]
    if job.get("fingerprint"):
        argv += ["--fingerprint", job["fingerprint"]]
    pids = job.get("pids") or []
    if pids:
        argv += ["--pids", ",".join(str(p) for p in pids)]
    if not job.get("notify", True):
        argv += ["--no-notify"]
    if job.get("plan_only"):
        argv += ["--plan-only"]
    if job.get("verification_path"):
        argv += ["--verification", job["verification_path"]]
    for flag, key in (("--execution", "execution_path"), ("--workers", "workers_path")):
        if job.get(key):
            argv += [flag, job[key]]
    if job.get("resume"):
        argv += ["--resume"]

    log(f"  子进程执行  cwd={root}")
    try:
        r = subprocess.run(argv, cwd=str(root), env=env)
        return r.returncode
    except Exception as e:
        log(f"  子进程启动失败：{type(e).__name__}: {e}")
        return 1


def enqueue(job: dict) -> Path:
    """把任务放进队列。调用方（对话窗口）只等这一次文件写入。"""
    run_id = job["run_id"]
    p = _queue_dir() / f"{run_id}.json"
    from .control import atomic_json
    atomic_json(p, job)
    return p


def spawn_detached(argv: list[str], cwd: str | Path, log_path: str | Path) -> int:
    """起一个脱离进程树的后台进程，返回 pid。"""
    env = os.environ.copy()
    root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONUNBUFFERED"] = "1"

    log = open(log_path, "ab", buffering=0)
    kwargs: dict = dict(
        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        cwd=str(cwd), env=env, close_fds=True,
    )
    if os.name == "nt":
        kwargs["creationflags"] = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(argv, **kwargs)
        return proc.pid
    finally:
        log.close()


def dispatch(
    text: str,
    repo: str | Path = ".",
    runs_dir: str | Path = "runs",
    thread_id: str | None = None,
    nonce: str | None = None,
    max_agents: int = 3,
    budget: float = 6.0,
    detach: bool = True,
    notify: bool = True,
    force_notify: bool = False,          # 显式 --notify：前台也投递
    plan_only: bool = False,
    provider: str = "codex",
    verifier: str = "codex",
    timeout: int = 900,
    mode: str = "auto",
    verification_path: str | None = None,
    execution_path: str | None = None,
    workers_path: str | None = None,
    resume: bool = False,
    plan_path: str | None = None,
    worker_sandbox: str | None = None,
) -> tuple[Dispatch, int]:
    """同步部分只做 intake，保证窗口那边立刻拿到 run_id。

    mode 决定「谁来跑」：
      auto        有常驻宿主就入队（毫秒级返回）；没有就当场跑完（会阻塞）
      serve       强制入队，等宿主来取（宿主没起就一直躺着）
      foreground  当场跑完
      detach      直接 fork。**在当前环境里活不过命令结束**，只在宽松环境用

    返回 (Dispatch, 退出码)。只有 foreground/detach 的退出码有意义。
    """
    it = intake(text, repo=repo, runs_dir=runs_dir,
                max_agents=max_agents, budget_usd=budget)
    verification_path = str(Path(verification_path).resolve()) if verification_path else None
    execution_path = str(Path(execution_path).resolve()) if execution_path else None
    workers_path = str(Path(workers_path).resolve()) if workers_path else None

    root_runs = Path(runs_dir).resolve()
    run_dir = root_runs / it.run_id
    if repo:
        _exclude_runs_dir(Path(repo), root_runs)
    run_dir.mkdir(parents=True, exist_ok=True)

    # 祖先链必须在**父进程**里算：后台进程是 detach 起的，等它启动时
    # dispatch 进程已经退出，链会断在那一层，就认不出 pwsh 的 pid 了。
    pids = sorted(thread_mod.ancestor_pids())
    # pid 在 Codex 桌面端会失配（见 thread.fingerprint_of 的说明），
    # 命令文本才是可靠证据——原话一定出现在那条命令行里。
    fingerprint = thread_mod.fingerprint_of(text)

    meta = {
        "run_id": it.run_id,
        "created_at": it.created_at,
        "repo": it.repo,
        "thread_id": thread_id or "",
        "nonce": nonce or "",
        "notify": notify,
        "plan_only": plan_only,
        "detached": detach,
        "state": "queued",
        "updated_at": _now(),
    }
    (run_dir / "dispatch.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    job = {
        "run_id": it.run_id,
        "runs_dir": str(root_runs),
        "repo": it.repo,
        "thread_id": thread_id or "",
        "nonce": nonce or "",
        "fingerprint": fingerprint,
        "pids": pids,
        "notify": notify,
        "plan_only": plan_only,
        "provider": provider,
        "verifier": verifier,
        "timeout": timeout,
        "verification_path": verification_path,
        "execution_path": execution_path,
        "workers_path": workers_path,
        "resume": resume,
    }

    if not detach and mode == "auto":
        mode = "foreground"

    # 有常驻宿主就让宿主跑；没有就当场跑完（会阻塞，但至少结果是可信的）。
    # 「当场跑完」在当前环境里是唯一保证能跑完的方式——fork 出去的进程会被
    # 父级 Job Object 带走，这一点已实测确认（见模块 docstring）。
    host = serve_pid()
    eta_note = ""
    if mode == "auto":
        # 有外部计划就能立刻数出任务数，据此按时长选阻塞还是投递；
        # 没有计划只能等规划完再算，这里退化为"有宿主就投递"。
        n_tasks = 0
        if plan_path:
            try:
                doc = json.loads(Path(plan_path).read_text(encoding="utf-8"))
                n_tasks = len(doc.get("tasks") or doc if isinstance(doc, list) else [])
            except (OSError, ValueError):
                n_tasks = 0
        if n_tasks:
            from . import eta

            est = eta.estimate(root_runs, n_tasks, int(max_agents))
            mode, why = eta.choose(est, host_running=bool(host))
            eta_note = f"{eta.render(est)}；{why}"
        else:
            mode = "serve" if host else "foreground"

    if mode == "serve":
        enqueue(job)
        meta.update({"state": "queued", "mode": "serve",
                     "host_pid": host, "detached": True, "updated_at": _now()})
        (run_dir / "dispatch.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return Dispatch(it.run_id, str(run_dir), thread_id or "", host, True, plan_only,
                        note=eta_note), 0

    if mode == "foreground":
        # 前台是同步的：报告就是这条命令的返回值，调用方（Codex）这一轮直接读得到。
        # 再投一次等于把同一份报告推回会话、多唤起一轮，还要先花最多 45 秒猜窗口。
        # 只有明确要求（--notify）才投。
        if not force_notify:
            notify = False
        meta.update({"state": "running", "mode": "foreground",
                     "detached": False, "updated_at": _now()})
        (run_dir / "dispatch.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        code = run_job(
            it.run_id, runs_dir=root_runs, repo=it.repo, thread_id=thread_id,
            nonce=nonce, pids=pids, notify=notify, plan_only=plan_only,
            provider=provider, verifier=verifier, timeout=timeout,
            verification_path=verification_path,
            execution_path=execution_path, workers_path=workers_path, resume=resume,
            plan_path=plan_path, worker_sandbox=worker_sandbox,
        )
        return Dispatch(it.run_id, str(run_dir), thread_id or "", 0, False, plan_only,
                        note=eta_note), code

    # mode == "detach"：只在确认父进程不清理子进程的环境里用
    root = Path(__file__).resolve().parent.parent
    argv = [
        sys.executable, "-m", "orch.dispatch",
        "--job", it.run_id,
        "--runs-dir", str(root_runs),
        "--repo", it.repo,
        "--provider", provider,
        "--verifier", verifier,
        "--timeout", str(timeout),
        "--max-agents", str(max_agents),
    ]
    if thread_id:
        argv += ["--thread", thread_id]
    if nonce:
        argv += ["--nonce", nonce]
    if fingerprint:
        argv += ["--fingerprint", fingerprint]
    if pids:
        argv += ["--pids", ",".join(str(p) for p in pids)]
    if not notify:
        argv += ["--no-notify"]
    if plan_path:
        argv += ["--plan", str(plan_path)]
    if worker_sandbox:
        argv += ["--worker-sandbox", str(worker_sandbox)]
    if plan_only:
        argv += ["--plan-only"]
    if verification_path:
        argv += ["--verification", verification_path]
    for flag, value in (("--execution", execution_path), ("--workers", workers_path)):
        if value:
            argv += [flag, value]
    if resume:
        argv += ["--resume"]

    pid = spawn_detached(argv, root, run_dir / "dispatch.log")
    meta.update({"state": "running", "mode": "detach", "pid": pid, "updated_at": _now()})
    (run_dir / "dispatch.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    return Dispatch(it.run_id, str(run_dir), thread_id or "", pid, True, plan_only), 0


def run_job(run_id, *args, **kwargs):
    from .control import RunLock
    directory = Path(args[0] if args else kwargs.get("runs_dir", "runs")) / run_id
    try:
        with RunLock(directory, "job.lock"), RunLock(directory):
            return _run_job(run_id, *args, **kwargs)
    except ValueError as e:
        print(str(e))
        return 4


def _guard_repair(baseline, plan_res, run_dir, operation_id):
    """修补只能变强，不能靠放宽标准通过。

    便宜模型修补时最省力的路径是"把判据改松"。这里在它通过校验之后
    再比一次强度，松了就整份打回——和校验器是两道独立的门。
    """
    if not baseline or plan_res.status != "ok":
        return plan_res

    from .contract import compare_plan_strength
    from .planner import Attempt
    from .trace import Tracer

    violations = compare_plan_strength(baseline, plan_res.tasks)
    if not violations:
        return plan_res

    tracer = Tracer(run_dir / "trace.jsonl", operation_id=operation_id)
    tracer.emit("plan.loosened", codes=[v.code for v in violations],
                detail=[v.message for v in violations])
    plan_res.status = "rejected"
    plan_res.attempts.append(
        Attempt(n=len(plan_res.attempts) + 1, ok=False, raw="",
                codes=[v.code for v in violations], violations=violations)
    )
    return plan_res


def _run_job(
    run_id: str,
    runs_dir: str | Path = "runs",
    repo: str | None = None,
    thread_id: str | None = None,
    nonce: str | None = None,
    pids: list[int] | set[int] | None = None,
    notify: bool = True,
    plan_only: bool = False,
    provider: str = "codex",
    verifier: str = "codex",
    timeout: int = 900,
    fingerprint: str | None = None,
    plan_path: str | None = None,
    worker_sandbox: str | None = None,
    verification_path: str | None = None,
    execution_path: str | None = None,
    workers_path: str | None = None,
    resume: bool = False,
) -> int:
    """后台主流程。任何异常都要回传，静默失败是最糟的结果。"""
    run_dir = Path(runs_dir).resolve() / run_id
    operation_id = uuid.uuid4().hex
    from .trace import Tracer
    previous_meta = run_dir / "dispatch.json"
    previous = json.loads(previous_meta.read_text(encoding="utf-8")) if previous_meta.exists() else {}
    Tracer(run_dir / "trace.jsonl", operation_id=operation_id).emit("operation.start", plan_only=plan_only)
    _patch_meta(run_dir, {"operation_id": operation_id, "execution_started": False,
                          "fallback_executed": False, "delivered": False, "finished_at": None,
                          "final_verified": False, "merged_worktree": ""})
    _patch_meta(run_dir, {"delivery_reused": bool(resume and previous.get("state") == "done" and previous.get("delivered"))})
    log = _Log(run_dir / "dispatch.log")
    log(f"job start  run={run_id}  pid={os.getpid()}")

    intent = _read_intent(run_dir)
    text = intent.get("raw_text", "")
    repo_path = repo or intent.get("repo") or "."

    # 1) 先定位窗口。趁早做：CommandExecution 记录要等命令结束才落盘，
    #    所以这里允许等（默认 45 秒），不占后续 plan/run 的时间。
    match = None
    if thread_id:
        match = thread_mod.Match(thread_id=thread_id, method="explicit")
        log(f"thread  {thread_id}（显式指定）")
    elif notify:
        want = {int(p) for p in (pids or [])}
        # 把候选 pid 记下来：认不出来时，这一行是唯一能用来复盘的证据。
        log(f"thread  候选 pids={sorted(want)[:8]}"
            f"{'…' if len(want) > 8 else ''}  nonce={'有' if nonce else '无'}"
            f"  fingerprint={fingerprint[:12] if fingerprint else '无'}")
        if want or nonce or fingerprint:
            match = thread_mod.wait_for(
                pids=want or None, nonce=nonce, cwd=repo_path,
                timeout=45, fingerprint=fingerprint)
        else:
            # 没有任何可精确匹配的证据（比如从普通终端投的），等满 45 秒是纯浪费
            # ——实测白等了 48 秒。这里只试一次 cwd 兜底：命中的是最近在该仓库
            # 活动过的窗口，投错的风险由 confidence=medium 标出来。
            match = thread_mod.resolve(cwd=repo_path) if repo_path else None
        if match:
            log(f"thread  {match.thread_id}  依据={match.method}({match.confidence})")
        else:
            log("thread  没认出来 —— 会照常执行，但结果无法回传")

    state = {"state": "planning", "thread_id": match.thread_id if match else "",
             "report": "", "updated_at": _now()}
    _patch_meta(run_dir, state)

    from .control import ControlledProvider, RunControl, RunStopped, load_execution
    from .planner import Attempt, PlanResult, plan_run
    from .runner import _load_plan, run_plan
    try:
        config_path = Path(execution_path) if execution_path else run_dir / "execution.json"
        if execution_path and not config_path.exists():
            raise ValueError("执行配置不存在")
        runtime = RunControl(run_dir, load_execution(config_path), resume)
        feedback = ""
        # 外部计划的强度基准：修补之后必须比它"不弱"，否则视为偷偷放宽标准
        baseline_tasks: list = []
        checkpoint_path = run_dir / "checkpoint.json"
        reuse_completed = bool(resume and checkpoint_path.exists() and
                               json.loads(checkpoint_path.read_text(encoding="utf-8")).get("state") == "done")
        ctrl_res: dict | None = None       # 对照组 A 的实测结果（试验模式才会填）
        for cycle in range(runtime.policy.max_replans + 1):
            if not reuse_completed:
                runtime.check()
            if resume and cycle == 0 and (run_dir / "checkpoint.json").exists():
                report, spec, _ = _load_plan(run_dir)
                plan_res = PlanResult("ok", tasks=report.tasks, layers=report.layers, spec_md=spec)
            elif plan_path and cycle == 0:
                # 初版计划由 Codex 直接给出：不调任何模型，只走同一套校验。
                # 未通过才把错误清单交给便宜模型修补（见设计稿 §2.4）。
                from .planner import plan_from_file

                plan_res = plan_from_file(run_dir, plan_path, operation_id=operation_id)
                baseline_tasks = list(plan_res.tasks)
            else:
                planner = ControlledProvider(_provider(provider, sandbox="read-only"), runtime)
                plan_res = plan_run(run_dir, planner, max_attempts=3, timeout=300,
                                    operation_id=operation_id, feedback=feedback)
                plan_res = _guard_repair(baseline_tasks, plan_res, run_dir, operation_id)
            if not reuse_completed:
                runtime.check()
            codes = ["PASS" if a.ok else "FAIL" for a in plan_res.attempts]
            if plan_res.status != "ok":
                if runtime.replan():
                    feedback = "上轮规划未通过：" + str([a.codes for a in plan_res.attempts])
                    Tracer(run_dir / "trace.jsonl", operation_id=operation_id).emit("plan.replan", reason=feedback)
                    continue
                return _finish(run_dir, match, notify, log,
                               _render_plan_failed(run_id, text, plan_res, run_dir), 3)
            if plan_only:
                return _finish(run_dir, match, notify, log,
                               _render_plan_ok(run_id, text, plan_res, run_dir, codes), 0, state="planned")
            _patch_meta(run_dir, {"state": "running", "updated_at": _now()})
            # Windows 上 codex 的 workspace-write 会卡在启动阶段（实现在
            # cli.cmd_run 里也注明了）。嵌套在外层沙箱里更明显：子进程起不来，
            # worker 一个文件都改不了，最后表现为"任务失败"，看着像模型不行。
            # 隔离仍有两道：worktree + 事后的越界检查。
            # 对照组 A：在真正动手之前跑一次「Codex 单干」，两边基线相同。
            # 放在执行之前是为了拿到同一个 base；A 在自己的 clone 里跑，不碰工作区。
            if ctrl_res is None:
                from . import control_arm as _ctrl

                if _ctrl.enabled():
                    log("对照组：开始跑 A（试验模式）")
                    ctrl_res = _ctrl.run(Path(repo_path), text,
                                         timeout=max(300, min(int(timeout or 900), 1800)))
                    (run_dir / "control.json").write_text(
                        json.dumps(ctrl_res, ensure_ascii=False), encoding="utf-8")
                    log(f"对照组：exit={ctrl_res.get('exit_code')} {ctrl_res.get('wall_s')}s")
                else:
                    ctrl_res = {"usage": {}, "wall_s": 0, "exit_code": 0, "note": "对照组已关闭"}

            box = worker_sandbox or ("bypass" if os.name == "nt" else "workspace-write")
            worker = _provider(provider, sandbox=box)
            # 只构造一次：_provider 可能有副作用（例如按顺序发放假 worker）
            worker_label = f"{getattr(worker, 'name', provider)} / {getattr(worker, 'model', '?')}"
            Tracer(run_dir / "trace.jsonl", operation_id=operation_id).emit(
                "worker.config", sandbox=box, provider=getattr(worker, "name", provider),
                model=getattr(worker, "model", ""), requested=provider)
            checker = _provider(verifier, sandbox="read-only") if verifier != "none" else None
            summary = run_plan(run_dir, worker, checker, max_agents=int(intent.get("max_agents") or 3),
                               timeout=timeout, operation_id=operation_id, verification_path=verification_path,
                               execution_path=execution_path, workers_path=workers_path,
                               resume=resume and cycle == 0, control=runtime, _lock_held=True)
            summary.worker_label = worker_label
            can_replan = summary.state == "failed" and all(
                o.status == "done" or o.retryable for o in summary.outcomes) and all(
                c.status in {"passed", "failed"} for c in summary.final_checks)
            if can_replan and runtime.replan():
                feedback = "上轮执行失败，保持原需求与固定检查不变重新规划：" + summary.reason
                Tracer(run_dir / "trace.jsonl", operation_id=operation_id).emit("plan.replan", reason=feedback)
                continue
            break
    except RunStopped as e:
        return _finish(run_dir, match, notify, log,
                       _render_error("运行停止", run_id, text, str(e)), 4, state=runtime.state, reason=str(e))
    except Exception as e:
        log(f"run 异常：{type(e).__name__}: {e}")
        started = any(e.get("operation_id") == operation_id and e.get("event") == "task.start"
                      for e in Tracer(run_dir / "trace.jsonl").events())
        return _finish(run_dir, match, notify, log,
                       _render_error("执行阶段异常", run_id, text, traceback.format_exc()), 4,
                       state="failed", stage="execution" if started else "preflight",
                       execution_started=started, reason=str(e))

    msg = _render_done(run_id, text, plan_res, summary, run_dir, _auto_apply(run_dir, summary))
    return _finish(run_dir, match, notify, log, msg, 0 if summary.ok else 4,
                   state=summary.state, stage="execution" if summary.execution_started else "preflight",
                   execution_started=summary.execution_started, reason=summary.reason,
                   final_verified=summary.final_verified, merged_worktree=summary.merged_worktree)


def _finish(run_dir: Path, match, notify: bool, log: _Log, message: str, code: int,
            *, state: str = "failed", stage: str = "planning",
            execution_started: bool = False, reason: str = "", final_verified: bool = False,
            merged_worktree: str = "") -> int:
    """收尾：写报告 → 更新状态 → 投回窗口。顺序不能反。"""
    (run_dir / _REPORT_NAME).write_text(message, encoding="utf-8")
    log(f"report  {run_dir / _REPORT_NAME}")

    from .trace import Tracer
    meta = json.loads((run_dir / "dispatch.json").read_text(encoding="utf-8"))
    result = {"state": state, "stage": stage, "execution_started": execution_started,
              "reason": reason, "fallback_executed": False, "exit_code": code,
              "report": str(run_dir / _REPORT_NAME), "finished_at": _now(),
              "final_verified": final_verified, "merged_worktree": merged_worktree}
    runtime_file = run_dir / "runtime.json"
    if runtime_file.exists():
        runtime = json.loads(runtime_file.read_text(encoding="utf-8"))
        result["resources"] = {k: v for k, v in runtime.items() if k != "active"}
    _patch_meta(run_dir, result)
    Tracer(run_dir / "trace.jsonl", operation_id=meta.get("operation_id")).emit("operation.end", **result)

    # 三种"没投递"要说清是哪一种：前台同步是正常的，投递失败才需要人管。
    sent, detail = (False, "前台同步返回：报告已在命令输出里，无需投递") if not notify \
        else (False, "跳过（未定位到当前窗口）")
    if meta.get("delivery_reused"):
        sent, detail, notify = True, "复用上次成功送达记录，未重复发送", False
    attempted = bool(notify and match and match.thread_id)
    if notify and match and match.thread_id:
        try:
            sent, detail = send_to_thread(match.thread_id, message)
        except Exception as e:
            sent, detail = False, f"{type(e).__name__}: {e}"
        log(f"queue   {'ok' if sent else 'FAIL'}  {detail[:200]}")

    _patch_meta(run_dir, {
        "delivered": sent,
        "delivery_detail": detail[:500],
        "finished_at": _now(),
        "updated_at": _now(),
    })
    log(f"job end  exit={code}  delivered={sent}")
    Tracer(run_dir / "trace.jsonl", operation_id=meta.get("operation_id")).emit(
        "operation.delivery", attempted=attempted, delivered=sent, detail=detail[:500])
    return code


# ---------------------------------------------------------------- 报告渲染

def _cost_block(plan_res, summary, run_dir: Path) -> str:
    """报告末尾的成本块：额度 + 金额。

    有对照组（本轮真跑过 A）就用实测相减；没有就查题库形态基线（标信心等级）。
    两种口径都写进报告，避免把估计当成实测读。
    """
    from . import control_arm, costing

    ctrl = None
    f = run_dir / "control.json"
    if f.exists():
        try:
            ctrl = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            ctrl = None
    if ctrl and not (ctrl.get("usage") or {}):
        # 对照组跑了但没拿到用量（超时/报错）：保留说明，按"无实测"处理
        ctrl = {"usage": {}, "wall_s": ctrl.get("wall_s"), "note": ctrl.get("note", "")} \
            if ctrl.get("note") and "已关闭" not in str(ctrl.get("note")) else None

    repo = "."
    try:
        repo = json.loads((run_dir / "dispatch.json").read_text(encoding="utf-8")).get("repo") or "."
    except Exception:
        pass

    tasks = list(getattr(plan_res, "tasks", []) or [])
    k = max(1, len(tasks))
    writes: list[str] = []
    for t in tasks:
        writes += list(getattr(t, "writes", []) or [])
    lines: list[int] = []
    for w in writes[:60]:
        p = Path(repo) / w
        try:
            if p.is_file():
                lines.append(len(p.read_text(encoding="utf-8", errors="replace").splitlines()))
        except Exception:
            pass
    unit = (sum(lines) / len(lines)) if lines else 0
    shared = any((Path(repo) / n).exists() for n in ("CONVENTIONS.md", "STYLE.md", "SPEC.md"))

    res = summary.resources or {}
    # 区分「确实是 0」和「没采到」：缺键就是未知，不能拿 0 顶替
    worker = {"input_tokens": res.get("tokens_in") if "tokens_in" in res else None,
              "cached_input_tokens": res.get("tokens_cached", 0) or 0,
              "output_tokens": res.get("tokens_out", 0) or 0}
    acc = costing.account(worker, costing.h_tokens(run_dir / "tasks.json"),
                          costing.signature(k, unit, shared), k,
                          control=ctrl or None, run_ok=bool(summary.ok))
    return costing.render(acc, control_wall=(ctrl or {}).get("wall_s"),
                          control_note=(ctrl or {}).get("note", ""),
                          enabled=control_arm.enabled())


def auto_apply_enabled() -> bool:
    v = (os.environ.get("ORCH_AUTO_APPLY") or "").strip().lower()
    return v not in {"off", "0", "false", "no"}


def _auto_apply(run_dir: Path, summary) -> str:
    """成功后自动把产物落到用户当前分支，返回一句给人看的说明。

    为什么要自动：不自动的话，用户体验是"跑完了但文件没变"，然后只有两条路——
    让模型自己手抄（没有冲突检测、没有记录），或者让用户回终端敲 apply。
    后者与"用户不碰终端"直接矛盾，前者是我们要堵的灰色动作。

    `apply_run` 本身已经把防护做全了：state 必须是 done、整合分支必须存在、
    不能在游离 HEAD 上合、工作区有未提交改动就不合、冲突自动 abort 并回滚。
    这里只负责把结果说清楚，不做任何"尽力尝试"。
    """
    if not summary.ok or not summary.merged_branch:
        return ""
    if not auto_apply_enabled():
        return ("自动合并已关闭（ORCH_AUTO_APPLY=off）：产物在集成分支，"
                "需要时执行 `orch apply`。")
    try:
        from .apply import apply_run

        res = apply_run(run_dir)
    except Exception as e:                       # noqa: BLE001
        return (f"自动合并出错：{type(e).__name__}: {e}。"
                f"产物在集成分支，可稍后 `orch apply` 重试。")
    if res.errors:
        return f"自动合并未执行：{res.errors[0]}（产物在集成分支，处理后可 `orch apply`）"
    if res.merged:
        return f"已自动合并到当前分支（{len(res.files)} 个文件）。要撤回：`orch undo {res.run_id}`。"
    return "自动合并未产生变化（当前分支已包含这些改动）。"


def _render_done(run_id, text, plan_res, summary, run_dir: Path, apply_note: str = "") -> str:
    title = "执行成功" if summary.ok else {"blocked": "执行被阻断", "waiting_input": "等待用户回答",
                                            "cancelled": "已取消", "resource_limit": "资源上限已到",
                                            "recovery_required": "需要人工检查"}.get(summary.state, "执行失败")
    lines: list[str] = [f"【orch {title}】{run_id}", ""]
    if summary.reason:
        lines.append(f"原因：{summary.reason}")
    if not summary.execution_started:
        lines.append("尚未启动 worker。")
    if summary.ok:
        if getattr(summary, "integration_checked", True):
            lines.append("已通过任务验收、独立整合及用户配置的固定整体验收。")
        else:
            lines.append("已通过任务验收与独立整合。**未配置整体验收**（verification.json），"
                         "因此不声称整体可用——建议补一份再交付。")
    if summary.merged_worktree:
        lines.append(f"整合产物：{summary.merged_worktree}")
    if summary.worker_label:
        lines.append(f"执行者：{summary.worker_label}")
    if summary.resources:
        res = summary.resources
        line = f"资源：模型调用 {res.get('calls', 0)}；检查命令 {res.get('commands', 0)}"
        # token 拿不到就说拿不到。"0" 和 "没测到" 必须能分开——
        # 否则做成本结论时会把"没采集"当成"不花钱"。
        if res.get("tokens_in") is not None:
            line += (f"；token 输入 {res.get('tokens_in', 0):,}"
                     f"（缓存 {res.get('tokens_cached', 0):,}）"
                     f"· 输出 {res.get('tokens_out', 0):,}"
                     f" · 轮次 {res.get('turns', 0):,}")
        else:
            line += "；token 未采集"
        lines.append(line + "；实际金额未知")
    lines.append(f"需求：{_clip(text, 70)}")
    layers = " / ".join(",".join(l) for l in plan_res.layers) or "-"
    lines.append(f"计划：{len(plan_res.tasks)} 任务 · {len(plan_res.layers)} 层 · {layers} "
                 f"· {len(plan_res.attempts)} 次尝试")
    if getattr(summary, "kinds", None):
        from . import kinds as _kinds

        lines.append(f"任务构成：{_kinds.render(summary.kinds.get('counts', {}))}")
    lines.append(f"执行：{len(summary.done)}/{len(summary.outcomes)} 完成 · "
                 f"{'OK' if summary.ok else 'FAIL'}")
    lines.append("")
    for o in summary.outcomes:
        mark = {"done": "DONE ", "failed": "FAIL ", "blocked": "BLOCK"}.get(o.status, o.status)
        detail = f"{len(o.changed)} 文件" if o.changed else ""
        if getattr(o, "diff_stat", ""):
            detail += f" · {o.diff_stat}"
        if o.out_of_scope:
            detail += f" · 越界 {o.out_of_scope}"
        if o.failures:
            detail += f" · 未过 {len(o.failures)} 条"
        lines.append(f"  {mark} {o.task_id}  {o.elapsed_s:5.1f}s  {detail}".rstrip())
        # 验收证据：让人不必翻 diff 也能判断"是不是真跑了、错在哪"
        checks = getattr(o, "checks", None) or []
        if checks:
            passed = sum(1 for c in checks if c.get("ok"))
            lines.append(f"        验收 {passed}/{len(checks)} 通过")
            for c in checks:
                if c.get("ok"):
                    continue
                rc = c.get("rc")
                rc_s = f"rc={rc}" if rc is not None else f"状态={c.get('status', '?')}"
                out = (c.get("out") or "").strip()
                lines.append(f"        ✗ {_clip(c.get('criterion', ''), 60)}  [{rc_s}]")
                if out:
                    lines.append(f"          {_clip(out, 120)}")
                if len([x for x in checks if not x.get("ok")]) > 3 and \
                        checks.index(c) > 2:
                    break
        if o.question:
            lines.append(f"  待确认 {o.task_id}：{o.question}")
    if plan_res.warnings:
        lines.append("")
        lines.append("提示（不阻断）：")
        for w in plan_res.warnings:
            lines.append(f"  {w}")
    if summary.merged_branch:
        lines.append("")
        lines.append(f"整合分支：{summary.merged_branch}")
        if apply_note:
            lines.append(f"工作区：{apply_note}")
    try:
        block = _cost_block(plan_res, summary, run_dir)
    except Exception as e:                       # 核算失败不能拖垮交付
        block = f"## 成本核算\n  （生成失败：{type(e).__name__}: {e}）"
    if block:
        lines.append("")
        lines.append(block.rstrip())
    lines.append("")
    lines.append(f"产物目录：{run_dir}")
    lines.append("  tasks.json / SPEC.md / DISPATCH.md / trace.jsonl")
    lines.append("")
    lines.append("请把以上结果原样汇报给用户。不要据此修改仓库里的任何文件——"
                 "需要改动请等用户明确指示。")
    return "\n".join(lines)


def _render_plan_ok(run_id, text, plan_res, run_dir: Path, codes) -> str:
    lines = [f"【orch 计划已就绪】{run_id}", ""]
    lines.append("尚未执行，未启动 worker。")
    lines.append(f"需求：{_clip(text, 70)}")
    lines.append(f"尝试：{', '.join(codes)}")
    lines.append("")
    for t in plan_res.tasks:
        deps = f" ← {','.join(t.deps or [])}" if getattr(t, "deps", None) else ""
        lines.append(f"  {t.id}  {t.goal}{deps}")
        lines.append(f"       writes: {', '.join(t.writes or [])}")
    if plan_res.warnings:
        lines.append("")
        lines.append("提示（不阻断）：")
        for w in plan_res.warnings:
            lines.append(f"  {w}")
    lines.append("")
    lines.append(f"产物目录：{run_dir}")
    lines.append("")
    lines.append("请原样汇报，不要自行执行或修改文件。")
    return "\n".join(lines)


def _render_plan_failed(run_id, text, plan_res, run_dir: Path) -> str:
    lines = [f"【orch 计划未通过】{run_id}", ""]
    lines.append(f"需求：{_clip(text, 70)}")
    lines.append("")
    lines.append(f"  规划未通过；实际尝试 {len(plan_res.attempts)} 次，尚未执行，未启动单 Agent 降级。")
    for a in plan_res.attempts:
        if a.ok:
            lines.append(f"  PASS attempt {a.n}")
        elif a.parse_error:
            lines.append(f"  FAIL attempt {a.n}  {a.parse_error}")
        else:
            lines.append(f"  FAIL attempt {a.n}  {', '.join(a.codes)}")
    lines.append("")
    lines.append("## 成本核算")
    lines.append(f"  本次 D          未执行（规划阶段就停了），worker 用量未采集")
    lines.append("  A 对照         未运行（规划失败不启动对照组，不重复花钱）")
    lines.append("  节省            无法给出——没有可比的两侧用量，任何百分比都是编的")
    lines.append("")
    lines.append(f"详情：{run_dir / 'plan.failed.json'}")
    lines.append("")
    lines.append("请原样汇报。不要自己拆任务、不要改文件。")
    return "\n".join(lines)


def _render_error(title, run_id, text, detail) -> str:
    tail = "\n".join(detail.strip().splitlines()[-8:])
    return "\n".join([
        f"【orch {title}】{run_id}",
        "",
        f"需求：{_clip(text, 70)}",
        "",
        "```",
        tail,
        "```",
        "",
        "请原样汇报，不要自行修改文件。",
    ])


def _clip(s: str, n: int) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[:n] + "…"


# ---------------------------------------------------------------- 杂项

def _provider(name: str, sandbox: str = "read-only"):
    from .planner import CLIProvider, StubProvider, codex_argv

    if name == "codex":
        return CLIProvider(codex_argv(sandbox=sandbox), name=f"codex-{sandbox}",
                           model="(codex 默认，未指定)")
    if name == "deepseek":
        from .providers import build_named

        # 这里必须把 sandbox 传下去：默认档位是 deepseek，而 Windows 上默认要 bypass。
        # 不传的话日志写 bypass、实际发 workspace-write（真实踩过：worker 起不了进程）。
        prov, why = build_named("deepseek", sandbox=sandbox)
        if prov is None:
            raise ValueError(f"deepseek provider 不可用：{why}")
        return prov
    if name == "stub":
        # 用仓库里那份合法计划当桩，报告才有内容可看（空 tasks 的桩只会render 出一片空白）
        sample = Path(__file__).resolve().parent.parent / "examples" / "tasks.good.json"
        doc = json.loads(sample.read_text(encoding="utf-8"))
        payload = {"spec_md": "# 桩计划\n\n由 stub provider 返回。\n", "tasks": doc["tasks"]}
        return StubProvider([json.dumps(payload, ensure_ascii=False)], name="stub")
    raise ValueError(f"dispatch 不支持的 provider：{name}")


def _read_intent(run_dir: Path) -> dict:
    p = run_dir / "intent.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _patch_meta(run_dir: Path, patch: dict) -> None:
    p = run_dir / "dispatch.json"
    try:
        meta = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        meta = {}
    meta.update(patch)
    p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="orch.dispatch",
        description="把一句话送进引擎，跑完把产物送回同一个 Codex 窗口",
    )
    ap.add_argument("text", nargs="?", help="自然语言需求，原样传入")
    ap.add_argument("--repo", default=".", help="目标仓库")
    ap.add_argument("--runs-dir", default=None,
                    help="运行记录放哪；默认 <repo>/.orch-runs（已自动排除出版本控制）")
    ap.add_argument("--thread", default=None, help="会话 UUID；省略则自动反查")
    ap.add_argument("--nonce", default=None, help="命令文本里的随机串，用于自动反查")
    ap.add_argument("--max-agents", type=int, default=3)
    ap.add_argument("--budget", type=float, default=6.0)
    ap.add_argument("--provider", default="deepseek", choices=["codex", "stub", "deepseek"],
                    help="worker 走哪个档；默认 deepseek（便宜）。codex 会烧 Codex 额度")
    ap.add_argument("--verifier", default="codex", choices=["none", "codex", "stub"])
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--verification", default=None, help="固定整体验收 JSON；默认运行目录 verification.json")
    ap.add_argument("--execution", default=None, help="资源与恢复配置 JSON")
    ap.add_argument("--workers", default=None, help="异构后端配置 JSON")
    ap.add_argument("--resume", action="store_true", help="恢复指定 --job 的已有运行，不重新规划已确认任务")
    ap.add_argument("--worker-sandbox", default=None,
                    choices=["workspace-write", "bypass", "read-only"],
                    help="worker 的沙箱模式；Windows 默认 bypass（workspace-write 会卡死）")
    ap.add_argument("--plan", default=None,
                    help="外部计划 JSON：由调用方（如 Codex）直接给出，跳过 Planner；"
                         "校验不过时交便宜模型修补")
    ap.add_argument("--plan-only", action="store_true", help="只编译计划，不执行")
    ap.add_argument("--notify", action="store_true",
                    help="前台也把报告投回会话（默认只在后台模式投；前台报告已在输出里）")
    ap.add_argument("--no-notify", action="store_true", help="不往窗口回传")
    ap.add_argument("--no-apply", action="store_true",
                    help="成功后不自动合并到当前分支（产物留在集成分支）")
    ap.add_argument("--mode", default="auto",
                    choices=["auto", "serve", "foreground", "detach"],
                    help="auto=有常驻宿主就入队、没有就当场跑；serve=只入队；"
                         "foreground=当场跑完；detach=直接 fork（当前环境活不过命令结束）")
    ap.add_argument("--foreground", action="store_true", help="等价于 --mode foreground")

    # 常驻宿主
    ap.add_argument("--serve", action="store_true", help="常驻，轮询队列并把结果投回窗口")
    ap.add_argument("--once", action="store_true", help="配合 --serve：只消费一轮就退出")
    ap.add_argument("--poll", type=float, default=3.0, help="轮询间隔秒数")
    ap.add_argument("--status", action="store_true", help="看宿主是否在跑、队列里有没有活")

    # 内部模式：detach 拉起的进程走这里
    ap.add_argument("--job", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--pids", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--fingerprint", default=None, help=argparse.SUPPRESS)

    args = ap.parse_args(argv)

    if args.status:
        pid = serve_pid()
        qdir = _queue_dir()
        pending = sorted(p.stem for p in qdir.glob("*.json"))
        print(f"宿主      {'pid ' + str(pid) if pid else '未启动'}")
        print(f"队列      {len(pending)} 个待办  {qdir}")
        for r in pending:
            print(f"          {r}")
        if not pid and pending:
            print("\n  宿主没跑，队列里的任务不会执行。先起一个：")
            print("    python -m orch.dispatch --serve")
        return 0

    if args.serve:
        return serve(poll=args.poll, once=args.once, runs_dir=args.runs_dir)

    # 自动合并的开关走环境变量：--job 是另一个进程、serve 还要跨进程投递，
    # 只有环境变量在这几条路径上都成立。
    if args.no_apply:
        os.environ["ORCH_AUTO_APPLY"] = "off"

    if args.job:
        pids = [int(x) for x in args.pids.split(",") if x.strip()] if args.pids else None
        return run_job(
            args.job, runs_dir=args.runs_dir, repo=args.repo,
            thread_id=args.thread, nonce=args.nonce, pids=pids,
            notify=not args.no_notify, plan_only=args.plan_only,
            provider=args.provider, verifier=args.verifier, timeout=args.timeout,
            fingerprint=args.fingerprint,
            verification_path=args.verification,
            execution_path=args.execution, workers_path=args.workers, resume=args.resume,
        )

    if not args.text:
        ap.print_help()
        return 2

    if args.resume:
        ap.error("恢复已有运行请使用 --job <run_id> --resume 或 orch run <run_id> --resume")

    mode = "foreground" if args.foreground else args.mode
    runs_dir = args.runs_dir or str(Path(args.repo).resolve() / ".orch-runs")
    notify = not args.no_notify
    d, code = dispatch(
        args.text, repo=args.repo, runs_dir=runs_dir,
        thread_id=args.thread, nonce=args.nonce,
        max_agents=args.max_agents, budget=args.budget,
        detach=(mode != "foreground"), notify=notify, force_notify=args.notify,
        plan_only=args.plan_only, provider=args.provider,
        verifier=args.verifier, timeout=args.timeout, mode=mode,
        verification_path=args.verification,
        execution_path=args.execution, workers_path=args.workers, resume=args.resume,
        plan_path=args.plan, worker_sandbox=args.worker_sandbox,
    )
    if getattr(d, "note", ""):
        print(f"调度     {d.note}")
    print(f"run      {d.run_id}")
    print(f"          {d.run_dir}")
    if d.detached:
        print(f"         已入队，宿主 pid {d.pid} 会取走；跑完投回窗口")
    print(f"         日志 {Path(d.run_dir) / 'dispatch.log'}")
    return code if not d.detached else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
