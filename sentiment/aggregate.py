"""⑥ 聚合：把一条条观点块算成「全市场偏多还是偏空」。

三级聚合（架构方案第六节）::

    观点块  ──→  品种净值  ──→  板块净值  ──→  大盘情绪分
    (带方向)     (同品种多家)    (同板块多品种)   (全市场)

★★ 三条口径规则，是产品定义的一部分，不是实现细节 ★★

1. **一家一票**：同一机构当天对同一品种发多篇（早报 + 午间更新），
   只取 ingest_time 最新的一篇。不去重的话，发报最勤的机构会单方面
   把指标拽向自己的方向——指标测的就不再是「全市场怎么看」了。

2. **NA ≠ 0**：某品种当天没有机构覆盖，**不产生该品种的记录**，绝不写 0。
   真 0 的含义是「有多有空、净额为零」，NA 的含义是「根本没人聊」，
   两者含义相反（MarketPsych 官方文档专门强调过这一点）。

3. **热度与方向分离**：coverage_count（覆盖机构家数）独立成列。
   3 家看多和 30 家看多不该同权，覆盖家数是多空分的置信度。

其余两个要点：

- **板块级是把该板块下所有观点块直接汇总**，不是对品种净值再求平均。
  再平均会让「只有 1 家覆盖的碳酸锂」和「8 家覆盖的螺纹钢」等权，
  一个冷门品种的极端值就能把整个板块带偏。
- **宏观/综述块（product_code 为 None）算进大盘，不算进任何板块**。
  它是对大盘的直接判断，恰恰是最该进大盘口径的那类观点；
  但它没有品种，硬塞进某个板块只会污染板块口径。

★ **负面非对称加权**（架构方案第五节的显式设计项）落在本层，
  由 ``config.BEAR_WEIGHT`` 控制、受 calc_version 管理，实现见 :func:`_net_score`。
  它必须在这一层而不是打分层：这是个会反复调参的口径，放在打分层每调一次
  就要重跑一遍全量打分。**demo 版恒取 1.0（完全等权）**，先把 baseline 跑通，
  理由见 config.BEAR_WEIGHT 的注释。

聚合是**可反复重算**的一步：它只读 score 表，不改 score 表。
口径变了就换个 calc_version 重算一遍，底层打分一行都不用重跑。
"""

import sqlite3

from . import config, db, products
from .models import DailyIndex

# ---------------------------------------------------------------- 小工具


def _get(row, key: str, default=None):
    """同时兼容 sqlite3.Row 和 dict 取值。

    聚合层的入参既可能来自 db.fetch_scored_segments（sqlite3.Row），
    也可能是测试里手搓的 dict，取值方式统一在这里收口。
    """
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return value


def _trade_date_of(row) -> str:
    """取该行归属的交易日。★ 一律取 ingest_time 的日期部分，绝不用 publish_time。

    周五发布、周一才入库的研报若按 publish_time 算进周五，等于把周一才知道的
    信息塞回周五，回测立刻出现未来函数。
    """
    date = _get(row, 'trade_date')
    if date:
        return str(date)
    return str(_get(row, 'ingest_time') or '')[:10]


def _round1(value: float) -> float:
    """统一保留 1 位小数：指标是给人看的，多余的小数位只会制造假精度。"""
    return round(float(value), 1)


def drop_unreadable(rows) -> list:
    """★ 剔掉「引擎没读懂」的观点块（``confidence == 0``），它们不参与任何计票。

    打分层对规则未命中的块写的是 ``direction=0.0, confidence=0.0``
    （见 score.classify_advice 的 rule_miss 分支）。**这个 0 的含义是「不知道」，
    不是「确实看平」**——把它当成一张中性票投进来，等于让引擎的失败悄悄
    把指标往中性拉，覆盖家数还会虚高。这与 NA ≠ 0 是同一条口径，
    只不过 NA ≠ 0 管的是「没人写」，这里管的是「写了但没读懂」。

    生产版这批块会被送去 LLM / 人工复核，判完了再写一行新的打分（只追加），
    下次重算聚合时自然就被算进来了。
    """
    return [row for row in rows if float(_get(row, 'confidence') or 0.0) > 0.0]


