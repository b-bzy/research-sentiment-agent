"""端到端冒烟测试：在临时目录里把全链路跑一遍。

单模块的单测（test_score / test_aggregate）保证每一环自己是对的，
但**接口对不上**这类问题它们一个都测不出来：
函数签名变了、返回值少了个 key、字段名从 product 改成 product_code……
每个模块的测试依然全绿，`run.py demo` 却跑不起来。

所以这里只做一件事：**用真实的样本研报，走完 ① 采集 → ③ 解析 → ④ 切分 →
⑤ 打分 → ⑥ 聚合 → ⑦ 呈现，断言每一环都真的产出了东西**，
并顺手守住几条口径红线（一家一票、NA ≠ 0、ingest_time 而非 publish_time）。

所有产物写进 ``tempfile.mkdtemp()``，不碰 ``data/sentiment.db`` 和 ``out/``。
"""

import contextlib
import io
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

# 让 `python3 -m unittest discover -s tests` 能 import 到 sentiment 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sentiment import (                                       # noqa: E402
    aggregate,
    config,
    db,
    ingest,
    parse,
    report,
    score,
    segment,
)

MVP_DIR = Path(__file__).resolve().parent.parent
INBOX_DIR = MVP_DIR / 'data' / 'inbox'


class TestPipelineEndToEnd(unittest.TestCase):
    """跑一次全链路，所有断言共用同一份结果（setUpClass 只跑一遍，快）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix='sentiment-e2e-'))
        cls.conn = db.connect(cls.tmp / 'e2e.db')
        db.init_schema(cls.conn)

        # ① 采集：不传 now_iso = demo 回填模式，用文件名日期造出 3 天的序列
        cls.st_ingest = ingest.ingest_inbox(cls.conn, inbox_dir=INBOX_DIR, verbose=False)
        # ③④⑤⑥
        cls.st_parse = parse.parse_pending(cls.conn)
        cls.st_segment = segment.segment_pending(cls.conn)
        cls.st_score = score.score_pending(cls.conn)
        cls.st_aggregate = aggregate.aggregate_all(cls.conn)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ------------------------------------------------------------ 前置
    def test_00_样本目录里有研报(self):
        files = list(INBOX_DIR.glob('*.txt'))
        self.assertTrue(files, f'样本目录空了，demo 跑不起来：{INBOX_DIR}')

    # ------------------------------------------------------------ 各环节记录数 > 0
    def test_01_采集有入库(self):
        self.assertGreater(self.st_ingest['new'], 0, '① 采集一篇都没入库')
        self.assertEqual(self.st_ingest['failed'], 0, '① 采集不该有失败件')
        self.assertEqual(db.count_table(self.conn, 'report'), self.st_ingest['new'])
        self.assertGreater(db.count_table(self.conn, 'org'), 0, '机构表是空的')

    def test_02_解析全部成功(self):
        self.assertGreater(self.st_parse['ok'], 0, '③ 解析一篇都没成功')
        self.assertEqual(self.st_parse['failed'], 0,
                         f'③ 解析有失败件：{self.st_parse["errors"]}')
        self.assertEqual(self.st_parse['ok'], self.st_parse['total'])

    def test_03_切分有产出且每篇都切得出块(self):
        self.assertGreater(self.st_segment['segments'], 0, '④ 一个观点块都没切出来')
        self.assertEqual(self.st_segment['empty'], 0, '④ 有研报一块都没切出来')
        self.assertEqual(db.count_table(self.conn, 'segment'),
                         self.st_segment['segments'])
        # 品种块必须占大头：全是宏观块说明品种词典没挂上
        self.assertGreater(self.st_segment['product'], self.st_segment['macro'])

    def test_04_打分覆盖全部观点块(self):
        self.assertEqual(self.st_score['scored'], db.count_table(self.conn, 'segment'),
                         '⑤ 有观点块没被打分')
        self.assertEqual(db.count_table(self.conn, 'score'), self.st_score['scored'])
        # 规则层至少要吃下一半，否则规则表根本没起作用
        self.assertGreater(self.st_score['rule_hit_rate'], 0.5,
                           f'⑤ 规则命中率过低：{self.st_score}')
        # 三个方向都要有：全是中性 = 打分逻辑被简化成了返回 0
        self.assertGreater(self.st_score['bull'], 0, '⑤ 一条看多都没有')
        self.assertGreater(self.st_score['bear'], 0, '⑤ 一条看空都没有')

    def test_05_聚合出多天序列(self):
        self.assertGreater(len(self.st_aggregate), 1,
                           '⑥ 只聚合出一天，看板画不出时间序列')
        self.assertGreater(db.count_table(self.conn, 'daily_index'), 0)
        for date, records in self.st_aggregate.items():
            with self.subTest(date=date):
                self.assertTrue(records, f'⑥ {date} 一行指标都没产出')
        dates = db.list_index_dates(self.conn)
        self.assertEqual(dates, sorted(dates), '交易日应升序')

    def test_06_三级指标齐全(self):
        for date in db.list_index_dates(self.conn):
            rows = db.fetch_daily_index(self.conn, date)
            levels = {r['level'] for r in rows}
            with self.subTest(date=date):
                self.assertEqual(levels, {'product', 'sector', 'market'},
                                 f'{date} 缺少某一级聚合：{levels}')

    # ------------------------------------------------------------ 口径红线
    def test_07_指标按ingest_time而非publish_time归集(self):
        rows = db.fetch_scored_segments(self.conn)
        self.assertTrue(rows)
        for row in rows[:50]:
            self.assertEqual(row['trade_date'], row['ingest_time'][:10],
                             'trade_date 必须来自 ingest_time')

    def test_08_一家一票不重复计票(self):
        for date in db.list_index_dates(self.conn):
            rows = db.fetch_scored_segments(self.conn, ingest_date=date)
            voted = aggregate.dedupe_one_org_one_vote(rows)
            keys = [(r['org_name'], r['product_code']) for r in voted]
            with self.subTest(date=date):
                self.assertEqual(len(keys), len(set(keys)),
                                 f'{date} 同机构同品种被投了多票')

    def test_09_NA不等于0_无覆盖的品种不产出记录(self):
        """当日没被任何机构写到的品种，daily_index 里必须**没有这一行**。

        ★ 「没被写到」和「写了但引擎没读懂」在这条口径下是同一件事：
          confidence == 0 的块（规则未命中）会被 aggregate.drop_unreadable 剔掉，
          如果一个品种当天**只**被这类块覆盖，它同样不该产出记录——
          否则等于用一次解析失败凭空造出一个「中性」的品种观点。
          所以这里的期望集合必须用同一条口径算，不能直接数原始行。
        """
        from sentiment import products as products_mod
        for date in db.list_index_dates(self.conn):
            covered = {r['product_code'] for r in
                       aggregate.drop_unreadable(
                           db.fetch_scored_segments(self.conn, ingest_date=date))
                       if r['product_code']}
            keys = {r['key'] for r in
                    db.fetch_daily_index(self.conn, date, level='product')}
            with self.subTest(date=date):
                self.assertEqual(keys, covered,
                                 f'{date} 的品种级指标与实际覆盖对不上（NA 被写成 0？）')
                self.assertLess(len(keys), len(products_mod.PRODUCTS),
                                '不该每个品种都有记录，没覆盖的应当缺席')

    def test_10_净得分公式与计数口径自洽(self):
        for date in db.list_index_dates(self.conn):
            for row in db.fetch_daily_index(self.conn, date):
                bull, bear, neutral = (int(row['bull_count']), int(row['bear_count']),
                                       int(row['neutral_count']))
                total = bull + bear + neutral
                with self.subTest(date=date, key=row['key']):
                    self.assertGreater(total, 0, '有记录就必须有票，不能是空壳')
                    self.assertAlmostEqual(float(row['net_score']),
                                           round((bull - bear) / total * 100, 1),
                                           places=6)
                    self.assertGreaterEqual(row['coverage_count'], 1)

    def test_11_证据原句必须是真实原文(self):
        """evidence 是可解释下钻的唯一来源，必须能在原文里找得到。"""
        rows = db.fetch_scored_segments(self.conn)
        for row in rows:
            evidence = (row['evidence'] or '').strip()
            with self.subTest(segment_id=row['segment_id']):
                self.assertTrue(evidence, '证据原句不能为空')
                self.assertTrue(
                    evidence in row['raw_text'] or evidence in (row['advice_text'] or ''),
                    f'证据不是原文子串：{evidence!r}')

    # ------------------------------------------------------------ ⑦ 呈现
    def test_12_呈现三种交付都能出东西(self):
        trade_date = report.resolve_date(self.conn)
        self.assertIsNotNone(trade_date)

        push = report.build_push_message(self.conn, trade_date)
        self.assertIn('大盘情绪', push)
        self.assertIn('baseline', push)

        term = report.render_terminal(self.conn, trade_date, use_color=False)
        self.assertIn('大盘情绪', term)
        self.assertIn('多空矩阵', term)

        matrix = report.render_matrix(self.conn, trade_date, use_color=False)
        self.assertIn('净得分', matrix)

    def test_13_html看板结构完整且数据非空(self):
        trade_date = report.resolve_date(self.conn)
        out = self.tmp / 'dashboard.html'
        path = report.export_html(self.conn, trade_date, out)
        html = Path(path).read_text(encoding='utf-8')

        self.assertTrue(html.startswith('<!DOCTYPE html>'))
        self.assertTrue(html.rstrip().endswith('</html>'))
        for marker in ('<style>', '<header>', '<footer>', 'class="hero"',
                       'table class="mx"', '<svg', 'ingest_time'):
            with self.subTest(marker=marker):
                self.assertIn(marker, html)
        # 折线图必须有点位：<circle> 一个都没有 = 序列是空的
        self.assertGreaterEqual(html.count('<circle'), 2, 'SVG 折线没有点位')
        # 矩阵里必须有真格子（不能整张表都是 NA 斜纹）
        self.assertIn('class="c ', html)
        # 下钻证据 tooltip 必须存在
        self.assertIn('class="tip"', html)

    # ------------------------------------------------------------ 幂等
    def test_14_重复跑不会重复入库(self):
        again = ingest.ingest_inbox(self.conn, inbox_dir=INBOX_DIR, verbose=False)
        self.assertEqual(again['new'], 0, 'MD5 去重失效，同一篇被重复入库')
        self.assertGreater(again['duplicated'], 0)

        self.assertEqual(parse.parse_pending(self.conn)['total'], 0)
        self.assertEqual(segment.segment_pending(self.conn)['reports'], 0)
        self.assertEqual(score.score_pending(self.conn)['total'], 0)

    def test_15_聚合可重算且结果稳定(self):
        """⑥ 是可反复重算的一步：同一口径再算一遍，结果必须一模一样。"""
        before = {(r['trade_date'], r['level'], r['key']): r['net_score']
                  for date in db.list_index_dates(self.conn)
                  for r in db.fetch_daily_index(self.conn, date)}
        aggregate.aggregate_all(self.conn)
        after = {(r['trade_date'], r['level'], r['key']): r['net_score']
                 for date in db.list_index_dates(self.conn)
                 for r in db.fetch_daily_index(self.conn, date)}
        self.assertEqual(before, after, '重算结果不稳定，口径不可复现')


class TestCliDemo(unittest.TestCase):
    """`python3 run.py demo` 本身能跑通——这是本项目唯一的成功标准。"""

    def test_16_run_demo_返回0并生成看板(self):
        tmp = Path(tempfile.mkdtemp(prefix='sentiment-cli-'))
        try:
            sys.path.insert(0, str(MVP_DIR))
            import run                                   # noqa: PLC0415

            out_html = tmp / 'dashboard.html'
            # demo 会往终端刷一整屏看板，跑测试时收进 buffer，别把测试输出淹了
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = run.main(['--db', str(tmp / 'cli.db'), 'demo',
                                 '--out', str(out_html), '--no-color'])
            self.assertEqual(code, 0, 'run.py demo 没有正常退出')
            self.assertIn('大盘情绪', buffer.getvalue(), 'demo 没有渲染出终端看板')
            self.assertTrue(out_html.is_file(), 'demo 没有产出 HTML 看板')
            self.assertGreater(out_html.stat().st_size, 5000, '看板文件小得不正常')

            conn = sqlite3.connect(tmp / 'cli.db')
            conn.row_factory = sqlite3.Row
            try:
                for table in ('org', 'report', 'segment', 'score', 'daily_index'):
                    with self.subTest(table=table):
                        self.assertGreater(db.count_table(conn, table), 0,
                                           f'{table} 表是空的')
            finally:
                conn.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestUnreadableBlocksNotCountedAsNeutral(unittest.TestCase):
    """★「引擎没读懂」不能算成「大家看中性」。

    规则未命中时打分层写的是 direction=0.0 + **confidence=0.0**。
    这个 0 的含义是「不知道」，如果被当成一张中性票投进聚合，
    引擎越读不懂、指标越靠近中性，而且覆盖家数还会虚高——
    这是 NA ≠ 0 的同一条口径在「块」这一层的延伸。
    """

    def test_19_confidence为零的块不参与计票(self):
        rows = [
            {'org_name': 'A', 'product_code': 'CU', 'direction': 1.0,
             'confidence': 0.8, 'ingest_time': '2026-09-10T06:30:00'},
            {'org_name': 'B', 'product_code': 'CU', 'direction': 0.0,
             'confidence': 0.0, 'ingest_time': '2026-09-10T06:30:00'},
        ]
        kept = aggregate.drop_unreadable(rows)
        self.assertEqual(len(kept), 1, '规则没读懂的块必须被剔除')
        self.assertEqual(kept[0]['org_name'], 'A')
        # 剔除前会被算成 1 多 1 中（净得分 +50），剔除后才是真实的 1 多（+100）
        self.assertEqual(aggregate._tally(rows), (1, 0, 1))
        self.assertEqual(aggregate._tally(kept), (1, 0, 0))

    def test_20_低置信度但非零的块照常计票(self):
        """0.5 的弱信号是「读懂了但没把握」，要计票、要进复核队列，不能丢。"""
        rows = [{'org_name': 'A', 'product_code': 'CU', 'direction': 0.5,
                 'confidence': 0.5, 'ingest_time': '2026-09-10T06:30:00'}]
        self.assertEqual(len(aggregate.drop_unreadable(rows)), 1)
        self.assertLess(0.5, config.LOW_CONFIDENCE)


class TestPresentationSameSource(unittest.TestCase):
    """★★ 看板头部的数字与下钻的证据必须同源（同一版打分口径）。

    头部的大盘分读 daily_index，格子里的方向与证据读 score。
    ``fetch_scored_segments`` 默认取每个块**最新**的一行打分，
    所以打分口径升版后，如果呈现层不把 model_version 传下去，
    同一张看板上会出现「顶上写着偏多、点开每个格子都是看空」。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='sentiment-src-'))
        self.conn = db.connect(self.tmp / 't.db')
        db.init_schema(self.conn)
        from sentiment.models import Report as R, Score as Sc, Segment as Sg
        rid = db.insert_report(self.conn, R(
            org_name='甲期货', title='商品日报', publish_time='2026-09-10',
            ingest_time='2026-09-10T06:30:00', source_channel='inbox',
            file_path='/tmp/a.txt', file_md5='md5-src-1',
            parse_status='ok', parsed_text='正文'))
        for i, code in enumerate(('CU', 'RB')):
            sid = db.insert_segment(self.conn, Sg(
                report_id=rid, product_code=code, product_name=code, sector=None,
                raw_text='操作建议：逢低偏多。', advice_text='逢低偏多', seq=i))
            db.insert_score(self.conn, Sc(segment_id=sid, direction=1.0, confidence=0.8,
                                          evidence='逢低偏多', method='rule',
                                          model_version=config.SCORE_VERSION))
            # 再追加一版**方向相反**的打分（模拟口径升版）
            db.insert_score(self.conn, Sc(segment_id=sid, direction=-1.0, confidence=0.9,
                                          evidence='逢高沽空', method='rule',
                                          model_version='rule-vTEST'))
        aggregate.aggregate_all(self.conn)                      # 只按 rule-v1 聚合

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_21_头部数字与格子方向来自同一口径(self):
        day = report._load_day(self.conn, '2026-09-10')
        self.assertGreater(day['score'], 50, '按 rule-v1 聚合，头部应当偏多')
        dirs = {float(v['direction']) for v in day['votes'].values()}
        self.assertEqual(dirs, {1.0},
                         '★ 格子必须也来自 rule-v1，不能取到 rule-vTEST 那批')

    def test_22_显式指定另一版口径时整页一起切换(self):
        aggregate.aggregate_all(self.conn, model_version='rule-vTEST')
        day = report._load_day(self.conn, '2026-09-10', model_version='rule-vTEST')
        self.assertLess(day['score'], 50)
        self.assertEqual({float(v['direction']) for v in day['votes'].values()}, {-1.0})
        self.assertEqual(day['score_version'], 'rule-vTEST')


