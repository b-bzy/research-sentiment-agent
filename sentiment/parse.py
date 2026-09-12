"""③ 解析：把研报原件转成**纯文本**，回写 report.parsed_text。

★★ 关于选型的取舍（生产版 vs demo 版）★★

架构方案第三节定的生产选型是 **MinerU**（opendatalab，≈79.6k star）：
期货日报是图文混排 + 大量表格，MinerU 的输出是**带标题层级的 Markdown**，
而「标题层级」正是下游 ``segment.py`` 按品种切块的主锚点——
用 pdfplumber / PyPDF2 之类抽出来是一团没有结构的文字流，切分直接崩。
解析质量决定整条链路的上限：把「沪铜」章节的观点错切到「沪铝」名下，
后面打分再准也没有意义。

本 demo 受「零第三方依赖」约束，**走 .txt/.md 直通**：样本研报本身就是纯文本，
等价于「MinerU 已经跑完、吐出了 Markdown」这一步的产物。
因此降级只发生在本文件内部，``segment.py`` 及其之后的逻辑与生产版完全一致，
不会因为这里少了一个 MinerU 而失真。

★ 三类失败的处理方式故意不同（不是笔误）：
  - **不支持的格式**（.docx / .xlsx …）：预期内的「这类文件本 demo 不管」，
    安静返回 status='failed'，不打断批处理。
    （这类文件在采集层就被 ingest.INBOX_SUFFIXES 挡下并打了日志，
    正常流程走不到这里；这里是给直接调用 parse_report 的人兜底。）
  - **PDF 但没装 pymupdf**：PDF 是真实环境 90% 的主力格式，缺依赖属于
    **环境配置错误**，必须抛异常炸出来让人去装；静默记 failed 会让人误以为是
    研报本身有问题，白白丢掉九成数据还查不出原因。
  - **读出来是一堆乱码**（二进制文件伪装成 .txt、编码猜错）：判 failed。
    见 :func:`garbled_ratio`——「成功解析出乱码」比「解析失败」危险得多。

★ 采集层的白名单（ingest.INBOX_SUFFIXES）与本文件支持的格式保持一致，
  所以 .html / .pdf 这两个分支**从 CLI 路径真的走得到**：
  往 data/inbox 丢一个 .html 会正常解析，丢一个 .pdf 会明确报「缺 pymupdf」。
"""

import re
from html.parser import HTMLParser
from pathlib import Path

from . import db

# ---------------------------------------------------------------- 常量

STATUS_OK = 'ok'
STATUS_FAILED = 'failed'

#: 纯文本类后缀，demo 主路径
TEXT_SUFFIXES = {'.txt', '.md', '.markdown'}
#: 网页类后缀，用标准库 html.parser 做极简正文抽取
HTML_SUFFIXES = {'.html', '.htm'}
#: PDF，需要 pymupdf；demo 环境通常没有
PDF_SUFFIXES = {'.pdf'}

#: 读文本的编码尝试顺序。研报来源杂，GBK 系编码在国内文本里仍很常见
_ENCODINGS = ('utf-8-sig', 'utf-8', 'gb18030')

#: 「不可读字符」占比超过它就判解析失败。
#: 0.10 是个很松的阈值：正常研报解析出来这个比例是 0（换行/制表符不算），
#: 而二进制文件、编码猜错的文件、加密 PDF 的文本层，比例都在 30% 以上。
MAX_GARBLED_RATIO = 0.10


class ParseDependencyError(RuntimeError):
    """解析依赖缺失（如 PDF 需要 pymupdf）。

    单独定义一个异常类型，是为了让 ``parse_pending`` 能把「环境没装库」
    和「文件本身有问题」两类失败分开统计——前者是运维问题，后者是数据问题，
    混在一起会让排查方向完全跑偏。
    """


# ---------------------------------------------------------------- 文本规范化

def normalize_text(text: str) -> str:
    """统一换行、空白与空行密度，输出下游可直接按行/按段处理的干净文本。

    ★ 只动空白字符，**不动任何实义字符**：证据原句要原文回显给交易员，
      在这里做同义替换/删词会让下钻结果与原文对不上。
    """
    if not text:
        return ''
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    # 全角空格、不换行空格统一成普通空格：切分时靠空白切「标题 / 正文」，
    # 研报里这两种空格混用极多，不统一会漏切
    text = text.replace('　', ' ').replace('\xa0', ' ')
    lines = [line.rstrip() for line in text.split('\n')]
    text = '\n'.join(lines)
    # 3 个以上连续换行压成 2 个：保留「空行分段」这一信号，去掉排版噪声
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# ---------------------------------------------------------------- 各格式解析