def _tally(rows) -> tuple[int, int, int]:
    """按多空计数口径把一组观点块数成 (看多, 看空, 中性)。

    阈值取自 config，正负对称：direction > +0.25 记多，< -0.25 记空，
    落在 [-0.25, +0.25] 之间的才记中性。

    ★ ±0.5 的「谨慎偏多 / 谨慎偏空」在这条口径下**算一票多 / 一票空**，
      不是中性——「暂不看空」表达的确实是方向，只是强度弱。
      强度的信息保留在 score.direction 里（矩阵格子的 + / ++ 就是靠它分档），
      计票这一层只关心方向的符号。
    """
    bull = bear = neutral = 0
    for row in rows:
        direction = float(_get(row, 'direction') or 0.0)
        if direction > config.BULL_CUTOFF:
            bull += 1
        elif direction < config.BEAR_CUTOFF:
            bear += 1
        else:
            neutral += 1
    return bull, bear, neutral


def _net_score(bull: int, bear: int, neutral: int,
               bear_weight: float | None = None) -> float:
    """净得分 = (看多 − W×看空) ÷ 总数 × 100，范围 −100 ~ +100。

    W = ``config.BEAR_WEIGHT``，即架构方案第五节点名的**负面非对称加权**——
    分析师不轻易喊空，正负等权会让指标长期漂在偏多区而失去区分度，
    所以空头票要加成。★ 这个系数只能活在聚合层：它是个会反复调参的口径，
    放在打分层的话每调一次就要重跑一遍全量打分（生产版要花 LLM 的钱）。
    受 calc_version 管理——改了它就必须升 calc_version，
    新旧两套口径的曲线才能并排存在库里对比（见 db.daily_index 的主键）。

    ★ **demo 版 W 恒为 1.0**，退化成架构方案里那条「必须先落地的 baseline 公式」
      （IndexMundi 净得分）。不是没实现，是顺序问题：先跑通最笨的等权口径当对照组，
      任何加权都要证明能跑赢它。3 天 24 篇虚构样本没有任何可以给 W 定标的数据，
      凭空拍一个 1.3 上去只是把数字调得更难看，换不来可验证的增量。
      详见 config.BEAR_WEIGHT 的注释。

    加权后仍夹在 [-100, +100] 内：W > 1 时全空的极端组会算出 −100×W，
    超出量纲会让「−100 = 全市场一致看空」这条刻度失去意义。
    total 为 0 的组不会被写库（NA ≠ 0，见 aggregate_day），这里只是兜底防除零。
    """
    total = bull + bear + neutral
    if total == 0:
        return 0.0
    weight = config.BEAR_WEIGHT if bear_weight is None else bear_weight
    raw = (bull - weight * bear) / total * 100
    return _round1(max(-100.0, min(100.0, raw)))


def _coverage(rows) -> int:
    """覆盖机构家数 = 去重后的机构名个数。★ 与方向完全无关，只测热度。

    注意品种级已经「一家一票」，所以品种级的 coverage 等于观点块数；
    但板块级/大盘级一家会覆盖多个品种，此时 coverage（家数）必然小于
    观点块数（票数），这正是两个维度分开存的意义。
    """
    return len({_get(row, 'org_name') for row in rows})


def _make_index(trade_date: str, level: str, key: str, rows,
                model_version: str, calc_version: str) -> DailyIndex:
    """把一组观点块结算成一条 DailyIndex。★ 两个版本号都要落在这一行上。"""
    bull, bear, neutral = _tally(rows)
    return DailyIndex(
        trade_date=trade_date,
        level=level,
        key=key,
        net_score=_net_score(bull, bear, neutral),
        bull_count=bull,
        bear_count=bear,
        neutral_count=neutral,
        coverage_count=_coverage(rows),
        model_version=model_version,
        calc_version=calc_version,
    )


