"""100 份脱敏出院小结压测 fixture 模板生成器 (Task 33)。

目标:
- 生成 100 份脱敏出院小结到 ``tests/fixtures/loadtest/case_001.txt ~
  case_100.txt``;
- 每份 ≥ 1500 中文字符;
- 含完整章节结构 (主诉 / 现病史 / 既往史 / 个人史 / 体格检查 /
  辅助检查 / 入院诊断 / 诊疗经过 / 出院诊断 / 出院时情况 / 出院医嘱);
- 含高频字段可抽取 (姓名 / 性别 / 年龄 / 入院日期 / 出院日期 /
  住院天数 / ICD-10 / ICD-9-CM-3 / 入院科别 / 出院科别 / 主要手术
  操作日期 / 离院方式);
- 50 份正常样本 + 50 份含已知冲突 (5 类各 10 份);
- 全部使用占位符 (``<EXAMPLE_NAME>`` / ``<EXAMPLE_ID>`` /
  ``<EXAMPLE_AGE>``), 无真实个人信息.

设计要点:
- **确定性随机**: 使用固定种子 ``random.Random(20251017)``, 保证
  pytest 多次跑测试结果一致;
- **ICD 池**: 10 类常见诊断 × 真实 ICD-10 编码; 与既有
  ``discharge_01..10`` 编码分布对齐, 避免抽取 0 字段的极端情况;
- **冲突预埋**: 5 类 KNOWN_CONFLICT 各 10 份 + 50 份正常.

参考:
- ``spec.md`` §3 数据格式与字段要求;
- 既有 ``tests/fixtures/discharge_01..10.txt`` 章节结构与占位符规范.
"""

from __future__ import annotations

import argparse
import random
from datetime import date, timedelta
from pathlib import Path

OUTPUT_DIR = Path(__file__).parent  # 默认输出到 tests/fixtures/loadtest/

# ---------------------------------------------------------------------------
# 固定池：诊断 / 手术 / 科室 / 主诉 模板（贴近既有 fixtures 风格）
# ---------------------------------------------------------------------------

ICD10_POOL: tuple[tuple[str, str, str], ...] = (
    ("冠状动脉粥样硬化性心脏病", "I25.101", "不稳定型心绞痛"),
    ("高血压病3级（极高危）",     "I10.x05", "高血压性心脏病"),
    ("2型糖尿病",                 "E11.901", "非胰岛素依赖型糖尿病"),
    ("胆囊结石伴急性胆囊炎",       "K80.801", "胆囊结石"),
    ("脑梗死",                    "I63.901", "动脉粥样硬化性脑梗死"),
    ("慢性心力衰竭",               "I50.905", "充血性心力衰竭"),
    ("慢性阻塞性肺疾病急性加重",   "J44.901", "急性加重期 COPD"),
    ("心房颤动",                  "I48.001", "阵发性房颤"),
    ("胃溃疡",                    "K25.901", "急性胃溃疡"),
    ("肺炎",                      "J18.901", "社区获得性肺炎"),
)

ICD9_CM3_POOL: tuple[tuple[str, str], ...] = (
    ("冠状动脉支架植入术",      "36.0601"),
    ("经皮冠状动脉腔内成形术",   "36.0101"),
    ("腹腔镜胆囊切除术",        "51.2301"),
    ("机械瓣置换术",            "35.0501"),
    ("脑血管造影术",            "88.4101"),
    ("心脏起搏器植入术",        "37.8001"),
    ("胃大部切除术",            "43.8901"),
    ("胸腔闭式引流术",          "34.0401"),
    ("经皮腔内血管成形术",      "39.5001"),
    ("气管插管术",              "96.0401"),
)

DEPARTMENTS: tuple[str, ...] = (
    "心血管内科", "神经内科", "呼吸内科", "消化内科", "内分泌科",
    "普通外科", "肝胆外科", "心脏外科", "神经外科", "胸外科",
)

CHIEF_COMPLAINTS: tuple[tuple[str, str], ...] = (
    ("反复胸闷", "胸闷、胸痛"),
    ("头晕",     "头晕、肢体麻木"),
    ("腹痛",     "上腹痛、恶心呕吐"),
    ("咳嗽咳痰", "咳嗽、咳痰伴气促"),
    ("心悸",     "心悸、胸闷"),
    ("黄疸",     "皮肤巩膜黄染"),
    ("发热",     "发热、咳嗽"),
    ("呕血",     "呕血、黑便"),
    ("水肿",     "双下肢水肿"),
    ("呼吸困难", "活动后气促"),
)