class TestPresentationNAAndEdgeCases(unittest.TestCase):
    """呈现层的 NA 与降级路径：NA ≠ 0 这条口径在页面上也要成立。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='sentiment-na-'))
        self.conn = db.connect(self.tmp / 't.db')
        db.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_23_没有打分支撑时baseline显示NA而不是50分(self):
        """构造一个只有品种级指标行、没有任何 score 的库（人工改库/聚合只跑一半）。"""
        from sentiment.models import DailyIndex
        db.upsert_daily_index(self.conn, DailyIndex(
            trade_date='2026-09-10', level='product', key='CU', net_score=100.0,
            bull_count=2, bear_count=0, neutral_count=0, coverage_count=2,
            model_version=config.SCORE_VERSION, calc_version=config.CALC_VERSION))
        day = report._load_day(self.conn, '2026-09-10')
        self.assertIsNone(day['baseline_score'], '★ 没有机构覆盖 → NA，绝不是 50 分')
        self.assertTrue(day['market_estimated'], '大盘行缺失要打标记')

        push = report.build_push_message(self.conn, '2026-09-10')
        self.assertIn('NA', push)
        self.assertNotIn('baseline（一家一票只数家数 0/0/0）50.0', push)

        out = self.tmp / 'na.html'
        html = Path(report.export_html(self.conn, '2026-09-10', out)).read_text(encoding='utf-8')
        self.assertIn('NA', html)
        self.assertIn('大盘级指标行缺失', html)

    def test_24_完全空库时HTML也不画一张全是0的看板(self):
        from sentiment.models import DailyIndex
        # 只有别的日期有数据，渲染一个没数据的日子
        db.upsert_daily_index(self.conn, DailyIndex(
            trade_date='2026-09-09', level='market', key='MARKET', net_score=10.0,
            bull_count=1, bear_count=0, neutral_count=0, coverage_count=1,
            model_version=config.SCORE_VERSION, calc_version=config.CALC_VERSION))
        out = self.tmp / 'empty.html'
        html = Path(report.export_html(self.conn, '2026-09-10', out)).read_text(encoding='utf-8')
        self.assertIn('当日无研报覆盖', html)
        self.assertNotIn('class="hero"', html, '空的一天不该渲染出一个 50.0 分的仪表')
        self.assertIn('<footer>', html)

    def test_25_环比取上一个有指标的交易日而不是画图窗口的倒数第二点(self):
        """★ 历史日期上也必须有环比。

        ``fetch_index_series`` 是「按日期倒序取最近 N 行」，
        如果拿它的倒数第二个点当上一期，历史一旦超过 N 天，
        指定一个较早的日期就会拿到一段与它无关的序列，环比直接变成「首日无环比」。
        """
        from datetime import date, timedelta
        from sentiment.models import DailyIndex
        start = date(2026, 1, 1)
        for i in range(70):
            db.upsert_daily_index(self.conn, DailyIndex(
                trade_date=(start + timedelta(days=i)).isoformat(),
                level='market', key='MARKET', net_score=10.0 + i,
                bull_count=3, bear_count=1, neutral_count=0, coverage_count=3,
                model_version=config.SCORE_VERSION, calc_version=config.CALC_VERSION))
        day = report._load_day(self.conn, '2026-01-05')
        self.assertEqual(day['prev_date'], '2026-01-04')
        self.assertIsNotNone(day['delta'])
        self.assertTrue(all(r['trade_date'] <= '2026-01-05' for r in day['series']),
                        '序列右端点必须钉在 trade_date 上，不能把「未来」画进图里')
        self.assertNotIn('首日无环比', report.build_push_message(self.conn, '2026-01-05'))


class TestIngestAndParseGuards(unittest.TestCase):
    """采集与解析的两道闸门：静默丢弃 / 静默成功都是最坏的失败方式。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='sentiment-guard-'))
        self.inbox = self.tmp / 'inbox'
        self.inbox.mkdir()
        self.conn = db.connect(self.tmp / 't.db')
        db.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_26_不支持的后缀要计数而不是静默丢弃(self):
        (self.inbox / '20260910_甲期货_日报.txt').write_text(
            '【有色】沪铜\n操作建议：逢低偏多。\n', encoding='utf-8')
        (self.inbox / '20260910_乙期货_日报.xlsx').write_bytes(b'not a report')
        stats = ingest.ingest_inbox(self.conn, inbox_dir=self.inbox, verbose=False)
        self.assertEqual(stats['new'], 1)
        self.assertEqual(stats['skipped_format'], 1,
                         '被过滤掉的文件必须计入统计，否则用户不知道自己丢进去的东西没被看一眼')

    def test_27_html与pdf在采集层放行让解析层的分支真的可达(self):
        (self.inbox / '20260910_甲期货_网页版日报.html').write_text(
            '<html><body><h1>【有色】沪铜</h1>'
            '<p>操作建议：逢低偏多，关注 78,000 一线支撑。</p></body></html>',
            encoding='utf-8')
        (self.inbox / '20260910_乙期货_日报.pdf').write_bytes(b'%PDF-1.4 fake')
        ingest.ingest_inbox(self.conn, inbox_dir=self.inbox, verbose=False)
        stats = parse.parse_pending(self.conn)
        self.assertEqual(stats['ok'], 1, 'HTML 分支必须真的走得到')
        self.assertEqual(stats['dependency_missing'], 1,
                         'PDF 缺 pymupdf 要明确失败，而不是假装没发生')
        segment.segment_pending(self.conn)
        codes = {r['product_code'] for r in
                 self.conn.execute('SELECT product_code FROM segment').fetchall()}
        self.assertIn('CU', codes)

    def test_28_二进制内容的txt判解析失败而不是解析成功(self):
        """★ 「成功解析出乱码」比「解析失败」危险：它会虚增链路小结里的每个计数。"""
        import os
        (self.inbox / '20260910_甲期货_乱码.txt').write_bytes(os.urandom(2000))
        ingest.ingest_inbox(self.conn, inbox_dir=self.inbox, verbose=False)
        stats = parse.parse_pending(self.conn)
        self.assertEqual(stats['ok'], 0)
        self.assertEqual(stats['failed'], 1)
        row = self.conn.execute('SELECT parse_status, parsed_text FROM report').fetchone()
        self.assertEqual(row['parse_status'], 'failed')
        self.assertIn('乱码', row['parsed_text'])
        # 失败的行进不了切分（db.list_unsegmented 只取 'ok'）
        self.assertEqual(segment.segment_pending(self.conn)['reports'], 0)

    def test_29_正常研报不会被可读性闸门误杀(self):
        for path in sorted(INBOX_DIR.glob('*.txt')):
            with self.subTest(name=path.name):
                text, status = parse.parse_report(str(path))
                self.assertEqual(status, 'ok')
                self.assertEqual(parse.garbled_ratio(text), 0.0)


