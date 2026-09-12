"""④ 切分：把一篇研报切成「一个机构 × 一个品种 × 一个观点」的块。

★★ 这是全链路最关键的一步 ★★

一篇期货日报的典型结构是**多品种平铺**::

    【贵金属】沪金沪银  行情回顾… 逻辑分析… 操作建议：暂不看空
    【有色】沪铜        行情回顾… 逻辑分析… 操作建议：谨慎逢低偏多
    【黑色】螺纹钢      行情回顾… 逻辑分析… 操作建议：逢高沽空

整篇打一个分，等于把三个方向相反的观点搅成一杯浆糊——RavenPack 官方文档也承认
整块打分会把对立观点抹成中性。**切错了，后面打分再准也没用**（把沪铜的观点记到
沪铝头上，指标就是错的，而且错得看不出来）。

--------------------------------------------------------------------------
双重锚定策略（架构方案第四节）
--------------------------------------------------------------------------

1. **标题层级粗切**：``【xxx】``、Markdown ``##``、``一、``/``1.`` 三级级联，
   哪一级能切出 ≥2 块就用哪一级；都切不出来才退到「段落 + 品种锚定」。
2. **品种词典校验归一**：用 ``products.match_products()`` 在块里匹配品种。
   标题命中优先（标题是最强锚，且干净）；标题没命中才看正文，
   且正文命中要过「出现在开头 or 出现在 ≥2 句」的门槛——
   否则「沪铝受**沪铜**带动走强」这种顺带提及会把沪铜的票投错地方。

四个分支，一个都不能少：

===============  ==========================================================
一块 1 个品种     正常 Segment
一块多个品种     ★ 每个品种各生成一个 Segment。先试着按段落拆开
                 （「沪铜：…／沪铝：…」这种能拆，各自带各自的操作建议）；
                 拆不开（「沪金沪银」合并叙述）才共享同一段 raw_text，
                 并在 raw_text 里明确标注这是多品种块，下钻时不会误导人
一块 0 个品种     ★ 宏观/综述块（product_code=None）。**不能丢**——
                 「今日商品整体偏强，多头思路为主」是对大盘的直接判断，
                 价值比单品种块还高
免责声明/报头     丢弃。免责声明也匹配不到品种，若当宏观块留下来，
                 就会用一段法律文本去投大盘情绪的票
===============  ==========================================================

--------------------------------------------------------------------------
一篇之内同品种只出一块（配合聚合层的「一家一票」）
--------------------------------------------------------------------------

聚合层的去重口径是「同机构同日同品种取最新一篇研报」，它挡得住**跨篇**重复，
挡不住**同篇**重复：一篇日报里沪铜在【有色】和【品种小结】各出现一次，
就会让这家机构对沪铜投两票。所以在这里就把同一 report 内同 product_code 的块
**合并成一块**（拼 raw_text、取更可信的 advice），而不是简单丢弃——
丢弃会丢掉下钻要用的原文。宏观块同理，一篇最多合成一块。
"""

import re

from . import db
from .models import Segment
from .products import match_products

# ---------------------------------------------------------------- 阈值常量

#: 宏观块最短长度：比这短的无品种块基本是标题行、页码、栏目名，不是观点
MIN_MACRO_LEN = 30

#: 报头（文件开头、第一个小标题之前那段）当作宏观块的最短长度。
#: 比宏观块更严，因为报头绝大多数是「XX期货 商品日报 2026-09-09」这类元信息
MIN_PREAMBLE_LEN = 60

#: 正文锚定（标题没命中品种时）最多认几个品种。超过这个数说明是综述块在点名
#: 一堆品种（「今日铜铝锌铅镍锡普涨」），此时按宏观块处理，不给每个品种投票
MAX_BODY_PRODUCTS = 3

#: advice_text 截断长度，防止把一整段当成操作建议
MAX_ADVICE_LEN = 120

#: 标题行里跟在 ``【xxx】`` 后面、参与品种锚定的最大字符数
MAX_TITLE_TAIL = 24

# ---------------------------------------------------------------- 正则