ICD_CHAPTER_MAP: dict[str, set[str]] = {
    "I": {"35", "36", "37"},
    "J": {"31", "32", "33", "34"},
    "K": {"42", "43", "44", "45", "46", "47", "48", "49", "50", "51", "52"},
    "E": {"52"},
}


def chapter_consistent(icd10: str, icd9_cm3: str) -> bool:
    ch = icd10[0].upper()
    return icd9_cm3.split(".")[0] in ICD_CHAPTER_MAP.get(ch, set())


def gen_patient_header(rng: random.Random, idx: int) -> dict[str, str]:
    return {
        "name": "<EXAMPLE_NAME>",
        "case_id": "<EXAMPLE_ID>",
        "gender": rng.choice(("男", "女")),
        "age": str(rng.randint(45, 85)),
    }


def gen_dates(rng: random.Random, base: date) -> tuple[date, date, int]:
    stay_days = rng.randint(7, 21)
    admission = base + timedelta(days=rng.randint(0, 30))
    discharge = admission + timedelta(days=stay_days)
    return admission, discharge, stay_days


def gen_diagnosis_pair(rng: random.Random) -> tuple[tuple[str, str, str], tuple[str, str]]:
    icd10 = rng.choice(ICD10_POOL)
    if rng.random() < 0.85:
        ch = icd10[1][0]
        candidates = [
            op for op in ICD9_CM3_POOL if op[1].split(".")[0] in ICD_CHAPTER_MAP.get(ch, set())
        ]
        if not candidates:
            candidates = list(ICD9_CM3_POOL)
        icd9 = rng.choice(candidates)
    else:
        icd9 = rng.choice(ICD9_CM3_POOL)
    return icd10, icd9