# ---------------------------------------------------------------- 一家一票


def dedupe_one_org_one_vote(rows) -> list:
    """★ 一家一票：同一 (机构, 品种, 交易日) 只保留 ingest_time 最新的一条。

    这是口径的一部分，不是性能优化——同一家机构当天对沪铜发了早报又发午评，
    两篇都计票的话，这家机构就在沪铜上投了两票。

    - 分组键里的品种用 product_code，None（宏观块）自成一组：
      一家机构对大盘的判断同样只该有一票。
    - 排序键为 (ingest_time, report_id, segment_id)，取最大者。
      后两项只是为了在 ingest_time 完全相同时结果稳定可复现。
    - 返回顺序保持入参中「幸存行」的原始相对顺序。
    """
    best: dict[tuple, tuple] = {}
    for idx, row in enumerate(rows):
        group_key = (
            _get(row, 'org_name'),
            _get(row, 'product_code'),
            _trade_date_of(row),
        )
        rank = (
            str(_get(row, 'ingest_time') or ''),
            int(_get(row, 'report_id') or 0),
            int(_get(row, 'segment_id') or 0),
        )
        kept = best.get(group_key)
        if kept is None or rank > kept[0]:
            best[group_key] = (rank, idx, row)
    return [row for _, _, row in sorted(best.values(), key=lambda item: item[1])]


# ---------------------------------------------------------------- 三级聚合


def aggregate_day(conn: sqlite3.Connection, trade_date: str,
                  model_version: str | None = None,
                  calc_version: str | None = None) -> list[DailyIndex]:
    """把某个**入库日**的全部打分结算成品种 / 板块 / 大盘三级指标。

    步骤：
      1. 取该 ingest 日期的所有已打分观点块（db.fetch_scored_segments，按
         ingest_time 过滤，不按 publish_time）
      2. 一家一票去重
      3. 品种级：按 product_code 分组，无覆盖的品种**不产生记录**（NA ≠ 0）
      4. 板块级：该板块下所有观点块**直接汇总**，不是对品种净值再平均
      5. 大盘级：全部观点块汇总，含宏观块

    ★ 写库前先删掉本日**这一对版本号**的旧记录：重算是聚合层的常态，若只做 upsert，
      某个品种今天不再被覆盖时会残留昨天的旧值，等于凭空捏造了一个覆盖——
      这同样是在破坏 NA ≠ 0。
      而 DELETE 必须同时带上 model_version：只按 calc_version 删的话，
      换个打分口径重跑聚合会把旧打分口径的那批指标行一起抹掉，
      「打分口径变了也能重算历史并对比新旧差异」就不成立了。

    ★ model_version 不传则取 config.SCORE_VERSION，而**不是「随便哪版最新的打分」**：
      一条指标必须说得清自己是哪一版打分口径算出来的，否则库里同时存在
      rule-v1 / rule-v2 两批打分时，这行指标的来源就是薛定谔的。
    """
    version = calc_version or config.CALC_VERSION
    score_version = model_version or config.SCORE_VERSION
    rows = db.fetch_scored_segments(conn, ingest_date=trade_date, model_version=score_version)
    voted = dedupe_one_org_one_vote(drop_unreadable(rows))

    by_product: dict[str, list] = {}
    by_sector: dict[str, list] = {}
    for row in voted:
        code = _get(row, 'product_code')
        if not code:
            # 宏观/综述块：进大盘，不进任何品种和板块
            continue
        by_product.setdefault(str(code), []).append(row)
        sector = _get(row, 'sector') or products.get_sector(code)
        if sector:
            by_sector.setdefault(str(sector), []).append(row)

    records: list[DailyIndex] = []
    for code in sorted(by_product):
        records.append(_make_index(trade_date, 'product', code, by_product[code],
                                   score_version, version))
    for sector in sorted(by_sector):
        records.append(_make_index(trade_date, 'sector', sector, by_sector[sector],
                                   score_version, version))
    if voted:
        records.append(_make_index(trade_date, 'market', 'MARKET', voted,
                                   score_version, version))

    conn.execute(
        'DELETE FROM daily_index '
        'WHERE trade_date = ? AND calc_version = ? AND model_version = ?',
        (trade_date, version, score_version),
    )
    conn.commit()
    for record in records:
        db.upsert_daily_index(conn, record)
    return records


