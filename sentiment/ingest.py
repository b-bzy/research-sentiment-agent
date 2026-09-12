"""① 采集：把 data/inbox 里的样本研报扫描入库。

真实环境的采集层是「官网适配器 + 邮件 IMAP + 聚合站兜底」三路并行，
demo 版把这一层退化成**投递目录扫描**：把样本研报（.txt）丢进 ``data/inbox``，
本模块负责算 MD5、解析文件名元数据、落库成 ``parse_status='pending'`` 的 report 行。
换成真实爬虫时，只需要把「文件从哪来」换掉，本模块下游的一切保持不变。

★★ 两个必须理解的设计点 ★★

1. **MD5 去重是全链路唯一的去重口子。**
   同一篇研报会同时从官网、客户邮件、聚合站被抓到（还会被重复抓多轮），
   按标题或 URL 判重都不稳（标题会带日期后缀、URL 会带 token），
   只有**文件内容 MD5** 稳定。判重逻辑落在 :func:`db.insert_report` 里，
   本模块只负责把 md5 算准并统计跳过了几篇。

2. **ingest_time 是「进入本系统的时间」，不是文件时间，更不是 publish_time。**
   所有日度指标一律按 ingest_time 归日。用 publish_time 算指标 = 未来函数：
   周五发布、周一才抓到的研报，按 publish_time 会被算进周五，
   等于把周一才知道的信息塞回周五，回测结果全部失真。

   ⚠️ **demo 回填模式（有意为之的取舍，不是疏忽）** ⚠️
   本 demo 需要在一次运行里造出「连续 3 天」的情绪序列，才能演示时间序列与环比。
   因此当调用方**不传 now_iso（即 now_iso=None）**时，本模块进入「回填模式」：
   用文件名里的 publish_time + 'T06:30:00' 充当 ingest_time，
   模拟「每天早上 6:30 抓到当天这批研报」。

   这正是架构方案第十一节点名的头号风险「用错时点」。之所以在 demo 里明知故犯：
   - demo 没有真实的历史抓取记录，全部现抓会让 3 天研报挤在同一个时间点，
     聚合出来只有 1 个数据点，看板上是一条没有长度的线，演示不出「按时间可比」；
   - 回填模式必须由调用方显式选择（不传 now_iso），且每次运行都会打印警告。

   **生产环境必须由调度器（APScheduler，每天 6:30 / 8:00 / 12:00 三轮）
   传入真实抓取时刻 now_iso，绝不允许走回填分支。**
"""

import hashlib
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from . import config
from .db import insert_report
from .models import Report

__all__ = [
    'parse_filename',
    'md5_of_file',
    'resolve_ingest_time',
    'ingest_inbox',
    'fetch_from_website',
]

# ---------------------------------------------------------------- 常量

#: demo 回填模式假定的每日抓取时刻（模拟盘前 6:30 那一轮）
BACKFILL_CLOCK = '06:30:00'

#: demo 回填模式的**盘中那一轮**（模拟 12:00 那一轮）。
#: 标题里带这些词的研报按这个时刻回填，而不是 6:30。
#: 为的是让「一家一票」这条口径在 demo 数据上**真的被触发一次**：
#: 同一家机构当天发早报 + 午评，两篇的 ingest_time 必须真的分先后，
#: 去重才是在按「取最新一篇」工作，而不是在靠 report_id 这个次级排序键兜底。
#: 生产环境不需要这套东西——真实抓取时刻由调度器给，本来就带分秒。
BACKFILL_INTRADAY_CLOCK = '12:30:00'
BACKFILL_INTRADAY_HINTS = ('午间', '午评', '午后', '盘中', '更新', '日中')

#: 文件名解析失败时的机构名兜底值（宁可标成未知，也不能中断整批）
UNKNOWN_ORG = '未知机构'

#: 合法来源渠道，与 models.Report.source_channel 契约一致
ALLOWED_CHANNELS = ('inbox', 'website', 'email')