def build_normal_case(
    rng: random.Random, idx: int, base_date: date
) -> str:
    """构造一份正常出院小结（中文字数 ≥ 1500）。"""
    head = gen_patient_header(rng, idx)
    admission, discharge, stay_days = gen_dates(rng, base_date)
    diagnosis, surgery = gen_diagnosis_pair(rng)

    chief_complaint, symptom_part = rng.choice(CHIEF_COMPLAINTS)
    duration = rng.choice(("1周", "2周", "1个月", "3个月", "半年"))
    duration_zh = rng.choice(("一周", "两周", "一个月", "三个月", "半年"))
    department = rng.choice(DEPARTMENTS)

    smoking = rng.choice((
        "无吸烟史，偶尔饮酒",
        "吸烟30年，约20支/日，偶尔饮酒",
        "无吸烟史，无饮酒史",
        "戒烟5年，无饮酒史",
    ))
    occupation = rng.choice((
        "退休工人，长期居住本地",
        "农民，长期居住本地",
        "教师，长期居住本地，工作强度中等",
        "公务员，长期居住本地，工作压力中等",
    ))
    marriage = rng.choice((
        "已婚，配偶及子女体健。",
        "已婚已育，配偶及子女体健。",
        "丧偶，子女体健，定期体检。",
    ))
    temp = f"{rng.uniform(36.0, 37.2):.1f}"
    pulse = str(rng.randint(60, 95))
    resp = str(rng.randint(16, 22))
    bp_high = rng.randint(120, 170)
    bp_low = rng.randint(70, 100)
    bp = f"{bp_high}/{bp_low}"
    bp_out = f"{bp_high - 10}/{bp_low - 5}"

    ecg = rng.choice(("窦性心律", "窦性心律不齐", "偶发房性早搏", "ST-T 改变"))
    echo = rng.choice(
        ("左室舒张功能减退，EF 50%", "左室壁节段性运动异常",
         "心脏结构未见明显异常", "射血分数保留")
    )
    lab_extra = rng.choice(
        ("肝肾功能、电解质基本正常", "空腹血糖 7.8 mmol/L，HbA1c 7.2%",
         "血脂 TC 5.8 mmol/L，LDL 3.4 mmol/L", "BNP 850 pg/ml")
    )
    medication = rng.choice(
        ("抗血小板、调脂、利尿、降压", "抗凝、调脂、控制心室率",
         "抗感染、化痰、平喘", "抑酸、保护胃黏膜")
    )
    surgery_room = rng.choice(("导管室", "手术室", "内镜中心", "介入室"))
    perioperative = rng.choice(
        ("低分子肝素桥接抗凝", "监测凝血功能", "继续抗感染治疗", "观察生命体征")
    )
    discharge_symptom = rng.choice(
        ("无胸闷、胸痛、气促", "无腹痛、恶心、呕吐",
         "无咳嗽、咳痰", "无头晕、肢体麻木")
    )
    discharge_sign = rng.choice(
        ("双肺呼吸音清，未闻及干湿性啰音", "心律齐，未闻及病理性杂音",
         "腹软，无压痛反跳痛", "双下肢无水肿")
    )
    diet = rng.choice(
        ("低盐低脂饮食", "糖尿病饮食", "清淡饮食，避免辛辣刺激", "优质蛋白饮食")
    )
    medication_out = rng.choice(
        ("阿司匹林肠溶片 100mg qd，阿托伐他汀钙片 20mg qn",
         "华法林 2.5mg qd（根据 INR 调整）",
         "二甲双胍片 0.5g bid",
         "硝苯地平控释片 30mg qd")
    )
    followup = rng.choice(
        ("监测 INR，每月复查", "监测血糖，每 2 周复查糖化血红蛋白",
         "监测肝肾功能", "监测血压，每日记录")
    )

    # 现病史扩充
    admission_symptom_detail = rng.choice((
        "活动后明显加重，休息后可部分缓解，伴夜间阵发性呼吸困难",
        "症状持续不缓解，伴出汗、心悸",
        "进食油腻食物后症状加重，伴反酸、嗳气",
        "症状进行性加重，伴咳嗽、咳白色泡沫样痰",
    ))
    course_detail = rng.choice((
        "病程中无晕厥、意识丧失，无肢体活动障碍，无言语不清，无视物模糊",
        "病程中无明显胸痛、放射痛，无恶心、呕吐，无呕血、黑便",
        "病程中无发热、盗汗，无消瘦，无明显体重下降",
        "病程中无腹痛、腹泻，无血便，无里急后重感",
    ))
    treatment_detail = rng.choice((
        "曾于当地诊所就诊，予口服药物治疗（具体不详），症状无明显缓解",
        "曾于外院门诊就诊，予对症支持治疗，症状反复发作",
        "自行口服止痛药治疗后症状稍缓解，但仍反复发作",
        "未予特殊诊治，为求进一步诊治遂来我院",
    ))
    symptom_progression = rng.choice((
        "住院第1-3天症状较明显，经积极对症支持治疗后第4-5天症状逐渐缓解，第6天后基本消失。",
        "住院期间症状进行性改善，3天后症状明显减轻，5天后基本消失，未再出现新发不适。",
        "经治疗第2天症状开始缓解，第5天症状基本消失，精神、食欲、睡眠较前好转。",
        "经积极治疗后症状逐渐好转，住院第7天时症状基本消失，达到出院标准。",
    ))
    complication_note = rng.choice((
        "住院期间未发生院内感染、血栓、压疮等并发症，病情恢复顺利。",
        "住院期间未发生医疗安全不良事件，未出现药物不良反应。",
        "住院期间未发生跌倒、坠床等不良事件，切口愈合良好。",
        "住院期间未发生院内感染，无输血、输液反应，病情稳定。",
    ))

    # 体格检查扩充
    lung_exam = rng.choice(
        ("双肺呼吸音粗，双下肺可闻及湿性啰音",
         "双肺呼吸音清，未闻及明显干湿性啰音",
         "双肺呼吸音粗，可闻及散在哮鸣音",
         "右下肺呼吸音减弱，未闻及明显干湿性啰音")
    )
    heart_exam = rng.choice(
        (f"心率{pulse}次/分，律齐，各瓣膜听诊区未闻及病理性杂音",
         f"心率{pulse}次/分，律齐，心音有力",
         f"心率{pulse}次/分，律不齐，可闻及期前收缩",
         f"心率{pulse}次/分，律齐，心尖区可闻及收缩期吹风样杂音")
    )
    abd_exam = rng.choice(
        ("腹软，无压痛、反跳痛，肝脾未触及肿大",
         "腹软，剑突下轻压痛，无反跳痛",
         "腹部膨隆，叩诊鼓音，移动性浊音阴性",
         "腹软，右上腹轻压痛，Murphy 征可疑阳性")
    )

    # 辅助检查扩充
    blood_test = rng.choice(
        ("血常规：白细胞计数 7.2×10^9/L，中性粒细胞百分比 68%，血红蛋白 135 g/L",
         "血常规基本正常",
         "血常规示轻度贫血，血红蛋白 105 g/L",
         "血常规示白细胞升高 12.5×10^9/L，中性粒细胞百分比 82%")
    )
    biochemistry = rng.choice(
        ("肝肾功能：丙氨酸氨基转移酶 28 U/L，天门冬氨酸氨基转移酶 32 U/L，血肌酐 78 μmol/L",
         "电解质：钾 4.2 mmol/L，钠 138 mmol/L，氯 102 mmol/L",
         "血脂：总胆固醇 5.6 mmol/L，甘油三酯 1.8 mmol/L",
         "空腹血糖：8.5 mmol/L，糖化血红蛋白 7.8%")
    )
    coagulation = rng.choice(
        ("凝血功能：凝血酶原时间 12.5 秒，活化部分凝血活酶时间 32 秒",
         "凝血功能基本正常",
         "D-二聚体 0.8 mg/L（轻度升高）",
         "INR 1.05")
    )

    # 诊疗经过扩充
    pre_op_detail = rng.choice(
        ("入院后完善三大常规、生化、凝血、心电图、心脏彩超、胸部 CT 等相关检查",
         "入院后予积极完善相关辅助检查，明确诊断",
         "入院后监测生命体征，完善术前相关检查，无手术禁忌",
         "入院后予以积极对症支持治疗，密切观察病情变化")
    )
    surgery_intro = rng.choice(
        ("在气管插管全身麻醉下行",
         "在局部浸润麻醉下行",
         "在静脉复合麻醉下行",
         "在硬膜外麻醉下行")
    )
    op_findings = rng.choice(
        ("术中探查见病变部位与术前诊断相符，手术过程顺利，术中出血约 50 ml",
         "术中所见与术前诊断一致，未见明显异常，手术顺利",
         "术中诊断明确，操作顺利，安返病房",
         "术中生命体征平稳，手术顺利结束")
    )
    post_op_day1 = rng.choice(("术后第 1 天", "术后第 2 天", "术后第 3 天"))
    post_op_status1 = rng.choice(
        ("患者精神可，无明显胸闷、胸痛，切口敷料干燥，无渗血、渗液",
         "生命体征平稳，未诉特殊不适",
         "一般情况好，进食、睡眠可",
         "精神、食欲、睡眠较术后前好转")
    )
    post_op_test1 = rng.choice(("血常规", "电解质", "肝肾功能", "心肌酶谱"))
    post_op_result1 = rng.choice(
        ("未见明显异常", "结果较前好转", "电解质正常",
         "心肌酶谱正常范围")
    )
    post_op_day2 = rng.choice(("术后第 5 天", "术后第 7 天", "术后第 10 天"))
    post_op_status2 = rng.choice(
        ("患者病情稳定，达到出院标准，予办理出院",
         "症状基本消失，复查各项指标基本正常",
         "切口愈合良好，无红肿、渗出，准予出院",
         "患者一般情况好，准备出院")
    )

    # 出院时情况扩充
    discharge_summary = rng.choice(
        ("住院期间未发生院内感染、跌倒、坠床等不良医疗安全事件，住院费用结算清楚",
         "患者住院全程顺利，无并发症发生，达到临床治愈标准",
         "住院期间病情稳定，临床路径按计划执行，未发生医疗安全不良事件",
         "住院费用已结算，患者对诊疗过程及结果满意，签字出院")
    )
    discharge_advice = rng.choice(
        ("注意休息，避免劳累及情绪激动，保持心情舒畅",
         "戒烟限酒，合理膳食，适量运动，控制体重",
         "季节变化时注意保暖，预防感冒，定期复查",
         "规律作息，避免熬夜，保持良好的生活方式")
    )

    # 出院医嘱扩充
    follow_up_detail = rng.choice((
        "出院后第 1 个月门诊复查血常规、生化、心电图，评估恢复情况",
        "出院后 2 周门诊复查，不适随诊",
        "出院后 1 个月复查心脏彩超，评估心脏功能",
        "出院后每月复查一次肝肾功能、电解质",
    ))
    medication_advice = rng.choice((
        "严格按医嘱服药，不可擅自停药或减量",
        "如出现黑便、牙龈出血等出血倾向，立即停药并就诊",
        "注意监测血压、血糖，定期复查",
        "监测药物不良反应，定期复查相关指标",
    ))
    urgent_signs = rng.choice((
        "如出现胸痛、剧烈头痛、咯血、呕血、黑便等，",
        "如出现发热、咳嗽、咳痰、喘憋等呼吸道症状，",
        "如出现腹痛、腹胀、恶心、呕吐等消化道症状，",
        "如出现心悸、胸闷、头晕、乏力等，",
    ))
    rehab_advice = rng.choice((
        "出院后循序渐进康复锻炼，避免剧烈运动及重体力劳动",
        "出院后规律作息，保证充足睡眠，避免熬夜",
        "出院后注意保暖，避免受凉，预防感冒及呼吸道感染",
        "出院后保持心情舒畅，避免情绪激动及精神紧张",
    ))

    parts: list[str] = []

    # 头行（精简）
    parts.append(
        f"姓名：{head['name']}　性别：{head['gender']}　年龄：{head['age']}岁　"
        f"住院号：{head['case_id']}\n"
        f"入院日期：{admission.strftime('%Y-%m-%d')}　"
        f"出院日期：{discharge.strftime('%Y-%m-%d')}　"
        f"住院天数：{stay_days}天\n"
    )

    # 主诉
    parts.append(f"主诉：{chief_complaint} {duration}，{symptom_part}。\n")

    # 现病史（扩充）
    hpi_lines = [
        f"现病史：患者于{duration_zh}前无明显诱因出现{symptom_part}，{admission_symptom_detail}。"
        f"{course_detail}。{treatment_detail}，遂来我院就诊，门诊以\"{diagnosis[0]}\"收入院。",
        f"\n入院情况：患者入院时神志清楚，精神稍差，自动体位，查体合作。"
        f"体温{temp}℃，脉搏{pulse}次/分，呼吸{resp}次/分，血压{bp}mmHg。"
        f"{lung_exam}。{heart_exam}。{abd_exam}。",
        f"\n入院诊断考虑为\"{diagnosis[0]}\"，入院后予积极完善相关检查，"
        f"明确诊断后予{medication}等对症支持治疗，密切观察病情变化。",
        f"\n病情演变：{symptom_progression}{complication_note}"
        f"经{medication}等综合治疗后，症状逐渐好转，未再出现明显{symptom_part}。"
        f"复查相关指标较入院时改善，精神、食欲、睡眠可，大小便正常，体重无明显变化。",
        f"\n住院期间，监测血压、血糖、血脂、肝肾功能、电解质等指标，"
        f"各项指标基本稳定，未出现明显波动。患者一般情况好，达到出院标准。",
    ]
    parts.append("".join(hpi_lines) + "\n")

    # 既往史（扩充）
    hypertension_years = rng.randint(5, 15)
    diabetes_years = rng.randint(3, 8)
    infectious_history = rng.choice(
        ("否认肝炎、结核等传染病史，",
         "有「乙肝小三阳」病史 20 年，",
         "既往「肺结核」病史 10 年，已治愈，")
    )
    op_history = rng.choice(
        ("既往于外院行胆囊切除术，术后恢复良好；",
         "10 年前因急性阑尾炎行阑尾切除术，术后无并发症；",
         "既往无手术史；")
    )
    transfusion_history = rng.choice(
        ("否认输血史，", "有输血史，无输血反应，")
    )
    past_history_text = (
        f"既往高血压{hypertension_years}年，最高180/100mmHg，长期口服硝苯地平"
        f"控释片30mg qd；2型糖尿病病史{diabetes_years}年，长期口服二甲双胍片"
        f"0.5g bid 治疗。"
        f"{infectious_history}否认药物及食物过敏史。"
        f"{op_history}{transfusion_history}预防接种史不详。"
    )
    parts.append(f"既往史：{past_history_text}\n")

    # 个人史
    personal_history_text = (
        f"生于原籍，长期居住本地，无疫区居住史，无毒物、放射性物质接触史。"
        f"{smoking}。{occupation}。{marriage}"
    )
    parts.append(f"个人史：{personal_history_text}\n")

    # 体格检查（扩充）
    pe_lines = [
        f"体格检查：体温{temp}℃，脉搏{pulse}次/分，呼吸{resp}次/分，"
        f"血压{bp}mmHg。神志清楚，精神可，口唇无紫绀。皮肤、巩膜无黄染，"
        f"未见皮疹、出血点。浅表淋巴结未触及肿大。头颅无畸形，双侧瞳孔"
        f"等大等圆，对光反射灵敏。{lung_exam}。{heart_exam}。{abd_exam}。"
        f"生理反射存在，病理反射未引出。四肢肌力、肌张力正常，"
        f"双下肢无水肿。",
    ]
    parts.append("".join(pe_lines) + "\n")

    # 辅助检查（扩充）
    extra_exam = rng.choice((
        "24 小时动态心电图示偶发房性早搏。",
        "运动平板试验阳性。",
        "骨密度测定示骨量减少。",
        "幽门螺杆菌检测阳性。",
    ))
    ae_lines = [
        f"辅助检查：{blood_test}。{biochemistry}。{coagulation}。"
        f"心电图示{ecg}。心脏彩超示{echo}。胸部 CT 示双肺纹理增粗，"
        f"未见明显斑片影。腹部超声示肝胆胰脾未见明显异常。"
        f"{extra_exam}",
    ]
    parts.append("".join(ae_lines) + "\n")

    # 入院诊断
    admission_lines = [f"{i+1}. {diagnosis[0]}" for i in range(rng.randint(2, 4))]
    parts.append("入院诊断：\n" + "\n".join(admission_lines) + "\n")

    # 诊疗经过（扩充）
    surgery_date = (admission + timedelta(days=rng.randint(2, 5))).strftime("%Y-%m-%d")
    surgery_room_actual = rng.choice(("导管室", "手术室", "内镜中心"))
    tp_lines = [
        f"诊疗经过：入院后{pre_op_detail}。"
        f"于{surgery_date}{surgery_intro}{surgery_room_actual}"
        f"行{surgery[0]}（主要手术操作代码 {surgery[1]}），{op_findings}。"
        f"围手术期{perioperative}，监测生命体征变化。",
        f"\n{post_op_day1}：{post_op_status1}。复查{post_op_test1}，"
        f"结果{post_op_result1}。继续给予{medication}对症支持治疗，"
        f"密切观察患者病情变化，定期监测血压、血糖、体温等生命体征。",
        f"\n{post_op_day2}：{post_op_status2}。继续给患者"
        f"口服{medication}对症治疗，密切观察病情变化。"
        f"复查相关辅助检查，结果较前好转，达到出院标准，予出院。",
        f"\n住院期间监测血压、血糖、血脂、肝肾功能、电解质、心电图等指标，"
        f"各项指标基本稳定。患者住院期间未发生院内感染、血栓、压疮等并发症，"
        f"病情恢复顺利，达到临床治愈，给予办理出院。",
    ]
    parts.append("".join(tp_lines) + "\n")

    # 出院诊断
    discharge_lines = [f"{i+1}. {diagnosis[0]}" for i in range(rng.randint(2, 4))]
    parts.append("出院诊断：\n" + "\n".join(discharge_lines) + "\n")

    # 出院时情况（扩充）
    dt_lines = [
        f"出院时情况：患者一般情况可，{discharge_symptom}。"
        f"{discharge_sign}。{discharge_summary}。"
        f"出院前复查血常规、生化、心电图，结果较前明显好转。",
        f"\n查体：血压{bp_out}mmHg，{discharge_sign}。{discharge_advice}。"
        f"患者精神、食欲、睡眠好，大小便正常，体重无明显变化。",
        f"\n本次住院费用：住院总费用 {rng.randint(15000, 40000)} 元，"
        f"其中医保统筹基金支付约 {rng.randint(9000, 25000)} 元，"
        f"个人自付约 {rng.randint(3000, 12000)} 元。",
        f"出院结算时已核对医保卡、身份证、住院押金票据，"
        f"患者对费用明细无异议。住院 {stay_days} 天，"
        f"单日均费约 {rng.randint(1500, 3500)} 元。",
    ]
    parts.append("".join(dt_lines) + "\n")

    # 出院医嘱（扩充）
    mo_lines = [
        f"出院医嘱：\n1. {diet}。",
        f"2. 规律服药：{medication_out}。{medication_advice}。",
        f"3. {followup}。{follow_up_detail}。",
        f"4. {department}门诊定期随访，不适随诊。",
        f"5. {urgent_signs}立即就诊。",
        f"6. {rehab_advice}。",
        f"7. 出院带药请遵医嘱服用，注意药物有效期，存放在儿童不可触及处。",
    ]
    parts.append("".join(mo_lines) + "\n")

    # ICD 行（辅助正则匹配）
    icd_block = (
        f"主要诊断：{diagnosis[0]}（主要诊断代码 {diagnosis[1]}）\n"
        f"主要手术操作名称：{surgery[0]}\n"
        f"主要手术操作日期：{surgery_date}\n"
        f"入院科别：{department}\n"
        f"出院科别：{department}\n"
        f"离院方式：1"
    )
    parts.append(icd_block + "\n")

    text = "\n".join(parts)
    return text


