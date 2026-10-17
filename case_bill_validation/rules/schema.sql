-- 病案结算清单核验系统 · 权威规则层 SQLite Schema
--
-- 三张表：
--   * billing_fields        —— 193 项结算清单字段（基本信息 31 + 门诊慢特病诊疗 6 +
--                              住院诊疗 58 + 医疗收费 98 = 193 项）
--   * violation_catalog     —— 条例第 38/40 条违规负面清单条目
--   * cross_field_rules     —— 跨字段时序/语义校验规则
--
-- 加载方式：migrations.init_billing_db() 解析 markdown 源后批量 INSERT；
-- 字段不变式：billing_fields COUNT == 193。

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS billing_fields (
    name              TEXT PRIMARY KEY,                 -- 字段名（业务可读，例 "入院日期"）
    section           TEXT NOT NULL,                    -- 分区名（基本信息/门诊慢特病/住院诊疗/医疗收费）
    field_type        TEXT NOT NULL,                    -- 类型标签：date / datetime / str / icd10 / icd9_cm3 / enum / int / float
    required          INTEGER NOT NULL DEFAULT 1,       -- 0=可选 / 1=必填（结算清单填写规范硬规则 3：所有项均为必填数据指标）
    value_constraint   TEXT,                            -- 取值约束（regex / 枚举 / 范围 / ICD 编码集）
    description       TEXT,                             -- 中文业务说明
    related_violation TEXT,                             -- 关联违规条目编号（条例 38/40 + 武汉盘点）
    example_value      TEXT                              -- 样例值（脱敏）
);

CREATE INDEX IF NOT EXISTS idx_billing_fields_section ON billing_fields(section);

CREATE TABLE IF NOT EXISTS violation_catalog (
    rule_id          TEXT PRIMARY KEY,                   -- 违规条目编号（条例 38.x / 40.x / WH.x）
    clause           TEXT NOT NULL,                      -- 条例归属：38 / 40 / WH-3.x / AUDIT-x
    severity         TEXT NOT NULL,                      -- 严重度：info / warn / error
    title            TEXT NOT NULL,                      -- 标题（"分解住院"、"挂床住院"等）
    description      TEXT NOT NULL,                      -- 详细描述（条例原文照抄级别）
    detection_method TEXT                                -- 检测方法提示（rule / llm / 行为模式）
);

CREATE INDEX IF NOT EXISTS idx_violation_catalog_severity ON violation_catalog(severity);
CREATE INDEX IF NOT EXISTS idx_violation_catalog_clause ON violation_catalog(clause);

CREATE TABLE IF NOT EXISTS cross_field_rules (
    name            TEXT PRIMARY KEY,                    -- 规则名（kebab-case）
    lhs_field       TEXT NOT NULL,                       -- 左操作数字段名（须在 billing_fields.name）
    rhs_field       TEXT NOT NULL,                       -- 右操作数字段名
    comparator      TEXT NOT NULL,                       -- 比较器：>=/<=/==/in_set/not_in_set/date_diff_equals/date_duration_equals/icd_chapter_consistent
    expected_value  TEXT,                                -- 比较期望值（in_set 列出合法值；date_diff_equals 标期望天数；date_duration_equals 用 FIELD:字段名 引用第三字段；icd_chapter_consistent 用 章节:前缀,前缀|...）
    error_message   TEXT NOT NULL,                       -- 不通过时的错误消息模板（含字段名占位）
    suggested_action TEXT,                               -- 建议修正动作
    severity        TEXT NOT NULL DEFAULT 'error'        -- 严重度
);

CREATE INDEX IF NOT EXISTS idx_cross_field_rules_lhs ON cross_field_rules(lhs_field);
CREATE INDEX IF NOT EXISTS idx_cross_field_rules_rhs ON cross_field_rules(rhs_field);