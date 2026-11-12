"""冻结题库：给"尺子"盖一个不可含糊的戳。

背景（必须写下来，免得再犯）：轨道 B 的题在正式跑之前被改过三次——
补 .gitignore、修检查脚本的 sys.path、改参考解的空串判定。
每改一次，之前跑出来的数据就不能和之后的比。这不是"迭代"，是**换了一把尺子**。

这个脚本做两件事：
  1) freeze：把题库的每个文件算 sha256，汇总成一把总哈希，写进 tasks/FREEZE.json
  2) audit ：用四种"作弊/退化"情形去撞验收，确认判据不是纸糊的

任何跑批工具（sim.py / exp_runner.py）在开始前都要核对这把总哈希；
对不上就拒绝写入结果——宁可停下，也不要产出一批口径不一的数据。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
TASKS = ROOT / "tasks"
FREEZE = TASKS / "FREEZE.json"
sys.path.insert(0, str(ROOT))

from scripts.exp_lib import init_repo, load_tasks, score        # noqa: E402


def sha256(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def task_digest(d: pathlib.Path) -> tuple[str, int]:
    """把一个题目录（除 FREEZE.json 外）压成一个哈希。"""
    h = hashlib.sha256()
    n = 0
    for f in sorted(x for x in d.rglob("*") if x.is_file()):
        rel = f.relative_to(d).as_posix()
        if rel.startswith("runs/") or rel == "FREEZE.json":
            continue
        h.update(rel.encode("utf-8"))
        h.update(f.read_bytes())
        n += 1
    return h.hexdigest(), n


def codex_version() -> str:
    try:
        from orch.planner import resolve_codex
        r = subprocess.run([resolve_codex(), "--version"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=60)
        return (r.stdout or r.stderr).strip().replace("\r", "")
    except Exception as e:                                    # noqa: BLE001
        return f"未知（{type(e).__name__}）"


def cmd_freeze(args) -> int:
    ids = [x.strip() for x in args.tasks.split(",") if x.strip()]
    tasks = {t.id: t for t in load_tasks(ids)}
    missing = [i for i in ids if i not in tasks]
    if missing:
        print(f"[✗] 题库里没有：{missing}")
        return 2

    files, total, counts = {}, hashlib.sha256(), {}
    for tid in ids:
        d = TASKS / tid
        digest, n = task_digest(d)
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        files[tid] = digest
        counts[tid] = {"files": n, "accept": len(meta.get("accept") or []),
                       "batch_size": meta.get("batch_size")}
        total.update(f"{tid}:{digest}".encode())

    doc = {
        "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "version": args.version,
        "total_hash": total.hexdigest(),
        "tasks": files,
        "counts": counts,
        "codex": codex_version(),
        "orch": subprocess.run(["git", "describe", "--tags"], cwd=str(ROOT),
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace").stdout.strip(),
        "rule": "冻结后任何文件改动都必须升 version，并声明旧数据作废。",
    }
    out = pathlib.Path(args.out) if args.out else FREEZE
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[ok] 已冻结 {args.version}")
    print(f"     总哈希 {doc['total_hash'][:16]}…  → {out.name}")
    for tid in ids:
        c = counts[tid]
        print(f"     {tid}: {c['files']} 个文件 · 验收 {c['accept']} 条 · 单元 {c['batch_size']}")
    print(f"     codex {doc['codex']} · orch {doc['orch']}")
    return 0


def all_frozen() -> dict[str, dict]:
    """把所有 FREEZE*.json 合并成 {任务: 记录}。

    为什么要多份：b1-b3 属于 taskset-v1，h1 属于另一版；旧数据的哈希不能被新题覆盖，
    否则"这一行是在哪把尺子下量的"就没法追溯了。
    """
    merged: dict[str, dict] = {}
    for f in sorted(TASKS.glob("FREEZE*.json")):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for tid, h in (doc.get("tasks") or {}).items():
            merged[tid] = {"hash": h, "version": doc.get("version"), "file": f.name}
    return merged


def verify_hash(ids: list[str]) -> tuple[bool, str]:
    """给跑批工具调用：核对当前题库是否仍是冻结时那一把。"""
    frozen = all_frozen()
    if not frozen:
        return False, "缺 tasks/FREEZE*.json（先跑 freeze_taskset.py freeze）"
    for tid in ids:
        if tid not in frozen:
            return False, f"{tid} 不在任何冻结清单里"
        now, _ = task_digest(TASKS / tid)
        if now != frozen[tid]["hash"]:
            return False, (f"{tid} 的文件已变化（冻结 {frozen[tid]['hash'][:8]} / "
                           f"现在 {now[:8]}，见 {frozen[tid]['file']}）")
    return True, "+".join(f"{frozen[t]['version']}:{frozen[t]['hash'][:8]}" for t in ids)


def cmd_check(args) -> int:
    ids = [x.strip() for x in args.tasks.split(",") if x.strip()]
    ok, info = verify_hash(ids)
    print(f"[{'✓' if ok else '✗'}] {info}")
    return 0 if ok else 1


# ------------------------------------------------------------------ audit

def _ref_patch(task) -> str:
    """把 ref/ 覆盖到 seed 上，产出等价于"标准答案"的补丁。"""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="audit-ref-"))
    try:
        repo = tmp / "repo"
        init_repo(task, repo)
        for item in (ROOT / "tasks" / task.id / "ref").rglob("*"):
            if item.is_file():
                dst = repo / item.relative_to(ROOT / "tasks" / task.id / "ref")
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, dst)
        # 注意：这里必须按字节读。中文 Windows 的默认编码是 gbk，
        # 而仓库内容是 UTF-8；用 text=True 去读补丁会在 reader 线程里抛
        # UnicodeDecodeError（表现是一堆 thread 报错，真正原因很容易被忽略）。
        base = subprocess.run(["git", "rev-list", "--max-parents=0", "HEAD"], cwd=str(repo),
                              capture_output=True).stdout.decode("utf-8", "replace").strip().splitlines()[-1]
        return subprocess.run(["git", "diff", base], cwd=str(repo),
                              capture_output=True).stdout.decode("utf-8", "replace")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_audit(args) -> int:
    ids = [x.strip() for x in args.tasks.split(",") if x.strip()]
    tasks = {t.id: t for t in load_tasks(ids)}
    rows, bad = [], 0

    for tid in ids:
        t = tasks[tid]
        n = len(t.accept)

        # ① 空补丁：应 0 通过
        r = score(t, "")
        ok0 = (r.get("resolved") is False) and r.get("apply_ok") is True
        rows.append((tid, "空补丁 → 0 通过", ok0, f"resolved={r.get('resolved')}"))

        # ② 参考解：应全通过
        r = score(t, _ref_patch(t))
        ok1 = bool(r.get("resolved"))
        rows.append((tid, f"参考解 → {n}/{n} 通过", ok1, f"resolved={r.get('resolved')}"))

        # ③ 篡改验收：把 seed 里的 checks 改成永真，再交空补丁
        #    评分用的是题库副本，所以仍应 0 通过——这一条验证"被验收者改不了标准"
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="audit-tamper-"))
        try:
            repo = tmp / "repo"
            init_repo(t, repo)
            for c in (repo / "checks").glob("*.py"):
                c.write_text("import sys\nprint('all good')\nsys.exit(0)\n", encoding="utf-8")
            patch = subprocess.run(["git", "diff"], cwd=str(repo),
                                   capture_output=True).stdout.decode("utf-8", "replace")
            r = score(t, patch)
            ok2 = (r.get("resolved") is False)
            rows.append((tid, "篡改 checks → 仍判不通过", ok2, f"resolved={r.get('resolved')}"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'题':<5}{'撞法':<26}{'结果':<6}备注")
    for tid, what, ok, note in rows:
        bad += 0 if ok else 1
        print(f"{tid:<5}{what:<26}{'✓' if ok else '✗':<6}{note}")
    print(f"\n结论：{'三关全过，验收可以当真' if not bad else f'{bad} 项未通过，先修题再跑'}")
    return 0 if not bad else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("freeze")
    p1.add_argument("--tasks", default="b1,b2,b3")
    p1.add_argument("--version", default="taskset-v1")
    p1.add_argument("--out", default="", help="冻结记录写到哪（默认 tasks/FREEZE.json）")
    p1.set_defaults(fn=cmd_freeze)

    p2 = sub.add_parser("check")
    p2.add_argument("--tasks", default="b1,b2,b3")
    p2.set_defaults(fn=cmd_check)

    p3 = sub.add_parser("audit")
    p3.add_argument("--tasks", default="b1,b2,b3")
    p3.set_defaults(fn=cmd_audit)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
