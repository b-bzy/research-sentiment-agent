"""BUG-06 验收测试：免责声明劫持品种方向。

对应 docs/08-BUG-06修复方案.md 第四节的 TC-FIX06-xx 用例。

分四组：
  A. 缺陷修复（修复前红、修复后绿）
  B. 防误伤（修复前后都必须绿——词表宁窄勿宽的守门人）
  C. 全量回归（逐值锁定，证明没改坏存量行为）
  D. 边界

    python3 -m unittest tests.test_segment_disclaimer -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sentiment.segment import (             # noqa: E402
    split_segments,
    _looks_like_disclaimer_advice,
    _extract_advice,
)
from sentiment.score import classify_advice  # noqa: E402


def segs(text, report_id=1):
    return split_segments(report_id, text)


def by_product(text, code):
    for s in segs(text):
        if s.product_code == code:
            return s
    return None


# ---------------------------------------------------------------- A. 缺陷修复

class TestDisclaimerHijack(unittest.TestCase):
    """A 组：文末声明块不得覆盖上一个品种的真实建议。"""

    def test_F01_风险提示不得劫持方向(self):
        t = """【黑色】螺纹钢
逻辑分析：需求回暖。
操作建议：逢低偏多。

【风险提示】
本报告基于公开资料，不保证准确性。
操作建议：请投资者谨慎决策，逢高沽空需自担风险。
"""
        rb = by_product(t, 'RB')
        self.assertIsNotNone(rb, '螺纹钢块不应消失')
        self.assertIn('逢低偏多', rb.advice_text)
        self.assertNotIn('沽空', rb.advice_text, '免责声明的方向词不得进入 advice')
        self.assertGreater(classify_advice(rb.advice_text)['direction'], 0.25,
                           '方向必须仍是看多')

    def test_F02_重要提示不得劫持方向(self):
        t = """【有色】沪铜
操作建议：多头思路为主。

【重要提示】
操作建议：本报告仅供参考，据此操作风险自担。
"""
        cu = by_product(t, 'CU')
        self.assertIsNotNone(cu)
        self.assertIn('多头', cu.advice_text)
        self.assertGreater(classify_advice(cu.advice_text)['direction'], 0.25)

    def test_F03_无标题声明段不得劫持方向(self):
        t = """【能化】甲醇
操作建议：逢高沽空。

本报告由本公司研究所编制。
操作建议：以上建议仅供参考，投资者应自行承担风险。
"""
        ma = by_product(t, 'MA')
        self.assertIsNotNone(ma)
        self.assertIn('沽空', ma.advice_text)
        self.assertLess(classify_advice(ma.advice_text)['direction'], -0.25)

    def test_F04_免责声明回归_原本就对(self):
        t = """【黑色】焦煤
操作建议：逢低偏多。

【免责声明】
操作建议：据此操作，风险自担。
"""
        jm = by_product(t, 'JM')
        self.assertIsNotNone(jm)
        self.assertIn('逢低偏多', jm.advice_text)

    def test_F07_连续两个声明块(self):
        t = """【贵金属】沪金
操作建议：谨慎逢低偏多。

【风险提示】
操作建议：市场有风险，入市需谨慎。

【免责声明】
操作建议：本报告不构成投资建议。
"""
        au = by_product(t, 'AU')
        self.assertIsNotNone(au)
        self.assertIn('偏多', au.advice_text)
        self.assertGreater(classify_advice(au.advice_text)['direction'], 0.25)

    def test_F18_声明语在句子中间也要识别(self):
        self.assertTrue(_looks_like_disclaimer_advice('逢高沽空，但请投资者自行判断'))
        self.assertTrue(_looks_like_disclaimer_advice('以上观点仅供参考'))


# ---------------------------------------------------------------- B. 防误伤

class TestNoFalsePositive(unittest.TestCase):
    """B 组：真实建议不得被当成声明语。词表宁窄勿宽。"""

    def test_F05_中部风险提示不得砍掉后续品种(self):
        t = """【黑色】螺纹钢
操作建议：逢低偏多。

【风险提示】
需求不及预期。

【有色】沪铜
操作建议：逢高沽空。

【能化】甲醇
操作建议：区间操作。
"""
        codes = [s.product_code for s in segs(t) if s.product_code]
        for want in ('RB', 'CU', 'MA'):
            self.assertIn(want, codes, f'{want} 块不得丢失')
        self.assertIn('逢低偏多', by_product(t, 'RB').advice_text)
        self.assertIn('沽空', by_product(t, 'CU').advice_text)
        self.assertIn('区间', by_product(t, 'MA').advice_text)

    def test_F06_真实风险分析不覆盖advice但保留原文(self):
        t = """【黑色】铁矿石
操作建议：逢低偏多。

