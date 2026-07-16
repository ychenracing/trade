#!/usr/bin/env python3
"""Step 1: 拉取13只标的数据，存pickle"""
import sys, os, time, pickle
import pandas as pd
import akshare as ak

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from a_share_turtle_v11 import normalize_ohlcv_frame

SYMBOLS = {
    "300308.SZ": "中际旭创", "300502.SZ": "新易盛", "300394.SZ": "天孚通信",
    "688008.SH": "澜起科技", "603986.SH": "兆易创新", "002409.SZ": "雅克科技",
    "688072.SH": "拓荆科技", "688110.SH": "联瑞新材", "300054.SZ": "鼎龙股份",
    "688535.SH": "华海诚科", "300776.SZ": "帝尔激光", "688205.SH": "德科立",
    "920045.BJ": "蘅东光",
}
START, END = "2025-01-02", "2026-07-08"

data_map = {}
for code, name in SYMBOLS.items():
    pure = code.split(".")[0]
    if pure.startswith(("0","3")): ak_code = f"sz{pure}"
    elif pure.startswith("6"): ak_code = f"sh{pure}"
    elif pure.startswith("9"): ak_code = f"bj{pure}"
    else: ak_code = f"sh{pure}"
    
    for attempt in range(3):
        try:
            df = ak.stock_zh_a_daily(symbol=ak_code, start_date=START, end_date=END, adjust="qfq")
            if df is not None and not df.empty:
                break
            time.sleep(1)
        except Exception as e:
            if attempt < 2: time.sleep(2)
            else: print(f"FAIL {code} {name}: {e}")
    
    if df is None or df.empty:
        print(f"SKIP {code} {name}: no data")
        continue
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    try:
        df_clean = normalize_ohlcv_frame(df)
        data_map[code] = df_clean
        print(f"OK {code} {name}: {len(df_clean)} bars")
    except Exception as e:
        print(f"FAIL {code} {name}: {e}")

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_cache.pkl"), "wb") as f:
    pickle.dump(data_map, f)
print(f"\nCached {len(data_map)} stocks to data_cache.pkl")
