#!/usr/bin/env python3
"""
台股外資買超追蹤器 — Python 後端腳本
資料來源：台灣證券交易所 (TWSE) T86 三大法人買賣超日報
用途：每日盤後自動抓取外資買超資料，篩選 >= 2000 萬台幣個股，輸出 JSON
"""

import json
import time
import logging
import argparse
from datetime import datetime, timedelta
from pathlib import Path

import requests

# ── 設定 ────────────────────────────────────────────────────────────────────
THRESHOLD_TWD = 20_000_000        # 篩選門檻：2000 萬台幣（以買超「張數 × 均價」估算）
TWSE_T86_URL  = "https://www.twse.com.tw/rwd/zh/fund/T86"
OUTPUT_DIR    = Path("./data")
LOG_FORMAT    = "%(asctime)s [%(levelname)s] %(message)s"

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger("twse_tracker")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.twse.com.tw/",
    "Accept-Language": "zh-TW,zh;q=0.9",
}

# 產業分類對照（股票代號前綴 → 類別標籤）
SECTOR_MAP = {
    "11": "cement", "12": "food", "13": "plastic", "14": "textile",
    "15": "electric_mach", "16": "wire", "17": "chemical",
    "18": "glass", "19": "paper", "20": "steel",
    "21": "rubber", "22": "auto", "23": "elec",   # 電子
    "24": "elec", "25": "elec", "26": "elec",
    "27": "elec", "28": "fin",                     # 金融
    "29": "fin", "30": "fin",
    "31": "semi", "32": "elec", "33": "elec",
    "60": "elec", "61": "elec",
}

def code_to_sector(code: str) -> str:
    prefix = code[:2]
    return SECTOR_MAP.get(prefix, "other")


def fetch_t86(date_str: str) -> dict | None:
    """
    從 TWSE 抓取指定日期的三大法人買賣超資料。
    date_str 格式：YYYYMMDD
    回傳原始 JSON dict，或 None（失敗時）。
    """
    params = {
        "response": "json",
        "date": date_str,
        "selectType": "ALL",       # 全部上市股票
    }
    try:
        resp = requests.get(TWSE_T86_URL, params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("stat") == "OK":
            return data
        log.warning("TWSE 回傳非 OK 狀態：%s（日期 %s 可能為非交易日）", data.get("stat"), date_str)
        return None
    except requests.RequestException as e:
        log.error("抓取失敗：%s", e)
        return None


def parse_t86(raw: dict, date_str: str) -> list[dict]:
    """
    解析 T86 原始 JSON，回傳外資買超個股清單。
    T86 欄位順序（fields）：
      0  證券代號
      1  證券名稱
      2  外資及陸資(不含外資自營商)-買進股數
      3  外資及陸資(不含外資自營商)-賣出股數
      4  外資及陸資(不含外資自營商)-買賣超股數
      5  外資自營商-買進股數
      6  外資自營商-賣出股數
      7  外資自營商-買賣超股數
      8  外資及陸資-買進股數  ← 我們用這欄（含自營）
      9  外資及陸資-賣出股數
      10 外資及陸資-買賣超股數
      11 投信-買進股數
      12 投信-賣出股數
      13 投信-買賣超股數
      14 自營商-買進股數（避險）
      15 自營商-賣出股數（避險）
      16 自營商-買賣超股數（避險）
      17 自營商-買進股數（自行）
      18 自營商-賣出股數（自行）
      19 自營商-買賣超股數（自行）
      20 三大法人買賣超股數
    """
    rows = raw.get("data", [])
    results = []

    for row in rows:
        code = row[0].strip()
        name = row[1].strip()

        # 跳過非個股列（如小計、合計）
        if not code.isdigit() or len(code) not in (4, 5, 6):
            continue

        def to_int(s: str) -> int:
            return int(s.replace(",", "").replace(" ", "") or "0")

        foreign_buy    = to_int(row[8])   # 張
        foreign_sell   = to_int(row[9])   # 張
        foreign_net    = to_int(row[10])  # 張（買超為正）

        # ── 金額估算 ────────────────────────────────────────────────────
        # TWSE T86 只提供「張數」，不含股價。
        # 我們用「當日均價」近似計算，預設先標記為 None，
        # fetch_price() 會補上均價；若無法取得則以 50 元估算。
        results.append({
            "code":         code,
            "name":         name,
            "sector":       code_to_sector(code),
            "foreign_buy":  foreign_buy,
            "foreign_sell": foreign_sell,
            "foreign_net":  foreign_net,
            "avg_price":    None,          # 待補
            "buy_twd":      None,          # 待補（元）
            "sell_twd":     None,          # 待補（元）
            "net_twd":      None,          # 待補（元）
            "date":         date_str,
            "updated_at":   datetime.now().isoformat(timespec="seconds"),
        })

    return results


def fetch_price(code: str) -> float | None:
    """
    從 TWSE 收盤行情抓取個股當日均價（成交均價）。
    TWSE STOCK_DAY 回傳當月每日資料，取最後一筆（當日）。
    """
    url = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
    today = datetime.today().strftime("%Y%m%d")
    params = {"response": "json", "date": today, "stockNo": code}
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=10)
        data = resp.json()
        if data.get("stat") != "OK":
            return None
        # 最後一筆 = 當日；欄位 6 = 收盤價
        last_row = data["data"][-1]
        price_str = last_row[6].replace(",", "")
        return float(price_str)
    except Exception:
        return None


