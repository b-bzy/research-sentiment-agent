"""国内商品期货品种词典（项目核心资产之一）。

为什么用词典而不是 NER 模型：品种是**封闭集合**（国内商品期货约 70 个），
词典匹配的准确率高于模型，且完全可解释、可维护——研究员发现漏词直接加一行别名
就能修，不用重训。

词典结构::

    {'code': 'CU', 'name': '沪铜', 'aliases': ['沪铜', '电解铜', '铜'], 'sector': '有色'}

★ 匹配必须「按别名长度降序 + 已匹配区间不可复用」：
  文本里的「氧化铝」如果先被短别名「铝」吃掉，沪铝就会抢走氧化铝的观点。
  长别名优先命中并占位，短别名不能再落在已占位的字符上，从根上避免子串误吃。
"""

import re

# ---------------------------------------------------------------- 板块

SECTORS: list[str] = ['贵金属', '有色', '黑色', '能化', '油脂油料', '农产品']

# ---------------------------------------------------------------- 品种词典
# 别名表遵循两条规矩：
#   1. 不收「金」「银」这类单字歧义词（资金/银行会误命中）；
#   2. 单字母合约代码（I 铁矿石、J 焦炭、C 玉米…）不进别名表，
#      否则英文串里的任意一个字母都会误命中。