def _read_text_file(path: Path) -> str:
    """按编码优先级读纯文本；全部失败则用 utf-8 + replace 兜底，绝不抛。"""
    raw = path.read_bytes()
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', errors='replace')


class _PlainTextExtractor(HTMLParser):
    """极简 HTML 正文抽取器（标准库实现，对标生产版的 trafilatura）。

    做两件事就够覆盖公众号/网页版日报：
    1. 丢掉 script / style / head 这类非正文标签里的内容；
    2. 把块级标签还原成换行——**换行是下游切分的段落信号，不能丢**。
    """

    _SKIP_TAGS = {'script', 'style', 'noscript', 'head', 'title', 'meta', 'link'}
    _BLOCK_TAGS = {
        'p', 'div', 'br', 'li', 'tr', 'section', 'article', 'header', 'footer',
        'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'table', 'ul', 'ol', 'pre',
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)   # &nbsp; 之类实体自动还原
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._chunks.append('\n')
        elif tag in ('td', 'th'):
            self._chunks.append(' ')

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCK_TAGS:
            self._chunks.append('\n')

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self._chunks.append(data)

    def get_text(self) -> str:
        return ''.join(self._chunks)


def _parse_html(path: Path) -> str:
    """HTML → 正文纯文本。"""
    parser = _PlainTextExtractor()
    parser.feed(_read_text_file(path))
    parser.close()
    return parser.get_text()


def _parse_pdf(path: Path) -> str:
    """PDF → 纯文本。装了 pymupdf/fitz 就用，没装抛 ParseDependencyError。

    ★ 即便装了 pymupdf，它也**只是应急方案**：它抽出来的是无层级的文字流，
      标题层级丢失，下游只能退回「品种词典单锚定」，切分准确率会明显下降。
      生产环境请用 MinerU（见模块 docstring）。
    """
    module = None
    for name in ('pymupdf', 'fitz'):
        try:
            module = __import__(name)
            break
        except ImportError:
            continue
    if module is None:
        raise ParseDependencyError(
            f'无法解析 PDF：{path.name}。\n'
            '本 demo 遵守「零第三方依赖」约束，未安装任何 PDF 解析库。\n'
            '解决办法二选一：\n'
            '  1) 把研报另存为 .txt / .md 放进 data/inbox/（demo 推荐路径）；\n'
            '  2) 生产环境请接 MinerU（架构方案第三节），它能输出带标题层级的\n'
            '     Markdown，标题层级正是按品种切分的主锚点。'
        )
    pages: list[str] = []
    with module.open(str(path)) as doc:      # type: ignore[attr-defined]
        for page in doc:
            pages.append(page.get_text())
    return '\n'.join(pages)


def garbled_ratio(text: str) -> float:
    """「不可读字符」占比：U+FFFD 替换字符 + 控制字符（\\n \\t 除外）。

    :func:`_read_text_file` 最后一档兜底是 ``utf-8 + errors='replace'``，
    它对任何字节流都不会抛异常——一个二进制文件也会被「成功」读成一串乱码。
    没有这道闸门的话，乱码会一路走成 parse_status='ok'、切出一个宏观块、
    打一个 direction=0/confidence=0 的分。指标数值不会被污染
    （confidence=0 的块在聚合层就被 drop_unreadable 剔掉了），
    但「研报 N 篇 / 宏观综述 N 个 / 机构 N 家」这些计数会虚增，
    讲解 demo 时数字对不上，排查起来还找不到源头。
    """
    if not text:
        return 0.0
    bad = sum(1 for ch in text
              if ch == '�' or (ord(ch) < 32 and ch not in '\n\t'))
    return bad / len(text)


