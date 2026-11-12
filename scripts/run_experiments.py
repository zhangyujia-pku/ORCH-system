#!/usr/bin/env python3
"""
跑实验：把 intents.json 里每一条跑一遍 intake → plan，记下重写次数。

只跑 plan，不跑 worker。要测的是"需求怎么写 → 被返工几次"，
worker 参不参与不影响这个变量，但会让每条从 1 分钟变成 4 分钟。

用法：
    python scripts/run_experiments.py --limit 3          # 先试 3 条
    python scripts/run_experiments.py                    # 全跑（约 40 分钟）
    python scripts/run_experiments.py --only L1,L5       # 只跑两端
    python scripts/run_experiments.py --dry-run          # 只看会跑哪些

结果写到 experiments/results.csv，可以用 intent_features.py --runs-dir experiments/runs 出表。
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orch.intake import intake  # noqa: E402
from orch.planner import (  # noqa: E402
    CLIProvider,
    ProviderError,
    codex_argv,
    resolve_codex,
)
from orch.planner import plan_run  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="experiments/intents.json")
    ap.add_argument("--repo", default="demo/tasks_app")
    ap.add_argument("--out", default="experiments/runs")
    ap.add_argument("--csv", default="experiments/results.csv")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default="", help="只跑某些档位，如 L1,L2")
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    spec = json.loads((ROOT / args.file).read_text(encoding="utf-8"))
    cases = spec["cases"]
    if args.only:
        want = {x.strip().upper() for x in args.only.split(",")}
        cases = [c for c in cases if c["level"].upper() in want]
    if args.limit:
        cases = cases[: args.limit]

    print(f"{len(cases)} 条 · 仓库 {args.repo} · 每条最多 3 次尝试\n")

    if args.dry_run:
        for c in cases:
            print(f"  {c['id']:<12} {c['level']}  {c['text'][:50]}…")
        return 0

    found = resolve_codex()
    if not Path(found).exists() and found == "codex" and not shutil.which("codex"):
        print("找不到 codex。设 CODEX_BIN 或装 Codex CLI。", file=sys.stderr)
        return 3
    print(f"codex  {found}\n")

    provider = CLIProvider(codex_argv(sandbox="read-only"), name="codex")
    out_dir = ROOT / args.out
    rows = []
    t_all = time.time()

    for i, c in enumerate(cases, 1):
        t0 = time.time()
        it = intake(c["text"], repo=ROOT / args.repo, runs_dir=out_dir,
                    max_agents=3, budget_usd=6)
        try:
            res = plan_run(out_dir / it.run_id, provider, max_attempts=3,
                           timeout=args.timeout)
            status, attempts = res.status, res.n_attempts
            codes = sorted({x for a in res.attempts for x in a.codes})
            n_tasks = len(res.tasks)
        except (ProviderError, ValueError) as e:
            status, attempts, codes, n_tasks = f"error:{e}", 0, [], 0

        dt = time.time() - t0
        # attempts 是总尝试次数；rewrites 才是"被打回重写了几次"
        rewrites = attempts - 1 if status == "ok" else attempts
        rows.append({
            "id": c["id"], "task": c["task"], "level": c["level"],
            "rewrites": rewrites, "attempts": attempts, "status": status,
            "codes": ",".join(codes), "tasks": n_tasks,
            "seconds": round(dt, 1), "run_id": it.run_id,
        })
        print(f"  [{i}/{len(cases)}] {c['id']:<12} {c['level']}  "
              f"重写 {rewrites}  {status:<8} {dt:5.0f}s  {','.join(codes)}")

        # 每条都落盘，中途中断也不丢
        write_csv(ROOT / args.csv, rows)

    total = time.time() - t_all
    ok = sum(1 for r in rows if r["status"] == "ok")
    print(f"\n完成 {len(rows)} 条，一次通过 {ok} 条，总耗时 {total/60:.1f} 分钟")
    print(f"结果：{ROOT / args.csv}")
    print(f"出表：python scripts/intent_features.py --runs-dir {args.out}")
    return 0


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        wr.writeheader()
        wr.writerows(rows)


if __name__ == "__main__":
    raise SystemExit(main())