def market_score_0_100(net_score: float) -> float:
    """净得分（−100 ~ +100）映射成大盘情绪分（0 ~ 100），保留 1 位小数。

    换算只是为了好读：60 以上偏多、40 以下偏空，比「净得分 +20」直观。
    """
    return _round1((float(net_score) + 100) / 2)


def aggregate_all(conn: sqlite3.Connection,
                  model_version: str | None = None,
                  calc_version: str | None = None) -> dict[str, list[DailyIndex]]:
    """对库里所有有打分数据的入库日各跑一遍聚合。

    返回 {交易日: [DailyIndex, ...]}，按日期升序。
    没有任何打分数据的日子压根不会出现在结果里（NA ≠ 0）。
    """
    score_version = model_version or config.SCORE_VERSION
    rows = drop_unreadable(db.fetch_scored_segments(conn, model_version=score_version))
    dates = sorted({_trade_date_of(row) for row in rows if _trade_date_of(row)})
    return {
        date: aggregate_day(conn, date, model_version=score_version, calc_version=calc_version)
        for date in dates
    }


# ---------------------------------------------------------------- 环比 / 榜单


def _index_dates(conn: sqlite3.Connection, calc_version: str,
                 model_version: str) -> list[str]:
    """所有已算出指标的交易日，升序。"""
    return db.list_index_dates(conn, calc_version, model_version)


def _previous_index_date(conn: sqlite3.Connection, trade_date: str,
                         calc_version: str, model_version: str) -> str | None:
    """上一个**有指标的**交易日；没有则 None。

    用「上一个有数据的日子」而不是自然日减一：周末和节假日没有研报，
    自然日减一会天天算出 delta=None，环比就废了。
    """
    earlier = [d for d in _index_dates(conn, calc_version, model_version) if d < trade_date]
    return earlier[-1] if earlier else None


def _index_row(conn: sqlite3.Connection, trade_date: str | None, level: str, key: str,
               calc_version: str, model_version: str):
    """取某天某个 key 的指标行，没有则 None。"""
    if not trade_date:
        return None
    for row in db.fetch_daily_index(conn, trade_date, level=level,
                                    calc_version=calc_version, model_version=model_version):
        if row['key'] == key:
            return row
    return None


def compare_with_previous(conn: sqlite3.Connection, trade_date: str,
                          level: str = 'market', key: str = 'MARKET',
                          calc_version: str | None = None,
                          model_version: str | None = None) -> dict:
    """环比：本期 vs 上一个有指标的交易日。用于推送里的「较昨日 +5」。

    ★ 没有前一日数据时 delta 为 **None，不是 0**。填 0 会被读成「跟昨天一样」，
      而真实含义是「昨天根本没有可比的数」——又一次 NA ≠ 0。

    返回::

        {'current': 净得分, 'prev': 净得分, 'delta': 差值, 'prev_date': 'YYYY-MM-DD',
         'current_score': 0-100 分, 'prev_score': …, 'delta_score': …,
         'trade_date': …, 'level': …, 'key': …}

    net 口径与 0-100 口径都给：曲线和阈值判断用 net，推送文案用 0-100 分。
    """
    version = calc_version or config.CALC_VERSION
    score_version = model_version or config.SCORE_VERSION
    current_row = _index_row(conn, trade_date, level, key, version, score_version)
    prev_date = _previous_index_date(conn, trade_date, version, score_version)
    prev_row = _index_row(conn, prev_date, level, key, version, score_version)

    current = float(current_row['net_score']) if current_row is not None else None
    prev = float(prev_row['net_score']) if prev_row is not None else None
    delta = _round1(current - prev) if (current is not None and prev is not None) else None

    current_score = market_score_0_100(current) if current is not None else None
    prev_score = market_score_0_100(prev) if prev is not None else None
    delta_score = (_round1(current_score - prev_score)
                   if (current_score is not None and prev_score is not None) else None)

    return {
        'trade_date': trade_date,
        'level': level,
        'key': key,
        'current': current,
        'prev': prev,
        'delta': delta,
        # 上一期的日期：只有真的取到了上一期的值才给，否则为 None，避免误读
        'prev_date': prev_date if prev_row is not None else None,
        'current_score': current_score,
        'prev_score': prev_score,
        'delta_score': delta_score,
        'coverage_count': int(current_row['coverage_count']) if current_row is not None else 0,
    }