class TestCliFriendlyErrors(unittest.TestCase):
    """CLI 的异常路径要说人话——其余路径都做了兜底，漏一条就显得整个工具不可信。"""

    def test_30_损坏的库文件给出可执行的修复提示(self):
        import os
        tmp = Path(tempfile.mkdtemp(prefix='sentiment-broken-'))
        try:
            sys.path.insert(0, str(MVP_DIR))
            import run                                          # noqa: PLC0415
            broken = tmp / 'broken.db'
            broken.write_bytes(os.urandom(4000))
            buffer = io.StringIO()
            with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stdout(buffer):
                run.main(['--db', str(broken), 'status'])
            message = str(ctx.exception)
            self.assertIn('损坏', message)
            self.assertIn('run.py reset', message, '必须给出一条能直接敲的修复命令')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestConfigContract(unittest.TestCase):
    """契约常量：改错了会静默污染全链路，单独钉住。"""

    def test_17_多空计数口径(self):
        self.assertEqual(config.BULL_CUTOFF, 0.25)
        self.assertEqual(config.BEAR_CUTOFF, -0.25)

    def test_18_双版本号分开管理(self):
        self.assertTrue(config.SCORE_VERSION)
        self.assertTrue(config.CALC_VERSION)
        self.assertNotEqual(config.SCORE_VERSION, config.CALC_VERSION,
                            '打分口径与聚合口径必须是两个独立的版本号')


if __name__ == '__main__':
    unittest.main(verbosity=2)
