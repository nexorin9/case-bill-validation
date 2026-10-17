"""导出层（Task 11）。

包含两个核心模块：

- :class:`case_bill_validation.export.excel.ExcelExporter`
    按 193 项字段规范分区写入 ``billing_export.xlsx``：保留 4 个分区
    （基本信息 / 门诊慢特病诊疗 / 住院诊疗 / 医疗收费），每行含
    字段名 / 类型 / 是否必填 / 实际值 / 状态 / 原文引用 span /
    规则命中摘要；同时生成「原文引用」汇总 sheet；
- :class:`case_bill_validation.export.case_writeback.CaseSystemWriteback`
    HTTP POST 病案系统字段备注回写；4xx/5xx 重试 3 次（指数退避
    1s/2s/4s）+ 死信目录 ``logs/case_system_dead/``。

业务侧只调用 :func:`export_excel` 与 :class:`CaseSystemWriteback`
两个公开 API，凭据一律从环境变量读，**不**硬编码。
"""

from __future__ import annotations

from .case_writeback import CaseSystemWriteback, WritebackResult
from .excel import ExcelExporter, export_excel

__all__ = [
    "CaseSystemWriteback",
    "ExcelExporter",
    "WritebackResult",
    "export_excel",
]
