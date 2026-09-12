"""⑦ 呈现层：推送文案 / 终端渲染 / 多空矩阵 / 单文件 HTML 看板。

对应架构方案第七节。这一层是整条流水线**唯一被用户看见的部分**，
所以三条设计原则写在最前面：

1. **三级信息结构**（CNN Fear & Greed 的信息架构 + AlphaSense 的引用要求）::

       大盘情绪分  →  板块 / 品种的多空构成  →  支撑它的研报原句

   交易员看到「焦煤 +65」的第一反应一定是「谁说的、凭什么」。
   答不上来这个指标就没人用，所以证据原句必须能一路下钻到格子里。

2. **热度与方向分离**。coverage_count（多少家发了研报）单独展示，
   不揉进净得分里：3 家看多和 30 家看多不该同权。

3. **NA ≠ 0**。当日没有机构覆盖的品种在 daily_index 里根本不存在记录，
   本层一律渲染成**空白格**而不是 0/中性，避免「没人说」被读成「大家都看中性」。

配色沿用国内习惯：**红 = 看多，绿 = 看空**（与欧美相反，别改）。

零第三方依赖：ANSI 转义序列手写，SVG 手写，CSS 内联，全程只用标准库。
"""

import html as _html
from datetime import date as _date
from pathlib import Path

from . import aggregate as aggregate_mod
from . import config, db
from . import products as products_mod

# ---------------------------------------------------------------- 展示常量

#: 终端渲染的总宽度（显示列数，中文按 2 列算）。锁死 100 列，窄终端也不折行。
TERM_WIDTH = 100

#: 终端矩阵最多展示的机构列数 / 品种行数，超出只提示不硬塞
MAX_MATRIX_ORGS = 8
MAX_MATRIX_ROWS = 20

#: 方向 → 矩阵符号。阈值与 config.BULL_CUTOFF 对齐，「强」档取 |1.5|
_STRONG_CUTOFF = 1.5

# ANSI 颜色。红=多、绿=空，与国内行情软件一致。
_RESET = '\033[0m'
_BOLD = '\033[1m'
_DIM = '\033[2m'
_BULL = '\033[38;5;203m'      # 红：看多
_BEAR = '\033[38;5;78m'       # 绿：看空
_FLAT = '\033[38;5;245m'      # 灰：中性
_HEAD = '\033[38;5;180m'      # 暖棕：标题
_WARN = '\033[38;5;214m'      # 橙：告警


# ---------------------------------------------------------------- 宽度工具
# 中文/全角字符占 2 个显示列，不处理的话所有表格都会错位。
# unicodedata 不在允许依赖内，这里直接按码点区间判断，够用且零依赖。

_WIDE_RANGES = (
    (0x1100, 0x115F),      # 朝鲜文字母
    (0x2E80, 0x303E),      # CJK 部首 + 中文标点（，。、「」）
    (0x3041, 0x33FF),      # 假名 + CJK 兼容
    (0x3400, 0x4DBF),      # CJK 扩展 A
    (0x4E00, 0x9FFF),      # CJK 基本区（绝大多数汉字）
    (0xA000, 0xA4CF),      # 彝文
    (0xAC00, 0xD7A3),      # 谚文音节
    (0xF900, 0xFAFF),      # CJK 兼容汉字
    (0xFE30, 0xFE6F),      # CJK 兼容形式
    (0xFF00, 0xFF60),      # 全角 ASCII（含 ｜）
    (0xFFE0, 0xFFE6),      # 全角符号
    (0x1F300, 0x1FAFF),    # emoji
    (0x20000, 0x3FFFD),    # CJK 扩展 B 及以后
)


def _dw(text: str) -> int:
    """字符串的**显示宽度**（中文/全角/emoji 记 2 列）。"""
    total = 0
    for ch in text:
        code = ord(ch)
        wide = False
        for low, high in _WIDE_RANGES:
            if low <= code <= high:
                wide = True
                break
        total += 2 if wide else 1
    return total


def _fit(text: str, width: int) -> str:
    """按显示宽度截断，超出用 … 收尾。"""
    if width <= 0:
        return ''
    if _dw(text) <= width:
        return text
    out = ''
    for ch in text:
        if _dw(out) + _dw(ch) > width - 1:
            break
        out += ch
    return out + '…'


def _pad(text: str, width: int, align: str = '<') -> str:
    """按显示宽度对齐填充。align: '<' 左 / '>' 右 / '^' 居中。

    ★ 一定要在**上色之前**做填充：ANSI 转义序列不占显示宽度，
      先上色再 ljust 会让每一列都往右漂。
    """
    text = _fit(text, width)
    gap = max(0, width - _dw(text))
    if align == '>':
        return ' ' * gap + text
    if align == '^':
        left = gap // 2
        return ' ' * left + text + ' ' * (gap - left)
    return text + ' ' * gap


def _c(text: str, color: str, use_color: bool = True) -> str:
    """上色。use_color=False 时原样返回（重定向到文件/日志时用）。"""
    return f'{color}{text}{_RESET}' if (use_color and color) else text


def _row2(left: str, right: str, width: int = TERM_WIDTH) -> str:
    """左右两端对齐的一行。"""
    gap = width - _dw(left) - _dw(right)
    if gap < 1:
        right = _fit(right, max(0, width - _dw(left) - 1))
        gap = max(1, width - _dw(left) - _dw(right))
    return left + ' ' * gap + right


# ---------------------------------------------------------------- 数值工具

def _score01(net_score: float) -> float:
    """净得分（-100 ~ +100）→ 大盘情绪分（0 ~ 100）。"""
    return (float(net_score) + 100.0) / 2.0


def _label(score: float) -> str:
    """0-100 分 → 中文档位。阈值取自 config，改口径只改一处。"""
    if score >= config.MARKET_BULL_THRESHOLD + 10:
        return '明显偏多'
    if score >= config.MARKET_BULL_THRESHOLD:
        return '偏多'
    if score <= config.MARKET_BEAR_THRESHOLD - 10:
        return '明显偏空'
    if score <= config.MARKET_BEAR_THRESHOLD:
        return '偏空'
    return '中性'


def _signed(value: float, digits: int = 1) -> str:
    """带正负号的数字，中性时也显式写 +0.0，避免读者误以为是缺失。"""
    return f'{value:+.{digits}f}'


def _tone(value: float, use_color: bool = True) -> str:
    """按数值取颜色：正=红（多），负=绿（空），零附近=灰。"""
    if not use_color:
        return ''
    if value > 0:
        return _BULL
    if value < 0:
        return _BEAR
    return _FLAT


def _symbol(direction: float) -> str:
    """方向 → 矩阵符号：++ / + / · / - / --。"""
    if direction >= _STRONG_CUTOFF:
        return '++'
    if direction > config.BULL_CUTOFF:
        return '+'
    if direction <= -_STRONG_CUTOFF:
        return '--'
    if direction < config.BEAR_CUTOFF:
        return '-'
    return '·'


def _dir_class(direction: float) -> str:
    """方向 → HTML class 名（决定格子底色深浅）。"""
    if direction >= _STRONG_CUTOFF:
        return 'b2'
    if direction > config.BULL_CUTOFF:
        return 'b1'
    if direction <= -_STRONG_CUTOFF:
        return 's2'
    if direction < config.BEAR_CUTOFF:
        return 's1'
    return 'n0'


def _short_org(name: str) -> str:
    """机构简称：去掉「期货/证券/研究所」等后缀，再截到 4 个字，供表头用。"""
    short = name
    for suffix in ('股份有限公司', '有限公司', '研究所', '研究院', '期货', '证券'):
        short = short.replace(suffix, '')
    short = short.strip() or name
    return short[:4]


