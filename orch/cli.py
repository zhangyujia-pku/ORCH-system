"""命令行入口。

    python -m orch intake "给 Flask 应用加 JWT 登录" --repo ~/proj
    python -m orch plan            # Planner 编译成 tasks.json（带重写闭环）
    python -m orch status
    python -m orch validate runs/<id>/tasks.json --repo ~/proj
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .contract import Report, repo_snapshot, validate_file
from .intake import FLAGS, find_latest_run, intake
from .planner import CLIProvider, PlanResult, Provider, StubProvider, plan_run
from .trace import Tracer
from .workers import (
    alternatives,
    format_matrix,
    format_routes,
    load_workers,
    next_worker,
    quota_report,
    route,
    suggest_tools,
)


def format_report(rep: Report, title: str = "") -> str:
    lines: list[str] = []
    if title:
        lines.append(title)
    if rep.tasks:
        n = len(rep.tasks)
        head = f"  {n} task{'s' if n > 1 else ''}"
        if rep.layers:
            layers = " / ".join(",".join(l) for l in rep.layers)
            head += f" · {len(rep.layers)} layers · max width {rep.max_width} · {layers}"
        lines.append(head)
    if rep.violations:
        lines.append("")
    for v in rep.violations:
        who = f"[{v.task_id}] " if v.task_id else ""
        mark = "ERROR" if v.severity == "error" else "WARN "
        lines.append(f"  {mark} {v.code} {who}{v.message}")
        if v.hint:
            lines.append(f"        → {v.hint}")
    lines.append("")
    n_e, n_w = len(rep.errors), len(rep.warnings)
    verdict = "PASS" if rep.ok else "FAIL"
    lines.append(f"  {n_e} error(s), {n_w} warning(s)  {verdict}")
    return "\n".join(lines)


def cmd_intake(args) -> int:
    it = intake(
        args.text,
        repo=args.repo,
        runs_dir=args.runs_dir,
        max_agents=args.max_agents,
        budget_usd=args.budget,
    )
    run_dir = Path(args.runs_dir) / it.run_id
    print(f"run  {it.run_id}")
    print(f"     {run_dir / 'intent.md'}")
    print(
        f"     {len(it.raw_text)} 字 · repo {it.repo_head or '(no git)'} · "
        f"max_agents {it.max_agents} · budget ${it.budget_usd:g}"
    )
    if it.flags:
        print(f"\n  含糊度 {it.score}/5")
        for c in it.flags:
            print(f"    {c}  {FLAGS[c]}")
        print(
            "\n  这些标记不是错误，是给实验分组用的。它们会原样带进 Planner，"
            "\n  让你后面能回答「intent 越含糊，重写次数是不是越多」。"
        )
    else:
        print("\n  含糊度 0/5 —— 需求已经够具体，Planner 大概率一次过。")
    print(f"\n  下一步：Planner 读 {run_dir.name}/intent.md，产出 tasks.json（M1，未接）")
    return 0


def cmd_eta(args) -> int:
    from . import eta

    est = eta.estimate(args.runs_dir, args.tasks, args.max_agents)
    try:
        from .dispatch import serve_pid

        host = bool(serve_pid())
    except Exception:                 # noqa: BLE001
        host = False
    mode, why = eta.choose(est, host_running=host)
    if args.json:
        print(json.dumps({**est, "mode": mode, "why": why, "host": host},
                         ensure_ascii=False, indent=2))
        return 0
    print(eta.render(est))
    print(f"建议     {mode}（{why}）")
    print(f"宿主     {'在跑' if host else '未启动'}")
    return 0


def cmd_tools(args) -> int:
    from . import tools as tools_mod

    items = tools_mod.load(args.config)
    if args.json:
        print(json.dumps([tools_mod.asdict(t) for t in items], ensure_ascii=False, indent=2))
        return 0
    print("工具（✓=本机已核验，·=已登记但未核验，不会被自动选中）\n")
    print(tools_mod.render(items))
    print("\n办公产物的判据可以直接写成：")
    print("  python -m orch.docxcheck 报告.docx --min-chars 200 --ban 我觉得 退出码 0")
    print("  python -m orch.xlsxcheck 收支.xlsx --min-rows 4 --cell A1=项目 退出码 0")
    print("  python -m orch.pptxcheck 汇报.pptx --min-slides 4 --require-title 退出码 0")
    return 0


def cmd_cleanup(args) -> int:
    from .cleanup import cleanup_run, cleanup_stale

    minutes = float(args.stale_after) * 60

    if args.stale:
        results = cleanup_stale(args.runs_dir, stale_after=minutes, dry_run=args.dry_run)
        if args.json:
            print(json.dumps([r.__dict__ for r in results], ensure_ascii=False, indent=2))
            return 0
        if not results:
            print("没有需要回收的运行")
            return 0
        for r in results:
            print(f"  {r.run_id}  工作树 {len(r.worktrees)} · 分支 {len(r.branches)}"
                  f"{'  ← 强杀' if r.killed else ''}")
        print(f"\n共 {len(results)} 个")
        return 0

    run_dir = Path(args.runs_dir) / args.run_id if args.run_id else find_latest_run(args.runs_dir)
    if run_dir is None or not run_dir.exists():
        print(f"没有找到 run：{args.runs_dir}", file=sys.stderr)
        return 2

    res = cleanup_run(run_dir, force=args.force, stale_after=minutes, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(res.__dict__, ensure_ascii=False, indent=2))
        return 0 if not res.errors else 3

    print(f"run {res.run_id}  判定 {res.verdict}\n")
    for x in res.skipped:
        print(f"  {x}")
    for x in res.errors:
        print(f"  错误  {x}", file=sys.stderr)
    if res.reclaimed:
        print(f"  已回收：工作树 {len(res.worktrees)} · 分支 {len(res.branches)}")
        print(f"  证据保留在 {run_dir}")
    return 0 if not res.errors else 3


def cmd_apply(args) -> int:
    from .apply import apply_run

    run_dir = Path(args.runs_dir) / args.run_id if args.run_id else find_latest_run(args.runs_dir)
    if run_dir is None or not run_dir.exists():
        print(f"没有找到 run：{args.runs_dir}", file=sys.stderr)
        return 2

    res = apply_run(run_dir, dry_run=args.dry_run, force=args.force)
    if args.json:
        print(json.dumps(res.__dict__, ensure_ascii=False, indent=2))
        return 0 if not res.errors else 3

    if res.errors:
        for e in res.errors:
            print(f"  错误  {e}", file=sys.stderr)
        return 3

    verb = "将合并" if res.dry_run else "已合并"
    print(f"{verb} {res.branch} → 当前分支（run {res.run_id}）\n")
    print(f"  涉及 {len(res.files)} 个文件")
    for f in res.files[:20]:
        print(f"    - {f}")
    if len(res.files) > 20:
        print(f"    … 还有 {len(res.files) - 20} 个")
    if res.dry_run:
        print("\n  预览模式，未改动仓库")
    return 0


def cmd_undo(args) -> int:
    from .undo import undo_run

    run_dir = Path(args.runs_dir) / args.run_id if args.run_id else find_latest_run(args.runs_dir)
    if run_dir is None or not run_dir.exists():
        print(f"没有找到 run：{args.runs_dir}", file=sys.stderr)
        return 2

    res = undo_run(run_dir, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(res.__dict__, ensure_ascii=False, indent=2))
        return 0 if not res.errors else 3

    head = "将撤销" if args.dry_run else "已撤销"
    print(f"{head}  run {res.run_id}  （仓库 {res.repo}）\n")
    for label, items in (("工作树", res.worktrees), ("分支", res.branches), ("目录", res.dirs)):
        print(f"  {label} {len(items)} 个")
        for it in items:
            print(f"    - {it}")
    for it in res.skipped:
        print(f"  跳过  {it}")
    for it in res.errors:
        print(f"  错误  {it}", file=sys.stderr)
    if not args.dry_run:
        print(f"\n  证据保留在 {run_dir}（trace / diff / 验收记录未被删除）")
    return 0 if not res.errors else 3


def cmd_bypass(args) -> int:
    from . import bypass

    if args.summary:
        st = bypass.summary(args.repo)
        print(f"自干记录 {st['total']} 条")
        for k, n in sorted(st["by_kind"].items(), key=lambda x: -x[1]):
            print(f"  {k:<8}{n:>4}")
        print(f"  平均位置数 {st['avg_spots']}")
        return 0

    try:
        entry = bypass.record(args.repo, args.kind, args.spots, args.note, args.file)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"已记账：{entry['kind']} · {entry['spots']} 处 · {entry['ts']}")
    if entry["note"]:
        print(f"  {entry['note']}")
    print(f"  → {bypass.log_path(args.repo)}")
    return 0


def cmd_status(args) -> int:
    run_dir = Path(args.runs_dir) / args.run_id if args.run_id else find_latest_run(args.runs_dir)
    if run_dir is None or not run_dir.exists():
        print(f"没有找到 run：{args.runs_dir}", file=sys.stderr)
        return 2

    print(f"run  {run_dir.name}")

    intent_file = run_dir / "intent.json"
    if intent_file.exists():
        it = json.loads(intent_file.read_text(encoding="utf-8"))
        print(f"     需求：{it['raw_text'][:60]}")
        print(f"     含糊度 {len(it.get('flags', []))}/5  {it.get('flags', [])}")

    events: list[dict] = []
    trace_file = run_dir / "trace.jsonl"
    if trace_file.exists():
        for line in trace_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if not events:
        print("\n  还没有事件 —— 等 Planner 接手。")
        return 0

    from .status import current_events, describe
    shown = events if args.all else current_events(events)
    tag = "全部历史" if args.all else "最近一次调用（旧记录仅最近阶段）"
    extra = ""
    print(f"     状态：{describe(events)}")
    deliveries = [e.get("data", {}) for e in current_events(events)
                  if e.get("event") == "operation.delivery"]
    if deliveries:
        delivery = deliveries[-1]
        label = "已送达" if delivery.get("delivered") else ("未送达" if delivery.get("attempted") else "未发送")
        print(f"     通知：{label}；{delivery.get('detail', '')}")
    print(f"\n  {len(shown)} 条事件 · {tag}{extra}")

    counts: dict[str, int] = {}
    for e in shown:
        k = e.get("event", "?")
        counts[k] = counts.get(k, 0) + 1
    for k, v in counts.items():
        print(f"    {k:<20} {v}")
    last = shown[-1]
    print(f"\n  最后一步：{last.get('event')}  {last.get('ts', '')}")
    return 0


def _build_provider(args) -> Provider:
    if args.provider == "stub":
        sample = Path(__file__).resolve().parent.parent / "examples" / "tasks.good.json"
        doc = json.loads(sample.read_text(encoding="utf-8"))
        payload = {
            "spec_md": "# 示例 SPEC\n\n由 stub provider 返回，用于离线跑通链路。\n",
            "tasks": doc["tasks"],
        }
        return StubProvider([json.dumps(payload, ensure_ascii=False)])
    if args.provider == "codex":
        from .planner import codex_argv

        return CLIProvider(codex_argv(), name="codex")
    if args.provider == "deepseek":
        # 走 workers.json 的便宜档：planner 的调用不占 Codex 额度
        from .providers import build_named

        prov, why = build_named("deepseek", getattr(args, "workers", None))
        if prov is None:
            raise SystemExit(f"--provider deepseek 不可用：{why}")
        return prov
    if args.provider == "cli":
        if not args.cmd:
            raise SystemExit("--provider cli 需要 --cmd（JSON 数组模板）")
        try:
            argv = json.loads(args.cmd)
        except json.JSONDecodeError as e:
            raise SystemExit(f"--cmd 必须是 JSON 数组：{e}")
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            raise SystemExit("--cmd 必须是字符串数组，例如 '[\"codex\",\"exec\",\"{prompt}\"]'")
        return CLIProvider(argv)
    raise SystemExit(f"未知 provider：{args.provider}")


def _report_plan(res: PlanResult, run_dir: Path) -> int:
    """规划结果的统一输出。外部计划与模型规划走同一套报告口径。"""
    for a in res.attempts:
        if a.ok:
            print(f"  PASS  attempt {a.n}")
        else:
            if a.parse_error:
                print(f"  FAIL  attempt {a.n}  {a.parse_error}")
            else:
                print(f"  FAIL  attempt {a.n}  {', '.join(a.codes)}")

    if res.status == "ok" and res.warnings:
        print("\n  提示（不阻断）：")
        for w in res.warnings:
            print(f"    {w}")

    if res.status == "ok":
        layers = " / ".join(",".join(l) for l in res.layers)
        print(f"\n  {len(res.tasks)} tasks · {len(res.layers)} layers · {layers}")
        print(f"  → {run_dir / 'tasks.json'}")
        print(f"  → {run_dir / 'SPEC.md'}")
        return 0

    print(f"\n  规划未通过；实际尝试 {len(res.attempts)} 次，尚未执行，未启动单 Agent 降级")
    print(f"  见 {run_dir / 'plan.failed.json'}")
    return 3


def cmd_plan(args) -> int:
    run_dir = Path(args.runs_dir) / args.run_id if args.run_id else find_latest_run(args.runs_dir)
    if run_dir is None or not run_dir.exists():
        print(f"没有找到 run：{args.runs_dir}", file=sys.stderr)
        return 2

    # 外部计划：Codex 自己当 planner 时走这条，一个模型都不调
    if getattr(args, "from_plan", None):
        from .planner import plan_from_file

        print(f"run  {run_dir.name}")
        print(f"     provider external（{Path(args.from_plan).name}）\n")
        return _report_plan(plan_from_file(run_dir, args.from_plan), run_dir)

    provider = _build_provider(args)
    print(f"run  {run_dir.name}")
    print(f"     provider {provider.name} · 最多 {args.attempts} 次尝试\n")

    res = plan_run(run_dir, provider, max_attempts=args.attempts, timeout=args.timeout)
    return _report_plan(res, run_dir)


def cmd_run(args) -> int:
    import shutil

    from .planner import codex_argv
    from .runner import run_plan

    if shutil.which("git") is None:
        print("找不到 git。run 要用 worktree 隔离，必须先有 git。\n"
              "  set PATH=%PATH%;%USERPROFILE%\\.workbuddy\\binaries\\PortableGit\\versions\\1.3.0\\mingw64\\bin",
              file=sys.stderr)
        return 3

    run_dir = Path(args.runs_dir) / args.run_id if args.run_id else find_latest_run(args.runs_dir)
    if run_dir is None or not run_dir.exists():
        print(f"没有找到 run：{args.runs_dir}", file=sys.stderr)
        return 2

    if args.provider == "codex":
        worker = CLIProvider(codex_argv(sandbox=args.worker_sandbox), name="codex")
        if args.worker_sandbox == "bypass":
            print("  ⚠ worker 绕过了 codex 沙箱（Windows 上 workspace-write 会卡死）；"
                  "隔离只靠 worktree + 事后的越界检查。\n")
    elif args.provider == "stub":
        worker = _build_provider(argparse.Namespace(provider="stub", cmd=None))
    else:
        worker = _build_provider(argparse.Namespace(provider="cli", cmd=args.cmd))

    verifier = None
    if args.verifier == "codex":
        verifier = CLIProvider(codex_argv(sandbox="read-only"), name="codex-verifier")
    elif args.verifier == "stub":
        verifier = StubProvider(
            ['{"results": [{"i": 1, "verdict": "PASS", "evidence": "stub"}]}'], name="stub-verifier"
        )
    elif args.verifier == "cli":
        verifier = _build_provider(argparse.Namespace(provider="cli", cmd=args.cmd))

    print(f"run  {run_dir.name}")
    print(f"     worker {getattr(worker, 'name', '?')} · verifier "
          f"{getattr(verifier, 'name', None) or 'none'} · 并发 {args.max_agents}\n")

    # 实际分派由 runner 唯一决定并记录；route 子命令用于独立试算。

    summary = run_plan(
        run_dir, worker, verifier,
        max_agents=args.max_agents, timeout=args.timeout,
        workers_path=args.workers,
        verification_path=args.verification,
        execution_path=args.execution,
        resume=args.resume,
    )
    if summary.reason:
        print(f"     {summary.state}: {summary.reason}")
    if not summary.execution_started:
        print("     尚未启动 worker。")
    if summary.ok:
        print("     已通过任务验收、独立整合及固定整体验收。")
    if summary.merged_worktree:
        print(f"     整合产物：{summary.merged_worktree}")

    for o in summary.outcomes:
        mark = {"done": "DONE  ", "failed": "FAIL  ", "blocked": "BLOCK "}[o.status]
        detail = f"{len(o.changed)} 文件" if o.changed else ""
        if o.out_of_scope:
            detail += f" · 越界 {o.out_of_scope}"
        if o.failures:
            detail += f" · 未过 {len(o.failures)} 条"
        if o.note and not o.out_of_scope:
            detail += f" · {o.note}"
        print(f"  {mark} {o.task_id}  {o.elapsed_s:5.1f}s  {detail}")

    if summary.merged_branch:
        print(f"\n  合并到 {summary.merged_branch}")
        for m in summary.merge_results:
            print(f"    {'ok  ' if m.ok else 'FAIL'} {m.branch} {m.detail}")

    print(f"\n  {len(summary.done)}/{len(summary.outcomes)} 任务完成 · "
          f"{'OK' if summary.ok else 'FAIL'}")
    return 0 if summary.ok else 4


def cmd_show(args) -> int:
    """打印最近一次 run 的计划，省得手拼路径（placeholder 最容易拼错）。"""
    run_dir = Path(args.runs_dir) / args.run_id if args.run_id else find_latest_run(args.runs_dir)
    if run_dir is None or not run_dir.exists():
        print(f"没有找到 run：{args.runs_dir}", file=sys.stderr)
        return 2

    tasks_file = run_dir / "tasks.json"
    spec_file = run_dir / "SPEC.md"
    if not tasks_file.exists():
        print(f"{run_dir.name} 还没有 tasks.json —— 先跑 orch plan", file=sys.stderr)
        return 2

    print(f"run  {run_dir.name}\n")
    print(tasks_file.read_text(encoding="utf-8"))
    if args.spec and spec_file.exists():
        print("\n----- SPEC.md -----\n")
        print(spec_file.read_text(encoding="utf-8"))
    return 0


def cmd_validate(args) -> int:
    files = repo_snapshot(args.repo) if args.repo else []
    rep = validate_file(args.path, files)
    if args.json:
        print(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_report(rep, title=str(args.path)))
    return 0 if rep.ok else 1


def cmd_route(args) -> int:
    """试算：这版计划会分给谁、省多少额度。只决策，不执行。"""
    if args.path:
        tasks_file = Path(args.path)
    else:
        run_dir = Path(args.runs_dir) / args.run_id if args.run_id else find_latest_run(args.runs_dir)
        if run_dir is None or not run_dir.exists():
            print(f"没有找到 run：{args.runs_dir}", file=sys.stderr)
            return 2
        tasks_file = run_dir / "tasks.json"

    if not tasks_file.exists():
        print(f"{tasks_file} 不存在 —— 先跑 orch plan", file=sys.stderr)
        return 2

    rep = validate_file(tasks_file, repo_snapshot(args.repo) if args.repo else [])
    if not rep.ok:
        print(format_report(rep, title=str(tasks_file)), file=sys.stderr)
        print("计划本身没通过校验，不路由。", file=sys.stderr)
        return 1

    workers, ladder = load_workers(args.workers)
    assignments = route(rep.tasks, workers, ladder,
                        require_verified=not args.allow_unverified)
    by_id = {t.id: t for t in rep.tasks}

    if args.json:
        print(json.dumps(
            {
                "ladder": list(ladder),
                "capabilities": {n: w.cap_list for n, w in workers.items()},
                "assignments": [
                    {"task": a.task_id, "worker": a.worker,
                     "reason": a.reason, "detail": a.detail,
                     "needs": list(a.needs), "gap": list(a.gap),
                     "alternatives": list(a.alternatives),
                     "tools": (list(t) if (t := suggest_tools(by_id[a.task_id], workers[a.worker]))
                               else None)}
                    for a in assignments if a.task_id in by_id
                ],
                "quota": quota_report(assignments, workers, ladder),
            },
            ensure_ascii=False, indent=2))
        return 0 if all(a.routable for a in assignments) else 1

    print(format_routes(rep.tasks, assignments, workers, ladder,
                        title=f"route  {tasks_file}"))

    if args.show_ladder:
        print()
        print(format_matrix(workers, ladder))

    if args.show_tools:
        print()
        print("  工具裁剪（只给必需的那几把）：")
        for a in assignments:
            if not a.routable:
                continue
            t = by_id.get(a.task_id)
            w = workers.get(a.worker)
            if t is None or w is None:
                continue
            tools = suggest_tools(t, w)
            if tools is None:
                print(f"    {a.task_id:<8}{a.worker:<8}不支持裁剪（--tools 不可用）")
            else:
                print(f"    {a.task_id:<8}{a.worker:<8}{' '.join(tools)}")

    gaps = [a for a in assignments if a.gap]
    unverified = [a for a in assignments if a.reason == "C10"]
    if gaps or unverified:
        print()
        for a in gaps:
            print(f"  ⚠ {a.task_id} 需要 {', '.join(a.gap)}，注册表里没有任何档具备。"
                  f"给某个档接线（workers.json 的 available），或改计划")
        for a in unverified:
            print(f"  ⚠ {a.task_id} 有档能做（{a.worker}），但 argv 没在本机核验过——"
                  f"按该 harness 的 --help 核完再置 argv_verified，或加 --allow-unverified 先看估算")
        return 1
    return 0


def cmd_cancel(args) -> int:
    from .control import atomic_json
    directory = Path(args.runs_dir) / args.run_id
    if not directory.is_dir():
        print("运行目录不存在")
        return 2
    runtime = directory / "runtime.json"
    data = json.loads(runtime.read_text(encoding="utf-8")) if runtime.exists() else {}
    atomic_json(directory / "cancel.request.json", {"execution_id": data.get("execution_id", "")})
    print("取消请求已记录；执行者将在检查点或进程轮询时停止。")
    return 0


def cmd_answer(args) -> int:
    from .control import RunLock
    from .recovery import record_answer
    directory = Path(args.runs_dir) / args.run_id
    if not directory.is_dir():
        print("运行目录不存在")
        return 2
    try:
        with RunLock(directory):
            record_answer(directory, args.task_id, args.text)
    except (OSError, ValueError) as e:
        print(str(e))
        return 2
    print("回答已追加保存；使用 run --resume 继续，原始需求未改写。")
    return 0


def cmd_retry(args) -> int:
    """把一次运行里"没做完的部分"重开一次小运行。

    为什么不做成"原地重跑"：orch 只在整层通过后才合并，失败层的改动不在用户仓库里，
    所以重试必须基于"旧基线 + 旧计划里未完成的任务"，而不是基于当前工作区打补丁。
    这里直接起一个新的 run（新的 run_dir、新的 trace），旧运行的证据原样保留。
    """
    import json as _json
    import subprocess as _sp
    directory = Path(args.run_dir).expanduser()
    if not directory.is_dir():
        print(f"运行目录不存在：{directory}")
        return 2
    tasks_file = directory / "tasks.json"
    cp_file = directory / "checkpoint.json"
    if not tasks_file.exists():
        print("这个运行目录里没有 tasks.json")
        return 2
    doc = _json.loads(tasks_file.read_text(encoding="utf-8"))
    tasks = doc.get("tasks") or []
    done: set[str] = set()
    if cp_file.exists():
        cp = _json.loads(cp_file.read_text(encoding="utf-8"))
        done = {o["task_id"] for o in cp.get("outcomes", []) if o.get("status") == "done"}
    wanted = [t.strip() for t in (args.tasks or "").split(",") if t.strip()]
    picked = [t for t in tasks if (t["id"] in wanted if wanted else t["id"] not in done)]
    if wanted:
        missing = [w for w in wanted if w not in {t["id"] for t in tasks}]
        if missing:
            print(f"计划里没有这些任务：{', '.join(missing)}")
            return 2
    if not picked:
        print("没有需要重跑的任务（全部已完成）。")
        return 0

    # 无论有没有 --plan，都只跑选中的子集：
    # 原实现把整份计划原样传下去，--tasks 只影响打印，等于没生效。
    plan = _json.loads(tasks_file.read_text(encoding="utf-8"))
    picked_ids = {t["id"] for t in picked}
    if args.plan:
        plan = _json.loads(Path(args.plan).read_text(encoding="utf-8"))
        have = {t.get("id") for t in (plan.get("tasks") or [])}
        unknown = sorted(picked_ids - have)
        if unknown:
            print(f"--plan 里缺少待重跑的任务：{', '.join(unknown)}")
            print("不回退执行整份计划——那会把已完成的任务再跑一遍。")
            return 2
    plan["tasks"] = [t for t in (plan.get("tasks") or []) if t.get("id") in picked_ids]
    if not plan["tasks"]:
        print("没有任何可重跑的任务：请检查 --tasks 与计划 id 是否一致")
        return 2
    if plan.get("layers"):
        plan["layers"] = [[i for i in layer if i in picked_ids] for layer in plan["layers"]]
        plan["layers"] = [layer for layer in plan["layers"] if layer]

    print("将重跑：")
    for t in plan["tasks"]:
        print(f"  {t['id']}  {t.get('goal', '')[:60]}")
        for a in t.get("acceptance") or []:
            print(f"       判据：{a}")
    if args.dry_run:
        return 0

    # 判据被改过就留痕：写一份 retry 说明，旧的判据原文一并存档
    stamp = __import__("time").strftime("%Y%m%d-%H%M%S")
    note = directory / f"retry-{stamp}.json"
    old = {t["id"]: t.get("acceptance") for t in tasks}
    changed = {t["id"]: {"before": old.get(t["id"]), "after": t.get("acceptance")}
               for t in plan["tasks"] if old.get(t["id"]) != t.get("acceptance")}
    note.write_text(_json.dumps(
        {"retried": [t["id"] for t in plan["tasks"]], "criteria_changed": changed,
         "source_run": str(directory)}, ensure_ascii=False, indent=1), encoding="utf-8")

    intent = directory / "intent.md"
    text = intent.read_text(encoding="utf-8").strip() if intent.exists() else plan.get("goal", "")
    plan_path = directory / f"retry-{stamp}.plan.json"
    plan_path.write_text(_json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")

    # 默认仓库取原运行记录里的 repo，而不是当前 cwd——
    # 从别的目录执行 retry 时，cwd 往往和这次运行毫无关系。
    repo_arg = args.repo
    if not repo_arg:
        try:
            repo_arg = _json.loads(
                (directory / "dispatch.json").read_text(encoding="utf-8")).get("repo") or ""
        except Exception:
            repo_arg = ""
    repo = Path(repo_arg).expanduser() if repo_arg else Path.cwd()
    cmd = [__import__("sys").executable, "-m", "orch.dispatch", text,
           "--repo", str(repo), "--plan", str(plan_path),
           "--mode", args.mode, "--provider", args.provider]
    if args.no_notify:
        cmd.append("--no-notify")
    print(f"\n开始重跑（repo={repo}）…")
    rc = _sp.call(cmd)
    if changed:
        print(f"注意：本次改了 {len(changed)} 个任务的判据，差异已存到 {note.name}")
    if rc != 0:
        print("重跑仍未通过；带上这次的证据再决定是改判据还是改需求。")
        return rc
    print("重跑通过。别忘了 orch apply 才能落到工作区。")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # dispatch 自带完整参数表（--thread / --nonce / --plan-only / --foreground…），
    # 挪过来注册只会和它重复一遍，所以直接在入口分派。
    if argv and argv[0] == "dispatch":
        from .dispatch import main as dispatch_main

        return dispatch_main(argv[1:])

    ap = argparse.ArgumentParser(
        prog="orch",
        description="多 agent 编排：入口层（intake）+ 契约校验（validate）+ 状态（status）。"
                    "出入口闭环用 `orch dispatch`（见 orch dispatch --help）",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("intake", help="把一句自然语言落成一次 run（原话存档）")
    i.add_argument("text", help="自然语言需求，原样传入，不要自己改写")
    i.add_argument("--repo", default=".", help="目标仓库")
    i.add_argument("--runs-dir", default="runs", help="run 目录的父目录")
    i.add_argument("--max-agents", type=int, default=4, help="并发上限（实验变量）")
    i.add_argument("--budget", type=float, default=6.0, help="美元上限")
    i.set_defaults(func=cmd_intake)

    s = sub.add_parser("status", help="看某次 run 到哪了")
    s.add_argument("run_id", nargs="?", default=None)
    s.add_argument("--runs-dir", default="runs")
    s.add_argument("--all", action="store_true", help="连历史运行一起统计")
    s.set_defaults(func=cmd_status)

    et = sub.add_parser("eta", help="估一下这批要跑多久，该阻塞还是投递")
    et.add_argument("--tasks", type=int, required=True, help="任务数")
    et.add_argument("--max-agents", type=int, default=3)
    et.add_argument("--runs-dir", default="runs")
    et.add_argument("--json", action="store_true")
    et.set_defaults(func=cmd_eta)

    tl = sub.add_parser("tools", help="看有哪些确定性工具（文档断言/差异比较）")
    tl.add_argument("--config", default=None, help="tools.json 路径，默认用内置表")
    tl.add_argument("--json", action="store_true")
    tl.set_defaults(func=cmd_tools)

    cl = sub.add_parser("cleanup", help="回收卡死的运行：清工作树与分支，保留证据")
    cl.add_argument("run_id", nargs="?", default=None)
    cl.add_argument("--runs-dir", default="runs")
    cl.add_argument("--stale", action="store_true", help="批量回收所有已死的运行")
    cl.add_argument("--force", action="store_true",
                    help="锁仍被持有但心跳停了时，验明 pid 后强杀")
    cl.add_argument("--stale-after", default="30", help="心跳多久没更新算挂起（分钟，默认 30）")
    cl.add_argument("--dry-run", action="store_true")
    cl.add_argument("--json", action="store_true")
    cl.set_defaults(func=cmd_cleanup)

    ap_ = sub.add_parser("apply", help="把一次成功运行的产物合并到当前分支")
    ap_.add_argument("run_id", nargs="?", default=None)
    ap_.add_argument("--runs-dir", default="runs")
    ap_.add_argument("--dry-run", action="store_true", help="只列会变哪些文件")
    ap_.add_argument("--force", action="store_true", help="工作区有未提交改动也继续")
    ap_.add_argument("--json", action="store_true")
    ap_.set_defaults(func=cmd_apply)

    u = sub.add_parser("undo", help="撤销一次运行：删分支与工作树，保留证据")
    u.add_argument("run_id", nargs="?", default=None)
    u.add_argument("--runs-dir", default="runs")
    u.add_argument("--dry-run", action="store_true", help="只列不删")
    u.add_argument("--json", action="store_true")
    u.set_defaults(func=cmd_undo)

    bp = sub.add_parser("bypass", help="记一笔自干（跳过编排、自己做时）")
    bp.add_argument("--kind", required=False, default=None,
                    help="互动类型；--summary 时不需要")
    bp.add_argument("--spots", type=int, default=1, help="改动位置数（不是行数）")
    bp.add_argument("--note", default="", help="一句话说明为什么自己做了")
    bp.add_argument("--file", action="append", default=[], help="涉及的文件，可重复")
    bp.add_argument("--repo", default=".")
    bp.add_argument("--summary", action="store_true", help="看自干统计")
    bp.set_defaults(func=cmd_bypass)

    sh = sub.add_parser("show", help="打印最近（或指定）run 的计划")
    sh.add_argument("run_id", nargs="?", default=None)
    sh.add_argument("--runs-dir", default="runs")
    sh.add_argument("--spec", action="store_true", help="同时打印 SPEC.md")
    sh.set_defaults(func=cmd_show)

    p = sub.add_parser("plan", help="让 Planner 把 intent 编译成 tasks.json（带重写闭环）")
    p.add_argument("run_id", nargs="?", default=None)
    p.add_argument("--runs-dir", default="runs")
    p.add_argument("--provider", default="stub", choices=["stub", "codex", "cli", "deepseek"])
    p.add_argument("--cmd", default=None,
                   help='JSON 数组模板，如 \'["codex","exec","--sandbox","read-only","{prompt}"]\'')
    p.add_argument("--from", dest="from_plan", default=None,
                   help="用外部计划文件替代 Planner：只校验、不调用模型")
    p.add_argument("--attempts", type=int, default=3)
    p.add_argument("--timeout", type=int, default=300)
    p.set_defaults(func=cmd_plan)

    r = sub.add_parser("run", help="按层发车：worktree × worker × 越界检查 × 验收 × 合并")
    r.add_argument("run_id", nargs="?", default=None)
    r.add_argument("--runs-dir", default="runs")
    r.add_argument("--provider", default="codex", choices=["stub", "codex", "cli", "deepseek"])
    r.add_argument("--verifier", default="codex", choices=["none", "stub", "codex", "cli"])
    r.add_argument("--cmd", default=None, help="--provider cli 时的 argv 模板")
    r.add_argument("--max-agents", type=int, default=3)
    r.add_argument("--timeout", type=int, default=900)
    r.add_argument("--worker-sandbox", default="workspace-write",
                   choices=["workspace-write", "bypass", "read-only"],
                   help="worker 的沙箱模式。默认 workspace-write（已切 MXC 后端）；"
                        "bypass 仅用于沙箱不可用时排障")
    r.add_argument("--workers", default=None,
                   help="按 workers.json 做能力路由（异构分派）；给了就忽略 --provider")
    r.add_argument("--verification", default=None,
                   help="用户维护的固定整体验收 JSON；默认运行目录 verification.json，缺失时阻断")
    r.add_argument("--execution", default=None, help="资源及恢复配置；默认运行目录 execution.json")
    r.add_argument("--resume", action="store_true", help="核对恢复点后继续，不重复接纳已完成成果")
    r.set_defaults(func=cmd_run)

    c = sub.add_parser("cancel", help="请求取消指定运行")
    c.add_argument("run_id")
    c.add_argument("--runs-dir", default="runs")
    c.set_defaults(func=cmd_cancel)
    a = sub.add_parser("answer", help="为待澄清任务追加用户回答")
    a.add_argument("run_id")
    a.add_argument("task_id")
    a.add_argument("text")
    a.add_argument("--runs-dir", default="runs")
    a.set_defaults(func=cmd_answer)

    rt = sub.add_parser("retry", help="重跑某次运行里没做完的任务（新起一次小运行，旧证据保留）")
    rt.add_argument("run_dir", help="旧运行的目录，例如 <repo>/.orch-runs/20261111-230547-xxxx")
    rt.add_argument("--tasks", default="", help="逗号分隔；空=所有未完成任务")
    rt.add_argument("--plan", default="", help="新的计划文件（可改判据）；不给则沿用旧计划")
    rt.add_argument("--repo", default="", help="目标仓库；默认当前目录")
    rt.add_argument("--mode", default="foreground", choices=["foreground", "deliver"])
    rt.add_argument("--provider", default="deepseek")
    rt.add_argument("--no-notify", action="store_true")
    rt.add_argument("--dry-run", action="store_true", help="只打印将重跑什么")
    rt.set_defaults(func=cmd_retry)

    ca = sub.add_parser("control-arm", help="对照组 A 的开关（试验模式默认开）")
    ca.add_argument("action", nargs="?", default="status", choices=["on", "off", "status"])
    ca.set_defaults(func=lambda a: __import__("orch.control_arm", fromlist=["main"]).main([a.action]))

    v = sub.add_parser("validate", help="校验一个 tasks.json")
    v.add_argument("path")
    v.add_argument("--repo", default=None, help="仓库根目录（用于展开 glob 写集）")
    v.add_argument("--json", action="store_true")
    v.set_defaults(func=cmd_validate)

    rt = sub.add_parser("route", help="试算：这版计划分给哪些 worker、省多少 codex 额度")
    rt.add_argument("path", nargs="?", default=None, help="tasks.json；省略则取最近一次 run")
    rt.add_argument("--run-id", default=None)
    rt.add_argument("--runs-dir", default="runs")
    rt.add_argument("--repo", default=None)
    rt.add_argument("--workers", default=None, help="worker 注册表路径，默认仓库根 workers.json")
    rt.add_argument("--json", action="store_true")
    rt.add_argument("--show-ladder", action="store_true", help="同时打印能力矩阵与 plugin 配置状态")
    rt.add_argument("--show-tools", action="store_true", help="同时打印每个任务应裁到哪些工具")
    rt.add_argument("--allow-unverified", action="store_true",
                    help="把 argv 未核验的 harness 也算进候选（默认不算：没跑过的档不能进估算）")
    rt.set_defaults(func=cmd_route)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