def enrich_with_price(stocks: list[dict], batch_delay: float = 0.3) -> list[dict]:
    """
    為每檔個股補上均價，計算買賣超金額（台幣）。
    為避免對 TWSE 造成過大負擔，每次請求間隔 batch_delay 秒。
    """
    log.info("開始補充股價（共 %d 檔）…", len(stocks))
    for i, stock in enumerate(stocks):
        price = fetch_price(stock["code"])
        if price is None:
            price = 50.0   # 無法取得時用保守估算
            log.debug("  %s 無法取得股價，以 50 元估算", stock["code"])

        # 1 張 = 1000 股
        stock["avg_price"] = price
        stock["buy_twd"]   = stock["foreign_buy"]  * 1000 * price
        stock["sell_twd"]  = stock["foreign_sell"] * 1000 * price
        stock["net_twd"]   = stock["foreign_net"]  * 1000 * price

        if (i + 1) % 20 == 0:
            log.info("  已處理 %d / %d 檔…", i + 1, len(stocks))
        time.sleep(batch_delay)

    return stocks


def filter_by_threshold(stocks: list[dict], threshold: int = THRESHOLD_TWD) -> list[dict]:
    """篩選外資買超金額 >= threshold（台幣）的個股。"""
    return [s for s in stocks if s.get("net_twd", 0) >= threshold]