#: inbox 里认哪些后缀。★ 必须与 parse.py 声明支持的格式保持一致——
#: 采集层白名单比解析层窄的话，parse.py 里写好的 HTML / PDF 分支从 CLI 路径
#: 根本走不到，往 inbox 丢一个 .pdf 会被**静默吞掉**：既不报错也不计入失败数，
#: 用户只会觉得「这篇怎么没进来」。
#: demo 的主路径仍是 .txt（等价于 MinerU 已经跑完的产物）；
#: .pdf 收进来是为了让「缺 pymupdf」这件事**明确地失败**，而不是假装没发生。
INBOX_SUFFIXES = ('.txt', '.md', '.markdown', '.html', '.htm', '.pdf')

#: 文件名里日期段能接受的几种写法
_DATE_FORMATS = ('%Y%m%d', '%Y-%m-%d', '%Y.%m.%d', '%Y/%m/%d')

#: 「长得像日期」的形状。用来识别 20261301 这种**非法但显然是日期位**的段——
#: 它该被当成解析失败的日期丢掉，而不是被当成机构名存进 org 表。
_DATE_SHAPE = re.compile(r'^\d{4}[-./]?\d{1,2}[-./]?\d{1,2}$')


def _log(verbose: bool, msg: str) -> None:
    """demo 版的日志就是 print，生产版换成 logging 即可。"""
    if verbose:
        print(f'[采集] {msg}')


# ---------------------------------------------------------------- 文件名解析

def _parse_date_token(token: str) -> str | None:
    """把文件名里的日期段归一成 'YYYY-MM-DD'，认不出返回 None。"""
    token = token.strip()
    if not token:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(token, fmt).strftime('%Y-%m-%d')
        except ValueError:
            continue
    return None


def parse_filename(path: Path | str) -> dict:
    """从 ``YYYYMMDD_机构名_标题.txt`` 解析出元数据。

    :returns: ``{'publish_time': str|None, 'org_name': str, 'title': str,
              'filename_ok': bool}``

    ★ **绝不抛异常。** 采集是批处理，一个畸形文件名不能带走整批
      ——所有异常路径都退化成兜底值，并用 ``filename_ok=False`` 标出来，
      让调用方能在日志里看见「这篇的元数据是猜的」。

    兜底规则（从严到宽）：

    ==============================  ==============  ==========  ==============
    文件名                          publish_time    org_name    title
    ==============================  ==============  ==========  ==============
    20260908_永安期货_商品日报.txt   2026-09-08      永安期货     商品日报
    20260908_永安期货.txt            2026-09-08      永安期货     永安期货
    永安期货_商品日报.txt             None           永安期货     商品日报
    20261301_永安期货_日报.txt        None           永安期货     日报
    随手命名.txt                      None          未知机构      随手命名
    ==============================  ==============  ==========  ==============

    倒数第二行是「日期段非法」的情形：``20261301`` 长得像日期但不是合法日期，
    丢掉这一段而不是把它当机构名，免得 org 表里凭空多出一家「20261301 期货」。

    标题里本身带下划线（``..._早评_有色专题.txt``）时，第 3 段之后原样拼回，
    不会把标题截断。

    生产环境的元数据不该靠文件名猜——官网抓取时页面上就有发布时间和机构，
    邮件抓取时信头里有发件人和收信时间，那些才是权威来源。
    文件名解析只是 demo 里「没有网页可抓」时的替代方案。
    """
    try:
        p = Path(path)
        stem = p.stem.strip()
        meta = {
            'publish_time': None,
            'org_name': UNKNOWN_ORG,
            'title': stem or p.name,
            'filename_ok': False,
        }
        if not stem:
            return meta

        parts = [seg.strip() for seg in stem.split('_')]
        parts = [seg for seg in parts if seg]
        if not parts:
            return meta

        publish_time = _parse_date_token(parts[0])
        # 首段是日期（或长得像日期但写错了，如 20261301）就吃掉它，
        # 否则整串都当「机构 + 标题」处理。
        # 「长得像日期」的段即使解析失败也不能留——否则 20261301 会变成机构名，
        # 在 org 表里凭空多出一家不存在的期货公司。
        looks_like_date = bool(_DATE_SHAPE.match(parts[0]))
        rest = parts[1:] if (publish_time or looks_like_date) else parts
        meta['publish_time'] = publish_time

        if rest:
            meta['org_name'] = rest[0]
            # 标题段可能自带下划线，第 3 段起原样拼回，不截断
            meta['title'] = '_'.join(rest[1:]) if len(rest) > 1 else rest[0]

        meta['filename_ok'] = bool(publish_time and len(rest) >= 2)
        return meta
    except Exception:                                    # noqa: BLE001 —— 兜底优先
        name = str(path)
        return {'publish_time': None, 'org_name': UNKNOWN_ORG,
                'title': name, 'filename_ok': False}