def _product_name(code: str) -> str:
    """品种代码 → 中文名，查不到就回退用代码本身。"""
    prod = products_mod.get_product(code)
    return prod['name'] if prod else code


def _sector_of(code: str) -> str:
    return products_mod.get_sector(code) or '其他'


# ---------------------------------------------------------------- 数据快照

def resolve_date(conn, trade_date: str | None = None,
                 calc_version: str | None = None,
                 model_version: str | None = None) -> str | None:
    """确定要渲染哪一天：显式传就用传的，不传取**已聚合的最后一天**。

    返回 None 表示库里一天都还没聚合出来（提示用户先跑 pipeline）。
    """
    if trade_date:
        return trade_date
    dates = db.list_index_dates(conn, calc_version, model_version)
    return dates[-1] if dates else None


def _load_day(conn, trade_date: str, calc_version: str | None = None,
              model_version: str | None = None) -> dict:
    """把渲染一天所需要的数据一次性捞齐，四个渲染函数共用同一份快照。

    这样做的好处是**四种交付口径天然一致**：推送里的 62.4 分、终端里的
    62.4 分、HTML 里的 62.4 分必然同源，不会因为各自查一遍库而对不上。

    ★★ 两个版本号必须一路带下去 ★★
    头部的大盘分/板块分读的是 ``daily_index``，格子里的方向和证据读的是 ``score``。
    如果只按 calc_version 取指标、而取 score 时不指定 model_version
    （``fetch_scored_segments`` 默认取每个块**最新**的一行打分），那么打分口径
    升版之后，同一张看板上的头部数字和下钻证据会分别来自两个口径——
    页面顶上写着 67.2 分（偏多），点开格子每一个都是看空。
    所以本函数把 model_version 同时用于两边，保证同源。

    ★ 一家一票：同机构同日同品种可能有多篇研报（官网 + 邮件重复推送、
      或早报 + 午评），必须去重到一票。
      **这里直接复用 aggregate.dedupe_one_org_one_vote，不在呈现层另写一套。**
      呈现层若自己去重，一旦两处的取舍规则出现分歧（比如一边按 ingest_time
      取最新、一边按 id 取最大），矩阵里的格子就会和上面的净得分对不上——
      而这种不一致恰恰是最难被发现的：两个数看起来都「合理」。
    """
    version = calc_version or config.CALC_VERSION
    score_version = model_version or config.SCORE_VERSION

    # --- 聚合结果（方向） ---
    idx_rows = db.fetch_daily_index(conn, trade_date, calc_version=version,
                                    model_version=score_version)
    market = None
    sectors: list = []
    prods: list = []
    for row in idx_rows:
        if row['level'] == 'market':
            market = row
        elif row['level'] == 'sector':
            sectors.append(row)
        elif row['level'] == 'product':
            prods.append(row)

    sector_rank = {name: i for i, name in enumerate(products_mod.SECTORS)}
    sectors.sort(key=lambda r: (sector_rank.get(r['key'], 99), r['key']))
    prods.sort(key=lambda r: (sector_rank.get(_sector_of(r['key']), 99),
                              -float(r['net_score']), r['key']))

    # --- 明细（证据，供下钻） ---
    votes: dict[tuple[str, str], object] = {}
    macro: list = []
    low_conf = 0
    # ★ drop_unreadable：规则没读懂的块（confidence == 0）不进矩阵。
    #   把它画成「·」会被读成「这家看中性」，而真相是「引擎没读懂这段话」。
    voted = aggregate_mod.dedupe_one_org_one_vote(
        aggregate_mod.drop_unreadable(
            db.fetch_scored_segments(conn, ingest_date=trade_date,
                                     model_version=score_version)))
    for row in voted:
        if not row['product_code']:
            macro.append(row)                       # 宏观/综述块，不进品种矩阵
            continue
        votes[(row['org_name'], row['product_code'])] = row

    org_hits: dict[str, int] = {}
    for (org_name, _code), row in votes.items():
        org_hits[org_name] = org_hits.get(org_name, 0) + 1
        if float(row['confidence']) < config.LOW_CONFIDENCE:
            low_conf += 1
    for row in macro:
        org_hits.setdefault(row['org_name'], 0)
    # 覆盖品种多的机构排前面，同数按名字，保证每次渲染顺序一致
    orgs = sorted(org_hits, key=lambda name: (-org_hits[name], name))

    # --- 时间序列（右端点钉死在 trade_date，避免把「未来」画进图里） ---
    # ★ end_date 必须传给查询本身，而不是查完再过滤：fetch_index_series 是
    #   「按 trade_date 倒序取最近 limit 行」，先查后滤的话，历史超过 limit 天以后
    #   指定一个较早的日期会拿到一段全在它之后的序列，过滤完就是空的。
    series = db.fetch_index_series(conn, 'market', 'MARKET', limit=60,
                                   calc_version=version, model_version=score_version,
                                   end_date=trade_date)

    # --- 环比：口径收口在聚合层，呈现层不另写一套 ---
    # ★ 不用「序列倒数第二个点」来当上一期：那是**画图窗口**的副产品，
    #   窗口一变环比就跟着变。aggregate.compare_with_previous 定义的
    #   「上一个有指标的交易日」才是产品口径（周末节假日没有研报，
    #   自然日减一会天天算出 delta=None）。
    cmp = aggregate_mod.compare_with_previous(conn, trade_date, calc_version=version,
                                              model_version=score_version)

    # --- 大盘数值 ---
    if market is not None:
        net = float(market['net_score'])
        coverage = int(market['coverage_count'])
        bull, bear, neutral = (int(market['bull_count']), int(market['bear_count']),
                               int(market['neutral_count']))
    else:
        # 兜底：market 级没算出来时用品种级汇总顶上，保证看板不至于开天窗。
        # ★ 正常流程走不到这里（aggregate_day 只要有票就一定写 market 行），
        #   能走到说明库被手工改过或聚合只跑了一半。所以要打标记，
        #   让页面上明说这个数是拼出来的，而不是伪装成正式指标。
        net = _pooled_net(prods)
        coverage = len(orgs)
        bull = sum(int(r['bull_count']) for r in prods)
        bear = sum(int(r['bear_count']) for r in prods)
        neutral = sum(int(r['neutral_count']) for r in prods)

    score = _score01(net)
    prev_score = cmp['prev_score']
    delta = (score - prev_score) if prev_score is not None else None

    # --- baseline 对照组（架构方案第六节：任何复杂口径都要能跟「只数家数」比） ---
    # ★ 口径必须与 aggregate.naive_baseline 完全同源，不能在呈现层另算一套：
    #   baseline 是用来卡「复杂度有没有换来价值」的尺子，尺子有两把就白比了。
    #   baseline：一家机构一票（把该机构当天所有观点块的正负号数一数，多的那边算它的立场），
    #             (看多家数 − 看空家数) ÷ 总家数 × 100，完全不看强度。
    #   本模型：观点块计票 + ±0.25 强度阈值，品种 → 板块 → 大盘三级聚合。
    baseline = aggregate_mod.naive_baseline(conn, trade_date, model_version=score_version)
    # ★ NA ≠ 0：当日一家机构都没有时 naive_baseline 返回 None，
    #   这里**保持 None** 一路传给三种渲染，由它们显示「NA」。
    #   兜成 0.0 会在页面上变成一个「50.0 分」——一个没有任何打分支撑、
    #   却看起来像「市场很中性」的数字，正是本文件第 3 条原则要避免的东西。
    baseline_net = baseline['net_score']

    return {
        'date': trade_date,
        'version': version,
        'score_version': score_version,
        'market': market,
        'sectors': sectors,
        'products': prods,
        'votes': votes,
        'macro': macro,
        'orgs': orgs,
        'org_hits': org_hits,
        'series': series,
        'net': net,
        'score': score,
        'label': _label(score),
        'coverage': coverage,
        'bull': bull,
        'bear': bear,
        'neutral': neutral,
        'prev_date': cmp['prev_date'],
        'prev_score': prev_score,
        'delta': delta,
        'baseline_net': baseline_net,
        # ★ None 一路传下去，渲染层显示「NA」而不是 50.0 分
        'baseline_score': _score01(baseline_net) if baseline_net is not None else None,
        'baseline_orgs': (baseline['bull_orgs'], baseline['bear_orgs'],
                          baseline['neutral_orgs']),
        'opinion_count': len(votes) + len(macro),
        'product_count': len(prods),
        'low_conf': low_conf,
        'empty': not idx_rows,
        # 大盘行缺失、靠品种级汇总兜底出来的数，页面上要标出来
        'market_estimated': market is None and bool(prods),
    }


