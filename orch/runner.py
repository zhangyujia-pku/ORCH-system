"""
runner：按层发车，层内并发，层间屏障。

每个任务的完整生命周期：
    worktree → worker → 越界检查 → commit → verifier → done

每层从同一整合版本创建工作目录，验收后按计划顺序整合，再启动下一层。
越界检查放在引擎 commit 之前，检查相对任务起点的路径（含 worker 自行提交）。
一条越界就废掉整个 attempt，不 commit、不验证，省掉后面的所有开销。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
import subprocess
import json
import shutil
import uuid
from contextlib import nullcontext

from .contract import Report, Task, validate_document
from .planner import Provider
from .trace import Tracer
from .verifier import Check, collect_file_context, verify_task
from .worker import run_worker
from .verification import is_protected, load_policy, protected_declarations, verify_integration
from .worktree import GitError, MergeResult, WorktreeManager
from . import handoff as handoff_mod
from . import kinds as kinds_mod
from .heartbeat import Heartbeat
from .control import ControlledProvider, RunControl, RunLock, RunStopped, load_execution
from .recovery import Recovery, find_answer, manifest_digest


def _file_state(root: Path, files: list[str]) -> dict[str, str]:
    """产物文件的内容指纹。

    验收命令本身是"被判者"，它不许改产物——改了就等于自己给自己出题。
    这一层比 assert_snapshot 更细：后者看提交点，前者看具体文件内容。
    """
    import hashlib

    out: dict[str, str] = {}
    for f in files or []:
        p = Path(root) / f
        try:
            out[f] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "<absent>"
        except OSError:
            out[f] = "<unreadable>"
    return out


def _diff_stat(diff: str) -> str:
    """从 unified diff 里数增删行，只用于报告展示。"""
    add = sum(1 for ln in diff.splitlines()
              if ln.startswith("+") and not ln.startswith("+++"))
    rem = sum(1 for ln in diff.splitlines()
              if ln.startswith("-") and not ln.startswith("---"))
    return f"+{add} −{rem}" if (add or rem) else ""


@dataclass
class TaskOutcome:
    task_id: str
    status: str  # done | failed | blocked
    branch: str = ""
    provider: str = ""
    changed: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0
    note: str = ""
    base_sha: str = ""
    head_sha: str = ""
    retryable: bool = False
    question: str = ""
    attempts: int = 0
    # 验收证据：报告里要能看见"到底跑了什么、返回了什么"，而不是只说"8/8 通过"。
    # 只存摘要（判据 + 是否通过 + 退出码 + 输出首行），完整 stdout 在 artifacts/*.verify.json。
    checks: list[dict] = field(default_factory=list)
    diff_stat: str = ""


@dataclass
class RunSummary:
    run_id: str
    outcomes: list[TaskOutcome] = field(default_factory=list)
    merged_branch: str = ""
    merge_results: list[MergeResult] = field(default_factory=list)
    ok: bool = False
    state: str = "running"
    reason: str = ""
    execution_started: bool = False
    operation_id: str = ""
    merged_worktree: str = ""
    final_verified: bool = False
    final_checks: list[Check] = field(default_factory=list)
    resources: dict = field(default_factory=dict)
    worker_label: str = ""      # 实际干活的档位与模型，报告里要写出来
    kinds: dict = field(default_factory=dict)   # 任务构成：代码/文档/表格…
    integration_checked: bool = True   # 有没有跑过整体验收

    @property
    def done(self) -> list[TaskOutcome]:
        return [o for o in self.outcomes if o.status == "done"]


def _load_plan(run_dir: Path) -> tuple[Report, str, dict]:
    import json

    doc = json.loads((run_dir / "tasks.json").read_text(encoding="utf-8"))
    rep = validate_document(doc, [])
    spec = ""
    sp = run_dir / "SPEC.md"
    if sp.exists():
        spec = sp.read_text(encoding="utf-8")
    meta = {}
    mp = run_dir / "intent.json"
    if mp.exists():
        meta = json.loads(mp.read_text(encoding="utf-8"))
    return rep, spec, meta


def missing_git_identity(repo: Path) -> str:
    """仓库能不能提交？不能就别启动 worker。

    这个问题问过才知道多贵：10 个 worker 全干完了，到提交那一步才发现
    没配 user.email，10 次模型调用白烧。前置检查要放在"花钱之前"。
    """
    for key in ("user.email", "user.name"):
        try:
            r = subprocess.run(["git", "config", key], cwd=str(repo),
                               capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return "git 不可用"
        if r.returncode != 0 or not r.stdout.strip():
            return f"未配置 {key}"
    return ""



def run_plan(
    run_dir: str | Path,
    worker_provider: Provider | None = None,
    verifier_provider: Provider | None = None,
    max_agents: int = 3,
    timeout: int = 900,
    keep_worktrees: bool = True,
    repo_rules: str = "",
    workers_path: str | None = None,
    operation_id: str | None = None,
    verification_path: str | Path | None = None,
    execution_path: str | Path | None = None,
    resume: bool = False,
    control: RunControl | None = None,
    _lock_held: bool = False,
) -> RunSummary:
    directory = Path(run_dir)
    operation_id = operation_id or uuid.uuid4().hex
    try:
        # 心跳与锁同时进入：锁证明"进程活着"，心跳证明"它在推进"。
        # 收尾时心跳自动停写，cleanup 靠这两个信号判活。
        with (nullcontext() if _lock_held else RunLock(directory)), Heartbeat(directory, operation_id):
            path = Path(execution_path) if execution_path else directory / "execution.json"
            if execution_path and not path.exists():
                raise ValueError(f"执行配置不存在：{path}")
            limits = load_execution(path)
            runtime = control or RunControl(directory, limits, resume)
            if runtime.policy != limits:
                raise ValueError("共享资源控制与执行配置不一致")
            recovery = Recovery(directory, resume)
            try:
                summary = _execute_plan(directory, worker_provider, verifier_provider, max_agents, timeout,
                                        keep_worktrees, repo_rules, workers_path, operation_id,
                                        verification_path, runtime, recovery, resume)
            except RunStopped as e:
                summary = RunSummary(directory.name, state=runtime.state, reason=str(e),
                                     operation_id=operation_id or uuid.uuid4().hex,
                                     outcomes=[TaskOutcome(**o) for o in recovery.data["outcomes"]],
                                     merged_worktree=recovery.data.get("integration_path", ""))
                summary.execution_started = any(e.get("operation_id") == operation_id and e.get("event") == "task.start"
                                                for e in Tracer(directory / "trace.jsonl").events())
            # 注意：这里会用运行控制器的计数覆盖 resources。
            # token 是 _execute_plan 里累加的，不在 runtime.data 里，
            # 直接覆盖会把用量冲掉——现象是 trace 里有、报告里"未采集"。
            _tokens = {k: v for k, v in summary.resources.items()
                       if k.startswith("tokens_") or k == "turns"}
            summary.resources = {k: v for k, v in runtime.data.items() if k != "active"}
            summary.resources.update(_tokens)
            recovery.save(state=summary.state, summary=asdict(summary))
            if runtime.state:
                Tracer(directory / "trace.jsonl", operation_id=summary.operation_id).emit(
                    "run.end", ok=False, state=summary.state, reason=summary.reason,
                    execution_started=summary.execution_started)
            return summary
    except (OSError, ValueError, GitError) as e:
        summary = RunSummary(directory.name, state="blocked", reason=str(e), operation_id=operation_id or uuid.uuid4().hex)
        Tracer(directory / "trace.jsonl", operation_id=summary.operation_id).emit(
            "run.end", ok=False, state="blocked", reason=str(e), execution_started=False)
        return summary


def _execute_plan(run_dir, worker_provider, verifier_provider, max_agents, timeout,
                  keep_worktrees, repo_rules, workers_path, operation_id,
                  verification_path, control, recovery, resume):
    run_dir = Path(run_dir)
    tracer = Tracer(run_dir / "trace.jsonl", operation_id=operation_id)
    tracer.session = 1 + sum(1 for e in tracer.events() if e.get("event") == "run.start")
    summary = RunSummary(run_id=run_dir.name, operation_id=tracer.operation_id)
    tracer.emit("run.preflight", run_id=summary.run_id, execution_started=False)

    def blocked(reason: str) -> RunSummary:
        summary.state = "blocked"
        summary.reason = reason
        tracer.emit("run.blocked", reason=reason, execution_started=False)
        tracer.emit("run.end", ok=False, state="blocked", reason=reason,
                    done=0, total=0, execution_started=False)
        return summary

    try:
        rep, spec_md, meta = _load_plan(run_dir)
    except (OSError, ValueError) as e:
        return blocked(f"计划或运行记录无法读取：{e}")
    if not rep.ok:
        return blocked(f"tasks.json 未通过校验：{[v.code for v in rep.violations]}")
    if not rep.tasks:
        return blocked("计划没有任务，不能报告执行成功")

    repo = Path(meta.get("repo", "."))
    run_id = run_dir.name

    # 基线必须钉死在 intake 时记录的那个 commit。
    # 用"当前 HEAD"会导致：同一个 run 重跑一次，起点就变成了上次合并后的状态，
    # 两次结果不可比，而且会悄悄把上一次的改动当成"原有代码"。
    recorded = (meta.get("repo_head") or "").strip()
    if not recorded:
        return blocked("缺少已记录的仓库基线；请重新建立运行记录。" + meta.get("repo_head_error", ""))
    import re
    if not re.fullmatch(r"[0-9a-fA-F]{4,64}", recorded):
        return blocked(f"基线标识非法：{recorded}")
    try:
        check = subprocess.run(
            ["git", "rev-parse", "--verify", f"{recorded}^{{commit}}"],
            cwd=str(repo), capture_output=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return blocked(f"无法确认仓库基线：{e}")
    if check.returncode:
        tracer.emit("run.base_missing", recorded=recorded)
        return blocked(f"记录的仓库基线不可用：{recorded}；未替换为当前 HEAD")
    from .contract import decode_bytes
    base = decode_bytes(check.stdout).strip()
    tracer.emit("run.base_resolved", recorded=recorded, resolved=base)

    # 异构分派：给了 workers.json 就按能力路由，没给就用单一 provider
    if not workers_path and any(t.needs for t in rep.tasks):
        return blocked("任务声明了外部能力；未提供 workers 配置，无法核验所选后端能力")
    if workers_path:
        from .providers import build_router
        from .workers import load_workers
        from .workers import route as route_tasks

        try:
            ws, ladder = load_workers(workers_path, strict=True)
            assigns = route_tasks(rep.tasks, ws, ladder)
            for a in assigns:
                tracer.emit("route.decided", task_id=a.task_id, worker=a.worker,
                            reason=a.reason, detail=a.detail, routable=a.routable,
                            needs=list(a.needs), gap=list(a.gap), prediction=False)
            if {a.task_id for a in assigns} != {t.id for t in rep.tasks} or len(assigns) != len(rep.tasks):
                return blocked("分派表与计划任务不一致")
            rejected = [a for a in assigns if not a.routable]
            if rejected:
                return blocked("路由阻断：" + "; ".join(f"{a.task_id}: {a.detail}" for a in rejected))
            amap = {a.task_id: a.worker for a in assigns}
            chains = {a.task_id: [a.worker, *a.alternatives] for a in assigns} if control.policy.switch_workers else None
            router, problems = build_router(amap, ws, candidates=chains)
        except (OSError, ValueError, KeyError) as e:
            return blocked(f"路由配置不可用：{e}")
        if router is None:
            return blocked("分派适配器未构建齐全：" + "; ".join(problems))
        worker_provider = router
        amap = dict(router.assign)
        tracer.emit("route.result", assign=amap, problems=problems,
                    distribution=router.distribution())

    if worker_provider is None:
        return blocked("未配置 worker")
    # 所有任务在任何 worker 启动前完成分派预检查。
    try:
        if hasattr(worker_provider, "for_task"):
            for task in rep.tasks:
                worker_provider.for_task(task)
    except (KeyError, ValueError) as e:
        return blocked(f"分派不完整：{e}")
    # 默认位置要"跟着人走"：验收配置属于仓库，不属于某次运行。
    # 只找 run_dir 会导致「明明放在仓库根却报缺失」，逼用户每次都传 --verification。
    if verification_path:
        policy_path = Path(verification_path)
    elif (repo / "verification.json").exists():
        policy_path = repo / "verification.json"
    elif (run_dir / "verification.json").exists():
        policy_path = run_dir / "verification.json"
    else:
        policy_path = None

    policy = None
    if policy_path is not None:
        try:
            policy = load_policy(policy_path, repo, base)
        except (OSError, ValueError, GitError) as e:
            # 文件在但读不动 = 配置坏了，必须停；文件不在 = 没配，可以跳过
            return blocked(f"固定整体验收配置不可用：{e}")
    else:
        tracer.emit("verification.skipped",
                    why="仓库没有 verification.json：本次只做任务级验收，不声称整体可用")
    missing = missing_git_identity(repo)
    if missing:
        return blocked(f"仓库不能提交（{missing}）：worker 干完也会卡在 commit。"
                       "先执行 git config user.name / user.email")
    for task in rep.tasks:
        declared = protected_declarations(task.writes, policy) if policy else []
        if declared:
            return blocked(f"任务 {task.id} 的 writes 包含固定检查文件：{declared}")
    signature = manifest_digest(base, meta, str(repo.resolve()), max_agents, timeout, repo_rules, json.loads((run_dir / "tasks.json").read_text(encoding="utf-8")),
                                spec_md, policy.digest if policy else "", asdict(control.policy),
                                Path(workers_path).read_text(encoding="utf-8") if workers_path and Path(workers_path).exists() else workers_path,
                                getattr(worker_provider, "name", type(worker_provider).__name__), getattr(worker_provider, "argv", []), getattr(verifier_provider, "name", None), getattr(verifier_provider, "argv", []))
    recovery.validate(signature)
    execution_id = recovery.data["execution_id"]
    manager = WorktreeManager(repo, run_dir / "wt" / execution_id,
                              prefix=f"orch/{run_id}/{execution_id[:8]}", integration_key=execution_id, control=control)
    # 整合目录固定在运行目录，跨 resume 复用同一条已确认历史。
    manager.integration_path = run_dir / "integration" / execution_id
    if resume and recovery.data["phase"] == "integrating":
        raise ValueError("上次中断于整合阶段，必须人工核对；不会猜测或重复合并")
    if resume and recovery.data.get("integration_path"):
        expected = recovery.data["base"]
        manager.assert_snapshot(manager.integration_path, expected)
    if resume and recovery.data.get("state") == "done":
        saved = dict(recovery.data["summary"])
        saved["outcomes"] = [TaskOutcome(**o) for o in saved["outcomes"]]
        saved["merge_results"] = [MergeResult(**m) for m in saved["merge_results"]]
        saved["final_checks"] = [Check(**c) for c in saved["final_checks"]]
        saved["operation_id"] = tracer.operation_id
        saved["execution_started"] = False
        saved["reason"] = "复用已验收的恢复点，无需重复执行"
        tracer.emit("run.end", ok=True, state="done", final_verified=True, execution_started=False,
                    reason=saved["reason"])
        return RunSummary(**saved)
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(exist_ok=True)
    if policy is not None:
        (artifacts / f"verification-{tracer.operation_id}.config.json").write_text(
            __import__("json").dumps(asdict(policy), ensure_ascii=False, indent=2), encoding="utf-8")
        tracer.emit("verification.config", path=policy.config_path, digest=policy.digest,
                    protected=list(policy.protected_blobs), checks=len(policy.checks))
    tracer.emit("run.start", run_id=run_id, base=base, n_tasks=len(rep.tasks),
                layers=rep.layers, max_agents=max_agents,
                worker=getattr(worker_provider, "name", None),
                verifier=getattr(verifier_provider, "name", None))

    by_id = {t.id: t for t in rep.tasks}
    summary.outcomes = [TaskOutcome(**o) for o in recovery.data["outcomes"]]
    summary.merge_results = [MergeResult(**m) for m in recovery.data["merges"]]
    summary.merged_branch = recovery.data.get("merged_branch", "")
    summary.merged_worktree = recovery.data.get("integration_path", "")
    status: dict[str, str] = {o.task_id: o.status for o in summary.outcomes}
    layer_base = recovery.data["base"] or base
    integration_failed = False

    # 任务构成：按 writes 后缀算，零模型成本。它回答"这批活里有多少是能机械验收的"。
    kind_info = kinds_mod.summary(rep.tasks)
    summary.kinds = kind_info
    tracer.emit("run.kinds", **kind_info)

    # 串联模式才做交接：并行层之间互不依赖，没有"前一步"可传
    try:
        mode = json.loads((run_dir / "tasks.json").read_text(encoding="utf-8")).get("mode", "auto")
    except (OSError, ValueError):
        mode = "auto"
    handoffs: dict[str, dict] = {}
    # token 用量按"每次尝试"累加：重试也是真花钱，只记最后一次会把成本记少。
    # 拿不到就留空——不编造 0，否则"零消耗"和"没测到"会混成同一个数字。
    usage_acc: dict[str, int] = {}
    tracer.emit("run.mode", mode=mode)

    def _handoff_for(task: Task) -> str:
        if mode != "chain":
            return ""
        # inputs_from 说"读什么"，比 deps 的"先后"更精确；没写才退回 deps
        srcs = task.inputs_from or task.deps or ()
        parts = [handoff_mod.render(handoffs[d]) for d in srcs if d in handoffs]
        return "\n\n".join(x for x in parts if x)

    for li, layer in enumerate(rep.layers):
        if li < recovery.data["next_layer"]:
            continue
        control.check()
        recovery.save(phase="running", next_layer=li, base=layer_base)
        runnable: list[Task] = []
        for tid in layer:
            if status.get(tid) in {"done", "failed", "blocked"}:
                continue
            t = by_id[tid]
            if integration_failed or any(status.get(d) != "done" for d in t.deps):
                reason = "前层成果整合失败，后续层停止发车" if integration_failed else "依赖未完成"
                status[tid] = "blocked"
                summary.outcomes.append(
                    TaskOutcome(task_id=tid, status="blocked", note=reason)
                )
                tracer.emit("task.blocked", task_id=tid, deps=list(t.deps), reason=reason)
            else:
                runnable.append(t)

        if not runnable:
            continue
        summary.outcomes = [o for o in summary.outcomes if o.task_id not in {t.id for t in runnable}]

        tracer.emit("layer.start", layer=li, tasks=[t.id for t in runnable], base=layer_base,
                    visibility="all_prior_integrated_layers")
        layer_outcomes: dict[str, TaskOutcome] = {}
        with ThreadPoolExecutor(max_workers=max(1, max_agents)) as pool:
            futures = {
                pool.submit(
                    _run_retry, t, manager, layer_base, worker_provider,
                    verifier_provider, spec_md, repo_rules, timeout, artifacts, tracer,
                    tuple(policy.protected_patterns) if policy else (),
                    control, recovery, _handoff_for(t), usage_acc,
                ): t
                for t in runnable
            }
            for fut in as_completed(futures):
                try:
                    out = fut.result()
                except Exception as e:
                    if isinstance(e, RunStopped):
                        raise
                    task = futures[fut]
                    tracer.emit("task.failed", task_id=task.id, reason=f"{type(e).__name__}: {e}")
                    out = TaskOutcome(task_id=task.id, status="failed", note=str(e), base_sha=layer_base)
                summary.outcomes.append(out)
                layer_outcomes[out.task_id] = out
                status[out.task_id] = out.status
        control.check()
        # 层内并发结束后单线程整合，顺序来自计划中的精确任务 id，不受完成快慢影响。
        accepted = [layer_outcomes[t.id] for t in runnable
                    if layer_outcomes[t.id].status == "done" and layer_outcomes[t.id].branch]
        if accepted:
            recovery.save(phase="integrating")
            branches = [out.branch for out in accepted]
            try:
                target, merges = manager.merge_all(branches, layer_base, run_id)
            except GitError as e:
                target, merges = "", [MergeResult(branches[0], False, str(e))]
            summary.merged_branch = target or summary.merged_branch
            if target:
                summary.merged_worktree = str(manager.integration_path)
            summary.merge_results.extend(merges)
            tracer.emit("merge.result", layer=li, base=layer_base, target=target,
                        results=[{"branch": m.branch, "ok": m.ok, "detail": m.detail} for m in merges])
            results = {m.branch: m for m in merges}
            integration_failed = any(not m.ok for m in merges) or len(merges) != len(accepted)
            for out in accepted:
                result = results.get(out.branch)
                if result and result.ok:
                    tracer.emit("task.integrated", task_id=out.task_id, branch=out.branch,
                                base=out.base_sha, head=out.head_sha, layer=li)
                    if mode == "chain":
                        f = artifacts / f"{out.task_id}-a{out.attempts}.out.md"
                        raw = f.read_text(encoding="utf-8") if f.exists() else (out.note or "")
                        entry = handoff_mod.store(run_dir, out.task_id, handoff_mod.extract(raw))
                        if entry["overflow"]:
                            tracer.emit("handoff.overflow", task_id=out.task_id,
                                        chars=len(raw), path=entry["path"],
                                        why="交接超 200 字，改为摘要 + 文件引用")
                        handoffs[out.task_id] = entry
                else:
                    out.status = "failed" if result else "blocked"
                    out.note = (f"成果整合失败：{result.detail}" if result else
                                "同层整合中断，该成果尚未接纳")
                    out.branch = ""
                    status[out.task_id] = out.status
                    tracer.emit("task.integration_failed", task_id=out.task_id, reason=out.note)
            if not integration_failed:
                try:
                    next_base = manager.integrated_sha()
                except GitError as e:
                    integration_failed = True
                    summary.merge_results.append(MergeResult(target, False, str(e)))
                    tracer.emit("layer.integration_failed", layer=li, reason=str(e))
                else:
                    tracer.emit("layer.integrated", layer=li, base_before=layer_base,
                                base_after=next_base, tasks=[o.task_id for o in accepted])
                    layer_base = next_base
            if integration_failed:
                tracer.emit("layer.integration_failed", layer=li, reason="整合未完整成功，停止后续层")
        waiting = any(o.status == "waiting_input" for o in summary.outcomes)
        recovery.save(phase="checkpoint", next_layer=li if waiting else li + 1, base=layer_base,
                      outcomes=[asdict(o) for o in summary.outcomes], merges=[asdict(m) for m in summary.merge_results],
                      integration_path=summary.merged_worktree, merged_branch=summary.merged_branch)
        if waiting:
            summary.state, summary.reason = "waiting_input", "任务需要用户回答；原需求未改写"
            summary.execution_started = any(e.get("operation_id") == tracer.operation_id and e.get("event") == "task.start"
                                            for e in tracer.events())
            tracer.emit("run.end", ok=False, state=summary.state, reason=summary.reason,
                        execution_started=summary.execution_started)
            return summary

    summary.execution_started = any(
        e.get("operation_id") == tracer.operation_id and e.get("event") == "task.start"
        for e in tracer.events()
    )
    summary.ok = all(o.status == "done" for o in summary.outcomes) and all(
        m.ok for m in summary.merge_results
    )
    if usage_acc:
        summary.resources = {
            **summary.resources,
            "tokens_in": usage_acc.get("input_tokens", 0),
            "tokens_cached": usage_acc.get("cached_input_tokens", 0),
            "tokens_out": usage_acc.get("output_tokens", 0),
            "tokens_reasoning": usage_acc.get("reasoning_output_tokens", 0),
            "turns": usage_acc.get("turns", 0),
        }
        tracer.emit("run.usage", **summary.resources)

    summary.integration_checked = policy is not None
    if summary.ok and policy is None:
        # 没配整体验收：任务级与整合都过了，但不能说"整体可用"——那是没验过的事
        tracer.emit("run.final_verify_skipped", why="未配置 verification.json")
    if summary.ok and policy is not None:
        tracer.emit("run.final_verify_start", path=summary.merged_worktree, sha=layer_base)
        verdict = verify_integration(policy, manager.integration_path, layer_base, timeout, control=control)
        summary.final_verified = verdict.ok
        summary.final_checks = verdict.checks
        summary.ok = verdict.ok
        (artifacts / f"final-{tracer.operation_id}.verify.json").write_text(
            __import__("json").dumps([asdict(c) for c in verdict.checks], ensure_ascii=False, indent=2), encoding="utf-8")
        tracer.emit("run.final_verify", ok=verdict.ok, sha=layer_base,
                    checks=[{"criterion": c.criterion, "ok": c.ok, "status": c.status} for c in verdict.checks])
    if not keep_worktrees:
        for wt in manager.root.iterdir():
            manager.remove_dir(wt)
    summary.state = "done" if summary.ok else "failed"
    if control.state:
        summary.ok, summary.state = False, control.state
    if not summary.ok:
        summary.reason = "；".join(o.note for o in summary.outcomes if o.status != "done")
        summary.reason += "；".join(m.detail for m in summary.merge_results if not m.ok)
        summary.reason += "；".join(f"整体验收 {c.status}：{c.criterion}" for c in summary.final_checks if not c.ok)
    tracer.emit("run.end", ok=summary.ok,
                done=len(summary.done), total=len(summary.outcomes),
                merged=summary.merged_branch, state=summary.state, reason=summary.reason,
                execution_started=summary.execution_started, final_verified=summary.final_verified,
                merged_worktree=summary.merged_worktree)
    return summary


def _archive_attempt(artifacts, task_id, number, recovery):
    archive = artifacts / "history" / recovery.data["execution_id"]
    archive.mkdir(parents=True, exist_ok=True)
    for source in artifacts.glob(f"{task_id}-a{number}.*"):
        shutil.copy2(source, archive / source.name)


def _run_retry(task, manager, base, worker_provider, verifier_provider, spec_md, repo_rules,
               timeout, artifacts, tracer, protected_paths, control, recovery, handoff="",
               usage_acc=None):
    waiting = recovery.data["waiting"].get(task.id)
    clarification = recovery.data.get("clarifications", {}).get(task.id, "")
    feedback = clarification
    if waiting:
        answer = find_answer(recovery.directory, recovery.data["execution_id"], task.id, waiting["id"])
        if not answer:
            return TaskOutcome(task.id, "waiting_input", question=waiting["question"], note="尚未收到对应问题的新回答")
        clarification += f"\n用户澄清（原话保留）：{json.dumps(answer, ensure_ascii=False)}"
        feedback = clarification
        with recovery.lock:
            recovery.data["waiting"].pop(task.id)
            recovery.data.setdefault("clarifications", {})[task.id] = clarification
            recovery.save()
    last = TaskOutcome(task.id, "failed", note="自动尝试次数已经耗尽")
    while recovery.data["failures"].get(task.id, 0) < control.policy.max_attempts:
        control.check()
        number = recovery.next_attempt(task.id)
        provider = (worker_provider.for_attempt(task, number, control.policy.switch_after)
                    if control.policy.switch_workers and hasattr(worker_provider, "for_attempt") else
                    worker_provider.for_task(task) if hasattr(worker_provider, "for_task") else worker_provider)
        tracer.emit("task.attempt", task_id=task.id, attempt=number, provider=provider.name,
                    model=getattr(provider, "model", ""))
        try:
            last = _run_one(task, manager, base, worker_provider, verifier_provider, spec_md, repo_rules,
                            timeout, artifacts, tracer, protected_paths, number, feedback, control, provider,
                            handoff=handoff, usage_acc=usage_acc)
        except RunStopped:
            _archive_attempt(artifacts, task.id, number, recovery)
            raise
        except Exception as e:
            last = TaskOutcome(task.id, "failed", note=f"{type(e).__name__}: {e}", base_sha=base)
        last.attempts = number
        _archive_attempt(artifacts, task.id, number, recovery)
        tracer.emit("task.attempt_end", task_id=task.id, attempt=number, status=last.status,
                    retryable=last.retryable, note=last.note)
        control.check()
        if last.question:
            recovery.wait(task.id, last.question)
            return last
        if last.status == "done" or not last.retryable:
            return last
        recovery.failed(task.id)
        diff_file = artifacts / f"{task.id}-a{number}.diff"
        diff = diff_file.read_text(encoding="utf-8") if diff_file.exists() else ""
        feedback = clarification + "\n上次失败证据，请在相同修改范围内修复：\n" + json.dumps(asdict(last), ensure_ascii=False) + "\n" + diff[-6000:]
    return last


def _run_one(
    task: Task,
    manager: WorktreeManager,
    base: str,
    worker_provider: Provider,
    verifier_provider: Provider | None,
    spec_md: str,
    repo_rules: str,
    timeout: int,
    artifacts: Path,
    tracer: Tracer,
    protected_paths: tuple[str, ...] = (),
    attempt: int = 1,
    feedback: str = "",
    control=None,
    provider_override=None,
    handoff: str = "",
    usage_acc: dict | None = None,
) -> TaskOutcome:
    t0 = time.time()
    # 单 worker 时就是它自己；带路由时按分派表取——这是"异构"唯一的接入点
    provider = provider_override or (worker_provider.for_task(task) if hasattr(worker_provider, "for_task") else worker_provider)
    prov_name = getattr(provider, "name", "")

    if control:
        provider = ControlledProvider(provider, control)
        verifier_provider = ControlledProvider(verifier_provider, control) if verifier_provider else None
    wt = manager.create(task.id, attempt, base)
    tracer.emit("task.start", task_id=task.id, branch=wt.branch,
                worktree=str(wt.path), provider=prov_name, base=wt.base, deps=list(task.deps))

    res = run_worker(task, wt, provider, manager, spec_md, repo_rules, timeout,
                     feedback=feedback, handoff=handoff)
    _usage = dict(getattr(res, "usage", {}) or {})
    if _usage:
        # 每次尝试都累加：重试烧掉的钱也是钱，只记最后一次会把成本记少。
        if usage_acc is not None:
            for k, v in _usage.items():
                usage_acc[k] = usage_acc.get(k, 0) + int(v or 0)
        tracer.emit("task.usage", task_id=task.id, attempt=attempt,
                    provider=prov_name, model=getattr(provider, "model", ""), **_usage)
    (artifacts / f"{task.id}-a{attempt}.out.md").write_text(res.raw or "", encoding="utf-8")
    if res.audit_failed:
        tracer.emit("task.audit_failed", task_id=task.id, attempt=attempt, reason=res.error)
        return TaskOutcome(task.id, "failed", note=res.error or "修改核验失败", base_sha=base)
    protected_changes = [p for p in res.changed if is_protected(p, protected_paths)]
    if protected_changes:
        res.out_of_scope = sorted(set(res.out_of_scope) | set(protected_changes))
    diff = manager.diff_text(wt)
    (artifacts / f"{task.id}-a{attempt}.diff").write_text(diff, encoding="utf-8")

    # 越界检查：只看路径，不看内容。越界就不 commit、不验证。
    if res.out_of_scope:
        tracer.emit("task.out_of_scope", task_id=task.id, files=res.out_of_scope)
        return TaskOutcome(
            task_id=task.id, status="failed", branch="", provider=prov_name, changed=res.changed,
            out_of_scope=res.out_of_scope, elapsed_s=time.time() - t0,
            note="越界写入，已废掉该 attempt", base_sha=wt.base,
        )
    if res.question:
        return TaskOutcome(task.id, "waiting_input", question=res.question, note="等待用户澄清", base_sha=base)
    if res.error:
        tracer.emit("task.failed", task_id=task.id, reason=res.error)
        return TaskOutcome(task_id=task.id, status="failed", branch="", provider=prov_name,
                           changed=res.changed, elapsed_s=time.time() - t0, note=res.error, base_sha=wt.base, retryable=True)

    try:
        commit = manager.commit(wt, f"orch: {task.id} {task.goal}")
    except GitError as e:
        tracer.emit("task.commit_failed", task_id=task.id, reason=str(e))
        return TaskOutcome(task_id=task.id, status="failed", provider=prov_name,
                           changed=res.changed, elapsed_s=time.time() - t0, note=f"提交失败：{e}", base_sha=wt.base)
    commit_event = ("task.committed" if commit.created else
                    "task.existing_commit" if commit.sha != wt.base else "task.no_commit")
    tracer.emit(commit_event, task_id=task.id, changed=res.changed, sha=commit.sha,
                base=wt.base, created_by_engine=commit.created)

    # 提交完立刻退回未暂存状态，再验收。
    # 原因：判据大量是 `git diff --name-only` / `git diff --unified=0`，它们比的是**工作区**；
    # 提交之后工作区是干净的，这些判据一律输出空，表现为"worker 改对了、验收 0/3 通过"。
    # 真跑 README.md 那次就是这么假失败的。验收过了再重新提交一次（只加 worker 改过的文件，
    # 免得把验收命令产生的缓存一并带进去）。
    before_state = _file_state(wt.path, res.changed)
    if commit.created:
        try:
            manager.uncommit(wt, wt.base)
        except GitError as e:
            tracer.emit("task.uncommit_failed", task_id=task.id, reason=str(e))

    verdict = verify_task(
        task, wt, diff, verifier_provider, timeout=timeout,
        files_context=collect_file_context(wt.path, res.changed), control=control,
    )
    after_state = _file_state(wt.path, res.changed)
    if after_state != before_state:
        # 验收命令把产物改了：不能凭"它自己改完再判自己"过。
        verdict.ok = False
        changed_now = sorted(k for k in after_state if before_state.get(k) != after_state.get(k))
        verdict.checks.append(Check("验收期间产物未被改动", "integrity", False,
                                    f"这些文件在验收过程中变了：{', '.join(changed_now) or '（新增/删除）'}",
                                    status="mutation"))
    (artifacts / f"{task.id}-a{attempt}.verify.json").write_text(
        __import__("json").dumps(
            [asdict(c) for c in verdict.checks],
            ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    head_sha = wt.base
    if verdict.ok:
        try:
            commit = manager.commit(wt, f"orch: {task.id} {task.goal}", files=res.changed)
        except GitError as e:
            tracer.emit("task.commit_failed", task_id=task.id, reason=str(e))
            verdict.ok = False
            verdict.checks.append(Check("产物可以提交", "commit", False, str(e)))
        else:
            head_sha = commit.sha
            try:
                manager.assert_snapshot(wt.path, commit.sha)
            except GitError as e:
                verdict.ok = False
                verdict.checks.append(Check("验收期间版本保持不变", "integrity", False, str(e),
                                            status="mutation"))

    tracer.emit("task.verify", task_id=task.id, ok=verdict.ok,
                failed=[c.criterion for c in verdict.failures],
                statuses=[c.status for c in verdict.checks])

    return TaskOutcome(
        task_id=task.id,
        status="done" if verdict.ok else "failed",
        branch=wt.branch if verdict.ok else "",
        provider=prov_name,
        changed=res.changed,
        failures=[c.criterion for c in verdict.failures],
        elapsed_s=time.time() - t0,
        note="" if verdict.ok else "验收未通过",
        base_sha=wt.base,
        head_sha=head_sha,
        retryable=not verdict.ok and all(c.status in {"passed", "failed"} for c in verdict.checks),
        checks=[{
            "criterion": c.criterion,
            "ok": c.ok,
            "status": c.status,
            "rc": c.returncode,
            "out": " ".join((c.stdout or c.stderr or "").split())[:160],
        } for c in verdict.checks],
        diff_stat=_diff_stat(diff),
    )
