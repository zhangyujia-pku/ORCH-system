#!/usr/bin/env python3
"""
同外壳、换后端：Codex(OpenAI 默认模型) vs Codex(DeepSeek-v4-pro) 的对照。

为什么要这么设计：
    要回答的是"同一个外壳下，换后端模型，执行质量有没有可测差异"。
    所以四件事必须固定：同一份 plans、同一个验收器、同一个仓库基线、同一个超时。
    只变一个东西——worker 背后调谁。

    验收器固定用 OpenAI 那边，是有意的：如果两边各用各的验收器，
    差异里就混进了"谁在打分"，那测的就不是 worker 了。

用法：
    python scripts/cmp_backends.py --limit 2          # 先跑 2 对
    python scripts/cmp_backends.py --pairs 8          # 8 对 = 16 次执行
    python scripts/cmp_backends.py --dry-run
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from orch.planner import CLIProvider, codex_argv  # noqa: E402
from orch.providers import build  # noqa: E402
from orch.runner import run_plan  # noqa: E402
from orch.workers import load_workers  # noqa: E402


def prepare(src: Path, dst: Path) -> None:
    """把计划原样搬过去。intent.json 里的 repo / repo_head 保持不动，
    这样两边的起点完全一致。

    不删已有目录：一次 rmtree 会触发 safe-delete 的批量确认（>50 文件直接失败），
    而且删了旧结果也没法回头查。调用方保证目录名唯一即可。
    """
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("intent.json", "intent.md", "SPEC.md", "tasks.json", "verification.json"):
        f = src / name
        if f.exists():
            shutil.copy2(f, dst / name)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="experiments/runs_anchor3")
    ap.add_argument("--out", default="experiments/cmp")
    ap.add_argument("--csv", default="experiments/results_cmp.csv")
    ap.add_argument("--pairs", type=int, default=8)
    ap.add_argument("--worker2", default="codex-ds",
                    help="第二臂用哪个 worker（workers.json 里的名字）")
    ap.add_argument("--repeat", type=int, default=1,
                    help="每个计划重复几遍（同一臂重复跑，用来压低噪声）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--timeout", type=int, default=600)  # deepseek 那臂 300s 会撞线
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.environ.get("DEEPSEEK_KEY"):
        print("缺 DEEPSEEK_KEY 环境变量（DeepSeek 那边跑不了）", file=sys.stderr)
        return 3

    srcs = sorted(p for p in (ROOT / args.src).iterdir() if (p / "tasks.json").exists())
    n = args.limit or args.pairs
    srcs = srcs[:n]
    # 重复几遍是统计功效问题：3 对只能说"方向一致"，18 对才能谈显著
    srcs = [(s, r) for s in srcs for r in range(1, args.repeat + 1)]

    ws, _ = load_workers(str(ROOT / "workers.json"))
    ds, why = build(ws[args.worker2])
    if ds is None:
        print(f"造不出 {args.worker2}：{why}", file=sys.stderr)
        return 3

    arms = {
        "openai": CLIProvider(codex_argv(sandbox="workspace-write"), name="codex-openai"),
        args.worker2: ds,
    }
    # 验收器固定，避免"谁打分"混进比较
    verifier = arms["openai"]

    print(f"{len(srcs)} 个计划 × {len(arms)} 个后端 = {len(srcs)*len(arms)} 次执行")
    if args.dry_run:
        for s, rep in srcs:
            print(f"   {s.name}  r{rep}")
        return 0

    out_root = ROOT / args.out
    batch = time.strftime("%m%d-%H%M")   # 目录名带批次，避免覆盖旧结果、也避免删目录
    rows: list[dict] = []
    t_all = time.time()

    for i, (src, rep) in enumerate(srcs, 1):
        for arm_name, prov in arms.items():
            run_dir = out_root / f"{batch}-{src.name}-r{rep}-{arm_name}"
            prepare(src, run_dir)
            t0 = time.time()
            try:
                s = run_plan(run_dir, prov, verifier, max_agents=1, timeout=args.timeout)
                o = s.outcomes[0] if s.outcomes else None
                rows.append({
                    "plan": src.name, "rep": rep, "arm": arm_name,
                    "status": s.state,
                    "verify_ok": "ok" if s.ok else "fail",
                    "failures": (s.reason or (" | ".join(o.failures) if o else ""))[:120],
                    "seconds": round(time.time() - t0, 1),
                })
            except Exception as e:  # 单次失败不该中断整批
                rows.append({"plan": src.name, "rep": rep, "arm": arm_name, "status": f"error",
                             "verify_ok": "fail", "failures": str(e)[:120],
                             "seconds": round(time.time() - t0, 1)})
            r = rows[-1]
            print(f"  [{i}/{len(srcs)}] {src.name[:15]} r{rep} {arm_name:<9} "
                  f"{r['status']:<8} {r['seconds']:5.0f}s  {r['failures'][:50]}")
            write_csv(ROOT / args.csv, rows)

    print(f"\n完成，总耗时 {(time.time()-t_all)/60:.1f} 分钟")
    print(f"结果：{ROOT / args.csv}")
    return 0


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    raise SystemExit(main())