def _baseline_text(day: dict, digits: int = 1) -> str:
    """baseline 分的显示文本。★ NA ≠ 0：没有机构覆盖时显示 NA，不显示 50.0。"""
    value = day['baseline_score']
    return 'NA（当日无机构覆盖）' if value is None else f'{value:.{digits}f} 分'


def _pooled_net(prod_rows) -> float:
    """把所有品种的多空家数拉平后算净得分 = baseline「只数家数」口径。"""
    bull = sum(int(r['bull_count']) for r in prod_rows)
    bear = sum(int(r['bear_count']) for r in prod_rows)
    neutral = sum(int(r['neutral_count']) for r in prod_rows)
    total = bull + bear + neutral
    return (bull - bear) / total * 100.0 if total else 0.0


def _prev_products(conn, day: dict) -> dict:
    """取上一交易日的品种级指标，key=品种代码，用来算「变化最大」。

    ★ 版本号从 day 快照里拿，保证和头部数字同源。
    """
    if not day['prev_date']:
        return {}
    rows = db.fetch_daily_index(conn, day['prev_date'], level='product',
                                calc_version=day['version'],
                                model_version=day['score_version'])
    return {r['key']: r for r in rows}


def _biggest_move(conn, day: dict, prev_map: dict) -> tuple | None:
    """找环比变化最大的品种，返回 (代码, 上期净值, 本期净值, 说明)。

    ★ 排名口径直接用 ``aggregate.top_movers``，呈现层不另排一次序。
      「变化最大」是聚合层定义的产品口径（上期没覆盖的品种不当 0 算差、
      变化为 0 的不算异动），在这里重写一遍迟早会和推送里的数对不上。
      本函数只负责把排名结果翻译成一句中文（「4 家转多」）。
    """
    movers = aggregate_mod.top_movers(conn, day['date'], n=1, calc_version=day['version'],
                                      model_version=day['score_version'])
    if not movers:
        return None
    top = movers[0]
    code = top['key']
    current = next((r for r in day['products'] if r['key'] == code), None)
    prev = prev_map.get(code)
    if current is None or prev is None:
        return None

    turn = int(current['bull_count']) - int(prev['bull_count'])
    if turn > 0:
        note = f'{turn} 家转多'
    elif turn < 0:
        note = f'{-turn} 家转空'
    else:
        note = f'覆盖 {int(top["coverage_count"])} 家'
    return code, float(top['prev']), float(top['current']), note


def _biggest_split(conn, day: dict) -> dict | None:
    """找分歧最大的品种，返回 ``aggregate.most_divergent`` 的一条记录（无则 None）。

    口径同样收口在聚合层：用 min(看多, 看空) 而不是 |净值| 排序——
    净值接近 0 有两种完全相反的成因，「大家都中性」和「一半看多一半看空」，
    后者才是交易员真正想知道的分歧。
    """
    items = aggregate_mod.most_divergent(conn, day['date'], n=1, calc_version=day['version'],
                                         model_version=day['score_version'])
    return items[0] if items else None


# ---------------------------------------------------------------- ① 推送文案

def build_push_message(conn, trade_date: str | None = None,
                       calc_version: str | None = None,
                       model_version: str | None = None) -> str:
    """生成企业微信/飞书推送文案：三级结构，一屏看完。

    ★ 推送策略（调研结论，产品生死线）：
      每日固定推一次，但**只在大盘分突破 60/40 或单日变化超过 ALERT_DELTA 时**
      在开头加 ⚠️ 重点提醒。中间区间天天喊「今天偏多」，两周就会被交易员静音——
      主动推送类产品死在这一点上的比死在算法上的多。
    """
    trade_date = resolve_date(conn, trade_date, calc_version)
    if not trade_date:
        return '（还没有任何已聚合的交易日，请先执行：python3 run.py pipeline）'

    day = _load_day(conn, trade_date, calc_version, model_version)
    if day['empty']:
        return f'（{trade_date} 没有聚合结果：当日无研报覆盖，按 NA ≠ 0 的口径不产出数字）'

    lines: list[str] = []

    # --- 加重提醒判定 ---
    alerts = _alert_reasons(day)
    if alerts:
        lines.append('⚠️ 重点提醒｜' + '；'.join(alerts))
        lines.append('')

    # --- 第一级：大盘一个数 ---
    delta_txt = f'较{_day_word(day["prev_date"], trade_date)} {_signed(day["delta"])}' \
        if day['delta'] is not None else '首日无环比'
    lines.append(
        f'📊 大盘情绪 {day["score"]:.1f} 分（{day["label"]}）'
        f'｜{delta_txt}'
        f'｜覆盖 {day["coverage"]} 家机构 / {day["opinion_count"]} 个观点'
    )
    lines.append('')

    # --- 第二级：板块构成 ---
    if day['sectors']:
        cells = [f'{r["key"]} {_signed(float(r["net_score"]), 0)}' for r in day['sectors']]
        lines.append('板块  ' + '  '.join(cells))
        lines.append('')

    # --- 第二级补充：今日看点（变化 / 分歧） ---
    prev_map = _prev_products(conn, day)
    move = _biggest_move(conn, day, prev_map)
    if move:
        code, old, new, note = move
        lines.append(f'变化最大  {_product_name(code)} '
                     f'{_signed(old, 0)} → {_signed(new, 0)}（{note}）')
    elif day['products']:
        top = day['products'][0]
        lines.append(f'最看多    {_product_name(top["key"])} '
                     f'{_signed(float(top["net_score"]), 0)}'
                     f'（{int(top["bull_count"])} 家看多）')

    split = _biggest_split(conn, day)
    if split is not None:
        lines.append(f'分歧最大  {split["name"]}'
                     f'（{split["bull"]} 家看多 / {split["bear"]} 家看空）')
    else:
        lines.append('分歧最大  无（今日各品种方向一致）')
    lines.append('')

    # --- 第三级：口径自证。任何复杂口径都要跟「只数家数」摆在一起比 ---
    b_bull, b_bear, b_neutral = day['baseline_orgs']
    lines.append(f'对照 baseline（一家一票只数家数 {b_bull}/{b_bear}/{b_neutral}）'
                 f'{_baseline_text(day)} ｜ 本模型 {day["score"]:.1f} 分')
    lines.append(f'口径 {day["version"]} ｜ 打分 {day["score_version"]} ｜ '
                 f'多空中 {day["bull"]}/{day["bear"]}/{day["neutral"]}'
                 f'（{day["product_count"]} 个品种有覆盖，其余记 NA 不记 0）')
    lines.append('[查看详情] 机构 × 品种矩阵与证据原句见 HTML 看板')

    return '\n'.join(lines)


