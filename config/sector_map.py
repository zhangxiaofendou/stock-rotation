"""
板块分类映射模块（同花顺行业底座）
==================================
板块宇宙采用**同花顺行业板块**（代码 881xxx），而非申万/东方财富：
- 申万指数上游(swsindex.com)长期停滞在 7.30，无法满足「数据到最新交易日」诉求；
- 东方财富行业板块在云端常因 K 线/清单接口被掐而回退 AkShare，同样卡在 7.30；
- 同花顺行业板块 K 线为活跃源，收盘后即可取到最新交易日（实测含 7.31）。

设计要点：
- 模块级 `SW_LEVEL2_MAP` / `SECTOR_GROUPS` / `SW_LEVEL2_BENCHMARK` 初始为空，
  由 `refresh_em_universe(em_map)` **原地填充**（不重新赋值全局变量，
  以保证 `from config.sector_map import SW_LEVEL2_MAP` 的引用始终有效）。
- 真实板块清单由 `data/sector_universe.ensure_em_industry_map(source)` 在
  管线/看板启动时拉取（缓存 JSON → 同花顺实时 → 静态兜底），并调用本模块的
  `refresh_em_universe` 完成填充。
- 板块组（大金融/新能源/消费/周期/科技/医药）按行业名**关键词自动归类**。
"""

# ============================================================
# 板块组定义（自动归类用）
# ============================================================
GROUP_ORDER = ["大金融", "新能源", "消费", "周期", "科技", "医药", "其他"]
GROUP_DESC = {
    "大金融": "银行/证券/保险/多元金融等金融地产相关板块",
    "新能源": "光伏/电池/风电/储能/电网/电源设备等新能源与电力设备板块",
    "消费": "食品饮料/家电/汽车/零售/旅游/纺服/美容等消费类板块",
    "周期": "钢铁/有色/化工/石油/煤炭/建材/建筑/机械/运输/农业等周期板块",
    "科技": "半导体/电子/计算机/通信/传媒/军工等科技 TMT 板块",
    "医药": "化学制药/中药/生物制品/医疗器械/医疗服务等医药生物板块",
    "其他": "未明确归入上述组的行业",
}
# 各板块组对应的风格基准（用于 RS 横截面归一）
GROUP_BENCHMARK = {
    "大金融": "000300.SH",  # 沪深300
    "消费": "000300.SH",    # 沪深300
    "周期": "000300.SH",    # 沪深300
    "新能源": "000905.SH",  # 中证500
    "科技": "000852.SH",    # 中证1000
    "医药": "000852.SH",    # 中证1000
    "其他": "000905.SH",    # 中证500
}

# 行业名 → 板块组的优先级关键词（靠前的组优先匹配；先到先得）
_GROUP_KEYWORDS = [
    ("大金融", ["银行", "证券", "保险", "信托", "期货", "多元金融", "财富",
                "金控", "租赁", "货币", "金融"]),
    ("医药", ["医药", "医疗", "生物", "制药", "中药", "疫苗", "医美",
              "健康", "医疗服务", "医疗器械", "医药商业"]),
    ("科技", ["半导体", "芯片", "电子", "元件", "光学", "消费电子", "计算机",
              "软件", "通信", "传媒", "游戏", "影视", "互联网", "数据",
              "人工智能", "机器人", "自动化", "航空装备", "航天", "军工",
              "卫星", "数字", "IT", "信息"]),
    ("新能源", ["光伏", "电池", "风电", "储能", "新能源", "电源", "电网",
                "电机", "充电", "氢能", "核能", "核电", "能源金属", "电力设备"]),
    ("消费", ["食品", "饮料", "酒", "家电", "家居", "服装", "纺织", "零售",
              "商超", "商贸", "旅游", "酒店", "餐饮", "美容", "化妆", "个护",
              "文娱", "教育", "汽车", "乘用车", "摩托", "商业", "品牌",
              "消费", "电商"]),
    ("周期", ["钢铁", "有色", "化工", "石油", "煤炭", "水泥", "建材", "建筑",
              "工程机械", "机械", "航运", "港口", "物流", "交通", "运输",
              "燃气", "环保", "农业", "种植", "养殖", "饲料", "化肥", "橡胶",
              "塑料", "金属", "稀土", "采掘", "造纸", "包装", "电力", "水务",
              "房地产", "机场"]),
]


def classify_em_group(name: str) -> str:
    """根据东财行业名关键词归类到板块组（先到先得）。"""
    if not name:
        return "其他"
    for grp, kws in _GROUP_KEYWORDS:
        for kw in kws:
            if kw in name:
                return grp
    return "其他"


