"""品种词典的单测。

词典本身是**项目核心资产之一**（架构方案第四节：品种是封闭集合，
词典匹配的准确率高于 NER 模型，且完全可解释、可维护）。
它出错的方式很隐蔽：不会抛异常，只会把沪铜的观点悄悄记到沪铝头上，
指标看起来一切正常，但每个数都是错的。

本文件专门钉住两条最容易被改坏的地基：

1. **别名长度降序 + 已匹配区间不可复用** —— 「氧化铝」必须先占位，
   否则短别名「铝」会把它啃掉，沪铝抢走氧化铝的观点；
2. **单字母合约代码不进别名表** —— I / J / C / A 这类代码一旦进了别名表，
   任意一个英文串都会误命中一堆品种。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sentiment import products                                  # noqa: E402


def codes(text: str) -> list[str]:
    return [p['code'] for p in products.match_products(text)]


class TestAliasPriority(unittest.TestCase):
    """★ 长别名优先占位：整个匹配逻辑的地基。"""

    def test_氧化铝不会被铝吃掉(self):
        self.assertEqual(codes('氧化铝'), ['AO'])
        self.assertEqual(codes('氧化铝期货价格企稳'), ['AO'])

    def test_同一句里氧化铝与沪铝各归各的(self):
        hits = codes('氧化铝现货松动，沪铝维持高位')
        self.assertEqual(hits, ['AO', 'AL'])

    def test_螺纹钢不会被螺纹重复命中(self):
        self.assertEqual(codes('螺纹钢'), ['RB'])
        self.assertEqual(codes('螺纹'), ['RB'])

    def test_热轧卷板与热卷是同一个品种(self):
        self.assertEqual(codes('热轧卷板'), ['HC'])
        self.assertEqual(codes('热卷'), ['HC'])

    def test_炼焦煤不会被拆成焦煤加焦炭(self):
        self.assertEqual(codes('炼焦煤库存回升'), ['JM'])


class TestAsciiBoundary(unittest.TestCase):
    """ASCII 别名必须有词边界，否则 PTA 里的 TA 会被当成另一个品种。"""

    def test_PTA整体命中一次(self):
        self.assertEqual(codes('PTA'), ['TA'])
        self.assertEqual(codes('pta 加工费走扩'), ['TA'])

    def test_PTA不会在英文串中间被误命中(self):
        self.assertEqual(codes('CAPTAIN'), [])
        self.assertEqual(codes('XPVCY'), [])

    def test_合约代码本身可作别名(self):
        self.assertEqual(codes('RB 主力合约'), ['RB'])
        self.assertEqual(codes('MA 主力'), ['MA'])

    def test_已知缺口_代码紧贴月份数字时不命中(self):
        """★ 记录一个**已知的、有意保留的**缺口，别在重构时误以为修好了。

        「RB2601」这种「代码 + 交割月」的写法在真实研报里很常见，但
        _boundary_ok 要求 ASCII 别名两侧都不是字母数字，所以它匹配不到。
        放宽成「右侧允许接数字」能修好这一条，但会削弱 PTA / PVC 这类
        别名的边界保护，收益与风险不成比例——demo 语料一律用中文品种名，
        真实上线前应当先用一批真研报量一下这种写法的占比再决定。
        """
        self.assertEqual(codes('RB2601 合约'), [])


class TestSingleLetterCodeNotAlias(unittest.TestCase):
    """★ 单字母合约代码（I 铁矿石 / J 焦炭 / C 玉米 / A 豆一…）不进别名表。

    进了的话，任何一个英文单词都会命中一串品种——这是整张词典最容易
    被「顺手补全」补坏的地方。
    """

    def test_单字母代码不在别名索引里(self):
        single = [code for code in products.PRODUCT_BY_CODE if len(code) == 1]
        self.assertTrue(single, '词典里本来就该有单字母代码的品种')
        index_keys = {alias for alias, _ in products._ALIAS_INDEX}
        for code in single:
            with self.subTest(code=code):
                self.assertNotIn(code.lower(), index_keys)

    def test_英文句子不会命中任何品种(self):
        self.assertEqual(codes('A JAIC report'), [])


class TestAmbiguousSingleChar(unittest.TestCase):
    """单字歧义词不收：「金」「银」会被资金/银行误命中。"""

    def test_资金与银行不命中贵金属(self):
        self.assertEqual(codes('增量资金进场，银行间流动性宽松'), [])

    def test_沪金沪银正常命中(self):
        self.assertEqual(codes('沪金沪银同步走强'), ['AU', 'AG'])


class TestOrderAndLookup(unittest.TestCase):
    """返回顺序按首次出现位置；查询接口的兜底行为。"""

    def test_按首次出现位置排序(self):
        self.assertEqual(codes('螺纹钢领涨，随后沪铜跟涨，最后铁矿石补涨'),
                         ['RB', 'CU', 'I'])

    def test_空文本返回空列表(self):
        self.assertEqual(products.match_products(''), [])
        self.assertEqual(products.match_products(None), [])

    def test_按代码与按中文名都能查到(self):
        self.assertEqual(products.get_product('CU')['name'], '沪铜')
        self.assertEqual(products.get_product('cu')['name'], '沪铜')
        self.assertEqual(products.get_product('沪铜')['code'], 'CU')
        self.assertIsNone(products.get_product('不存在的品种'))
        self.assertIsNone(products.get_product(None))

    def test_板块查询(self):
        self.assertEqual(products.get_sector('CU'), '有色')
        self.assertEqual(products.get_sector('AO'), '有色')
        self.assertIsNone(products.get_sector('ZZZ'))

    def test_match_one_只取第一个(self):
        self.assertEqual(products.match_one_product('沪铜与沪铝')['code'], 'CU')
        self.assertIsNone(products.match_one_product('今日无品种'))


class TestDictionaryHealth(unittest.TestCase):
    """词典结构自身的健康检查：改坏了要立刻炸出来。"""

    def test_代码唯一且板块合法(self):
        codes_ = [p['code'] for p in products.PRODUCTS]
        self.assertEqual(len(codes_), len(set(codes_)), '合约代码不能重复')
        for prod in products.PRODUCTS:
            with self.subTest(code=prod['code']):
                self.assertIn(prod['sector'], products.SECTORS)
                self.assertTrue(prod['aliases'], '每个品种至少要有一个别名')
                self.assertIn(prod['name'], prod['aliases'],
                              '中文名本身必须在别名里，否则按名字写的研报匹配不到')

    def test_别名索引按长度降序(self):
        lengths = [len(alias) for alias, _ in products._ALIAS_INDEX]
        self.assertEqual(lengths, sorted(lengths, reverse=True),
                         '★ 长度降序是长别名优先占位的前提，顺序错了氧化铝就会被铝吃掉')


if __name__ == '__main__':
    unittest.main(verbosity=2)
