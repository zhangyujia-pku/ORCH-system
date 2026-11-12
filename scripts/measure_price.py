#!/usr/bin/env python3
"""
实测 DeepSeek 两个模型的输入单价（元 / 百万 token）。

为什么不查官网价：文档站 302 跳转、平台页 429 限流，抓不到可信的价目表。
而且"标价"和"这个账号实际扣的价"未必一样（缓存、活动、阶梯都可能有）。

为什么用余额差：`usage` 只给 token 数不给钱，`/user/balance` 是唯一权威的扣费来源。

★ 精度限制：余额精确到 0.01 元。所以单次调用必须花到 0.1 元以上，
  否则 0.01 的粒度本身就吃掉大半信号。这就是要用大 prompt 的原因，
  不是为了"测得多"，是为了"信号大于噪声"。

★ 缓存：DeepSeek 有前缀缓存，第二次起相同的长前缀会便宜很多。
  所以每个模型先跑一次预热（不计数），再测稳态。
  冷启动价和稳态价差距可以有数倍，报哪个必须说清楚。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

BASE = "https://api.deepseek.com"
KEY = os.environ.get("DEEPSEEK_KEY") or ""
MODELS = ["deepseek-flash", "deepseek-v4-pro"]


def _req(path: str, body: dict | None = None, timeout: int = 600) -> dict:
    data = json.dumps(body).encode() if body else None
    r = urllib.request.Request(BASE + path, data=data,
                               headers={"Authorization": f"Bearer {KEY}",
                                        "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.load(resp)


def balance() -> float:
    d = _req("/user/balance")
    return float(d["balance_infos"][0]["total_balance"])


def big_prompt(n_chars: int) -> str:
    """拼一段足够长的中文。内容无所谓，要的是 token 量。"""
    unit = ("这是一个用于测量 token 计价的填充段落。它不承载业务含义，"
            "只是为了让单次调用的输入规模足够大，从而让余额的 0.01 元粒度可以忽略。"
            "反复出现是为了凑长度。")
    return unit * (n_chars // len(unit) + 1)


def chat(model: str, prompt: str, max_tokens: int = 16) -> tuple[dict, float]:
    t0 = time.time()
    d = _req("/chat/completions", {
        "model": model, "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens, "stream": False,
    })
    return d, time.time() - t0


SETTLE = 130   # 余额批量结算的延迟；实测 1~2 分钟


def measure(model: str, prompt: str, hot_n: int = 5) -> dict:
    """先测冷启动（全 miss），再测稳态（前缀命中）。

    余额是延迟批量入账的：调完立刻读会看到 0，必须等结算。
    """
    # --- 冷启动：第一次调用，前缀没有缓存 ---
    b0 = balance()
    d0, dt0 = chat(model, prompt)
    time.sleep(SETTLE)
    b1 = balance()
    cold_cost = round(b0 - b1, 2)
    u0 = d0.get("usage", {})

    # --- 稳态：同前缀再跑 hot_n 次，前缀应当命中 ---
    t0 = time.time()
    hits = 0
    for _ in range(hot_n):
        d, _ = chat(model, prompt)
        hits += (d.get("usage", {}).get("prompt_cache_hit_tokens") or 0)
    hot_sec = (time.time() - t0) / hot_n
    time.sleep(SETTLE)
    b2 = balance()
    hot_total = round(b1 - b2, 2)

    pin = u0.get("prompt_tokens", 0)
    return {
        "model": model,
        "in": pin,
        "out": u0.get("completion_tokens", 0),
        "cold_cost": cold_cost,
        "cold_price": round(cold_cost / pin * 1_000_000, 3) if pin else None,
        "hot_cost_each": round(hot_total / hot_n, 3) if hot_total else 0.0,
        "hot_price": round(hot_total / hot_n / pin * 1_000_000, 3) if pin and hot_total else None,
        "cache_hit_ratio": round(hits / (pin * hot_n), 2) if pin else None,
        "sec_cold": round(dt0, 1), "sec_hot": round(hot_sec, 1),
    }


def main() -> int:
    if not KEY:
        print("缺 DEEPSEEK_KEY", file=sys.stderr)
        return 3
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    prompt = big_prompt(200_000)

    b_start = balance()
    print(f"起始余额 {b_start:.2f} 元；每模型预热 1 次 + 计数 {n} 次\n")

    for m in MODELS:
        r = measure(m, prompt, hot_n=n)
        print(f"  {m:<18} 输入 {r['in']:>7,}  "
              f"冷 {r['cold_cost']:>5.2f}元/{r['cold_price']}元每M  "
              f"热 {r['hot_cost_each']:>5.2f}元/{r['hot_price']}元每M  "
              f"命中率 {r['cache_hit_ratio']}  {r['sec_cold']}s/{r['sec_hot']}s")
        rows.append(r)

    print(f"\n总花费 {b_start - balance():.2f} 元")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
