"""发货 的状态机。状态与动作的合法组合见下方注释。"""
from app.common.errors import Conflict

# 合法迁移：created -> processing -> done；任何状态都可以 cancelled
STATES = ("created", "processing", "done", "cancelled")
ACTIONS = ("start", "finish", "cancel")

TRANSITIONS = {
    ("created", "start"): "processing",
    ("processing", "finish"): "done",
    ("created", "cancel"): "cancelled",
    ("processing", "cancel"): "cancelled",
}


def next_state(current, action):
    """返回迁移后的状态；非法组合抛 Conflict，消息里带当前状态与动作。"""
    if action == "cancel" and current in ("created", "processing"):
        return "cancelled"
    key = (current, action)
    if key not in TRANSITIONS:
        raise Conflict(f"state: 不允许 {current} --{action}-->")
    return TRANSITIONS[key]
