"""探针：Codex 在输入超过 context_window 时的行为。

判定四选一：
  OK        答对针（装得下，且注意力够）
  WRONG     答错/含糊（可能静默截断或注意力失效）
  HARDFAIL  显式报错（context_length_exceeded 之类）
  OTHER     其他异常

用法：python scripts/probe_ctx_limit.py 300000 550000 ...
目标值是"估算 token"，实际 token 以 codex 返回的 usage 为准。
"""
from __future__ import annotations

import json
import random
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.exp_lib import run_codex, codex_argv  # noqa: E402

OUT = ROOT / "runs" / "exp" / "ctx-probe"
NEEDLE = "ZQ-31415-92653"
# 10k 档实测：20,123 字符 → 37,910 输入 token（含约 1.48 万系统底座）
# ⇒ 正文约 0.87 字符/token（数字+空格分词密），比预想的更"贵"
CHARS_PER_TOKEN = 0.994
BASE_TOKENS = 14780


def filler(n_chars: int, rng: random.Random) -> str:
    digits = "0123456789"
    return "".join(rng.choice(digits) + " " for _ in range(n_chars // 2))


def build(target_tokens: int, rng: random.Random) -> str:
    body_chars = int(max(target_tokens - BASE_TOKENS, 1000) * CHARS_PER_TOKEN)
    q1 = body_chars // 4
    q3 = body_chars // 2
    head = "只做一件事：读完材料，然后回答问题。\n\n"
    tail = ("\n\n问题：材料里出现过的 PASSCODE 是什么？只输出那串字符本身，"
            "不要解释、不要加标点。")
    return (head + filler(q1, rng)
            + f"\n\nPASSCODE: {NEEDLE}\n\n"
            + filler(q3, rng)
            + f"\n\nPASSCODE: {NEEDLE}\n\n"
            + filler(q1, rng)
            + tail)


def classify(rc: int, err: str, msg: str, raw: str) -> str:
    low = (err + " " + raw[-4000:]).lower()
    hard = ("context_length_exceeded", "maximum context length", "too many tokens",
            "context window", "input is too long", "token limit")
    if any(k in low for k in hard) or rc not in (0,):
        return "HARDFAIL"
    if NEEDLE.lower() in (msg or "").lower():
        return "OK"
    if msg and msg.strip():
        return "WRONG"
    return "OTHER"


def one(target: int) -> dict:
    rng = random.Random(target)
    prompt = build(target, rng)
    work = Path(tempfile.mkdtemp(prefix=f"ctxprobe-{target}-"))
    OUT.mkdir(parents=True, exist_ok=True)
    rec = {"target_est": target, "chars": len(prompt),
           "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
    t0 = time.time()
    # 直接调，不用 run_codex：要留住原始事件流做证据
    import subprocess
    proc = subprocess.Popen(codex_argv("A", work, "bypass"), cwd=str(work),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.PIPE,
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        out_b, err_b = proc.communicate(prompt.encode("utf-8"), timeout=1500)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        out_b, err_b = b"", b"timeout"
        rc = -1
    raw = out_b.decode("utf-8", "replace")
    err = err_b.decode("utf-8", "replace")
    (OUT / f"{target}.jsonl").write_text(raw, encoding="utf-8")
    (OUT / f"{target}.err.txt").write_text(err, encoding="utf-8")

    msg, usage = "", {}
    for ln in raw.splitlines():
        try:
            e = json.loads(ln)
        except Exception:                       # noqa: BLE001
            continue
        if e.get("type") == "item.completed":
            it = e.get("item") or {}
            if it.get("type") == "agent_message":
                msg = it.get("text") or msg
        if e.get("type") == "turn.completed":
            usage = e.get("usage") or usage
        if e.get("type") in ("turn.failed", "error"):
            rec.setdefault("failure_events", []).append(str(e)[:400])

    rec.update({"rc": rc, "actual_input": usage.get("input_tokens"),
                "cached": usage.get("cached_input_tokens"),
                "output": usage.get("output_tokens"),
                "wall_s": round(time.time() - t0, 1),
                "verdict": classify(rc, err, msg, raw),
                "msg_head": (msg or "")[:200], "err_head": err[:400]})
    return rec


def main() -> None:
    targets = [int(x) for x in sys.argv[1:]] or [10000]
    OUT.mkdir(parents=True, exist_ok=True)
    for t in targets:
        rec = one(t)
        print(json.dumps(rec, ensure_ascii=False), flush=True)
        with open(OUT / "summary.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