#: 【xxx】yyy —— 最常见的小节标题。正文可能紧跟在同一行（架构方案第四节的例子）
_BRACKET_HEAD = re.compile(r'^\s*[【\[［]\s*(?P<inner>[^】\]］\n]{1,20}?)\s*[】\]］]\s*(?P<tail>.*)$')

#: Markdown 标题 —— 生产版 MinerU 输出的形态
_MD_HEAD = re.compile(r'^\s*#{1,6}\s+(?P<title>\S.{0,60})$')

#: 一、/（一）/1. —— 纯文本研报的常见编号标题，整行必须短，避免吃到正文里的编号列表
_NUM_HEAD = re.compile(
    r'^\s*(?:[一二三四五六七八九十]{1,3}[、．.]'
    r'|[（(][一二三四五六七八九十\d]{1,3}[）)]'
    r'|\d{1,2}[、．.)])\s*(?P<title>\S.{0,30})$'
)

#: 标题尾巴的截断点。★ 不含「、」和「/」：「沪金、沪银」「铜/铝」是多品种标题的
#: 正常写法，在这里截断会把第二个品种整个丢掉
_TITLE_CUT = re.compile(r'[\s，,。：:；;（(]')

#: 免责声明/声明类噪声块特征词
#: ★ BUG-06：原先只收「据此操作，风险自担」这类**完整搭配**，机构把它拆开写
#:   （「据此操作」「风险自担」分在两句）或换成「本报告不保证准确性」就整条漏掉，
#:   于是 `【风险提示】` 这类块不被认成声明块。第二行起是补的**独立**特征词。
_DISCLAIMER = re.compile(
    r'免责声明|分析师声明|版权所有|不构成投资建议|据此操作，风险自担|'
    r'投资咨询业务资格|证监许可|期货从业资格|本报告的著作权|联系我们|客服电话|'
    r'风险自担|风险自负|自行承担|概不负责|不承担任何|不保证.{0,6}(?:准确|完整|可靠)|'
    r'仅供参考|据此入市|请投资者|投资者应当|风险提示如下'
)

#: 声明语特征：这些短语在真实操作建议里几乎不可能出现，一旦出现在候选 advice 里，
#: 说明抽到的是法律文本而不是观点。
#: ★ 刻意**不收**「谨慎」「风险」「注意」——它们在真实建议里极常见
#:   （「谨慎逢低偏多」「注意回调风险」），收进来会误杀真实观点。
#:   判定失败的代价不对称：漏杀只是少覆盖一次，误杀会丢掉真实方向，所以宁窄勿宽。
_ADVICE_DISCLAIMER = re.compile(
    r'风险自担|风险自负|自行承担|自担风险|概不负责|不承担|不保证|不构成|'
    r'仅供参考|据此操作|据此入市|请投资者|投资者应|谨慎决策|市场有风险|入市需谨慎'
)

#: 免责声明**段落起点**。研报的免责声明永远挂在最后、且从不带小节标题，
#: 会被粗切原封不动地粘到最后一个品种块尾巴上（沪铜的操作建议因此变成
#: 「据此操作，风险自担」）。所以在切块之前先把这条尾巴整段砍掉。
_DISCLAIMER_START = re.compile(
    r'^\s*[【\[［]?\s*(?:免责声明|重要声明|法律声明|特别声明|分析师声明|免责条款|版权声明)',
    re.M,
)

#: 报头特征词（机构名 + 报告类型 + 日期那一坨）
_HEADER_HINT = re.compile(r'日报|周报|晨报|晨会|早评|月报|专题报告|研究所|发布时间|发布日期|分析师[:：]|投资咨询号')

#: 宏观/综述类小节标题特征词，判「无品种块到底是不是宏观块」用
_MACRO_TITLE = re.compile(
    r'宏观|综述|概述|总览|汇总|要闻|资讯|市场|大盘|大势|摘要|导读|热点|焦点|金融|海外|全球'
)

#: 操作建议标签（强）：期货日报的操作建议栏高度模板化，命中率最高的一批。
#: ★ 前置 (?<!构成) 是为了挡掉免责声明里的「本报告不构成投资建议」——
#:   它排在正文最后，不挡就会盖掉真正的操作建议。
_STRONG_LABEL = re.compile(
    r'(?<!构成)(?:操作建议|操作策略|策略建议|交易策略|交易建议|投资建议|操作观点|策略观点|'
    r'操作思路|日内策略|短线策略|策略推荐|观点及建议)\s*[:：]?\s*'
)

