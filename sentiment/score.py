"""⑤ 打分：把一个观点块判成「方向 + 置信度 + 证据原句」。

这是整条链路的心脏。架构方案第五节定的是**三层漏斗**：

    输入块 ┬─ ① 规则命中（约 60–70%）──→ 直接出方向，置信度高
           ├─ ② 规则未命中 / 冲突 ─────→ LLM 判断，出方向 + 置信度
           └─ ③ LLM 低置信度 ──────────→ 人工复核队列，结果回流成标注集

demo 版**只实现第一层（规则层）**，但把第二层的接口位留好（``score_with_llm``），
第三层用「置信度」这一列天然接上：``confidence < config.LOW_CONFIDENCE`` 的行
就是生产版要送去 LLM / 人工的队列，不需要额外建表。

为什么 demo 只做规则层：硬性约束是零第三方依赖 + 不联网，装不了任何 LLM SDK，
也调不了 API。而规则层本来就是这个场景里性价比最高的一层——期货日报的
「操作建议」栏高度模板化，句式就那么几十种，规则能吃下大半，且**完全可解释**：
交易员追问「凭什么说焦煤偏空」时，能直接把命中的原句甩给他。

★★ 本文件最需要小心的地方：中文否定与程度副词 ★★

调研文档 01 第三节的四个坑里，有两个直接落在这一层：

1. **「分析师不轻易喊空」** → 研报里大量出现「暂不看空」「谨慎追空」「前多保护
   利润」这种**变相偏多**的表达。它们字面上带「空」字，词袋法必然判反。
2. **「一段文本里有对立观点会被抹成中性」** → 已经由 ④ 切分层解决（切到
   一机构 × 一品种），本层只处理单个观点块。

因此规则引擎的执行顺序被固定成：

    特例表（固定搭配，直接命中） → 常规规则 → 通用否定反转 → 程度副词调强度

顺序不能换。若先跑「否定词 + 方向词」的通用逻辑，「暂不看空」会被当成
「看空的否定」处理，结果落在中性上——而它真实的含义是**偏多**。

★ 关于「分析师不轻易喊空」这个坑，本层做的是**语义那一半**：
  SPECIAL_RULES 专门吃「暂不看空 / 谨慎追空 / 前多保护利润」这类**变相偏多**的
  表达，不让字面上的「空」字把方向带反。这一半必须在打分层做，它是「判准方向」。

  **加权那一半（负面非对称加权）不在本层**，已落地在 ``aggregate._net_score``，
  由 ``config.BEAR_WEIGHT`` 控制、受 calc_version 管理（demo 版取 1.0 等权，
  理由见 config.BEAR_WEIGHT 的注释）。放在聚合层是因为它是个会反复调参的口径：
  打分表只追加不可修改（model_version 锁死口径），放在这里每调一次权重就要
  重跑一遍全量打分，生产版这一步要花 LLM 的钱。

★ 关于打分刻度：见下方 RULES 表，全项目统一，不得在别处另立一套。
"""

import re
import sqlite3
from dataclasses import dataclass

from . import config, db
from .models import Score, Segment

__all__ = [
    'RulePattern', 'RULES', 'NEGATIONS', 'INTENSIFIERS',
    'classify_advice', 'score_segment', 'score_pending', 'score_with_llm',
]

# ================================================================ 常量

#: 规则命中时写进 Score.method
METHOD_RULE = 'rule'

#: 规则**未**命中时 classify_advice 的返回标记。
#: 注意：它只在内存里流转，不写进 DB——models.py 约定 score.method 只有
#: rule / llm / human 三种取值。落库时统一写 'rule'，靠 confidence == 0.0
#: 标识「规则没吃下来」，生产版按这个条件捞出来转 LLM 层。
METHOD_RULE_MISS = 'rule_miss'

#: 否定词表。中文的否定几乎全靠这几个字，够用。
NEGATIONS = ['不', '勿', '莫', '无需', '不宜', '暂不', '不要', '难以']

#: 程度副词表：只调**强度与置信度**，绝不改变符号。
#: > 1 是增强（坚定做多 比 做多 更强），< 1 是减弱（谨慎逢低偏多 比 逢低偏多 更弱）。
INTENSIFIERS = {
    '坚定': 1.3, '强烈': 1.3, '大幅': 1.2, '积极': 1.15,
    '谨慎': 0.6, '略': 0.5, '小幅': 0.6, '轻仓': 0.7, '适度': 0.7,
}

#: 通用否定反转后的强度。**固定 0.5，不是简单地把原方向取反。**
#: 「不建议做多」不等于「坚定做空」——否定只表达「别往那个方向走」，
#: 信息量远小于一个正面主张，硬取反会把 -2.0 这种极端值凭空造出来。
#: 0.5 正好落在「谨慎偏空/偏多」这一档，与 难例 的口径（暂不看多 = -0.5）一致。
NEGATION_MAGNITUDE = 0.5

#: 通用否定反转后的置信度。低于 config.LOW_CONFIDENCE(0.6)，
#: 意味着这类推断出来的方向会**自动进复核队列**——这是有意的：
#: 固定搭配（特例表）可信，临时推断出来的否定不可信。
NEGATION_CONFIDENCE = 0.55

#: direction 的绝对值上限（刻度契约 -2.0 ~ +2.0）
MAX_ABS_DIRECTION = 2.0

#: 程度副词**减弱**后的绝对值下限。
#: 多空计数口径的分界线是 ±0.25，如果允许「谨慎/略」把 0.5 一路打到 0.2，
#: 副词就等于改变了符号语义（偏多变中性），违反「不改变符号」的约定。
MIN_SCALED_ABS = 0.3

#: advice_text 为空时，从 raw_text 尾部取多少字兜底（操作建议一般在块尾）
FALLBACK_TAIL_CHARS = 120

