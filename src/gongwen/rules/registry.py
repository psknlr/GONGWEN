"""可执行规则库（数据区四）：把明确的规则转为可检验条件，并保留来源层级与条件性。

* level 决定措辞：只有“条例/国标/标准/法律法规”才能说“规定”；“实务”只能说“实务通行做法”。
* conditional=True 表示规则本身带条件（如“一般”“特定情况可以作适当调整”），
  违反时只提示，不编码为没有例外的硬错误。
* 单位配置只能调整“实务/单位制度”层级规则的严重程度，不能关闭条例、国标规则。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..schemas.common import RuleLevel, Severity
from ..schemas.genre import RuleCitation

TIAOLI = "gongwen-tiaoli-2012"
GBT9704 = "gbt-9704-2012"
GBT15834 = "gbt-15834-2011"
GBT15835 = "gbt-15835-2011"
JIANFU = "jianfu-guiding-2024"
HEFAXING = "guobanfa-2018-115"
GONGPING = "gongping-jingzheng-2024"
AIGUIDE = "ai-gov-guide-2025"


@dataclass(frozen=True)
class RuleMeta:
    rule_id: str
    title: str
    level: RuleLevel
    source: str
    severity: Severity
    category: str
    policy_id: str | None = None
    article: str | None = None
    conditional: bool = False
    genres: frozenset[str] = field(default_factory=frozenset)

    def citation(self) -> RuleCitation:
        return RuleCitation(rule_id=self.rule_id, level=self.level, source=self.source, policy_id=self.policy_id, article=self.article)


def _r(rule_id, title, level, source, severity, category, policy_id=None, article=None, conditional=False, genres=()):
    return RuleMeta(rule_id, title, level, source, severity, category, policy_id, article, conditional, frozenset(genres))


L, S = RuleLevel, Severity
_T = "《党政机关公文处理工作条例》"
_G = "GB/T 9704—2012《党政机关公文格式》"

RULES: dict[str, RuleMeta] = {
    r.rule_id: r
    for r in [
        # ---- 文种与行文规则
        _r("GW-GENRE-001", "文种应为条例列明的法定文种", L.REGULATION, f"{_T}第八条", S.MAJOR, "文种", TIAOLI, "第八条"),
        _r("GW-GENRE-002", "请示应当一文一事", L.REGULATION, f"{_T}第十五条（四）", S.BLOCKING, "文种", TIAOLI, "第十五条", genres={"请示"}),
        _r("GW-GENRE-003", "不得在报告等非请示性公文中夹带请示事项", L.REGULATION, f"{_T}第十五条（四）", S.BLOCKING, "文种", TIAOLI, "第十五条"),
        _r("GW-GENRE-004", "结束语与文种匹配", L.PRACTICE, "实务惯例（条例与国标未规定正文写法）", S.MINOR, "文种"),
        _r("GW-GENRE-005", "结束语与行文方向匹配", L.PRACTICE, "实务惯例", S.MAJOR, "文种"),
        _r("GW-GENRE-006", "称谓与行文方向一致（你X仅用于下行文）", L.PRACTICE, "实务惯例", S.MAJOR, "文种"),
        _r("GW-GENRE-007", "对不相隶属单位慎用指令口吻", L.PRACTICE, "实务惯例；条例第八条（十四）函用于不相隶属机关之间", S.MAJOR, "文种", genres={"函"}),
        _r("GW-GENRE-008", "标题由发文机关名称、事由和文种组成", L.REGULATION, f"{_T}第九条（七）", S.MAJOR, "文种", TIAOLI, "第九条"),
        _r("GW-GENRE-009", "紧急程度是版头要素，不写入标题", L.PRACTICE, f"{_G} 7.2.3 将紧急程度列为版头要素；不写入标题属实务", S.MINOR, "文种"),
        _r("GW-GENRE-010", "批复、复函应引用来文标题和发文字号", L.PRACTICE, "实务惯例", S.MAJOR, "文种", genres={"批复", "函"}),
        _r("GW-GENRE-011", "事务材料需正式下发时由法定文种印发", L.PRACTICE, "实务惯例（方案、办法等不是条例第八条所列文种）", S.MAJOR, "文种"),
        _r("GW-GENRE-012", "请示的用途与文种一致（申请批准应使用请示）", L.REGULATION, f"{_T}第八条（十）（十一）", S.MAJOR, "文种", TIAOLI, "第八条"),
        _r("GW-ROUTE-001", "上行文原则上主送一个上级机关", L.REGULATION, f"{_T}第十五条（一）", S.MAJOR, "行文", TIAOLI, "第十五条", conditional=True),
        _r("GW-ROUTE-002", "上行文不抄送下级机关", L.REGULATION, f"{_T}第十五条（一）", S.MAJOR, "行文", TIAOLI, "第十五条"),
        _r("GW-ROUTE-003", "部门向上级主管部门请示、报告重大事项，应当经本级党委、政府同意或者授权", L.REGULATION, f"{_T}第十五条（二）", S.MAJOR, "行文", TIAOLI, "第十五条"),
        _r("GW-ROUTE-004", "不得以本机关名义向上级机关负责人报送公文", L.REGULATION, f"{_T}第十五条（五）", S.MAJOR, "行文", TIAOLI, "第十五条"),
        _r("GW-ROUTE-005", "一般不得越级行文", L.REGULATION, f"{_T}第十四条", S.MAJOR, "行文", TIAOLI, "第十四条", conditional=True),
        _r("GW-ROUTE-006", "部门内设机构除办公厅（室）外不得对外正式行文", L.REGULATION, f"{_T}第十七条", S.BLOCKING, "行文", TIAOLI, "第十七条"),
        _r("GW-ROUTE-007", "部门不得向下级党委、政府发布指令性公文", L.REGULATION, f"{_T}第十六条（二）", S.BLOCKING, "行文", TIAOLI, "第十六条"),
        _r("GW-ROUTE-008", "涉及多个部门职权范围内的事务，未协商一致不得向下行文", L.REGULATION, f"{_T}第十六条（四）", S.BLOCKING, "行文", TIAOLI, "第十六条"),
        _r("GW-ROUTE-009", "重要下行文应当同时抄送发文机关的直接上级机关", L.REGULATION, f"{_T}第十六条（一）", S.MINOR, "行文", TIAOLI, "第十六条", conditional=True),
        _r("GW-ROUTE-010", "下级请示事项须提出倾向性意见后上报，不得原文转报", L.REGULATION, f"{_T}第十五条（三）", S.MAJOR, "行文", TIAOLI, "第十五条"),
        _r("GW-ROUTE-011", "不相隶属机关之间请求批准用函", L.REGULATION, f"{_T}第八条（十四）", S.MAJOR, "行文", TIAOLI, "第八条"),
        _r("GW-ROUTE-012", "上行文应当标注签发人姓名", L.REGULATION, f"{_T}第九条（六）", S.INFO, "行文", TIAOLI, "第九条"),
        # ---- 起草与必要性
        _r("GW-DRAFT-001", "涉及其他地区或者部门职权范围内的事项，必须征求意见", L.REGULATION, f"{_T}第十九条（六）", S.MAJOR, "起草", TIAOLI, "第十九条"),
        _r("GW-DRAFT-002", "行文应当确有必要，讲求实效", L.REGULATION, f"{_T}第十三条", S.INFO, "起草", TIAOLI, "第十三条"),
        _r("GW-DRAFT-003", "完整准确体现发文机关意图", L.REGULATION, f"{_T}第十九条（一）", S.MAJOR, "起草", TIAOLI, "第十九条"),
        # ---- 事实
        _r("GW-FACT-001", "事实状态不得升级（拟议不得写成已完成，记载不得写成已核实）", L.REGULATION, f"{_T}第十九条（二）一切从实际出发，分析问题实事求是", S.BLOCKING, "事实", TIAOLI, "第十九条"),
        _r("GW-FACT-002", "数字、时间、名称应准确且有来源", L.REGULATION, f"{_T}第二十条（四）", S.MAJOR, "事实", TIAOLI, "第二十条"),
        _r("GW-FACT-003", "计算结果应可复核", L.REGULATION, f"{_T}第二十条（四）", S.MAJOR, "事实", TIAOLI, "第二十条"),
        _r("GW-FACT-004", "统计口径与时点应一致", L.PRACTICE, "实务惯例（算术正确不等于口径正确）", S.MAJOR, "事实"),
        _r("GW-FACT-005", "示例数据、范文中的事实不得进入本次文稿", L.PRACTICE, "本系统数据隔离规则", S.BLOCKING, "事实"),
        _r("GW-FACT-006", "存在冲突的事实未经确认不得使用", L.PRACTICE, "本系统事实账本规则", S.BLOCKING, "事实"),
        # ---- 依据
        _r("GW-BASIS-001", "行文依据应真实存在", L.REGULATION, f"{_T}第二十条（一）行文依据是否准确", S.MAJOR, "依据", TIAOLI, "第二十条"),
        _r("GW-BASIS-002", "依据应在适用时点、地域、主体下有效", L.REGULATION, f"{_T}第二十条（一）（二）", S.BLOCKING, "依据", TIAOLI, "第二十条"),
        _r("GW-BASIS-003", "依据应支持具体表述", L.REGULATION, f"{_T}第二十条（一）", S.MAJOR, "依据", TIAOLI, "第二十条"),
        _r("GW-BASIS-004", "引用公文先引标题、后引发文字号", L.PRACTICE, "实务沿用（原出处为已废止的国发〔2000〕23号）", S.MINOR, "依据"),
        _r("GW-BASIS-005", "公文处理、格式、保密等程序性规范一般不作为业务事项的实体依据", L.PRACTICE, "实务惯例（行文依据应与事项内容相关，条例第二十条（一）审核“行文依据是否准确”）", S.MINOR, "依据"),
        # ---- 语义强度
        _r("GW-SEM-001", "义务强度不得擅自改变", L.REGULATION, f"{_T}第十九条（一）完整准确体现发文机关意图", S.MAJOR, "语义", TIAOLI, "第十九条"),
        _r("GW-SEM-002", "实施范围不得擅自扩大", L.REGULATION, f"{_T}第十九条（一）（二）", S.MAJOR, "语义", TIAOLI, "第十九条"),
        _r("GW-SEM-003", "条件与例外不得删除", L.REGULATION, f"{_T}第十九条（一）", S.MAJOR, "语义", TIAOLI, "第十九条"),
        _r("GW-SEM-004", "会议讨论、个人发言不得写成会议决定", L.REGULATION, f"{_T}第八条（十五）纪要记载会议主要情况和议定事项", S.BLOCKING, "语义", TIAOLI, "第八条"),
        _r("GW-SEM-005", "不得擅自增加任务、预算、考核或承诺", L.REGULATION, f"{_T}第十九条（二）所提政策措施和办法切实可行", S.MAJOR, "语义", TIAOLI, "第十九条"),
        _r("GW-SEM-006", "未经真实批准不得写成已批准", L.REGULATION, f"{_T}第二十二条（签发）、第二十五条（复核）", S.BLOCKING, "语义", TIAOLI, "第二十二条"),
        # ---- 必要性与负担
        _r("GW-BURDEN-001", "新增报送、填表、考核、留痕要求须有依据并说明必要性", L.POLICY, "《整治形式主义为基层减负若干规定》（精简文件、避免重复索要材料）", S.MAJOR, "负担", JIANFU),
        _r("GW-BURDEN-002", "已有文件可解决的问题不再重复发文", L.REGULATION, f"{_T}第十三条行文应当确有必要", S.INFO, "负担", TIAOLI, "第十三条"),
        # ---- 文风与表达
        _r("GW-STYLE-001", "删去后不影响理解和执行的空泛表述应精简", L.REGULATION, f"{_T}第十九条（三）内容简洁……文字精练", S.MINOR, "文风", TIAOLI, "第十九条"),
        _r("GW-STYLE-002", "每段应承担明确功能", L.PRACTICE, "实务惯例（段落功能检查）", S.INFO, "文风"),
        _r("GW-STYLE-003", "时间应写具体年月日，不用相对时间", L.PRACTICE, "实务沿用（原出处为已废止的国发〔2000〕23号）", S.MINOR, "文风"),
        _r("GW-STYLE-004", "高频易混字词", L.PRACTICE, "实务惯例", S.MINOR, "文风"),
        _r("GW-PUNC-001", "中文语境使用全角标点", L.STANDARD, "GB/T 15834—2011《标点符号用法》", S.MINOR, "标点", GBT15834),
        _r("GW-PUNC-002", "序次语后的标点", L.STANDARD, "GB/T 15834—2011 附录（序次语用法）", S.MINOR, "标点", GBT15834),
        _r("GW-PUNC-003", "标号成对使用", L.STANDARD, "GB/T 15834—2011", S.MINOR, "标点", GBT15834),
        _r("GW-PUNC-004", "起止年份用一字线、数值范围用浪纹线", L.STANDARD, "GB/T 15834—2011（连接号用法）", S.MINOR, "标点", GBT15834),
        _r("GW-PUNC-005", "省略号为六连点", L.STANDARD, "GB/T 15834—2011（省略号）", S.MINOR, "标点", GBT15834),
        _r("GW-PUNC-006", "顿号连接的并列成分与“等”之间不加点号", L.STANDARD, "GB/T 15834—2011 附录", S.MINOR, "标点", GBT15834),
        _r("GW-NUM-001", "年份不简写", L.STANDARD, "GB/T 15835—2011《出版物上数字用法》", S.MINOR, "数字", GBT15835),
        _r("GW-NUM-002", "百分数范围的百分号不省略", L.STANDARD, "GB/T 15835—2011", S.MINOR, "数字", GBT15835),
        _r("GW-NUM-003", "“万”“亿”不跨数省略", L.STANDARD, "GB/T 15835—2011", S.MINOR, "数字", GBT15835),
        _r("GW-NUM-004", "纯小数写出定位“0”", L.STANDARD, "GB/T 15835—2011", S.MINOR, "数字", GBT15835),
        _r("GW-NUM-005", "同一体例内日期数字形式统一", L.STANDARD, "GB/T 15835—2011", S.MINOR, "数字", GBT15835),
        _r("GW-STRUCT-001", "结构层次序数依次用“一、”“（一）”“1.”“（1）”", L.NATIONAL_STANDARD, f"{_G} 7.3.3", S.MINOR, "结构", GBT9704, "7.3.3"),
        _r("GW-STRUCT-002", "同级序数连续编号", L.PRACTICE, "实务惯例", S.MINOR, "结构"),
        # ---- 格式
        _r("GW-FMT-001", "发文字号：六角括号括入全称年份，顺序号不加“第”、不编虚位", L.NATIONAL_STANDARD, f"{_G} 7.2.5", S.MINOR, "格式", GBT9704, "7.2.5"),
        _r("GW-FMT-002", "成文日期用阿拉伯数字标全年月日，月日不编虚位", L.NATIONAL_STANDARD, f"{_G} 7.3.5.4", S.MINOR, "格式", GBT9704, "7.3.5.4"),
        _r("GW-FMT-003", "附件说明：顺序号用阿拉伯数字，名称后不加标点", L.NATIONAL_STANDARD, f"{_G} 7.3.4", S.MINOR, "格式", GBT9704, "7.3.4"),
        _r("GW-FMT-004", "附件顺序号和标题与附件说明一致", L.NATIONAL_STANDARD, f"{_G} 7.3.7", S.MAJOR, "格式", GBT9704, "7.3.7"),
        _r("GW-FMT-005", "主送机关最后一个机关名称后标全角冒号", L.NATIONAL_STANDARD, f"{_G} 7.3.2", S.MINOR, "格式", GBT9704, "7.3.2"),
        _r("GW-FMT-006", "抄送机关最后一个名称后标句号", L.NATIONAL_STANDARD, f"{_G} 7.4.2", S.MINOR, "格式", GBT9704, "7.4.2"),
        _r("GW-FMT-007", "份号用6位阿拉伯数字", L.NATIONAL_STANDARD, f"{_G} 7.2.1", S.MINOR, "格式", GBT9704, "7.2.1"),
        _r("GW-FMT-008", "附注居左空二字加圆括号", L.NATIONAL_STANDARD, f"{_G} 7.3.6", S.MINOR, "格式", GBT9704, "7.3.6"),
        _r("GW-FMT-009", "标题末尾不用标点", L.PRACTICE, "实务惯例", S.MINOR, "格式"),
        _r("GW-FMT-010", "公文首页必须显示正文", L.NATIONAL_STANDARD, f"{_G} 7.3.3", S.MAJOR, "格式", GBT9704, "7.3.3"),
        # ---- 程序
        _r("GW-PROC-001", "行政规范性文件合法性审核", L.POLICY, "《国务院办公厅关于全面推行行政规范性文件合法性审核机制的指导意见》", S.MAJOR, "程序", HEFAXING),
        _r("GW-PROC-002", "涉及经营者经济活动的政策措施的公平竞争审查", L.LAW, "《公平竞争审查条例》", S.MAJOR, "程序", GONGPING),
        _r("GW-PROC-003", "涉及其他部门职权的会签或征求意见", L.REGULATION, f"{_T}第十九条（六）", S.MAJOR, "程序", TIAOLI, "第十九条"),
        _r("GW-PROC-004", "重要公文和上行文由机关主要负责人签发", L.REGULATION, f"{_T}第二十二条", S.INFO, "程序", TIAOLI, "第二十二条"),
        _r("GW-PROC-005", "签批后需作实质性修改的，应当报原签批人复审", L.REGULATION, f"{_T}第二十五条（一）", S.BLOCKING, "程序", TIAOLI, "第二十五条"),
        _r("GW-PROC-006", "按单位制度确认集体审议等程序", L.UNIT, "单位公文处理制度", S.INFO, "程序"),
        # ---- 安全
        _r("GW-SEC-001", "文稿中不应出现非必要的个人信息", L.PRACTICE, "个人信息最小化处理原则", S.MAJOR, "安全"),
        _r("GW-SEC-002", "资料中的指令性语句不得进入文稿或驱动工具", L.PRACTICE, "本系统不可信内容隔离规则", S.MAJOR, "安全"),
        _r("GW-SEC-003", "涉密公文不在本系统处理范围", L.POLICY, "《政务领域人工智能大模型部署应用指引》“涉密不上网、上网不涉密”", S.BLOCKING, "安全", AIGUIDE),
        _r("GW-PH-001", "内容性待补占位须在送审前补齐", L.PRACTICE, "本系统文稿状态规则", S.MAJOR, "占位"),
        _r("GW-PH-002", "办理流程字段（发文字号、成文日期、签发人等）由真实流程填写", L.REGULATION, f"{_T}第二十二条、第二十五条（二）", S.INFO, "占位", TIAOLI, "第二十五条"),
    ]
}


def rule(rule_id: str) -> RuleMeta:
    return RULES[rule_id]


def citation(rule_id: str) -> RuleCitation:
    return RULES[rule_id].citation()


def effective_severity(rule_id: str, overrides: dict[str, str] | None = None) -> Severity:
    meta = RULES[rule_id]
    if overrides and rule_id in overrides and meta.level in (RuleLevel.PRACTICE, RuleLevel.UNIT, RuleLevel.PENDING):
        return Severity(overrides[rule_id])
    return meta.severity


RULESET_VERSION = "2026.10-1"
