"""从已有实验记录里抽 A 臂的形态基线，生成 config/baseline.json。

为什么用中位数：A 的 CV 在 40%–63%，均值会被一两个离群格带偏（r1yes 的 A 有一次只花 $0.09）。
中位数 + 区间更接近「下次大概会花多少」。

形态签名只用三项：k（单元数）、单元行数档、是否共享全局约定——E48 证明只有这三项
会改变 D/A 的关系，其他特征（语言、领域）没有证据。
"""

from __future__ import annotations

import json
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from orch.costing import bucket, signature  # noqa: E402

TASKS = ROOT / "tasks"
SOURCES = [
    "runs/exp/ladder.jsonl", "runs/exp/h1.jsonl", "runs/exp/results.jsonl",
    "runs/exp/router_p.jsonl", "runs/exp/router_p2.jsonl",
    "runs/exp/docs.jsonl", "runs/exp/docs_fix.jsonl", "runs/exp/docs_v2.jsonl",
]
OUT = ROOT / "config" / "baseline.json"


def load(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def features(tid: str) -> tuple[int, float, bool] | None:
    d = TASKS / tid
    meta_f = d / "meta.json"
    if not meta_f.exists():
        return None
    meta = json.loads(meta_f.read_text(encoding="utf-8"))
    k = meta.get("batch_size")
    if not k:
        return None                      # 没有明确单元数的题（t01/t04…）不进基线
    seed = d / "seed"
    if not seed.exists():
        return None
    srcs = [p for p in seed.rglob("*.py") if "checks" not in p.parts]
    lines = sum(len(p.read_text(encoding="utf-8", errors="replace").splitlines()) for p in srcs)
    shared = any(p.name in ("CONVENTIONS.md", "STYLE.md", "SPEC.md")
                 for p in seed.rglob("*") if p.is_file())
    return int(k), lines / max(1, int(k)), shared


def main() -> int:
    rows: list[dict] = []
    for s in SOURCES:
        rows += load(ROOT / s)

    # 同一个 (题, 臂, 次) 只留一条：docs 那批有多次重跑，后写的覆盖前写的
    latest: dict[tuple, dict] = {}
    for r in rows:
        latest[(r.get("task"), r.get("arm"), r.get("rep"))] = r
    rows = list(latest.values())

    groups: dict[str, list[dict]] = {}
    skipped: list[str] = []
    for r in rows:
        if r.get("arm") != "A" or r.get("resolved") is not True:
            continue
        tid = r.get("task")
        f = features(tid)
        if not f:
            skipped.append(str(tid))
            continue
        k, unit, shared = f
        sig = signature(k, unit, shared)
        groups.setdefault(sig, []).append(r)

    sigs = {}
    for sig, rs in sorted(groups.items()):
        ins = [r["usage"]["input_tokens"] for r in rs if r.get("usage")]
        if not ins:
            continue
        cached = [(r["usage"].get("cached_input_tokens") or 0) for r in rs if r.get("usage")]
        out = [(r["usage"].get("output_tokens") or 0) for r in rs if r.get("usage")]
        sigs[sig] = {
            "a_in": int(st.median(ins)),
            "a_cached": int(st.median(cached)),
            "a_out": int(st.median(out)),
            "n": len(ins),
            "lo": min(ins), "hi": max(ins),
            "cv": round(st.stdev(ins) / st.mean(ins) * 100) if len(ins) > 1 else 0,
            "tasks": sorted({str(r["task"]) for r in rs}),
        }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "built_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
        "note": "A 臂的形态基线：中位数 + 区间。形态 = k | 单元行数档 | 是否共享约定。",
        "signatures": sigs,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"[ok] 写入 {OUT}")
    for sig, v in sigs.items():
        print(f"  {sig:<22} n={v['n']}  A输入中位 {v['a_in']:>9,}  区间 {v['lo']:,}–{v['hi']:,}  CV {v['cv']}%  {v['tasks']}")
    if skipped:
        print(f"  [skip] 无单元数的题：{', '.join(sorted(set(skipped)))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