def _alert_reasons(day: dict) -> list[str]:
    """判断今天该不该加重提醒，返回理由列表（空 = 普通日报）。"""
    reasons: list[str] = []
    score, prev = day['score'], day['prev_score']
    bull_line, bear_line = config.MARKET_BULL_THRESHOLD, config.MARKET_BEAR_THRESHOLD

    if prev is None:
        # 没有昨日可比：只有落在极值区才提醒，中间区间保持安静
        if score >= bull_line:
            reasons.append(f'首日即位于偏多区（{score:.1f} ≥ {bull_line}）')
        elif score <= bear_line:
            reasons.append(f'首日即位于偏空区（{score:.1f} ≤ {bear_line}）')
        return reasons

    if prev < bull_line <= score:
        reasons.append(f'上破 {bull_line} 进入偏多区')
    if prev > bear_line >= score:
        reasons.append(f'下破 {bear_line} 进入偏空区')
    if abs(day['delta']) >= config.ALERT_DELTA:
        reasons.append(f'单日变化 {_signed(day["delta"])} 超过 {config.ALERT_DELTA} 分阈值')
    return reasons


def _day_word(prev_date: str | None, trade_date: str) -> str:
    """环比措辞：相邻自然日说「昨日」，隔了几天就直接报日期。"""
    if not prev_date:
        return '上期'
    try:
        gap = (_date.fromisoformat(trade_date) - _date.fromisoformat(prev_date)).days
    except ValueError:
        return prev_date
    return '昨日' if gap == 1 else prev_date[5:]


# ---------------------------------------------------------------- ② 多空矩阵

def render_matrix(conn, trade_date: str | None = None, use_color: bool = True,
                  calc_version: str | None = None,
                  model_version: str | None = None) -> str:
    """机构 × 品种的多空矩阵（文本表格），格子里用 ++ + · - -- 表示方向。

    行取品种、列取机构：机构是十几家的量级，品种有几十个，这样排才塞得进终端。

    ★ 空白格 = 该机构当天没写这个品种（NA），**不是中性**。
      把 NA 画成 · 会让读者以为「这家看中性」，是这类矩阵最常见的误读来源。
    """
    trade_date = resolve_date(conn, trade_date, calc_version, model_version)
    if not trade_date:
        return '（还没有任何已聚合的交易日）'
    day = _load_day(conn, trade_date, calc_version, model_version)
    return _render_matrix_from(day, use_color)


def _render_matrix_from(day: dict, use_color: bool = True) -> str:
    prods = day['products']
    orgs = day['orgs']
    if not prods or not orgs:
        return '  （当日没有品种级覆盖，矩阵为空）'

    shown_orgs = orgs[:MAX_MATRIX_ORGS]
    shown_prods = prods[:MAX_MATRIX_ROWS]

    w_name, w_net, w_cnt, w_cell = 10, 7, 9, 9
    lines: list[str] = []

    header = (_pad('  品种', w_name) + _pad('净得分', w_net, '>') + '  '
              + _pad('多/空/中', w_cnt) + ''.join(_pad(_short_org(o), w_cell, '^')
                                                  for o in shown_orgs))
    lines.append(_c(header, _BOLD + _HEAD, use_color))
    lines.append(_c('  ' + '─' * (TERM_WIDTH - 2), _DIM, use_color))

    last_sector = None
    for row in shown_prods:
        code = row['key']
        sector = _sector_of(code)
        if sector != last_sector:
            lines.append(_c(f'  【{sector}】', _DIM, use_color))
            last_sector = sector

        net = float(row['net_score'])
        name_cell = _pad('  ' + _product_name(code), w_name)
        net_cell = _c(_pad(_signed(net, 1), w_net, '>'), _tone(net, use_color), use_color)
        cnt_cell = _pad(f'{int(row["bull_count"])}/{int(row["bear_count"])}'
                        f'/{int(row["neutral_count"])}', w_cnt)

        cells = []
        for org_name in shown_orgs:
            vote = day['votes'].get((org_name, code))
            if vote is None:
                cells.append(' ' * w_cell)           # ★ NA：留白，绝不画 ·
                continue
            direction = float(vote['direction'])
            cells.append(_c(_pad(_symbol(direction), w_cell, '^'),
                            _tone(direction, use_color), use_color))

        lines.append(name_cell + net_cell + '  ' + cnt_cell + ''.join(cells))

    notes = []
    if len(orgs) > MAX_MATRIX_ORGS:
        notes.append(f'另有 {len(orgs) - MAX_MATRIX_ORGS} 家机构未在终端列出')
    if len(prods) > MAX_MATRIX_ROWS:
        notes.append(f'另有 {len(prods) - MAX_MATRIX_ROWS} 个品种未在终端列出')
    lines.append('')
    lines.append(_c('  图例  ++ 强烈看多   + 看多   · 中性   - 看空   -- 强烈看空   '
                    '空白 = 当日未覆盖(NA)', _DIM, use_color))
    if notes:
        lines.append(_c('  ' + '；'.join(notes) + '（完整矩阵见 HTML 看板）', _DIM, use_color))
    return '\n'.join(lines)


# ---------------------------------------------------------------- ③ 终端渲染

