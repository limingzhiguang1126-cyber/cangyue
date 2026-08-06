"""币安小市值筛选器测试（使用 mock 数据，不访问真实网络）。"""

from unittest.mock import patch

from src.screener.binance_smallcap import (
    _is_stable,
    _supply,
    build_smallcap_list,
    fetch_binance_usdt_symbols,
)


def _mock_coinlore():
    """构造 CoinLore 数据：10 个币，市值从小到大。"""
    coins = {}
    for i, sym in enumerate(["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "III", "JJJ"], 1):
        coins[sym] = {
            "symbol": sym,
            "name": f"Token {sym}",
            "rank": str(1000 - i * 5),
            "price_usd": "1.0",
            "csupply": str(i * 1000),
            "tsupply": str(i * 1000),
            "market_cap_usd": str(i * 1000),
        }
    # 加一个稳定币（应被剔除）
    coins["USDC"] = {
        "symbol": "USDC",
        "name": "USD Coin",
        "rank": "5",
        "price_usd": "1.0",
        "csupply": "100000",
        "tsupply": "100000",
        "market_cap_usd": "100000",
    }
    return coins


def _mock_exchange_info():
    symbols = [
        {"symbol": f"{s}USDT", "baseAsset": s, "quoteAsset": "USDT", "status": "TRADING"}
        for s in ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "III", "JJJ", "USDC"]
    ]
    # 一个非 USDT 报价的，应被忽略
    symbols.append({"symbol": "ETHBTC", "baseAsset": "ETH", "quoteAsset": "BTC", "status": "TRADING"})
    return {"symbols": symbols}


def _mock_binance_24h(symbol):
    price = {"AAA": "1.0", "BBB": "1.0", "CCC": "1.0", "DDD": "1.0", "EEE": "1.0",
             "FFF": "1.0", "GGG": "1.0", "HHH": "1.0", "III": "1.0", "JJJ": "1.0",
             "USDC": "1.0"}[symbol]
    return {"lastPrice": price, "quoteVolume": "50000"}


def _mock_binance_all_24h(symbols, overrides=None):
    """构造 fetch_binance_all_24h 转换后的返回格式。"""
    overrides = overrides or {}
    result = {}
    for s in symbols:
        base = _mock_binance_24h(s)
        ov = overrides.get(s, {})
        result[s] = {
            "last_price": float(ov.get("lastPrice", base["lastPrice"])),
            "quote_volume": float(ov.get("quoteVolume", base["quoteVolume"])),
        }
    return result


def test_fetch_binance_usdt_symbols():
    with patch("src.screener.binance_smallcap._get_json", return_value=_mock_exchange_info()):
        syms = fetch_binance_usdt_symbols()
    assert "AAA" in syms
    assert "ETH" not in syms  # 非 USDT 报价
    assert len(syms) == 11


def test_is_stable():
    coin = {"name": "USD Coin"}
    assert _is_stable("USDC", coin, 1.0) is True
    assert _is_stable("USDT", {}, 1.0) is True
    assert _is_stable("BTC", {"name": "Bitcoin"}, 64000.0) is False


def test_supply():
    assert _supply({"csupply": "123", "tsupply": "456"}) == 123.0
    assert _supply({"csupply": "0?", "tsupply": "456"}) == 456.0
    assert _supply({"csupply": "0?", "tsupply": "0?"}) == 0.0


def test_build_smallcap_list():
    coinlore = _mock_coinlore()
    symbols = [s["baseAsset"] for s in _mock_exchange_info()["symbols"]
               if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"]
    with patch("src.screener.binance_smallcap.fetch_binance_usdt_symbols", return_value=symbols), \
         patch("src.screener.binance_smallcap.fetch_binance_all_24h",
               return_value=_mock_binance_all_24h(symbols)), \
         patch("src.screener.binance_smallcap.fetch_coinlore_all", return_value=coinlore):
        result = build_smallcap_list(top=5)

    # 10 个普通币（市值 1000~10000），USDC 被剔除
    assert len(result) == 5
    # 按市值升序：AAA(1000) 最小
    assert result[0]["symbol"] == "AAA"
    assert result[0]["est_market_cap"] == 1000.0
    assert all(r["symbol"] != "USDC" for r in result)


def test_build_smallcap_list_price_mismatch():
    """价格交叉验证：币安价与 CoinLore 价差过大时剔除。"""
    coinlore = _mock_coinlore()
    # AAA 在 CoinLore 价格 1.0，但币安实际价格 100.0 -> 应剔除
    symbols = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    with patch("src.screener.binance_smallcap.fetch_binance_usdt_symbols", return_value=symbols), \
         patch("src.screener.binance_smallcap.fetch_binance_all_24h",
               return_value=_mock_binance_all_24h(symbols, overrides={"AAA": {"lastPrice": "100.0"}})), \
         patch("src.screener.binance_smallcap.fetch_coinlore_all", return_value=coinlore):
        result = build_smallcap_list(top=3)

    assert all(r["symbol"] != "AAA" for r in result)
    assert result[0]["symbol"] == "BBB"