#: 操作建议标签（弱）：必须带冒号，否则「建议关注库存」这种句子会被误当成标签
_WEAK_LABEL = re.compile(r'(?:建议|策略|观点|操作上|思路|展望|小结|总结)\s*[:：]\s*')

#: 退化抽取时，认为「像操作建议」的线索词
_ADVICE_HINT = ('建议', '策略', '思路', '操作', '为主', '持有', '观望')

#: 句末标点
_SENT_END = set('。！？；!?;')

#: advice 开头要剃掉的装饰符
_ADVICE_LEAD = re.compile(r'^[\s★☆●•·◆■※\-—–*#>》、,，。:：]+')


# ---------------------------------------------------------------- 小工具

def _split_sentences(text: str) -> list[str]:
    """按句末标点 + 换行切句，保留标点。切句是「正文品种锚定」和「退化抽建议」的基础。"""
    out: list[str] = []
    buf: list[str] = []
    for ch in text:
        if ch == '\n':
            piece = ''.join(buf).strip()
            if piece:
                out.append(piece)
            buf = []
            continue
        buf.append(ch)
        if ch in _SENT_END:
            piece = ''.join(buf).strip()
            if piece:
                out.append(piece)
            buf = []
    tail = ''.join(buf).strip()
    if tail:
        out.append(tail)
    return out


def _clean_advice(text: str) -> str:
    """清掉装饰符与多余空白，并截断到 MAX_ADVICE_LEN。"""
    text = _ADVICE_LEAD.sub('', text.strip())
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) > MAX_ADVICE_LEN:
        text = text[:MAX_ADVICE_LEN]
    return text


def _first_sentence_after(text: str, pos: int) -> str:
    """取 ``text[pos:]`` 的第一句。

    首句过短（如「操作建议：谨慎。」）时补上下一句，避免把「谨慎」这种
    没有方向的半截话交给打分层——它会被判成中性，把真实观点抹平。
    """
    sentences = _split_sentences(text[pos:])
    if not sentences:
        return ''
    advice = sentences[0]
    if len(advice) < 6 and len(sentences) > 1:
        advice = advice + sentences[1]
    return _clean_advice(advice)


def _extract_advice(text: str) -> str:
    """抽「操作建议」原句。

    四级退化，一级比一级不可靠，但保证**永远有东西给打分层**：

    1. 强标签（操作建议／操作策略／交易策略…）后面的第一句——覆盖绝大多数日报；
    2. 弱标签（建议：／策略：／观点：）后面的第一句；
    3. 块内最后一个含线索词（建议/思路/为主/观望…）的句子；
    4. 块内最后一个非空句子（研报的结论几乎总在最后）。

    同类标签出现多次时取**最靠后**的那个：研报里「操作建议」通常收在段尾，
    前面出现的往往是「昨日操作建议回顾」。
    """
    for pattern in (_STRONG_LABEL, _WEAK_LABEL):
        matches = list(pattern.finditer(text))
        # ★ BUG-06 同类防线：块尾常直接跟一段无标题声明（「操作建议：本建议仅供参考」），
        #   只看 matches[-1] 会抽到法律文本。改为从后往前回退，取第一个「剔掉声明句后
        #   还剩东西」的候选。无声明语时逐字等价于原来的 matches[-1] 逻辑。
        for match in reversed(matches):
            advice = _drop_disclaimer_sentences(_first_sentence_after(text, match.end()))
            if advice:
                return advice

    sentences = _split_sentences(text)
    if not sentences:
        return ''
    # ★ 退化路径同样要跳过声明句，否则块尾的「本报告不构成投资建议」会成为方向来源
    clean = [s for s in sentences if not _looks_like_disclaimer_advice(s)]
    if not clean:
        return ''
    for sentence in reversed(clean):
        if any(hint in sentence for hint in _ADVICE_HINT):
            return _clean_advice(sentence)
    return _clean_advice(clean[-1])