# ============================================================
# 板块宇宙（运行时由 refresh_em_universe 填充）
#   SW_LEVEL2_MAP: {BKxxxx: (名称, 组名, 组名)}
#   SECTOR_GROUPS: {组名: {"name","description","level2_codes":[...]}}
#   SW_LEVEL1_MAP: 以板块组作为粗粒度一级（兼容旧 level1 视图）
# ============================================================
SW_LEVEL1_MAP = {g: g for g in GROUP_ORDER}
SW_LEVEL1_NAME_MAP = {g: g for g in GROUP_ORDER}
SW_LEVEL2_MAP: dict = {}
SECTOR_GROUPS: dict = {}
SW_LEVEL1_BENCHMARK: dict = {}
SW_LEVEL2_BENCHMARK: dict = {}


def refresh_em_universe(em_map: dict):
    """用东财行业清单（{BKxxxx: 名称}）原地重建板块宇宙。

    原地修改模块级字典，使所有 `from config.sector_map import SW_LEVEL2_MAP`
    的既有引用立即生效（无需重新 import）。
    """
    if not em_map:
        return

    # 1) 二级行业宇宙
    SW_LEVEL2_MAP.clear()
    for code, name in em_map.items():
        grp = classify_em_group(name)
        SW_LEVEL2_MAP[code] = (name, grp, grp)

    # 2) 板块组（确保 GROUP_ORDER 各组都存在，再补自动归类组）
    groups = {}
    for g in GROUP_ORDER:
        groups[g] = {
            "name": g,
            "description": GROUP_DESC.get(g, ""),
            "level2_codes": [],
        }
    for code, (name, grp, _) in SW_LEVEL2_MAP.items():
        if grp not in groups:
            groups[grp] = {
                "name": grp,
                "description": GROUP_DESC.get(grp, ""),
                "level2_codes": [],
            }
        groups[grp]["level2_codes"].append(code)
    SECTOR_GROUPS.clear()
    SECTOR_GROUPS.update(groups)

    # 3) 基准映射（按板块组）
    bench = {}
    for code, (name, grp, _) in SW_LEVEL2_MAP.items():
        bench[code] = GROUP_BENCHMARK.get(grp, "000905.SH")
    SW_LEVEL2_BENCHMARK.clear()
    SW_LEVEL2_BENCHMARK.update(bench)


def get_em_industry_universe() -> dict:
    """返回当前板块宇宙 {code: 名称}（供调试/校验）。"""
    return {code: v[0] for code, v in SW_LEVEL2_MAP.items()}


# ============================================================
# 辅助函数
# ============================================================
def get_sector_name(code: str) -> str:
    """根据代码获取板块名称"""
    if code in SW_LEVEL1_MAP:
        return SW_LEVEL1_MAP[code]
    if code in SW_LEVEL2_MAP:
        return SW_LEVEL2_MAP[code][0]
    return code


def get_sector_benchmark(code: str) -> str:
    """获取板块对应的基准指数"""
    if code in SW_LEVEL2_BENCHMARK:
        return SW_LEVEL2_BENCHMARK[code]
    grp = get_sector_group(code)
    return GROUP_BENCHMARK.get(grp, "000905.SH")  # 默认中证500


def get_sector_group(code: str) -> str:
    """获取板块所属的板块组（大金融/新能源/消费/周期/科技/医药/其他）"""
    for group_name, group_info in SECTOR_GROUPS.items():
        if code in group_info.get("level2_codes", []):
            return group_name
    return "其他"


def get_all_level2_codes() -> list:
    """获取所有行业板块代码（同花顺 881xxx）"""
    return list(SW_LEVEL2_MAP.keys())


def get_all_level1_codes() -> list:
    """获取所有一级（板块组）代码"""
    return list(SW_LEVEL1_MAP.keys())


def _load_cache_file() -> dict:
    """导入时从本地缓存 JSON 加载板块宇宙（无需网络）。

    缓存路径与 data/sector_universe.CACHE_PATH 一致。云端首次实时拉取后会
    写入该文件，此后任意子进程（如 indicators.calc_all）导入本模块即可获得宇宙，
    无需再走网络。
    """
    import json
    import os
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "em_industry_universe.json")
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and d:
                return d
        except Exception:
            pass
    return {}


# 导入即尝试从缓存加载（无需网络），保证子进程也有板块宇宙可用。
refresh_em_universe(_load_cache_file())