def render_terminal(conn, trade_date: str | None = None, use_color: bool = True,
                    calc_version: str | None = None,
                    model_version: str | None = None) -> str:
    """终端富文本渲染：大盘分 + 横向条形图 + 板块表格 + 多空矩阵。

    宽度锁死在 TERM_WIDTH（100 显示列）以内，颜色用 ANSI 转义序列手写，
    不引入 rich / colorama。
    """
    trade_date = resolve_date(conn, trade_date, calc_version, model_version)
    if not trade_date:
        return '（还没有任何已聚合的交易日，请先执行：python3 run.py pipeline）'

    day = _load_day(conn, trade_date, calc_version, model_version)
    if day['empty']:
        return f'（{trade_date} 无聚合结果：当日无研报覆盖，按 NA ≠ 0 的口径不产出数字）'

    out: list[str] = []
    bar = '═' * TERM_WIDTH

    # ---- 标题条 ----
    out.append(_c(bar, _HEAD, use_color))
    out.append(_c(_row2('  期货研报多空打分 Agent · 大盘情绪日报',
                        f'{day["date"]} ｜ 口径 {day["version"]}  '),
                  _BOLD + _HEAD, use_color))
    out.append(_c(bar, _HEAD, use_color))
    out.append('')

    # ---- 第一级：大盘一个数 ----
    tone = _tone(day['net'], use_color)
    head_left = ('  大盘情绪  ' + _c(f'{day["score"]:5.1f}', _BOLD + tone, use_color)
                 + ' / 100  ' + _c(day['label'], tone, use_color))
    if day['delta'] is not None:
        delta_txt = (f'较{_day_word(day["prev_date"], day["date"])} '
                     + _c(_signed(day['delta']), _tone(day['delta'], use_color), use_color))
    else:
        delta_txt = _c('首日无环比', _DIM, use_color)
    head_right = f'{delta_txt}   覆盖 {day["coverage"]} 家机构 / {day["opinion_count"]} 个观点  '
    # _row2 需要按可见宽度对齐，这里先用无色版本算间距
    plain_left = f'  大盘情绪  {day["score"]:5.1f} / 100  {day["label"]}'
    plain_right = (f'较{_day_word(day["prev_date"], day["date"])} '
                   f'{_signed(day["delta"])}' if day['delta'] is not None else '首日无环比')
    plain_right += f'   覆盖 {day["coverage"]} 家机构 / {day["opinion_count"]} 个观点  '
    gap = max(1, TERM_WIDTH - _dw(plain_left) - _dw(plain_right))
    out.append(head_left + ' ' * gap + head_right)

    # ---- 情绪刻度条 ----
    out.extend(_gauge(day['score'], use_color))
    out.append('')

    # ---- 第二级：板块 ----
    # 表头各列宽度与数据行严格一致（14 / 7 / 2 / 9 / 8 / 5 / 25），否则一眼就看出是拼的
    out.append(_c(_pad('  板块热力', 14) + _pad('净得分', 7, '>') + '  '
                  + _pad('多/空/中', 9) + _pad('覆盖', 8) + '     '
                  + _pad('← 偏空', 12) + '┼' + _pad('偏多 →', 12, '>'),
                  _BOLD + _HEAD, use_color))
    out.append(_c('  ' + '─' * (TERM_WIDTH - 2), _DIM, use_color))
    if day['sectors']:
        for row in day['sectors']:
            net = float(row['net_score'])
            line = (_pad('  ' + row['key'], 14)
                    + _c(_pad(_signed(net, 1), 7, '>'), _tone(net, use_color), use_color)
                    + '  ' + _pad(f'{int(row["bull_count"])}/{int(row["bear_count"])}'
                                  f'/{int(row["neutral_count"])}', 9)
                    + _pad(f'{int(row["coverage_count"])} 家', 8)
                    + '     ' + _diverging_bar(net, use_color))
            out.append(line)
    else:
        out.append(_c('  （当日无板块级覆盖）', _DIM, use_color))
    out.append('')

    # ---- 第三级：机构 × 品种矩阵 ----
    out.append(_c('  多空矩阵  机构 × 品种', _BOLD + _HEAD, use_color))
    out.append(_render_matrix_from(day, use_color))
    out.append('')

    # ---- 今日看点 ----
    prev_map = _prev_products(conn, day)
    move = _biggest_move(conn, day, prev_map)
    split = _biggest_split(conn, day)
    if move or split is not None:
        out.append(_c('  今日看点', _BOLD + _HEAD, use_color))
        if move:
            code, old, new, note = move
            out.append(f'  变化最大  {_product_name(code)} '
                       f'{_c(_signed(old, 0), _tone(old, use_color), use_color)} → '
                       f'{_c(_signed(new, 0), _tone(new, use_color), use_color)}（{note}）')
        if split is not None:
            out.append(f'  分歧最大  {split["name"]}'
                       f'（{split["bull"]} 家看多 / {split["bear"]} 家看空）')
        out.append('')

    # ---- 口径自证与免责 ----
    out.append(_c('  ' + '─' * (TERM_WIDTH - 2), _DIM, use_color))
    b_bull, b_bear, b_neutral = day['baseline_orgs']
    out.append(_c(f'  对照 baseline（一家一票只数家数，多/空/中 '
                  f'{b_bull}/{b_bear}/{b_neutral} 家）{_baseline_text(day)}'
                  f'  ｜  本模型（品种→板块→大盘三级聚合）{day["score"]:.1f} 分',
                  _DIM, use_color))
    out.append(_c(f'  打分口径 {day["score_version"]} ｜ 聚合口径 {day["version"]} ｜ '
                  f'宏观块 {len(day["macro"])} 个 ｜ 低置信度待复核 {day["low_conf"]} 条',
                  _DIM, use_color))
    if day['market_estimated']:
        out.append(_c('  ⚠ 大盘级指标行缺失，上方数字由品种级汇总兜底，'
                      '不是正式聚合结果（请重跑 aggregate）', _WARN, use_color))
    out.append(_c('  指标按 ingest_time 归集（非 publish_time，避免未来函数）｜ '
                  'demo 数据为虚构样本，不构成投资建议', _DIM, use_color))
    return '\n'.join(out)


def _gauge(score: float, use_color: bool = True) -> list[str]:
    """0-100 情绪刻度条：从中性线 50 向两侧填充，下面一行标出指针位置。"""
    width = 56
    prefix = '  情绪刻度    0 ├'
    track = ['·'] * width
    center = width // 2
    pos = max(0, min(width - 1, int(round(score / 100.0 * (width - 1)))))
    if pos >= center:
        for i in range(center, pos + 1):
            track[i] = '█'
        color = _BULL
    else:
        for i in range(pos, center + 1):
            track[i] = '█'
        color = _BEAR
    if track[center] == '·':
        track[center] = '┼'

    line = prefix + _c(''.join(track), color, use_color) + '┤ 100'

    # 标注行：指针优先占位，中性刻度只在不打架时才画
    label = f'▲ {score:.1f}'
    p_lo = _dw(prefix) + pos
    p_hi = p_lo + _dw(label)
    tick_text = '中性 50'
    t_lo = _dw(prefix) + center - 3
    t_hi = t_lo + _dw(tick_text)
    marks = [(p_lo, label, _BOLD + color)]
    if t_hi + 1 <= p_lo or t_lo >= p_hi + 1:
        marks.append((t_lo, tick_text, _DIM))
    marks.sort(key=lambda m: m[0])

    row, cursor = '', 0
    for col, text, style in marks:
        row += ' ' * max(0, col - cursor) + _c(text, style, use_color)
        cursor = col + _dw(text)
    return [line, row]


def _diverging_bar(net: float, use_color: bool = True, half: int = 12) -> str:
    """以 0 为中心的双向条：左绿（空）右红（多）。"""
    n = max(0, min(half, int(round(abs(net) / 100.0 * half))))
    if net >= 0:
        left = _c('·' * half, _DIM, use_color)
        right = _c('█' * n, _BULL, use_color) + _c('·' * (half - n), _DIM, use_color)
    else:
        left = _c('·' * (half - n), _DIM, use_color) + _c('█' * n, _BEAR, use_color)
        right = _c('·' * half, _DIM, use_color)
    return left + _c('┼', _DIM, use_color) + right


# ---------------------------------------------------------------- ④ HTML 看板