【风险提示】
需求不及预期，海外发运超预期回升，宏观情绪转弱。
"""
        i = by_product(t, 'I')
        self.assertIsNotNone(i)
        self.assertIn('逢低偏多', i.advice_text, '真实建议不得被风险分析覆盖')
        self.assertIn('需求不及预期', i.raw_text, 'raw_text 必须保留风险段，下钻不丢信息')

    def test_F09_谨慎不得被当成声明语(self):
        self.assertFalse(_looks_like_disclaimer_advice('谨慎逢低偏多，关注支撑'))
        self.assertFalse(_looks_like_disclaimer_advice('谨慎追空'))
        t = "【有色】沪锌\n操作建议：谨慎逢低偏多，关注 21,000 支撑。\n"
        zn = by_product(t, 'ZN')
        self.assertIn('谨慎逢低偏多', zn.advice_text)
        self.assertGreater(classify_advice(zn.advice_text)['direction'], 0.25)

    def test_F10_风险二字不得被当成声明语(self):
        self.assertFalse(_looks_like_disclaimer_advice('注意回调风险，逢低偏多'))
        self.assertFalse(_looks_like_disclaimer_advice('警惕下行风险，观望为主'))

    def test_F11_仅字不得被当成声明语(self):
        self.assertFalse(_looks_like_disclaimer_advice('仅日内短差操作'))
        self.assertFalse(_looks_like_disclaimer_advice('建议仅轻仓参与'))

    def test_F12_标题锚到品种的块含风险提示仍保留(self):
        t = """【黑色】焦炭
逻辑分析：供给收缩。风险提示：政策超预期。
操作建议：逢低偏多。
"""
        j = by_product(t, 'J')
        self.assertIsNotNone(j, '标题锚定到品种的块不得因含风险提示被丢弃')
        self.assertIn('逢低偏多', j.advice_text)

    def test_extract_advice_无声明语时与原逻辑等价(self):
        """改动 C 的等价性：没有声明语时仍取最靠后的标签。"""
        t = '操作建议：昨日回顾为逢高沽空。\n操作建议：今日逢低偏多。'
        self.assertIn('逢低偏多', _extract_advice(t))


# ---------------------------------------------------------------- C. 全量回归

class TestFullRegression(unittest.TestCase):
    """C 组：27 篇真实样本的切分结果逐值锁定。"""

    @classmethod
    def setUpClass(cls):
        import glob
        from sentiment.parse import parse_report
        from sentiment import config
        cls.segments = []
        files = sorted(glob.glob(str(config.INBOX_DIR / '*.txt')))
        for i, f in enumerate(files):
            text, status = parse_report(f)
            if status == 'ok':
                cls.segments.extend(split_segments(i + 1, text))
        cls.files = files

    def test_F13_样本切分结果不变(self):
        self.assertEqual(len(self.files), 27, '样本应为 27 篇')
        self.assertEqual(len(self.segments), 227, '观点块总数必须仍是 227')
        product = sum(1 for s in self.segments if s.product_code)
        macro = sum(1 for s in self.segments if not s.product_code)
        self.assertEqual(product, 220, '品种块必须仍是 220')
        self.assertEqual(macro, 7, '宏观块必须仍是 7')

    def test_F13b_advice指纹不变(self):
        import hashlib
        fp = hashlib.md5(
            ''.join(s.advice_text or '' for s in self.segments).encode()
        ).hexdigest()
        self.assertEqual(fp, '8a9e6a247b9d9eebef08c6e665a597e6',
                         '全部 advice_text 必须与修复前逐字一致')

    def test_F13c_没有任何advice是声明语(self):
        bad = [s.advice_text for s in self.segments
               if _looks_like_disclaimer_advice(s.advice_text or '')]
        self.assertEqual(bad, [], f'不应有声明语混进 advice：{bad[:3]}')


# ---------------------------------------------------------------- D. 边界

class TestEdgeCases(unittest.TestCase):

    def test_F08_声明块在开头不崩(self):
        t = """【免责声明】
本报告仅供参考，据此操作风险自担。

【有色】沪铝
操作建议：逢低偏多。
"""
        out = segs(t)
        al = by_product(t, 'AL')
        self.assertIsNotNone(al, '开头的声明不得吃掉后面的品种')
        self.assertIn('逢低偏多', al.advice_text)
        self.assertFalse(any(_looks_like_disclaimer_advice(s.advice_text or '')
                             for s in out))

    def test_F17_全篇只有声明块(self):
        t = """【风险提示】
市场有风险，入市需谨慎。
操作建议：请投资者自行承担风险。
"""
        out = segs(t)   # 不抛异常即可
        self.assertFalse(any(s.product_code for s in out), '不得凭空产生品种块')

    def test_空输入不崩(self):
        self.assertEqual(segs(''), [])
        self.assertFalse(_looks_like_disclaimer_advice(''))


if __name__ == '__main__':
    unittest.main(verbosity=2)