# ---------------------------------------------------------------- MD5

def md5_of_file(path: Path | str, chunk_size: int = 1 << 20) -> str:
    """算文件内容 MD5（分块读，PDF 上百兆也不会把内存吃穿）。

    ★ 只哈希**内容**，不掺文件名/路径/时间：同一篇研报从官网下下来叫
      ``xxx.pdf``、从邮件附件存下来叫 ``附件1.pdf``，内容一样就必须判成同一篇。
    """
    digest = hashlib.md5()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b''):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------- 时间戳

def _normalize_now(now_iso: str) -> str:
    """把调用方传的时间归一成 'YYYY-MM-DDTHH:MM:SS'。

    容错三种写法：``2026-09-10T06:30:00`` / ``2026-09-10 06:30:00`` / ``2026-09-10``
    （只给日期时补 6:30，即盘前那一轮）。实在认不出就原样返回，
    宁可存一个奇怪的字符串，也不要在采集环节抛异常中断整批。
    """
    text = now_iso.strip()
    if len(text) == 10 and _parse_date_token(text):
        return f'{text}T{BACKFILL_CLOCK}'
    try:
        return datetime.fromisoformat(text.replace(' ', 'T')).strftime('%Y-%m-%dT%H:%M:%S')
    except ValueError:
        return text


def resolve_ingest_time(now_iso: str | None, publish_time: str | None,
                        title: str = '') -> str:
    """定出这篇研报的 ingest_time（★ 全链路指标的唯一时间基准）。

    - ``now_iso`` 有值 → **生产路径**：整批共用调用方给的抓取时刻。
      一次抓取就是一个时点，同批研报理应同一个 ingest_time。
    - ``now_iso`` 为 None → **demo 回填**：publish_time + 'T06:30:00'，
      假装「这篇是它发布当天早上 6:30 被抓到的」。见模块 docstring 的警告。
      标题里带「午间 / 午评 / 更新」等字样的按 12:30 回填，模拟盘中那一轮。
    - 回填模式下连 publish_time 都没解析出来 → 只能退回当前时刻，
      这种文件在 demo 里会掉进「今天」，日志会提示。
    """
    if now_iso:
        return _normalize_now(now_iso)
    if publish_time:
        # ★ demo 回填：拿 publish_time 当 ingest_time，生产环境禁止
        clock = (BACKFILL_INTRADAY_CLOCK
                 if any(hint in (title or '') for hint in BACKFILL_INTRADAY_HINTS)
                 else BACKFILL_CLOCK)
        return f'{publish_time}T{clock}'
    return datetime.now().strftime('%Y-%m-%dT%H:%M:%S')


# ---------------------------------------------------------------- 扫描入库

