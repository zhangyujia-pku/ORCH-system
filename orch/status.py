"""状态查询使用同一次调用的事实；兼容旧事件，不从 session 猜跨阶段关系。"""

LABELS = {
    "planning": "规划中，尚未执行", "planned": "计划就绪，尚未执行",
    "running": "执行中", "blocked": "执行被阻断，尚未执行",
    "failed": "失败", "done": "执行成功",
    "cancelled": "已取消", "resource_limit": "资源上限已到",
    "waiting_input": "等待用户回答",
    "recovery_required": "需要人工检查残留进程",
}


def current_events(events: list[dict]) -> list[dict]:
    if not events:
        return []
    operation = events[-1].get("operation_id")
    if operation:
        return [e for e in events if e.get("operation_id") == operation]
    # 旧事件没有关联标识，只显示最近阶段，不能声称完整运行恢复。
    starts = [i for i, e in enumerate(events) if e.get("event") in
              {"run.start", "run.preflight", "plan.start", "operation.start"}]
    return events[starts[-1]:] if starts else events


def describe(events: list[dict]) -> str:
    for event in reversed(current_events(events)):
        name, data = event.get("event"), event.get("data", {})
        if name in {"operation.end", "run.end"}:
            state = data.get("state") or ("done" if data.get("ok") else "failed")
            label = LABELS.get(state, state)
            if state == "done":
                label += "（已通过固定整体验收）" if data.get("final_verified") else "（旧记录，整体验收证据未确认）"
            return label + (f"：{data['reason']}" if data.get("reason") else "")
        if name == "plan.fallback":
            return "规划未通过，未发现单 Agent 降级执行证据"
        if name == "plan.accepted":
            return LABELS["planned"]
        if name in {"task.start", "run.start"}:
            if name == "run.start" and "raw_text" in data:
                return "需求已存档，尚未执行"
            return LABELS["running"]
        if name == "plan.start":
            return LABELS["planning"]
        if name == "run.preflight":
            return "执行前检查中，尚未执行"
        if name == "operation.start":
            return "调用已开始，尚未执行"
    return "状态待确认（仅有事件记录）"