#: 「操作建议」栏的标签词。★ 退化路径（advice_text 为空，只能拿 raw_text 打分）
#: 的第一道闸门：先把原文截到最后一个这类标签之后，再打分。
#: 与 segment._STRONG_LABEL 是同一族标记，只是这里额外收了几个更松的写法——
#: 切分层抽不出建议时，这里能捡回来的就多捡一点。
#: (?<!构成) 挡的是免责声明里的「本报告不构成投资建议」，它排在正文最后，
#: 不挡就会把整块原文截成一段法律文本。
_ADVICE_LABEL_RE = re.compile(
    r'(?<!构成)(?:操作建议|操作策略|策略建议|交易策略|交易建议|投资建议|操作观点|'
    r'策略观点|操作思路|日内策略|短线策略|策略推荐|观点及建议|操作上|策略上)\s*[:：]?\s*'
)

#: 句子级分隔符，用来把证据切成完整原句
_SENT_SEP = frozenset('。！？；\n!?;')

#: 子句级分隔符，句子太长时用它收窄证据，同时也是「否定词回看窗口」的截断边界
_CLAUSE_SEP = frozenset('，。；、：！？,;:.!?（）()【】[]“”"\'《》\n\t·—-　 ')

#: 否定词回看窗口长度（字符）。再长就会跨过修饰对象抓到无关的「不」。
_NEG_WINDOW = 6

#: 子句切分符。★ 规则**逐子句**扫描（见 classify_advice 的第 0 步），
#: 「空头思路为主，反弹勿追多」这种复合建议才不会被第二个子句里的特例词劫走。
_CLAUSE_SPLIT_RE = re.compile(r'[，,；;。！？!?、\n\t]+')


# ================================================================ 规则表

@dataclass(frozen=True)
class RulePattern:
    """一条规则。frozen 是为了可哈希、且规则表不可被运行时篡改。"""

    pattern: str          # 正则（在「去空白 + ASCII 转小写」后的文本上匹配）
    direction: float      # -2.0 ~ +2.0
    confidence: float     # 0.0 ~ 1.0
    desc: str             # 中文说明，写进返回值的 'rule' 字段，供看板下钻展示


# ---------------------------------------------------------------- ① 特例组
# ★ 必须排在最前面。这一组是「固定搭配」，字面上带方向词但真实含义相反或减半，
#   通用逻辑一定会判错，所以直接硬编码。
#   ★ 特例命中后**不再套用否定层和程度副词层**——否定与副词已经写在 pattern 里，
#     再套一次等于打两遍折。
#
# ★ 一个关键的语义区分（这一组最容易写错的地方）：
#     「谨慎 + 追X」 = 别去追X   → 反向，例：谨慎追空 = +0.5
#     「谨慎 + 做X」 = 小心地做X → 同向减弱，例：谨慎做多 = +1.0 × 0.6 = +0.6
#   所以「谨慎」只允许出现在「追/杀」类动词前面参与反转，绝不能和「做/看」合并。
#
#: 否定/劝阻类前缀。抽成常量，多条特例规则共用，避免改了一处漏了另一处。
#: ★ 这一组**故意不收裸「不」**：「不看空 / 不做多」交给通用否定层处理就够了，
#:   而且必须交给它——通用否定层会数否定词个数，「不能不看空」这种双重否定
#:   在那里才会被正确地判成 −1.0；收进特例组就变成一次性反转，判成 +0.5。
_DISSUADE = r'(?:不宜|不要|不必|无需|难以|切勿|勿|莫)'

#: 「追X / 杀跌」类动词专用的劝阻前缀，比 _DISSUADE 多收「谨慎」和裸「不」。
#: - 「谨慎」：只能出现在这一族前面。「谨慎追空」= 别去追空（反转），
#:   而「谨慎做多」= 小心地做多（同向减弱），两者绝不能合并成一条规则。
#: - 裸「不」：「不追高，等回调」「不追空」在研报里是很常见的写法，而「追高/追空」
#:   不在任何常规规则里，不收的话这类句子既不命中特例也不命中常规，直接掉进
#:   rule_miss。备选项按长度降序排（不宜 / 不要 … 在前，不 在后），
#:   正则的备选是最左优先，长的排前面才不会被「不」抢先吃掉一个字。
_DISSUADE_CAUTIOUS = r'(?:谨慎|不宜|不要|不必|无需|难以|切勿|勿|莫|不)'

