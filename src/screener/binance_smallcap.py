"""币安小市值合约标的筛选器（v3）。

从币安“有 U 本位永续合约（TRADING）+（现货 TRADING 或 Binance Alpha）”的标的中，
筛选市值最小的 N 个，同时输出**流通市值**与 **FDV（完全稀释市值）**。

相比 v2 的核心修复（针对用户反馈：①都是流通市值没标 FDV；②不少标的已被币安下架/暂停）：
1. **上架状态以币安官方 API 为准**：
   - 合约：直接调 `fapi.binance.com/fapi/v1/exchangeInfo`，只保留
     `status=TRADING` + `contractType=PERPETUAL` + `quoteAsset=USDT` 的永续合约。
     上一版用 S3 每日 K 线目录“推导”合约对，会把已 SETTLING/下架的旧合约也算进去
     （如 VIDT/NTRN/CUDIS/42 等 38/50 标的实际已暂停交易，是用户反馈“下架”问题的根因）。
   - 现货：直接调币安 `bapi/asset/v2/public/asset-service/product/get-products`，
     该接口只返回**当前 TRADING** 的现货产品，天然排除 BREAK/下架交易对，
     且自带币安官方**流通供应量 `cs`**（如 WOO=18.6 亿、USTC=55.6 亿，与真实值一致）。
2. **市值口径**：
   - 现货标的：市值 = 币安官方 `cs` × 币安实时价（权威，替代上一版不可靠的 CoinLore）。
   - Alpha 标的：市值 = Binance Alpha 官方 `marketCap`（= Alpha 流通供应量 × 价格）。
   - 同时标注 FDV：Alpha 标的使用 Alpha 官方 `fdv`；现货-only 标的用
     CoinLore 总供应量(tsupply，经价格交叉验证，且 ≥ 币安流通量) × 实时价估算。
3. **数据源（全部为币安官方 API，经 CORS 代理转发——fapi/www.binance.com 在部分网络不可直连）**：
   - 币安合约 exchangeInfo:  fapi.binance.com/fapi/v1/exchangeInfo
   - 币安现货产品(get-products): www.binance.com/bapi/asset/v2/public/asset-service/product/get-products
   - 币安现货 24h 行情:      data-api.binance.vision/api/v3/ticker/24hr
   - Binance Alpha 列表:     www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list
   - CoinLore（仅现货标的的 FDV 总供应量参考）: api.coinlore.net

用法：
    python -m src.screener.binance_smallcap --top 50
    python -m src.screener.binance_smallcap --top 50 --json
    python -m src.screener.binance_smallcap --top 50 --output data/smallcap_top50.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib3
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

import requests

from ..utils.logger import setup_logging

setup_logging()
logger = logging.getLogger("fin-alert.screener.smallcap")

urllib3.disable_warnings()

# ----------------------------------------------------------------------
# 数据源常量
# ----------------------------------------------------------------------
# 币安行情镜像域名（api.binance.com 在部分地区不可达）
BINANCE_API_BASE = "https://data-api.binance.vision"

# 币安官方现货产品接口（自带 TRADING 状态 + 官方流通供应量 cs）
# 注意：fapi.binance.com / www.binance.com 在部分网络不可直连，需经 CORS 代理。
BINANCE_GET_PRODUCTS_URL = (
    "https://www.binance.com/bapi/asset/v2/public/asset-service/product/get-products"
)
BINANCE_FAPI_EXCHANGE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"

# Binance Alpha token 列表（官方 bapi）
ALPHA_TOKEN_LIST_URL = (
    "https://www.binance.com/bapi/defi/v1/public/"
    "wallet-direct/buw/wallet/cex/alpha/all/token/list"
)

# CORS 代理（用于访问被墙的 fapi.binance.com / www.binance.com bapi）
PROXY_CORS_SH = "https://proxy.cors.sh/"
PROXY_CORS_LOL = "https://api.cors.lol/?url="

# CoinLore 全市场市值排名（免费无需 key，仅用作现货标的的 FDV 总供应量参考）
COINLORE_API_BASE = "https://api.coinlore.net"
COINLORE_PAGE_SIZE = 100
COINLORE_WORKERS = 8

# 稳定币/法币锚定标的，按 symbol 排除
STABLE_SYMBOLS = {
    "USDT", "USDC", "FDUSD", "TUSD", "USDP", "BUSD", "DAI", "AEUR", "EUR",
    "EURI", "USDE", "USDY", "RLUSD", "USD1", "XUSD", "FRAX", "SUSD",
}

# 合约中带缩放前缀的 base asset（如 1000PEPE、1000000BOB），归一化时去掉
SCALE_PREFIXES = ("1000000", "100000", "10000", "1000", "100")

# CoinLore 明确匹配到错误同名币（仅影响现货标的的 FDV 总供应量估算）。
# 这些币无法从 CoinLore 获得可靠总供应量（如 TURBO=Turbo meme 被配成 Turbo Wallet、
# ANC=Anchor Protocol 被配成 Anoncoin），其 FDV 将标记为不可靠。
UNRELIABLE_COINLORE = {"TURBO", "ANC"}

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; fin-alert/0.1)"}


def _get_json(url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 25) -> Any:
    resp = requests.get(url, params=params, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _to_float(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
        return v if v == v else default
    except (TypeError, ValueError):
        return default


def _fetch_via_proxy(url: str, timeout: int = 40) -> requests.Response:
    """优先直连，失败则依次走 CORS 代理（cors.sh -> cors.lol）。"""
    _cors_headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        "Origin": "https://cnb.cool",
    }
    attempts = [
        ("direct", url, _HEADERS),
        (PROXY_CORS_SH, f"{PROXY_CORS_SH}{url}", _cors_headers),
        (PROXY_CORS_LOL, f"{PROXY_CORS_LOL}{requests.utils.quote(url, safe='')}", _cors_headers),
    ]
    last_err: Optional[Exception] = None
    for name, u, headers in attempts:
        try:
            resp = requests.get(u, timeout=timeout, headers=headers, verify=False)
            if resp.status_code == 200:
                return resp
            if resp.status_code == 429:
                last_err = RuntimeError(f"{name} rate limited")
                continue
            last_err = RuntimeError(f"{name} HTTP {resp.status_code}")
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            logger.warning("fetch via %s failed: %s", name, exc)
        time.sleep(2)
    raise RuntimeError(f"无法访问 {url}: {last_err}")


# ----------------------------------------------------------------------
# 币安 USDT-M 永续合约（fapi exchangeInfo，权威 TRADING 状态）
# ----------------------------------------------------------------------
def fetch_futures_exchange_info() -> Dict[str, Dict[str, Any]]:
    """返回币安 U 本位合约全部交易对信息。

    key = 合约 symbol（如 PEPEUSDT / 1000PEPEUSDT / 42USDT），
    value = {status, contractType, baseAsset, quoteAsset}。
    数据源：fapi.binance.com/fapi/v1/exchangeInfo（权威，含 SETTLING/PENDING_TRADING 状态）。
    """
    resp = _fetch_via_proxy(BINANCE_FAPI_EXCHANGE_INFO, timeout=60)
    data = resp.json()
    out: Dict[str, Dict[str, Any]] = {}
    for s in data.get("symbols", []):
        out[s["symbol"]] = {
            "status": s.get("status", ""),
            "contractType": s.get("contractType", ""),
            "baseAsset": s.get("baseAsset", ""),
            "quoteAsset": s.get("quoteAsset", ""),
        }
    logger.info("binance futures contracts: %d", len(out))
    return out


def active_futures_tokens(futures: Dict[str, Dict[str, Any]]) -> set:
    """提取当前 TRADING 的 USDT 永续合约 base asset（归一化缩放前缀后）。

    规则：
    - 保留每个 TRADING 合约自身的 base asset（如 1000PEPE / 1MBABYDOGE / 0G / 42）。
    - 若 base asset 带缩放前缀（1000/10000/100000/1000000），且**不存在**同名非缩放合约
      （如 BOBUSDT 存在时，1000000BOB 是另一个币，不得映射回 BOB），则额外映射回非缩放名。
      例：只有 1000PEPEUSDT -> 映射到 PEPE；而 1000000BOBUSDT + 显式 BOBUSDT(SETTLING)
      同时存在 -> 不映射到 BOB（避免把已下架/结算中的 BOB 误当活跃）。
    """
    active = set()
    # 第一遍：收集所有 TRADING USDT 永续合约的 base asset
    for sym, meta in futures.items():
        if (
            sym.endswith("USDT")
            and meta.get("status") == "TRADING"
            and meta.get("contractType") == "PERPETUAL"
            and meta.get("quoteAsset") == "USDT"
        ):
            active.add(sym[:-4])

    # 判断是否存在“同名非缩放合约”（无论状态），用于拦截缩放映射
    all_bases = {s[:-4] for s in futures if s.endswith("USDT")}
    tokens = set(active)
    for base in active:
        un = _strip_scale_prefix(base)
        if un and un not in all_bases:
            tokens.add(un)
    return tokens


def _strip_scale_prefix(base: str) -> Optional[str]:
    """去掉缩放前缀，返回非缩放 base（如 1000PEPE -> PEPE）。

    仅当命中 SCALE_PREFIXES 且剩余部分非空时才返回；否则返回 None
    （如 0G / 1INCH / 1MBABYDOGE / 42 等本身以数字开头的币名不会被误剥）。
    """
    for prefix in sorted(SCALE_PREFIXES, key=len, reverse=True):
        if base.startswith(prefix) and len(base) > len(prefix):
            return base[len(prefix):]
    return None


def normalize_futures_base(base: str) -> set:
    """把合约 base asset 归一化为可能的 token symbol 集合（兼容旧接口，测试用）。

    例：1000PEPE -> {1000PEPE, PEPE}；PEPE -> {PEPE}
    注：业务上请使用 active_futures_tokens（含“同名非缩放合约拦截”），
    避免 1000000BOB 被误映射为已下架的 BOB。
    """
    out = {base} if base else set()
    un = _strip_scale_prefix(base)
    if un:
        out.add(un)
    return out


# ----------------------------------------------------------------------
# 币安现货产品（get-products，权威 TRADING 状态 + 官方流通供应量 cs）
# ----------------------------------------------------------------------
def fetch_binance_products() -> Dict[str, Dict[str, Any]]:
    """获取币安现货产品列表。

    返回 key = base asset（仅 USDT 计价、当前 TRADING 的产品），
    value = {price, quote_volume, circulating_supply, status}。
    该接口只返回当前可交易（TRADING）的产品，天然排除 BREAK/已下架交易对，
    且 `cs` 为币安官方流通供应量（如 WOO=1,862,426,473）。
    """
    resp = _fetch_via_proxy(BINANCE_GET_PRODUCTS_URL, timeout=60)
    data = resp.json().get("data", [])
    result: Dict[str, Dict[str, Any]] = {}
    for p in data:
        if p.get("q") != "USDT":
            continue
        base = p.get("b")
        if not base:
            continue
        status = p.get("st", "")
        if status != "TRADING":
            continue
        result[base] = {
            "price": _to_float(p.get("c")),
            "quote_volume": _to_float(p.get("qv")),
            "circulating_supply": _to_float(p.get("cs")),
            "status": status,
            "name": p.get("adn") or p.get("an") or "",
        }
    logger.info("binance spot TRADING USDT products: %d", len(result))
    return result


def fetch_spot_ticker() -> Dict[str, Dict[str, Any]]:
    """币安现货 24h 行情（data-api.binance.vision，价格/成交额）。"""
    data = _get_json(f"{BINANCE_API_BASE}/api/v3/ticker/24hr", timeout=30)
    result: Dict[str, Dict[str, Any]] = {}
    for t in data:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        if base == "USDT":
            continue
        price = _to_float(t.get("lastPrice"))
        vol = _to_float(t.get("quoteVolume"))
        if price > 0:
            result[base] = {"price": price, "quote_volume": vol}
    logger.info("binance spot 24h ticker USDT pairs: %d", len(result))
    return result


# ----------------------------------------------------------------------
# Binance Alpha token 列表
# ----------------------------------------------------------------------
def fetch_alpha_tokens() -> List[Dict[str, Any]]:
    """获取 Binance Alpha 全部 token（含官方 marketCap / fdv / 供应量）。

    官方接口 www.binance.com 国内不可直连，尝试直连与多个 CORS 代理。
    返回 token dict 列表，字段含 symbol / name / marketCap / fdv / price /
    circulatingSupply / totalSupply / volume24h / chainName / alphaId 等。
    """
    resp = _fetch_via_proxy(ALPHA_TOKEN_LIST_URL, timeout=60)
    data = resp.json()
    if isinstance(data, dict) and "data" in data:
        tokens = data["data"]
        logger.info("alpha tokens fetched: %d", len(tokens))
        return tokens
    raise RuntimeError(f"无法解析 Binance Alpha 列表: {str(data)[:200]}")


# ----------------------------------------------------------------------
# CoinLore 全市场数据（仅用作现货标的 FDV 的总供应量参考）
# ----------------------------------------------------------------------
def fetch_coinlore_page(start: int) -> List[Dict[str, Any]]:
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


def fetch_coinlore_all() -> Dict[str, List[Dict[str, Any]]]:
    """拉取 CoinLore 全量币种，按 symbol 索引（保留同名候选）。"""
    first = _get_json(
        f"{COINLORE_API_BASE}/api/tickers/",
        params={"start": 0, "limit": COINLORE_PAGE_SIZE},
        timeout=20,
    )
    total = int(first["info"]["coins_num"])
    pages = (total + COINLORE_PAGE_SIZE - 1) // COINLORE_PAGE_SIZE
    logger.info("coinlore total coins: %d (pages=%d)", total, pages)

    coins: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for c in first.get("data", []):
        coins[c["symbol"].upper()].append(c)
    with ThreadPoolExecutor(max_workers=COINLORE_WORKERS) as pool:
        futures = {pool.submit(fetch_coinlore_page, i * COINLORE_PAGE_SIZE): i for i in range(1, pages)}
        for fut in as_completed(futures):
            for c in fut.result():
                coins[c["symbol"].upper()].append(c)
    logger.info("coinlore collected symbols: %d", len(coins))
    return coins


# ----------------------------------------------------------------------
# 数据组装与筛选
# ----------------------------------------------------------------------
def _pick_alpha(sym: str, alpha_all: Dict[str, List[Dict[str, Any]]],
                spot_price: Optional[float]) -> Optional[Dict[str, Any]]:
    """在 Alpha 同名候选中选出与币安现货一致的版本。

    - 若有现货价：选价格与现货最接近的候选（可能是 offline 旧 Alpha 币，
      但价格对得上说明就是当前币安现货对应的那个 token）。
    - 若无现货：选 offline=False 且 24h 成交量最高的候选。
    """
    cands = alpha_all.get(sym, [])
    if not cands:
        return None
    if spot_price and spot_price > 0:
        best, best_ratio = None, None
        for t in cands:
            ap = _to_float(t.get("price"))
            if ap <= 0:
                continue
            ratio = spot_price / ap
            if best_ratio is None or abs(ratio - 1) < abs(best_ratio - 1):
                best, best_ratio = t, ratio
        return best
    # 注意：Alpha 的 offline / fullyDelisted 仅表示"已从 Alpha 计划下架"，
    # 并不代表币安合约下架（如 H/PLAY/AIO 的 alpha offline=True 但仍有活跃 TRADING 永续合约）。
    # 因此无现货候选时不按 offline 过滤，直接取成交量最高的同名候选；
    # 上架状态由调用方的 TRADING 永续合约条件严格把关。
    return max(cands, key=lambda t: _to_float(t.get("volume24h")))


def _coinlore_total_supply(
    sym: str, spot_price: float, cs: float,
    coinlore: Dict[str, List[Dict[str, Any]]],
    ratio_lo: float = 0.3, ratio_hi: float = 3.0,
) -> Optional[float]:
    """在 CoinLore 同名候选中选价格最接近的，返回其总供应量（用于 FDV 估算）。

    校验：
    - 价格 ratio 需落在 (ratio_lo, ratio_hi)，否则视为同名冲突返回 None；
    - 总供应量须 ≥ 币安官方流通量 cs（总供应不可能小于流通量），否则视为数据不可靠返回 None。
    """
    cands = coinlore.get(sym, [])
    best, best_ratio, best_ts = None, None, 0.0
    for c in cands:
        cl_price = _to_float(c.get("price_usd"))
        if cl_price <= 0 or spot_price <= 0:
            continue
        ratio = spot_price / cl_price
        ts = _to_float(c.get("tsupply"))
        if best_ratio is None:
            best, best_ratio, best_ts = c, ratio, ts
        elif abs(ratio - 1) < abs(best_ratio - 1) - 1e-9:
            best, best_ratio, best_ts = c, ratio, ts
        elif abs(ratio - 1) < abs(best_ratio - 1) + 1e-9 and ts > best_ts:
            best, best_ratio, best_ts = c, ratio, ts
    if best is None or best_ratio is None:
        return None
    if not (ratio_lo < best_ratio < ratio_hi):
        return None
    ts = _to_float(best.get("tsupply"))
    if ts <= 0 or ts < cs:
        return None
    return ts


def build_smallcap_list(
    top: int = 50,
    futures: Optional[Dict[str, Dict[str, Any]]] = None,
    products: Optional[Dict[str, Dict[str, Any]]] = None,
    spot_ticker: Optional[Dict[str, Dict[str, Any]]] = None,
    alpha_tokens: Optional[List[Dict[str, Any]]] = None,
    coinlore: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> List[Dict[str, Any]]:
    """主流程：当前 TRADING 的 USDT 永续合约 且 (现货 TRADING 或 Binance Alpha) 的标的中，
    按流通市值升序取 Top N，同时输出 FDV。

    市值口径：
    - 现货标的：市值 = 币安官方 `cs` × 币安实时价
    - Alpha 标的：市值 = Binance Alpha 官方 `marketCap`
    FDV 口径：
    - Alpha 标的：Binance Alpha 官方 `fdv`
    - 现货-only 标的：CoinLore 总供应量(≥币安流通量，价格交叉验证通过) × 实时价
    """
    if futures is None:
        futures = fetch_futures_exchange_info()
    if products is None:
        products = fetch_binance_products()
    if spot_ticker is None:
        spot_ticker = fetch_spot_ticker()
    if alpha_tokens is None:
        alpha_tokens = fetch_alpha_tokens()
    if coinlore is None:
        coinlore = fetch_coinlore_all()

    # 当前 TRADING 的永续合约 base asset（归一化后）
    futures_tokens = active_futures_tokens(futures)
    logger.info("active TRADING USDT perpetual base tokens: %d", len(futures_tokens))

    # 当前 TRADING 的现货产品 base
    spot_active = set(products)

    # Alpha 索引（symbol -> 同名候选列表）
    alpha_all: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for t in alpha_tokens:
        alpha_all[t.get("symbol", "").upper()].append(t)

    # 候选池：有 TRADING 合约 且 (现货 TRADING 或 Alpha)
    candidates = {s for s in futures_tokens if s in spot_active or s in alpha_all}
    logger.info("candidates (TRADING futures & (spot|alpha)): %d", len(candidates))

    results: List[Dict[str, Any]] = []
    for sym in candidates:
        if sym in STABLE_SYMBOLS:
            continue
        in_spot = sym in spot_active
        in_alpha = sym in alpha_all
        spot_info = products.get(sym)
        ticker = spot_ticker.get(sym)
        spot_price = (ticker or {}).get("price") or (spot_info or {}).get("price") or 0.0
        spot_vol = (ticker or {}).get("quote_volume") or (spot_info or {}).get("quote_volume") or 0.0
        spot_cs = (spot_info or {}).get("circulating_supply") or 0.0

        a = _pick_alpha(sym, alpha_all, spot_price or None)
        alpha_ok = False
        if a is not None:
            ap = _to_float(a.get("price"))
            amc = _to_float(a.get("marketCap"))
            if in_spot and ap > 0 and spot_price > 0:
                # 有现货：Alpha 版本价格须与现货一致（同一币）
                alpha_ok = 0.5 < spot_price / ap < 2.0
            else:
                # 仅 Alpha：不按 alpha 的 offline/fullyDelisted 过滤（那只表示 Alpha 计划状态），
                # 上架状态已由调用方 TRADING 永续合约条件严格把关。
                alpha_ok = amc > 0
            if alpha_ok and amc > 0:
                price = spot_price if in_spot and spot_price > 0 else ap
                vol24 = spot_vol if in_spot and spot_vol > 0 else _to_float(a.get("volume24h"))
                results.append({
                    "symbol": sym,
                    "name": a.get("name", ""),
                    "market_cap": amc,
                    "fdv": _to_float(a.get("fdv")),
                    "price": price,
                    "quote_volume_24h": vol24,
                    "circulating_supply": _to_float(a.get("circulatingSupply")),
                    "total_supply": _to_float(a.get("totalSupply")),
                    "source": "alpha",
                    "chain": a.get("chainName", ""),
                    "in_spot": in_spot,
                    "in_alpha": True,
                    "alpha_id": a.get("alphaId", ""),
                })
                continue

        # 现货（币安官方 cs）+ CoinLore 总供应量估算 FDV
        if in_spot and spot_cs > 0 and spot_price > 0:
            mc = spot_cs * spot_price
            fdv: Optional[float] = None
            total_supply: Optional[float] = None
            if sym not in UNRELIABLE_COINLORE:
                ts = _coinlore_total_supply(sym, spot_price, spot_cs, coinlore)
                if ts:
                    fdv = ts * spot_price
                    total_supply = ts
            results.append({
                "symbol": sym,
                "name": (spot_info or {}).get("name", ""),
                "market_cap": mc,
                "fdv": fdv,
                "price": spot_price,
                "quote_volume_24h": spot_vol,
                "circulating_supply": spot_cs,
                "total_supply": total_supply,
                "source": "spot",
                "chain": "",
                "in_spot": True,
                "in_alpha": in_alpha,
                "alpha_id": "",
            })

    results.sort(key=lambda r: r["market_cap"])
    logger.info("valid candidates: %d, returning top %d", len(results), top)
    return results[:top]


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="币安小市值合约标的筛选器（有 TRADING USDT 永续合约 + 现货/Alpha，输出市值与 FDV）"
    )
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

    print(f"{'#':>3} {'symbol':<12} {'market_cap':>16} {'fdv':>16} {'price_usd':>12} {'vol24':>14} {'source':<7} name")
    for i, r in enumerate(result, 1):
        fdv_s = f"{r['fdv']:,.0f}" if r.get("fdv") else "N/A"
        print(
            f"{i:>3} {r['symbol']:<12} {r['market_cap']:>16,.0f} {fdv_s:>16} "
            f"{r['price']:>12,.6g} {r['quote_volume_24h']:>14,.0f} "
            f"{r['source']:<7} {r['name'][:24]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
