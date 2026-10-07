"""领域错误类型。

所有可归因于调用方输入的问题抛出 :class:`ServiceError`，
由 FastAPI 统一转换为 422 响应；资源不存在用 404；
服务依赖（数据库）不可用时用 503。
"""

from __future__ import annotations


class ServiceError(Exception):
    """一次计算被整体拒绝。"""

    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def error(code: str, message: str) -> ServiceError:
    return ServiceError(code, message)
