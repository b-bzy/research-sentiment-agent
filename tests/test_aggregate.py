"""⑥ 聚合层单元测试。

用内存 SQLite 造假数据，重点验证四条**口径**（而不是实现细节）：

- 一家一票：同机构同日同品种发多篇只算一票，取 ingest_time 最新那篇
- NA ≠ 0：没被覆盖的品种不产生记录，没有前值时 delta 是 None 不是 0
- 净得分公式：(看多 − 看空) ÷ 总数 × 100
- 热度与方向分离：coverage_count 数的是机构家数，跟多空方向无关

口径写错不会报错，只会安静地算出一个看起来很合理的错数字，
所以这些用例本质上是「产品定义的可执行版本」。
"""

import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sentiment import aggregate, config, db          # noqa: E402
from sentiment.models import Report, Score, Segment  # noqa: E402

# 造数据时用来生成互不相同的 file_md5（report 表对它有唯一约束）
_SEQ = [0]


def _next_md5() -> str:
    _SEQ[0] += 1
    return hashlib.md5(f'fake-{_SEQ[0]}'.encode()).hexdigest()


def _new_conn():
    """每个用例一个全新的内存库。"""
    conn = db.connect(':memory:')
    db.init_schema(conn)
    return conn


def _add_report(conn, org: str, ingest_time: str, publish_time: str | None = None) -> int:
    """插入一篇研报，返回 report_id。"""
    report = Report(
        org_name=org,
        title=f'{org}日报',
        publish_time=publish_time or ingest_time[:10],
        ingest_time=ingest_time,
        source_channel='inbox',
        file_path=f'/tmp/{org}-{_SEQ[0]}.txt',
        file_md5=_next_md5(),
        parse_status='ok',
        parsed_text='正文',
    )
    report_id = db.insert_report(conn, report)
    assert report_id is not None
    return report_id


def _add_view(conn, report_id: int, code: str | None, direction: float,
              sector: str | None = None, seq: int = 0) -> int:
    """给某篇研报加一个「品种 + 方向」的观点块（segment + score 一条龙）。"""
    segment = Segment(
        report_id=report_id,
        product_code=code,
        product_name=code,
        sector=sector,
        raw_text='行情回顾……逻辑分析……',
        advice_text='操作建议：逢低偏多',
        seq=seq,
    )
    segment_id = db.insert_segment(conn, segment)
    db.insert_score(conn, Score(
        segment_id=segment_id,
        direction=direction,
        confidence=0.8,
        evidence='操作建议：逢低偏多',
        method='rule',
        model_version=config.SCORE_VERSION,
    ))
    return segment_id


def _one(conn, org: str, code: str | None, direction: float,
         ingest_time: str = '2026-09-10T07:30:00', sector: str | None = None) -> int:
    """最常用的造数快捷方式：一家机构 = 一篇研报 = 一个品种观点。"""
    report_id = _add_report(conn, org, ingest_time)
    return _add_view(conn, report_id, code, direction, sector=sector)


def _by_key(records, level: str) -> dict:
    """把 aggregate_day 的返回按 key 索引，方便断言。"""
    return {r.key: r for r in records if r.level == level}


