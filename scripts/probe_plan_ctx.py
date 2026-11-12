"""探针：规划阶段"读不读仓库"对 H 侧额度与计划质量的影响。

三个变体，同一句题面、同一模型（Codex 默认档）、同一仓库副本：
  P0  只给文件清单（= 现行 skill 的硬约束）
  P1  给文件清单 + 全仓文件正文
  P2  给文件清单 + 允许自由探索（ls / cat / grep 自己来）

每个变体只跑一次规划，不执行、不打分；记录 usage、墙钟、计划条数与 writes 覆盖。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.exp_lib import Task, run_codex, codex_argv  # noqa: E402
from scripts.exp_runner import PLAN_PROMPT, extract_json  # noqa: E402

TASKS_DIR = ROOT / "tasks"


def list_files(work: Path) -> str:
    return "\n".join(
        "- " + str(p.relative_to(work)).replace("\\", "/")
        for p in sorted(work.rglob("*"))
        if p.is_file() and ".git" not in p.parts
    )[:3000]


def dump_repo(work: Path, limit_chars: int = 240_000) -> str:
    """把仓库正文拼进去，模拟"先通读一遍"。"""
    out, total = [], 0
    for p in sorted(work.rglob("*")):
        if not p.is_file() or ".git" in p.parts:
            continue
        try:
            txt = p.read_text(encoding="utf-8")
        except Exception:                                  # noqa: BLE001
            continue
        chunk = f"\n=== {str(p.relative_to(work)).replace(chr(92), '/')} ===\n{txt}"
        if total + len(chunk) > limit_chars:
            break
        out.append(chunk)
        total += len(chunk)
    return "".join(out)


def ref_files(task: Task) -> set[str]:
    """参考解改了哪些文件——用来衡量计划覆盖得全不全。"""
    ref = TASKS_DIR / task.id / "ref"
    if not ref.is_dir():
        return set()
    return {str(p.relative_to(ref)).replace("\\", "/")
            for p in ref.rglob("*") if p.is_file() and ".git" not in p.parts}


def run_variant(task: Task, variant: str, work: Path) -> dict:
    files = list_files(work)
    if variant == "P0":
        prompt = PLAN_PROMPT.format(goal=task.goal, files=files)
    elif variant == "P1":
        prompt = PLAN_PROMPT.format(goal=task.goal, files=files) + \
            "\n\n仓库现有内容如下（供你判断现状，不要输出它们）：\n" + dump_repo(work)
    else:  # P2
        prompt = PLAN_PROMPT.format(goal=task.goal, files=files) + (
            "\n\n需要的话你可以自己查看仓库（ls / cat / grep 都行），"
            "看清楚现状再拆；最后仍然只输出那个 JSON 对象。")

    r = run_codex(codex_argv("A", work, "bypass"), work, prompt, 600)
    doc = extract_json(r["message"] or "") or {}
    tasks = doc.get("tasks") or []
    writes = set()
    for t in tasks:
        for w in (t.get("writes") or []):
            writes.add(str(w).replace("\\", "/").lstrip("./"))
    u = r["usage"] or {}
    ref = ref_files(task)
    covered = len(writes & ref)
    return {
        "task": task.id, "variant": variant,
        "input": u.get("input_tokens"), "cached": u.get("cached_input_tokens"),
        "output": u.get("output_tokens"), "wall": round(r["wall_s"], 1),
        "rc": r["exit_code"], "n_tasks": len(tasks),
        "writes": sorted(writes), "ref_n": len(ref), "covered": covered,
        "err": (r["stderr"] or "")[-200:],
    }


def main() -> None:
    only = sys.argv[1:] or ["h1", "b3"]
    for tid in only:
        task = Task.load(TASKS_DIR / tid)
        for variant in ("P0", "P1", "P2"):
            tmp = Path(tempfile.mkdtemp(prefix=f"probe-{tid}-{variant}-"))
            work = tmp / "repo"
            shutil.copytree(task.seed, work)
            try:
                rec = run_variant(task, variant, work)
            except Exception as e:                    # noqa: BLE001
                rec = {"task": tid, "variant": variant, "error": f"{type(e).__name__}: {e}"}
            print(json.dumps(rec, ensure_ascii=False), flush=True)
            with open(ROOT / "runs" / "exp" / "probe-plan-ctx.jsonl", "a",
                      encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