SPECIAL_RULES: list[RulePattern] = [
    # ---- 反转类：字面带「空」，真实偏多 ----
    RulePattern(r'(?:暂|暂时|短期内|目前)不(?:宜)?(?:看|做|追|沽|杀)空', 0.5, 0.80,
                '★难例：暂不看空 = 暂时不往空头方向想 → 谨慎偏多'),
    RulePattern(_DISSUADE_CAUTIOUS + r'(?:过度|过分|盲目|急于)?追空',
                0.5, 0.78,
                '★难例：谨慎追空 / 不追空 = 别去追空 → 谨慎偏多，绝不是看空'),
    RulePattern(_DISSUADE + r'(?:过度|过分|盲目|急于)?(?:看|做|沽|杀|建)空',
                0.5, 0.75,
                '不宜做空 / 无需过度看空 → 谨慎偏多'),
    RulePattern(_DISSUADE_CAUTIOUS + r'(?:过度|过分|盲目)?(?:追跌|杀跌)',
                0.5, 0.75,
                '不宜杀跌 = 别往下砸 → 谨慎偏多'),

    # ---- 反转类：字面带「多/高」，真实偏空 ----
    RulePattern(r'(?:暂|暂时|短期内|目前)不(?:宜)?(?:看|做|追|买)多', -0.5, 0.80,
                '★难例：暂不看多 → 谨慎偏空'),
    RulePattern(_DISSUADE_CAUTIOUS + r'(?:过度|过分|盲目|急于)?(?:追多|追高|追涨)',
                -0.5, 0.78,
                '★难例：不宜追高 / 不追高 = 别去追多 → 谨慎偏空'),
    RulePattern(_DISSUADE + r'(?:过度|过分|盲目|急于)?(?:看|做|买|建)多',
                -0.5, 0.75,
                '不宜做多 / 无需过度看多 → 谨慎偏空'),

    # ---- 持仓类：有仓位但转谨慎，方向保留、置信度压低 ----
    RulePattern(r'(?:前多|多单|多头持仓)(?:可)?(?:继续|逐步|部分)?(?:保护利润|保护盈利|止盈|减仓|减持|离场|平仓|落袋)',
                0.5, 0.55,
                '★难例：前多保护利润 = 持多但转谨慎 → 偏多，置信度压低'),
    RulePattern(r'(?:前空|空单|空头持仓)(?:可)?(?:继续|逐步|部分)?(?:保护利润|保护盈利|止盈|减仓|减持|离场|平仓|回补)',
                -0.5, 0.55,
                '前空保护利润 = 持空但转谨慎 → 偏空，置信度压低'),
    RulePattern(r'(?:前多|多单|多头持仓)(?:可)?(?:继续|逢低|暂时)?(?:持有|续持|持仓|捂住)',
                0.5, 0.72,
                '前多持有 = 有多单但不加仓 → 谨慎偏多'),
    RulePattern(r'(?:前空|空单|空头持仓)(?:可)?(?:继续|逢高|暂时)?(?:持有|续持|持仓)',
                -0.5, 0.72,
                '前空持有 → 谨慎偏空'),

    # ---- 试仓类：动作很轻，只给半档 ----
    RulePattern(r'(?:逢低|回调|企稳)?(?:轻仓|少量|小仓位|试探性)?试多', 0.5, 0.70,
                '逢低轻仓试多 = 只是试仓 → 谨慎偏多'),
    RulePattern(r'(?:逢高|反弹|冲高)?(?:轻仓|少量|小仓位|试探性)?试空', -0.5, 0.70,
                '逢高轻仓试空 = 只是试仓 → 谨慎偏空'),
]

# ---------------------------------------------------------------- ② 复合中性组
# ★ 必须排在方向规则前面：「高抛低吸」里含「高抛」，先让长的复合词占位，
#   否则会被当成「高抛 = 看空」，而它其实是标准的区间中性表述。
COMPOUND_NEUTRAL_RULES: list[RulePattern] = [
    RulePattern(r'高抛低吸|低吸高抛|高抛低补|高卖低买', 0.0, 0.85,
                '高抛低吸 = 区间来回做，无方向 → 中性'),
    RulePattern(r'区间(?:内)?(?:操作|震荡|运行|交易|对待|波动|整理|思路)', 0.0, 0.82,
                '区间操作 → 中性'),
    RulePattern(r'(?:多空|涨跌|上下)(?:交织|分歧|博弈|拉锯|两难)', 0.0, 0.75,
                '多空交织 = 分歧大，无共识 → 中性'),
    RulePattern(r'上有压力(?:下有支撑)?|下有支撑上有压力', 0.0, 0.70,
                '上有压力下有支撑 = 区间震荡 → 中性'),
]

# ---------------------------------------------------------------- ③ 强烈组 ±2.0
STRONG_RULES: list[RulePattern] = [
    # +2.0 强烈看多
    # ★ (?<!难) 是必须的：可选前缀 (?:以)? 会把否定词「难以」的第二个字「以」
    #   吃进匹配区间，通用否定层回看左窗时只看到残缺的「难」（不在 NEGATIONS 表里），
    #   于是「难以多头思路为主」被判成 +2.0，符号完全反转。
    #   这是「否定词与规则 pattern 抢字符」这一类问题的唯一实例——
    #   NEGATIONS 里只有「难以」是以「以」结尾的。
    RulePattern(r'(?<!难)(?:以)?多头(?:思路|格局|思维|策略)(?:为主|对待|操作)?', 2.0, 0.90,
                '多头思路为主 → 强烈看多'),
    RulePattern(r'(?:坚定|坚决|积极|大胆|果断)(?:做多|看多|买入|持多|加多|多配)', 2.0, 0.88,
                '坚定做多 → 强烈看多'),
    RulePattern(r'建议(?:积极|逢低|择机)?(?:做多|买入|多配|建多|多单入场)', 2.0, 0.85,
                '建议做多 → 强烈看多'),
    RulePattern(r'(?:做多|买入|看多)(?:思路|操作)?为主', 2.0, 0.85,
                '做多思路为主 → 强烈看多'),
    RulePattern(r'强烈看多|重点做多|全力做多', 2.0, 0.90,
                '强烈看多 → 强烈看多'),
    RulePattern(r'逢(?:低|回调)(?:积极|大胆|果断)(?:做多|买入|建多)', 2.0, 0.85,
                '逢低积极做多 → 强烈看多'),

    # -2.0 强烈看空
    RulePattern(r'(?<!难)(?:以)?空头(?:思路|格局|思维|策略)(?:为主|对待|操作)?', -2.0, 0.90,
                '空头思路为主 → 强烈看空'),
    RulePattern(r'(?:坚定|坚决|积极|大胆|果断)(?:做空|看空|沽空|抛空|持空|加空)', -2.0, 0.88,
                '坚定做空 → 强烈看空'),
    RulePattern(r'建议(?:积极|逢高|择机)?(?:做空|沽空|卖出|建空|空单入场)', -2.0, 0.85,
                '建议做空 → 强烈看空'),
    RulePattern(r'(?:做空|沽空|卖出|看空)(?:思路|操作)?为主', -2.0, 0.85,
                '做空思路为主 → 强烈看空'),
    RulePattern(r'强烈看空|重点做空|全力做空', -2.0, 0.90,
                '强烈看空 → 强烈看空'),
    RulePattern(r'逢(?:高|反弹)(?:积极|大胆|果断)(?:做空|沽空|建空)', -2.0, 0.85,
                '逢高积极做空 → 强烈看空'),
]

