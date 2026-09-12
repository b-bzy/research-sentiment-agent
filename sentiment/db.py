"""SQLite 落地层：schema + DAO。

架构方案第九节定的五张表原样落地（Postgres → SQLite 只换方言，字段不变）：
org / report / segment / score / daily_index。

★★ 三个必须理解的设计决策 ★★

1. **report 存 publish_time 和 ingest_time 两个时间戳，所有指标计算一律用
   ingest_time。**
   publish_time 是研报自己标注的发布日，ingest_time 是它进入本系统的时刻。
   周五发布、周一才抓到的研报，如果按 publish_time 算进周五的指标，就等于把
   周一才知道的信息塞回周五——回测立刻出现未来函数，指标全部失真。
   因此本文件所有按日期过滤的查询，一律走 ingest_time，不提供按 publish_time
   聚合的接口（从接口层面堵死误用）。

2. **score 表只追加不修改，每行带 model_version。**
   打分是贵且慢的一步（真实环境要调 LLM）。口径迭代时新写一行新版本的打分，
   老行原样保留，才能重算历史并对比新旧差异；就地 UPDATE 会让纵向可比性失效。

3. **daily_index 可按新 calc_version 重算，不用重跑打分。**
   主键是 (trade_date, level, key, model_version, calc_version) —— 聚合口径变了
   就换个 calc_version 重算一遍，新旧结果并存可比，而底层 score 一行都不用重跑。
   这就是「打分与聚合彻底分离」在表结构上的体现。

   ★ 主键里**两个版本号都要有**，缺一不可。只放 calc_version 的话，
   打分口径升版（rule-v1 → rule-v2）后重跑聚合会**静默覆盖**旧口径的指标行：
   表面上「打分表只追加、历史可重算」，实际上重算完就再也拿不到旧口径的指标去对比，
   而且看板头部的数字（来自 daily_index）会和下钻的证据（来自 score 最新行）
   分别属于两个口径，交易员点开格子看到的方向能和大盘分完全相反。
"""

import sqlite3
from pathlib import Path

from . import config
from .models import DailyIndex, Report, Score, Segment

# ---------------------------------------------------------------- schema

SCHEMA_SQL = """
-- 1. 机构。demo 里不维护后台，随 report 落地时按机构名 upsert。
CREATE TABLE IF NOT EXISTS org (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    short_name  TEXT,
    weight      REAL    NOT NULL DEFAULT 1.0,   -- 机构权重，v2 加权聚合用，demo 恒为 1
    is_active   INTEGER NOT NULL DEFAULT 1
);

-- 2. 研报（原件级）
CREATE TABLE IF NOT EXISTS report (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id         INTEGER NOT NULL REFERENCES org(id),
    title          TEXT    NOT NULL,
    publish_time   TEXT,                        -- 'YYYY-MM-DD' 研报标注的发布日，仅展示
    ingest_time    TEXT    NOT NULL,            -- ★ 'YYYY-MM-DDTHH:MM:SS' 指标一律用它
    source_channel TEXT    NOT NULL,            -- inbox / website / email
    file_path      TEXT    NOT NULL,
    file_md5       TEXT    NOT NULL UNIQUE,     -- ★ 内容 MD5 去重：同一篇会从多渠道重复抓到
    parse_status   TEXT    NOT NULL DEFAULT 'pending',   -- pending / ok / failed
    parsed_text    TEXT    NOT NULL DEFAULT '',
    created_at     TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_report_ingest ON report(ingest_time);
CREATE INDEX IF NOT EXISTS idx_report_status ON report(parse_status);

-- 3. 观点块：一个机构 × 一个品种 × 一个观点
CREATE TABLE IF NOT EXISTS segment (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id    INTEGER NOT NULL REFERENCES report(id) ON DELETE CASCADE,
    product_code TEXT,                          -- NULL = 宏观/综述块（对大盘的直接判断）
    product_name TEXT,
    sector       TEXT,
    raw_text     TEXT    NOT NULL,
    advice_text  TEXT    NOT NULL DEFAULT '',   -- 抽出的操作建议原句，规则层主要靠它
    seq          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_segment_report  ON segment(report_id);
CREATE INDEX IF NOT EXISTS idx_segment_product ON segment(product_code);

-- 4. 打分（不可变，只追加）
CREATE TABLE IF NOT EXISTS score (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id    INTEGER NOT NULL REFERENCES segment(id) ON DELETE CASCADE,
    direction     REAL    NOT NULL,             -- -2 ~ +2
    confidence    REAL    NOT NULL,             -- 0 ~ 1
    evidence      TEXT    NOT NULL DEFAULT '',  -- ★ 证据原句，可解释下钻的唯一来源
    method        TEXT    NOT NULL,             -- rule / llm / human
    model_version TEXT    NOT NULL,             -- ★ 打分口径版本号
    created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_score_segment ON score(segment_id);

-- 5. 每日指标（可按新 calc_version 重算）
CREATE TABLE IF NOT EXISTS daily_index (
    trade_date     TEXT    NOT NULL,            -- 取自 report.ingest_time 的日期部分
    level          TEXT    NOT NULL,            -- product / sector / market
    "key"          TEXT    NOT NULL,            -- 品种代码 / 板块名 / 'MARKET'
    net_score      REAL    NOT NULL,            -- -100 ~ +100
    bull_count     INTEGER NOT NULL DEFAULT 0,
    bear_count     INTEGER NOT NULL DEFAULT 0,
    neutral_count  INTEGER NOT NULL DEFAULT 0,
    coverage_count INTEGER NOT NULL DEFAULT 0,  -- ★ 覆盖机构家数 = 热度，与方向分离
    model_version  TEXT    NOT NULL,            -- ★ 这行指标基于哪一版**打分**口径
    calc_version   TEXT    NOT NULL,            -- ★ 基于哪一版**聚合**口径
    updated_at     TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (trade_date, level, "key", model_version, calc_version)
);
CREATE INDEX IF NOT EXISTS idx_index_date ON daily_index(trade_date, level);
"""