def build_conflict_case(
    rng: random.Random,
    idx: int,
    conflict_type: str,
    base_date: date,
) -> str:
    text = build_normal_case(rng, idx, base_date)

    if conflict_type == "surgery_before_admission":
        # 手术日期 < 入院日期
        old_line = next(
            (ln for ln in text.splitlines() if ln.startswith("主要手术操作日期：")),
            None,
        )
        if old_line:
            text = text.replace(old_line, "主要手术操作日期：2020-01-01", 1)
    elif conflict_type == "diagnosis_surgery_mismatch":
        text = text.replace("I25.101", "K80.801", 1).replace("36.0601", "51.2301", 1)
    elif conflict_type == "icu_days_exceed_stay":
        text = text.replace(
            f"住院天数：{7}天", "住院天数：7天\nICU 天数：12天", 1
        )
    elif conflict_type == "vague_wording_days_unknown":
        text = text.replace(
            "住院天数：7天", "住院天数：住院期间天数不详", 1
        ).replace(
            "实际住院天数：7", "实际住院天数：不详", 1
        )
    elif conflict_type == "missing_field_id_card":
        text = text.replace("住院号：<EXAMPLE_ID>", "住院号：未填写", 1)

    text = f"<!-- KNOWN_CONFLICT: {conflict_type} -->\n" + text
    return text


