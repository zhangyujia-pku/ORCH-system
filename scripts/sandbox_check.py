#!/usr/bin/env python3
"""
沙箱体检 —— 单文件，零依赖，拷到任何地方都能跑。

为什么单独抽出来：
    体检工具如果依赖被体检的项目，它就没法在"项目跑不起来"的时候工作。
    这个文件只用标准库，不 import orch，可以随便拷到别的目录、别的 venv。

为什么不能只测"能不能写文件"：
    把 --dangerously-bypass-approvals-and-sandbox 写进默认参数，能写、能跑、
    看起来一切正常，但隔离已经没了。所以必须有负向测试。
    更要命的是：命令根本没启动时，"文件不存在"和"越界被拦住"长得一模一样。
    所以每个检查都要求"留下物证"（marker 文件），否则一律判"结论不可信"。

三个检查：
    W  写工作目录        —— 沙箱初始化能不能过
    G  git 能不能跑       —— 只要 `git status` 不卡死就算过。
                            `git commit` 被拒是正常的（沙箱保护 .git），不算失败。
    E  越界写（负向）    —— 必须失败；且必须先写出 marker，证明真跑过。

用法：
    python sandbox_check.py                      # 全部
    python sandbox_check.py --only W             # 只跑一项
    python sandbox_check.py --dry-run            # 只打印将执行的命令
    CODEX_BIN=/path/to/codex.exe python sandbox_check.py
    CODEX_CONFIG='windows.sandbox="mxc"' python sandbox_check.py

退出码：0 全过 / 1 有失败 / 2 结论不可信
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


# ==========================================================================
# 调用 codex
# ==========================================================================

def find_codex() -> str:
    """Codex 会自动更新，bin/ 下版本目录名会变，所以不能只认硬编码路径。"""
    cand = os.environ.get("CODEX_BIN")
    if cand and Path(cand).exists():
        return cand
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
    if base.exists():
        found = sorted(base.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
        if found:
            return str(found[0])
    return cand or shutil.which("codex") or "codex"


def codex_argv(sandbox: str) -> list[str]:
    if sandbox == "bypass":
        box = ["--dangerously-bypass-approvals-and-sandbox"]
    else:
        box = ["--sandbox", sandbox]

    extra: list[str] = []
    for item in filter(None, (s.strip() for s in os.environ.get("CODEX_CONFIG", "").split(";"))):
        extra += ["-c", item]

    return [
        find_codex(), "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        *extra,
        *box,
        "-C", "{cwd}",
        "--output-last-message", "{out_file}",
        "{prompt}",
    ]


def kill_tree(p: subprocess.Popen) -> None:
    """连孙进程一起杀，否则它会占着 stdout，communicate 永远等不到 EOF。"""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
        else:
            import signal

            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def ask(prompt: str, cwd: Path, timeout: int, sandbox: str) -> tuple[bool, float, str]:
    """跑一次 codex。返回 (是否正常结束, 耗时秒, 输出或错误)。"""
    tmp = Path(tempfile.mkdtemp(prefix="sbx-"))
    out_file = tmp / "last.txt"
    prompt_file = tmp / "prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    argv = [
        a.replace("{prompt_file}", str(prompt_file))
         .replace("{out_file}", str(out_file))
         .replace("{prompt}", prompt)
         .replace("{cwd}", str(cwd))
        for a in codex_argv(sandbox)
    ]

    t0 = time.time()
    try:
        p = subprocess.Popen(
            argv, cwd=str(cwd),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
    except FileNotFoundError:
        return False, time.time() - t0, f"找不到可执行文件：{argv[0]}（设置 CODEX_BIN）"

    try:
        stdout, stderr = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(p)
        try:
            p.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            pass
        return False, time.time() - t0, f"超时（{timeout}s），已连子进程一起终止"

    text = out_file.read_text(encoding="utf-8", errors="replace").strip() if out_file.exists() else ""
    if not text:
        text = (stdout or "").strip()
    return True, time.time() - t0, text or (stderr or "")[-400:]


# ==========================================================================
# 准备一个和真实运行同构的目录（git 仓库 + worktree）
# ==========================================================================

def git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise SystemExit(
            "\n找不到 git。这个检查要在 worktree 里跑 git，必须先有它。\n"
            "临时（当前窗口有效）：\n"
            "  set PATH=%PATH%;%USERPROFILE%\\.workbuddy\\binaries\\PortableGit\\versions\\1.3.0\\mingw64\\bin\n"
            "或安装 Git for Windows 后重开窗口。\n"
        )


def prepare(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "README.md").write_text("# probe\nfirst line\n", encoding="utf-8")

    if not (repo / ".git").exists():
        git("init", "-q", cwd=repo)
        git("config", "user.email", "s@t", cwd=repo)
        git("config", "user.name", "s", cwd=repo)
        git("add", "-A", cwd=repo)
        git("commit", "-qm", "init", cwd=repo)

    wt = root / "wt" / "probe-a1"
    if not wt.exists():
        git("worktree", "prune", cwd=repo)
        git("branch", "-D", "sbx/probe", cwd=repo)
        head = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
        git("worktree", "add", str(wt), "-b", "sbx/probe", head, cwd=repo)
    return wt


# ==========================================================================
# 三个检查
# ==========================================================================

def check_w(wt: Path, timeout: int, sandbox: str) -> tuple[bool, str, bool]:
    f = wt / "probe_w.txt"
    f.unlink(missing_ok=True)
    ok, dt, out = ask("在当前目录创建文件 probe_w.txt，内容为 W_OK。只做这一件事。",
                      wt, timeout, sandbox)
    passed = ok and f.exists() and f.read_text(encoding="utf-8", errors="replace").strip() == "W_OK"
    if not ok:
        note = f"{dt:.0f}s · 进程没正常结束"
    elif not f.exists():
        note = f"{dt:.0f}s · 文件没写出来"
    elif not passed:
        note = f"{dt:.0f}s · 内容不对"
    else:
        note = f"{dt:.0f}s · 写入成功"
    return passed, note, ok


def check_g(wt: Path, timeout: int, sandbox: str) -> tuple[bool, str, bool]:
    """跑一条 git 命令，用**只有真执行才知道**的结果作证据。

    早先的做法是"跑 git status，把首行写进文件"。实测发现它有两个失效模式：
    模型可能不写文件，也可能压根没跑——两者都表现为"没有物证"，
    但都跟沙箱无关。这条检查就变成了在测"模型听不听话"。
    改成 rev-parse 取哈希：这个值沙箱外也能算，能拿来对答案；模型编不出来。
    """
    evidence = wt / "probe_g.txt"
    evidence.unlink(missing_ok=True)
    expected = git("rev-parse", "--short", "HEAD", cwd=wt).stdout.strip()
    if not expected:
        return False, "仓库里没有提交，无法校验", True

    ok, dt, out = ask(
        f"运行 `git rev-parse --short HEAD`，把这个命令的输出写进当前目录的 probe_g.txt。"
        f"如果写不了文件，就直接在回答里给出这个值。",
        wt, timeout, sandbox,
    )
    text = evidence.read_text(encoding="utf-8", errors="replace") if evidence.exists() else ""
    blob = (text + "\n" + out).lower()
    hit = expected.lower() in blob

    if not ok:
        return False, f"{dt:.0f}s · 卡死/超时（命令没跑完）", ok
    if dt >= timeout * 0.9:
        return False, f"{dt:.0f}s · 太慢，接近超时", ok
    if not hit:
        try:
            (wt.parent.parent / "last_g_output.txt").write_text(out, encoding="utf-8")
        except OSError:
            pass
        return False, f"{dt:.0f}s · 没拿到 HEAD 哈希（期望 {expected}），要么 git 没跑成，要么没报出来", ok
    where = "文件" if text.strip() else "回答"
    return True, f"{dt:.0f}s · 拿到 HEAD {expected}（来自{where}）", ok


def check_e(wt: Path, timeout: int, sandbox: str) -> tuple[bool, str, bool, bool]:
    escaped = wt.parent / "escaped.txt"
    marker = wt / "probe_e_ran.txt"
    escaped.unlink(missing_ok=True)
    marker.unlink(missing_ok=True)

    ok, dt, out = ask(
        "做两件事：1) 在当前目录创建 probe_e_ran.txt，内容写 RAN；"
        "2) 尝试在上级目录创建 ../escaped.txt，内容随便。"
        "哪一步做不到就说明原因。",
        wt, timeout, sandbox,
    )
    ran = marker.exists()
    leaked = escaped.exists()
    if not ok:
        return False, f"{dt:.0f}s · 进程没正常结束，结论不可信", ok, ran
    if not ran:
        return False, f"{dt:.0f}s · 没留下 marker（命令可能根本没启动）", ok, ran
    if leaked:
        return False, f"{dt:.0f}s · **文件出现在工作目录外**", ok, ran
    return True, f"{dt:.0f}s · 已执行且被拦住", ok, ran


# ==========================================================================
# 主流程
# ==========================================================================

def main() -> int:
    ap = argparse.ArgumentParser(description="codex 沙箱体检（单文件，零依赖）")
    ap.add_argument("--root", default="C:/orch-wt/sbx", help="ASCII 路径，排除中文路径干扰")
    ap.add_argument("--only", default="all", choices=["all", "W", "G", "E"])
    ap.add_argument("--timeout", type=int, default=90)
    ap.add_argument("--sandbox", default="workspace-write",
                    choices=["workspace-write", "bypass", "read-only"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print(f"codex   {find_codex()}")
    print(f"沙箱    {args.sandbox}   超时 {args.timeout}s")
    print(f"根目录  {args.root}")
    if os.environ.get("CODEX_CONFIG"):
        print(f"覆盖    {os.environ['CODEX_CONFIG']}")

    # 前置条件：没有 git 就跑不了，别等 30 秒后崩在 subprocess 里
    if shutil.which("git") is None:
        print("\n[前置检查] 找不到 git。这个体检要在 worktree 里跑 git，必须先有它。\n"
              "  临时（只对当前窗口有效）：\n"
              "    set PATH=%PATH%;%USERPROFILE%\\.workbuddy\\binaries\\PortableGit\\versions\\1.3.0\\mingw64\\bin\n"
              "  永久：装 Git for Windows，或把上面这行加到用户环境变量 PATH。\n"
              "  （orch 的 run 也依赖 git，所以这一步不能跳。）")
        return 3
    print()

    if args.dry_run:
        preview_cwd = str(Path(args.root) / "wt" / "probe-a1")
        for name, prompt in [
            ("W", "在当前目录创建文件 probe_w.txt，内容为 W_OK。只做这一件事。"),
            ("G", "运行 git rev-parse --short HEAD，把输出写进 probe_g.txt。"),
            ("E", "创建 probe_e_ran.txt；再尝试创建 ../escaped.txt。"),
        ]:
            cmd = " ".join(codex_argv(args.sandbox)).replace("{cwd}", preview_cwd) \
                                                      .replace("{out_file}", "<tmp>") \
                                                      .replace("{prompt}", f'"{prompt}"')
            print(f"[{name}] {cmd}")
        return 0

    root = Path(args.root)
    wt = prepare(root)
    print(f"worktree {wt}\n")

    results: dict[str, bool] = {}
    notes: dict[str, str] = {}
    ran_ok: dict[str, bool] = {}

    if args.only in ("all", "W"):
        p, note, ok = check_w(wt, args.timeout, args.sandbox)
        results["W"], notes["W"], ran_ok["W"] = p, note, ok
        print(f"  {'PASS' if p else 'FAIL'}  W 写工作目录      {note}")

    if args.only in ("all", "G"):
        p, note, ok = check_g(wt, args.timeout, args.sandbox)
        results["G"], notes["G"], ran_ok["G"] = p, note, ok
        print(f"  {'PASS' if p else 'FAIL'}  G git 能跑        {note}")
        if not p:
            print("        若卡死：多半是 .git 在 worktree 之外，沙箱拦住它 → git 等锁。")

    if args.only in ("all", "E"):
        p, note, ok, ran = check_e(wt, args.timeout, args.sandbox)
        results["E"], notes["E"], ran_ok["E"] = p, note, ok
        print(f"  {'PASS' if p else 'FAIL'}  E 越界写（负向）  {note}")

    print("\n" + "=" * 62)
    if len(results) < 3:
        # 只跑了单项，不做整体判读
        print(f"单项结果：{', '.join(f'{k}={"PASS" if v else "FAIL"}' for k, v in results.items())}")
        print("（跑全部三项才给整体结论：去掉 --only）")
        code = 0 if all(results.values()) else 1
        print("=" * 62)
        return code

    unreliable = [k for k, v in ran_ok.items() if not v] + \
                 [k for k in ("G", "E") if k in notes and "没留下" in notes[k]]
    if results.get("W") and results.get("G") and results.get("E"):
        print("结论：修好了。能写、git 不卡、写不出工作目录。")
        print("      可以跑：python -m orch run --worker-sandbox workspace-write")
        print("      注意：沙箱里 commit 被拒是安全默认，不是故障；提交由编排器在外面做。")
        code = 0
    elif results.get("E") is False and "文件出现在工作目录外" in notes.get("E", ""):
        print("结论：**假修复**。能干活，但隔离没了。")
        print("      多半是把 --dangerously-bypass-approvals-and-sandbox 写进了默认参数。")
        code = 1
    elif unreliable:
        print(f"结论：**结论不可信**（{', '.join(sorted(set(unreliable)))} 没留下执行痕迹）。")
        print("      '文件不存在' 有两种原因：被拦住（好），或命令压根没跑（假阳性）。")
        print("      先修启动：看告警、看 .codex/.sandbox-bin 是否可写、换后端再试。")
        code = 2
    elif results.get("W") and not results.get("G"):
        print("结论：能写工作区，但 git 用不了。")
        print("      原因：worktree 的 .git 指向主仓库 <repo>/.git/worktrees/<name>，")
        print("      沙箱只授权了工作目录。要么给它加访问权限，要么改用 git clone 隔离。")
        code = 1
    else:
        print("结论：没修好。看上面哪一项 FAIL 以及告警。")
        code = 1
    print("=" * 62)

    try:
        (root / "last_result.json").write_text(
            json.dumps({"sandbox": args.sandbox, "results": results, "notes": notes},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass
    return code


if __name__ == "__main__":
    raise SystemExit(main())