class TestDedupeOneOrgOneVote(unittest.TestCase):
    """★ 一家一票。"""

    def test_同机构同品种同日多篇只留最新一篇(self):
        rows = [
            {'org_name': '永安期货', 'product_code': 'CU', 'trade_date': '2026-09-10',
             'ingest_time': '2026-09-10T07:00:00', 'report_id': 1, 'segment_id': 1,
             'direction': 1.0},
            {'org_name': '永安期货', 'product_code': 'CU', 'trade_date': '2026-09-10',
             'ingest_time': '2026-09-10T13:00:00', 'report_id': 2, 'segment_id': 2,
             'direction': -1.0},
        ]
        kept = aggregate.dedupe_one_org_one_vote(rows)
        self.assertEqual(len(kept), 1)
        # 取 ingest_time 最新的那篇：午评（看空）覆盖早报（看多）
        self.assertEqual(kept[0]['segment_id'], 2)
        self.assertEqual(kept[0]['direction'], -1.0)

    def test_不同机构不同品种不同日都不会被误删(self):
        rows = [
            {'org_name': '永安期货', 'product_code': 'CU', 'trade_date': '2026-09-10',
             'ingest_time': '2026-09-10T07:00:00', 'report_id': 1, 'segment_id': 1},
            {'org_name': '南华期货', 'product_code': 'CU', 'trade_date': '2026-09-10',
             'ingest_time': '2026-09-10T07:00:00', 'report_id': 2, 'segment_id': 2},
            {'org_name': '永安期货', 'product_code': 'RB', 'trade_date': '2026-09-10',
             'ingest_time': '2026-09-10T07:00:00', 'report_id': 1, 'segment_id': 3},
            {'org_name': '永安期货', 'product_code': 'CU', 'trade_date': '2026-09-11',
             'ingest_time': '2026-09-11T07:00:00', 'report_id': 3, 'segment_id': 4},
        ]
        self.assertEqual(len(aggregate.dedupe_one_org_one_vote(rows)), 4)

    def test_宏观块也遵守一家一票(self):
        # product_code=None 自成一组：一家机构对大盘同样只该有一票
        rows = [
            {'org_name': '永安期货', 'product_code': None, 'trade_date': '2026-09-10',
             'ingest_time': '2026-09-10T07:00:00', 'report_id': 1, 'segment_id': 1},
            {'org_name': '永安期货', 'product_code': None, 'trade_date': '2026-09-10',
             'ingest_time': '2026-09-10T12:00:00', 'report_id': 2, 'segment_id': 2},
        ]
        kept = aggregate.dedupe_one_org_one_vote(rows)
        self.assertEqual([r['segment_id'] for r in kept], [2])

    def test_聚合时同机构重复发报只计一票(self):
        conn = _new_conn()
        # 永安早报看多沪铜，午间改口看空 → 只认午间那票
        _one(conn, '永安期货', 'CU', 2.0, ingest_time='2026-09-10T07:00:00')
        _one(conn, '永安期货', 'CU', -2.0, ingest_time='2026-09-10T13:00:00')
        records = aggregate.aggregate_day(conn, '2026-09-10')
        cu = _by_key(records, 'product')['CU']
        self.assertEqual((cu.bull_count, cu.bear_count, cu.neutral_count), (0, 1, 0))
        self.assertEqual(cu.coverage_count, 1)   # 两篇研报，仍然只有一家机构
        self.assertEqual(cu.net_score, -100.0)


class TestNetScore(unittest.TestCase):
    """净得分公式与多空计数阈值。"""

    def test_净得分公式(self):
        conn = _new_conn()
        # 2 多 1 空 1 中 → (2-1)/4*100 = 25.0
        _one(conn, 'A期货', 'CU', 1.0)
        _one(conn, 'B期货', 'CU', 2.0)
        _one(conn, 'C期货', 'CU', -1.0)
        _one(conn, 'D期货', 'CU', 0.0)
        cu = _by_key(aggregate.aggregate_day(conn, '2026-09-10'), 'product')['CU']
        self.assertEqual((cu.bull_count, cu.bear_count, cu.neutral_count), (2, 1, 1))
        self.assertEqual(cu.net_score, 25.0)
        # 净得分与 0-100 分的换算
        self.assertEqual(aggregate.market_score_0_100(cu.net_score), 62.5)

    def test_多空计数阈值边界(self):
        conn = _new_conn()
        # 恰好等于 ±0.25 的「谨慎偏多/偏空」计中性，超过才计票
        _one(conn, 'A期货', 'CU', config.BULL_CUTOFF)     # +0.25 → 中性
        _one(conn, 'B期货', 'CU', config.BEAR_CUTOFF)     # -0.25 → 中性
        _one(conn, 'C期货', 'CU', 0.5)                    # → 看多
        _one(conn, 'D期货', 'CU', -0.5)                   # → 看空
        cu = _by_key(aggregate.aggregate_day(conn, '2026-09-10'), 'product')['CU']
        self.assertEqual((cu.bull_count, cu.bear_count, cu.neutral_count), (1, 1, 2))
        self.assertEqual(cu.net_score, 0.0)

    def test_大盘分换算(self):
        self.assertEqual(aggregate.market_score_0_100(-100), 0.0)
        self.assertEqual(aggregate.market_score_0_100(0), 50.0)
        self.assertEqual(aggregate.market_score_0_100(100), 100.0)
        self.assertEqual(aggregate.market_score_0_100(33.3), 66.7)