def _iter_inbox_files(directory: Path) -> tuple[list[Path], list[Path]]:
    """列出 inbox 里的文件，返回 ``(待采集, 后缀不支持被跳过的)``。

    两个列表都**按文件名排序**，保证每次跑的顺序（以及 report.id）一致。
    子目录和隐藏文件（.DS_Store 之类）两边都不进。

    ★ 被跳过的也要返回，不能就地丢掉：静默丢弃是采集层最坏的一种失败——
      用户看到「新增 24 篇」，不会想到自己丢进去的第 25 个文件压根没被看一眼。
    """
    accepted: list[Path] = []
    skipped: list[Path] = []
    for p in directory.iterdir():
        if not p.is_file() or p.name.startswith('.'):
            continue
        (accepted if p.suffix.lower() in INBOX_SUFFIXES else skipped).append(p)
    return (sorted(accepted, key=lambda p: p.name),
            sorted(skipped, key=lambda p: p.name))


def ingest_inbox(conn: sqlite3.Connection,
                 now_iso: str | None = None,
                 channel: str = 'inbox',
                 inbox_dir: Path | str | None = None,
                 verbose: bool = True) -> dict:
    """扫描 inbox 目录，逐个文件算 MD5 → 判重 → 解析文件名 → 落库。

    :param conn: 已建表的 SQLite 连接
    :param now_iso: 本轮抓取时刻 ``'YYYY-MM-DDTHH:MM:SS'``。
        **生产环境必传**（由调度器给出真实抓取时刻）；
        传 None 则进入 demo 回填模式（见模块 docstring 的警告）。
    :param channel: ``'inbox' | 'website' | 'email'``，写进 report.source_channel
    :param inbox_dir: 覆盖扫描目录，默认 ``config.INBOX_DIR``（测试用）
    :param verbose: 是否打印日志
    :returns: ``{'new': int, 'duplicated': int, 'failed': int,
              'skipped_format': int, 'reports': list[Report]}``，
              ``reports`` 只含本轮**新入库**的记录（已带上数据库分配的 id）；
              ``skipped_format`` 是后缀不在 INBOX_SUFFIXES 白名单里、
              **一眼都没看**的文件数（.docx / .xlsx / .zip …）

    每篇的处理顺序是刻意的：**先算 MD5、先判重，再做别的**。
    重复件占了实际抓取量的大头（多渠道 + 多轮重抓），
    早跳过就不用为它解析元数据、不用为它凭空建机构记录。

    落库时 ``parse_status='pending'``、``parsed_text=''``：
    采集只管把原件登记在册，解析是下一环的事。解析规则会迭代，
    原件留在 file_path，随时可以重跑覆盖 parsed_text。
    """
    if channel not in ALLOWED_CHANNELS:
        raise ValueError(f'未知来源渠道：{channel}，只允许 {ALLOWED_CHANNELS}')

    directory = Path(inbox_dir) if inbox_dir else config.INBOX_DIR
    stats: dict = {'new': 0, 'duplicated': 0, 'failed': 0,
                   'skipped_format': 0, 'reports': []}

    if not directory.is_dir():
        _log(verbose, f'投递目录不存在，跳过采集：{directory}')
        return stats

    backfill = now_iso is None
    if backfill:
        _log(verbose, '⚠️ demo 回填模式：ingest_time 用「文件名 publish_time + '
                      f'{BACKFILL_CLOCK}」模拟每日盘前抓取。'
                      '生产环境必须由调度器传入真实抓取时刻，否则指标会引入未来函数。')
    else:
        _log(verbose, f'本轮抓取时刻 ingest_time = {_normalize_now(now_iso)}')

    files, skipped = _iter_inbox_files(directory)
    for path in skipped:
        stats['skipped_format'] += 1
        _log(verbose, f'暂不支持的格式，跳过：{path.name}'
                      f'（本 demo 只收 {"/".join(INBOX_SUFFIXES)}）')

    for path in files:
        # 1. 算 MD5（顺带拿文件大小，空文件直接判失败，不让它污染库）
        try:
            file_md5 = md5_of_file(path)
            size = path.stat().st_size
        except OSError as exc:
            stats['failed'] += 1
            _log(verbose, f'读取失败，跳过：{path.name}（{exc}）')
            continue

        if size == 0:
            stats['failed'] += 1
            _log(verbose, f'空文件，跳过：{path.name}')
            continue

        # 2. 解析文件名元数据（永远不抛异常，失败也有兜底值）
        meta = parse_filename(path)
        if not meta['filename_ok']:
            _log(verbose, f'文件名不合 YYYYMMDD_机构_标题 规范，已用兜底元数据：{path.name}')

        # 3. 定 ingest_time —— ★ 不是文件 mtime，也不是 publish_time（回填模式除外）
        ingest_time = resolve_ingest_time(now_iso, meta['publish_time'], meta['title'])
        if backfill and not meta['publish_time']:
            _log(verbose, f'回填模式下拿不到 publish_time，退回当前时刻：{path.name}')

        report = Report(
            org_name=meta['org_name'],
            title=meta['title'],
            publish_time=meta['publish_time'] or '',   # 仅展示，不参与任何计算
            ingest_time=ingest_time,                   # ★ 指标一律用它
            source_channel=channel,
            file_path=str(path.resolve()),             # 留原件路径，解析规则迭代后可重跑
            file_md5=file_md5,
            parse_status='pending',                    # 采集只登记，解析是下一环
            parsed_text='',
        )

        # 4. 落库。★ md5 撞了 → insert_report 返回 None，即「这篇已经抓过」
        try:
            report_id = insert_report(conn, report)
        except sqlite3.Error as exc:
            stats['failed'] += 1
            _log(verbose, f'入库失败，跳过：{path.name}（{exc}）')
            continue

        if report_id is None:
            stats['duplicated'] += 1
            continue

        stats['new'] += 1
        stats['reports'].append(report)

    _log(verbose, f'采集完成：新增 {stats["new"]} 篇，'
                  f'重复跳过 {stats["duplicated"]} 篇，失败 {stats["failed"]} 篇，'
                  f'格式不支持 {stats["skipped_format"]} 个')
    return stats


