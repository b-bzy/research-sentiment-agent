"""⑤ 打分层单元测试。

这个文件是整个 demo 的**回归护栏**：打分判反了，后面的聚合再准也是错的。
所以断言集中在两处：

1. **打分刻度契约**（-2.0 / -1.0 / -0.5 / 0 / +0.5 / +1.0 / +2.0 七档）；
2. **★ 难例**——「谨慎追空」「暂不看空」「不宜追高」这类字面带方向词、
   真实含义相反的固定搭配。这几条判反了这个 demo 就废了，因此每一条都单独断言，
   而且断言的是**符号**（> 0 / < 0），不是约等于——符号错了才是致命错误。

跑法::

    python3 -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

# 让 `python3 -m unittest discover -s tests` 能 import 到 sentiment 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sentiment import config, db                              # noqa: E402
from sentiment.models import Report, Segment                  # noqa: E402
from sentiment.score import (                                 # noqa: E402
    INTENSIFIERS,
    METHOD_RULE,
    METHOD_RULE_MISS,
    NEGATIONS,
    RULES,
    RulePattern,
    classify_advice,
    score_pending,
    score_segment,
    score_with_llm,
)


def d(text: str) -> float:
    """打分方向的快捷方式，让断言读起来像人话。"""
    return classify_advice(text)['direction']


class TestHardCases(unittest.TestCase):
    """★★ 难例：字面带方向词但含义相反，判反了这个 demo 就废了。"""

    def test_01_谨慎追空_是偏多不是看空(self):
        # 「别去追空」——全项目最容易判反的一条
        result = classify_advice('谨慎追空')
        self.assertGreater(result['direction'], 0,
                           '「谨慎追空」= 别去追空 → 必须是偏多，绝不能判成看空')
        self.assertEqual(result['direction'], 0.5)
        self.assertEqual(result['method'], METHOD_RULE)

    def test_02_暂不看空_是偏多(self):
        result = classify_advice('暂不看空')
        self.assertGreater(result['direction'], 0, '「暂不看空」= 暂时不看空 → 偏多')
        self.assertEqual(result['direction'], 0.5)

    def test_03_前多保护利润_偏多且置信度低(self):
        result = classify_advice('前多保护利润')
        self.assertEqual(result['direction'], 0.5, '持多单 → 方向仍偏多')
        self.assertLess(result['confidence'], config.LOW_CONFIDENCE,
                        '「保护利润」= 转谨慎，置信度必须压低到复核阈值以下')

    def test_04_不宜追高_是偏空(self):
        result = classify_advice('不宜追高')
        self.assertLess(result['direction'], 0, '「不宜追高」= 别追多 → 偏空')
        self.assertEqual(result['direction'], -0.5)

    def test_05_暂不看多_是偏空(self):
        self.assertEqual(d('暂不看多'), -0.5)
        self.assertLess(d('暂不看多'), 0)

    def test_06_逢低偏多(self):
        self.assertEqual(d('逢低偏多'), 1.0)

    def test_07_多头思路为主_强烈看多(self):
        self.assertEqual(d('多头思路为主'), 2.0)

    def test_08_空头思路为主_强烈看空(self):
        self.assertEqual(d('空头思路为主'), -2.0)

    def test_09_逢高沽空_看空(self):
        self.assertEqual(d('逢高沽空'), -1.0)

    def test_10_中性三兄弟(self):
        for text in ('区间操作', '观望为主', '高抛低吸'):
            with self.subTest(text=text):
                self.assertEqual(d(text), 0.0)

    def test_11_高抛低吸不能被高抛吃掉(self):
        # 复合中性词必须比「高抛」先命中，否则区间策略会被判成看空
        self.assertEqual(d('高抛低吸'), 0.0)
        self.assertEqual(d('高抛为主'), -1.0, '单独的「高抛」仍然是看空')

    def test_12_谨慎追空与谨慎做空必须分开(self):
        # 「谨慎 + 追X」= 别去追X（反转）；「谨慎 + 做X」= 小心地做X（同向减弱）
        self.assertGreater(d('谨慎追空'), 0)
        self.assertLess(d('谨慎做空'), 0, '「谨慎做空」是小心地做空，仍然看空')

    def test_13_不宜过度看空_是偏多(self):
        self.assertGreater(d('不宜过度看空'), 0)
        self.assertGreater(d('无需过度看空'), 0)


class TestScale(unittest.TestCase):
    """打分刻度契约：七档取值必须落在约定的档位上。"""

    def test_14_强烈看多档(self):
        for text in ('坚定做多', '建议做多', '以多头思路对待', '强烈看多'):
            with self.subTest(text=text):
                self.assertEqual(d(text), 2.0)

    def test_15_强烈看空档(self):
        for text in ('坚定做空', '建议做空', '以空头思路对待', '强烈看空'):
            with self.subTest(text=text):
                self.assertEqual(d(text), -2.0)

    def test_16_看多档(self):
        for text in ('逢低做多', '偏多思路', '逢低买入', '多单入场'):
            with self.subTest(text=text):
                self.assertEqual(d(text), 1.0)

    def test_17_看空档(self):
        for text in ('逢高做空', '偏空思路', '逢高抛空', '空单入场'):
            with self.subTest(text=text):
                self.assertEqual(d(text), -1.0)

    def test_18_谨慎偏多档(self):
        for text in ('前多持有', '逢低轻仓试多', '暂不看空', '谨慎追空'):
            with self.subTest(text=text):
                self.assertEqual(d(text), 0.5)

    def test_19_谨慎偏空档(self):
        for text in ('前空持有', '逢高轻仓试空', '暂不看多', '不宜追高'):
            with self.subTest(text=text):
                self.assertEqual(d(text), -0.5)

    def test_20_中性档(self):
        for text in ('区间操作', '短差操作', '观望为主', '高抛低吸', '暂时观望',
                     '多空交织', '宽幅震荡', '等待方向指引'):
            with self.subTest(text=text):
                self.assertEqual(d(text), 0.0)

    def test_21_方向绝对值不越界(self):
        # 「坚定」叠在 +2.0 上会算出 2.6，必须被夹回 2.0
        self.assertEqual(d('坚定做多'), 2.0)
        self.assertEqual(d('强烈看空'), -2.0)
        for rule in RULES:
            with self.subTest(rule=rule.desc):
                self.assertLessEqual(abs(rule.direction), 2.0)


class TestNegation(unittest.TestCase):
    """否定词处理：特例优先，特例没命中再走通用反转。"""

    def test_22_通用否定反转方向(self):
        self.assertGreater(d('不看空'), 0, '不 + 看空 → 反转成偏多')
        self.assertLess(d('不看多'), 0, '不 + 看多 → 反转成偏空')

    def test_23_不建议做多_被反转(self):
        result = classify_advice('不建议做多')
        self.assertLess(result['direction'], 0)
        self.assertTrue(result['negated'])
        self.assertEqual(result['direction'], -0.5,
                         '否定只表示「别往那个方向」，强度统一压到谨慎档，不是 -2.0')
        self.assertLess(result['confidence'], config.LOW_CONFIDENCE,
                        '推断出来的否定不如固定搭配可信，应自动进复核队列')

    def test_24_双重否定不反转(self):
        result = classify_advice('不得不看空')
        self.assertLess(result['direction'], 0, '偶数个否定词 = 没否定')
        self.assertFalse(result['negated'])

    def test_25_跨子句的否定不算数(self):
        # 「不」修饰的是「确定性」，不能跨过逗号去反转「逢高沽空」
        result = classify_advice('不确定性上升，逢高沽空')
        self.assertEqual(result['direction'], -1.0)
        self.assertFalse(result['negated'])

    def test_26_特例优先于通用否定(self):
        # 若先跑通用逻辑，「暂不看空」会被当成「看空的否定」而落到中性上
        result = classify_advice('暂不看空')
        self.assertFalse(result['negated'], '「暂不看空」应由特例表直接命中')
        self.assertEqual(result['direction'], 0.5)

    def test_27_否定词表全覆盖(self):
        # 表里每个否定词接在方向词前，都应该把方向掰过来
        for word in NEGATIONS:
            with self.subTest(word=word):
                self.assertGreater(d(f'{word}看空'), 0, f'「{word}看空」应判成偏多')


class TestCompoundClause(unittest.TestCase):
    """复合建议句：多个子句时，**主张写在最前面的那个子句说了算**。

    集成时发现的真实 bug：规则表把特例组排在最前面（这对单子句是对的），
    但整句一起扫时，第二个子句里的特例词会劫走第一个子句的强信号——
    「空头思路为主，反弹勿追多」被判成 −0.5，强度直接掉了两档。
    """

    def test_34a_强信号在前_不被后半句的特例劫走(self):
        result = classify_advice('空头思路为主，反弹勿追多。')
        self.assertEqual(result['direction'], -2.0,
                         '主张是「空头思路为主」，「勿追多」只是补充，不能反过来盖掉它')
        result = classify_advice('多头思路为主，回调无需过度看空。')
        self.assertEqual(result['direction'], 2.0)

    def test_34b_难例在前依然要判对(self):
        # 子句顺序反过来时，第一个子句仍然说了算
        self.assertEqual(d('暂不看多，关注 3,479 一线压力。'), -0.5)
        self.assertEqual(d('谨慎追空，等待企稳信号。'), 0.5)

    def test_34c_中性子句不该压过明确方向(self):
        # 退化路径（advice 抽空时用整块原文打分）里，第一段一定是「行情回顾」，
        # 「震荡」会命中中性规则，不能让它把块尾真正的操作建议挡在外面
        result = classify_advice('行情回顾：昨日震荡走高。操作建议：逢低偏多。')
        self.assertEqual(result['direction'], 1.0)
        self.assertIn('逢低偏多', result['evidence'])

    def test_34d_复合中性词不会被拆开吃掉(self):
        # 「有方向优先于中性」这条规则只在**子句之间**生效，
        # 不允许在子句内部绕开复合中性组去命中「高抛」
        self.assertEqual(d('高抛低吸'), 0.0)
        self.assertEqual(d('高抛低吸，运行区间参考 755-778。'), 0.0)
        self.assertEqual(d('区间操作，运行区间参考 14,215-14,648。'), 0.0)


class TestIntensifier(unittest.TestCase):
    """程度副词：调强度与置信度，**绝不改符号**。"""

    def test_28_减弱词不改符号(self):
        base = d('逢低偏多')
        weak = d('谨慎逢低偏多')
        self.assertGreater(weak, 0, '「谨慎」只是打折，不能把偏多打成偏空/中性')
        self.assertLess(weak, base)
        self.assertGreater(weak, config.BULL_CUTOFF, '打折后仍要落在看多区间内')

    def test_29_增强词提高强度与置信度(self):
        plain = classify_advice('偏多思路')
        strong = classify_advice('大幅偏多')
        self.assertGreater(strong['direction'], plain['direction'])
        self.assertGreater(strong['confidence'], plain['confidence'])

    def test_30_略偏空仍是偏空(self):
        self.assertLess(d('略偏空'), 0)
        self.assertGreater(d('略偏空'), -1.0)

    def test_31_策略里的略不是程度副词(self):
        # 「操作策略」里的「略」若被当成 0.5 倍强度，全市场分数会系统性偏小
        self.assertEqual(d('操作策略上逢低偏多'), 1.0)

    def test_32_减弱不会跌破多空分界线(self):
        # 0.5 档再叠减弱词也不能滑到 ±0.25 以内变成中性
        result = classify_advice('谨慎小幅偏多')
        self.assertGreater(result['direction'], config.BULL_CUTOFF)

    def test_33_程度副词表取值合法(self):
        for word, factor in INTENSIFIERS.items():
            with self.subTest(word=word):
                self.assertGreater(factor, 0)
                self.assertLess(factor, 2)


class TestEvidenceAndMiss(unittest.TestCase):
    """证据原句与「规则没吃下来」的标记位。"""

    def test_34_证据是原文完整句子(self):
        text = '沪铜：库存持续去化。操作建议：谨慎追空，注意止损。'
        result = classify_advice(text)
        self.assertIn('谨慎追空', result['evidence'])
        self.assertIn(result['evidence'][:4], text, '证据必须来自原文，不能是拼出来的')
        self.assertNotIn('库存持续去化', result['evidence'], '证据只取命中所在的那一句')

    def test_35_命中片段与规则说明都要返回(self):
        result = classify_advice('操作上逢高沽空为宜')
        self.assertEqual(result['matched'], '逢高沽空')
        self.assertTrue(result['rule'], '规则说明不能为空，看板下钻要用')
        # 「建议做空」比裸「逢高沽空」更强硬，应当升一档到 -2.0
        self.assertEqual(d('操作上建议逢高沽空'), -2.0)

    def test_36_未命中时置信度为零(self):
        result = classify_advice('昨日沪铜收涨0.8%，库存小幅去化。')
        self.assertEqual(result['direction'], 0.0)
        self.assertEqual(result['confidence'], 0.0)
        self.assertEqual(result['method'], METHOD_RULE_MISS,
                         'confidence=0 表示规则没吃下来，生产版转 LLM 层')
        self.assertTrue(result['evidence'], '未命中也要留原句，人工复核时要看')

    def test_37_中性命中与未命中必须能区分(self):
        neutral = classify_advice('观望为主')
        miss = classify_advice('本文由某某研究所提供')
        self.assertEqual(neutral['direction'], miss['direction'])
        self.assertGreater(neutral['confidence'], 0.0, '「确实看平」置信度大于 0')
        self.assertEqual(miss['confidence'], 0.0, '「不知道」置信度等于 0')

    def test_38_空文本安全返回(self):
        for text in ('', '   ', '\n'):
            with self.subTest(text=repr(text)):
                result = classify_advice(text)
                self.assertEqual(result['direction'], 0.0)
                self.assertEqual(result['method'], METHOD_RULE_MISS)

    def test_39_词内空格不影响匹配(self):
        # PDF 解析出来的中文经常带词内空格
        self.assertEqual(d('逢 低 偏 多'), 1.0)


class TestScoreSegment(unittest.TestCase):
    """score_segment：advice_text 优先，空则退化用 raw_text 尾部。"""

    @staticmethod
    def make(advice: str, raw: str = '', seg_id: int = 1) -> Segment:
        return Segment(report_id=1, product_code='CU', product_name='沪铜',
                       sector='有色', raw_text=raw, advice_text=advice,
                       seq=0, id=seg_id)

    def test_40_优先用操作建议句(self):
        seg = self.make('谨慎追空', raw='沪铜昨日大幅下跌，空头氛围浓厚。')
        score = score_segment(seg)
        self.assertGreater(score.direction, 0, 'advice_text 才是判断依据，别被行情回顾带偏')
        self.assertEqual(score.segment_id, 1)
        self.assertEqual(score.method, METHOD_RULE)
        self.assertEqual(score.model_version, config.SCORE_VERSION)

    def test_41_建议为空时退化用原文尾部(self):
        seg = self.make('', raw='行情回顾：昨日震荡走高。逻辑分析：库存去化。操作建议：逢低偏多。')
        score = score_segment(seg)
        self.assertEqual(score.direction, 1.0)
        self.assertIn('逢低偏多', score.evidence)

    def test_42_整块都没信号则置信度为零(self):
        seg = self.make('', raw='本报告由研究所提供，仅供参考，不构成投资建议依据说明。')
        score = score_segment(seg)
        self.assertEqual(score.confidence, 0.0)
        self.assertEqual(score.method, METHOD_RULE, '落库的 method 只能是 rule/llm/human')

    def test_43_证据字段不为空(self):
        seg = self.make('操作建议：前多保护利润。')
        score = score_segment(seg)
        self.assertIn('前多保护利润', score.evidence)


class TestScorePending(unittest.TestCase):
    """score_pending：批量入库 + 统计口径。"""

    def setUp(self):
        self.conn = db.connect(':memory:')
        db.init_schema(self.conn)
        report = Report(org_name='某某期货', title='商品日报', publish_time='2026-09-09',
                        ingest_time='2026-09-10T07:30:00', source_channel='inbox',
                        file_path='/tmp/demo.txt', file_md5='md5-demo-001',
                        parse_status='ok', parsed_text='正文')
        self.report_id = db.insert_report(self.conn, report)
        self.advices = ['谨慎追空', '逢高沽空', '观望为主', '昨日库存小幅去化']
        for i, advice in enumerate(self.advices):
            db.insert_segment(self.conn, Segment(
                report_id=self.report_id, product_code='CU', product_name='沪铜',
                sector='有色', raw_text=f'沪铜。操作建议：{advice}。',
                advice_text=advice, seq=i))

    def tearDown(self):
        self.conn.close()

    def test_44_全部入库并统计命中率(self):
        stats = score_pending(self.conn)
        self.assertEqual(stats['total'], 4)
        self.assertEqual(stats['scored'], 4)
        self.assertEqual(stats['rule_hit'], 3)
        self.assertEqual(stats['rule_miss'], 1)
        self.assertAlmostEqual(stats['rule_hit_rate'], 0.75)
        self.assertEqual(stats['model_version'], config.SCORE_VERSION)
        self.assertEqual(db.count_table(self.conn, 'score'), 4)

    def test_45_方向分布按正负0点25口径(self):
        stats = score_pending(self.conn)
        self.assertEqual(stats['bull'], 1)      # 谨慎追空 +0.5
        self.assertEqual(stats['bear'], 1)      # 逢高沽空 -1.0
        self.assertEqual(stats['neutral'], 2)   # 观望为主 + 未命中
        self.assertGreaterEqual(stats['low_confidence'], 1, '未命中的那条必须进复核队列')

    def test_46_重复跑不会重复打分(self):
        score_pending(self.conn)
        again = score_pending(self.conn)
        self.assertEqual(again['total'], 0, '同一口径下已打过分的块不再重复打')
        self.assertEqual(db.count_table(self.conn, 'score'), 4)

    def test_47_升版本号可重算历史且旧行保留(self):
        score_pending(self.conn)
        stats = score_pending(self.conn, model_version='rule-v2')
        self.assertEqual(stats['total'], 4, '换口径版本要能重跑全量')
        self.assertEqual(db.count_table(self.conn, 'score'), 8, '★ 打分表只追加，旧行必须还在')


class TestRuleTable(unittest.TestCase):
    """规则表本身的健康检查。"""

    def test_48_规则表规模与取值合法(self):
        self.assertGreaterEqual(len(RULES), 40)
        for rule in RULES:
            with self.subTest(rule=rule.desc):
                self.assertIsInstance(rule, RulePattern)
                self.assertGreaterEqual(rule.direction, -2.0)
                self.assertLessEqual(rule.direction, 2.0)
                self.assertGreater(rule.confidence, 0.0)
                self.assertLessEqual(rule.confidence, 1.0)
                self.assertTrue(rule.desc, '每条规则都要有中文说明，否则无法下钻解释')

    def test_49_没有重复的正则(self):
        patterns = [r.pattern for r in RULES]
        self.assertEqual(len(patterns), len(set(patterns)), '重复的规则永远轮不到第二条')

    def test_50_特例组排在最前面(self):
        # 顺序即优先级：特例必须先于通用规则被匹配到
        head = [r.desc for r in RULES[:4]]
        self.assertTrue(any('暂不看空' in desc for desc in head))

    def test_51_每条规则都能被自己的样例命中(self):
        samples = {
            '谨慎追空': 0.5, '暂不看空': 0.5, '不宜追高': -0.5, '暂不看多': -0.5,
            '前多保护利润': 0.5, '前空止盈': -0.5, '前多持有': 0.5, '前空持有': -0.5,
            '逢低轻仓试多': 0.5, '逢高轻仓试空': -0.5,
            '多头思路为主': 2.0, '空头思路为主': -2.0,
            '逢低偏多': 1.0, '逢高沽空': -1.0,
            '区间操作': 0.0, '观望为主': 0.0, '高抛低吸': 0.0,
        }
        for text, expected in samples.items():
            with self.subTest(text=text):
                self.assertEqual(d(text), expected)


class TestDegradedPathSignFlip(unittest.TestCase):
    """★★ 退化路径的整段判反——本文件最重要的一组回归护栏。

    advice_text 为空时只能拿整段 raw_text 打分，而整段的结构是
    「行情回顾 → 逻辑分析 → 操作建议」。逻辑分析段里几乎必然出现
    「承压运行 / 震荡偏弱 / 重心下移」这类 **WEAK_RULES 里的行情倾向词**，
    它们**自带方向**，会抢在块尾真正的操作建议前面命中，把 +1.0 判成 −0.5。

    这个洞很隐蔽：指标不会报错，只会悄悄变错；而且用「昨日震荡走高」
    （命中的是**中性**规则）做测试恰好绕得开它。所以这里专门用带方向的
    行情倾向词做前缀，把三道闸门（截「操作建议」标签 / 关弱信号组 /
    取最后一个有方向的子句）逐条钉死。
    """

    @staticmethod
    def seg(raw: str) -> Segment:
        return Segment(report_id=1, product_code='JM', product_name='焦煤',
                       sector='黑色', raw_text=raw, advice_text='', seq=0, id=1)

    def test_54_逻辑分析里的承压运行不能盖掉块尾的逢低偏多(self):
        raw = ('行情回顾：昨日焦煤主力合约收于 1,042 元/吨，跌 3.2%。'
               '逻辑分析：港口与钢厂库存连续累积，旺季需求迟迟未能兑现，板块承压运行。'
               '操作建议：逢低偏多，关注 1,034 一线支撑。')
        score = score_segment(self.seg(raw))
        self.assertEqual(score.direction, 1.0,
                         '「承压运行」是行情倾向词，不该压过块尾真正的操作建议')
        self.assertIn('逢低偏多', score.evidence)

    def test_55_偏强运行同样不能盖掉块尾的逢高沽空(self):
        # 反方向也要测：否则「恰好朝着我们想要的方向错」会被当成正确
        raw = ('行情回顾：昨日甲醇主力合约收于 2,418 元/吨，涨 1.1%。'
               '逻辑分析：短期成本支撑仍在，盘面震荡偏强，重心逐步上移。'
               '操作建议：逢高沽空，关注 2,470 一线压力。')
        score = score_segment(self.seg(raw))
        self.assertEqual(score.direction, -1.0)
        self.assertIn('逢高沽空', score.evidence)

    def test_56_没有操作建议标签时取最后一个有方向的子句(self):
        raw = ('昨日铁矿石承压运行，港口库存累积。'
               '不过午后钢厂补库启动，盘面快速拉升，整体仍以逢低偏多对待。')
        score = score_segment(self.seg(raw))
        self.assertGreater(score.direction, 0,
                           '期货日报的主张一律收在块尾，前面是行情回顾与逻辑分析')

    def test_57_整段只有行情倾向词时仍保留弱信号不丢(self):
        """关掉弱信号组是为了不让它抢方向，不是为了把它扔掉。"""
        raw = ('沪镍方面，昨日盘面小幅回落。'
               '库存持续累积，下游采购意愿低迷，价格重心下移。')
        score = score_segment(self.seg(raw))
        self.assertEqual(score.direction, -0.5)
        self.assertEqual(score.confidence, 0.5,
                         '行情倾向词的置信度低于阈值，天然落在人工复核队列里')

    def test_58_单句建议仍然取第一个子句(self):
        """两种输入的文体结构正好相反，开关不能串味。"""
        self.assertEqual(d('空头思路为主，反弹勿追多。'), -2.0)
        self.assertEqual(classify_advice('空头思路为主，反弹勿追多。',
                                         prefer_last=True)['direction'], -0.5,
                         'prefer_last 只该给整段原文用，给单句用会把主张让给补充说明')

    def test_59_allow_weak开关本身生效(self):
        self.assertEqual(d('板块承压运行'), -0.5)
        self.assertEqual(classify_advice('板块承压运行', allow_weak=False)['method'],
                         METHOD_RULE_MISS)


class TestNegationPatternCollision(unittest.TestCase):
    """★ 否定词与规则 pattern 抢字符：唯一的实例是「难以」。

    STRONG_RULES 的可选前缀 (?:以)? 会把「难以」的第二个字吃进匹配区间，
    通用否定层回看左窗时只看到残缺的「难」（不在 NEGATIONS 表里），符号反转。
    """

    def test_60_难以加多空头思路要反转(self):
        self.assertLess(d('难以多头思路为主'), 0)
        self.assertGreater(d('难以空头思路为主'), 0)

    def test_61_自然措辞不受影响(self):
        self.assertLess(d('短期难以形成多头格局'), 0)
        self.assertLess(d('难以延续多头思路'), 0)
        self.assertLess(d('不以多头思路为主'), 0)
        self.assertGreater(d('暂不以空头思路对待'), 0)

    def test_62_正常的多空头思路不受牵连(self):
        self.assertEqual(d('多头思路为主'), 2.0)
        self.assertEqual(d('以空头思路对待'), -2.0)


class TestBareNegationBeforeChase(unittest.TestCase):
    """★ 裸「不」+ 追X：研报里很常见的写法，不收就掉进 rule_miss。"""

    def test_63_不追高不追空要命中且方向正确(self):
        self.assertEqual(d('不追高'), -0.5)
        self.assertEqual(d('不追空'), 0.5)
        self.assertEqual(d('不追涨'), -0.5)
        self.assertEqual(d('不追高，等回调后再看'), -0.5)

    def test_64_同族的其他写法一并守住(self):
        for text, expected in (('不宜追高', -0.5), ('勿追多', -0.5),
                               ('谨慎追空', 0.5), ('切勿追空', 0.5)):
            with self.subTest(text=text):
                self.assertEqual(d(text), expected)

    def test_65_双重否定不能被裸不劫走(self):
        """裸「不」只加在「追X」一族上，看/做/沽 一族仍走通用否定层。

        通用否定层会**数**否定词个数，「不能不看空」这种双重否定在那里
        才会被正确地判成看空；收进特例组就变成一次性反转，判成偏多。
        """
        self.assertEqual(d('不能不看空'), -1.0)
        self.assertEqual(d('不宜做空'), 0.5)


class TestLLMPlaceholder(unittest.TestCase):
    """第二层 LLM 是占位接口，demo 不实现但必须写清生产版怎么做。"""

    def test_52_llm接口抛未实现(self):
        with self.assertRaises(NotImplementedError):
            score_with_llm('逢低偏多')

    def test_53_llm接口留了生产版说明(self):
        doc = score_with_llm.__doc__ or ''
        for keyword in ('JSON', '证据', 'DeepSeek', '联网'):
            with self.subTest(keyword=keyword):
                self.assertIn(keyword, doc)


if __name__ == '__main__':
    unittest.main(verbosity=2)