def save_json(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log.info("已儲存：%s", path)


def get_trading_dates(n: int = 5) -> list[str]:
    """回傳最近 n 個交易日的日期（YYYYMMDD），跳過週六、週日。"""
    dates = []
    d = datetime.today()
    while len(dates) < n:
        if d.weekday() < 5:   # 0=Mon … 4=Fri
            dates.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    return dates


# ── 主程式 ──────────────────────────────────────────────────────────────────
def run(date_str: str | None = None, enrich: bool = True) -> None:
    if date_str is None:
        # 預設抓今天（若為非交易日，TWSE 會回傳空資料）
        date_str = datetime.today().strftime("%Y%m%d")

    log.info("═══ 台股外資追蹤器 ═══")
    log.info("目標日期：%s", date_str)
    log.info("篩選門檻：%s 萬元", THRESHOLD_TWD // 10000)

    # 1. 抓原始資料
    raw = fetch_t86(date_str)
    if raw is None:
        log.error("無法取得 %s 資料，程式結束。", date_str)
        return

    # 2. 解析張數
    stocks = parse_t86(raw, date_str)
    log.info("解析完成，共 %d 檔上市股票", len(stocks))

    # 3. 補充均價（可選；約需 1-3 分鐘）
    if enrich:
        stocks = enrich_with_price(stocks)
    else:
        log.warning("跳過股價補充，改用張數過濾（net_shares >= 200 張 ≈ 2000萬）")
        for s in stocks:
            s["net_twd"] = s["foreign_net"] * 1000 * 100   # 假設 100 元均價

    # 4. 篩選
    filtered = filter_by_threshold(stocks)
    filtered.sort(key=lambda x: x["net_twd"], reverse=True)
    log.info("符合條件（≥ %d 萬）：%d 檔", THRESHOLD_TWD // 10000, len(filtered))

    # 5. 組成輸出 JSON（網頁直接讀取）
    output = {
        "meta": {
            "date":         date_str,
            "threshold_twd": THRESHOLD_TWD,
            "total_stocks": len(filtered),
            "total_buy_twd": sum(s["buy_twd"] or 0 for s in filtered),
            "total_net_twd": sum(s["net_twd"] or 0 for s in filtered),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        },
        "stocks": filtered,
    }

    # 6. 儲存
    out_path = OUTPUT_DIR / f"{date_str}.json"
    save_json(output, out_path)

    # 同時輸出 latest.json（網頁預設讀這個）
    save_json(output, OUTPUT_DIR / "latest.json")

    log.info("═══ 完成 ═══")
    log.info("前 5 大買超個股：")
    for s in filtered[:5]:
        net_yi = (s["net_twd"] or 0) / 1e8
        log.info("  %s %s  買超 %.2f 億元", s["code"], s["name"], net_yi)


def run_weekly() -> None:
    """抓本週已過的每個交易日（週一到今天）。"""
    today = datetime.today()
    days_since_monday = today.weekday()   # 0=Mon
    week_dates = []
    for i in range(days_since_monday + 1):
        d = today - timedelta(days=i)
        if d.weekday() < 5:
            week_dates.append(d.strftime("%Y%m%d"))
    week_dates.sort()

    log.info("本週追蹤日期：%s", week_dates)
    for date_str in week_dates:
        run(date_str, enrich=False)   # 加快速度；若要精準金額請改 enrich=True
        time.sleep(2)

    # 合併本週資料
    weekly = {"week": [], "stocks_agg": {}}
    for date_str in week_dates:
        p = OUTPUT_DIR / f"{date_str}.json"
        if not p.exists():
            continue
        day_data = json.loads(p.read_text(encoding="utf-8"))
        weekly["week"].append({
            "date":         date_str,
            "total_net_twd": day_data["meta"]["total_net_twd"],
            "total_stocks":  day_data["meta"]["total_stocks"],
        })
        for s in day_data["stocks"]:
            code = s["code"]
            if code not in weekly["stocks_agg"]:
                weekly["stocks_agg"][code] = {
                    "code": code, "name": s["name"],
                    "sector": s["sector"],
                    "week_net_twd": 0, "days": 0,
                }
            weekly["stocks_agg"][code]["week_net_twd"] += s.get("net_twd", 0)
            weekly["stocks_agg"][code]["days"] += 1

    # 本週累計排行
    top = sorted(
        weekly["stocks_agg"].values(),
        key=lambda x: x["week_net_twd"],
        reverse=True
    )
    weekly["top_week"] = top[:20]
    save_json(weekly, OUTPUT_DIR / "weekly.json")
    log.info("本週累計資料已儲存至 data/weekly.json")


# ── CLI ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="台股外資買超追蹤器")
    parser.add_argument("--date",   help="指定日期 YYYYMMDD（預設今天）")
    parser.add_argument("--weekly", action="store_true", help="抓取本週所有交易日")
    parser.add_argument(
        "--no-enrich", dest="enrich", action="store_false",
        help="跳過股價補充（速度較快但金額為估算值）"
    )
    parser.set_defaults(enrich=True)
    args = parser.parse_args()

    if args.weekly:
        run_weekly()
    else:
        run(date_str=args.date, enrich=args.enrich)