# ---------------------------------------------------------------- 生产版占位

def fetch_from_website(org: str):
    """【生产版接口占位】按机构名抓取其官网研究所栏目的最新研报。

    :param org: 机构名，如「永安期货」
    :raises NotImplementedError: demo 版不联网，恒抛异常

    **生产版该怎么做**（架构方案第二节的选型）：

    1. **官网适配器**：``requests`` 拉列表页 + ``BeautifulSoup`` 解析出
       ``(标题, 发布时间, PDF 链接)``，再下载 PDF 落到 file_path。
       每家一个适配器（``ADAPTERS = {'永安期货': YonganAdapter, ...}``），
       几十家源、页面结构简单，上 Scrapy 是杀鸡用牛刀。
       配套：限速 + UA 轮换 + 失败重试；期货公司官网反爬普遍很弱，不需要代理池。
    2. **邮件 IMAP**：很多期货公司只对客户发邮件推送，
       开一个专用邮箱订阅，用标准库 ``imaplib`` 收信取附件，比爬官网还稳。
       发件人域名 → 机构名映射，收信时间是天然可信的 ingest_time。
    3. **聚合站兜底**：官网抓失败时从慧博/东财补齐，
       但聚合站的发布时间可能失真，只作补漏、并标记来源以便区分。
    4. 无论哪条路，落库前都走和 :func:`ingest_inbox` 完全相同的三件事：
       **算内容 MD5 判重 → 记录真实抓取时刻为 ingest_time → parse_status='pending'**。
       这也是把采集逻辑和入库逻辑分开写的原因：换数据源不影响下游任何一环。

    **demo 版为什么不实现**：硬性约束是零第三方依赖 + 不联网
    （requests / BeautifulSoup 都用不了），且真实抓取会引入网络波动与反爬
    不确定性，让 demo 无法稳定复现。demo 改用 ``data/inbox`` 投递目录，
    把「文件从哪来」这一步替换掉，而 MD5 去重、双时间戳、pending 状态机
    这些**真正体现设计取舍的部分原样保留**。
    """
    raise NotImplementedError(
        f'demo 版不联网，无法抓取「{org}」的官网研报。'
        '请把样本 .txt 放进 data/inbox 后调用 ingest_inbox()。'
    )