class TestNaIsNotZero(unittest.TestCase):
    """★ NA ≠ 0：没覆盖就没有记录，绝不写 0。"""

    def test_无覆盖品种不产生记录(self):
        conn = _new_conn()
        _one(conn, 'A期货', 'CU', 1.0)
        records = aggregate.aggregate_day(conn, '2026-09-10')
        keys = _by_key(records, 'product').keys()
        self.assertEqual(set(keys), {'CU'})
        # 螺纹钢当天没人写 → 库里查不到它，而不是查到一条 net_score=0
        self.assertEqual(
            [r for r in db.fetch_daily_index(conn, '2026-09-10', level='product')
             if r['key'] == 'RB'],
            [],
        )

    def test_当天完全没数据则一条记录都不写(self):
        conn = _new_conn()
        _one(conn, 'A期货', 'CU', 1.0)
        self.assertEqual(aggregate.aggregate_day(conn, '2026-09-11'), [])
        self.assertEqual(db.fetch_daily_index(conn, '2026-09-11'), [])

    def test_重算时不再被覆盖的品种记录会消失(self):
        conn = _new_conn()
        _one(conn, 'A期货', 'CU', 1.0)
        aggregate.aggregate_day(conn, '2026-09-10')
        self.assertTrue(db.fetch_daily_index(conn, '2026-09-10', level='product'))
        # 打分被撤掉后重算：旧记录必须清掉，不能残留一个「昨天的沪铜 +100」
        conn.execute('DELETE FROM score')
        conn.commit()
        self.assertEqual(aggregate.aggregate_day(conn, '2026-09-10'), [])
        self.assertEqual(db.fetch_daily_index(conn, '2026-09-10'), [])


class TestCoverageIsIndependentOfDirection(unittest.TestCase):
    """★ 热度与方向分离。"""

    def test_全中性也有覆盖家数(self):
        conn = _new_conn()
        for org in ('A期货', 'B期货', 'C期货'):
            _one(conn, org, 'CU', 0.0)
        cu = _by_key(aggregate.aggregate_day(conn, '2026-09-10'), 'product')['CU']
        self.assertEqual(cu.net_score, 0.0)          # 方向为 0
        self.assertEqual(cu.coverage_count, 3)       # 热度照样是 3 家
        self.assertEqual(cu.neutral_count, 3)

    def test_方向相同但家数不同时净得分一样热度不同(self):
        conn_small, conn_big = _new_conn(), _new_conn()
        for org in ('A期货', 'B期货'):
            _one(conn_small, org, 'CU', 1.0)
        for org in ('A期货', 'B期货', 'C期货', 'D期货', 'E期货'):
            _one(conn_big, org, 'CU', 1.0)
        small = _by_key(aggregate.aggregate_day(conn_small, '2026-09-10'), 'product')['CU']
        big = _by_key(aggregate.aggregate_day(conn_big, '2026-09-10'), 'product')['CU']
        self.assertEqual(small.net_score, big.net_score)     # 方向一样都是 +100
        self.assertEqual((small.coverage_count, big.coverage_count), (2, 5))

    def test_板块级覆盖数的是机构家数不是观点块数(self):
        conn = _new_conn()
        # 一家机构在有色板块写了 3 个品种 → 3 个观点块，但只有 1 家覆盖
        report_id = _add_report(conn, '永安期货', '2026-09-10T07:30:00')
        _add_view(conn, report_id, 'CU', 1.0, sector='有色', seq=0)
        _add_view(conn, report_id, 'AL', 1.0, sector='有色', seq=1)
        _add_view(conn, report_id, 'ZN', -1.0, sector='有色', seq=2)
        sector = _by_key(aggregate.aggregate_day(conn, '2026-09-10'), 'sector')['有色']
        self.assertEqual(sector.bull_count + sector.bear_count + sector.neutral_count, 3)
        self.assertEqual(sector.coverage_count, 1)