# ---------------------------------------------------------------- ④ 常规组 ±1.0
NORMAL_RULES: list[RulePattern] = [
    # +1.0 看多
    RulePattern(r'逢(?:低|回调|调整|企稳|回踩)(?:轻仓|适度|逐步)?(?:偏多|做多|买入|多配|建多|布多|布局多单|入多)',
                1.0, 0.82, '逢低偏多 / 逢低做多 → 看多'),
    # 「维持偏多」比裸「偏多」特异，必须排在它前面
    RulePattern(r'(?:维持|保持|延续|继续)(?:偏多|多头)(?:思路|格局|判断|观点)?', 1.0, 0.80,
                '维持多头格局 → 看多'),
    RulePattern(r'偏多(?:思路|操作|对待|看待|格局|判断|观点|运行)?(?:为主)?', 1.0, 0.80,
                '偏多思路 → 看多'),
    RulePattern(r'多单(?:轻仓|逐步)?(?:入场|进场|建仓|加仓|布局)', 1.0, 0.78,
                '多单入场 → 看多'),
    RulePattern(r'低吸(?:为主)?|低多(?:为主)?', 1.0, 0.72,
                '低吸 / 低多 → 看多'),
    RulePattern(r'看多|做多|买多|买入', 1.0, 0.70,
                '裸「看多/做多」→ 看多（置信度低于成句表述）'),

    # -1.0 看空
    RulePattern(r'逢(?:高|反弹|反抽|冲高|回升)(?:轻仓|适度|逐步)?(?:偏空|做空|沽空|抛空|放空|建空|布空|布局空单|入空|卖出|空)',
                -1.0, 0.82, '逢高沽空 / 逢高做空 → 看空'),
    RulePattern(r'(?:维持|保持|延续|继续)(?:偏空|空头)(?:思路|格局|判断|观点)?', -1.0, 0.80,
                '维持空头格局 → 看空'),
    RulePattern(r'偏空(?:思路|操作|对待|看待|格局|判断|观点|运行)?(?:为主)?', -1.0, 0.80,
                '偏空思路 → 看空'),
    RulePattern(r'空单(?:轻仓|逐步)?(?:入场|进场|建仓|加仓|布局)', -1.0, 0.78,
                '空单入场 → 看空'),
    RulePattern(r'高抛(?:为主)?|高空(?:为主)?', -1.0, 0.72,
                '高抛 / 高空 → 看空（已让「高抛低吸」先占位）'),
    RulePattern(r'看空|做空|沽空|抛空', -1.0, 0.70,
                '裸「看空/做空」→ 看空（置信度低于成句表述）'),
]

# ---------------------------------------------------------------- ⑤ 弱信号组 ±0.5
# 这一组是**行情倾向词**而不是明确的操作建议，置信度一律给 0.5（低于
# LOW_CONFIDENCE 阈值，会被标成待复核）。
# ★ 有意不收「回落 / 反弹 / 上涨 / 下跌」：这些词在「行情回顾」段落里描述的是
#   **昨天已经发生的事**，不是对后市的判断。advice_text 抽不到时会退回 raw_text
#   兜底，收了它们必然制造大量假信号。
WEAK_RULES: list[RulePattern] = [
    RulePattern(r'(?:震荡)?偏强(?:运行|震荡|整理|格局|走势)?|重心(?:逐步|震荡)?上移|易涨难跌',
                0.5, 0.50, '震荡偏强 / 重心上移 → 弱偏多（行情倾向，非操作建议）'),
    RulePattern(r'(?:震荡)?偏弱(?:运行|震荡|整理|格局|走势)?|重心(?:逐步|震荡)?下移|易跌难涨|承压(?:运行|下行)?',
                -0.5, 0.50, '震荡偏弱 / 重心下移 / 承压 → 弱偏空（行情倾向，非操作建议）'),
    RulePattern(r'逢高(?:减仓|止盈|减持|了结)', 0.5, 0.50,
                '逢高减仓：减的是多单，与「前多保护利润」同族 → 弱偏多'),
    RulePattern(r'逢低(?:回补|止盈|减空)', -0.5, 0.50,
                '逢低回补：补的是空单，与「前空保护利润」同族 → 弱偏空'),
]

# ---------------------------------------------------------------- ⑥ 中性组 0.0
NEUTRAL_RULES: list[RulePattern] = [
    RulePattern(r'(?:暂时|保持|建议|继续|以)?观望(?:为主|为宜|情绪)?', 0.0, 0.85,
                '观望为主 → 中性'),
    RulePattern(r'短(?:线|差|期)(?:操作|交易|参与|对待)|日内(?:交易|操作|短差)', 0.0, 0.80,
                '短差操作 / 日内交易 → 中性'),
    RulePattern(r'(?:暂|暂时)?不(?:宜)?(?:入场|参与|操作|介入)', 0.0, 0.72,
                '暂不宜入场 → 中性（不表达方向，只表达不动手）'),
    RulePattern(r'(?:等待|观察)(?:方向|指引|信号|时机|企稳|明朗)', 0.0, 0.72,
                '等待方向指引 → 中性'),
    RulePattern(r'(?:宽幅|高位|低位|区间|维持)?震荡(?:运行|整理|格局|走势|为主|调整|市)?', 0.0, 0.70,
                '震荡运行 → 中性（「震荡偏强/偏弱」已被弱信号组先吃掉）'),
    RulePattern(r'方向不明|缺乏(?:方向|驱动)|驱动不足', 0.0, 0.68,
                '方向不明 / 缺乏驱动 → 中性'),
    RulePattern(r'中性(?:对待|观点|看待)?', 0.0, 0.70,
                '中性对待 → 中性'),
]

#: ★ 全量规则表，**按特异性从高到低**拼装：
#:   特例（固定搭配） > 复合中性（长词占位） > 强烈 > 常规 > 弱信号 > 中性。
#:   匹配时**从上往下第一个命中的即返回**，所以顺序就是优先级，不能随意重排。
RULES: list[RulePattern] = (
    SPECIAL_RULES
    + COMPOUND_NEUTRAL_RULES
    + STRONG_RULES
    + NORMAL_RULES
    + WEAK_RULES
    + NEUTRAL_RULES
)

