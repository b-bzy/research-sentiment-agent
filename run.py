#!/usr/bin/env python3
"""期货研报多空打分 Agent —— demo 版 CLI 入口。

    python3 run.py demo              # ★ 一键跑通全链路并出 HTML 看板
    python3 run.py ingest            # ① 采集：扫 data/inbox，按 MD5 去重入库
    python3 run.py pipeline          # ③④⑤⑥ 解析 → 切分 → 打分 → 聚合
    python3 run.py show   [--date D] # ⑦ 终端渲染（大盘分 + 板块 + 多空矩阵）
    python3 run.py push   [--date D] # ⑦ 打印企微/飞书推送文案
    python3 run.py html   [--date D] # ⑦ 导出单文件 HTML 看板
    python3 run.py status            # 各表行数速览
    python3 run.py reset             # 删库重来（demo 专用）

全链路七个环节的职责分工：

    ① ingest    采集：inbox 落地 + MD5 去重 + 打两个时间戳
    ③ parse     解析：原件 → 纯文本
    ④ segment   切分：按品种词典切成「一机构 × 一品种」的观点块
    ⑤ score     打分：规则层出方向 / 置信度 / 证据原句
    ⑥ aggregate 聚合：品种 → 板块 → 大盘，一家一票、NA ≠ 0
    ⑦ report    呈现：推送文案 / 终端渲染 / HTML 看板

★ 打分（⑤）与聚合（⑥）是分开的两个子命令环节：打分贵且不可变，
  聚合便宜且可反复重算。改聚合口径只需重跑 ⑥，不必重跑 ⑤。
"""

import argparse
import sqlite3
import sys
from pathlib import Path

# 允许在任意工作目录下 `python3 run.py` 执行
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sentiment import config, db, report   # noqa: E402


# ---------------------------------------------------------------- 环节装配
# 五个环节各由一个模块承担，入口函数统一是 `xxx(conn) -> dict 统计`。
# 这里用晚绑定（用到才 import）而不是模块顶部 import：
# 少一个模块只会让对应的子命令报错，show / push / html 这些纯呈现命令照常可用。

_STAGES = (
    # (键, 模块名, 中文标签, 入口函数名)
    ('ingest', 'ingest', '① 采集', 'ingest_inbox'),
    ('parse', 'parse', '③ 解析', 'parse_pending'),
    ('segment', 'segment', '④ 切分', 'segment_pending'),
    ('score', 'score', '⑤ 打分', 'score_pending'),
    ('aggregate', 'aggregate', '⑥ 聚合', 'aggregate_all'),
)

_STAGE_BY_KEY = {row[0]: row for row in _STAGES}


def _load_module(name: str):
    """加载 sentiment 子模块，缺席/报错返回 None（不连累其他子命令）。"""
    try:
        return __import__(f'sentiment.{name}', fromlist=[name])
    except Exception:
        return None


def _run_stage(conn, key: str) -> str:
    """执行一个环节，返回一行中文明细。"""
    _, mod_name, label, fn_name = _STAGE_BY_KEY[key]
    module = _load_module(mod_name)
    if module is None:
        raise SystemExit(f'✗ {label} 无法执行：sentiment/{mod_name}.py 缺失或导入失败')
    fn = getattr(module, fn_name, None)
    if not callable(fn):
        raise SystemExit(f'✗ {label} 无法执行：sentiment/{mod_name}.py 里没有 '
                         f'{fn_name}(conn) 这个入口函数')
    return _summarize(key, fn(conn))


def _summarize(key: str, stats) -> str:
    """把各环节返回的统计字典翻译成一行中文明细。

    各环节报的数含义并不相同（采集报篇数、切分报块数、聚合报指标行数），
    统一在这里翻译，CLI 打印的数字才对得上库里实际发生的事。
    """
    if not isinstance(stats, dict):
        return '完成（该环节未返回统计信息）'

    def g(name, default=0):
        return stats.get(name, default)

    if key == 'ingest':
        return (f'新入库 {g("new")} 篇 ｜ MD5 重复跳过 {g("duplicated")} 篇 '
                f'｜ 失败 {g("failed")} 篇 ｜ 格式不支持 {g("skipped_format")} 个')
    if key == 'parse':
        return f'解析成功 {g("ok")} / {g("total")} 篇 ｜ 失败 {g("failed")} 篇'
    if key == 'segment':
        return (f'切出 {g("segments")} 个观点块（品种 {g("product")} / '
                f'宏观综述 {g("macro")} / 多品种合并 {g("multi")}）'
                f'｜ 来自 {g("reports")} 篇研报')
    if key == 'score':
        # ★「本样本集」三个字不能省：这是规则表在这 24 篇虚构样本上的命中率，
        #   不是模型效果。样本是同一个作者写的，规则和语料互相知根知底，
        #   换一批真研报必然下降（架构方案第五节的预期是 60–70%）。
        return (f'打分 {g("scored")} / {g("total")} 块 ｜ '
                f'规则命中率（本样本集）{g("rule_hit_rate", 0.0) * 100:.1f}% ｜ '
                f'多/空/中 {g("bull")}/{g("bear")}/{g("neutral")} ｜ '
                f'低置信度待复核 {g("low_confidence")} 条')
    if key == 'aggregate':
        # aggregate_all 返回 {交易日: [DailyIndex, ...]}
        rows = sum(len(v) for v in stats.values() if isinstance(v, (list, tuple)))
        return f'{len(stats)} 个交易日 ｜ 写入 {rows} 行日度指标（无覆盖的品种不产出记录）'
    return '完成'