class TestThreeLevels(unittest.TestCase):
    """品种 / 板块 / 大盘三级的分工。"""

    def test_板块是把观点块直接汇总而不是对品种净值再平均(self):
        conn = _new_conn()
        # 冷门品种：1 家看空；主力品种：4 家看多
        _one(conn, 'A期货', 'LC', -2.0, sector='能化')
        for org in ('A期货', 'B期货', 'C期货', 'D期货'):
            _one(conn, org, 'MA', 2.0, sector='能化')
        sector = _by_key(aggregate.aggregate_day(conn, '2026-09-10'), 'sector')['能化']
        # 观点块直接汇总：(4-1)/5*100 = 60；若按品种净值等权平均会得到 (100-100)/2 = 0
        self.assertEqual((sector.bull_count, sector.bear_count), (4, 1))
        self.assertEqual(sector.net_score, 60.0)

    def test_宏观块进大盘不进板块也不进品种(self):
        conn = _new_conn()
        _one(conn, 'A期货', 'CU', 1.0, sector='有色')
        _one(conn, 'B期货', None, -2.0)          # 宏观/综述块
        records = aggregate.aggregate_day(conn, '2026-09-10')
        self.assertEqual(set(_by_key(records, 'product')), {'CU'})
        self.assertEqual(set(_by_key(records, 'sector')), {'有色'})
        market = _by_key(records, 'market')['MARKET']
        self.assertEqual((market.bull_count, market.bear_count), (1, 1))   # 宏观块计入大盘
        self.assertEqual(market.net_score, 0.0)
        self.assertEqual(market.coverage_count, 2)
        # 有色板块只有沪铜那一票，宏观块没混进来
        self.assertEqual(_by_key(records, 'sector')['有色'].bull_count, 1)

    def test_板块用词典兜底推断(self):
        conn = _new_conn()
        # 切分层没写 sector 时，聚合层按品种词典补：CU → 有色
        _one(conn, 'A期货', 'CU', 1.0, sector=None)
        self.assertIn('有色', _by_key(aggregate.aggregate_day(conn, '2026-09-10'), 'sector'))


class TestTradeDateSource(unittest.TestCase):
    """★ 交易日取自 ingest_time，不是 publish_time。"""

    def test_按入库日归集不按发布日(self):
        conn = _new_conn()
        # 周五（09-04）发布、周一（09-07）才入库 → 必须算进 09-07
        report_id = _add_report(conn, '永安期货', '2026-09-07T07:30:00',
                                publish_time='2026-09-04')
        _add_view(conn, report_id, 'CU', 2.0)
        self.assertEqual(aggregate.aggregate_day(conn, '2026-09-04'), [])
        self.assertTrue(aggregate.aggregate_day(conn, '2026-09-07'))


class TestAggregateAll(unittest.TestCase):

    def test_跑遍所有有数据的日期(self):
        conn = _new_conn()
        _one(conn, 'A期货', 'CU', 1.0, ingest_time='2026-09-09T07:30:00')
        _one(conn, 'B期货', 'CU', -1.0, ingest_time='2026-09-10T07:30:00')
        result = aggregate.aggregate_all(conn)
        self.assertEqual(sorted(result), ['2026-09-09', '2026-09-10'])
        self.assertEqual(db.list_index_dates(conn), ['2026-09-09', '2026-09-10'])
        self.assertEqual(_by_key(result['2026-09-10'], 'market')['MARKET'].net_score, -100.0)


