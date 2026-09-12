"""④ 切分层的单测。

架构方案第四节称切分是「全链路最关键的一步」，第十一节把「切分错误」
列为三大风险之一。原因是它的失败方式**不会报错**：把沪铜的观点切到沪铝名下，
指标照常算得出来，只是每个数都是错的，而且从结果上看不出来。

所以这里逐个分支钉死（一个都不能少）：

    一块 1 个品种  → 正常 Segment
    一块多个品种  → 能按段落拆就拆开，拆不开则共享原文 + 明确标注
    一块 0 个品种  → 宏观/综述块（product_code=None），不能丢
    免责声明/报头  → 丢弃，不能拿一段法律文本去投大盘情绪的票

以及三条容易被改坏的规则：标题层级三级级联、正文锚定的「≥2 句或首句」门槛、
同篇同品种合并。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sentiment import segment as seg                            # noqa: E402


def split(text: str):
    return seg.split_segments(1, text)


def by_code(segments) -> dict:
    return {s.product_code: s for s in segments}


class TestFourBranches(unittest.TestCase):
    """四个分支，一个都不能少。"""

    def test_单品种块(self):
        text = (
            '【有色】沪铜\n'
            '行情回顾：昨日沪铜主力合约收于 79,259 元/吨，涨 0.8%。\n'
            '逻辑分析：国内社会库存连续去化，现货升水走扩，供应弹性有限。\n'
            '操作建议：逢低偏多，关注 78,100 一线支撑。\n'
        )
        segments = split(text)
        self.assertEqual(len(segments), 1)
        one = segments[0]
        self.assertEqual(one.product_code, 'CU')
        self.assertEqual(one.product_name, '沪铜')
        self.assertEqual(one.sector, '有色')
        self.assertIn('逢低偏多', one.advice_text)
        self.assertIn('行情回顾', one.raw_text)

    def test_多品种可拆时各自带各自的操作建议(self):
        # ★ 必须有 ≥2 个 【】 标题，标题层级才会被采用（只有 1 个会退到段落兜底）
        text = (
            '【有色】沪铜、沪铝\n'
            '沪铜：库存持续去化，操作建议：逢低偏多。\n'
            '沪铝：成本塌陷，操作建议：逢高沽空。\n'
            '\n'
            '【黑色】螺纹钢\n'
            '操作建议：观望为主。\n'
        )
        got = by_code(split(text))
        self.assertEqual(set(got), {'CU', 'AL', 'RB'})
        self.assertIn('逢低偏多', got['CU'].advice_text)
        self.assertIn('逢高沽空', got['AL'].advice_text)
        # ★ 拆开了就不该带多品种标注，否则下钻时会误导人
        for s in got.values():
            self.assertFalse(s.raw_text.startswith('[多品种合并块'))
        # 拆开后每份原文里只有自己那一段
        self.assertNotIn('成本塌陷', got['CU'].raw_text)
        self.assertNotIn('库存持续去化', got['AL'].raw_text)

    def test_多品种拆不开时共享原文并明确标注(self):
        text = (
            '【贵金属】沪金沪银\n'
            '行情回顾：昨日沪金沪银同步走强，金银比价小幅回落。\n'
            '逻辑分析：沪金沪银的驱动完全同源，美元指数走弱与实际利率下行。\n'
            '操作建议：暂不看空。\n'
            '\n'
            '【黑色】螺纹钢\n'
            '操作建议：观望为主。\n'
        )
        got = by_code(split(text))
        self.assertEqual(set(got), {'AU', 'AG', 'RB'})
        for code in ('AU', 'AG'):
            s = got[code]
            with self.subTest(code=code):
                self.assertTrue(s.raw_text.startswith('[多品种合并块'),
                                '共享原文必须打标注，否则交易员会以为系统抓错了原文')
                self.assertIn('沪金', s.raw_text)
                self.assertIn('沪银', s.raw_text)
                self.assertIn('暂不看空', s.advice_text)
        self.assertFalse(got['RB'].raw_text.startswith('[多品种合并块'),
                         '单品种块不该被打上多品种标注')

    def test_一段里同时讲两个品种就不拆(self):
        """★ 判定严到几乎不会误拆：宁可共享，也不能拆错。"""
        text = (
            '【有色】沪铜、沪铝\n'
            '第一段：沪铜与沪铝同步走强，两者驱动一致。\n'
            '第二段：库存双双去化。\n'
            '操作建议：逢低偏多。\n'
            '\n'
            '【黑色】螺纹钢\n'
            '操作建议：观望为主。\n'
        )
        got = by_code(split(text))
        self.assertEqual(set(got), {'CU', 'AL', 'RB'})
        self.assertTrue(got['CU'].raw_text.startswith('[多品种合并块'))
        self.assertTrue(got['AL'].raw_text.startswith('[多品种合并块'))

    def test_宏观块不能丢(self):
        text = (
            '【宏观】市场综述\n'
            '行情回顾：昨日国内商品市场普遍走强，文华商品指数上涨 0.87%。\n'
            '逻辑分析：供给端的政策扰动持续发酵，海外流动性预期偏宽松。\n'
            '操作建议：谨慎逢低偏多。\n'
            '\n'
            '【有色】沪铜\n'
            '行情回顾：昨日沪铜主力合约收于 79,259 元/吨。\n'
            '操作建议：逢低偏多。\n'
        )
        got = by_code(split(text))
        self.assertIn(None, got, '宏观/综述块是对大盘的直接判断，价值比单品种块还高')
        macro = got[None]
        self.assertIsNone(macro.product_name)
        self.assertIsNone(macro.sector)
        self.assertIn('谨慎逢低偏多', macro.advice_text)

    def test_标题写着宏观时正文里的品种抢不走它(self):
        text = (
            '【宏观】市场综述\n'
            '行情回顾：今日原油领涨，带动整个能化板块走强，原油的强势尤为突出。\n'
            '逻辑分析：地缘扰动叠加供给收缩，市场风险偏好整体修复。\n'
            '操作建议：偏多思路为主。\n'
            '\n'
            '【黑色】螺纹钢\n'
            '操作建议：逢低偏多，关注 3,310 一线支撑。\n'
        )
        got = by_code(split(text))
        self.assertIn(None, got, '标题是宏观栏目就必须归宏观，否则原油凭空多一票、大盘还少一个判断')
        self.assertNotIn('SC', got)


class TestDisclaimerAndHeader(unittest.TestCase):
    """免责声明与报头：不能拿法律文本去投大盘情绪的票。"""

    def test_文末免责声明整段砍掉不粘到最后一个品种上(self):
        text = (
            '【能化】原油\n'
            '行情回顾：昨日原油主力合约收于 511.3 元/桶。\n'
            '操作建议：逢高沽空，关注 505 一线支撑。\n'
            '\n'
            '免责声明\n'
            '本报告不构成投资建议，据此操作，风险自担。版权所有。\n'
        )
        segments = split(text)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].product_code, 'SC')
        self.assertIn('逢高沽空', segments[0].advice_text)
        self.assertNotIn('风险自担', segments[0].raw_text,
                         '免责声明粘在块尾会让「据此操作，风险自担」变成原油的操作建议')

    def test_报头元信息不会变成宏观块(self):
        text = (
            '永安期货 商品早评日报\n'
            '发布日期：2026-09-09\n'
            '分析师：张三\n'
            '\n'
            '【有色】沪铜\n'
            '操作建议：逢低偏多。\n'
            '\n'
            '【黑色】螺纹钢\n'
            '操作建议：逢高沽空。\n'
        )
        got = by_code(split(text))
        self.assertNotIn(None, got, '一行页眉不该当成对大盘的判断')
        self.assertEqual(set(got), {'CU', 'RB'})

    def test_报头后面的真摘要要留下(self):
        # 摘要必须够长（≥ MIN_PREAMBLE_LEN）才算「真摘要」：
        # 门槛比宏观块更严，因为报头绝大多数是「XX期货 商品日报 2026-09-09」这类元信息
        text = (
            '永安期货 商品早评日报\n'
            '发布日期：2026-09-09\n'
            '今日商品整体偏强，工业品领涨，增量资金进场特征明显，'
            '市场对中上游环节的重新定价仍在进行中，节奏比方向更难把握，'
            '建议以偏多思路对待，同时控制单品种敞口与整体仓位。\n'
            '\n'
            '【有色】沪铜\n'
            '操作建议：逢低偏多。\n'
            '\n'
            '【黑色】螺纹钢\n'
            '操作建议：逢高沽空。\n'
        )
        self.assertGreaterEqual(len('今日商品整体偏强，工业品领涨，增量资金进场特征明显，'
                                    '市场对中上游环节的重新定价仍在进行中，节奏比方向更难把握，'
                                    '建议以偏多思路对待，同时控制单品种敞口与整体仓位。'),
                                seg.MIN_PREAMBLE_LEN)
        got = by_code(split(text))
        self.assertIn(None, got, '报头后面那段真摘要是对大盘的直接判断，丢了可惜')
        self.assertIn('偏多思路', got[None].advice_text)


class TestHeadingCascade(unittest.TestCase):
    """★ 标题层级三级级联：bracket → md → num → 段落兜底。

    级联而不是混着找，是为了避开假标题：正文里的「1、供应端 2、需求端」
    如果和【有色】混着找，一个品种块会被劈成好几截。
    """

    def _kind(self, text: str) -> str:
        lines = text.split('\n')
        for kind in ('bracket', 'md', 'num'):
            if len(seg._find_heads(lines, kind)) >= 2:
                return kind
        return 'paragraph'

    def test_bracket优先(self):
        text = '【有色】沪铜\n1、供应端偏紧。\n2、需求端回升。\n\n【黑色】螺纹钢\n操作建议：逢低偏多。\n'
        self.assertEqual(self._kind(text), 'bracket')
        got = by_code(split(text))
        self.assertEqual(set(got), {'CU', 'RB'}, '正文里的编号不该把品种块劈开')

    def test_markdown标题分支(self):
        text = (
            '## 沪铜\n操作建议：逢低偏多。\n\n'
            '## 螺纹钢\n操作建议：逢高沽空。\n'
        )
        self.assertEqual(self._kind(text), 'md')
        self.assertEqual(set(by_code(split(text))), {'CU', 'RB'})

    def test_编号标题分支(self):
        text = (
            '一、沪铜\n操作建议：逢低偏多。\n\n'
            '二、螺纹钢\n操作建议：逢高沽空。\n'
        )
        self.assertEqual(self._kind(text), 'num')
        self.assertEqual(set(by_code(split(text))), {'CU', 'RB'})

    def test_无标题时按段落加品种锚定兜底(self):
        text = (
            '沪铜方面，昨日主力合约收于 79,259 元/吨，库存持续去化。操作建议：逢低偏多。\n'
            '螺纹钢方面，建材成交量连续放量，终端补库启动。操作建议：逢高沽空。\n'
        )
        self.assertEqual(self._kind(text), 'paragraph')
        got = by_code(split(text))
        self.assertEqual(set(got), {'CU', 'RB'})
        self.assertIn('逢低偏多', got['CU'].advice_text)
        self.assertIn('逢高沽空', got['RB'].advice_text)


class TestBodyAnchorThreshold(unittest.TestCase):
    """★ 正文锚定门槛：出现在首句，或在 ≥2 个句子里出现过。"""

    def test_顺带提及不算命中(self):
        # 「原油」只在中段出现一次 → 这是一段市场综述，不是原油的观点
        text = (
            '今日商品市场整体走强，工业品普遍收涨，成交显著放大。'
            '能化板块受原油拖累表现偏弱。'
            '资金面上增量资金进场特征明显，市场风险偏好继续修复。'
            '操作建议：偏多思路为主。'
        )
        self.assertEqual(seg._body_products(text), [])

    def test_首句命中算数(self):
        text = '沪铜方面，昨日主力合约收于 79,259 元/吨。库存持续去化。操作建议：逢低偏多。'
        self.assertEqual([p['code'] for p in seg._body_products(text)], ['CU'])

    def test_多句命中算数(self):
        text = '昨日工业品走强。沪铜库存去化明显。沪铜现货升水走扩。操作建议：逢低偏多。'
        self.assertEqual([p['code'] for p in seg._body_products(text)], ['CU'])

    def test_点名一堆品种时按宏观块处理(self):
        block = seg._Block('', '今日铜铝锌铅镍锡普涨，铜领涨，铝跟涨，锌走强，铅企稳，镍反弹，锡上行。')
        hits, source = seg._resolve_products(block)
        self.assertEqual(hits, [], '综述在点名，不是六个品种各自的观点')
        self.assertEqual(source, 'none')


class TestSameReportMerge(unittest.TestCase):
    """★ 一篇之内同品种只出一块（配合聚合层的「一家一票」）。"""

    def test_同篇同品种合并成一块且原文不丢(self):
        text = (
            '【有色】沪铜\n'
            '行情回顾：昨日沪铜主力合约收于 79,259 元/吨。\n'
            '操作建议：逢低偏多。\n'
            '\n'
            '【品种小结】沪铜\n'
            '补充：沪铜近月挤仓风险仍需关注，现货升水走扩。\n'
        )
        segments = split(text)
        cu = [s for s in segments if s.product_code == 'CU']
        self.assertEqual(len(cu), 1, '同篇出现两次会让这家机构对沪铜投两票')
        self.assertIn('79,259', cu[0].raw_text)
        self.assertIn('近月挤仓', cu[0].raw_text, '合并而不是丢弃：下钻要用的原文一句不少')

    def test_seq按出场顺序连续编号(self):
        text = (
            '【有色】沪铜\n操作建议：逢低偏多。\n\n'
            '【黑色】螺纹钢\n操作建议：逢高沽空。\n\n'
            '【能化】甲醇\n操作建议：观望为主。\n'
        )
        segments = split(text)
        self.assertEqual([s.seq for s in segments], list(range(len(segments))))
        self.assertEqual([s.report_id for s in segments], [1] * len(segments))


class TestAdviceExtraction(unittest.TestCase):
    """advice_text 抽取的四级退化：保证**永远有东西给打分层**。"""

    def test_强标签优先且取最靠后的一个(self):
        text = ('昨日操作建议：逢高沽空（已失效）。'
                '最新情况：库存快速去化。'
                '操作建议：逢低偏多，关注 78,100 一线支撑。')
        self.assertIn('逢低偏多', seg._extract_advice(text))

    def test_弱标签必须带冒号(self):
        # 「建议关注库存」不带冒号，不能被当成标签
        text = '建议关注库存变化。观点：偏多思路为主。'
        self.assertIn('偏多思路为主', seg._extract_advice(text))

    def test_免责声明里的不构成投资建议不算标签(self):
        text = '操作建议：逢低偏多。本报告不构成投资建议。'
        self.assertIn('逢低偏多', seg._extract_advice(text))

    def test_没有任何标签时退回最后一个含线索词的句子(self):
        text = '昨日沪铜收涨。库存持续去化。整体仍以偏多思路对待。'
        self.assertIn('偏多思路', seg._extract_advice(text))

    def test_首句过短时补上下一句(self):
        text = '操作建议：谨慎。仓位上以逢低偏多为主。'
        advice = seg._extract_advice(text)
        self.assertIn('逢低偏多', advice, '「谨慎」这种半截话会被判成中性，把真实观点抹平')


class TestEdgeCases(unittest.TestCase):
    """边界输入：切分是批处理，畸形输入不能带走整篇。"""

    def test_空文本返回空列表(self):
        self.assertEqual(split(''), [])
        self.assertEqual(split('   \n\n  '), [])
        self.assertEqual(seg.split_segments(1, None), [])

    def test_整篇都是免责声明则一块都不产出(self):
        self.assertEqual(split('免责声明\n本报告不构成投资建议，据此操作，风险自担。'), [])

    def test_过短的无品种块被丢弃(self):
        text = '【有色】沪铜\n操作建议：逢低偏多。\n\n【附录】\n完\n'
        got = by_code(split(text))
        self.assertEqual(set(got), {'CU'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