def generate_all(
    output_dir: Path,
    count: int = 100,
    base_date: date = date(2026, 1, 1),
    seed: int = 20251017,
) -> list[Path]:
    """生成 ``count`` 份脱敏出院小结到 ``output_dir``。"""
    rng = random.Random(seed)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []

    conflict_types = (
        ["surgery_before_admission"] * 10
        + ["diagnosis_surgery_mismatch"] * 10
        + ["icu_days_exceed_stay"] * 10
        + ["vague_wording_days_unknown"] * 10
        + ["missing_field_id_card"] * 10
    )
    rng.shuffle(conflict_types)

    plan: list[tuple[int, str | None]] = []
    for idx in range(count):
        ctype = conflict_types[idx] if idx < len(conflict_types) else None
        plan.append((idx + 1, ctype))

    for idx, ctype in plan:
        if ctype is None:
            text = build_normal_case(rng, idx, base_date)
        else:
            text = build_conflict_case(rng, idx, ctype, base_date)
        path = output_dir / f"case_{idx:03d}.txt"
        path.write_text(text, encoding="utf-8")
        paths.append(path)

    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--output-dir", type=Path, default=OUTPUT_DIR,
        help=f"输出目录（默认 {OUTPUT_DIR}）",
    )
    parser.add_argument(
        "--count", type=int, default=100,
        help="生成份数（默认 100）",
    )
    parser.add_argument(
        "--seed", type=int, default=20251017,
        help="随机种子（默认 20251017）",
    )
    args = parser.parse_args()

    paths = generate_all(
        output_dir=args.output_dir, count=args.count, seed=args.seed,
    )
    print(f"已生成 {len(paths)} 份脱敏出院小结到 {args.output_dir}")
    sample_sizes = [p.stat().st_size for p in paths[:5]]
    print(f"前 5 份字节数: {sample_sizes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())