class TestCompareWithPrevious(unittest.TestCase):
    """★ 环比：没有前值时 delta 必须是 None，不是 0。"""

    def test_没有前一日数据时delta为None(self):
        conn = _new_conn()
        _one(conn, 'A期货', 'CU', 1.0, ingest_time='2026-09-10T07:30:00')
        aggregate.aggregate_all(conn)
        cmp = aggregate.compare_with_previous(conn, '2026-09-10')
        self.assertEqual(cmp['current'], 100.0)
        self.assertIsNone(cmp['prev'])
        self.assertIsNone(cmp['delta'])          # ← 绝不能是 0
        self.assertIsNone(cmp['delta_score'])
        self.assertIsNone(cmp['prev_date'])

    def test_有前一日数据时算出差值(self):
        conn = _new_conn()
        _one(conn, 'A期货', 'CU', 0.0, ingest_time='2026-09-09T07:30:00')   # 净值 0
        _one(conn, 'A期货', 'CU', 1.0, ingest_time='2026-09-10T07:30:00')   # 净值 +100
        aggregate.aggregate_all(conn)
        cmp = aggregate.compare_with_previous(conn, '2026-09-10')
        self.assertEqual(cmp['prev_date'], '2026-09-09')
        self.assertEqual((cmp['prev'], cmp['current'], cmp['delta']), (0.0, 100.0, 100.0))
        # 0-100 分口径：50 → 100，较昨日 +50
        self.assertEqual((cmp['prev_score'], cmp['current_score'], cmp['delta_score']),
                         (50.0, 100.0, 50.0))

    def test_品种昨日无覆盖时delta也是None(self):
        conn = _new_conn()
        _one(conn, 'A期货', 'CU', 1.0, ingest_time='2026-09-09T07:30:00')
        _one(conn, 'A期货', 'RB', 1.0, ingest_time='2026-09-10T07:30:00')
        aggregate.aggregate_all(conn)
        cmp = aggregate.compare_with_previous(conn, '2026-09-10', level='product', key='RB')
        self.assertEqual(cmp['current'], 100.0)
        self.assertIsNone(cmp['prev'])           # 昨天没人写螺纹，不是 0 而是没有
        self.assertIsNone(cmp['delta'])


class TestTopMovers(unittest.TestCase):

    def test_按净值绝对变化排序且跳过昨日无覆盖的品种(self):
        conn = _new_conn()
        # 09-09：沪铜全空(-100)、螺纹全多(+100)
        for org in ('A期货', 'B期货'):
            _one(conn, org, 'CU', -1.0, ingest_time='2026-09-09T07:30:00')
            _one(conn, org, 'RB', 1.0, ingest_time='2026-09-09T07:30:00')
        # 09-10：沪铜翻多(+100，变化 200)、螺纹转中性(0，变化 -100)、原油首次覆盖
        for org in ('A期货', 'B期货'):
            _one(conn, org, 'CU', 1.0, ingest_time='2026-09-10T07:30:00')
            _one(conn, org, 'RB', 0.0, ingest_time='2026-09-10T07:30:00')
            _one(conn, org, 'SC', 1.0, ingest_time='2026-09-10T07:30:00')
        aggregate.aggregate_all(conn)
        movers = aggregate.top_movers(conn, '2026-09-10')
        self.assertEqual([m['key'] for m in movers], ['CU', 'RB'])   # 原油无前值，被跳过
        self.assertEqual(movers[0]['delta'], 200.0)
        self.assertEqual(movers[1]['delta'], -100.0)
        self.assertEqual(movers[0]['name'], '沪铜')
        self.assertEqual(movers[0]['prev_date'], '2026-09-09')

    def test_第一天没有前值则榜单为空(self):
        conn = _new_conn()
        _one(conn, 'A期货', 'CU', 1.0, ingest_time='2026-09-10T07:30:00')
        aggregate.aggregate_all(conn)
        self.assertEqual(aggregate.top_movers(conn, '2026-09-10'), [])


class TestMostDivergent(unittest.TestCase):

    def test_分歧榜按对立票数降序净值绝对值升序(self):
        conn = _new_conn()
        orgs = ['A期货', 'B期货', 'C期货', 'D期货', 'E期货',
                'F期货', 'G期货', 'H期货', 'I期货']
        # 原油：4 多 5 空 → min=4，净值 -11.1
        for org in orgs[:4]:
            _one(conn, org, 'SC', 1.0)
        for org in orgs[4:]:
            _one(conn, org, 'SC', -1.0)
        # 沪铜：2 多 2 空 → min=2，净值 0
        for org in orgs[:2]:
            _one(conn, org, 'CU', 1.0)
        for org in orgs[2:4]:
            _one(conn, org, 'CU', -1.0)
        # 螺纹：4 家一边倒，没有分歧 → 不进榜
        for org in orgs[:4]:
            _one(conn, org, 'RB', 1.0)
        aggregate.aggregate_day(conn, '2026-09-10')
        items = aggregate.most_divergent(conn, '2026-09-10')
        self.assertEqual([i['key'] for i in items], ['SC', 'CU'])    # 螺纹被排除
        self.assertEqual((items[0]['bull'], items[0]['bear']), (4, 5))
        self.assertEqual(items[0]['name'], '原油')


