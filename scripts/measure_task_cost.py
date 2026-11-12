#!/usr/bin/env python3
"""
实测"每跑一次任务花多少钱"（元/任务）。

为什么放弃"先测单价再乘 token"：
    缓存价低到测不出来。116k token 的命中价约 0.02 元/百万，要凑出 0.01 元的
    余额变化得跑几千万 token。而任务成本本身在 0.05~0.2 元量级，
    直接把 N 次任务的余额差除以 N 精度就够了——绕开单价这一层。

方法：
    1. 读余额 → 跑 N 次真实任务（与对照实验同一批语料）→ 等结算 → 再读余额
    2. 成本 = 差额 / N
    3. 余额是延迟批量入账的（实测 1~2 分钟），所以必须等，不能调完立刻读

误差来源：
    - 余额粒度 0.01 元。N 次任务累计要 > 1 元，否则误差 >1%
    - 这段时间不能有别的调用占用同一个 key
"""

from __future__ import annotations

import json
import os
import statistics as st
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from orch.providers import build  # noqa: E402
from orch.runner import run_plan  # noqa: E402
from orch.workers import load_workers  # noqa: E402

SETTLE = 150


def balance() -> float:
    key = os.environ["DEEPSEEK_KEY"]
    r = urllib.request.Request("https://api.deepseek.com/user/balance",
                               headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(r, timeout=30) as x:
        return float(json.load(x)["balance_infos"][0]["total_balance"])


def prepare(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("intent.json", "intent.md", "SPEC.md", "tasks.json", "verification.json"):
        f = src / name
        if f.exists():
            (dst / name).write_bytes(f.read_bytes())


def run_batch(worker: str, srcs: list[Path], out_root: Path, batch: str) -> dict:
    ws, _ = load_workers(str(ROOT / "workers.json"))
    prov, why = build(ws[worker])
    if prov is None:
        return {"worker": worker, "error": why}

    b0 = balance()
    times, oks = [], 0
    for i, src in enumerate(srcs, 1):
        d = out_root / f"{batch}-{worker}-{i}"
        prepare(src, d)
        t0 = time.time()
        try:
            s = run_plan(d, prov, None, max_agents=1, timeout=600)
            if s.ok:
                oks += 1
        except Exception as e:
            print("  失败：", str(e)[:80])
        times.append(time.time() - t0)
        print(f"  [{i}/{len(srcs)}] {src.name[:14]:<14}{times[-1]:>6.0f}s  "
              f"累计 {time.time()-t0:.0f}s")
    time.sleep(SETTLE)
    b1 = balance()
    spend = round(b0 - b1, 2)
    return {
        "worker": worker, "n": len(srcs), "ok": oks, "spend": spend,
        "per_task": round(spend / len(srcs), 3),
        "median_s": round(st.median(times), 0),
    }


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    srcs = sorted(p for p in (ROOT / "experiments/medium").iterdir()
                  if (p / "tasks.json").exists())
    srcs = (srcs * 3)[:n]              # 循环取，凑够 N 次
    batch = time.strftime("%m%d-%H%M")
    out_root = ROOT / "experiments/cost"

    print(f"每次任务成本测量：每臂 {n} 次，起始余额 {balance():.2f} 元\n")
    results = []
    for w in ("codex-ds-flash", "codex-ds"):
        print(f"== {w}")
        results.append(run_batch(w, srcs, out_root, batch))
        print()

    print(f"{'worker':<18}{'次数':>5}{'通过':>5}{'总花费':>9}{'元/任务':>10}{'中位耗时':>10}")
    for r in results:
        if "error" in r:
            print(f"{r['worker']:<18}  失败：{r['error']}")
            continue
        print(f"{r['worker']:<18}{r['n']:>5}{r['ok']:>5}{r['spend']:>9.2f}"
              f"{r['per_task']:>10.3f}{r['median_s']:>9.0f}s")
    print(f"\n结束余额 {balance():.2f} 元")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
