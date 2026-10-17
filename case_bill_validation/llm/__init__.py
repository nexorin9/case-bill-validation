"""LLM 网关与 prompt 模板。

- :mod:`case_bill_validation.llm.gateway` —— ``ModelGateway``（OpenAI-compatible）；
- :mod:`case_bill_validation.llm.prompts` —— 字段抽取 / 语义核 prompt 模板；
- :mod:`case_bill_validation.llm.schemas` —— JSON Schema 常量 + jsonschema
  严格校验入口（Task 21）。
"""

from case_bill_validation.llm.gateway import (
    GatewayError,
    LLMCallMetadata,
    ModelGateway,
    StubModelGateway,
)
from case_bill_validation.llm.schemas import (
    EXTRACTION_SCHEMA,
    SEMANTIC_CHECK_SCHEMA,
    SCHEMAS,
    get_schema,
    get_validator,
    is_valid_payload,
    iter_errors,
    schema_names,
    validate_payload,
)

__all__ = [
    # gateway
    "ModelGateway",
    "StubModelGateway",
    "GatewayError",
    "LLMCallMetadata",
    # schemas
    "SEMANTIC_CHECK_SCHEMA",
    "EXTRACTION_SCHEMA",
    "SCHEMAS",
    "get_schema",
    "get_validator",
    "validate_payload",
    "iter_errors",
    "is_valid_payload",
    "schema_names",
]
