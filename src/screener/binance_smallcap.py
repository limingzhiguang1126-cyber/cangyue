"""币安现货小市值标的筛选器。

从币安 USDT 现货交易对中筛选市值最小的 N 个标的。
解决 CoinLore 免费接口的 symbol 同名冲突问题：
先用币安 24h 行情价格与 CoinLore 价格做交叉验证，
再剔除稳定币与同名冲突币，最后按估算市值升序取 Top N。

数据源：
- 币安现货行情（交易所唯一可信价格源）: data-api.binance.vision
- CoinLore 全市场市值排名（免费无需 key）: api.coinlore.net

用法：
    python -m src.screener.binance_smallcap --top 50
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

import requests

from ..utils.logger import setup_logging

setup_logging()
logger = logging.getLogger("fin-alert.screener.smallcap")

# 币安行情镜像域名（api.binance.com 在部分地区不可达）
BINANCE_API_BASE = "https://data-api.binance.vision"
COINLORE_API_BASE = "https://api.coinlore.net"

# CoinLore 分页拉取参数
COINLORE_PAGE_SIZE = 100
COINLORE_WORKERS = 8

# 稳定币/法币锚定标的（币安现货常见），按 symbol 排除
STABLE_SYMBOLS = {
    "USDT", "USDC", "FDUSD", "TUSD", "USDP", "BUSD", "DAI", "AEUR", "EUR",
    "EURI", "USDE", "USDY", "RLUSD", "USD1", "XUSD", "FRAX", "SUSD", "GHST",
}

# 同名冲突黑名单：CoinLore 中该 symbol 指向了别的同名币（与币安现货实际币不符），
# 或流通供应量数据缺失/失真导致无法估算市值。动态维护。
KNOWN_MISMATCH = {
    "STX", "HOLO", "WIN", "TURBO", "BNSOL", "POL", "AT", "BB", "SHELL", "U",
    "GENIUS", "PUMP", "HOME", "C", "ZBT", "KITE", "ESP", "KAT", "ERA", "FF",
    "TREE", "SENT", "ACM", "WAL", "ATM", "LUNA", "COMP", "PORTAL", "QUICK",
    "ID", "JUP", "ZKP", "EPIC", "HYPER", "GTC", "SUN", "GRAM", "XPL", "NIGHT",
    "ROBO", "MAV",
}

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; fin-alert/0.1)"}


def _get_json(url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 20) -> Any:
    resp = requests.get(url, params=params, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


# ----------------------------------------------------------------------
# 币安现货
# ----------------------------------------------------------------------
def fetch_binance_usdt_symbols() -> List[str]:
    """返回币安现货 USDT 报价、状态为 TRADING 的 base asset 列表。"""
    data = _get_json(f"{BINANCE_API_BASE}/api/v3/exchangeInfo", timeout=25)
    symbols = [
        s["baseAsset"]
        for s in data.get("symbols", [])
        if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"
    ]
    logger.info("binance USDT TRADING base assets: %d", len(symbols))
    return symbols


def fetch_binance_24h(symbol: str) -> Optional[Dict[str, Any]]:
    """拉取单个币对 24h 行情（价格 / 成交额）。失败返回 None。"""
    try:
        data = _get_json(f"{BINANCE_API_BASE}/api/v3/ticker/24hr", params={"symbol": f"{symbol}USDT"})
        return {
            "last_price": float(data["lastPrice"]),
            "quote_volume": float(data["quoteVolume"]),
        }
    except Exception as exc:  # noqa: BLE001
        logger.debug("binance 24h %s failed: %s", symbol, exc)
        return None


def fetch_binance_all_24h(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """并发拉取全部 24h 行情。"""
    result: Dict[str, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {pool.submit(fetch_binance_24h, s): s for s in symbols}
        for fut in as_completed(futures):
            sym = futures[fut]
            data = fut.result()
            if data:
                result[sym] = data
    logger.info("binance 24h fetched: %d / %d", len(result), len(symbols))
    return result


# ----------------------------------------------------------------------
# CoinLore 全市场数据
# ----------------------------------------------------------------------
def fetch_coinlore_page(start: int) -> List[Dict[str, Any]]:
    """拉取 CoinLore 一页（100 条）。"""
    try:
        data = _get_json(
            f"{COINLORE_API_BASE}/api/tickers/",
            params={"start": start, "limit": COINLORE_PAGE_SIZE},
            timeout=20,
        )
        return data.get("data", [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("coinlore page start=%d failed: %s", start, exc)
        return []


def fetch_coinlore_all() -> Dict[str, Dict[str, Any]]:
    """拉取 CoinLore 全量币种，按 symbol 索引。"""
    first = _get_json(
        f"{COINLORE_API_BASE}/api/tickers/",
        params={"start": 0, "limit": COINLORE_PAGE_SIZE},
        timeout=20,
    )
    total = int(first["info"]["coins_num"])
    pages = (total + COINLORE_PAGE_SIZE - 1) // COINLORE_PAGE_SIZE
    logger.info("coinlore total coins: %d (pages=%d)", total, pages)

    coins: Dict[str, Dict[str, Any]] = {c["symbol"]: c for c in first.get("data", [])}
    with ThreadPoolExecutor(max_workers=COINLORE_WORKERS) as pool:
        futures = {pool.submit(fetch_coinlore_page, i * COINLORE_PAGE_SIZE): i for i in range(1, pages)}
        for fut in as_completed(futures):
            for c in fut.result():
                coins[c["symbol"]] = c
    logger.info("coinlore collected symbols: %d", len(coins))
    return coins


# ----------------------------------------------------------------------
# 交叉验证与筛选
# ----------------------------------------------------------------------
def _is_stable(symbol: str, coin: Dict[str, Any], bn_price: float) -> bool:
    if symbol in STABLE_SYMBOLS:
        return True
    name = (coin.get("name") or "").lower()
    if 0.95 <= bn_price <= 1.05 and any(k in name for k in ("stable", "dollar", "usd", "coin")):
        return True
    return False


def _supply(coin: Dict[str, Any]) -> float:
    """流通供应量（优先 csupply，缺失回退 tsupply）。"""
    for key in ("csupply", "tsupply"):
        try:
            val = float(coin.get(key) or 0)
            if val > 0:
                return val
        except (TypeError, ValueError):
            continue
    return 0.0


def build_smallcap_list(
    top: int = 50,
    min_price_ratio: float = 0.5,
    max_price_ratio: float = 2.0,
) -> List[Dict[str, Any]]:
    """主流程：币安现货 ∩ CoinLore，价格交叉验证后按估算市值升序取 Top N。

    估算市值 = 币安实时价格 × CoinLore 流通供应量。
    同名冲突（CoinLore 价格与币安价格差异过大）的标的会被剔除。
    """
    symbols = fetch_binance_usdt_symbols()
    binance_24h = fetch_binance_all_24h(symbols)
    coinlore = fetch_coinlore_all()

    candidates: List[Dict[str, Any]] = []
    for sym, bn in binance_24h.items():
        coin = coinlore.get(sym)
        if not coin or not coin.get("rank"):
            continue
        if sym in KNOWN_MISMATCH:
            continue
        try:
            cl_price = float(coin["price_usd"])
        except (TypeError, ValueError):
            continue
        bn_price = bn["last_price"]
        if bn_price <= 0 or cl_price <= 0:
            continue
        # 价格交叉验证：防止 CoinLore symbol 匹配到别的同名币
        ratio = bn_price / cl_price
        if not (min_price_ratio < ratio < max_price_ratio):
            logger.debug("skip %s (price ratio %.2f, cl=%s bn=%s)", sym, ratio, cl_price, bn_price)
            continue
        if _is_stable(sym, coin, bn_price):
            continue
        supply = _supply(coin)
        if supply <= 0:
            logger.debug("skip %s (no supply)", sym)
            continue
        candidates.append(
            {
                "symbol": sym,
                "name": coin.get("name", ""),
                "rank": int(coin["rank"]),
                "price_usd": bn_price,
                "quote_volume_24h": bn["quote_volume"],
                "circulating_supply": supply,
                "est_market_cap": bn_price * supply,
            }
        )

    candidates.sort(key=lambda r: r["est_market_cap"])
    logger.info("valid candidates: %d, returning top %d", len(candidates), top)
    return candidates[:top]


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="币安现货小市值标的筛选器")
    parser.add_argument("--top", type=int, default=50, help="返回数量（默认 50）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    parser.add_argument("--output", default="", help="可选：结果写入文件（JSON）")
    args = parser.parse_args(argv)

    result = build_smallcap_list(top=args.top)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        logger.info("result written to %s", args.output)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(f"{'#':>3} {'symbol':<12} {'est_market_cap':>16} {'price_usd':>12} {'vol24':>16}")
    for i, r in enumerate(result, 1):
        print(
            f"{i:>3} {r['symbol']:<12} {r['est_market_cap']:>16,.0f} "
            f"{r['price_usd']:>12,.6g} {r['quote_volume_24h']:>16,.0f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
