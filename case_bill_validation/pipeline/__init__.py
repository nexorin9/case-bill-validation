"""链式 NLP 流水线（5 步处理器 + REQUIRES/PROVIDES 依赖图编排）。

模块清单（按主路径顺序）：

- :mod:`case_bill_validation.pipeline.core` —— Processor 抽象基类 + 编排器
  （Task 5 实装 BillingValidationPipeline）；
- :mod:`case_bill_validation.pipeline.segmenter` —— 段落/句子切分（Task 4）；
- :mod:`case_bill_validation.pipeline.extractor` —— 字段抽取（Task 6）；
- :mod:`case_bill_validation.pipeline.cross_validator` —— 跨字段校验（Task 7）；
- :mod:`case_bill_validation.pipeline.rule_checker` —— 必填/类型/ICD 编码（Task 7）；
- :mod:`case_bill_validation.pipeline.semantic_checker` —— LLM 语义核（Task 8）。

设计参考 ``github_ref/stanza/stanza/pipeline/core.py:176-513`` 与
``constituency_processor.py:108-115`` 的 REQUIRES/PROVIDES 注册模式。
"""
