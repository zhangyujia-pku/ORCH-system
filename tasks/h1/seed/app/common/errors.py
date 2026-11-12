"""领域错误：约定层次里唯一允许抛出的异常族。"""


class AppError(Exception):
    """所有领域异常的基类。"""


class ValidationError(AppError):
    """入参不合法。消息里必须带字段名。"""


class NotFound(AppError):
    """目标不存在。"""


class Conflict(AppError):
    """状态冲突（如重复创建、已删除再改）。"""