#: 预编译，避免每次打分重复编译正则
_COMPILED: list[tuple[re.Pattern, RulePattern]] = [
    (re.compile(r.pattern), r) for r in RULES
]

#: 特例组的 pattern 集合：命中特例时跳过否定层与副词层
_SPECIAL_PATTERNS: frozenset[str] = frozenset(r.pattern for r in SPECIAL_RULES)

#: 弱信号组的 pattern 集合：退化路径下要能整组关掉（allow_weak=False）
_WEAK_PATTERNS: frozenset[str] = frozenset(r.pattern for r in WEAK_RULES)

#: 否定词识别正则，按长度降序拼装（先认「不宜」再认「不」，避免重复计数）
_NEG_RE = re.compile('|'.join(sorted(NEGATIONS, key=len, reverse=True)))

#: 有歧义的单字程度副词要额外加边界约束。
#: 「策略上做多」里的「略」不是程度副词，不加这条会被打成 0.5 倍强度。
_INTENSIFIER_RE: dict[str, re.Pattern] = {
    '略': re.compile(r'(?<![策战忽省方])略'),
}


# ================================================================ 文本预处理

def _prepare(text: str) -> tuple[str, list[int]]:
    """把原文压成便于正则匹配的形式，同时保留「压缩后下标 → 原文下标」的映射。

    做两件事：
    1. 去掉全部空白字符——PDF 解析出来的中文常见「逢低 偏多」这种词内断空格，
       不去掉规则就白写了；
    2. ASCII 字母转小写（中文不受影响）。

    返回 ``(compact, index_map)``，``index_map[i]`` 是 ``compact[i]`` 在原文里的
    下标。有了它，命中位置才能映射回**原文**去截取证据句——证据必须是原句，
    不能是被处理过的字符串。
    """
    chars: list[str] = []
    index_map: list[int] = []
    for i, ch in enumerate(text):
        if ch.isspace():
            continue
        chars.append(ch.lower() if ch.isascii() else ch)
        index_map.append(i)
    return ''.join(chars), index_map


def _left_window(compact: str, start: int, size: int = _NEG_WINDOW) -> str:
    """取命中位置左边的一小段，且**在最近的标点处截断**。

    截断是关键：「不确定性上升，逢高沽空」里的「不」修饰的是「确定性」，
    跨过逗号去抓它会把看空判成看多。一个子句内的否定才作数。
    """
    window = compact[max(0, start - size):start]
    for i in range(len(window) - 1, -1, -1):
        if window[i] in _CLAUSE_SEP:
            return window[i + 1:]
    return window


def _negation_count(window: str) -> int:
    """数窗口里的否定词个数。双重否定（偶数个）等于没否定，所以要数而不是判有无。"""
    return len(_NEG_RE.findall(window))


def _intensity_factor(scope: str) -> float:
    """算程度副词的合成系数。多个副词叠乘（「谨慎轻仓」= 0.6 × 0.7）。"""
    factor = 1.0
    for word, value in INTENSIFIERS.items():
        regex = _INTENSIFIER_RE.get(word)
        hit = regex.search(scope) is not None if regex is not None else word in scope
        if hit:
            factor *= value
    return factor


def _sign(value: float) -> float:
    return 1.0 if value > 0 else -1.0


def _scale_direction(direction: float, factor: float) -> float:
    """按程度副词缩放强度。**符号绝不改变**，且缩放后仍留在 [0.3, 2.0] 区间内。"""
    magnitude = abs(direction) * factor
    magnitude = min(MAX_ABS_DIRECTION, max(MIN_SCALED_ABS, magnitude))
    return round(_sign(direction) * magnitude, 2)


def _scale_confidence(confidence: float, factor: float) -> float:
    """增强词提高置信度，减弱词降低置信度（「谨慎」本身就是分析师在打折）。"""
    return round(min(0.99, max(0.05, confidence * (0.6 + 0.4 * factor))), 2)


def _extract_evidence(text: str, start: int, end: int, max_len: int = 60) -> str:
    """从**原文**里截出命中片段所在的完整句子，作为证据。

    交易员追问「凭什么」时，这一句就是答案，所以宁可多带上下文也不能只给关键词。
    句子太长（超过 max_len）时退一步，收窄到逗号级子句，保证看板一行放得下。
    """
    n = len(text)
    left = start
    while left > 0 and text[left - 1] not in _SENT_SEP:
        left -= 1
    right = end
    while right < n and text[right] not in _SENT_SEP:
        right += 1
    sentence = text[left:right].strip()
    if len(sentence) <= max_len:
        return sentence

    clause_left, clause_right = start, end
    while clause_left > left and text[clause_left - 1] not in _CLAUSE_SEP:
        clause_left -= 1
    while clause_right < right and text[clause_right] not in _CLAUSE_SEP:
        clause_right += 1
    clause = text[clause_left:clause_right].strip() or sentence
    if len(clause) > max_len * 2:
        clause = clause[:max_len * 2] + '…'
    return clause


def _tail_sentence(text: str, max_len: int = 60) -> str:
    """取最后一个非空句子。规则没命中时拿它当证据，人工复核时能看到原话。"""
    stripped = (text or '').strip()
    if not stripped:
        return ''
    if len(stripped) <= max_len:
        return stripped
    return _extract_evidence(stripped, len(stripped) - 1, len(stripped), max_len)


def _tail_text(text: str, size: int = FALLBACK_TAIL_CHARS) -> str:
    """取块尾部若干字，并**从第一个句子边界之后开始**，避免切出半句话。"""
    stripped = (text or '').strip()
    if len(stripped) <= size:
        return stripped
    fragment = stripped[-size:]
    for i, ch in enumerate(fragment):
        if ch in _SENT_SEP:
            return fragment[i + 1:].strip() or fragment.strip()
    return fragment.strip()


