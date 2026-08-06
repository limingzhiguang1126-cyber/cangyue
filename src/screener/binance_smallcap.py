"""币安小市值合约标的筛选器（v2）。

从币安“有合约（USDT-M 永续）+ 现货或 Alpha”的标的中，
筛选市值最小的 N 个。

相比 v1 的核心改进：
1. **新增合约筛选**：标的必须同时满足「币安 USDT 永续合约」+「币安现货或 Binance Alpha」
   （即用户要求的“有合约的，现货或者 alpha 都行”）。
2. **市值更准**：
   - 优先使用 Binance Alpha 官方接口自带的 marketCap / circulatingSupply（权威）。
   - 现货-only 币改用 CoinLore **总供应量(tsupply)** 估算（v1 用 csupply，对 BTTC 等
     小币会差 1000 倍导致市值虚低）；并用币安实时价 × 供应量计算。
   - 同名冲突（CoinLore 匹配到错误同名币）通过“价格交叉验证 + 白名单/黑名单”过滤。
3. **数据源**：
   - 币安现货行情: data-api.binance.vision（国内可达）
   - 币安 USDT-M 永续合约符号: data.binance.vision S3 桶（fapi.binance.com 直连常被墙，
     从每日 K 线目录推断当前合约对）
   - Binance Alpha: www.binance.com/bapi/...（国内不可直连，经 proxy.cors.sh 转发）

用法：
    python -m src.screener.binance_smallcap --top 50
    python -m src.screener.binance_smallcap --top 50 --json
    python -m src.screener.binance_smallcap --top 50 --output data/smallcap_top50.json
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
import urllib3
import xml.etree.ElementTree as ET
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
# 币安公开数据 S3 桶（合约每日 K 线目录 -> 推导合约交易对）
BINANCE_S3_BUCKET = "https://data.binance.vision.s3.amazonaws.com/"
FUTURES_KLINES_PREFIX = "data/futures/um/daily/klines/"
# Binance Alpha token 列表（官方 bapi，国内不可直连，经 CORS 代理转发）
ALPHA_TOKEN_LIST_URL = (
    "https://www.binance.com/bapi/defi/v1/public/"
    "wallet-direct/buw/wallet/cex/alpha/all/token/list"
)
# CORS 代理（用于访问被墙的 www.binance.com bapi）
PROXY_CORS_SH = "https://proxy.cors.sh/"
PROXY_CORS_LOL = "https://api.cors.lol/?url="

# CoinLore 全市场市值排名（免费无需 key）
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

# CoinLore 明确匹配到错误同名币（与币安现货实际币种不符）的黑名单。
# 这些币无法从 CoinLore 获得可靠市值（如 TURBO=Turbo meme 被配成 Turbo Wallet、
# ANC=Anchor Protocol 被配成 Anoncoin）。它们真实市值通常较大，不影响小市值 Top N。
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


# ----------------------------------------------------------------------
# 币安现货
# ----------------------------------------------------------------------
def fetch_binance_spot() -> Dict[str, Dict[str, Any]]:
    """返回币安现货 USDT 报价、TRADING 状态交易对的行情字典。

    key = base asset（去掉 USDT 后缀），value = {price, quote_volume}
    """
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
    logger.info("binance spot USDT pairs: %d", len(result))
    return result


# ----------------------------------------------------------------------
# 币安 USDT-M 永续合约符号（从 S3 每日 K 线目录推断）
# ----------------------------------------------------------------------
def fetch_usdt_futures_symbols() -> List[str]:
    """返回币安 USDT-M 永续合约交易对符号（如 PEPEUSDT、1000WHYUSDT）。

    由于 fapi.binance.com 直连常被墙，改用 data.binance.vision S3 桶的
    data/futures/um/daily/klines/<SYMBOL>/ 目录列表，其存在的 symbol 即
    有 U 本位合约 K 线数据的交易对（含 USDT / USDC / BUSD 计价与季度交割）。
    """
    symbols: List[str] = []
    params = {
        "list-type": "2",
        "prefix": FUTURES_KLINES_PREFIX,
        "delimiter": "/",
        "max-keys": "1000",
    }
    token: Optional[str] = None
    ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
    while True:
        p = dict(params)
        if token:
            p["continuation-token"] = token
        resp = requests.get(BINANCE_S3_BUCKET, params=p, timeout=25, verify=False)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        for cp in root.findall("s3:CommonPrefixes", ns):
            prefix = cp.find("s3:Prefix", ns).text or ""
            sym = prefix.replace(FUTURES_KLINES_PREFIX, "").rstrip("/")
            if sym:
                symbols.append(sym)
        token_el = root.find("s3:NextContinuationToken", ns)
        token = token_el.text if token_el is not None else None
        if not token:
            break
        time.sleep(0.2)

    # 只保留 USDT 计价 + 永续（去掉交割 _yyyymm、BUSD/USDC 计价、SETTLED、纯数字指数）
    result = []
    for s in symbols:
        if not s.endswith("USDT"):
            continue
        if re.search(r"_\d{6}$", s):  # 季度交割合约 BTCUSDT_260925
            continue
        if "SETTLED" in s or "BUSD" in s or "USDC" in s:
            continue
        if s in ("USDTUSDT", "BTCUSD1", "ETHUSD1"):
            continue
        result.append(s)
    logger.info("binance USDT-M perpetual pairs: %d", len(result))
    return result


def normalize_futures_base(base: str) -> set:
    """把合约 base asset 归一化为可能的 token symbol 集合。

    例：1000PEPE -> {1000PEPE, PEPE}；PEPE -> {PEPE}
    """
    out = {base} if base else set()
    # 先处理最长前缀，匹配一次即停，避免 1000PEPE 被 100 前缀再截成 0PEPE
    for prefix in sorted(SCALE_PREFIXES, key=len, reverse=True):
        if base.startswith(prefix) and len(base) > len(prefix):
            out.add(base[len(prefix):])
            break
    return out


# ----------------------------------------------------------------------
# Binance Alpha token 列表
# ----------------------------------------------------------------------
def fetch_alpha_tokens() -> List[Dict[str, Any]]:
    """获取 Binance Alpha 全部 token（含权威 marketCap / circulatingSupply）。

    官方接口 www.binance.com 国内不可直连，尝试多个 CORS 代理。
    返回 token dict 列表，字段含 symbol / name / marketCap / price /
    circulatingSupply / volume24h / chainName / offline / alphaId 等。
    """
    last_err: Optional[Exception] = None
    proxies = [
        (PROXY_CORS_SH, ALPHA_TOKEN_LIST_URL),
        (PROXY_CORS_LOL, ALPHA_TOKEN_LIST_URL),
    ]
    for prefix, url in proxies:
        try:
            if prefix == PROXY_CORS_SH:
                resp = requests.get(
                    f"{prefix}{url}", timeout=30,
                    headers={"User-Agent": "Mozilla/5.0", "Origin": "https://cnb.cool"},
                )
            else:
                resp = requests.get(
                    f"{prefix}{requests.utils.quote(url, safe='')}", timeout=30,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
            if resp.status_code == 429:
                last_err = RuntimeError("CORS proxy rate limited")
                continue
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and "data" in data:
                tokens = data["data"]
                logger.info("alpha tokens fetched: %d", len(tokens))
                return tokens
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            logger.warning("alpha fetch via %s failed: %s", prefix, exc)
        time.sleep(3)
    raise RuntimeError(f"无法获取 Binance Alpha 列表: {last_err}")


# ----------------------------------------------------------------------
# CoinLore 全市场数据
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
    online = [t for t in cands if t.get("offline") is False]
    pool = online if online else cands
    return max(pool, key=lambda t: _to_float(t.get("volume24h")))


def _coinlore_best(sym: str, spot_price: float,
                   coinlore: Dict[str, List[Dict[str, Any]]],
                   ratio_lo: float = 0.3, ratio_hi: float = 3.0) -> Optional[Dict[str, Any]]:
    """在 CoinLore 同名候选中选价格最接近的，返回 (估算市值, coin)。

    估算市值 = 币安实时价 × max(csupply, tsupply)。
    价格 ratio 超出 (ratio_lo, ratio_hi) 视为同名冲突，返回 None。
    """
    cands = coinlore.get(sym, [])
    best, best_ratio, best_supply = None, None, 0.0
    for c in cands:
        cl_price = _to_float(c.get("price_usd"))
        if cl_price <= 0 or spot_price <= 0:
            continue
        ratio = spot_price / cl_price
        ts = _to_float(c.get("tsupply"))
        cs = _to_float(c.get("csupply"))
        supply = max(ts, cs)
        # 优先选价格最接近的；价格相同时选供应量更大（同名冲突多为小盘错配）
        if best_ratio is None:
            best, best_ratio, best_supply = c, ratio, supply
        elif abs(ratio - 1) < abs(best_ratio - 1) - 1e-9:
            best, best_ratio, best_supply = c, ratio, supply
        elif abs(ratio - 1) < abs(best_ratio - 1) + 1e-9 and supply > best_supply:
            best, best_ratio, best_supply = c, ratio, supply
    if best is None or best_ratio is None:
        return None
    if not (ratio_lo < best_ratio < ratio_hi):
        return None
    ts = _to_float(best.get("tsupply"))
    cs = _to_float(best.get("csupply"))
    supply = max(ts, cs)
    if supply <= 0:
        return None
    return {"coin": best, "supply": supply, "est_market_cap": spot_price * supply}


def build_smallcap_list(
    top: int = 50,
    futures: Optional[List[str]] = None,
    spot: Optional[Dict[str, Dict[str, Any]]] = None,
    alpha_tokens: Optional[List[Dict[str, Any]]] = None,
    coinlore: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> List[Dict[str, Any]]:
    """主流程：有 USDT-M 永续合约 且 (币安现货 或 Binance Alpha) 的标的中，按估算市值升序取 Top N。

    市值口径（由可信到次可信）：
    1. Binance Alpha marketCap（若 Alpha 版本与币安现货价格一致，或该币无现货）
    2. CoinLore 估算市值（币安实时价 × max(csupply, tsupply)，价格交叉验证通过）
    """
    if futures is None:
        futures = fetch_usdt_futures_symbols()
    if spot is None:
        spot = fetch_binance_spot()
    if alpha_tokens is None:
        alpha_tokens = fetch_alpha_tokens()
    if coinlore is None:
        coinlore = fetch_coinlore_all()

    # 构建 Alpha 索引（symbol -> 同名候选列表）
    alpha_all: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for t in alpha_tokens:
        alpha_all[t.get("symbol", "").upper()].append(t)

    # 有合约的 base asset 归一化集合
    futures_tokens: set = set()
    for s in futures:
        if s.endswith("USDT"):
            futures_tokens.update(normalize_futures_base(s[:-4]))

    # 候选池：有合约 且 (现货 或 Alpha)
    candidates = {s for s in futures_tokens if s in spot or s in alpha_all}
    logger.info("candidates (futures & (spot|alpha)): %d", len(candidates))

    results: List[Dict[str, Any]] = []
    for sym in candidates:
        in_spot = sym in spot
        in_alpha = sym in alpha_all
        spot_price = spot[sym]["price"] if in_spot else None

        # 1) 优先 Alpha（权威市值）
        a = _pick_alpha(sym, alpha_all, spot_price)
        if a is not None:
            ap = _to_float(a.get("price"))
            amc = _to_float(a.get("marketCap"))
            alpha_ok = False
            if in_spot and ap > 0:
                # 有现货：Alpha 版本价格须与现货一致（同一币）
                alpha_ok = 0.2 < spot_price / ap < 5.0
            elif not in_spot:
                # 仅 Alpha：须仍在 Alpha 交易（offline=False），避免已下架的历史残留币
                alpha_ok = a.get("offline") is False
            if alpha_ok and amc > 0:
                results.append({
                    "symbol": sym,
                    "name": a.get("name", ""),
                    "market_cap": amc,
                    "price": ap,
                    "quote_volume_24h": _to_float(a.get("volume24h")),
                    "circulating_supply": _to_float(a.get("circulatingSupply")),
                    "source": "alpha",
                    "chain": a.get("chainName", ""),
                    "in_spot": in_spot,
                    "in_alpha": True,
                    "alpha_id": a.get("alphaId", ""),
                })
                continue

        # 2) 现货 + CoinLore（补充）
        if in_spot and sym not in UNRELIABLE_COINLORE:
            cl = _coinlore_best(sym, spot_price, coinlore)
            if cl:
                results.append({
                    "symbol": sym,
                    "name": cl["coin"].get("name", ""),
                    "market_cap": cl["est_market_cap"],
                    "price": spot_price,
                    "quote_volume_24h": spot[sym]["quote_volume"],
                    "circulating_supply": cl["supply"],
                    "source": "coinlore",
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
        description="币安小市值合约标的筛选器（有 USDT 永续合约 + 现货/Alpha）"
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

    print(f"{'#':>3} {'symbol':<12} {'market_cap':>16} {'price_usd':>12} {'vol24':>14} {'source':<9} name")
    for i, r in enumerate(result, 1):
        print(
            f"{i:>3} {r['symbol']:<12} {r['market_cap']:>16,.0f} "
            f"{r['price']:>12,.6g} {r['quote_volume_24h']:>14,.0f} "
            f"{r['source']:<9} {r['name'][:24]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