# ---------------------------------------------------------------- 连接

def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """建立连接。row_factory 用 sqlite3.Row（按列名取值），并开启外键约束。"""
    path = Path(db_path) if db_path else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """建表，全部 IF NOT EXISTS，可反复调用。"""
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def reset_db(db_path: Path | str | None = None) -> None:
    """删库重来（demo 用）。真实环境绝不该有这个函数。"""
    path = Path(db_path) if db_path else config.DB_PATH
    if path.exists():
        path.unlink()


# ---------------------------------------------------------------- 写入

def upsert_org(conn: sqlite3.Connection, org_name: str, short_name: str | None = None) -> int:
    """按机构名 upsert，返回 org_id。demo 不做机构后台，见到新机构就建。"""
    cur = conn.execute('SELECT id FROM org WHERE name = ?', (org_name,))
    row = cur.fetchone()
    if row is not None:
        return int(row['id'])
    cur = conn.execute(
        'INSERT INTO org(name, short_name, weight, is_active) VALUES (?, ?, 1.0, 1)',
        (org_name, short_name or org_name),
    )
    conn.commit()
    return int(cur.lastrowid)


def insert_report(conn: sqlite3.Connection, r: Report) -> int | None:
    """插入研报。**file_md5 重复直接返回 None**，这是全链路唯一的去重口子。

    同一篇研报会从官网、邮件、聚合站被重复抓到，按内容 MD5 判重比按标题/URL 稳。
    """
    # 先查 md5：重复件直接退出，避免为一篇根本不会入库的研报凭空建出机构记录
    if conn.execute('SELECT 1 FROM report WHERE file_md5 = ?', (r.file_md5,)).fetchone():
        return None
    org_id = upsert_org(conn, r.org_name)
    try:
        cur = conn.execute(
            """INSERT INTO report(org_id, title, publish_time, ingest_time, source_channel,
                                  file_path, file_md5, parse_status, parsed_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (org_id, r.title, r.publish_time, r.ingest_time, r.source_channel,
             r.file_path, r.file_md5, r.parse_status, r.parsed_text),
        )
    except sqlite3.IntegrityError:
        # md5 撞了 = 这篇已经在库里，安静跳过
        return None
    conn.commit()
    r.id = int(cur.lastrowid)
    return r.id


def update_report_parsed(conn: sqlite3.Connection, report_id: int, text: str, status: str) -> None:
    """回写解析结果。解析规则会迭代，原件保留在 file_path，随时可重跑覆盖。"""
    conn.execute(
        'UPDATE report SET parsed_text = ?, parse_status = ? WHERE id = ?',
        (text, status, report_id),
    )
    conn.commit()


def insert_segment(conn: sqlite3.Connection, s: Segment) -> int:
    """插入观点块。"""
    cur = conn.execute(
        """INSERT INTO segment(report_id, product_code, product_name, sector,
                               raw_text, advice_text, seq)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (s.report_id, s.product_code, s.product_name, s.sector,
         s.raw_text, s.advice_text, s.seq),
    )
    conn.commit()
    s.id = int(cur.lastrowid)
    return s.id


