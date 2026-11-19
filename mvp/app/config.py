# -*- coding: utf-8 -*-
"""配置加载与校验：阈值、地址、超时全部来自 config.yaml，代码不写死。"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .errors import AppError, ErrorCode

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")  # 只认大写环境变量，避免误伤正文


class AppCfg(BaseModel):
    """应用级开关。"""

    model_config = ConfigDict(extra="forbid")

    env: str = "dev"
    synthetic_only: bool = True
    timezone: str = "Asia/Shanghai"
    audit_retention_days: int = Field(default=180, ge=180)  # 红线：不得低于 180 天

    @field_validator("audit_retention_days")
    @classmethod
    def _check_retention(cls, v: int) -> int:
        """留存低于 180 天直接拒绝启动，避免"配置一改就违规"。"""
        if v < 180:
            raise ValueError("audit_retention_days 不得小于 180")
        return v


class ServerCfg(BaseModel):
    """监听地址；MVP 默认只绑本机。"""

    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = 8010


class StorageCfg(BaseModel):
    """SQLite 位置与 SQL 脚本目录。"""

    model_config = ConfigDict(extra="forbid")

    db_path: str = "./data/mvp.db"
    sql_dir: str = "./sql"


class ModelCfg(BaseModel):
    """OpenAI 兼容模型配置；缺少 key 时整体降级而不是报错退出。"""

    model_config = ConfigDict(extra="forbid")

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    embedding_model: str = ""
    timeout_seconds: float = 20.0
    max_retries: int = 2
    temperature: float = 0.0

    @property
    def chat_enabled(self) -> bool:
        """三项齐全才认为可用；缺一项都视为关闭，避免半配置状态。"""
        return bool(self.base_url and self.api_key and self.model)

    @property
    def embedding_enabled(self) -> bool:
        """向量检索需要单独的 embedding 模型名。"""
        return bool(self.base_url and self.api_key and self.embedding_model)


class RagCfg(BaseModel):
    """检索参数：分块、召回量、检索模式。"""

    model_config = ConfigDict(extra="forbid")

    chunk_size: int = Field(default=380, ge=80, le=2000)
    chunk_overlap: int = Field(default=60, ge=0, le=400)
    top_k: int = Field(default=5, ge=1, le=20)
    mode: str = "hybrid"  # fts / vector / hybrid
    max_scan: int = Field(default=500, ge=10, le=5000)  # 向量暴力扫描上限


class HarnessCfg(BaseModel):
    """Agent 循环的预算与超时。"""

    model_config = ConfigDict(extra="forbid")

    max_steps: int = Field(default=4, ge=1, le=8)
    tool_timeout_seconds: float = Field(default=8.0, gt=0)


class Settings(BaseModel):
    """配置树根节点；source_path 记录来源，便于 /health 回显。"""

    model_config = ConfigDict(extra="forbid")

    app: AppCfg = AppCfg()
    server: ServerCfg = ServerCfg()
    storage: StorageCfg = StorageCfg()
    model: ModelCfg = ModelCfg()
    rag: RagCfg = RagCfg()
    harness: HarnessCfg = HarnessCfg()
    source_path: str = ""

    @property
    def threshold_version(self) -> str:
        """阈值版本当前取配置文件修改时间，配置一改版本自动变，避免"改了没记录"。"""
        try:
            return str(int(Path(self.source_path).stat().st_mtime)) if self.source_path else "0"
        except OSError:
            return "0"


def _expand(value: Any) -> Any:
    """递归展开 ${VAR}；未设置的变量保留占位符，交给后续校验暴露。"""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), m.group(0)), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def load_settings(path: str | None = None) -> Settings:
    """读取并校验 config.yaml。

    入参：
        path: 配置文件路径；为空时读环境变量 SOLAR_MVP_CONFIG，再退回 ./config.yaml
    出参：Settings 实例，相对路径已解析为绝对路径
    失败行为：文件缺失或字段非法抛 AppError(CONFIG_INVALID)，由启动流程直接退出。
    """
    raw_path = path or os.environ.get("SOLAR_MVP_CONFIG", "config.yaml")
    cfg_path = Path(raw_path).expanduser().resolve()
    if not cfg_path.exists():
        raise AppError(ErrorCode.CONFIG_INVALID, 500, f"配置文件不存在: {cfg_path}")

    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:  # YAML 语法错误必须给出可定位信息
        raise AppError(ErrorCode.CONFIG_INVALID, 500, f"YAML 解析失败: {exc}") from exc

    data = _expand(data)
    try:
        settings = Settings(**data)
    except Exception as exc:  # Pydantic 的校验信息保留，便于定位是哪个键
        raise AppError(ErrorCode.CONFIG_INVALID, 500, f"配置校验失败: {exc}") from exc

    # 相对路径按配置文件所在目录解析，避免"从别的目录启动就找不到库"
    base = cfg_path.parent
    if not Path(settings.storage.db_path).is_absolute():
        settings.storage.db_path = str((base / settings.storage.db_path).resolve())
    if not Path(settings.storage.sql_dir).is_absolute():
        settings.storage.sql_dir = str((base / settings.storage.sql_dir).resolve())
    settings.source_path = str(cfg_path)
    return settings