def _is_disclaimer(text: str) -> bool:
    """是不是免责声明/联系方式这类噪声块。"""
    return bool(_DISCLAIMER.search(text))


def _looks_like_disclaimer_advice(advice: str) -> bool:
    """候选操作建议是不是法律声明语（BUG-06 的结构性防线）。

    ★ 为什么要有这道防线：标题白名单（`_DISCLAIMER_START`）永远补不完——
      风险提示 / 重要提示 / 特别提示 / 风险揭示 / 投资者须知……而且大量免责声明
      **根本没有标题**。但声明语本身的措辞高度固定，**按内容判定比按标题判定可靠**。

    ★ 为什么不直接把「风险提示」加进 `_DISCLAIMER_START`：那个函数是从匹配位置
      **砍掉后面全部内容**。真实研报里 `【风险提示】` 常作为每个品种的分析小节出现在
      中部（「风险提示：需求不及预期」），从那里砍下去会把后面所有品种的观点全删光。
    """
    return bool(advice) and bool(_ADVICE_DISCLAIMER.search(advice))


def _drop_disclaimer_sentences(advice: str) -> str:
    """从候选建议里**按句剔掉**声明语，而不是整段拒绝。

    ★ 为什么必须按句剔而不是整段判：`_first_sentence_after` 在原文没有换行时
      会带出后面若干句，「操作建议：逢低偏多。本报告不构成投资建议。」就是一个候选。
      整段判声明会把真实建议「逢低偏多」一起丢掉（实测会让打分退化到法律文本）；
      按句剔则只丢掉后半句，留下「逢低偏多。」。

    剔干净后为空说明整条候选都是法律文本，返回空串让调用方继续往前回退。
    """
    if not advice:
        return ''
    kept = [s for s in _split_sentences(advice) if not _looks_like_disclaimer_advice(s)]
    return _clean_advice(''.join(kept)) if kept else ''


def _strip_disclaimer_tail(text: str) -> str:
    """砍掉文末的免责声明段。

    免责声明不带小节标题，粗切时会被当成上一块的续写粘到**最后一个品种块**上，
    于是「不构成投资建议，据此操作，风险自担」就成了原油的操作建议——
    这类错误很隐蔽，指标不会报错，只会悄悄变错。

    只在声明出现在全文靠后（>30%）时才砍：少数机构把声明放在页眉，
    从头砍会把整篇研报删光。放在开头的那种交给按块过滤去处理。
    """
    for match in _DISCLAIMER_START.finditer(text):
        if match.start() > len(text) * 0.3:
            return text[:match.start()].strip()
    return text


def _is_macro_title(anchor: str) -> bool:
    """标题看起来是不是「宏观/综述」类栏目。"""
    return bool(anchor) and bool(_MACRO_TITLE.search(anchor))


# ---------------------------------------------------------------- ①标题粗切

class _Block:
    """粗切后的一块。``anchor`` 是标题里参与品种锚定的那截文本（无标题时为空）。"""

    __slots__ = ('anchor', 'text')

    def __init__(self, anchor: str, text: str) -> None:
        self.anchor = anchor
        self.text = text

    def __repr__(self) -> str:            # pragma: no cover - 调试用
        return f'_Block(anchor={self.anchor!r}, text={self.text[:30]!r}...)'


def _title_tail(tail: str) -> str:
    """从 ``【xxx】`` 后面同一行的内容里，截出「还算标题」的那一小截。

    ``【贵金属】沪金沪银  行情回顾…`` → ``沪金沪银``；
    ``【有色】沪铜：昨日铜价…``       → ``沪铜``。
    截断是为了不让正文里顺带提到的品种混进标题锚。
    """
    tail = tail.strip()
    if not tail:
        return ''
    match = _TITLE_CUT.search(tail)
    if match:
        tail = tail[:match.start()]
    return tail[:MAX_TITLE_TAIL].strip()