#: 内联 CSS。风格取向：暗色金融终端，克制、密集、以数字为主角，不做圆角卡片和阴影堆叠。
_CSS = """
* { box-sizing: border-box; }
body {
  margin: 0; padding: 28px 20px 60px;
  background: #0d1015; color: #e6e9ef;
  font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei",
               "Helvetica Neue", Arial, sans-serif;
  font-size: 13px; line-height: 1.6;
}
.wrap { max-width: 1180px; margin: 0 auto; }
.num { font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
       font-variant-numeric: tabular-nums; }
.bull { color: #e5484d; }   /* 红 = 看多，国内习惯 */
.bear { color: #12a67a; }   /* 绿 = 看空 */
.flat { color: #8b93a7; }
.mut  { color: #8b93a7; }

header { display: flex; align-items: baseline; justify-content: space-between;
         border-bottom: 1px solid #262c3a; padding-bottom: 12px; margin-bottom: 22px; }
header h1 { font-size: 15px; font-weight: 600; letter-spacing: .06em; margin: 0; }
header .meta { font-size: 12px; color: #8b93a7; }

.panel { border: 1px solid #212736; background: #141821; padding: 18px 20px; margin-bottom: 18px; }
.panel > h2 { font-size: 12px; font-weight: 600; letter-spacing: .16em; color: #98a1b6;
              margin: 0 0 14px; text-transform: uppercase; }

.hero { display: flex; align-items: stretch; gap: 34px; flex-wrap: wrap; }
.hero .big { min-width: 250px; }
.hero .score { font-size: 68px; line-height: 1; font-weight: 600; letter-spacing: -.02em; }
.hero .score small { font-size: 18px; color: #8b93a7; font-weight: 400; margin-left: 6px; }
.hero .tag { display: inline-block; margin-top: 10px; padding: 2px 12px;
             border: 1px solid currentColor; font-size: 12px; letter-spacing: .1em; }
.hero .delta { margin-left: 12px; font-size: 13px; }
.alert { margin: 0 0 16px; padding: 9px 14px; border-left: 3px solid #e0a02c;
         background: rgba(224,160,44,.10); color: #f0c674; font-size: 12.5px; }
.stats { display: flex; gap: 30px; flex-wrap: wrap; align-items: center; flex: 1; }
.stat { min-width: 96px; }
.stat .k { font-size: 11px; color: #8b93a7; letter-spacing: .08em; }
.stat .v { font-size: 22px; margin-top: 2px; }

.srow { display: flex; align-items: center; gap: 12px; padding: 5px 0; }
.srow .sname { width: 76px; }
.srow .sval  { width: 62px; text-align: right; }
.srow .scov  { width: 96px; font-size: 11.5px; color: #8b93a7; }
.sbar { flex: 1; display: flex; align-items: center; height: 13px; }
.sbar .half { flex: 1; display: flex; height: 100%; background: rgba(255,255,255,.030); }
.sbar .half.l { justify-content: flex-end; }
.sbar .mid { width: 1px; height: 100%; background: #3b4358; }
.sbar i { display: block; height: 100%; }
.sbar .l i { background: #12a67a; }
.sbar .r i { background: #e5484d; }

table.mx { border-collapse: collapse; width: 100%; font-size: 12px; }
table.mx th, table.mx td { border: 1px solid #212736; padding: 5px 6px; text-align: center; }
table.mx thead th { background: #171c27; color: #98a1b6; font-weight: 500;
                    font-size: 11.5px; letter-spacing: .04em; }
table.mx td.name { text-align: left; white-space: nowrap; }
table.mx td.sec  { text-align: left; background: #11151d; color: #8b93a7;
                   font-size: 11.5px; letter-spacing: .1em; }
table.mx td.c { position: relative; cursor: default; font-weight: 600; }
table.mx td.b2 { background: rgba(229,72,77,.55); color: #fff2f2; }
table.mx td.b1 { background: rgba(229,72,77,.24); color: #ffc9cb; }
table.mx td.n0 { background: rgba(139,147,167,.13); color: #aeb6c7; }
table.mx td.s1 { background: rgba(18,166,122,.24); color: #9fe6cd; }
table.mx td.s2 { background: rgba(18,166,122,.55); color: #eafff7; }
table.mx td.na { background: repeating-linear-gradient(45deg,
                 transparent, transparent 4px, rgba(255,255,255,.028) 4px,
                 rgba(255,255,255,.028) 5px); }
table.mx td.lowconf .sym { border-bottom: 1px dashed currentColor; }

td.c .tip { display: none; position: absolute; z-index: 40; left: 50%; bottom: 145%;
            transform: translateX(-50%); width: 330px; padding: 10px 12px;
            background: #060810; border: 1px solid #39415a; color: #e6e9ef;
            text-align: left; font-weight: 400; box-shadow: 0 8px 26px rgba(0,0,0,.6); }
td.c:hover .tip { display: block; }
td.c:hover { outline: 1px solid #7d879e; }
/* tooltip 内部四行必须显式 block：默认的 span 是 inline，会挤成一坨读不了 */
.tip .t1, .tip .t2, .tip .t3, .tip .t4 { display: block; }
.tip .t1 { font-size: 12.5px; font-weight: 600; margin-bottom: 4px; }
.tip .t2 { font-size: 11.5px; color: #8b93a7; margin-bottom: 7px; }
.tip .t3 { font-size: 12px; line-height: 1.7; border-left: 2px solid #4a5470;
           padding-left: 9px; color: #dfe4ee; }
.tip .t4 { font-size: 11px; color: #6f778c; margin-top: 8px; }

.legend { margin-top: 12px; font-size: 11.5px; color: #8b93a7; }
.legend b { display: inline-block; min-width: 22px; color: #c8cede; font-weight: 600; }
footer { border-top: 1px solid #262c3a; margin-top: 26px; padding-top: 14px;
         font-size: 11.5px; color: #6f778c; line-height: 1.9; }
footer .warn { color: #b08a4a; }
"""


def export_html(conn, trade_date: str | None = None, out_path: str | Path | None = None,
                calc_version: str | None = None,
                model_version: str | None = None) -> str:
    """生成单文件 HTML 看板（零依赖：内联 CSS + 手写 SVG，无 JS）。

    包含五块：
      1. 顶部大盘情绪分仪表（大字 + 颜色 + 环比 + 热度）
      2. 大盘情绪分历史折线图（手写 SVG，数据来自 fetch_index_series）
      3. 板块热力条
      4. 机构 × 品种多空矩阵，**格子悬停显示该机构该品种的证据原句**
         （纯 CSS :hover + title 属性双保险，不写一行 JS）
      5. 底部：口径版本号、覆盖家数、demo 数据声明

    返回写入的文件路径。
    """
    trade_date = resolve_date(conn, trade_date, calc_version, model_version)
    if not trade_date:
        raise ValueError('库里还没有任何已聚合的交易日，请先执行 pipeline')

    day = _load_day(conn, trade_date, calc_version, model_version)
    path = Path(out_path) if out_path else config.OUT_DIR / f'dashboard-{trade_date}.html'
    path.parent.mkdir(parents=True, exist_ok=True)

    parts: list[str] = []
    parts.append('<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">')
    parts.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    parts.append(f'<title>大盘情绪日报 {_esc(trade_date)}</title>')
    parts.append(f'<style>{_CSS}</style>\n</head>\n<body>\n<div class="wrap">')

    parts.append(_html_header(day))
    if day['empty']:
        # ★ 与推送/终端同一条口径：当日无覆盖就明说没有数，不画一张全是 0 的看板。
        #   一张「大盘 50.0 分」的空看板比没有看板更危险——它看起来像个结论。
        parts.append('<div class="panel"><div class="alert">'
                     f'{_esc(trade_date)} 没有聚合结果：当日无研报覆盖，'
                     '按 NA ≠ 0 的口径不产出任何数字。</div></div>')
    else:
        parts.append(_html_hero(day))
        parts.append(_html_chart(day))
        parts.append(_html_sectors(day))
        parts.append(_html_matrix(day))
    parts.append(_html_footer(day))

    parts.append('</div>\n</body>\n</html>\n')
    path.write_text('\n'.join(parts), encoding='utf-8')
    return str(path)


def _esc(text) -> str:
    """HTML 转义。研报原文里带 < > & 的情况不多但必须挡住。"""
    return _html.escape(str(text if text is not None else ''), quote=True)


def _cls(value: float) -> str:
    return 'bull' if value > 0 else ('bear' if value < 0 else 'flat')


def _html_header(day: dict) -> str:
    return (
        '<header>'
        '<h1>期货研报多空打分 AGENT · 大盘情绪日报</h1>'
        f'<div class="meta num">交易日 {_esc(day["date"])} ｜ 聚合口径 {_esc(day["version"])}'
        f' ｜ 打分口径 {_esc(day["score_version"])}</div>'
        '</header>'
    )