def _miss_result(text: str) -> dict:
    """规则未命中的返回值。★ confidence = 0.0 是「规则没吃下来」的标记位。"""
    return {
        'direction': 0.0,
        'confidence': 0.0,
        'evidence': _tail_sentence(text),
        'matched': '',
        'rule': '',
        'negated': False,
        'method': METHOD_RULE_MISS,
    }


# ================================================================ 核心引擎

def _clause_spans(text: str) -> list[tuple[int, int]]:
    """把原文切成子句区间 ``[(start, end), ...]``（不含分隔符本身）。

    切子句是为了让规则**在一个子句内**竞争。期货日报的操作建议经常是复合句：
    「空头思路为主，反弹勿追多。」——两个子句表达的是**同一个方向**，
    主张写在第一个子句里，第二个子句只是补充。若在整句上按规则表优先级扫，
    排在最前面的特例组会先在第二个子句里命中「勿追多」（−0.5 谨慎偏空），
    把第一个子句里那句 −2.0 的强烈看空整个盖掉，强度直接掉两档。
    """
    spans: list[tuple[int, int]] = []
    cursor = 0
    for match in _CLAUSE_SPLIT_RE.finditer(text):
        if match.start() > cursor:
            spans.append((cursor, match.start()))
        cursor = match.end()
    if cursor < len(text):
        spans.append((cursor, len(text)))
    return spans


def _classify_span(raw: str, start: int, end: int, allow_weak: bool = True) -> dict | None:
    """在 ``raw[start:end]`` 这一个子句里跑完整的规则表，没命中返回 None。

    :param allow_weak: 是否让 WEAK_RULES（行情倾向词）参与匹配。
        退化路径下传 False——见 :func:`classify_advice` 的说明。

    ★ 证据句始终从**完整原文** ``raw`` 里按句边界截取，不是从子句里截——
      交易员看到的必须是研报里那句完整的话。
    """
    compact, index_map = _prepare(raw[start:end])
    if not compact:
        return None

    for regex, rule in _COMPILED:
        if not allow_weak and rule.pattern in _WEAK_PATTERNS:
            continue
        match = regex.search(compact)
        if match is None:
            continue

        lo, hi = match.span()
        origin_start = start + index_map[lo]
        origin_end = start + index_map[hi - 1] + 1
        fragment = raw[origin_start:origin_end]

        direction = rule.direction
        confidence = rule.confidence
        negated = False

        # 特例组自带否定与副词，跳过后续两层；中性块没有方向可调，同样跳过
        if rule.pattern not in _SPECIAL_PATTERNS and direction != 0.0:
            window = _left_window(compact, lo)
            if _negation_count(window) % 2 == 1:
                # 通用否定：方向反转，强度统一压到谨慎档
                negated = True
                direction = -_sign(direction) * NEGATION_MAGNITUDE
                confidence = NEGATION_CONFIDENCE
            else:
                factor = _intensity_factor(window + match.group(0))
                if factor != 1.0:
                    direction = _scale_direction(direction, factor)
                    confidence = _scale_confidence(confidence, factor)

        return {
            'direction': round(direction, 2),
            'confidence': round(confidence, 2),
            'evidence': _extract_evidence(raw, origin_start, origin_end),
            'matched': fragment,
            'rule': rule.desc,
            'negated': negated,
            'method': METHOD_RULE,
        }
    return None