PRODUCTS: list[dict] = [
    # ---------------- 贵金属 ----------------
    {'code': 'AU', 'name': '沪金', 'aliases': ['沪金', '黄金'], 'sector': '贵金属'},
    {'code': 'AG', 'name': '沪银', 'aliases': ['沪银', '白银'], 'sector': '贵金属'},

    # ---------------- 有色 ----------------
    {'code': 'CU', 'name': '沪铜', 'aliases': ['沪铜', '电解铜', '阴极铜', '铜'], 'sector': '有色'},
    {'code': 'AL', 'name': '沪铝', 'aliases': ['沪铝', '电解铝', '铝'], 'sector': '有色'},
    {'code': 'ZN', 'name': '沪锌', 'aliases': ['沪锌', '锌锭', '锌'], 'sector': '有色'},
    {'code': 'NI', 'name': '沪镍', 'aliases': ['沪镍', '镍'], 'sector': '有色'},
    {'code': 'SN', 'name': '沪锡', 'aliases': ['沪锡', '锡'], 'sector': '有色'},
    {'code': 'PB', 'name': '沪铅', 'aliases': ['沪铅', '铅'], 'sector': '有色'},
    # 氧化铝必须比「铝」长，才能在匹配时先占位
    {'code': 'AO', 'name': '氧化铝', 'aliases': ['氧化铝'], 'sector': '有色'},

    # ---------------- 黑色 ----------------
    {'code': 'RB', 'name': '螺纹钢', 'aliases': ['螺纹钢', '螺纹', '建材'], 'sector': '黑色'},
    {'code': 'HC', 'name': '热卷', 'aliases': ['热轧卷板', '热轧卷', '热轧', '热卷'], 'sector': '黑色'},
    {'code': 'I', 'name': '铁矿石', 'aliases': ['铁矿石', '铁矿'], 'sector': '黑色'},
    {'code': 'JM', 'name': '焦煤', 'aliases': ['炼焦煤', '焦煤'], 'sector': '黑色'},
    {'code': 'J', 'name': '焦炭', 'aliases': ['焦炭'], 'sector': '黑色'},
    {'code': 'SF', 'name': '硅铁', 'aliases': ['硅铁'], 'sector': '黑色'},
    {'code': 'SM', 'name': '锰硅', 'aliases': ['锰硅', '硅锰'], 'sector': '黑色'},
    {'code': 'SS', 'name': '不锈钢', 'aliases': ['不锈钢'], 'sector': '黑色'},

    # ---------------- 能化 ----------------
    {'code': 'SC', 'name': '原油', 'aliases': ['原油'], 'sector': '能化'},
    {'code': 'FU', 'name': '燃料油', 'aliases': ['燃料油', '燃油'], 'sector': '能化'},
    {'code': 'BU', 'name': '沥青', 'aliases': ['沥青'], 'sector': '能化'},
    {'code': 'TA', 'name': 'PTA', 'aliases': ['PTA', '精对苯二甲酸'], 'sector': '能化'},
    {'code': 'EG', 'name': '乙二醇', 'aliases': ['乙二醇', 'MEG'], 'sector': '能化'},
    {'code': 'MA', 'name': '甲醇', 'aliases': ['甲醇'], 'sector': '能化'},
    {'code': 'V', 'name': 'PVC', 'aliases': ['PVC', '聚氯乙烯'], 'sector': '能化'},
    {'code': 'PP', 'name': '聚丙烯', 'aliases': ['聚丙烯'], 'sector': '能化'},
    {'code': 'L', 'name': '塑料', 'aliases': ['线型低密度聚乙烯', 'LLDPE', '聚乙烯', '塑料'], 'sector': '能化'},
    {'code': 'EB', 'name': '苯乙烯', 'aliases': ['苯乙烯'], 'sector': '能化'},
    {'code': 'UR', 'name': '尿素', 'aliases': ['尿素'], 'sector': '能化'},
    {'code': 'SA', 'name': '纯碱', 'aliases': ['纯碱'], 'sector': '能化'},
    {'code': 'FG', 'name': '玻璃', 'aliases': ['玻璃'], 'sector': '能化'},
    {'code': 'SI', 'name': '工业硅', 'aliases': ['工业硅', '金属硅'], 'sector': '能化'},
    {'code': 'LC', 'name': '碳酸锂', 'aliases': ['碳酸锂'], 'sector': '能化'},

    # ---------------- 油脂油料 ----------------
    {'code': 'Y', 'name': '豆油', 'aliases': ['大豆油', '豆油'], 'sector': '油脂油料'},
    {'code': 'P', 'name': '棕榈油', 'aliases': ['棕榈油', '棕油'], 'sector': '油脂油料'},
    {'code': 'OI', 'name': '菜油', 'aliases': ['菜籽油', '菜油'], 'sector': '油脂油料'},
    {'code': 'M', 'name': '豆粕', 'aliases': ['豆粕'], 'sector': '油脂油料'},
    {'code': 'RM', 'name': '菜粕', 'aliases': ['菜籽粕', '菜粕'], 'sector': '油脂油料'},
    {'code': 'A', 'name': '豆一', 'aliases': ['黄大豆一号', '豆一', '大豆'], 'sector': '油脂油料'},

    # ---------------- 农产品 ----------------
    {'code': 'C', 'name': '玉米', 'aliases': ['玉米'], 'sector': '农产品'},
    {'code': 'SR', 'name': '白糖', 'aliases': ['白糖', '郑糖'], 'sector': '农产品'},
    {'code': 'CF', 'name': '棉花', 'aliases': ['棉花', '郑棉'], 'sector': '农产品'},
    {'code': 'AP', 'name': '苹果', 'aliases': ['苹果'], 'sector': '农产品'},
    {'code': 'LH', 'name': '生猪', 'aliases': ['生猪'], 'sector': '农产品'},
    {'code': 'JD', 'name': '鸡蛋', 'aliases': ['鸡蛋'], 'sector': '农产品'},
    {'code': 'PK', 'name': '花生', 'aliases': ['花生'], 'sector': '农产品'},
]

# ---------------------------------------------------------------- 索引

#: code -> 品种，供 O(1) 查
PRODUCT_BY_CODE: dict[str, dict] = {p['code']: p for p in PRODUCTS}

#: 中文名 -> 品种，供 O(1) 查
PRODUCT_BY_NAME: dict[str, dict] = {p['name']: p for p in PRODUCTS}

#: 板块 -> 该板块下的品种列表
PRODUCTS_BY_SECTOR: dict[str, list[dict]] = {s: [] for s in SECTORS}
for _p in PRODUCTS:
    PRODUCTS_BY_SECTOR.setdefault(_p['sector'], []).append(_p)


