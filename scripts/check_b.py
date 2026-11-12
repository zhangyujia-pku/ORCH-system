"""轨道 B 的现场核对：一条命令看四件事。

为什么做成脚本：手册里原本是几段内联 Python，在 PowerShell 下引号会被吃掉，
而且 trace 的字段名（ok 不是 passed）写错时不会报错、只会永远打印"无"。
把它固化下来，既能防手误，也能保证两份文档（bash / PowerShell）口径一致。

用法：
    python scripts/check_b.py --task b3 --arm D
    python scripts/check_b.py --task b3 --arm D --rep 1
    python scripts/check_b.py --results runs/exp/track_b.jsonl
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs" / "exp"


def latest_run(task: str, arm: str, rep: int) -> Path | None:
    pat = str(RUNS / task / arm / f"r{rep}" / ".orch-runs" / "*")
    dirs = sorted(glob.glob(pat))
    return Path(dirs[-1]) if dirs else None


def read_json(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                    # noqa: BLE001
        return None


def mark(ok: bool) -> str:
    return "✓" if ok else "✗"


def check_cell(task: str, arm: str, rep: int) -> int:
    cell = RUNS / task / arm / f"r{rep}"
    print(f"=== 现场核对：{task} 臂 {arm} 第 {rep} 次 ===")
    print(f"目录：{cell}")
    if not cell.exists():
        print("[✗] 该格还没有产物。先跑这一步，或确认 --out 指向的文件里已有记录。")
        return 1

    problems = 0

    # 1) 计划的形态（只有 C/D 有）
    run = latest_run(task, arm, rep)
    if arm in ("C", "D"):
        if run is None:
            print("[✗] 没有 .orch-runs/：编排没起来（看 stderr 或日志）")
            problems += 1
        else:
            plan = read_json(run / "tasks.json") or {}
            tasks = plan.get("tasks") or []
            writes = [tuple(t.get("writes") or []) for t in tasks]
            flat = [w for ws in writes for w in ws]
            overlap = len(flat) != len(set(flat))
            print(f"[{mark(bool(tasks))}] 计划任务数：{len(tasks)}")
            for t in tasks:
                print(f"      {t.get('id')} → {t.get('writes')}")
            print(f"[{mark(not overlap)}] 写入集互不重叠：{'是' if not overlap else '否（有文件被两个任务同时写）'}")
            problems += 0 if tasks else 1
            problems += 1 if overlap else 0

            # 2) 验收与提交（trace）
            trace = run / "trace.jsonl"
            bad, changed, usage, status = [], {}, {}, {}
            if trace.exists():
                for line in trace.read_text(encoding="utf-8").splitlines():
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    ev, d = e.get("event"), e.get("data") or {}
                    if ev == "task.verify" and d.get("ok") is False:
                        bad.append(d.get("task_id"))
                    elif ev == "task.committed":
                        changed[d.get("task_id")] = d.get("changed") or []
                    elif ev == "run.usage":
                        usage = d
                    elif ev in ("task.attempt_end", "task.out_of_scope"):
                        # 失败不一定走到 verify：越界会被直接废掉，没有 verify 事件。
                        # 只查 verify 会得出"全部通过"的错误结论——踩过。
                        tid = d.get("task_id")
                        if ev == "task.out_of_scope":
                            status[tid] = f"越界写入 {d.get('files')}"
                        elif d.get("status") not in (None, "done"):
                            status[tid] = f"{d.get('status')}：{d.get('note') or ''}".strip()
            print(f"[{mark(not bad)}] 验收未过的任务：{bad or '无'}")
            problems += 1 if bad else 0

            # 未出现在 status 里的任务 = 走到了 done
            done = max(0, len(tasks) - len(status)) if tasks else 0
            ok_all = done == len(tasks) and not status
            print(f"[{mark(ok_all)}] 完成 {done}/{len(tasks)}"
                  f"{'' if ok_all else '（未完成的：' + '；'.join(f'{k} {v}' for k, v in status.items()) + '）'}")
            problems += 0 if ok_all else 1

            if changed:
                stray = [k for k, v in changed.items() if len(v) > 1]
                print(f"[{mark(not stray)}] 单任务改多文件：{stray or '无'}")
                problems += 1 if stray else 0
            print(f"[{mark(bool(usage))}] 运行级 token："
                  f"{'输入 ' + format(usage.get('tokens_in', 0), ',') if usage else '未采集（成本算不出来）'}")
            problems += 0 if usage else 1

            # 3) 报告
            dis = run / "DISPATCH.md"
            if dis.exists():
                head = dis.read_text(encoding="utf-8").splitlines()
                keep = [x for x in head[:12] if x.startswith(("【", "执行者", "资源", "计划", "执行："))]
                print("--- DISPATCH.md 摘要 ---")
                for x in keep:
                    print("   ", x)
                has_exec = any(x.startswith("执行者") for x in keep)
                print(f"[{mark(has_exec)}] 执行者一行存在")
                problems += 0 if has_exec else 1
            else:
                print("[✗] 没有 DISPATCH.md")
                problems += 1

    # 4) 产出补丁。落点是 runs/exp/<题>/<臂>/patches/r<次>.patch，
    #    不在 r1/ 里面——写错过一次，会让人以为"没产出补丁"。
    patch = RUNS / task / arm / "patches" / f"r{rep}.patch"
    size = patch.stat().st_size if patch.exists() else 0
    print(f"[{mark(size > 0)}] 补丁：{patch.name if patch.exists() else '（无）'} · {size} 字节")
    problems += 0 if size > 0 else 1

    if problems:
        print(f"\n结论：{problems} 项待查 —— 先看上面打 ✗ 的项，再决定是重跑还是保留。")
        return 1
    print("\n结论：四项全过。这一格可以计入结果。")
    return 0


def check_results(path: Path) -> int:
    if not path.exists():
        print(f"（{path} 不存在）")
        return 1
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    if not rows:
        print("（没有记录）")
        return 1

    print(f"共 {len(rows)} 格\n")
    print(f"{'格':<10}{'单元':>5}{'通过':>5}{'resolved':>10}{'墙钟':>9}{'错误':>8}")
    bad = 0
    for r in rows:
        detail = (r.get("score") or {}).get("detail") or []
        ok = sum(1 for d in detail if d.get("rc") == 0)
        tag = f"{r['task']}/{r['arm']}"
        print(f"{tag:<10}{len(detail):>5}{ok:>5}"
              f"{str(bool(r.get('resolved'))):>10}{r.get('total_wall_s') or 0:>8.0f}s"
              f"{'有' if r.get('error') else '—':>8}")
        # 异常：一个单元都没跑 / 全过却判失败
        if detail and ok == len(detail) and not r.get("resolved"):
            print("      ↳ [异常] 每单元都返回 0，整体却判失败 —— 疑似管线 bug，别当模型失败")
            bad += 1
        if not detail and not r.get("error"):
            print("      ↳ [异常] 没有验收明细，也没有错误信息")
            bad += 1
    print()
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task")
    ap.add_argument("--arm")
    ap.add_argument("--rep", type=int, default=1)
    ap.add_argument("--results", default="")
    args = ap.parse_args(argv)

    if args.results:
        return check_results(Path(args.results))
    if not (args.task and args.arm):
        ap.print_help()
        return 2
    return check_cell(args.task, args.arm.upper(), args.rep)


if __name__ == "__main__":
    raise SystemExit(main())