def _html_hero(day: dict) -> str:
    tone = _cls(day['net'])
    alerts = _alert_reasons(day)
    blocks = []
    if alerts:
        blocks.append(f'<div class="alert">⚠️ 重点提醒｜{_esc("；".join(alerts))}</div>')

    if day['delta'] is None:
        delta_html = '<span class="delta mut">首日无环比</span>'
    else:
        delta_html = (f'<span class="delta num {_cls(day["delta"])}">'
                      f'较{_esc(_day_word(day["prev_date"], day["date"]))} '
                      f'{_signed(day["delta"])}</span>')

    # ★ baseline 为 None（当日无机构覆盖）时显示 NA，不显示 50.0——
    #   一个没有任何打分支撑的「50 分」会被读成「市场很中性」，与真相相反。
    baseline_cell = ('NA', '') if day['baseline_score'] is None \
        else (f'{day["baseline_score"]:.1f}', '分')
    stats = [
        ('覆盖机构', f'{day["coverage"]}', '家'),
        ('观点总数', f'{day["opinion_count"]}', '条'),
        ('有覆盖品种', f'{day["product_count"]}', '个'),
        ('多 / 空 / 中', f'{day["bull"]}/{day["bear"]}/{day["neutral"]}', ''),
        ('baseline 一家一票', baseline_cell[0], baseline_cell[1]),
    ]
    stat_html = ''.join(
        f'<div class="stat"><div class="k">{_esc(k)}</div>'
        f'<div class="v num">{_esc(v)}<small class="mut" style="font-size:11px"> {_esc(u)}</small>'
        f'</div></div>'
        for k, v, u in stats
    )

    blocks.append(
        '<div class="panel"><div class="hero">'
        '<div class="big">'
        f'<div class="score num {tone}">{day["score"]:.1f}'
        '<small>/ 100</small></div>'
        f'<div><span class="tag {tone}">{_esc(day["label"])}</span>{delta_html}</div>'
        '</div>'
        f'<div class="stats">{stat_html}</div>'
        '</div></div>'
    )
    return '\n'.join(blocks)


def _html_chart(day: dict) -> str:
    """手写 SVG 折线图：大盘情绪分的历史序列。

    纵轴固定 0-100 而不是自适应——自适应会把 ±1 分的日常波动画成惊涛骇浪，
    这类情绪指标的读者要的是「离 50 有多远」，量纲必须锁死才具备纵向可比性。
    """
    series = day['series']
    if not series:
        return ''

    points = [(r['trade_date'], _score01(float(r['net_score'])), int(r['coverage_count']))
              for r in series]

    w, h = 1080, 250
    x0, x1 = 52, 1058
    y_top, y_bot = 22, 206

    def yy(score: float) -> float:
        return y_bot - score / 100.0 * (y_bot - y_top)

    n = len(points)
    def xx(i: int) -> float:
        return (x0 + x1) / 2 if n == 1 else x0 + i * (x1 - x0) / (n - 1)

    svg = [f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" '
           'preserveAspectRatio="none" role="img" aria-label="大盘情绪分历史走势">']
    # ★ gradientUnits 必须用 userSpaceOnUse：默认的 objectBoundingBox 会把渐变
    #   按面积图自身的包围盒来分段，红绿分界线就跟着数据浮动，而不是钉在 50 分。
    #   这里直接锚到 y(100) ~ y(0)，红绿交界必然落在中性线 50 上。
    svg.append(
        f'<defs><linearGradient id="area" gradientUnits="userSpaceOnUse" '
        f'x1="0" y1="{yy(100):.1f}" x2="0" y2="{yy(0):.1f}">'
        '<stop offset="0%" stop-color="#e5484d" stop-opacity=".38"/>'
        '<stop offset="49.9%" stop-color="#e5484d" stop-opacity=".04"/>'
        '<stop offset="50.1%" stop-color="#12a67a" stop-opacity=".04"/>'
        '<stop offset="100%" stop-color="#12a67a" stop-opacity=".38"/>'
        '</linearGradient></defs>'
    )
    # 偏多 / 偏空 区带：让「60 以上偏多、40 以下偏空」这条口径在图上直接可见
    svg.append(f'<rect x="{x0}" y="{yy(100):.1f}" width="{x1 - x0}" '
               f'height="{yy(config.MARKET_BULL_THRESHOLD) - yy(100):.1f}" '
               'fill="#e5484d" opacity=".055"/>')
    svg.append(f'<rect x="{x0}" y="{yy(config.MARKET_BEAR_THRESHOLD):.1f}" '
               f'width="{x1 - x0}" height="{yy(0) - yy(config.MARKET_BEAR_THRESHOLD):.1f}" '
               'fill="#12a67a" opacity=".055"/>')

    for level in (0, 20, 40, 50, 60, 80, 100):
        y = yy(level)
        mid = (level == 50)
        dash = ' stroke-dasharray="3 4"' if mid else ''
        stroke = '#3b4358' if mid else '#202634'
        svg.append(f'<line x1="{x0}" y1="{y:.1f}" x2="{x1}" y2="{y:.1f}" '
                   f'stroke="{stroke}" stroke-width="1"{dash}/>')
        svg.append(f'<text x="{x0 - 10}" y="{y + 4:.1f}" text-anchor="end" '
                   f'fill="#6f778c" font-size="11" font-family="monospace">{level}</text>')

    coords = [(xx(i), yy(s)) for i, (_d, s, _c2) in enumerate(points)]
    if n > 1:
        area = ' '.join(f'{x:.1f},{y:.1f}' for x, y in coords)
        area += f' {coords[-1][0]:.1f},{yy(50):.1f} {coords[0][0]:.1f},{yy(50):.1f}'
        svg.append(f'<polygon points="{area}" fill="url(#area)"/>')
        line = ' '.join(f'{x:.1f},{y:.1f}' for x, y in coords)
        svg.append(f'<polyline points="{line}" fill="none" stroke="#e8b339" '
                   'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')

    step = max(1, n // 8)
    for i, (date, score, cov) in enumerate(points):
        x, y = coords[i]
        color = '#e5484d' if score >= 50 else '#12a67a'
        last = (i == n - 1)
        svg.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{4.5 if last else 3}" '
                   f'fill="{color}" stroke="#0d1015" stroke-width="1.5">'
                   f'<title>{_esc(date)}　{score:.1f} 分　覆盖 {cov} 家</title></circle>')
        if i % step == 0 or last:
            svg.append(f'<text x="{x:.1f}" y="{y_bot + 24}" text-anchor="middle" '
                       f'fill="#6f778c" font-size="11" font-family="monospace">'
                       f'{_esc(date[5:])}</text>')
        if last:
            svg.append(f'<text x="{x - 10:.1f}" y="{y - 12:.1f}" text-anchor="end" '
                       f'fill="{color}" font-size="13" font-weight="600" '
                       f'font-family="monospace">{score:.1f}</text>')

    svg.append('</svg>')

    tip = ('单点时序列只有一天，多跑几天 pipeline 才能看出升温/降温'
           if n == 1 else
           f'最近 {n} 个交易日 ｜ 无覆盖的日子按 NA 处理，序列直接缺该点，不补 0')
    return ('<div class="panel"><h2>大盘情绪分 · 历史走势</h2>'
            + '\n'.join(svg)
            + f'<div class="legend">{_esc(tip)}　·　红区 = 60 以上偏多，'
              f'绿区 = 40 以下偏空，虚线 = 中性线 50</div></div>')