def _bracket_anchor(line: str) -> str | None:
    """行首是 ``【xxx】`` 则返回锚文本，否则 None。

    锚文本的取法分两种，为的是**尽量少带正文进来**：
    - 括号内本身就是品种（``【沪铜】``）→ 只取括号内，最干净；
    - 括号内是板块/栏目（``【有色】``）→ 取括号内 + 后面截断的一小截标题。
    """
    match = _BRACKET_HEAD.match(line)
    if not match:
        return None
    inner = match.group('inner').strip()
    if not inner:
        return None
    if match_products(inner):
        return inner
    return f'{inner} {_title_tail(match.group("tail"))}'.strip()


def _find_heads(lines: list[str], kind: str) -> list[tuple[int, str]]:
    """按指定层级找标题，返回 ``[(行号, 锚文本), ...]``。"""
    heads: list[tuple[int, str]] = []
    for idx, line in enumerate(lines):
        if kind == 'bracket':
            anchor = _bracket_anchor(line)
        elif kind == 'md':
            match = _MD_HEAD.match(line)
            anchor = match.group('title').strip(' #') if match else None
        else:
            match = _NUM_HEAD.match(line)
            anchor = match.group('title').strip() if match else None
        if anchor:
            heads.append((idx, anchor))
    return heads


def _blocks_from_heads(lines: list[str], heads: list[tuple[int, str]]) -> list[_Block]:
    """按标题行号把全文切块；第一个标题之前的部分单独当「报头」处理。"""
    blocks: list[_Block] = []

    preamble = _clean_preamble('\n'.join(lines[:heads[0][0]]))
    if preamble:
        blocks.append(_Block('', preamble))

    for i, (line_no, anchor) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(lines)
        body = '\n'.join(lines[line_no:end]).strip()
        if body:
            blocks.append(_Block(anchor, body))
    return blocks


def _clean_preamble(preamble: str) -> str:
    """处理第一个小标题之前那段（报头 / 摘要），返回要保留的内容，丢弃则返回空串。

    「永安期货 商品早评日报 / 发布日期：2026-09-09 / 分析师：张三」这种元信息必须丢——
    它匹配不到品种，留下来就是个宏观块，等于用一行页眉去投大盘情绪的票。

    但报头后面**可能接着一段真正的摘要**（「今日商品整体偏强……」），那是对大盘的
    直接判断，丢了可惜。所以逐行剥：从上往下把带报头特征词的行、以及过短的行
    （机构名、日期、栏目名）剥掉，遇到第一行有实质内容的长句就停手；
    剩下的够长才当宏观块留下。
    """
    if _is_disclaimer(preamble):
        return ''
    lines = [line for line in preamble.split('\n') if line.strip()]
    idx = 0
    while idx < len(lines) and (_HEADER_HINT.search(lines[idx]) or len(lines[idx].strip()) < 20):
        idx += 1
    body = '\n'.join(lines[idx:]).strip()
    return body if len(body) >= MIN_PREAMBLE_LEN else ''


def _blocks_by_paragraph(text: str) -> list[_Block]:
    """兜底切法：没有任何标题层级时，按段落 + 品种锚定切。

    规则很简单：**段首命中的品种变了就开新块**，命中不到品种的段落并入当前块。
    这样单品种专题报告会自然地合成一块，而「一段讲一个品种」的无标题日报
    也能切开。
    """
    paragraphs = [p.strip() for p in text.split('\n') if p.strip()]
    if not paragraphs:
        return []

    blocks: list[_Block] = []
    current_code: str | None = None
    buffer: list[str] = []
    anchor = ''

    for para in paragraphs:
        hits = match_products(para)
        code = hits[0]['code'] if len(hits) == 1 else None
        if code and code != current_code and buffer:
            blocks.append(_Block(anchor, '\n'.join(buffer)))
            buffer, anchor = [], ''
        if code and code != current_code:
            current_code = code
            anchor = para[:MAX_TITLE_TAIL]
        buffer.append(para)

    if buffer:
        blocks.append(_Block(anchor, '\n'.join(buffer)))
    return blocks


def _split_blocks(text: str) -> list[_Block]:
    """标题层级三级级联 + 段落兜底。

    级联而不是「三种标题混着找」，是为了避开假标题：正文里的
    「1、供应端…… 2、需求端……」如果和 ``【有色】`` 混在一起找，
    一个品种块会被劈成好几截。只要 ``【】`` 这一级切得出 ≥2 块，
    就不去看更弱的层级。
    """
    lines = text.split('\n')
    for kind in ('bracket', 'md', 'num'):
        heads = _find_heads(lines, kind)
        if len(heads) >= 2:
            return _blocks_from_heads(lines, heads)
    return _blocks_by_paragraph(text)


