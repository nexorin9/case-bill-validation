"""应用配置（环境变量驱动；pydantic-settings）。"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class CaseBillValidationSettings(BaseSettings):
    """所有运行时配置项；凭据一律从环境变量读，业务层零硬编码。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ----- 模型网关 -----
    model_gateway_base_url: str = Field(
        default="https://api.openai.com/v1",
        description="OpenAI-compatible 服务的 base URL；本地协议替身可指向自建 stub。",
    )
    model_gateway_api_key: str = Field(
        default="sk-stub",
        description="模型网关 API key；从环境变量读，业务层不持有。",
    )
    model_gateway_model: str = Field(
        default="gpt-4o-mini",
        description="默认模型名；调结构化输出时使用。",
    )
    model_gateway_timeout: int = Field(
        default=30,
        description="单次模型调用超时秒数。",
    )

    # ----- 病案系统回写 -----
    case_system_write_url: str = Field(
        default="https://case-system.example.internal/api/field-note",
        description="病案系统字段备注回写端点；HTTP POST。",
    )
    case_system_write_token: str = Field(
        default="replace-me",
        description="病案系统回写凭据；从不入仓库。",
    )

    # ----- HIS 出院小结拉取（备用入口） -----
    his_discharge_url: str = Field(
        default="https://his.example.internal/api/discharge",
        description="HIS 出院小结拉取端点；备用入口，未配置时不启用。",
    )
    his_discharge_token: str = Field(
        default="replace-me",
        description="HIS 拉取凭据。",
    )

    # ----- 审计与持久化 -----
    audit_log_dir: Path = Field(
        default=Path("./logs/audit"),
        description="审计日志目录；JSON Lines。",
    )
    billing_db_path: Path = Field(
        default=Path("./billing_fields.db"),
        description="SQLite 数据库路径；存 193 项字段规范与违规清单。",
    )
    export_dir: Path = Field(
        default=Path("./exports"),
        description="结算清单 Excel 落盘目录。",
    )

    # ----- 运行时 -----
    app_host: str = Field(default="127.0.0.1")
    app_port: int = Field(default=8000)


def get_settings() -> CaseBillValidationSettings:
    """依赖注入风格的 settings 工厂；测试可覆盖环境变量后再次调用。"""
    return CaseBillValidationSettings()