def _html_sectors(day: dict) -> str:
    if not day['sectors']:
        return ''
    rows = []
    for row in day['sectors']:
        net = float(row['net_score'])
        pct = min(100.0, abs(net))
        neg = f'<i style="width:{pct:.1f}%"></i>' if net < 0 else ''
        pos = f'<i style="width:{pct:.1f}%"></i>' if net > 0 else ''
        rows.append(
            '<div class="srow">'
            f'<span class="sname">{_esc(row["key"])}</span>'
            f'<span class="sbar"><span class="half l">{neg}</span>'
            f'<span class="mid"></span><span class="half r">{pos}</span></span>'
            f'<span class="sval num {_cls(net)}">{_signed(net)}</span>'
            f'<span class="scov num">{int(row["coverage_count"])} 家 · '
            f'{int(row["bull_count"])}/{int(row["bear_count"])}/{int(row["neutral_count"])}'
            '</span>'
            '</div>'
        )
    return ('<div class="panel"><h2>板块热力</h2>' + ''.join(rows)
            + '<div class="legend">条长 = 净得分绝对值（-100 ~ +100）'
              '　·　右红 = 偏多，左绿 = 偏空　·　家数为覆盖热度，与方向分开看</div></div>')


def _html_matrix(day: dict) -> str:
    """机构 × 品种矩阵。★ 每个格子内嵌证据原句 tooltip，可解释下钻是刚需不是加分项。"""
    prods, orgs = day['products'], day['orgs']
    if not prods or not orgs:
        return ('<div class="panel"><h2>多空矩阵 · 机构 × 品种</h2>'
                '<div class="legend">当日无品种级覆盖。</div></div>')

    head = ['<tr><th style="text-align:left">品种</th><th>净得分</th><th>多/空/中</th>']
    head += [f'<th>{_esc(_short_org(o))}</th>' for o in orgs]
    head.append('</tr>')

    body: list[str] = []
    last_sector = None
    span = len(orgs) + 3
    for row in prods:
        code = row['key']
        sector = _sector_of(code)
        if sector != last_sector:
            body.append(f'<tr><td class="sec" colspan="{span}">{_esc(sector)}</td></tr>')
            last_sector = sector
        net = float(row['net_score'])
        cells = [
            f'<td class="name">{_esc(_product_name(code))}'
            f'<span class="mut num" style="font-size:11px"> {_esc(code)}</span></td>',
            f'<td class="num {_cls(net)}">{_signed(net)}</td>',
            f'<td class="num mut">{int(row["bull_count"])}/{int(row["bear_count"])}'
            f'/{int(row["neutral_count"])}</td>',
        ]
        for org_name in orgs:
            cells.append(_html_cell(day, org_name, code))
        body.append('<tr>' + ''.join(cells) + '</tr>')

    legend = (
        '<div class="legend">'
        '<b>++</b> 强烈看多　<b>+</b> 看多　<b>·</b> 中性　<b>-</b> 看空　<b>--</b> 强烈看空　'
        '斜纹格 = 该机构当日未覆盖该品种（NA，不是中性）<br>'
        '鼠标悬停任意格子可看该机构该品种的证据原句；虚线下划线 = 置信度低于 '
        f'{config.LOW_CONFIDENCE}，真实环境会进人工复核队列。'
        '</div>'
    )
    # ★ 这里刻意不套 overflow-x:auto 的滚动容器：CSS 里只要有一个轴不是 visible，
    #   另一个轴就会被计算成 auto，证据原句的 tooltip 会被裁掉上半截。
    #   机构特别多时宁可让整页横向滚动，也不能牺牲下钻——下钻是刚需，不是加分项。
    return ('<div class="panel"><h2>多空矩阵 · 机构 × 品种（悬停格子下钻到证据原句）</h2>'
            '<table class="mx"><thead>'
            + ''.join(head) + '</thead><tbody>' + ''.join(body) + '</tbody></table>'
            + legend + '</div>')


def _html_cell(day: dict, org_name: str, code: str) -> str:
    vote = day['votes'].get((org_name, code))
    if vote is None:
        # ★ NA ≠ 0：没覆盖就画成斜纹空格，绝不渲染成 0 或中性
        return '<td class="na" title="该机构当日未覆盖该品种（NA）"></td>'

    direction = float(vote['direction'])
    confidence = float(vote['confidence'])
    evidence = (vote['evidence'] or vote['advice_text'] or vote['raw_text'] or '')[:220]
    low = ' lowconf' if confidence < config.LOW_CONFIDENCE else ''
    title = (f'{org_name} · {_product_name(code)}｜方向 {direction:+.1f}｜'
             f'置信度 {confidence:.2f}｜{vote["method"]}\n{evidence}')
    tip = (
        '<span class="tip">'
        f'<span class="t1">{_esc(org_name)} · {_esc(_product_name(code))}</span>'
        f'<span class="t2 num">方向 {direction:+.1f}　置信度 {confidence:.2f}　'
        f'{_esc(vote["method"])} / {_esc(vote["model_version"])}</span>'
        f'<span class="t3">{_esc(evidence) or "（无证据原句）"}</span>'
        f'<span class="t4">{_esc(vote["title"])}　发布 {_esc(vote["publish_time"])}　'
        f'入库 {_esc(str(vote["ingest_time"])[11:16])}</span>'
        '</span>'
    )
    return (f'<td class="c {_dir_class(direction)}{low}" title="{_esc(title)}">'
            f'<span class="sym">{_symbol(direction)}</span>{tip}</td>')


def _html_footer(day: dict) -> str:
    weight_note = ('正负等权（BEAR_WEIGHT = 1.0，即 IndexMundi 净得分 baseline 公式）'
                   if config.BEAR_WEIGHT == 1.0 else
                   f'空头票加权 ×{config.BEAR_WEIGHT}（负面非对称加权）')
    estimated = ('<span class="warn">⚠ 大盘级指标行缺失，上方数字由品种级汇总兜底，'
                 '不是正式聚合结果。</span><br>') if day['market_estimated'] else ''
    if day['empty']:
        counts = '当日无研报覆盖，按 NA ≠ 0 不产出任何数字。<br>'
    else:
        counts = (
            f'覆盖热度：{day["coverage"]} 家机构 / {day["opinion_count"]} 个观点 / '
            f'{day["product_count"]} 个品种有覆盖 ｜ 宏观综述块 {len(day["macro"])} 个 ｜ '
            f'低置信度待人工复核 {day["low_conf"]} 条<br>'
            f'口径对照：baseline（一家一票只数家数，多/空/中 '
            f'{day["baseline_orgs"][0]}/{day["baseline_orgs"][1]}/'
            f'{day["baseline_orgs"][2]} 家）'
            f'{_esc(_baseline_text(day))} ｜ '
            f'本模型（品种 → 板块 → 大盘三级聚合）{day["score"]:.1f} 分。'
            '任何复杂加权都必须先跑赢这条 baseline，否则复杂度没换来价值。<br>'
        )
    return (
        '<footer>'
        f'口径版本：打分 <span class="num">{_esc(day["score_version"])}</span>'
        f' ｜ 聚合 <span class="num">{_esc(day["version"])}</span>'
        '（双版本号分开管理，且一起构成 daily_index 的主键：'
        '任一口径变更都能重算历史并与旧口径并存对比）<br>'
        + counts
        + f'净得分口径：{_esc(weight_note)}。'
        '非对称加权的开关在聚合层（config.BEAR_WEIGHT），改它必须同时升 '
        '聚合口径版本号。<br>'
        '时点口径：所有指标按研报 <b>入库时间 ingest_time</b> 归集，'
        '不用研报标注的发布时间，避免回测出现未来函数。<br>'
        + estimated
        + '<span class="warn">⚠ 本页为作品集 demo，'
        '数据为虚构样本，机构名称与研报内容均非真实，不构成任何投资建议。</span>'
        '</footer>'
    )
