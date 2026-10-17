"""python -m case_bill_validation 入口（参考 Stanza python -m stanza 风格）。"""

from __future__ import annotations

import logging

import uvicorn

from case_bill_validation.config import get_settings
from case_bill_validation.server import create_app  # noqa: F401  延迟导入以避免循环


def main() -> None:
    """启动 uvicorn；端口与 host 来自环境变量。"""
    settings = get_settings()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    uvicorn.run(
        "case_bill_validation.server:create_app",
        host=settings.app_host,
        port=settings.app_port,
        factory=True,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()