def _product_name(code: str) -> str:
    """品种代码 → 中文名，词典里没有就退回代码本身。"""
    prod = products.get_product(code)
    return prod['name'] if prod else code


def top_movers(conn: sqlite3.Connection, trade_date: str, n: int = 3,
               calc_version: str | None = None,
               model_version: str | None = None) -> list[dict]:
    """变化最大的品种：与**上一个有指标的交易日**比 net_score 的绝对变化，降序。

    ★ 上一期没有覆盖的品种直接跳过，不当成 0 去算变化——
      「昨天没人聊」和「昨天大家看中性」是两回事，混为一谈会凭空造出一堆假异动。
    ★ 变化为 0 的品种不算「变化最大」，一并过滤，免得榜单被填充满噪音。
    """
    version = calc_version or config.CALC_VERSION
    score_version = model_version or config.SCORE_VERSION
    prev_date = _previous_index_date(conn, trade_date, version, score_version)
    if not prev_date:
        return []
    prev_map = {row['key']: row for row in
                db.fetch_daily_index(conn, prev_date, level='product',
                                     calc_version=version, model_version=score_version)}

    movers: list[dict] = []
    for row in db.fetch_daily_index(conn, trade_date, level='product',
                                    calc_version=version, model_version=score_version):
        prev_row = prev_map.get(row['key'])
        if prev_row is None:
            continue
        delta = _round1(float(row['net_score']) - float(prev_row['net_score']))
        if delta == 0:
            continue
        movers.append({
            'key': row['key'],
            'name': _product_name(row['key']),
            'current': float(row['net_score']),
            'prev': float(prev_row['net_score']),
            'prev_date': prev_date,
            'delta': delta,
            'coverage_count': int(row['coverage_count']),
        })
    movers.sort(key=lambda item: (-abs(item['delta']), item['key']))
    return movers[:n]


def most_divergent(conn: sqlite3.Connection, trade_date: str, n: int = 3,
                   calc_version: str | None = None,
                   model_version: str | None = None) -> list[dict]:
    """分歧最大的品种：用于推送里的「分歧最大：原油（4 家看多 / 5 家看空）」。

    口径：看多、看空都不为 0（真有对立观点），
    先按 min(看多, 看空) 降序（对立得越充分分歧越实），
    再按 |净得分| 升序（越接近势均力敌分歧越大）。

    ★ 分歧本身就是信息：净得分 0 可能是「没人有观点」，也可能是「4 多 5 空吵翻天」，
      这个榜单就是用来把后者从前者里拎出来的。
    """
    version = calc_version or config.CALC_VERSION
    score_version = model_version or config.SCORE_VERSION
    items: list[dict] = []
    for row in db.fetch_daily_index(conn, trade_date, level='product',
                                    calc_version=version, model_version=score_version):
        bull, bear = int(row['bull_count']), int(row['bear_count'])
        if bull == 0 or bear == 0:
            continue
        items.append({
            'key': row['key'],
            'name': _product_name(row['key']),
            'bull': bull,
            'bear': bear,
            'neutral': int(row['neutral_count']),
            'net_score': float(row['net_score']),
            'coverage_count': int(row['coverage_count']),
        })
    items.sort(key=lambda item: (-min(item['bull'], item['bear']),
                                 abs(item['net_score']),
                                 item['key']))
    return items[:n]