# ---------------------------------------------------------------- 子命令

def cmd_reset(args) -> int:
    db.reset_db(args.db)
    print(f'✓ 已清空数据库：{args.db or config.DB_PATH}')
    return 0


def cmd_ingest(args) -> int:
    conn = _open(args)
    print(f"① 采集完成：{_run_stage(conn, 'ingest')}")
    print(f'   当前库内研报 {db.count_table(conn, "report")} 篇 / '
          f'机构 {db.count_table(conn, "org")} 家')
    return 0


def cmd_pipeline(args) -> int:
    conn = _open(args)
    for key in ('parse', 'segment', 'score', 'aggregate'):
        label = _STAGE_BY_KEY[key][2]
        print(f'{label}完成：{_run_stage(conn, key)}')
    _print_totals(conn)
    return 0


def cmd_show(args) -> int:
    conn = _open(args)
    print(report.render_terminal(conn, args.date, use_color=not args.no_color))
    return 0


def cmd_push(args) -> int:
    conn = _open(args)
    print(report.build_push_message(conn, args.date))
    return 0


def cmd_matrix(args) -> int:
    conn = _open(args)
    print(report.render_matrix(conn, args.date, use_color=not args.no_color))
    return 0


def cmd_html(args) -> int:
    conn = _open(args)
    trade_date = report.resolve_date(conn, args.date)
    if not trade_date:
        print('✗ 库里还没有已聚合的交易日，请先执行：python3 run.py pipeline')
        return 1
    path = report.export_html(conn, trade_date, args.out)
    print(f'✓ HTML 看板已导出：{path}')
    print('  用浏览器打开即可；单文件、无外部依赖、可直接发给别人。')
    return 0


def cmd_status(args) -> int:
    conn = _open(args)
    _print_totals(conn)
    dates = db.list_index_dates(conn)
    if dates:
        print(f'  已聚合交易日：{dates[0]} ~ {dates[-1]}（共 {len(dates)} 天）')
    else:
        print('  已聚合交易日：无')
    return 0


def cmd_demo(args) -> int:
    """★ 一键跑通：reset → ingest → parse → segment → score → aggregate → 呈现。"""
    print('═' * 72)
    print('  期货研报多空打分 Agent · demo 全链路')
    print('═' * 72)

    db.reset_db(args.db)
    print('  0/6  reset      已清空数据库')

    conn = _open(args)
    for i, key in enumerate(('ingest', 'parse', 'segment', 'score', 'aggregate'), start=1):
        label = _STAGE_BY_KEY[key][2]
        print(f'  {i}/6  {key:<10} {label}  {_run_stage(conn, key)}')
    print('  6/6  report     ⑦ 呈现  推送文案 / 终端看板 / HTML 看板')
    print()

    # ---- 环节小结：用库里实际行数报数，比各环节自报的返回值更可信 ----
    print('─' * 72)
    print('  链路小结')
    print('─' * 72)
    rows = [
        ('机构 org', db.count_table(conn, 'org'), '按机构名 upsert'),
        ('研报 report', db.count_table(conn, 'report'), 'file_md5 去重后的原件数'),
        ('观点块 segment', db.count_table(conn, 'segment'), '一机构 × 一品种的粒度'),
        ('打分 score', db.count_table(conn, 'score'), f'只追加，口径 {config.SCORE_VERSION}'),
        ('日度指标 daily_index', db.count_table(conn, 'daily_index'),
         f'可重算，口径 {config.CALC_VERSION}'),
    ]
    for name, count, note in rows:
        print(f'  {name:<22}{count:>6}    {note}')
    dates = db.list_index_dates(conn)
    if dates:
        print(f'  {"覆盖交易日":<20}{len(dates):>6}    {dates[0]} ~ {dates[-1]}'
              f'（按 ingest_time 归集，非 publish_time）')
    print()

    trade_date = report.resolve_date(conn)
    if not trade_date:
        print('✗ 没有聚合出任何交易日，检查 data/inbox 里是否有样本研报。')
        return 1

    print(report.render_terminal(conn, trade_date, use_color=not args.no_color))
    print()
    print('─' * 72)
    print('  盘前推送文案预览（企业微信 / 飞书）')
    print('─' * 72)
    print(report.build_push_message(conn, trade_date))
    print()

    path = report.export_html(conn, trade_date, args.out)
    print('─' * 72)
    print(f'  ✓ HTML 看板已导出： {path}')
    print('    浏览器打开可看：情绪分仪表 / 历史折线 / 板块热力 / 多空矩阵（悬停下钻证据原句）')
    print('─' * 72)
    return 0


