"""发票 的状态机。状态与动作的合法组合见下方注释。"""

# 合法迁移：created -> processing -> done；任何状态都可以 cancelled
STATES = ("created", "processing", "done", "cancelled")
ACTIONS = ("start", "finish", "cancel")

TRANSITIONS = {}


def next_state(current, action):
    """返回迁移后的状态；非法组合抛 Conflict，消息里带当前状态与动作。"""
    raise NotImplementedError("next_state")