def classify_advice(text: str, *, allow_weak: bool = True,
                    prefer_last: bool = False) -> dict:
    """核心规则引擎：把一句操作建议判成方向 + 置信度 + 证据。

    :param allow_weak: 是否让 WEAK_RULES（承压运行 / 震荡偏弱 / 重心下移 这类
        **行情倾向词**）参与匹配。默认 True。
        ★ 退化路径（advice_text 为空，只能整段 raw_text 打分）必须传 False，
        原因见下面「两个开关」。
    :param prefer_last: 多个子句都有方向时，取**最后**一个而不是第一个。
        默认 False。★ 同样只给退化路径用。

    执行顺序（**不能换**）：

    0. **逐子句扫描**：先按「，；。！？」把建议切成子句，**第一个命中的子句说了算**。
       期货研报的操作建议把主张写在最前面，后面的子句是补充说明
       （「空头思路为主，反弹勿追多」）。不切子句的话，规则表最前面的特例组
       会先在后半句命中「勿追多」，把前半句 −2.0 的强烈看空压成 −0.5。
    1. **特例表**：「谨慎追空 / 暂不看空 / 不宜追高 / 前多保护利润」这类固定
       搭配直接命中，命中后不再进否定层和副词层。
       若先跑通用否定逻辑，「暂不看空」会被拆成「不 + 看空」而被反转成中性，
       但它真实的含义是**偏多**——这是本项目最容易判反、也最致命的一类错误。
    2. **常规规则表**：按特异性从高到低扫，第一个命中即返回。
    3. **通用否定反转**：命中片段左侧一个子句内出现奇数个否定词 → 方向反转，
       强度统一压到 ±0.5，置信度压到 0.55（推断出来的方向不如固定搭配可信）。
    4. **程度副词**：调 direction 的绝对值和 confidence，**不改变符号**。

    返回::

        {'direction': float,   # -2.0 ~ +2.0
         'confidence': float,  # 0.0 ~ 1.0
         'evidence': str,      # ★ 证据原句，下钻用
         'matched': str,       # 命中的原文片段
         'rule': str,          # 命中的规则说明（可解释性）
         'negated': bool,      # 是否被通用否定层反转过
         'method': 'rule' | 'rule_miss'}

    匹配不到任何规则 → direction=0.0, confidence=0.0, method='rule_miss'。
    ★ 这里的 0 和「中性规则命中的 0」含义完全不同：前者是「不知道」，后者是
    「确实看平」。靠 confidence 区分——0.0 表示规则没吃下来，生产版这批要转
    LLM 层，绝不能当成中性票投进聚合里。

    ★★ 两个开关为什么存在（allow_weak / prefer_last）★★

    它们只服务于一种输入：**整段 raw_text**（切分层没抽到操作建议时的退化路径）。
    一段完整的观点块长这样::

        行情回顾：昨日焦煤主力收于 1,042 元/吨，跌 3.2%。
        逻辑分析：库存连续累积，旺季需求未兑现，板块承压运行。
        操作建议：逢低偏多，关注 1,034 一线支撑。

    上面第 0 步「第一个有方向的子句说了算」在这里会**整个判反**：
    「承压运行」是 WEAK_RULES 里的 −0.5，它出现在**逻辑分析**段里，
    排在真正的操作建议「逢低偏多」(+1.0) 前面，于是 +1.0 被判成 −0.5。

    根因是 WEAK_RULES 收的是**行情倾向词**而不是操作建议，它们大量出现在
    行情回顾/逻辑分析段落里描述盘面，本来就不该在有明确操作建议时参与竞争。
    所以退化路径按三档处理（见 :func:`score_segment`）：
    先截「操作建议：」之后的文本正常打分 → 截不到就 allow_weak=False + 取最后一个
    有方向的子句 → 还是没有，才允许弱信号兜底。
    """
    raw = text or ''
    if not raw.strip():
        return _miss_result(raw)

    spans = _clause_spans(raw)
    results = [_classify_span(raw, start, end, allow_weak) for start, end in spans]
    hits = [r for r in results if r is not None]

    # ★ 有方向的子句优先于中性子句。
    #   「行情回顾：昨日震荡走高。操作建议：逢低偏多。」里，「震荡」会命中中性规则，
    #   若按顺序取第一个命中，就把块尾真正的操作建议挡在外面了。
    #   中性的含义是「没有方向」，它不该压过一个明确的方向表述。
    # ★ 注意这一步比较的是**每个子句各自的最高优先级命中**，不是把整张规则表
    #   重扫一遍找非零值：否则「高抛低吸」会绕开复合中性组去命中「高抛」，
    #   区间策略被判成看空。
    directional = [r for r in hits if r['direction'] != 0.0]
    if directional:
        # prefer_last：期货日报的主张一律收在块尾，前面是行情回顾与逻辑分析。
        # 对**单句建议**必须取第一个（「空头思路为主，反弹勿追多」的主张在前半句），
        # 对**整段原文**必须取最后一个——两种输入的文体结构正好相反。
        return directional[-1] if prefer_last else directional[0]
    if hits:
        return hits[-1] if prefer_last else hits[0]

    # 兜底：子句级一个都没命中，再在整段上扫一遍。
    # 规则里没有跨子句的模式，正常不会有额外命中；留着是为了「压缩空白后才连成词」
    # （PDF 抽出来的「逢低\n偏多」）这类畸形排版，宁可多扫一遍也不白丢信号。
    if len(spans) != 1:
        result = _classify_span(raw, 0, len(raw), allow_weak)
        if result is not None:
            return result

    return _miss_result(raw)


def _advice_after_label(raw: str) -> str:
    """截出「操作建议：」标签之后的文本，没有标签返回空串。

    这是退化路径的第一道闸门。整段原文的结构是
    「行情回顾 → 逻辑分析 → 操作建议」，前两段全是**描述盘面**的话
    （承压运行 / 震荡偏弱 / 重心下移），拿它们去判方向必然把观点判反。
    先按标签把原文截到真正的建议那一段，问题就从源头没了。

    同类标签出现多次时取**最靠后**的：前面出现的往往是「昨日操作建议回顾」。
    """
    matches = list(_ADVICE_LABEL_RE.finditer(raw))
    if not matches:
        return ''
    return raw[matches[-1].end():].strip()


def score_segment(segment: Segment) -> Score:
    """给一个观点块打分，产出可入库的 Score。

    取文本的优先级（**顺序是有讲究的，见下**）：

    1. ``advice_text``——切分层抽出来的「操作建议」原句，最干净，优先用；
    2. ``raw_text`` 里「操作建议：」标签之后的那一段——切分层没抽到时，
       在打分层再按标签截一次；
    3. ``raw_text`` 的**尾部** 120 字，且**关掉弱信号组、取最后一个有方向的子句**；
    4. 整块原文，同样关掉弱信号组；
    5. 最后才允许弱信号（行情倾向词）兜底——总比白白丢掉一个信号强，
       但它的置信度只有 0.5，天然落在人工复核队列里。

    ★★ 第 3、4 档为什么要关掉弱信号组 ★★
    WEAK_RULES 收的是「承压运行 / 震荡偏弱 / 重心下移」这类**行情倾向词**，
    它们几乎全部出现在块首的「逻辑分析」段落里，描述的是盘面而不是主张。
    不关掉的话，一个块尾写着「逢低偏多」(+1.0) 的块会被块首的「板块承压运行」
    抢先命中，判成 −0.5——**符号完全反转**，而且指标不会报错，只会悄悄变错。
    这是本文件唯一一处会整段判反的地方，三道闸门（截标签 / 关弱信号 / 取最后一个
    有方向的子句）任何一道单独都能挡住它，这里三道全上。

    ★ evidence 一律填**真实原句**（从原文按句边界截出来的），这是可解释下钻的
      唯一来源。规则没命中时也填块尾原句，好让人工复核看到引擎面对的是什么。

    ★ 落库的 method 恒为 'rule'（models.py 只允许 rule/llm/human）；
      「规则没吃下来」用 confidence == 0.0 表达。
    """
    # (文本, classify_advice 的关键字参数)
    candidates: list[tuple[str, dict]] = []
    advice = (segment.advice_text or '').strip()
    if advice:
        candidates.append((advice, {}))

    raw = (segment.raw_text or '').strip()
    if raw:
        # ★ 退化路径：整段原文里前两段是行情回顾/逻辑分析，不能直接拿去判方向
        degraded = {'allow_weak': False, 'prefer_last': True}
        labeled = _advice_after_label(raw)
        if labeled:
            candidates.append((labeled, {}))
        tail = _tail_text(raw)
        if tail and tail != labeled:
            candidates.append((tail, degraded))
        if raw != tail:
            candidates.append((raw, degraded))
        # 最后一档：明确的操作建议一句都没找到，才让行情倾向词说话
        candidates.append((raw, {'prefer_last': True}))

    result = _miss_result('')
    for text, options in candidates:
        result = classify_advice(text, **options)
        if result['method'] == METHOD_RULE:
            break

    return Score(
        segment_id=int(segment.id) if segment.id is not None else 0,
        direction=float(result['direction']),
        confidence=float(result['confidence']),
        evidence=result['evidence'],
        method=METHOD_RULE,
        model_version=config.SCORE_VERSION,
    )