def parse_report(file_path: str) -> tuple[str, str]:
    """解析单个原件，返回 ``(text, status)``，status ∈ {'ok', 'failed'}。

    - ``.txt`` / ``.md``  → 直接读（demo 主路径，等价于 MinerU 的产物）
    - ``.html`` / ``.htm``→ 标准库 html.parser 极简正文抽取
    - ``.pdf``            → 有 pymupdf 就用，**没装则抛 ParseDependencyError**
    - 其他后缀 / 文件不存在 / 内容为空 / **解析出一堆乱码** → ``('', 'failed')``

    :raises ParseDependencyError: 输入是 PDF 但环境里没有可用的 PDF 解析库。
    """
    path = Path(file_path)
    if not path.is_file():
        return '', STATUS_FAILED

    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        raw = _read_text_file(path)
    elif suffix in HTML_SUFFIXES:
        raw = _parse_html(path)
    elif suffix in PDF_SUFFIXES:
        raw = _parse_pdf(path)          # 缺依赖时在这里抛，故意不吞
    else:
        return '', STATUS_FAILED

    text = normalize_text(raw)
    # 解析出空文本等于没解析成功：让它留在 failed 里等人看，
    # 记 ok 会让一篇空研报静悄悄地流进后面的切分和聚合
    if not text:
        return '', STATUS_FAILED
    # ★ 可读性闸门：读出来是一堆乱码等于没读懂，同样算失败（见 garbled_ratio）
    if garbled_ratio(text) > MAX_GARBLED_RATIO:
        return '', STATUS_FAILED
    return text, STATUS_OK


# ---------------------------------------------------------------- 批处理

def _failure_reason(file_path: str) -> str:
    """给解析失败的行写一个**说得清是哪一类问题**的原因。

    原因会写进 report.parsed_text 留在库里，人工排查时不用再翻日志。
    「不支持的格式」是数据问题，「乱码」多半是编码或文件本身的问题，
    两者的处理方式完全不同，不能都写成一句「解析失败」。
    """
    path = Path(file_path)
    if not path.is_file():
        return f'原件不存在或不可读：{file_path}'
    suffix = path.suffix.lower()
    if suffix not in (TEXT_SUFFIXES | HTML_SUFFIXES | PDF_SUFFIXES):
        return f'不支持的格式 {suffix or "(无后缀)"}：{file_path}'
    try:
        ratio = garbled_ratio(normalize_text(_read_text_file(path)))
    except OSError:
        return f'原件读取失败：{file_path}'
    if ratio > MAX_GARBLED_RATIO:
        return (f'解析出的内容不可读（乱码占比 {ratio:.0%} > {MAX_GARBLED_RATIO:.0%}），'
                f'多半不是文本文件或编码不对：{file_path}')
    return f'解析出空内容：{file_path}'


def parse_pending(conn) -> dict:
    """把库里 ``parse_status='pending'`` 的研报全部解析并回写。

    返回统计::

        {'total': 待解析篇数, 'ok': 成功, 'failed': 失败,
         'dependency_missing': 因缺依赖失败的篇数,
         'errors': [(report_id, title, 原因), ...]}

    ★ 失败的行会把原因写进 parsed_text（前缀 ``[解析失败]``）并把 parse_status
      置为 'failed'。原因留在库里，人工排查时不用再翻日志；而 parse_status
      是下游唯一的闸门（``db.list_unsegmented`` 只取 'ok'），坏数据不会漏下去。
    """
    rows = db.list_pending_parse(conn)
    stats = {'total': len(rows), 'ok': 0, 'failed': 0,
             'dependency_missing': 0, 'errors': []}

    for row in rows:
        report_id = int(row['id'])
        title = row['title']
        file_path = row['file_path']
        try:
            text, status = parse_report(file_path)
            reason = '' if status == STATUS_OK else _failure_reason(file_path)
        except ParseDependencyError as exc:
            text, status = '', STATUS_FAILED
            reason = str(exc)
            stats['dependency_missing'] += 1
        except Exception as exc:                       # noqa: BLE001
            # 单篇解析炸掉不能拖垮整批：记下来、标 failed、继续跑下一篇
            text, status = '', STATUS_FAILED
            reason = f'{type(exc).__name__}: {exc}'

        if status == STATUS_OK:
            db.update_report_parsed(conn, report_id, text, STATUS_OK)
            stats['ok'] += 1
        else:
            db.update_report_parsed(conn, report_id, f'[解析失败] {reason}', STATUS_FAILED)
            stats['failed'] += 1
            stats['errors'].append((report_id, title, reason))

    return stats