# ---------------------------------------------------------------- 对照组


def naive_baseline(conn: sqlite3.Connection, trade_date: str,
                   model_version: str | None = None) -> dict:
    """★ 最笨的对照组：只数「多少家看多、多少家看空」。

    它和正式指标差在两点，正好卡住「复杂度有没有换来价值」这个问题：

    ==============  ==========================  ==========================
    维度            正式指标                    naive baseline
    ==============  ==========================  ==========================
    计票单位        观点块（一家可投多个品种）  **机构**（一家就是一票）
    方向判定        阈值 ±0.25，带强度刻度      **只看正负号，完全不看强度**
    正负权重        config.BEAR_WEIGHT          **恒等权**
    ==============  ==========================  ==========================

    ★ baseline 一侧的公式**永远保持等权**，即使 BEAR_WEIGHT 被调成 1.3。
      它是尺子，尺子不能跟着被测的东西一起变；两条曲线之差正好回答
      「非对称加权到底带来了什么」这个问题。

    机构口径：把该机构当天所有观点块里 direction > 0 与 < 0 的条数一比，
    哪边多这家就算哪边，打平算中性。然后
    ``(看多家数 − 看空家数) ÷ 总家数 × 100``。

    调研结论（华泰研报情感因子的中性化检验）说得很清楚：复杂模型必须证明
    自己相对「数家数」有增量，否则这套打分口径可以被一行 SQL 替代。
    所以 demo 里两个数必须并排显示，而不是只报一个好看的。

    返回里 index_* 是正式口径的同日大盘数，diff = 正式 − baseline。
    当天完全没有数据时，net_score / market_score 为 **None（NA ≠ 0）**。
    """
    # ★ 打分口径必须显式钉住：baseline 是用来和正式指标比的尺子，
    #   两者必须建立在**同一批打分**上，否则比出来的差异里混着口径升版的影响。
    rows = db.fetch_scored_segments(conn, ingest_date=trade_date,
                                    model_version=model_version or config.SCORE_VERSION)
    voted = dedupe_one_org_one_vote(drop_unreadable(rows))

    # --- baseline：先把每家机构自己的多空票数压成一个立场 ---
    per_org: dict[str, list[int]] = {}
    for row in voted:
        direction = float(_get(row, 'direction') or 0.0)
        tally = per_org.setdefault(str(_get(row, 'org_name')), [0, 0])
        if direction > 0:
            tally[0] += 1
        elif direction < 0:
            tally[1] += 1

    bull_orgs = sum(1 for up, down in per_org.values() if up > down)
    bear_orgs = sum(1 for up, down in per_org.values() if down > up)
    total_orgs = len(per_org)
    neutral_orgs = total_orgs - bull_orgs - bear_orgs
    baseline_net = (_round1((bull_orgs - bear_orgs) / total_orgs * 100)
                    if total_orgs else None)

    # --- 正式口径：同一批去重后的观点块，按 ±0.25 阈值数块 ---
    bull, bear, neutral = _tally(voted)
    index_net = _net_score(bull, bear, neutral) if voted else None

    return {
        'trade_date': trade_date,
        # baseline（数家数）
        'bull_orgs': bull_orgs,
        'bear_orgs': bear_orgs,
        'neutral_orgs': neutral_orgs,
        'total_orgs': total_orgs,
        'net_score': baseline_net,
        'market_score': market_score_0_100(baseline_net) if baseline_net is not None else None,
        # 正式指标（数观点块 + 强度阈值）
        'index_bull': bull,
        'index_bear': bear,
        'index_neutral': neutral,
        'index_block_count': len(voted),
        'index_net_score': index_net,
        'index_market_score': market_score_0_100(index_net) if index_net is not None else None,
        # 两者之差：长期盯这个数，才知道复杂口径到底带来了什么
        'diff': (_round1(index_net - baseline_net)
                 if (index_net is not None and baseline_net is not None) else None),
    }