# ---------------------------------------------------------------- ②品种锚定

def _body_products(text: str) -> list[dict]:
    """正文级品种锚定（只在标题没命中品种时才用）。

    门槛：**出现在首句，或在 ≥2 个句子里出现过**。

    - 首句命中 = 「沪铜方面，昨日……」这种把品种写在开头的无标题块，可信；
    - 多句命中 = 整块反复在讲同一个品种，可信；
    - 只在中段出现一次 = 顺带提及，必须挡掉。「能化受**原油**拖累偏弱」出现在
      一段市场综述里，放行就等于把一整段大盘判断记成原油的观点，
      既污染了原油，又丢掉了一个宏观块。
    """
    sentences = _split_sentences(text)
    if not sentences:
        return []

    hit_sentences: dict[str, int] = {}
    first_seen: dict[str, int] = {}
    prod_by_code: dict[str, dict] = {}
    for idx, sentence in enumerate(sentences):
        for prod in match_products(sentence):
            code = prod['code']
            prod_by_code[code] = prod
            hit_sentences[code] = hit_sentences.get(code, 0) + 1
            first_seen.setdefault(code, idx)

    kept = [code for code in hit_sentences
            if hit_sentences[code] >= 2 or first_seen[code] == 0]
    kept.sort(key=lambda code: (first_seen[code], code))
    return [prod_by_code[code] for code in kept]


def _resolve_products(block: _Block) -> tuple[list[dict], str]:
    """给一块定品种，返回 ``(品种列表, 锚定来源)``，来源 ∈ {'title','body','none'}。

    锚定来源要一路带到去重环节：标题锚定的块比正文锚定的可信得多，
    同品种撞车时优先信标题。
    """
    if block.anchor:
        hits = match_products(block.anchor)
        if hits:
            return hits, 'title'
        # ★ 标题写着「宏观 / 市场综述」就一定是宏观块，不许正文里的品种把它抢走。
        #   「【宏观】市场综述 …… 今日原油领涨……」如果被判成原油块，会一箭双雕地错：
        #   原油凭空多一票，大盘还少了一个最该有的判断。
        if _is_macro_title(block.anchor):
            return [], 'none'

    hits = _body_products(block.text)
    if not hits:
        return [], 'none'
    if len(hits) > MAX_BODY_PRODUCTS:
        # 「今日铜铝锌铅镍锡普涨」——这是综述，不是六个品种各自的观点
        return [], 'none'
    return hits, 'body'


# ---------------------------------------------------------------- ③多品种块

def _mark_multi(text: str, products: list[dict]) -> str:
    """给共享 raw_text 的多品种块打标注。

    标注写进 raw_text 而不是只存在代码里，是因为**下钻时交易员会看到这段原文**：
    不写清楚「这段话是沪金沪银共用的」，他会以为系统把沪银的原文抓错了。
    """
    names = '、'.join(p['name'] for p in products)
    return (f'[多品种合并块｜{len(products)} 个品种：{names}｜'
            f'以下原文为这些品种共享，未能按段落拆开]\n{text}')