class TestNaiveBaseline(unittest.TestCase):
    """★ 对照组：复杂口径必须能跟「数家数」并排比较。"""

    def test_只数家数不看强度(self):
        conn = _new_conn()
        # A 家：两个品种都看多 → 这家算看多
        report_a = _add_report(conn, 'A期货', '2026-09-10T07:30:00')
        _add_view(conn, report_a, 'CU', 2.0, sector='有色', seq=0)
        _add_view(conn, report_a, 'AL', 0.5, sector='有色', seq=1)
        # B 家：一多一空打平 → 这家算中性
        report_b = _add_report(conn, 'B期货', '2026-09-10T07:30:00')
        _add_view(conn, report_b, 'CU', 1.0, sector='有色', seq=0)
        _add_view(conn, report_b, 'AL', -1.0, sector='有色', seq=1)
        # C 家：看空
        _one(conn, 'C期货', 'CU', -2.0, sector='有色')

        base = aggregate.naive_baseline(conn, '2026-09-10')
        self.assertEqual((base['bull_orgs'], base['bear_orgs'], base['neutral_orgs']), (1, 1, 1))
        self.assertEqual(base['total_orgs'], 3)
        self.assertEqual(base['net_score'], 0.0)          # (1-1)/3*100
        self.assertEqual(base['market_score'], 50.0)

    def test_并排给出正式口径与差值(self):
        conn = _new_conn()
        # 一家机构写 3 个看多品种：正式口径按观点块数 = 3 票全多 → +100
        report_a = _add_report(conn, 'A期货', '2026-09-10T07:30:00')
        for seq, code in enumerate(('CU', 'AL', 'ZN')):
            _add_view(conn, report_a, code, 1.0, sector='有色', seq=seq)
        # 另一家只写 1 个看空品种
        _one(conn, 'B期货', 'RB', -1.0, sector='黑色')

        base = aggregate.naive_baseline(conn, '2026-09-10')
        # baseline 按机构：1 多 1 空 → 0
        self.assertEqual(base['net_score'], 0.0)
        # 正式口径按观点块：(3-1)/4*100 = 50
        self.assertEqual(base['index_net_score'], 50.0)
        self.assertEqual(base['index_block_count'], 4)
        self.assertEqual(base['diff'], 50.0)
        # 与实际写库的大盘记录一致
        market = _by_key(aggregate.aggregate_day(conn, '2026-09-10'), 'market')['MARKET']
        self.assertEqual(market.net_score, base['index_net_score'])

    def test_没有数据时baseline为None而不是0(self):
        conn = _new_conn()
        base = aggregate.naive_baseline(conn, '2026-09-10')
        self.assertEqual(base['total_orgs'], 0)
        self.assertIsNone(base['net_score'])       # ← NA ≠ 0
        self.assertIsNone(base['index_net_score'])
        self.assertIsNone(base['diff'])

    def test_baseline同样遵守一家一票(self):
        conn = _new_conn()
        _one(conn, 'A期货', 'CU', 2.0, ingest_time='2026-09-10T07:00:00')
        _one(conn, 'A期货', 'CU', -2.0, ingest_time='2026-09-10T13:00:00')
        base = aggregate.naive_baseline(conn, '2026-09-10')
        self.assertEqual(base['total_orgs'], 1)
        self.assertEqual((base['bull_orgs'], base['bear_orgs']), (0, 1))
        self.assertEqual(base['net_score'], -100.0)


