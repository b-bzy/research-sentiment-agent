"""数据模型契约。

全链路（采集 → 解析 → 切分 → 打分 → 聚合 → 呈现）都以这 4 个 dataclass
作为唯一的数据交换格式，字段含义在此定稿，各模块不得自行加减。

粒度对应关系::

    Report（一篇研报） → Segment（一机构 × 一品种的观点块） → Score（一次打分）
                                                          ↓
                                                    DailyIndex（当日聚合结果）
"""

from dataclasses import dataclass


@dataclass
class Report:
    """一篇研报的原件级记录。

    ★ publish_time / ingest_time 两个时间戳都要存，但**所有指标计算一律用
      ingest_time**。研报上标注的 publish_time 是「研报写好的那天」，而系统
      可能周一才抓到周五发的报告；拿 publish_time 去算日度指标，等于把周一才
      知道的信息塞回周五，直接引入未来函数——这是研报类指标最致命的错误。

    ★ file_md5 是去重键。同一篇研报会从官网、邮件、聚合站多渠道重复抓到，
      按文件内容 MD5 判重比按标题/URL 稳。
    """

    org_name: str                 # 机构名，如「永安期货」
    title: str                    # 研报标题
    publish_time: str             # 'YYYY-MM-DD'，研报标注的发布日（仅展示，不参与计算）
    ingest_time: str              # 'YYYY-MM-DDTHH:MM:SS'，进入本系统的时间 ★指标一律用它
    source_channel: str           # 'inbox' | 'website' | 'email'
    file_path: str                # 原件落地路径，解析规则会迭代，必须留原件以便重跑
    file_md5: str                 # ★ 文件内容 MD5，跨渠道去重
    parse_status: str             # 'pending' | 'ok' | 'failed'
    parsed_text: str = ''         # 解析出的纯文本，便于人工核对与重跑
    id: int | None = None


@dataclass
class Segment:
    """切分后的观点块，粒度 = 一个机构 × 一个品种 × 一个观点。

    整篇研报打一个分会把「沪铜看多、螺纹看空」搅成一杯中性浆糊，
    所以必须先切到品种粒度再打分。

    ★ product_code 为 None 表示「宏观/综述块」——这类块匹配不到具体品种，
      但往往是对大盘的直接判断，价值很高，单独归类而不是丢弃。

    ★ advice_text 单独抽出「操作建议」原句：期货日报的操作建议栏高度模板化，
      规则层主要靠它命中，抽不到则留空串，退回用 raw_text 打分。
    """

    report_id: int
    product_code: str | None      # 品种代码，None = 宏观/综述块
    product_name: str | None      # 品种中文名
    sector: str | None            # 板块名
    raw_text: str                 # 该品种的整块原文，下钻时展示上下文
    advice_text: str              # ★ 抽出的操作建议原句（抽不到则空串）
    seq: int                      # 块在研报内的顺序，保证可复现
    id: int | None = None


@dataclass
class Score:
    """一次打分结果。**只追加不修改**。

    ★ evidence 是证据原句，可解释下钻的唯一来源。交易员看到「焦煤 +65」的
      第一反应一定是「谁说的、凭什么」，答不上来这个指标就没人用。

    ★ model_version 记录打分口径版本。口径迭代后要能重算历史并对比新旧差异；
      不记版本，纵向可比性立刻失效。
    """

    segment_id: int
    direction: float              # -2.0 ~ +2.0，负=看空 正=看多
    confidence: float             # 0.0 ~ 1.0，低于阈值进人工复核队列
    evidence: str                 # ★ 证据原句，下钻用
    method: str                   # 'rule' | 'llm' | 'human'
    model_version: str            # ★ 打分口径版本号
    id: int | None = None


@dataclass
class DailyIndex:
    """日度聚合指标，可按新 calc_version 随时重算。

    ★ trade_date 取自 report.ingest_time 的日期部分，不是 publish_time。

    ★ coverage_count（覆盖机构家数）独立成列，与方向彻底分离：
      3 家看多和 30 家看多不该同权，热度是方向分的置信度，两者必须能分开看。

    ★ NA ≠ 0：某品种当日无机构覆盖时，**不产生该品种的记录**，绝不写 0。
      写 0 会被误读成「大家都看中性」。

    ★ **两个版本号都要记**（model_version + calc_version），且一起构成主键。
      一条指标是「某一版打分口径」经「某一版聚合口径」算出来的，缺任何一半，
      这个数就不知道自己是怎么来的：打分口径升版后重跑聚合会静默覆盖旧结果，
      「任何一个口径变了都能重算历史并对比新旧差异」这句话就只剩一半成立。
    """

    trade_date: str               # 'YYYY-MM-DD'，来自 ingest_time
    level: str                    # 'product' | 'sector' | 'market'
    key: str                      # 品种代码 / 板块名 / 'MARKET'
    net_score: float              # -100 ~ +100
    bull_count: int               # 看多家数
    bear_count: int               # 看空家数
    neutral_count: int            # 中性家数
    coverage_count: int           # ★ 覆盖机构家数 = 热度，与方向分离
    model_version: str            # ★ 这行指标基于哪一版打分口径
    calc_version: str             # ★ 聚合口径版本号
