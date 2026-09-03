"""One authoritative ordered universe for daily scan and formal stress."""

SYMBOL_NAMES = {
    "300308": "中际旭创",
    "300502": "新易盛",
    "300394": "天孚通信",
    "688256": "寒武纪",
    "603986": "兆易创新",
    "688072": "拓荆科技",
    "688300": "联瑞新材",
    "300054": "鼎龙股份",
    "688361": "中科飞测",
    "002409": "雅克科技",
    "688498": "源杰科技",
    "688120": "华海清科",
    "002384": "东山精密",
    "688082": "盛美上海",
    "300604": "长川科技",
    "601869": "长飞光纤",
    "300408": "三环集团",
}

ORDERED_SYMBOLS = tuple(SYMBOL_NAMES)

VALIDATION_UNIVERSES = {
    "1_symbol": ORDERED_SYMBOLS[:1],
    "3_symbols": ORDERED_SYMBOLS[:3],
    "5_symbols": ORDERED_SYMBOLS[:5],
    "13_symbols": ORDERED_SYMBOLS[:13],
    "17_symbols": ORDERED_SYMBOLS,
}

ESTABLISHED_BASE_CORE = frozenset(ORDERED_SYMBOLS[:5])
ESTABLISHED_EXPANSION_CORE = frozenset(ORDERED_SYMBOLS[:13])