class TestDualVersionOnDailyIndex(unittest.TestCase):
    """★★ 双版本号必须**都**落在 daily_index 上，且一起构成主键。

    架构方案第九节要点 2：「打分口径和聚合口径分别管理，任何一个变了
    都能重算历史并对比新旧差异」。只记 calc_version 的话，这句话只有一半成立：
    打分口径升版后重跑聚合会**静默覆盖**旧口径的指标行。
    """

    @staticmethod
    def _rescore(conn, direction: float, model_version: str) -> None:
        """把库里所有观点块按新口径重打一遍分（只追加，旧行保留）。"""
        for row in conn.execute('SELECT id FROM segment').fetchall():
            db.insert_score(conn, Score(segment_id=int(row['id']), direction=direction,
                                        confidence=0.9, evidence='重打的分',
                                        method='rule', model_version=model_version))

    def test_打分口径升版后新旧指标并存不互相覆盖(self):
        conn = _new_conn()
        _one(conn, 'A期货', 'CU', 1.0)
        _one(conn, 'B期货', 'CU', 1.0)
        aggregate.aggregate_all(conn)
        self.assertEqual(len(db.fetch_daily_index(conn, '2026-09-10', level='market')), 1)

        # 换一版打分口径，把所有块重打成看空，再按同一个聚合口径重算
        self._rescore(conn, -1.0, 'rule-v2')
        aggregate.aggregate_all(conn, model_version='rule-v2')

        rows = conn.execute(
            "SELECT model_version, net_score FROM daily_index "
            "WHERE trade_date='2026-09-10' AND level='market' ORDER BY model_version"
        ).fetchall()
        self.assertEqual([(r['model_version'], r['net_score']) for r in rows],
                         [(config.SCORE_VERSION, 100.0), ('rule-v2', -100.0)],
                         '★ 两版打分口径的指标必须并存，否则没法对比新旧差异')

    def test_查询默认只返回当前打分口径的那一份(self):
        conn = _new_conn()
        _one(conn, 'A期货', 'CU', 1.0)
        aggregate.aggregate_all(conn)
        self._rescore(conn, -1.0, 'rule-v2')
        aggregate.aggregate_all(conn, model_version='rule-v2')

        # 不指定 model_version → 用 config 当前口径，不能一个 key 返回两行
        rows = db.fetch_daily_index(conn, '2026-09-10', level='market')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['model_version'], config.SCORE_VERSION)
        self.assertEqual(rows[0]['net_score'], 100.0)
        # 显式指定则拿到另一份
        other = db.fetch_daily_index(conn, '2026-09-10', level='market',
                                     model_version='rule-v2')
        self.assertEqual(other[0]['net_score'], -100.0)

    def test_聚合口径升版同样并存(self):
        """这一半原本就是达标的，一并钉住，防止改主键时改坏。"""
        conn = _new_conn()
        _one(conn, 'A期货', 'CU', 1.0)
        aggregate.aggregate_all(conn)
        aggregate.aggregate_all(conn, calc_version='netscore-v2TEST')
        self.assertEqual(db.list_index_dates(conn), ['2026-09-10'])
        self.assertEqual(db.list_index_dates(conn, calc_version='netscore-v2TEST'),
                         ['2026-09-10'])
        self.assertEqual(db.count_table(conn, 'daily_index'), 6)   # 3 级 × 2 个口径


class TestBearWeight(unittest.TestCase):
    """★ 负面非对称加权：坑的「加权那一半」落在聚合层，由 config.BEAR_WEIGHT 控制。

    demo 版恒取 1.0（等权），先把 IndexMundi baseline 跑通；
    但**机制必须真的存在**，否则这条设计项就是两层互相甩锅、最后落在地上。
    """

    def test_demo版保持等权即baseline公式(self):
        self.assertEqual(config.BEAR_WEIGHT, 1.0,
                         'demo 必须先落地等权 baseline，改这个值要同时升 CALC_VERSION')
        self.assertEqual(aggregate._net_score(3, 1, 0), 50.0)      # (3-1)/4*100
        self.assertEqual(aggregate._net_score(1, 1, 2), 0.0)

    def test_加权系数生效且不越量纲(self):
        # 空头票加权 1.3：(3 - 1.3×1) / 4 × 100 = 42.5
        self.assertEqual(aggregate._net_score(3, 1, 0, bear_weight=1.3), 42.5)
        # 全空时不能算出 -130：越出量纲会让「-100 = 全市场一致看空」失去意义
        self.assertEqual(aggregate._net_score(0, 4, 0, bear_weight=1.3), -100.0)
        self.assertEqual(aggregate._net_score(4, 0, 0, bear_weight=1.3), 100.0)

    def test_baseline一侧永远等权(self):
        """尺子不能跟着被测的东西一起变。"""
        conn = _new_conn()
        _one(conn, 'A期货', 'CU', 1.0)
        _one(conn, 'B期货', 'CU', -1.0)
        base = aggregate.naive_baseline(conn, '2026-09-10')
        self.assertEqual(base['net_score'], 0.0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
