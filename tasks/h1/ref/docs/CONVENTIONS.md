# 分层约定（唯一出处）

写任何领域代码之前先读这里，其他地方不再重复。

1. **models** 只负责结构与校验：
   - 提供 `to_dict()` / `from_dict(d)`；
   - 提供 `validate()`，非法时抛 `common.errors.ValidationError`，**消息必须带字段名**；
   - 不在 models 里访问存储。

2. **store** 只负责持久化（内存）：
   - 提供 `create/get/list/update/delete`；
   - `list` 必须用 `common.pagination.paginate`，返回 `(rows, meta)`；
   - `delete` 是**软删除**：置 `deleted_at`，之后 `get` 抛 `NotFound`，`list` 不再返回它。

3. **service** 只负责编排：
   - 顺序固定：校验 → 存储 → 事件；
   - 事件写到 `self.events`（列表），形如 `{"type": "...", "id": "...", "at": now_iso()}`；
   - 时间一律用 `common.clock.now_iso()`，不要用 datetime.now()。

4. **异常**一律用 `common.errors` 里的类型，不要抛裸 `ValueError`。
