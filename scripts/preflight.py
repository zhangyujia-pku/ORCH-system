"""换机器第一件事：跑这个。

它只做**不花钱**的检查：Python、git、codex 二进制、沙箱能不能起进程、凭据在不在。
模型调用一次都不发——所以要验"端到端通不通"还得真跑一次任务，但那一步会花钱，
不该在环境没验干净的时候花。

用法：
    python scripts/preflight.py
    python scripts/preflight.py --json
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OK, WARN, FAIL = "PASS", "WARN", "FAIL"


def _run(argv: list[str], timeout: int = 20) -> tuple[int, str]:
    try:
        r = subprocess.run(argv, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    raw = (r.stdout or b"") + (r.stderr or b"")
    text = ""
    for enc in ("utf-8", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", "replace")
    # codex 常打这行无害告警（arg0 临时目录清不掉），会盖住真正的结论
    text = "\n".join(ln for ln in text.splitlines()
                     if "failed to clean up stale arg0" not in ln)
    return r.returncode, text.strip()


def check_python() -> tuple[str, str, str]:
    v = sys.version_info
    detail = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) < (3, 10):
        return FAIL, "Python", f"{detail} —— 需要 3.10+"
    return OK, "Python", detail


def check_git() -> tuple[str, str, str]:
    if not shutil.which("git"):
        return FAIL, "git", "找不到 git（worktree 依赖它）"
    code, out = _run(["git", "--version"])
    return (OK if code == 0 else FAIL), "git", out or "版本未知"


def check_import() -> tuple[str, str, str]:
    sys.path.insert(0, str(ROOT))
    try:
        from orch.cli import main           # noqa: F401
    except Exception as e:                  # noqa: BLE001
        return FAIL, "orch 导入", f"{type(e).__name__}: {e}"
    return OK, "orch 导入", str(ROOT)


def check_codex_exec() -> tuple[str, str, str]:
    """这个 codex 版本的 `exec` 认不认 workers.json 里用的那些参数。

    只验版本号是不够的：0.167 换成别的版本后，`--json` / `--skip-git-repo-check` /
    `--sandbox` / `-C` 任意一个被改名或移除，orch 都会在**真正派发时**才失败，
    而失败信息（“退出码 1”）看起来像模型挂了。这条检查不花钱，能在派发前拦住。
    """
    sys.path.insert(0, str(ROOT))
    try:
        from orch.planner import resolve_codex

        exe = resolve_codex()
    except Exception as e:                  # noqa: BLE001
        return WARN, "codex exec 参数", f"拿不到 codex 路径：{e}"
    if not exe or not Path(exe).exists():
        return WARN, "codex exec 参数", "拿不到 codex 路径，跳过"
    code, out = _run([exe, "exec", "--help"])
    if code != 0:
        return FAIL, "codex exec 参数", f"`exec --help` 退出码 {code}：{out[-120:]}"
    need = ["--json", "--skip-git-repo-check", "--sandbox", "--cd", "-c", "-m"]
    missing = [f for f in need if f not in out]
    if missing:
        return FAIL, "codex exec 参数", (
            f"缺 {', '.join(missing)}——这个版本与 workers.json 的模板不兼容；"
            f"先 `codex --version` 记下版本，再决定是换版本还是改模板")
    return OK, "codex exec 参数", "json / skip-git-repo-check / sandbox / cd 都在"


def check_codex() -> tuple[str, str, str]:
    sys.path.insert(0, str(ROOT))
    try:
        from orch.planner import resolve_codex

        exe = resolve_codex()
    except Exception as e:                  # noqa: BLE001
        return FAIL, "codex 二进制", f"{type(e).__name__}: {e}"
    if not exe or not Path(exe).exists():
        return FAIL, "codex 二进制", f"解析不到：{exe}"
    code, out = _run([exe, "--version"])
    return (OK if code == 0 else WARN), "codex 二进制", f"{exe} · {out}"


def _win_sandbox_present() -> tuple[bool, str]:
    """Windows 沙箱是否已就位（看服务是否注册）。

    这是 `codex sandbox` 探针失败时的退路。实测（2026-11-13）：
    探针返回 1、stdout 全空；但同一台机器上 `codex exec --sandbox workspace-write`
    能正常起，输出里明确写着 `sandbox: workspace-write ... (network access enabled)`。
    也就是说探针不可靠，不能拿它判死刑。

    只读注册表，不调外部命令 —— `sc` / `net` 在部分机器的命令安全策略里被拉黑，
    一调就整段检查作废（实测会报 PROGRAM BLOCKED，且不允许绕过）。
    """
    try:
        import winreg
    except ImportError:
        return False, "非 Windows 环境"
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Services\CodexSandboxService.OpenAI.Codex",
        ):
            pass
    except OSError:
        return False, "沙箱服务没注册（注册表里没有这个服务）"
    return True, "沙箱服务已注册、配置就位（探针本身失效）"


def check_sandbox() -> tuple[str, str, str]:
    """沙箱能不能起进程——这是真实踩过的坑，而且能免费验。

    新版 codex 在 Windows 上 `workspace-write` 会报
    `registered Core launch requires an installed app`，worker 因此一个文件都改不了。
    orch 在 Windows 默认改走 bypass；这条检查确认「bypass 那条路至少能起进程」。
    """
    sys.path.insert(0, str(ROOT))
    try:
        from orch.planner import resolve_codex

        exe = resolve_codex()
    except Exception:                       # noqa: BLE001
        return WARN, "沙箱起进程", "拿不到 codex 路径，跳过"
    if not exe or not Path(exe).exists():
        return WARN, "沙箱起进程", "拿不到 codex 路径，跳过"

    if os.name == "nt":
        argv = [exe, "sandbox", "--", "cmd", "/c", "echo", "orch-preflight"]
    else:
        argv = [exe, "sandbox", "--", "sh", "-c", "echo orch-preflight"]
    code, out = _run(argv, timeout=60)
    # codex 自己会往 stderr 打 "failed to clean up stale arg0 temp dirs"，
    # 无害但会盖住真正的结论，过滤掉
    out = "\n".join(ln for ln in out.splitlines()
                     if "failed to clean up stale arg0" not in ln)
    if "orch-preflight" in out:
        return OK, "沙箱起进程", "codex sandbox 能起子进程"
    if os.name == "nt":
        healthy, detail = _win_sandbox_present()
        if healthy:
            # 探针在某些 codex 版本里什么都不输出、直接退出 1，但沙箱其实是好的
            # （exec 能起、配置也生效）。这时报红会把人带偏，降级成提醒。
            return WARN, "沙箱起进程", f"探针无输出（这个版本不可靠），但{detail}"
    return FAIL, "沙箱起进程", (out.splitlines() or ["无输出"])[-1][:160]


def check_env() -> tuple[str, str, str]:
    key = os.environ.get("DEEPSEEK_KEY")
    if key:
        return OK, "模型凭据", "环境变量 DEEPSEEK_KEY 已设置"
    return WARN, "模型凭据", "未设置 DEEPSEEK_KEY —— 真跑任务前要设，或让 codex 自己带登录态"


def check_defaults() -> tuple[str, str, str]:
    sys.path.insert(0, str(ROOT))
    try:
        from orch.workers import load_workers

        ws, ladder = load_workers(str(ROOT / "workers.json"))
    except Exception as e:                  # noqa: BLE001
        return WARN, "worker 注册表", f"{type(e).__name__}: {e}"
    first = ladder[0] if ladder else "（空）"
    return OK, "worker 注册表", f"{len(ws)} 档 · ladder={ladder} · 默认 {first}"


CHECKS = (check_python, check_git, check_import, check_codex, check_codex_exec,
          check_sandbox, check_env, check_defaults)


def main(argv: list[str] | None = None) -> int:
    as_json = "--json" in (argv if argv is not None else sys.argv[1:])
    rows = [fn() for fn in CHECKS]
    worst = FAIL if any(r[0] == FAIL for r in rows) else (
        WARN if any(r[0] == WARN for r in rows) else OK)

    if as_json:
        print(json.dumps({"result": worst,
                          "platform": platform.platform(),
                          "checks": [{"level": l, "name": n, "detail": d} for l, n, d in rows]},
                         ensure_ascii=False, indent=2))
        return 0 if worst != FAIL else 2

    print(f"平台　{platform.platform()}\n")
    for level, name, detail in rows:
        mark = {"PASS": "✓", "WARN": "!", "FAIL": "✗"}[level]
        print(f"  {mark} {name:<12} {detail}")
    print()
    if worst == FAIL:
        print("有硬性检查没过，先修它，再谈跑任务。")
        return 2
    if worst == WARN:
        print("可以跑，但上面带 ! 的项要先想清楚（尤其模型凭据）。")
        return 0
    print("环境就绪。下一步：python -m unittest discover -s tests -t . 　（约 8 分钟）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