def _build_alias_index() -> list[tuple[str, dict]]:
    """构建别名索引：(小写别名, 品种)，**按别名长度降序**。

    长度降序是整个匹配逻辑的地基：先让「氧化铝」「螺纹钢」这类长别名占位，
    「铝」「螺纹」才不会去啃它们的字符。等长别名按字典序排，保证结果可复现。
    """
    pairs: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for prod in PRODUCTS:
        # 合约代码本身也算别名，但单字母代码歧义太大（I / J / C / A …）直接跳过
        aliases = list(prod['aliases'])
        if len(prod['code']) >= 2:
            aliases.append(prod['code'])
        for alias in aliases:
            key = _lower_ascii(alias)
            if not key or key in seen:
                continue
            seen.add(key)
            pairs.append((key, prod))
    pairs.sort(key=lambda item: (-len(item[0]), item[0]))
    return pairs


def _lower_ascii(text: str) -> str:
    """只把 ASCII 字母转小写，保证转换前后**字符串长度不变**。

    直接 str.lower() 对个别 Unicode 字符会改变长度，一旦长度变了，
    下面按下标记录的「已占位区间」就会错位。
    """
    return ''.join(c.lower() if c.isascii() else c for c in text)


_ASCII_TOKEN = re.compile(r'^[0-9a-z]+$')


def _is_ascii_token(alias: str) -> bool:
    """别名是否为纯 ASCII 字母数字（如 PTA / PVC / LLDPE / RB）。"""
    return bool(_ASCII_TOKEN.match(alias))


def _boundary_ok(text: str, start: int, end: int, alias: str) -> bool:
    """ASCII 别名要求两侧不是字母数字，避免 PTA 里的 TA 被当成 PTA 之外的品种。"""
    if not _is_ascii_token(alias):
        return True
    if start > 0 and (text[start - 1].isascii() and text[start - 1].isalnum()):
        return False
    if end < len(text) and (text[end].isascii() and text[end].isalnum()):
        return False
    return True


_ALIAS_INDEX: list[tuple[str, dict]] = _build_alias_index()


def get_product(code: str | None) -> dict | None:
    """按合约代码取品种；取不到再退回按中文名取。找不到返回 None。"""
    if not code:
        return None
    key = code.strip().upper()
    prod = PRODUCT_BY_CODE.get(key)
    if prod is not None:
        return prod
    return PRODUCT_BY_NAME.get(code.strip())


def get_sector(code: str | None) -> str | None:
    """按合约代码取板块名，找不到返回 None。"""
    prod = get_product(code)
    return prod['sector'] if prod else None


def match_products(text: str) -> list[dict]:
    """在文本中匹配品种，返回去重后的品种列表，按**首次出现位置**排序。

    算法：别名长度降序扫描 + 已命中区间占位。
    例：「氧化铝」先占掉三个字符，随后短别名「铝」再来匹配时发现区间已被占用，
    直接跳过——沪铝就不会误吃氧化铝的观点。

    匹配不到任何品种时返回空列表，调用方据此把该块标记为「宏观/综述块」。
    """
    if not text:
        return []

    lowered = _lower_ascii(text)
    length = len(lowered)
    occupied = [False] * length          # 字符级占位表
    first_pos: dict[str, int] = {}       # code -> 首次命中位置

    for alias, prod in _ALIAS_INDEX:
        alias_len = len(alias)
        cursor = 0
        while cursor <= length - alias_len:
            pos = lowered.find(alias, cursor)
            if pos < 0:
                break
            end = pos + alias_len
            # 与已命中区间重叠，或 ASCII 边界不合法 → 放弃这一处，继续往后找
            if any(occupied[pos:end]) or not _boundary_ok(lowered, pos, end, alias):
                cursor = pos + 1
                continue
            for i in range(pos, end):
                occupied[i] = True
            code = prod['code']
            if code not in first_pos or pos < first_pos[code]:
                first_pos[code] = pos
            cursor = end

    ordered = sorted(first_pos.items(), key=lambda kv: (kv[1], kv[0]))
    return [PRODUCT_BY_CODE[code] for code, _ in ordered]


def match_one_product(text: str) -> dict | None:
    """只要第一个命中的品种；用于「一块里只该有一个品种」的场景。"""
    hits = match_products(text)
    return hits[0] if hits else None
