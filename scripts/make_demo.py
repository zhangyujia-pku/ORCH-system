"""把 demo 夹具还原成一个可用的 git 仓库。

为什么要有它：
  `demo/tasks_app` 自带一个嵌套 `.git`（3.2M 历史），把它塞进封存包会让
  外层仓库把它当成 gitlink —— 换台机器 clone 出来是个空目录。
  所以打包时只带 `demo/fixture` 的源码，用这个脚本重建仓库：
  同样四份文件、同样一个 `init` 提交，跑实验的起点完全一致。

用法：
  python scripts/make_demo.py                # 生成 demo/tasks_app（已存在则拒绝）
  python scripts/make_demo.py --force        # 覆盖重建
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "demo" / "fixture"
TARGET = ROOT / "demo" / "tasks_app"


def _git(cwd: Path, *args: str) -> None:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit(f"[FAIL] git {' '.join(args)}：{(r.stderr or r.stdout).strip()}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="从 demo/fixture 重建 demo/tasks_app")
    ap.add_argument("--force", action="store_true", help="目标已存在时覆盖")
    args = ap.parse_args(argv)

    if not FIXTURE.is_dir():
        print(f"[FAIL] 找不到夹具模板：{FIXTURE}", file=sys.stderr)
        return 2
    if TARGET.exists():
        if not args.force:
            print(f"{TARGET} 已存在（要覆盖加 --force）")
            return 0
        shutil.rmtree(TARGET)

    shutil.copytree(FIXTURE, TARGET)
    _git(TARGET, "init", "-q")
    _git(TARGET, "config", "user.name", "orch-demo")
    _git(TARGET, "config", "user.email", "demo@example.local")
    _git(TARGET, "add", "-A")
    _git(TARGET, "commit", "-qm", "init: tasks_app demo fixture")
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(TARGET),
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout.strip()
    print(f"已生成 {TARGET}（提交 {head}）")
    print("下一步：python -m orch dispatch \"...\" --repo demo/tasks_app --plan plan.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