def _split_multi_by_paragraph(block: _Block, products: list[dict]) -> list[tuple[dict, str]] | None:
    """尝试把多品种块按段落拆成各自独立的块，拆不了返回 None。

    能拆的典型形态（各品种各有各的操作建议，拆开后打分才准）::

        【有色】沪铜、沪铝
        沪铜：库存持续去化，操作建议：逢低偏多。
        沪铝：成本塌陷，操作建议：逢高沽空。

    拆不了的典型形态（合并叙述，一句话里两个品种）::

        【贵金属】沪金沪银
        昨日沪金沪银同步走强……操作建议：暂不看空。

    判定条件严到几乎不会误拆：任何一个段落同时讲到两个标题品种就立刻放弃，
    且每个标题品种都必须有属于自己的段落。**宁可共享，也不能拆错。**
    """
    lines = [ln for ln in block.text.split('\n') if ln.strip()]
    if len(lines) < 3:                       # 标题行 + 至少两段
        return None

    head_line, paragraphs = lines[0], lines[1:]
    codes = {p['code'] for p in products}
    owned: dict[str, list[str]] = {}
    shared_prefix: list[str] = []
    current: str | None = None

    for para in paragraphs:
        hits = [p['code'] for p in match_products(para) if p['code'] in codes]
        if len(hits) > 1:
            return None                      # 一段里讲了多个品种 → 不可拆
        if hits:
            current = hits[0]
            owned.setdefault(current, []).append(para)
        elif current:
            owned[current].append(para)      # 无品种的续段跟着上一个品种走
        else:
            shared_prefix.append(para)       # 还没出现任何品种 → 公共前言

    if set(owned) != codes:
        return None                          # 有品种没分到段落 → 不可拆

    parts: list[tuple[dict, str]] = []
    for prod in products:
        body = [head_line] + shared_prefix + owned[prod['code']]
        parts.append((prod, '\n'.join(body)))
    return parts


# ---------------------------------------------------------------- ④去重编号

class _Draft:
    """入库前的草稿块。``source`` 记锚定来源，用于同品种撞车时判谁更可信。"""

    __slots__ = ('product', 'raw_text', 'advice', 'source', 'order')

    def __init__(self, product: dict | None, raw_text: str, advice: str,
                 source: str, order: int) -> None:
        self.product = product
        self.raw_text = raw_text
        self.advice = advice
        self.source = source
        self.order = order

    def rank(self) -> tuple:
        """可信度排序键：标题锚定 > 正文锚定；有操作建议 > 没有；靠后 > 靠前。

        「靠后的赢」是因为研报的结论通常在后面（前面那次往往是「昨日回顾」）。
        """
        return (self.source == 'title', bool(self.advice), self.order)


def _merge_drafts(base: _Draft, extra: _Draft) -> _Draft:
    """把同一 report 内同品种（或同为宏观）的两块合并成一块。

    合并而不是丢弃：raw_text 拼起来，下钻时该机构对该品种说过的话一句不少；
    advice / source 取更可信的那一份。
    """
    winner, loser = (base, extra) if base.rank() >= extra.rank() else (extra, base)
    merged_text = f'{base.raw_text}\n\n{extra.raw_text}'
    return _Draft(winner.product, merged_text, winner.advice or loser.advice,
                  winner.source, base.order)


def _finalize(report_id: int, drafts: list[_Draft]) -> list[Segment]:
    """同一 report 内按 product_code 去重合并，再按出场顺序编 seq。"""
    slots: list[_Draft] = []
    index: dict[str, int] = {}

    for draft in drafts:
        key = draft.product['code'] if draft.product else '__MACRO__'
        if key in index:
            pos = index[key]
            slots[pos] = _merge_drafts(slots[pos], draft)
        else:
            index[key] = len(slots)
            slots.append(draft)

    segments: list[Segment] = []
    for seq, draft in enumerate(slots):
        prod = draft.product
        advice = draft.advice or _extract_advice(draft.raw_text)
        segments.append(Segment(
            report_id=report_id,
            product_code=prod['code'] if prod else None,
            product_name=prod['name'] if prod else None,
            sector=prod['sector'] if prod else None,
            raw_text=draft.raw_text,
            advice_text=advice,
            seq=seq,
        ))
    return segments


# ---------------------------------------------------------------- 主入口

