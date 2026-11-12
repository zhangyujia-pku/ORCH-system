"""把 orch 装成一个随处可用的命令。

为什么需要它：
    仓库里跑的永远是 `python -m orch`，而 `-m` 要求当前目录能找到这个包。
    真实使用时没人会"先 cd 到 orch 仓库再开客户端"——客户端开在哪，命令就得在哪能用。

    于是这里做两件事：
      1. 在用户 bin 目录放一个 `orch.cmd`（Windows）或 `orch`（POSIX）垫片；
      2. 垫片只负责把仓库路径塞进 sys.path，然后转给真正的 CLI。
    路径写在垫片里，所以换机器要重装一次（一条命令，不联网）。

用法：
    python scripts/install_cli.py            # 装到 %USERPROFILE%\\bin（或 ~/bin）
    python scripts/install_cli.py --check    # 只检查装没装、能不能跑
    python scripts/install_cli.py --dir <目录>
"""

from __future__ import annotations

import argparse
import locale
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = "orch"          # 垫片里那个 .py 的名字（Windows 下还会生成 orch.cmd）
SKILL_SRC = ROOT / "skill" / "SKILL.md"


def skill_target() -> Path:
    """Codex 的用户级 skills 目录。

    不装这个，用户就只能在终端里敲命令——而产品的整个前提是"在 Codex 里说需求、
    在 Codex 里收结果"。之前安装脚本只装命令，skill 靠口令"自己复制过去"，
    等于主入口没装。CODEX_HOME 优先，和 codex 自己解析配置的顺序一致。
    """
    base = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    return base / "skills" / "orch" / "SKILL.md"


def skill_status() -> tuple[str, str]:
    """same / missing / differs / nosource。"""
    dst = skill_target()
    if not SKILL_SRC.exists():
        return "nosource", str(SKILL_SRC)
    if not dst.exists():
        return "missing", str(dst)
    same = (SKILL_SRC.read_text(encoding="utf-8")
            == dst.read_text(encoding="utf-8"))
    return ("same" if same else "differs"), str(dst)


def install_skill() -> tuple[bool, str]:
    """把 skill/SKILL.md 复制到 Codex 用户级 skills 目录。"""
    dst = skill_target()
    if not SKILL_SRC.exists():
        return False, f"仓库里缺 {SKILL_SRC}"
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        # 统一 LF：Windows 上 checkout 出来常是 CRLF，而 skill 是按行解析 frontmatter 的，
        # 让格式跟着 git 配置漂移没有好处。
        text = SKILL_SRC.read_text(encoding="utf-8").replace("\r\n", "\n")
        dst.write_text(text, encoding="utf-8", newline="\n")
    except OSError as e:
        return False, f"{dst} 写不进去：{e}"
    return True, str(dst)


def default_bin_dir() -> Path:
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    return home / "bin"


def on_path(d: Path) -> bool:
    target = str(d).rstrip("\\/").lower()
    return any(str(Path(p).as_posix()).rstrip("/").lower().endswith(target)
               or p.rstrip("\\/").lower() == target
               for p in os.environ.get("PATH", "").split(os.pathsep) if p)


def _write_cmd(shim: Path, py: Path) -> None:
    """写 Windows .cmd 垫片。

    .cmd 由 cmd.exe 按控制台代码页解释，既不能无条件按 ASCII 写
    （解释器路径含中文就 UnicodeEncodeError），也不能无条件按 UTF-8 写
    （中文 Windows 默认 936，UTF-8 字节会变乱码）。
    先按本机 ANSI 代码页写；连它也编不了时，退化为不把解释器路径写进
    .cmd，交给 PATH 里的 python —— 这层垫片始终是纯 ASCII，不会乱码。
    """
    text = (
        "@echo off\r\n"
        "setlocal\r\n"
        f'"{sys.executable}" "%~dp0{py.name}" %*\r\n'
    )
    enc = locale.getpreferredencoding(False) or "mbcs"
    try:
        shim.write_text(text, encoding=enc)
        return
    except (UnicodeEncodeError, LookupError):
        pass
    shim.write_text(
        "@echo off\r\n"
        "setlocal\r\n"
        f'python "%~dp0{py.name}" %*\r\n',
        encoding="ascii")