# ---------------------------------------------------------------- 辅助

def _open(args):
    """打开连接并保证表结构存在。

    ★ 库文件被写坏（不是 SQLite 文件、或表结构与当前 schema 冲突）时，
      sqlite3 会抛一段对使用者毫无意义的 traceback。demo 的数据随时可以重跑，
      所以这里把它翻译成一句话 + 一条可执行的修复命令——
      其余所有异常路径（inbox 缺失、空库 show、空库 html）都做了友好兜底，
      唯独这条漏了就显得整个 CLI 不可信。
    """
    path = args.db or config.DB_PATH
    try:
        conn = db.connect(args.db)
        db.init_schema(conn)
    except sqlite3.DatabaseError as exc:
        raise SystemExit(
            f'✗ 数据库文件已损坏或不是 SQLite 文件：{path}\n'
            f'  底层报错：{exc}\n'
            f'  执行 python3 run.py reset 重建即可（demo 数据可随时重跑）。'
        ) from exc
    return conn


def _print_totals(conn) -> None:
    print(f'  库内合计：机构 {db.count_table(conn, "org")} 家 ｜ '
          f'研报 {db.count_table(conn, "report")} 篇 ｜ '
          f'观点块 {db.count_table(conn, "segment")} 个 ｜ '
          f'打分 {db.count_table(conn, "score")} 条 ｜ '
          f'日度指标 {db.count_table(conn, "daily_index")} 行')


# ---------------------------------------------------------------- 入口

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='run.py',
        description='期货研报多空打分 Agent（demo 版）：'
                    '把各家期货公司研报判成看多/看空/中性，汇总成大盘偏多还是偏空。',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='第一次用直接跑：python3 run.py demo',
    )
    parser.add_argument('--db', default=None,
                        help=f'SQLite 路径，默认 {config.DB_PATH}')
    sub = parser.add_subparsers(dest='command', metavar='子命令')

    def add(name: str, help_text: str, func):
        sp = sub.add_parser(name, help=help_text, description=help_text)
        sp.set_defaults(func=func)
        return sp

    p_demo = add('demo', '★ 一键全链路：reset → 采集 → 解析 → 切分 → 打分 → 聚合 → 出看板',
                 cmd_demo)
    p_demo.add_argument('--out', default=None, help='HTML 看板输出路径')
    p_demo.add_argument('--no-color', action='store_true', help='关闭 ANSI 颜色')

    add('ingest', '① 采集：扫描 data/inbox 落地入库（file_md5 去重）', cmd_ingest)
    add('pipeline', '③④⑤⑥ 解析 → 切分 → 打分 → 聚合', cmd_pipeline)

    p_show = add('show', '⑦ 终端渲染：大盘分 + 板块热力 + 多空矩阵', cmd_show)
    p_show.add_argument('--date', default=None, help='交易日 YYYY-MM-DD，默认最新一天')
    p_show.add_argument('--no-color', action='store_true', help='关闭 ANSI 颜色')

    p_matrix = add('matrix', '⑦ 只打印机构 × 品种多空矩阵', cmd_matrix)
    p_matrix.add_argument('--date', default=None, help='交易日 YYYY-MM-DD，默认最新一天')
    p_matrix.add_argument('--no-color', action='store_true', help='关闭 ANSI 颜色')

    p_push = add('push', '⑦ 打印企业微信 / 飞书推送文案', cmd_push)
    p_push.add_argument('--date', default=None, help='交易日 YYYY-MM-DD，默认最新一天')

    p_html = add('html', '⑦ 导出单文件 HTML 看板', cmd_html)
    p_html.add_argument('--date', default=None, help='交易日 YYYY-MM-DD，默认最新一天')
    p_html.add_argument('--out', default=None, help='输出路径，默认 out/dashboard-<日期>.html')

    add('status', '各表行数与已聚合交易日速览', cmd_status)
    add('reset', '删库重来（demo 专用）', cmd_reset)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, 'command', None):
        parser.print_help()
        return 0
    # 各子命令的可选参数不一致，统一补默认值，省掉一堆 getattr
    for name, default in (('date', None), ('out', None), ('no_color', False)):
        if not hasattr(args, name):
            setattr(args, name, default)
    return args.func(args)


if __name__ == '__main__':
    raise SystemExit(main())
