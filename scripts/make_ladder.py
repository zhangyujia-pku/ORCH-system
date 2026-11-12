"""规模阶梯题库：同一族任务，只改单元数 N。

目的（2026-11-11 重订）：
    变量只留一个——**编排 / 不编排**。要回答的是"多大规模上编排才划算"，
    所以题必须是同一种工作、同一个单元内容，只有 N 变化。

设计：
    sc01 / sc02 / sc04 / sc08 / sc16 —— 单元数 1 / 2 / 4 / 8 / 16
    单元内容与 b3 完全一致（给 fetch_all 加超时重试），逐字相同；
    差别只有"要改几个文件"。

为什么复用 b3 的模板而不是另写一套：
    换了模板就换了任务形状，N 的效应会和"题型变了"混在一起。

用法：
    python scripts/make_ladder.py --force --verify
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from scripts.make_track_b import (  # noqa: E402
    B3_CHECK, B3_SEED, B3_SOL, B3_UNITS, add_ignore, w,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
TASKS = ROOT / "tasks"
SIZES = (1, 2, 4, 8, 16)


def units_for(size: int) -> list[tuple[int, str]]:
    """给出 size 个单元。B3_UNITS 只有 8 个名字，超过就循环补后缀。

    单元内容与 b3 逐字相同——多出来的只是"再多改一个同样的文件"，
    正是阶梯实验需要的那种"只改数量、不改性质"。
    """
    base = [name for _, name in B3_UNITS]
    out = []
    for i in range(1, size + 1):
        name = base[(i - 1) % len(base)]
        if i > len(base):
            name = f"{name}_v{1 + (i - 1) // len(base)}"
        out.append((i, name))
    return out


def build_one(size: int) -> str:
    tid = f"sc{size:02d}"
    dest = TASKS / tid
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    seed, checks, ref = dest / "seed", dest / "checks", dest / "ref"
    add_ignore(seed)

    units = units_for(size)
    for nn, name in units:
        sub = dict(NN=f"{nn:02d}", NAME=name)
        w(seed / "src" / f"loader_{nn:02d}.py", B3_SEED.substitute(sub))
        w(ref / "src" / f"loader_{nn:02d}.py", B3_SOL.substitute(sub))
        body = B3_CHECK.substitute(sub)
        w(checks / f"unit_{nn:02d}.py", body)
        w(seed / "checks" / f"unit_{nn:02d}.py", body)

    accept = [f"python checks/unit_{i:02d}.py" for i in range(1, size + 1)]
    files = "、".join(f"loader_{i:02d}.py" for i in range(1, size + 1))
    goal = (
        f"在 src 下 {size} 个 loader（{files}）里给 fetch_all 加超时重试："
        "单个 id 遇到 TimeoutError 最多尝试 3 次，3 次都失败就把 TimeoutError 抛出；"
        "其它异常（如 ValueError）不重试，直接抛；成功时返回值与顺序保持不变。"
        "每个 loader 一个文件，互不依赖，可以并行改。"
        f"验收：依次跑 {' '.join(a.replace('python ', '') for a in accept)}，全部退出码 0。"
    )
    meta = {
        "id": tid, "tier": "B", "goal": goal, "timeout_s": 2400,
        "accept": accept, "batch_size": size, "kind": "超时重试（同构·规模阶梯）",
    }
    (dest / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
    return tid


def verify(tid: str) -> tuple[int, int, int]:
    """双向自检：seed 必败、ref 必过。返回 (seed 通过数, ref 通过数, 总数)。"""
    dest = TASKS / tid
    meta = json.loads((dest / "meta.json").read_text(encoding="utf-8"))
    n = meta["batch_size"]

    def count(dropin: pathlib.Path | None) -> int:
        # 用临时副本，避免污染 tasks/ 下的种子
        work = ROOT / "runs" / "_verify" / tid
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)
        shutil.copytree(dest / "seed", work)
        if dropin:
            for f in dropin.rglob("*"):
                if f.is_file():
                    rel = f.relative_to(dropin)
                    (work / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, work / rel)
        ok = 0
        for cmd in meta["accept"]:
            r = subprocess.run(cmd, cwd=str(work), shell=True,
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=120)
            if r.returncode == 0:
                ok += 1
        return ok

    return count(None), count(dest / "ref"), n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()

    bad = 0
    for size in SIZES:
        tid = build_one(size)
        print(f"[ok] {tid}：{size} 个单元")
        if args.verify:
            seed_ok, ref_ok, n = verify(tid)
            good = seed_ok == 0 and ref_ok == n
            bad += 0 if good else 1
            print(f"[verify] {tid}：seed 通过 {seed_ok}/{n}，ref 通过 {ref_ok}/{n}"
                  f" → {'合格' if good else '不合格'}")

    shutil.rmtree(ROOT / "runs" / "_verify", ignore_errors=True)
    print(f"\n结论：{'全部合格' if not bad else f'{bad} 题不合格'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
