"""从真实仓库的一条修复提交，造一道 M 档题。

做法（关键在"只回退源码，保留测试"）：
    提交 C 通常同时改了源码和测试。直接 revert 会把测试也删掉，题就没判据了。
    所以只 `git show C -- <源文件>` 取源码部分反向应用，测试留在修复后的状态。
    于是：仓库处于"buggy 源码 + 修好该 bug 的测试"，跑测试必失败。

产出：tasks/<id>/{meta.json, seed/, checks/}
     seed    完整仓库（buggy 源码 + 新测试），不含 .git
     checks  只跑那几条失败测试的脚本
     meta    accept 指向 checks

用法：
    python scripts/make_task_from_commit.py --repo C:/probe-repos/more-itertools \
        --commit fbd5513 --id t04 --source more_itertools/more.py \
        --tests tests/test_more.py
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASKS = ROOT / "tasks"


def run(cmd: list[str], cwd: Path, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, timeout=timeout)


def failing_tests(repo: Path, test_file: str) -> list[str]:
    """跑一遍测试，把失败的 node id 抓出来。"""
    r = run([sys.executable, "-m", "pytest", "-q", test_file, "--tb=no", "-q"], repo, 900)
    out = (r.stdout + r.stderr).decode("utf-8", "replace")
    ids = re.findall(r"^(?:FAILED|ERROR)\s+([^\s]+)", out, re.M)
    return sorted(set(ids))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="本地已 clone 的仓库")
    ap.add_argument("--commit", required=True, help="要回退的修复提交")
    ap.add_argument("--id", required=True, help="任务 id，如 t04")
    ap.add_argument("--source", required=True, help="源码文件（相对仓库根），可逗号分隔")
    ap.add_argument("--tests", required=True, help="测试文件（相对仓库根）")
    ap.add_argument("--tier", default="M")
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)

    repo = Path(args.repo).resolve()
    srcs = [s.strip() for s in args.source.split(",") if s.strip()]
    dest = TASKS / args.id
    if dest.exists() and not args.force:
        print(f"{dest} 已存在（加 --force 覆盖）", file=sys.stderr)
        return 2

    head = run(["git", "rev-parse", "HEAD"], repo).stdout.decode().strip()
    print(f"仓库 HEAD {head[:8]}；目标提交 {args.commit}")

    # 取源码部分的 diff（不含测试）
    # 用 C^..C 而不是 git show：merge 提交上 git show 给的是合并 diff，常常不能 apply。
    patch = run(["git", "diff", f"{args.commit}^", args.commit, "--", *srcs],
                repo).stdout.decode("utf-8", "replace")
    if "diff --git" not in patch:
        print("该提交没有改这些源文件，换个提交", file=sys.stderr)
        return 2

    tmp = Path(tempfile.mkdtemp(prefix=f"mktask-{args.id}-"))
    work = tmp / "repo"
    # 工作树要停在"提交之后"的状态：测试是新的
    r = run(["git", "worktree", "add", "--detach", str(work), args.commit], repo)
    if r.returncode != 0:
        r = run(["git", "worktree", "add", "--detach", "-f", str(work), args.commit], repo)
        if r.returncode != 0:
            print("worktree 创建失败：" + r.stderr.decode("utf-8", "replace")[:300], file=sys.stderr)
            return 2

    try:
        # 反向应用源码补丁 → 源码回到 buggy，测试仍是修复后的
        pf = tmp / "src.patch"
        pf.write_text(patch, encoding="utf-8")
        r = run(["git", "apply", "-R", "--whitespace=nowarn", str(pf)], work)
        if r.returncode != 0:
            print("反向应用失败：" + r.stderr.decode("utf-8", "replace")[:400], file=sys.stderr)
            return 2

        failed = failing_tests(work, args.tests)
        if not failed:
            print("回退源码后测试没有失败——这道题没有判据，丢弃", file=sys.stderr)
            return 3
        print(f"回退后失败 {len(failed)} 条，例如 {failed[0]}")

        # 造 seed：去掉 .git 的整棵工作树
        if dest.exists():
            shutil.rmtree(dest)
        seed = dest / "seed"
        shutil.copytree(work, seed, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))

        # 正向应用一遍，确认这题可解（参考解 = 原提交的源码改动）
        r = run(["git", "apply", "--whitespace=nowarn", str(pf)], work)
        if r.returncode != 0:
            print("正向应用失败，题目不可解", file=sys.stderr)
            return 4
        still = failing_tests(work, args.tests)
        if still:
            print(f"参考解没能让测试全过（还剩 {len(still)} 条），丢弃", file=sys.stderr)
            return 4

        # checks：只跑这几条。**同时放进 seed**——
        # 规划器会把 goal 里的 "python checks/xxx.py" 原样抄进 acceptance，
        # 仓库里没有这个文件时，worker 的验收会直接报"找不到文件"，
        # 看起来像模型没做完，其实是题本身不自洽。
        checks = dest / "checks"
        checks.mkdir(parents=True, exist_ok=True)
        (seed / "checks").mkdir(parents=True, exist_ok=True)
        ids = " ".join(f'"{t}"' for t in failed)
        (checks / f"check_{args.id}.py").write_text(
            f'''"""只跑这几条测试：它们是该 bug 的判据。"""
import subprocess, sys

ids = [{", ".join(repr(t) for t in failed)}]
r = subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=short", *ids])
sys.exit(0 if r.returncode == 0 else 1)
''', encoding="utf-8")

        # seed 里放一份同样的 check，供 agent 自测（打分时用题库副本覆盖）
        (seed / "checks" / f"check_{args.id}.py").write_text(
            (checks / f"check_{args.id}.py").read_text(encoding="utf-8"), encoding="utf-8")

        subject = run(["git", "log", "-1", "--format=%s", args.commit], repo).stdout.decode("utf-8", "replace").strip()
        (dest / "meta.json").write_text(json.dumps({
            "id": args.id, "tier": args.tier,
            "goal": (f"{args.tests} 里有若干测试失败。修好它们，不要修改 {args.tests}，"
                     f"验收跑 python checks/check_{args.id}.py"),
            "timeout_s": args.timeout,
            "accept": [f"python checks/check_{args.id}.py"],
            "source_commit": args.commit, "source_subject": subject,
            "upstream": str(repo),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"已生成 tasks/{args.id}（{len(failed)} 条判据，来源：{subject[:50]}）")
        return 0
    finally:
        run(["git", "worktree", "remove", "--force", str(work)], repo)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
