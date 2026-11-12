"""对照组 A：在真实运行里顺带跑一次「Codex 单干」，把节省从估计变成实测。

默认开启（用户明确要求），但可以**持久关闭**，且能中止正在跑的对照，
不影响主任务 D。设计要点：

  1. 绝不移动 ~/.codex/skills/orch 或 ~/bin/orch*。改为给对照进程一个
     临时 CODEX_HOME（只放 auth.json + config.toml，不带 skills/）。
  2. off 写配置 + 停止哨兵；哨兵优先于环境变量，避免"显示关了却还在花钱"。
  3. 主循环轮询哨兵/配置，取消时终止整个子进程树，主任务继续。
  4. 配置损坏一律 fail-closed（当成关闭），不静默回落到默认开启去额外消费。
  5. 配置写入用临时文件 + os.replace，避免半截 JSON。

实验脚本用 ORCH_CONTROL_ARM=off 关掉，避免污染 D 的成本与时间。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HOME_CFG = Path.home() / ".orch"
CONFIG = HOME_CFG / "config.json"
STOP = HOME_CFG / "CONTROL_ARM_STOP"
REAL_CODEX_HOME = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


# ---------------------------------------------------------------- 开关
def _read_cfg() -> tuple[dict, str]:
    """返回 (配置, 来源标签)。损坏时返回 ({}, "config-corrupt")。"""
    try:
        raw = CONFIG.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("顶层不是对象")
        return data, "config"
    except FileNotFoundError:
        return {}, "default"
    except Exception:
        return {}, "config-corrupt"


def status() -> tuple[bool, str]:
    """(有效状态, 原因)。优先级：停止哨兵 > 环境变量 > 持久配置 > 默认开启。"""
    if STOP.exists():
        return False, "停止哨兵（control-arm off 已请求）"
    env = (os.environ.get("ORCH_CONTROL_ARM") or "").strip().lower()
    if env in {"off", "0", "false", "no"}:
        return False, "环境变量 ORCH_CONTROL_ARM=off"
    if env in {"on", "1", "true", "yes"}:
        return True, "环境变量 ORCH_CONTROL_ARM=on"
    data, src = _read_cfg()
    if src == "config-corrupt":
        return False, "配置文件损坏（fail-closed，不额外消费）"
    if src == "default":
        return True, "默认开启（无配置文件）"
    return bool(data.get("control_arm", True)), f"持久配置 {CONFIG}"


def enabled() -> bool:
    return status()[0]


def set_enabled(on: bool) -> None:
    HOME_CFG.mkdir(parents=True, exist_ok=True)
    data, _ = _read_cfg()
    data["control_arm"] = bool(on)
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, CONFIG)                       # 原子替换
    if on:
        STOP.unlink(missing_ok=True)
    else:
        STOP.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")


def stop_requested() -> bool:
    """运行中检查：哨兵存在，或配置/环境已变成 off。"""
    return STOP.exists() or not enabled()


# ---------------------------------------------------------------- 进程
def _kill_tree(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=15)
        else:
            import signal
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:
        try:
            os.kill(pid, 9)
        except Exception:
            pass


def _isolated_home(tmp: Path) -> Path:
    """临时 CODEX_HOME：只拷认证与配置，故意不带 skills/。"""
    home = tmp / "codex-home"
    home.mkdir(parents=True, exist_ok=True)
    for name in ("auth.json", "config.toml", "models_cache.json"):
        src = REAL_CODEX_HOME / name
        if src.is_file():
            shutil.copy2(src, home / name)
    return home


def run(repo: Path, goal: str, timeout: int = 900) -> dict:
    """在临时 clone + 临时 CODEX_HOME 里跑一次 A，返回用量。任何失败都只记 note。"""
    from .planner import parse_json_events, resolve_codex

    if not enabled():
        return {"usage": {}, "wall_s": 0, "exit_code": 0,
                "note": "对照组已关闭（未启动）"}

    repo = Path(repo).resolve()
    tmp = Path(tempfile.mkdtemp(prefix="orch-ctrl-"))
    work = tmp / "repo"
    t0 = time.time()
    try:
        try:
            r = subprocess.run(["git", "clone", "--quiet", "--no-hardlinks",
                                str(repo), str(work)],
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=120)
            if r.returncode != 0 or not work.is_dir():
                raise RuntimeError((r.stderr or "clone 失败")[-200:])
        except Exception as e:
            return {"usage": {}, "wall_s": 0.0, "exit_code": -1,
                    "note": f"对照组未能启动：{type(e).__name__}: {e}"}

        home = _isolated_home(tmp)
        env = dict(os.environ)
        env["CODEX_HOME"] = str(home)
        env.pop("ORCH_CONTROL_ARM", None)         # 对照本身不再触发对照

        argv = [resolve_codex(), "exec", "--json", "--skip-git-repo-check"]
        if os.name == "nt":
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            argv += ["--sandbox", "workspace-write"]

        p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, encoding="utf-8",
                             errors="replace", cwd=str(work), env=env)
        box: dict = {}

        def _communicate() -> None:
            try:
                box["out"], box["err"] = p.communicate(input=goal, timeout=timeout)
            except subprocess.TimeoutExpired:
                box["timeout"] = True
            except Exception as e:                 # noqa: BLE001
                box["error"] = str(e)

        th = threading.Thread(target=_communicate, daemon=True)
        th.start()
        cancelled = False
        while th.is_alive():
            if stop_requested():
                cancelled = True
                _kill_tree(p.pid)
                break
            th.join(0.5)
        th.join(timeout=3)

        _, usage = parse_json_events(box.get("out") or "")
        note = ""
        if cancelled:
            note = "对照组已被 control-arm off 中止（子进程树已终止）"
        elif box.get("timeout"):
            _kill_tree(p.pid)
            note = f"对照组超时（{timeout}s），子进程树已终止"
        elif box.get("error"):
            note = f"对照组读取失败：{box['error']}"
        elif p.returncode != 0:
            note = f"对照组退出码 {p.returncode}：" + \
                   " ".join((box.get("err") or "").split())[:160]
        # 即使失败，拿到的 usage 也如实返回，不丢已有用量
        return {"usage": usage or {}, "wall_s": round(time.time() - t0, 1),
                "exit_code": -3 if cancelled else (p.returncode or 0),
                "note": note, "cancelled": cancelled}
    except Exception as e:                       # noqa: BLE001
        return {"usage": {}, "wall_s": round(time.time() - t0, 1), "exit_code": -1,
                "note": f"对照组异常：{type(e).__name__}: {e}"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    """`orch control-arm [on|off|status]`。"""
    args = list(argv or sys.argv[1:])
    action = (args[0] if args else "status").lower()
    if action in {"on", "off"}:
        set_enabled(action == "on")
        on, why = status()
        print(f"对照组：{'开启' if on else '关闭'}（{why}）")
        if action == "off":
            print("正在运行的对照组会在数秒内被中止；主任务 D 不受影响。")
        return 0
    if action == "status":
        on, why = status()
        print(f"对照组：{'开启（试验模式）' if on else '关闭'}  来源：{why}")
        print(f"配置文件：{CONFIG}")
        return 0
    print("用法：orch control-arm [on|off|status]")
    return 2