def score_pending(conn: sqlite3.Connection, model_version: str | None = None) -> dict:
    """批量给「还没有当前口径打分」的观点块打分并入库。

    ★ 只追加，不修改历史打分行：口径升版（改 SCORE_VERSION）后再跑一次，
      新旧两个版本的打分并存，随时能重算历史、对比差异。

    返回统计::

        {'total': 待打分块数, 'scored': 实际入库条数,
         'rule_hit': 规则命中数, 'rule_miss': 未命中数,
         'rule_hit_rate': 命中率 0~1,
         'low_confidence': 置信度低于阈值的条数（★ 生产版转 LLM / 人工的队列长度）,
         'bull'/'bear'/'neutral': 按 ±0.25 口径的方向分布,
         'model_version': 本次打分的口径版本号}
    """
    version = model_version or config.SCORE_VERSION
    rows = db.list_unscored_segments(conn, version)

    stats = {
        'total': len(rows),
        'scored': 0,
        'rule_hit': 0,
        'rule_miss': 0,
        'rule_hit_rate': 0.0,
        'low_confidence': 0,
        'bull': 0,
        'bear': 0,
        'neutral': 0,
        'model_version': version,
    }

    for row in rows:
        segment = _row_to_segment(row)
        score = score_segment(segment)
        score.model_version = version
        db.insert_score(conn, score)

        stats['scored'] += 1
        if score.confidence <= 0.0:
            stats['rule_miss'] += 1
        else:
            stats['rule_hit'] += 1
        if score.confidence < config.LOW_CONFIDENCE:
            stats['low_confidence'] += 1
        if score.direction > config.BULL_CUTOFF:
            stats['bull'] += 1
        elif score.direction < config.BEAR_CUTOFF:
            stats['bear'] += 1
        else:
            stats['neutral'] += 1

    if stats['total']:
        stats['rule_hit_rate'] = round(stats['rule_hit'] / stats['total'], 4)
    return stats


def _row_to_segment(row: sqlite3.Row) -> Segment:
    """DB 行 → Segment。db.list_unscored_segments 返回的是 segment.* + org_name。"""
    return Segment(
        report_id=int(row['report_id']),
        product_code=row['product_code'],
        product_name=row['product_name'],
        sector=row['sector'],
        raw_text=row['raw_text'] or '',
        advice_text=row['advice_text'] or '',
        seq=int(row['seq'] or 0),
        id=int(row['id']),
    )


# ================================================================ 第二层：LLM（占位）

def score_with_llm(text: str) -> dict:
    """三层漏斗的第二层：规则没命中或多条规则冲突时，交给 LLM 判。**demo 不实现。**

    为什么 demo 不做
    ----------------
    1. **不能联网**：demo 靠自带样本数据离线跑通全链路，调不了任何远程 API；
    2. **不能装 SDK**：硬性约束是零第三方依赖（只用 Python 3.12 标准库），
       openai / dashscope / zhipuai 这些包一个都装不了；
    3. 而且**不该先做**：规则层能吃下期货日报「操作建议」栏的大半，先把规则的
       命中率和难例跑准，才知道 LLM 真正要兜的是哪一类句子——反过来先上 LLM，
       等于花钱买一个不知道边界在哪的黑箱。

    生产版怎么做（接口位已经留好，替换本函数实现即可）
    ------------------------------------------------
    - **模型选型**：DeepSeek / Qwen / GLM 的 API。中文金融文本表现够用，
      成本远低于 GPT 系；日均百篇 × 每篇十几个块的量级，费用可以忽略。
    - **输入**：只喂**一个观点块**（一机构 × 一品种），绝不喂整篇。整篇喂进去
      会把「沪铜看多、螺纹看空」压成中性——RavenPack 官方文档承认了这个局限。
    - **输出强制 JSON**（response_format=json_object + few-shot）::

          {"direction": -2 ~ +2,      // 与本文件 RULES 同一把刻度
           "confidence": 0 ~ 1,
           "evidence": "原文里的一句话"}

    - **★ 必须要求返回证据原句，且校验它确实是原文子串**；对不上就判失败重试。
      没有证据的分数在金融场景等于废分——交易员一定会追问「凭什么」。
    - **难例写进 system prompt**：把「谨慎追空 = 偏多」「暂不看空 = 偏多」
      「不宜追高 = 偏空」这几条直接举例，这是 LLM 最容易跟着字面判反的地方。
    - **落库**：``Score(method='llm', model_version='deepseek-chat@2026-09')``，
      模型或 prompt 一变就升版本号，老打分行原样保留，历史随时可重算。
    - **兜底**：API 超时/JSON 解析失败 → 退回规则层结果并标记低置信度，
      流水线绝不能因为一次调用失败就断掉。
    - **第三层**：``confidence < config.LOW_CONFIDENCE`` 的进人工复核队列，
      人工判完的结果回流成标注集，用来迭代规则表和 prompt。

    :raises NotImplementedError: demo 版恒抛出。
    """
    raise NotImplementedError(
        'demo 版不接 LLM：不能联网、不能装 SDK。'
        '生产版在此调用 DeepSeek/Qwen/GLM 并强制返回 {方向, 置信度, 证据句} 的 JSON，'
        '详见本函数 docstring。'
    )