def write_launcher(bin_dir: Path) -> tuple[Path, Path]:
    """写两个文件：真正的入口（UTF-8，能装中文路径）和一层薄垫片（纯 ASCII）。"""
    bin_dir.mkdir(parents=True, exist_ok=True)
    py = bin_dir / "orch.py"
    # 入口用 UTF-8：仓库路径里有中文，.cmd 走系统代码页会乱码，
    # 所以把路径放在 .py 里，.cmd 只负责用 python 调它。
    py.write_text(
        '"""orch 命令入口（由 scripts/install_cli.py 生成，换机器重装一次即可）。"""\n'
        "import sys\n\n"
        f"REPO = {str(ROOT)!r}\n"
        "if REPO not in sys.path:\n"
        "    sys.path.insert(0, REPO)\n\n"
        "from orch.cli import main\n\n"
        'if __name__ == "__main__":\n'
        "    raise SystemExit(main(sys.argv[1:]))\n",
        encoding="utf-8")

    if os.name == "nt":
        shim = bin_dir / "orch.cmd"
        _write_cmd(shim, py)
        # Git Bash 不认 .cmd，顺手放一个同名的 sh 垫片；cmd 不会碰它（无扩展名）
        posix = bin_dir / "orch"
        posix.write_text(
            "#!/bin/sh\n"
            f'exec "{sys.executable}" "{py}" "$@"\n', encoding="utf-8")
        return py, shim

    shim = bin_dir / "orch"
    shim.write_text(
        "#!/bin/sh\n"
        f'exec "{sys.executable}" "{py}" "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    return py, shim


def verify(bin_dir: Path) -> tuple[bool, str]:
    """在**别的目录**里跑一次，确认真的与 cwd 无关。"""
    shim = bin_dir / ("orch.cmd" if os.name == "nt" else "orch")
    # 必须**按名字**跑一次：直接给全路径测不出 PATH / PATHEXT 有没有配好。
    if os.name == "nt":
        argv = ["cmd", "/c", "orch --help"]
    else:
        argv = ["sh", "-c", "orch --help"]
    try:
        r = subprocess.run(argv, capture_output=True, timeout=60, cwd=str(Path.home()))
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    out = (r.stdout or b"").decode("utf-8", "replace") + (r.stderr or b"").decode("utf-8", "replace")
    if r.returncode == 0 and "intake" in out:
        return True, shim.name
    return False, out.strip().splitlines()[-1][:200] if out.strip() else "无输出"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="装 orch：命令垫片 + Codex skill（换机器跑一次）")
    ap.add_argument("--dir", default=None, help="垫片放哪，默认 ~/bin")
    ap.add_argument("--check", action="store_true", help="只检查")
    ap.add_argument("--no-skill", action="store_true",
                    help="只装命令，不往 Codex 的 skills 目录放 orch")
    args = ap.parse_args(argv)

    bin_dir = Path(args.dir) if args.dir else default_bin_dir()

    if args.check:
        ok, detail = verify(bin_dir)
        print(f"{'✓' if ok else '✗'} 命令 {bin_dir} → {detail}")
        state, where = skill_status()
        note = {
            "same": "已装，与仓库一致",
            "missing": "没装——Codex 里不会出现 orch，只能在终端里敲命令",
            "differs": "已装但内容与仓库不一致（改过 skill 后没重装）",
            "nosource": "仓库里没有 skill/SKILL.md",
        }[state]
        print(f"{'✓' if state == 'same' else ('!' if state == 'differs' else '✗')} "
              f"skill → {where}\n     {note}")
        if not ok:
            print("跑 python scripts/install_cli.py 装一次")
        elif state != "same":
            print("跑 python scripts/install_cli.py 补一次，然后重启 Codex")
        return 0 if (ok and state == "same") else (2 if not ok else 3)

    py, shim = write_launcher(bin_dir)
    print(f"已写入 {py.name} / {shim.name}  →  {bin_dir}")
    if not on_path(bin_dir):
        print(f"! {bin_dir} 不在 PATH 里：把这条路径加进 PATH，否则 `orch` 仍然找不到")
        print("  （在「系统属性 → 环境变量」里追加一条即可；不要用 setx 拼 %PATH%，"
              "它会把超长 PATH 截断）")

    skill_ok = False
    if args.no_skill:
        print("- 跳过 skill（--no-skill）：只能从终端用，Codex 对话里不会触发")
    else:
        skill_ok, where = install_skill()
        print(f"{'✓' if skill_ok else '✗'} skill      →  {where}")
        if not skill_ok:
            print("  Codex 对话里不会出现 orch；可以用 --dir 之外的方式手动放，或检查权限")

    ok, detail = verify(bin_dir)
    if ok:
        print(f"✓ 在任意目录可用：orch --help（用 {shim}）")
        if skill_ok:
            print("  下一步：**重启 Codex**，然后在对话里说需求即可——不用开终端。")
        return 0
    print(f"✗ 装好了但跑不通：{detail}")
    print("  先确认 python 能导入：python -c \"import orch\"")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