def insert_score(conn: sqlite3.Connection, sc: Score) -> int:
    """插入打分。**只追加，永不 UPDATE**，靠 model_version 区分口径版本。"""
    cur = conn.execute(
        """INSERT INTO score(segment_id, direction, confidence, evidence, method, model_version)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (sc.segment_id, sc.direction, sc.confidence, sc.evidence, sc.method, sc.model_version),
    )
    conn.commit()
    sc.id = int(cur.lastrowid)
    return sc.id


def upsert_daily_index(conn: sqlite3.Connection, di: DailyIndex) -> None:
    """写入/覆盖日度指标。

    主键 (trade_date, level, key, model_version, calc_version)：同一天、
    同一对口径重算就地覆盖；**任何一个版本号变了，新旧结果并存**，
    可以直接把两套口径的曲线画在一起比。
    """
    conn.execute(
        """INSERT INTO daily_index(trade_date, level, "key", net_score,
                                   bull_count, bear_count, neutral_count,
                                   coverage_count, model_version, calc_version, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'))
           ON CONFLICT(trade_date, level, "key", model_version, calc_version) DO UPDATE SET
               net_score      = excluded.net_score,
               bull_count     = excluded.bull_count,
               bear_count     = excluded.bear_count,
               neutral_count  = excluded.neutral_count,
               coverage_count = excluded.coverage_count,
               updated_at     = excluded.updated_at""",
        (di.trade_date, di.level, di.key, di.net_score,
         di.bull_count, di.bear_count, di.neutral_count,
         di.coverage_count, di.model_version, di.calc_version),
    )
    conn.commit()


# ---------------------------------------------------------------- 查询

def list_reports(conn: sqlite3.Connection, ingest_date: str | None = None) -> list[sqlite3.Row]:
    """列研报。ingest_date 形如 'YYYY-MM-DD'，★ 按 ingest_time 过滤而非 publish_time。"""
    sql = """SELECT r.*, o.name AS org_name
             FROM report r JOIN org o ON o.id = r.org_id"""
    params: tuple = ()
    if ingest_date:
        sql += ' WHERE substr(r.ingest_time, 1, 10) = ?'
        params = (ingest_date,)
    sql += ' ORDER BY r.ingest_time, r.id'
    return conn.execute(sql, params).fetchall()


def list_pending_parse(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """待解析的研报（parse_status = 'pending'）。"""
    return conn.execute(
        """SELECT r.*, o.name AS org_name
           FROM report r JOIN org o ON o.id = r.org_id
           WHERE r.parse_status = 'pending'
           ORDER BY r.id"""
    ).fetchall()


def list_unsegmented(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """已解析成功但还没切分的研报。"""
    return conn.execute(
        """SELECT r.*, o.name AS org_name
           FROM report r JOIN org o ON o.id = r.org_id
           WHERE r.parse_status = 'ok'
             AND NOT EXISTS (SELECT 1 FROM segment s WHERE s.report_id = r.id)
           ORDER BY r.id"""
    ).fetchall()


def list_unscored_segments(conn: sqlite3.Connection,
                           model_version: str | None = None) -> list[sqlite3.Row]:
    """还没被打过分的观点块。

    传 model_version 则表示「没有该版本打分的块」——口径升版后重跑打分用，
    老版本的打分行原样保留。
    """
    sql = """SELECT sg.*, o.name AS org_name FROM segment sg
             JOIN report r ON r.id = sg.report_id
             JOIN org o    ON o.id = r.org_id
             WHERE NOT EXISTS (SELECT 1 FROM score sc
                               WHERE sc.segment_id = sg.id {ver})
             ORDER BY sg.id"""
    if model_version:
        return conn.execute(sql.format(ver='AND sc.model_version = ?'),
                            (model_version,)).fetchall()
    return conn.execute(sql.format(ver='')).fetchall()


def fetch_scored_segments(conn: sqlite3.Connection,
                          ingest_date: str | None = None,
                          model_version: str | None = None) -> list[sqlite3.Row]:
    """★ 聚合层的唯一数据入口：JOIN report + segment + score。

    - 按 **report.ingest_time 的日期**过滤（`substr(ingest_time,1,10)`），
      绝不用 publish_time，避免未来函数。
    - score 只追加，同一个块可能有多行打分：这里**只取每个块 id 最大的那一行**
      （即最新一次打分）；指定 model_version 则取该版本内最新的一行。
    - 返回的行同时带 org_name / ingest_time，聚合层做「一家一票」去重要用。
    """
    params: list = []
    # 相关子查询：在同一个 segment 的多行打分里挑 id 最大的一行（= 最新一次打分）
    ver_filter = ''
    if model_version:
        ver_filter = 'AND s2.model_version = ?'
        params.append(model_version)

    sql = f"""
        SELECT r.id            AS report_id,
               o.name          AS org_name,
               r.title         AS title,
               r.publish_time  AS publish_time,
               r.ingest_time   AS ingest_time,
               substr(r.ingest_time, 1, 10) AS trade_date,
               sg.id           AS segment_id,
               sg.product_code AS product_code,
               sg.product_name AS product_name,
               sg.sector       AS sector,
               sg.advice_text  AS advice_text,
               sg.raw_text     AS raw_text,
               sg.seq          AS seq,
               sc.direction    AS direction,
               sc.confidence   AS confidence,
               sc.evidence     AS evidence,
               sc.method       AS method,
               sc.model_version AS model_version
        FROM segment sg
        JOIN report r ON r.id = sg.report_id
        JOIN org o    ON o.id = r.org_id
        JOIN score sc ON sc.id = (
            SELECT s2.id FROM score s2
            WHERE s2.segment_id = sg.id {ver_filter}
            ORDER BY s2.id DESC LIMIT 1
        )
    """
    if ingest_date:
        sql += ' WHERE substr(r.ingest_time, 1, 10) = ?'
        params.append(ingest_date)
    sql += ' ORDER BY r.ingest_time, r.id, sg.seq'
    return conn.execute(sql, tuple(params)).fetchall()


def fetch_daily_index(conn: sqlite3.Connection, trade_date: str,
                      level: str | None = None,
                      calc_version: str | None = None,
                      model_version: str | None = None) -> list[sqlite3.Row]:
    """取某天的聚合结果。

    ★ **两个版本号都要过滤**，不传则用 config 里的当前口径。少过滤一个，
      库里同时存在 (rule-v1, netscore-v1) 和 (rule-v2, netscore-v1) 两套结果时，
      同一个品种会返回两行，看板上就会出现两个互相矛盾的净得分。
    """
    sql = ('SELECT * FROM daily_index '
           'WHERE trade_date = ? AND calc_version = ? AND model_version = ?')
    params: list = [trade_date, calc_version or config.CALC_VERSION,
                    model_version or config.SCORE_VERSION]
    if level:
        sql += ' AND level = ?'
        params.append(level)
    sql += ' ORDER BY level, net_score DESC, "key"'
    return conn.execute(sql, tuple(params)).fetchall()


def fetch_index_series(conn: sqlite3.Connection, level: str, key: str,
                       limit: int = 30,
                       calc_version: str | None = None,
                       model_version: str | None = None,
                       end_date: str | None = None) -> list[sqlite3.Row]:
    """取某个 key 的时间序列，**按 trade_date 升序**返回（画曲线用）。

    :param limit: 最多取几个交易日
    :param end_date: 序列的**右端点**（含）。渲染历史某一天时必须传，
        否则「最近 limit 天」是从库里最新的一天往回数，
        指定一个较早的日期会拿到一段与它无关、甚至完全在它之后的序列。

    NA ≠ 0：没有覆盖的日子本来就不存在记录，序列里会直接缺这一天，
    不要在这里补 0，那会被误读成「当天大家都看中性」。
    """
    sql = ('SELECT * FROM daily_index '
           'WHERE level = ? AND "key" = ? AND calc_version = ? AND model_version = ?')
    params: list = [level, key, calc_version or config.CALC_VERSION,
                    model_version or config.SCORE_VERSION]
    if end_date:
        sql += ' AND trade_date <= ?'
        params.append(end_date)
    sql += ' ORDER BY trade_date DESC LIMIT ?'
    params.append(limit)
    return list(reversed(conn.execute(sql, tuple(params)).fetchall()))


def list_index_dates(conn: sqlite3.Connection, calc_version: str | None = None,
                     model_version: str | None = None) -> list[str]:
    """列出所有已算出指标的交易日，升序。两个版本号都按当前口径过滤。"""
    rows = conn.execute(
        'SELECT DISTINCT trade_date FROM daily_index '
        'WHERE calc_version = ? AND model_version = ? ORDER BY trade_date',
        (calc_version or config.CALC_VERSION, model_version or config.SCORE_VERSION),
    ).fetchall()
    return [row['trade_date'] for row in rows]


def count_table(conn: sqlite3.Connection, table: str) -> int:
    """统计行数，给 CLI 的 status 命令和测试用。表名做白名单校验，杜绝注入。"""
    allowed = {'org', 'report', 'segment', 'score', 'daily_index'}
    if table not in allowed:
        raise ValueError(f'未知表名：{table}')
    return int(conn.execute(f'SELECT COUNT(*) AS n FROM {table}').fetchone()['n'])