def split_segments(report_id: int, text: str) -> list[Segment]:
    """把一篇研报切成「一个机构 × 一个品种」的观点块。

    流程：标题粗切 → 逐块品种锚定 → 多品种拆分/标注 → 同品种合并 → 编号。

    :param report_id: 所属研报 id
    :param text: ``parse.parse_report`` 输出的规范化纯文本
    :return: 按 seq 升序的 Segment 列表；空文本返回空列表
    """
    text = _strip_disclaimer_tail((text or '').strip())
    if not text:
        return []

    drafts: list[_Draft] = []
    order = 0
    last_group: list[_Draft] = []      # 上一块产出的草稿（多品种块会有好几份）
    seen_product = False               # 全文是否已经出现过品种块

    for block in _split_blocks(text):
        body = block.text.strip()
        if not body:
            continue

        products, source = _resolve_products(block)

        # 免责声明/联系方式：标题锚定到品种的块可以留（正常块里带风险提示很常见），
        # 其余一律丢——不能让一段法律文本冒充宏观观点去投大盘的票
        if _is_disclaimer(body) and source != 'title':
            continue

        # ---------- 无品种块：宏观块 or 上一个品种的续写 ----------
        if not products:
            if len(body) < MIN_MACRO_LEN:
                continue                      # 太短，是栏目名/页码之类的碎片
            # ★ 「匹配不到品种」不等于「宏观」。沪铜专题里的「三、库存与价差」
            #   一个品种字都没有，但它显然是沪铜观点的一部分，不是对大盘的判断。
            #   判据：标题像宏观栏目（宏观/综述/要闻…），或它出现在任何品种块之前
            #   （开头的摘要、市场综述）——两者都不满足就并回上一个品种块。
            if _is_macro_title(block.anchor) or not seen_product:
                draft = _Draft(None, body, _extract_advice(body), 'none', order)
                drafts.append(draft)
                order += 1
                last_group = [draft]
            elif last_group and last_group[0].product is not None:
                cont_advice = _extract_advice(body) if _STRONG_LABEL.search(body) else ''
                # ★ BUG-06：文末【风险提示】里的「操作建议：据此操作风险自担」
                #   会把上一个品种的真实方向整个盖掉（逢低偏多 → 逢高沽空，符号翻转）。
                #   这里只拦 advice 覆盖，raw_text 照常拼接——
                #   下钻看原文时该机构说过的话一句不少，但方向不被法律文本污染。
                if _looks_like_disclaimer_advice(cont_advice):
                    cont_advice = ''
                for draft in last_group:
                    draft.raw_text = f'{draft.raw_text}\n{body}'
                    if cont_advice:           # 续写里带显式「操作建议：」才覆盖
                        draft.advice = cont_advice
            continue

        seen_product = True

        # ---------- 单品种块 ----------
        if len(products) == 1:
            draft = _Draft(products[0], body, _extract_advice(body), source, order)
            drafts.append(draft)
            order += 1
            last_group = [draft]
            continue

        # ---------- 多品种块 ----------
        parts = _split_multi_by_paragraph(block, products) if source == 'title' else None
        group: list[_Draft] = []
        if parts:                             # 拆得开：各品种各自的原文与操作建议
            for prod, part_text in parts:
                group.append(_Draft(prod, part_text, _extract_advice(part_text), source, order))
                order += 1
        else:                                 # 拆不开：共享原文 + 明确标注
            marked = _mark_multi(body, products)
            advice = _extract_advice(body)
            for prod in products:
                group.append(_Draft(prod, marked, advice, source, order))
                order += 1
        drafts.extend(group)
        last_group = group

    return _finalize(report_id, drafts)


def segment_pending(conn) -> dict:
    """对「已解析成功但还没切分」的研报批量切分入库。

    返回统计::

        {'reports': 处理篇数, 'segments': 入库块数,
         'product': 品种块数, 'macro': 宏观块数,
         'multi': 多品种块产生的块数, 'empty': 一块都没切出来的篇数}

    ``db.list_unsegmented`` 只取 ``parse_status='ok'`` 且尚无 segment 的研报，
    所以本函数天然幂等，可以反复跑。
    """
    rows = db.list_unsegmented(conn)
    stats = {'reports': 0, 'segments': 0, 'product': 0, 'macro': 0, 'multi': 0, 'empty': 0}

    for row in rows:
        report_id = int(row['id'])
        segments = split_segments(report_id, row['parsed_text'] or '')
        stats['reports'] += 1
        if not segments:
            stats['empty'] += 1
            continue
        for seg in segments:
            db.insert_segment(conn, seg)
            stats['segments'] += 1
            if seg.product_code is None:
                stats['macro'] += 1
            else:
                stats['product'] += 1
                if seg.raw_text.startswith('[多品种合并块'):
                    stats['multi'] += 1
    return stats
