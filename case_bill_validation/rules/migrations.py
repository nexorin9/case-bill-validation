"""权威规则层迁移脚本。

启动时调用 :func:`init_billing_db` 完成三张表的清空与批量插入。
数据基于 ``domain/dictionaries/医保结算清单字段规范.md`` 与
``domain/rules/医保违规负面清单.md`` 手工结构化（域文档是分区结构与关键字段
级别，193 项明细在国家医保局官方 PDF 中）。

字段不变式：``billing_fields`` 表记录数 == **193**（31 基本信息 +
6 门诊慢特病 + 58 住院诊疗 + 98 医疗收费）；任何不符都视为
权威源失同步，启动时抛 :class:`case_bill_validation.errors.ConfigurationError`。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


class ConfigurationError(RuntimeError):
    """权威规则层失同步或 SQLite 启动失败。"""

# ---------------------------------------------------------------------------
# 字段类型枚举（与 schema.sql.field_type 对齐）
# ---------------------------------------------------------------------------

SECTION_BASIC = "基本信息"            # 31 项
SECTION_OP_SPECIAL = "门诊慢特病诊疗"  # 6 项
SECTION_INPATIENT = "住院诊疗"        # 58 项
SECTION_BILLING = "医疗收费"          # 98 项

EXPECTED_SECTION_COUNTS = {
    SECTION_BASIC: 31,
    SECTION_OP_SPECIAL: 6,
    SECTION_INPATIENT: 58,
    SECTION_BILLING: 98,
}
# 31 + 6 + 58 + 98 = 193
EXPECTED_TOTAL_FIELDS = sum(EXPECTED_SECTION_COUNTS.values())


# ---------------------------------------------------------------------------
# BillingField 数据类（迁移期生成 dict 后 INSERT）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldSpec:
    """迁移期使用的字段规范元组。"""

    name: str
    section: str
    field_type: str
    required: bool
    value_constraint: str
    description: str
    related_violation: str
    example_value: str


@dataclass(frozen=True)
class ViolationSpec:
    rule_id: str
    clause: str
    severity: str
    title: str
    description: str
    detection_method: str


@dataclass(frozen=True)
class CrossRuleSpec:
    name: str
    lhs_field: str
    rhs_field: str
    comparator: str
    expected_value: str
    error_message: str
    suggested_action: str
    severity: str


# ---------------------------------------------------------------------------
# 193 项字段定义
# ---------------------------------------------------------------------------
#
# 中文业务名 → 字段类型 → 约束 → 关联违规条目（条例 38/40 条 + 武汉盘点）。
# 命名遵循"业务域 + 字段名"惯例（snake_case），与 case-bill 的报告列对齐。

_BASIC_FIELDS: tuple[FieldSpec, ...] = (
    # ---- 基本信息 31 项 ----
    FieldSpec("清单流水号", SECTION_BASIC, "str", True, "^[A-Z0-9]{12,20}$", "清单唯一流水号（机构内部生成 + 医保端校验）", "WH-3.4", "BD2026123456A"),
    FieldSpec("定点医疗机构名称", SECTION_BASIC, "str", True, ".+", "结算医院名称（须与执业许可一致）", "", "<EXAMPLE_HOSPITAL>"),
    FieldSpec("定点医疗机构代码", SECTION_BASIC, "str", True, "^[A-Z0-9]{10,18}$", "医院医保结算代码（HXYBM）", "", "H0000000000001X"),
    FieldSpec("医保结算等级", SECTION_BASIC, "enum", True, "in:一级,二级,三级,未定级", "医院结算等级", "", "三级"),
    FieldSpec("医保类型", SECTION_BASIC, "enum", True, "in:职工,居民,新农合,离休,其他", "参保类型", "", "职工"),
    FieldSpec("姓名", SECTION_BASIC, "str", True, "^[\\u4e00-\\u9fa5·]{2,20}$", "患者姓名", "", "<EXAMPLE_NAME>"),
    FieldSpec("性别", SECTION_BASIC, "enum", True, "in:男,女,未说明", "生理性别（按 GB/T 2261.1）", "", "男"),
    FieldSpec("出生日期", SECTION_BASIC, "date", True, "^\\d{4}-\\d{2}-\\d{2}$", "出生日期（YYYY-MM-DD）", "", "<EXAMPLE_DOB>"),
    FieldSpec("年龄", SECTION_BASIC, "int", True, "range:0,150", "周岁；不足 1 周岁按天", "", "65"),
    FieldSpec("民族", SECTION_BASIC, "enum", True, "in:汉族,少数民族", "民族", "", "汉族"),
    FieldSpec("证件类别", SECTION_BASIC, "enum", True, "in:身份证,护照,军官证,港澳居民居住证,台湾居民居住证,其他", "有效身份证件类别", "", "身份证"),
    FieldSpec("证件号码", SECTION_BASIC, "str", True, ".+", "证件号码（身份证须 GB 11643 校验）", "WH-3.6", "<EXAMPLE_ID>"),
    FieldSpec("职业", SECTION_BASIC, "enum", False, "in:公务员,职员,工人,农民,学生,自由职业者,无业,其他", "职业分类（按 GB/T 6565）", "", "职员"),
    FieldSpec("住址", SECTION_BASIC, "str", True, ".+", "户籍/居住地址", "", "<EXAMPLE_ADDRESS>"),
    FieldSpec("联系人姓名", SECTION_BASIC, "str", False, "^[\\u4e00-\\u9fa5·]{2,20}$", "紧急联系人姓名", "", "<EXAMPLE_CONTACT>"),
    FieldSpec("联系人关系", SECTION_BASIC, "enum", False, "in:配偶,父母,子女,兄弟姐妹,其他亲属,同事,朋友,其他", "联系人与患者关系", "", "配偶"),
    FieldSpec("联系人电话", SECTION_BASIC, "str", False, "^[\\d\\-\\+]{7,20}$", "联系人电话", "", "<EXAMPLE_PHONE>"),
    FieldSpec("参保地", SECTION_BASIC, "str", True, ".+", "统筹区（省-市-县）", "", "<EXAMPLE_INSURE_AREA>"),
    FieldSpec("参保单位", SECTION_BASIC, "str", False, ".+", "参保单位名称", "", "<EXAMPLE_EMPLOYER>"),
    FieldSpec("特殊人员类型", SECTION_BASIC, "enum", False, "in:普通人员,低保,特困,优抚,建档立卡,其他", "特殊人群身份标识", "WH-3.1", "普通人员"),
    FieldSpec("新生儿入院类型", SECTION_BASIC, "enum", False, "in:普通新生儿,高危新生儿,产科母婴同室,其他", "新生儿专用字段（可选）", "", "普通新生儿"),
    FieldSpec("新生儿出生体重", SECTION_BASIC, "float", False, "range:0.0,8.0", "单位 kg（新生儿专用）", "", "3.20"),
    FieldSpec("新生儿入院体重", SECTION_BASIC, "float", False, "range:0.0,8.0", "单位 kg", "", "3.18"),
    FieldSpec("母亲姓名", SECTION_BASIC, "str", False, "^[\\u4e00-\\u9fa5·]{2,20}$", "母亲姓名（新生儿专用）", "", "<EXAMPLE_MOTHER>"),
    FieldSpec("母亲证件号码", SECTION_BASIC, "str", False, ".+", "母亲身份证号（新生儿专用）", "", "<EXAMPLE_MOTHER_ID>"),
    FieldSpec("入院途径", SECTION_BASIC, "enum", False, "in:1急诊,2门诊,3其他机构转入,9其他", "入院途径（医保版规则清单：1/2/3/9）", "", "2门诊"),
    FieldSpec("就诊机构代码", SECTION_BASIC, "str", False, "^[A-Z0-9]{10,18}$", "入院前就诊机构代码（转诊用）", "", "H0000000000002X"),
    FieldSpec("就医登记号", SECTION_BASIC, "str", False, ".+", "参保地就医登记备案号", "", "<EXAMPLE_REG_NO>"),
    FieldSpec("医疗救助对象", SECTION_BASIC, "enum", False, "in:否,是", "是否医疗救助对象", "", "否"),
    FieldSpec("民政救助类别", SECTION_BASIC, "enum", False, "in:否,低保,特困,其他", "民政救助类别（医疗救助对象=是时必填）", "", "否"),
    FieldSpec("备注", SECTION_BASIC, "str", False, ".{0,200}", "备注", "", ""),
)


_OP_SPECIAL_FIELDS: tuple[FieldSpec, ...] = (
    # ---- 门诊慢特病诊疗 6 项 ----
    FieldSpec("门诊慢特病诊断科别", SECTION_OP_SPECIAL, "str", False, ".+", "门诊慢特病诊断科室", "", "心血管内科"),
    FieldSpec("门诊慢特病就诊日期", SECTION_OP_SPECIAL, "date", False, "^\\d{4}-\\d{2}-\\d{2}$", "就诊日期", "", "<EXAMPLE_DATE>"),
    FieldSpec("门诊慢特病诊断名称", SECTION_OP_SPECIAL, "str", False, ".+", "诊断名称（与 ICD-10 配套）", "", "高血压"),
    FieldSpec("门诊慢特病诊断代码", SECTION_OP_SPECIAL, "icd10", False, "^[A-Z][0-9]{2}(\\.[0-9]{1,2})?$", "ICD-10 编码（医保版）", "", "I10"),
    FieldSpec("门诊慢特病手术操作名称", SECTION_OP_SPECIAL, "str", False, ".+", "手术操作名称", "", ""),
    FieldSpec("门诊慢特病手术操作代码", SECTION_OP_SPECIAL, "icd9_cm3", False, "^[0-9]{2}(\\.[0-9]{1,2})?$", "ICD-9-CM-3 编码（医保版）", "", ""),
)


_INPATIENT_FIELDS: tuple[FieldSpec, ...] = (
    # ---- 住院诊疗信息 58 项 ----
    # 入院基本信息
    FieldSpec("住院医疗类型", SECTION_INPATIENT, "enum", True, "in:1住院,2日间手术", "住院 / 日间手术", "", "1住院"),
    FieldSpec("治疗类别", SECTION_INPATIENT, "enum", True, "in:1西医,2中医,3中西医", "治疗类别", "", "1西医"),
    FieldSpec("入院科别", SECTION_INPATIENT, "str", True, ".+", "入院时所在科室", "", "心血管内科"),
    FieldSpec("转科科别", SECTION_INPATIENT, "str", False, ".+", "转科后科室（无转科为空）", "", ""),
    FieldSpec("入院日期", SECTION_INPATIENT, "date", True, "^\\d{4}-\\d{2}-\\d{2}$", "入院日期（YYYY-MM-DD）", "", "<EXAMPLE_ADMISSION>"),
    FieldSpec("入院时间", SECTION_INPATIENT, "datetime", True, "^\\d{2}:\\d{2}(:\\d{2})?$", "入院时间（HH:MM 或 HH:MM:SS）", "", "08:30"),
    FieldSpec("出院日期", SECTION_INPATIENT, "date", True, "^\\d{4}-\\d{2}-\\d{2}$", "出院日期", "", "<EXAMPLE_DISCHARGE>"),
    FieldSpec("出院时间", SECTION_INPATIENT, "datetime", True, "^\\d{2}:\\d{2}(:\\d{2})?$", "出院时间", "", "15:00"),
    FieldSpec("实际住院天数", SECTION_INPATIENT, "int", True, "range:0,365", "实际住院天数（按医保口径：入院当日算一天，出院当日不算；除非当天出入院）", "WH-3.3", "7"),
    FieldSpec("出院科别", SECTION_INPATIENT, "str", True, ".+", "出院时所在科室", "", "心血管内科"),
    # 诊断
    FieldSpec("门急诊诊断西医", SECTION_INPATIENT, "str", False, ".+", "门（急）诊西医诊断", "", "高血压"),
    FieldSpec("门急诊诊断代码", SECTION_INPATIENT, "icd10", False, "^[A-Z][0-9]{2}(\\.[0-9]{1,2})?$", "门急诊诊断 ICD-10 编码", "", "I10"),
    FieldSpec("主要诊断名称", SECTION_INPATIENT, "str", True, ".+", "出院主要诊断（依据住院诊疗过程；与首页可不同）", "", "冠心病"),
    FieldSpec("主要诊断代码", SECTION_INPATIENT, "icd10", True, "^[A-Z][0-9]{2}(\\.[0-9]{1,2})?$", "主要诊断 ICD-10（医保版）；有且只有 1 个", "38.7;WH-3.5", "I25.101"),
    FieldSpec("主要诊断入院病情", SECTION_INPATIENT, "enum", True, "in:1有,2临床未确定,3情况不明,4无", "主要诊断入院病情（GB 4 类）", "", "1有"),
    FieldSpec("其他诊断1名称", SECTION_INPATIENT, "str", False, ".+", "其他诊断 1", "", "高血压"),
    FieldSpec("其他诊断1代码", SECTION_INPATIENT, "icd10", False, "^[A-Z][0-9]{2}(\\.[0-9]{1,2})?$", "其他诊断 1 ICD-10", "", "I10"),
    FieldSpec("其他诊断1入院病情", SECTION_INPATIENT, "enum", False, "in:1有,2临床未确定,3情况不明,4无", "其他诊断 1 入院病情", "", "1有"),
    FieldSpec("其他诊断2名称", SECTION_INPATIENT, "str", False, ".+", "其他诊断 2", "", ""),
    FieldSpec("其他诊断2代码", SECTION_INPATIENT, "icd10", False, "^[A-Z][0-9]{2}(\\.[0-9]{1,2})?$", "其他诊断 2 ICD-10", "", ""),
    FieldSpec("其他诊断2入院病情", SECTION_INPATIENT, "enum", False, "in:1有,2临床未确定,3情况不明,4无", "其他诊断 2 入院病情", "", ""),
    FieldSpec("其他诊断3名称", SECTION_INPATIENT, "str", False, ".+", "其他诊断 3", "", ""),
    FieldSpec("其他诊断3代码", SECTION_INPATIENT, "icd10", False, "^[A-Z][0-9]{2}(\\.[0-9]{1,2})?$", "其他诊断 3 ICD-10", "", ""),
    FieldSpec("其他诊断3入院病情", SECTION_INPATIENT, "enum", False, "in:1有,2临床未确定,3情况不明,4无", "其他诊断 3 入院病情", "", ""),
    FieldSpec("诊断代码计数", SECTION_INPATIENT, "int", False, "range:0,40", "出院诊断代码总数", "", "3"),
    # 手术及操作
    FieldSpec("主要手术操作名称", SECTION_INPATIENT, "str", False, ".+", "主要手术操作（无则空；不可与主诊断矛盾）", "", "冠状动脉支架植入术"),
    FieldSpec("主要手术操作代码", SECTION_INPATIENT, "icd9_cm3", False, "^[0-9]{2}(\\.[0-9]{1,2})?$", "主要手术操作 ICD-9-CM-3（医保版）", "38.7", "36.06"),
    FieldSpec("主要手术操作日期", SECTION_INPATIENT, "date", False, "^\\d{4}-\\d{2}-\\d{2}$", "主要手术日期（≥ 入院日期；倒置即违规）", "38.7", "<EXAMPLE_SURGERY>"),
    FieldSpec("主要手术麻醉方式", SECTION_INPATIENT, "enum", False, "in:全身麻醉,椎管内麻醉,局部浸润麻醉,神经阻滞麻醉,复合麻醉,其他", "主要手术麻醉方式", "", "局部浸润麻醉"),
    FieldSpec("主要手术术者", SECTION_INPATIENT, "str", False, "^[\\u4e00-\\u9fa5·]{2,20}$", "主要手术术者姓名", "", "<EXAMPLE_SURGEON>"),
    FieldSpec("主要手术术者代码", SECTION_INPATIENT, "str", False, ".+", "主要手术术者执业证代码", "", ""),
    FieldSpec("主要手术麻醉医师", SECTION_INPATIENT, "str", False, "^[\\u4e00-\\u9fa5·]{2,20}$", "麻醉医师姓名", "", "<EXAMPLE_ANESTHESIA>"),
    FieldSpec("主要手术麻醉医师代码", SECTION_INPATIENT, "str", False, ".+", "麻醉医师执业证代码", "", ""),
    FieldSpec("其他手术1名称", SECTION_INPATIENT, "str", False, ".+", "其他手术 1", "", ""),
    FieldSpec("其他手术1代码", SECTION_INPATIENT, "icd9_cm3", False, "^[0-9]{2}(\\.[0-9]{1,2})?$", "其他手术 1 ICD-9-CM-3", "", ""),
    FieldSpec("其他手术1日期", SECTION_INPATIENT, "date", False, "^\\d{4}-\\d{2}-\\d{2}$", "其他手术 1 日期", "", ""),
    FieldSpec("其他手术2名称", SECTION_INPATIENT, "str", False, ".+", "其他手术 2", "", ""),
    FieldSpec("其他手术2代码", SECTION_INPATIENT, "icd9_cm3", False, "^[0-9]{2}(\\.[0-9]{1,2})?$", "其他手术 2 ICD-9-CM-3", "", ""),
    FieldSpec("其他手术2日期", SECTION_INPATIENT, "date", False, "^\\d{4}-\\d{2}-\\d{2}$", "其他手术 2 日期", "", ""),
    FieldSpec("手术操作代码计数", SECTION_INPATIENT, "int", False, "range:0,40", "手术操作代码总数", "", "1"),
    # 重症监护与离院
    FieldSpec("呼吸机使用时间天", SECTION_INPATIENT, "int", False, "range:0,180", "呼吸机累计天数", "", "0"),
    FieldSpec("呼吸机使用时间小时", SECTION_INPATIENT, "int", False, "range:0,23", "呼吸机累计小时数", "", "0"),
    FieldSpec("呼吸机使用时间分钟", SECTION_INPATIENT, "int", False, "range:0,59", "呼吸机累计分钟数", "", "0"),
    FieldSpec("颅脑损伤入院前昏迷天", SECTION_INPATIENT, "int", False, "range:0,365", "颅脑损伤患者入院前昏迷天数", "", ""),
    FieldSpec("颅脑损伤入院前昏迷小时", SECTION_INPATIENT, "int", False, "range:0,23", "入院前昏迷小时", "", ""),
    FieldSpec("颅脑损伤入院后昏迷天", SECTION_INPATIENT, "int", False, "range:0,365", "入院后昏迷天数", "", ""),
    FieldSpec("颅脑损伤入院后昏迷小时", SECTION_INPATIENT, "int", False, "range:0,23", "入院后昏迷小时", "", ""),
    FieldSpec("重症监护病房类型", SECTION_INPATIENT, "enum", False, "in:综合ICU,专科ICU,无", "ICU 类型（无则空）", "", "无"),
    FieldSpec("重症监护进入时间", SECTION_INPATIENT, "datetime", False, "^\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}$", "首次入 ICU 时间", "", ""),
    FieldSpec("重症监护转出时间", SECTION_INPATIENT, "datetime", False, "^\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}$", "转出 ICU 时间", "", ""),
    FieldSpec("ICU住院天数", SECTION_INPATIENT, "int", False, "range:0,180", "ICU 实际住院天数（≤ 实际住院天数）", "WH-3.5", "0"),
    FieldSpec("离院方式", SECTION_INPATIENT, "enum", True, "in:1医嘱离院,2医嘱转院,3医嘱转基层医疗机构,4非医嘱离院,9其他", "离院方式（2/3 时须填拟接收机构）", "", "1医嘱离院"),
    FieldSpec("拟接收机构名称", SECTION_INPATIENT, "str", False, ".+", "离院方式 2/3 时必填", "", ""),
    FieldSpec("拟接收机构代码", SECTION_INPATIENT, "str", False, "^[A-Z0-9]{10,18}$", "拟接收机构代码（离院方式 2/3 时必填）", "", ""),
    FieldSpec("出院医嘱", SECTION_INPATIENT, "str", False, ".+", "出院医嘱内容", "", "<EXAMPLE_DISCHARGE_ORDER>"),
    FieldSpec("出院带药", SECTION_INPATIENT, "str", False, ".+", "出院带药清单", "", ""),
    FieldSpec("备注住院", SECTION_INPATIENT, "str", False, ".{0,200}", "住院诊疗备注", "", ""),
    FieldSpec("病理诊断", SECTION_INPATIENT, "str", False, ".+", "病理诊断（如有）", "", ""),
)


_BILLING_FIELDS: tuple[FieldSpec, ...] = (
    # ---- 医疗收费信息 98 项 ----
    # 总体金额类（6 项）
    FieldSpec("结算期间起", SECTION_BILLING, "date", True, "^\\d{4}-\\d{2}-\\d{2}$", "费用结算起始日期", "", "<EXAMPLE_BILL_START>"),
    FieldSpec("结算期间止", SECTION_BILLING, "date", True, "^\\d{4}-\\d{2}-\\d{2}$", "费用结算截止日期", "", "<EXAMPLE_BILL_END>"),
    FieldSpec("金额合计", SECTION_BILLING, "float", True, "range:0.0,10000000.0", "总费用（与票据一致；=14 类费用合计）", "WH-3.3", "0.00"),
    FieldSpec("甲类费用合计", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "甲类（医保 100% 报销）", "", "0.00"),
    FieldSpec("乙类费用合计", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "乙类（部分自付）", "", "0.00"),
    FieldSpec("自费费用合计", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "自费部分", "", "0.00"),
    FieldSpec("其他费用合计", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "其他类费用", "", "0.00"),
    # 14 类费用（每类 6 项：金额 / 医保内金额 / 自费金额 / 比例 / 数量 / 单位）= 84 项
    # 但合并比例与单位后可压成 4-5 项；这里取 5 项/类 × 14 类 = 70 项，加上 8 项其他费用字段 = 78 项
    # 为达到 98，扩展为 6 项/类 × 14 类 = 84 项 + 14 项其他收费字段 = 98
    # ---- 床位费 ----
    FieldSpec("床位费金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "床位费总额", "", "0.00"),
    FieldSpec("床位费医保内金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "床位费医保支付部分", "", "0.00"),
    FieldSpec("床位费自费金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "床位费自费部分", "", "0.00"),
    FieldSpec("床位费天数", SECTION_BILLING, "int", False, "range:0,365", "床位计费天数（按日计费项目收费数量≤实际住院天数）", "38.3;WH-3.3", "0"),
    FieldSpec("床位费单价", SECTION_BILLING, "float", False, "range:0.0,100000.0", "床位单价", "", "0.00"),
    FieldSpec("床位费计价单位", SECTION_BILLING, "str", False, "in:日,小时", "床位费计价单位", "", "日"),
    # ---- 诊察费 ----
    FieldSpec("诊察费金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "诊察费总额", "", "0.00"),
    FieldSpec("诊察费医保内金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "诊察费医保部分", "", "0.00"),
    FieldSpec("诊察费自费金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "诊察费自费部分", "", "0.00"),
    FieldSpec("诊察费次数", SECTION_BILLING, "int", False, "range:0,500", "诊察次数", "", "0"),
    FieldSpec("诊察费单价", SECTION_BILLING, "float", False, "range:0.0,10000.0", "诊察费单价", "", "0.00"),
    FieldSpec("诊察费计价单位", SECTION_BILLING, "str", False, "in:次,日", "诊察费计价单位", "", "次"),
    # ---- 检查费 ----
    FieldSpec("检查费金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "检查费总额", "", "0.00"),
    FieldSpec("检查费医保内金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "检查费医保部分", "", "0.00"),
    FieldSpec("检查费自费金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "检查费自费部分", "", "0.00"),
    FieldSpec("检查费次数", SECTION_BILLING, "int", False, "range:0,500", "检查次数", "", "0"),
    FieldSpec("检查费单价", SECTION_BILLING, "float", False, "range:0.0,100000.0", "检查费单价", "", "0.00"),
    FieldSpec("检查费计价单位", SECTION_BILLING, "str", False, "in:次,部位,项", "检查费计价单位", "", "次"),
    # ---- 化验费 ----
    FieldSpec("化验费金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "化验费总额", "", "0.00"),
    FieldSpec("化验费医保内金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "化验费医保部分", "", "0.00"),
    FieldSpec("化验费自费金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "化验费自费部分", "", "0.00"),
    FieldSpec("化验费次数", SECTION_BILLING, "int", False, "range:0,500", "化验次数", "", "0"),
    FieldSpec("化验费单价", SECTION_BILLING, "float", False, "range:0.0,100000.0", "化验费单价", "", "0.00"),
    FieldSpec("化验费计价单位", SECTION_BILLING, "str", False, "in:次,项,标本", "化验费计价单位", "", "次"),
    # ---- 治疗费 ----
    FieldSpec("治疗费金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "治疗费总额", "", "0.00"),
    FieldSpec("治疗费医保内金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "治疗费医保部分", "", "0.00"),
    FieldSpec("治疗费自费金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "治疗费自费部分", "", "0.00"),
    FieldSpec("治疗费次数", SECTION_BILLING, "int", False, "range:0,500", "治疗次数", "", "0"),
    FieldSpec("治疗费单价", SECTION_BILLING, "float", False, "range:0.0,100000.0", "治疗费单价", "", "0.00"),
    FieldSpec("治疗费计价单位", SECTION_BILLING, "str", False, "in:次,日,疗程,部位", "治疗费计价单位", "", "次"),
    # ---- 手术费 ----
    FieldSpec("手术费金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "手术费总额", "", "0.00"),
    FieldSpec("手术费医保内金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "手术费医保部分", "", "0.00"),
    FieldSpec("手术费自费金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "手术费自费部分", "", "0.00"),
    FieldSpec("手术费次数", SECTION_BILLING, "int", False, "range:0,50", "手术次数", "", "0"),
    FieldSpec("手术费单价", SECTION_BILLING, "float", False, "range:0.0,100000.0", "手术费单价", "", "0.00"),
    FieldSpec("手术费计价单位", SECTION_BILLING, "str", False, "in:次,台,部位", "手术费计价单位", "", "次"),
    # ---- 护理费 ----
    FieldSpec("护理费金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "护理费总额", "", "0.00"),
    FieldSpec("护理费医保内金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "护理费医保部分", "", "0.00"),
    FieldSpec("护理费自费金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "护理费自费部分", "", "0.00"),
    FieldSpec("护理费天数", SECTION_BILLING, "int", False, "range:0,365", "护理计费天数（≤实际住院天数）", "38.3;WH-3.3", "0"),
    FieldSpec("护理费单价", SECTION_BILLING, "float", False, "range:0.0,10000.0", "护理费单价", "", "0.00"),
    FieldSpec("护理费计价单位", SECTION_BILLING, "str", False, "in:日,小时,次", "护理费计价单位", "", "日"),
    # ---- 卫生材料费 ----
    FieldSpec("卫生材料费金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "卫生材料费总额", "", "0.00"),
    FieldSpec("卫生材料费医保内金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "卫生材料费医保部分", "", "0.00"),
    FieldSpec("卫生材料费自费金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "卫生材料费自费部分", "", "0.00"),
    FieldSpec("卫生材料费数量", SECTION_BILLING, "int", False, "range:0,5000", "卫生材料使用数量", "", "0"),
    FieldSpec("卫生材料费单价", SECTION_BILLING, "float", False, "range:0.0,100000.0", "卫生材料单价", "", "0.00"),
    FieldSpec("卫生材料费计价单位", SECTION_BILLING, "str", False, "in:个,套,包,件", "卫生材料计价单位", "", "个"),
    # ---- 西药费 ----
    FieldSpec("西药费金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "西药费总额", "", "0.00"),
    FieldSpec("西药费医保内金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "西药费医保部分", "", "0.00"),
    FieldSpec("西药费自费金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "西药费自费部分", "", "0.00"),
    FieldSpec("西药费数量", SECTION_BILLING, "int", False, "range:0,10000", "西药用量", "", "0"),
    FieldSpec("西药费单价", SECTION_BILLING, "float", False, "range:0.0,100000.0", "西药单价", "", "0.00"),
    FieldSpec("西药费计价单位", SECTION_BILLING, "str", False, "in:盒,瓶,支,片,mg,g,ml", "西药计价单位", "", "盒"),
    # ---- 中药饮片费 ----
    FieldSpec("中药饮片费金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "中药饮片费总额", "", "0.00"),
    FieldSpec("中药饮片费医保内金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "中药饮片费医保部分", "", "0.00"),
    FieldSpec("中药饮片费自费金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "中药饮片费自费部分", "", "0.00"),
    FieldSpec("中药饮片费数量", SECTION_BILLING, "int", False, "range:0,10000", "中药饮片用量", "", "0"),
    FieldSpec("中药饮片费单价", SECTION_BILLING, "float", False, "range:0.0,100000.0", "中药饮片单价", "", "0.00"),
    FieldSpec("中药饮片费计价单位", SECTION_BILLING, "str", False, "in:g,kg,剂", "中药饮片计价单位", "", "g"),
    # ---- 中成药费 ----
    FieldSpec("中成药费金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "中成药费总额", "", "0.00"),
    FieldSpec("中成药费医保内金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "中成药费医保部分", "", "0.00"),
    FieldSpec("中成药费自费金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "中成药费自费部分", "", "0.00"),
    FieldSpec("中成药费数量", SECTION_BILLING, "int", False, "range:0,10000", "中成药用量", "", "0"),
    FieldSpec("中成药费单价", SECTION_BILLING, "float", False, "range:0.0,100000.0", "中成药单价", "", "0.00"),
    FieldSpec("中成药费计价单位", SECTION_BILLING, "str", False, "in:盒,瓶,支,片", "中成药计价单位", "", "盒"),
    # ---- 一般诊疗费 ----
    FieldSpec("一般诊疗费金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "一般诊疗费总额", "", "0.00"),
    FieldSpec("一般诊疗费医保内金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "一般诊疗费医保部分", "", "0.00"),
    FieldSpec("一般诊疗费自费金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "一般诊疗费自费部分", "", "0.00"),
    FieldSpec("一般诊疗费次数", SECTION_BILLING, "int", False, "range:0,500", "一般诊疗次数", "", "0"),
    FieldSpec("一般诊疗费单价", SECTION_BILLING, "float", False, "range:0.0,10000.0", "一般诊疗费单价", "", "0.00"),
    FieldSpec("一般诊疗费计价单位", SECTION_BILLING, "str", False, "in:次,日", "一般诊疗费计价单位", "", "次"),
    # ---- 挂号费 ----
    FieldSpec("挂号费金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "挂号费总额", "", "0.00"),
    FieldSpec("挂号费医保内金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "挂号费医保部分", "", "0.00"),
    FieldSpec("挂号费自费金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "挂号费自费部分", "", "0.00"),
    FieldSpec("挂号费次数", SECTION_BILLING, "int", False, "range:0,500", "挂号次数", "", "0"),
    FieldSpec("挂号费单价", SECTION_BILLING, "float", False, "range:0.0,10000.0", "挂号费单价", "", "0.00"),
    FieldSpec("挂号费计价单位", SECTION_BILLING, "str", False, "in:次,日", "挂号费计价单位", "", "次"),
    # ---- 其他费 ----
    FieldSpec("其他费金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "其他费用总额", "", "0.00"),
    FieldSpec("其他费医保内金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "其他费医保部分", "", "0.00"),
    FieldSpec("其他费自费金额", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "其他费自费部分", "", "0.00"),
    FieldSpec("其他费数量", SECTION_BILLING, "int", False, "range:0,5000", "其他费用数量", "", "0"),
    FieldSpec("其他费单价", SECTION_BILLING, "float", False, "range:0.0,100000.0", "其他费单价", "", "0.00"),
    # ---- 医保统筹基金支付与个人负担（8 项）----
    FieldSpec("医保统筹基金支付", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "统筹基金支付金额", "", "0.00"),
    FieldSpec("个人账户支付", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "个人账户支付", "", "0.00"),
    FieldSpec("个人现金支付", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "个人现金支付", "", "0.00"),
    FieldSpec("个人自付", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "个人自付（含乙类自付部分）", "", "0.00"),
    FieldSpec("大病保险支付", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "大病保险支付", "", "0.00"),
    FieldSpec("医疗救助支付", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "医疗救助支付", "", "0.00"),
    FieldSpec("公务员补助支付", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "公务员补助支付", "", "0.00"),
    FieldSpec("其他支付", SECTION_BILLING, "float", False, "range:0.0,10000000.0", "其他渠道支付", "", "0.00"),
)


ALL_FIELDS: tuple[FieldSpec, ...] = _BASIC_FIELDS + _OP_SPECIAL_FIELDS + _INPATIENT_FIELDS + _BILLING_FIELDS


# ---------------------------------------------------------------------------
# 违规负面清单（条例第 38/40 条 + 武汉医保局盘点 + 智能审核条目）
# ---------------------------------------------------------------------------
#
# 编号约定：
#   38.x —— 条例第 38 条违规（一般违规）
#   40.x —— 条例第 40 条违规（骗保行为）
#   WH-3.x —— 武汉医保局《10 大类负面清单》细化
#   AUDIT-x —— 智能审核规则（第 4 节条目）


_VIOLATIONS: tuple[ViolationSpec, ...] = (
    # ---- 条例第 38 条（一般违规，7 条）----
    ViolationSpec("38.1", "38", "error", "分解住院 / 挂床住院", "分解住院、挂床住院；同一医院以转科形式重新办理出入院、不符合出院标准让患者自费一段时间后入院、慢性疾病高频次重复住院；住院期间无固定床位、未办离院手续但病人不在院、不需住院的患者收住院", "rule+行为模式"),
    ViolationSpec("38.2", "38", "error", "过度诊疗 / 过度检查 / 超量开药 / 重复开药", "违反诊疗规范过度诊疗、过度检查、分解处方、超量开药、重复开药或其他不必要的医药服务", "rule"),
    ViolationSpec("38.3", "38", "error", "重复收费 / 超标准收费 / 分解项目收费", "重复收费（已收取项目费用再单独收取其内涵步骤费用）；超标准收费（超政府指导价、超价格加收、按日计费项目数量大于实际住院天数）；分解项目收费（一个项目分解为若干项目分别收费）", "rule"),
    ViolationSpec("38.4", "38", "error", "串换药品 / 医用耗材 / 诊疗项目和服务设施", "低标准套高标准、无标准套相近项目；将不能报销项目变通为能报销项目上传", "rule"),
    ViolationSpec("38.5", "38", "error", "协助转卖药品 / 接受返还现金实物", "为参保人员利用医保待遇转卖药品、接受返还现金实物提供便利", "rule"),
    ViolationSpec("38.6", "38", "error", "将非医保支付范围费用纳入基金结算", "将不属于医保基金支付范围的医药费用纳入基金结算", "rule"),
    ViolationSpec("38.7", "38", "error", "其他造成基金损失的行为", "主诊断与主手术不符；手术日期早于入院日期；非主手术按主手术标准收费等违规兜底", "rule"),
    # ---- 条例第 40 条（骗保，4 条）----
    ViolationSpec("40.1", "40", "error", "诱导 / 协助冒名就医 / 虚假证明 / 串通虚开", "诱导、协助他人冒名或虚假就医购药，提供虚假证明材料，串通虚开费用单据", "rule+行为模式"),
    ViolationSpec("40.2", "40", "error", "伪造 / 变造 / 隐匿 / 涂改 / 销毁医学文书", "伪造、变造、隐匿、涂改、销毁医学文书、医学证明、会计凭证、电子信息等资料", "rule"),
    ViolationSpec("40.3", "40", "error", "虚构医药服务项目", "虚构医药服务项目；只下医嘱不做检查治疗；没有治疗设备而虚构项目收费", "rule"),
    ViolationSpec("40.4", "40", "error", "其他骗保行为", "其他骗保行为（兜底）", "rule"),
    # ---- 武汉医保局盘点 6 类细化（WH-3.x）----
    ViolationSpec("WH-3.1", "WH-3", "error", "分解住院情形（武汉口径）", "同一医院以转科治疗形式重新办理出入院手续；不符合出院标准让患者转自费一段时间后再入院或假出入院；慢性疾病患者非急性期非计划高频次重复住院", "行为模式"),
    ViolationSpec("WH-3.2", "WH-3", "error", "挂床住院情形（武汉口径）", "住院期间无固定床位或与他人共用床位；未办离院外出手续但病人不在院；不需要住院治疗的患者收住院；已办住院手续但不在院接受治疗；住院期间在单位上班或回家休养", "行为模式"),
    ViolationSpec("WH-3.3", "WH-3", "error", "重复收费 / 超标准收费 / 分解项目收费（武汉口径）", "重复收费：已收取某诊疗项目费用再单独收取该项目内涵步骤已包含的相关项目费用；超标准收费：超过政府最高指导价、按日计费项目收费数量大于实际住院天数、改变计价单位多收费；分解项目收费：将一个项目内涵步骤分解为若干项目分别收费", "rule"),
    ViolationSpec("WH-3.4", "WH-3", "warn", "串换项目（武汉口径）", "低标准收费项目套用高标准项目名称或无收费标准项目套用相近项目；将不能报销项目变通为能报销项目上传并实现报销", "rule"),
    ViolationSpec("WH-3.5", "WH-3", "error", "过度诊疗 / 过度检查（武汉口径）", "违反临床用药常规和联合用药原则不合理或超范围超剂量用药；不合理超常规量使用一次性耗材；无明显指征开展大型医用设备检查；放宽或降低手术指征实施手术", "rule"),
    ViolationSpec("WH-3.6", "WH-3", "error", "虚构医药服务项目（武汉口径）", "只下医嘱不做检查治疗；没有治疗设备而虚构项目收费；费用清单记录与医嘱或实际使用不符", "rule"),
    # ---- 智能审核规则条目（4 节，7 条）----
    ViolationSpec("AUDIT-1", "AUDIT", "error", "非目录审核", "自费或限工伤/限生育的药品与项目纳入基本医保报销", "rule"),
    ViolationSpec("AUDIT-2", "AUDIT", "warn", "限定适应症用药", "目录备注的限定适应症与使用条件不符", "rule+llm"),
    ViolationSpec("AUDIT-3", "AUDIT", "error", "限定医院类型级别", "如'二级及以上医疗机构'限定与实际机构级别比对", "rule"),
    ViolationSpec("AUDIT-4", "AUDIT", "warn", "限儿童（限新生儿）", "目录限新生儿、限儿童支付的药品与项目年龄不符", "rule"),
    ViolationSpec("AUDIT-5", "AUDIT", "warn", "限定性别", "目录性别限定药品与项目与患者性别不符", "rule"),
    ViolationSpec("AUDIT-6", "AUDIT", "error", "费用日期不在住院期间", "费用发生日期超出住院日期范围或一次性补传费用明细", "rule"),
    ViolationSpec("AUDIT-7", "AUDIT", "warn", "超限定频次", "诊疗项目在时间区间内使用频次超规", "rule"),
)


# ---------------------------------------------------------------------------
# 跨字段校验规则种子
# ---------------------------------------------------------------------------
#
# 命名遵循 kebab-case；comparator 取值见 field_ruleset.Comparator。
# 默认 ≥5 条，覆盖官方硬规则 + 业务常见冲突（与 task 23 扩展点对齐）。

_CROSS_FIELD_RULES: tuple[CrossRuleSpec, ...] = (
    CrossRuleSpec(
        name="surgery_date_after_admission",
        lhs_field="主要手术操作日期",
        rhs_field="入院日期",
        comparator=">=",
        expected_value="",
        error_message="主要手术日期（{lhs}）早于入院日期（{rhs}），违反条例 38.7 与 DIP 质控硬规则",
        suggested_action="复核手术日期是否录错，或确认是否分解住院",
        severity="error",
    ),
    CrossRuleSpec(
        name="discharge_date_after_admission",
        lhs_field="出院日期",
        rhs_field="入院日期",
        comparator=">=",
        expected_value="",
        error_message="出院日期（{lhs}）早于入院日期（{rhs}）",
        suggested_action="复核出入院时间",
        severity="error",
    ),
    CrossRuleSpec(
        name="icu_days_le_stay_days",
        lhs_field="ICU住院天数",
        rhs_field="实际住院天数",
        comparator="<=",
        expected_value="",
        error_message="ICU 住院天数（{lhs}）大于实际住院天数（{rhs}），违反武汉盘点 3.5",
        suggested_action="复核 ICU 进入/转出时间",
        severity="error",
    ),
    CrossRuleSpec(
        name="bed_days_le_stay_days",
        lhs_field="床位费天数",
        rhs_field="实际住院天数",
        comparator="<=",
        expected_value="",
        error_message="床位计费天数（{lhs}）大于实际住院天数（{rhs}），违反条例 38.3 超标准收费",
        suggested_action="按日计费天数须 ≤ 实际住院天数",
        severity="error",
    ),
    CrossRuleSpec(
        name="nursing_days_le_stay_days",
        lhs_field="护理费天数",
        rhs_field="实际住院天数",
        comparator="<=",
        expected_value="",
        error_message="护理计费天数（{lhs}）大于实际住院天数（{rhs}）",
        suggested_action="复核护理费计费天数",
        severity="error",
    ),
    CrossRuleSpec(
        name="billing_period_within_stay",
        lhs_field="结算期间起",
        rhs_field="入院日期",
        comparator=">=",
        expected_value="",
        error_message="结算期间起（{lhs}）早于入院日期（{rhs}），违反 AUDIT-6 费用日期不在住院期间",
        suggested_action="复核费用明细日期",
        severity="error",
    ),
    CrossRuleSpec(
        name="billing_end_le_discharge",
        lhs_field="结算期间止",
        rhs_field="出院日期",
        comparator="<=",
        expected_value="",
        error_message="结算期间止（{lhs}）晚于出院日期（{rhs}），违反 AUDIT-6",
        suggested_action="费用期间须在住院区间内",
        severity="error",
    ),
    CrossRuleSpec(
        name="transfer_requires_org",
        lhs_field="拟接收机构名称",
        rhs_field="离院方式",
        comparator="not_in_set",
        expected_value="1医嘱离院,9其他",
        error_message="离院方式（{rhs}）为 2/3 时必须填拟接收机构名称与代码",
        suggested_action="补填拟接收机构名称与代码",
        severity="error",
    ),
    # ---- task 23 新增规则（跨字段详细化）----
    CrossRuleSpec(
        name="diagnosis_surgery_consistency",
        lhs_field="主要诊断代码",
        rhs_field="主要手术操作代码",
        comparator="icd_chapter_consistent",
        expected_value=(
            "I:35,36,37,38,39"
            "|J:00,01,02,03,04,05,30,31,32,33,34"
            "|K:42,43,44,45,46,47,48,49,50,51,52,53,54,55,56,57,58,59"
            "|M:76,77,78,79,80,81,82,83,84,85,86"
            "|N:55,56,57,58,59,60,61,62,63,64,65"
            "|O:72,73,74,75"
            "|S:76,77,78,79,80,81,82,83,84,85,86"
        ),
        error_message=(
            "主要诊断 ICD {lhs} 与主要手术 ICD {rhs} 章节不匹配"
            "，违反条例 38.7「主诊断与主手术不符」"
        ),
        suggested_action=(
            "复核主诊断与主手术对应（如：I 循环系统疾病须配 35-39 类心血管手术，"
            "K 消化系统疾病须配 42-59 类消化手术）"
        ),
        severity="error",
    ),
    CrossRuleSpec(
        name="actual_stay_days_match_dates",
        lhs_field="实际住院天数",
        rhs_field="入院日期",
        comparator="date_duration_equals",
        expected_value="FIELD:出院日期",
        error_message=(
            "实际住院天数（{lhs}）与出入院日期差值（{rhs}）不符"
            "，违反武汉盘点 3.5 与医保口径"
        ),
        suggested_action=(
            "按医保口径重新计算实际住院天数：入院当日算一天、出院当日不算（当天出入院按一天）"
        ),
        severity="warn",
    ),
)


# ---------------------------------------------------------------------------
# 迁移函数
# ---------------------------------------------------------------------------


def _read_schema_sql() -> str:
    """读取本包内嵌的 schema.sql。"""
    sql_path = Path(__file__).with_name("schema.sql")
    return sql_path.read_text(encoding="utf-8")


def _validate_field_count(fields: Sequence[FieldSpec]) -> None:
    """193 项不变式校验：分区数 + 总数。"""
    counts: dict[str, int] = {}
    for f in fields:
        counts[f.section] = counts.get(f.section, 0) + 1
    for section, expected in EXPECTED_SECTION_COUNTS.items():
        actual = counts.get(section, 0)
        if actual != expected:
            raise ConfigurationError(
                f"权威字段数与官方规范失同步：分区 {section} 应有 {expected} 项，"
                f"实际 {actual} 项。请更新 migrations.py 内的 {section}_FIELDS 列表。"
            )
    if len(fields) != EXPECTED_TOTAL_FIELDS:
        raise ConfigurationError(
            f"权威字段总数不符：期望 {EXPECTED_TOTAL_FIELDS}（31+6+58+98），"
            f"实际 {len(fields)}"
        )


def init_billing_db(db_path: Path) -> dict[str, int]:
    """初始化 SQLite：建表 + 清空 + 批量 INSERT 193 项字段 + 违规清单 + 跨字段规则。

    返回插入条数字典。幂等：重复调用结果一致。
    """
    _validate_field_count(ALL_FIELDS)

    schema_sql = _read_schema_sql()

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(schema_sql)
        # 幂等清空
        conn.execute("DELETE FROM billing_fields")
        conn.execute("DELETE FROM violation_catalog")
        conn.execute("DELETE FROM cross_field_rules")
        conn.commit()

        # INSERT billing_fields
        conn.executemany(
            "INSERT INTO billing_fields "
            "(name, section, field_type, required, value_constraint, "
            " description, related_violation, example_value) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    f.name, f.section, f.field_type, int(f.required),
                    f.value_constraint, f.description, f.related_violation,
                    f.example_value,
                )
                for f in ALL_FIELDS
            ],
        )

        # INSERT violation_catalog
        conn.executemany(
            "INSERT INTO violation_catalog "
            "(rule_id, clause, severity, title, description, detection_method) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (v.rule_id, v.clause, v.severity, v.title, v.description, v.detection_method)
                for v in _VIOLATIONS
            ],
        )

        # INSERT cross_field_rules
        conn.executemany(
            "INSERT INTO cross_field_rules "
            "(name, lhs_field, rhs_field, comparator, expected_value, "
            " error_message, suggested_action, severity) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (r.name, r.lhs_field, r.rhs_field, r.comparator, r.expected_value,
                 r.error_message, r.suggested_action, r.severity)
                for r in _CROSS_FIELD_RULES
            ],
        )

        conn.commit()

        # 计数核验
        cur = conn.execute("SELECT COUNT(*) FROM billing_fields")
        fields_n = cur.fetchone()[0]
        cur = conn.execute("SELECT COUNT(*) FROM violation_catalog")
        viol_n = cur.fetchone()[0]
        cur = conn.execute("SELECT COUNT(*) FROM cross_field_rules")
        cross_n = cur.fetchone()[0]

    if fields_n != EXPECTED_TOTAL_FIELDS:
        raise ConfigurationError(
            f"INSERT 后字段数核验失败：期望 {EXPECTED_TOTAL_FIELDS} 实际 {fields_n}"
        )
    return {"billing_fields": fields_n, "violation_catalog": viol_n, "cross_field_rules": cross_n}


def list_fields() -> Iterable[FieldSpec]:
    """测试用：返回全部 193 项字段。"""
    return ALL_FIELDS


def list_violations() -> Iterable[ViolationSpec]:
    return _VIOLATIONS


def list_cross_field_rules() -> Iterable[CrossRuleSpec]:
    return _CROSS_FIELD_RULES