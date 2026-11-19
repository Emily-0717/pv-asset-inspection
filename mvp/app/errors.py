# -*- coding: utf-8 -*-
"""集中定义错误码与业务异常，禁止在业务代码里散落字符串。

为什么集中：错误码要能被前端、测试和审计同时引用，散落字符串必然出现
"同一含义两个码"，回放与告警就对不上。
"""
from __future__ import annotations


class ErrorCode:
    """全部对外错误码；新增码必须同时补 HTTP 状态与测试。"""

    AUTH_MISSING = "AUTH_MISSING"              # 未带凭据
    AUTH_INVALID = "AUTH_INVALID"              # 凭据无效/已停用
    ROLE_FORBIDDEN = "ROLE_FORBIDDEN"          # 角色无该能力
    TENANT_FORBIDDEN = "TENANT_FORBIDDEN"      # 跨租户访问
    VALIDATION_FAILED = "VALIDATION_FAILED"    # 入参不合法
    NOT_FOUND = "NOT_FOUND"                    # 资源不存在
    CONFLICT = "CONFLICT"                      # 唯一键/状态冲突
    CONFIG_INVALID = "CONFIG_INVALID"          # 配置缺失或非法
    SYNTHETIC_MIX_FORBIDDEN = "SYNTHETIC_MIX_FORBIDDEN"  # 合成/真实混存
    MODEL_DISABLED = "MODEL_DISABLED"          # 未配置模型，能力关闭
    MODEL_TIMEOUT = "MODEL_TIMEOUT"            # 模型超时
    MODEL_UPSTREAM = "MODEL_UPSTREAM"          # 模型侧非 2xx
    MODEL_INVALID_OUTPUT = "MODEL_INVALID_OUTPUT"  # 返回不合规
    EVIDENCE_INSUFFICIENT = "EVIDENCE_INSUFFICIENT"  # 无证据，不出置信度
    INTERNAL_ERROR = "INTERNAL_ERROR"          # 兜底


class AppError(Exception):
    """带错误码与 HTTP 状态的业务异常；由全局处理器转成统一信封。

    入参：
        code: ErrorCode 中的常量
        status: HTTP 状态码
        msg: 面向人的说明（不含堆栈、不含敏感数据）
    出参：无（作为异常抛出）
    失败行为：不吞异常，交由 main.py 的处理器统一序列化。
    """

    def __init__(self, code: str, status: int, msg: str) -> None:
        super().__init__(msg)
        self.code = code
        self.status = status
        self.msg = msg


def not_found(what: str) -> AppError:
    """构造 404；统一文案，避免各路由各写一句。"""
    return AppError(ErrorCode.NOT_FOUND, 404, f"{what}不存在")


def forbidden(msg: str = "当前角色无权执行该操作") -> AppError:
    """构造 403；权限判断只走这里，保证审计里原因一致。"""
    return AppError(ErrorCode.ROLE_FORBIDDEN, 403, msg)
