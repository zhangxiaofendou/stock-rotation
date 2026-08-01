"""
AI 模块示例数据（seed）
======================
仅在数据库为空时写入，用于云端自初始化与本地演示。所有记录 is_seed=1，
与真实爬虫/人工导入数据明确区分，可在「导入真实数据前」用 clear_seed 清理。

示例数据仅覆盖少数板块，目的是让「研报/新闻共识」卡片与「ML 多因子排序」
在没有任何外部数据源时也能跑通并展示效果；不构成任何投资建议。
"""

from config.sector_map import get_sector_name

# 研报示例：字段含义见 ai/store.py upsert_research_reports 元组顺序
# (sector_code, broker, stock_code, stock_name, rating, prev_rating,
#  target_price, prev_target_price, rating_change, coverage_date,
#  core_view, risk_keywords, source_url)
_RAW_REPORTS = [
    # ---- 电力设备：多家上调，目标价上调潮 ----
    ("881281", "中信证券", None, None, "买入", "增持", 38.0, 32.0, "上调", "2026-07-10",
     "储能与海风装机超预期，龙头订单饱满，盈利拐点确认。", "原材料涨价;海外贸易壁垒", "https://example.com/r/eq1"),
    ("881281", "华泰证券", None, None, "买入", "买入", 40.0, 35.0, "维持", "2026-07-08",
     "装机高景气延续，估值仍处历史低位，维持买入。", "竞争加剧", "https://example.com/r/eq2"),
    ("881281", "国泰君安", None, None, "增持", "增持", 36.5, 33.0, "维持", "2026-06-28",
     "板块估值修复途中，盈利确定性提升。", "产能过剩", "https://example.com/r/eq3"),
    ("881281", "中金公司", "300750.SZ", "宁德时代", "买入", "增持", 320.0, 280.0, "上调", "2026-07-05",
     "电池龙头全球份额回升，海外订单放量。", "汇率波动", "https://example.com/r/eq4"),

    # ---- 计算机：评级上调 + 首次覆盖 ----
    ("881272", "广发证券", None, None, "买入", "中性", 45.0, 38.0, "上调", "2026-07-09",
     "AI 应用落地加速，国产算力需求爆发。", "技术迭代;政策落地不及预期", "https://example.com/r/cs1"),
    ("881272", "招商证券", None, None, "增持", None, 42.0, None, "首次", "2026-07-02",
     "信创与算力双主线，首次覆盖给予增持。", "下游预算收紧", "https://example.com/r/cs2"),
    ("881272", "天风证券", None, None, "买入", "买入", 44.0, 40.0, "维持", "2026-06-26",
     "行业景气上行，龙头业绩兑现。", "估值偏高", "https://example.com/r/cs3"),

    # ---- 医药生物：覆盖券商数量上升 ----
    ("881142", "兴业证券", None, None, "增持", "增持", 30.0, 28.0, "维持", "2026-07-06",
     "创新药出海逻辑强化，政策边际缓和。", "医保控费;临床失败", "https://example.com/r/med1"),
    ("881142", "东吴证券", None, None, "买入", None, 33.0, None, "首次", "2026-07-01",
     "CXO 订单回暖，首次覆盖看多。", "地缘政治", "https://example.com/r/med2"),
    ("881142", "国盛证券", None, None, "增持", "中性", 31.0, 27.0, "上调", "2026-06-24",
     "集采影响出清，板块配置价值凸显。", "集采扩围", "https://example.com/r/med3"),

    # ---- 电子：分化（有上调也有维持） ----
    ("881121", "国信证券", None, None, "买入", "增持", 52.0, 46.0, "上调", "2026-07-07",
     "半导体国产替代加速，设备订单高增。", "周期波动", "https://example.com/r/el1"),
    ("881121", "长江证券", None, None, "增持", "增持", 49.0, 47.0, "维持", "2026-06-30",
     "消费电子温和复苏，等待旺季验证。", "需求疲弱", "https://example.com/r/el2"),

    # ---- 有色金属：下调（对比，形成分歧） ----
    ("881168", "中泰证券", None, None, "中性", "增持", 22.0, 25.0, "下调", "2026-07-04",
     "商品价格回落，盈利预期下修。", "价格继续下行", "https://example.com/r/met1"),
    ("881168", "方正证券", None, None, "减持", "中性", 19.0, 23.0, "下调", "2026-06-27",
     "供需转弱，建议降低配置。", "宏观下行", "https://example.com/r/met2"),

    # ---- 食品饮料：中性（关注度平稳） ----
    ("881273", "海通证券", None, None, "增持", "增持", 88.0, 85.0, "维持", "2026-07-03",
     "消费弱复苏，龙头份额稳固，维持增持。", "需求不及预期", "https://example.com/r/fb1"),
    ("881273", "申万宏源", None, None, "中性", "中性", 80.0, 80.0, "维持", "2026-06-25",
     "板块缺乏催化，等待基本面信号。", "成本上行", "https://example.com/r/fb2"),

    # ---- 汽车：首次覆盖 ----
    ("881125", "东方证券", None, None, "买入", None, 28.0, None, "首次", "2026-07-08",
     "智能驾驶渗透率提升，整车出海提速，首次覆盖买入。", "价格战", "https://example.com/r/auto1"),

    # ---- 国防军工：上调 ----
    ("881166", "安信证券", None, None, "买入", "增持", 60.0, 54.0, "上调", "2026-07-06",
     "装备列装加速，订单确定性高。", "交付节奏", "https://example.com/r/mil1"),
]

# 新闻示例：(sector_code, headline, category, sentiment, published_at, source, url)
_RAW_NEWS = [
    ("881281", "国家能源局：上半年新型储能装机同比增长超 80%", "政策", "positive", "2026-07-12", "财联社", "https://example.com/n1"),
    ("881272", "国产大模型再获重大升级，多家厂商宣布开放API", "行业", "positive", "2026-07-11", "证券时报", "https://example.com/n2"),
    ("881168", "伦敦金属交易所铜价单周下跌 4%，有色板块承压", "行业", "negative", "2026-07-10", "华尔街见闻", "https://example.com/n3"),
    ("881142", "多款国产创新药获FDA突破性疗法认定", "行业", "positive", "2026-07-09", "医药魔方", "https://example.com/n4"),
    ("881125", "6月新能源汽车零售渗透率突破 50%", "行业", "positive", "2026-07-08", "乘联会", "https://example.com/n5"),
    ("881273", "部分白酒渠道库存高企，旺季动销承压", "行业", "negative", "2026-07-07", "21世纪经济报道", "https://example.com/n6"),
]


def get_seed_research_reports() -> list:
    """返回可直接传给 AIStore.upsert_research_reports 的元组列表。"""
    out = []
    for (
        code, broker, sc, sn, rating, prev_rating, tp, prev_tp,
        change, date, view, risks, url,
    ) in _RAW_REPORTS:
        target_change_pct = None
        if tp is not None and prev_tp not in (None, 0):
            target_change_pct = round((tp - prev_tp) / prev_tp * 100.0, 2)
        out.append((
            code, get_sector_name(code), broker, sc, sn, rating, prev_rating,
            tp, prev_tp, change, target_change_pct, date, view, risks, url, 1,
        ))
    return out


def get_seed_news() -> list:
    """返回可直接传给 AIStore.upsert_news 的元组列表。"""
    return [
        (code, head, cat, sent, date, src, url, 1)
        for (code, head, cat, sent, date, src, url) in _RAW_NEWS
    ]
