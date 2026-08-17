"""Daily scan universe and request defaults."""

from quantfusion.config.paths import REGIME_DATA_DIR

SYMBOLS: dict[str, str] = {
    "300308": "中际旭创",
    "300502": "新易盛",
    "300394": "天孚通信",
    "688498": "源杰科技",
    "002281": "光迅科技",
    "601869": "长飞光纤",
    "688008": "澜起科技",
    "603986": "兆易创新",
    "300223": "北京君正",
    "688825": "长鑫科技",
    "688256": "寒武纪",
    "688041": "海光信息",
    "002371": "北方华创",
    "688012": "中微公司",
    "688072": "拓荆科技",
    "688082": "盛美上海",
    "688120": "华海清科",
    "688037": "芯源微",
    "688361": "中科飞测",
    "300604": "长川科技",
    "688019": "安集科技",
    "300054": "鼎龙股份",
    "002409": "雅克科技",
    "300666": "江丰电子",
    "688268": "华特气体",
    "688300": "联瑞新材",
}
START_DATE = "2026-07-01"
INITIAL_CAPITAL = 2_000_000.0
DEFAULT_CACHE_DIR = "data_cache"
DEFAULT_OUTPUT_DIR = "daily_signals"
DEFAULT_REGIME_DATA_DIR = str(REGIME_DATA_DIR)
