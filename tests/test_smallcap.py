"""币安小市值合约筛选器测试（使用 mock 数据，不访问真实网络）。"""

from src.screener.binance_smallcap import (
    _coinlore_total_supply,
    _pick_alpha,
    _strip_scale_prefix,
    _to_float,
    active_futures_tokens,
    build_smallcap_list,
    normalize_futures_base,
)


# ----------------------------------------------------------------------
# 测试数据
# ----------------------------------------------------------------------
def _mock_futures():
    """合约：AAA/BBB/CCC/DDD 有 TRADING 永续合约；EFF 只有现货无合约；
    BOB 有 SETTLING 显式合约（应拦截缩放映射）；PEPE 只有 1000PEPE 缩放合约。"""
    return {
        "AAAUSDT": {"status": "TRADING", "contractType": "PERPETUAL", "baseAsset": "AAA", "quoteAsset": "USDT"},
        "BBBUSDT": {"status": "TRADING", "contractType": "PERPETUAL", "baseAsset": "BBB", "quoteAsset": "USDT"},
        "CCCUSDT": {"status": "TRADING", "contractType": "PERPETUAL", "baseAsset": "CCC", "quoteAsset": "USDT"},
        "1000CCCUSDT": {"status": "TRADING", "contractType": "PERPETUAL", "baseAsset": "1000CCC", "quoteAsset": "USDT"},
        "DDDUSDT": {"status": "TRADING", "contractType": "PERPETUAL", "baseAsset": "DDD", "quoteAsset": "USDT"},
        "USDXBUSD": {"status": "TRADING", "contractType": "PERPETUAL", "baseAsset": "USDX", "quoteAsset": "BUSD"},
        "AAAUSDT_SETTLING": {"status": "SETTLING", "contractType": "PERPETUAL", "baseAsset": "ZZZ", "quoteAsset": "USDT"},
        "BOBUSDT": {"status": "SETTLING", "contractType": "PERPETUAL", "baseAsset": "BOB", "quoteAsset": "USDT"},
        "1000000BOBUSDT": {"status": "TRADING", "contractType": "PERPETUAL", "baseAsset": "1000000BOB", "quoteAsset": "USDT"},
        "1000PEPEUSDT": {"status": "TRADING", "contractType": "PERPETUAL", "baseAsset": "1000PEPE", "quoteAsset": "USDT"},
        "NVDAUSDT": {"status": "TRADING", "contractType": "TRADIFI_PERPETUAL", "baseAsset": "NVDA", "quoteAsset": "USDT"},
        "42USDT": {"status": "TRADING", "contractType": "PERPETUAL", "baseAsset": "42", "quoteAsset": "USDT"},
    }


def _mock_products():
    """币安现货产品（get-products 的简化）：仅 TRADING 现货，带官方流通供应量 cs。"""
    return {
        "AAA": {"price": 1.0, "quote_volume": 50000.0, "circulating_supply": 1000.0, "status": "TRADING", "name": "Token AAA"},
        "BBB": {"price": 1.0, "quote_volume": 50000.0, "circulating_supply": 5000.0, "status": "TRADING", "name": "Token BBB"},
        "CCC": {"price": 1.0, "quote_volume": 50000.0, "circulating_supply": 3000.0, "status": "TRADING", "name": "Token CCC"},
        "DDD": {"price": 1.0, "quote_volume": 50000.0, "circulating_supply": 5000.0, "status": "TRADING", "name": "Token DDD"},
        "USDC": {"price": 1.0, "quote_volume": 100000.0, "circulating_supply": 100000.0, "status": "TRADING", "name": "USD Coin"},
    }


def _mock_spot_ticker():
    return {
        "AAA": {"price": 1.0, "quote_volume": 50000.0},
        "BBB": {"price": 1.0, "quote_volume": 50000.0},
        "CCC": {"price": 1.0, "quote_volume": 50000.0},
        "DDD": {"price": 1.0, "quote_volume": 50000.0},
        "USDC": {"price": 1.0, "quote_volume": 100000.0},
    }


def _mock_alpha():
    """Alpha token：DDD 有现货也有 Alpha；EEE 同名冲突（价格不同）；ZZZ 仅 Alpha。"""
    return [
        {
            "symbol": "DDD",
            "name": "Token DDD",
            "marketCap": "2500",
            "fdv": "5000",
            "price": "1.0",
            "circulatingSupply": "2500",
            "totalSupply": "5000",
            "volume24h": "100",
            "chainName": "BSC",
            "offline": False,
            "alphaId": "ALPHA_1",
        },
        {
            "symbol": "EEE",
            "name": "Other EEE",
            "marketCap": "999999",
            "fdv": "999999",
            "price": "100.0",
            "circulatingSupply": "9999",
            "totalSupply": "9999",
            "volume24h": "1000",
            "chainName": "BSC",
            "offline": False,
            "alphaId": "ALPHA_2",
        },
        {
            "symbol": "ZZZ",
            "name": "Token ZZZ",
            "marketCap": "2500",
            "fdv": "5000",
            "price": "0.1",
            "circulatingSupply": "25000",
            "totalSupply": "50000",
            "volume24h": "50",
            "chainName": "Solana",
            "offline": False,
            "alphaId": "ALPHA_3",
        },
    ]


def _mock_coinlore():
    """CoinLore 数据：AAA/BBB/CCC 有同名候选（仅用作 FDV 总供应量参考）。

    AAA: tsupply=2000 >= 币安流通 1000 -> FDV 可用
    BBB: tsupply=5000 == 币安流通 5000 -> FDV 可用
    CCC: 同名候选价格偏差大 -> 应被价格交叉验证剔除
    DDD: 有 Alpha，无需 CoinLore
    USDC: 稳定币，应被剔除
    """
    return {
        "AAA": [
            {"symbol": "AAA", "name": "Token AAA", "rank": "10", "price_usd": "1.0",
             "csupply": "1000", "tsupply": "2000"},
        ],
        "BBB": [
            {"symbol": "BBB", "name": "Token BBB", "rank": "20", "price_usd": "1.0",
             "csupply": "5000", "tsupply": "5000"},
        ],
        "CCC": [
            {"symbol": "CCC", "name": "Token CCC wrong", "rank": "30", "price_usd": "0.5",
             "csupply": "3000", "tsupply": "3000"},
        ],
        "DDD": [
            {"symbol": "DDD", "name": "Token DDD", "rank": "40", "price_usd": "0.5",
             "csupply": "5000", "tsupply": "5000"},
        ],
        "USDC": [
            {"symbol": "USDC", "name": "USD Coin", "rank": "5", "price_usd": "1.0",
             "csupply": "100000", "tsupply": "100000"},
        ],
    }


# ----------------------------------------------------------------------
# 单元测试
# ----------------------------------------------------------------------
def test_to_float():
    assert _to_float("123") == 123.0
    assert _to_float("0?") == 0.0
    assert _to_float("abc") == 0.0
    assert _to_float(None) == 0.0


def test_strip_scale_prefix():
    assert _strip_scale_prefix("PEPE") is None
    assert _strip_scale_prefix("1000PEPE") == "PEPE"
    assert _strip_scale_prefix("1000000BOB") == "BOB"
    # 以数字开头但不是缩放前缀的币名，不应被剥
    assert _strip_scale_prefix("0G") is None
    assert _strip_scale_prefix("1INCH") is None
    assert _strip_scale_prefix("1MBABYDOGE") is None
    assert _strip_scale_prefix("42") is None


def test_normalize_futures_base():
    assert normalize_futures_base("PEPE") == {"PEPE"}
    assert normalize_futures_base("1000PEPE") == {"1000PEPE", "PEPE"}
    assert normalize_futures_base("0G") == {"0G"}


def test_active_futures_tokens_only_trading_perpetual():
    """只保留 TRADING + PERPETUAL + USDT 计价的合约；SETTLING / TRADIFI / 非 USDT 计价剔除。"""
    tokens = active_futures_tokens(_mock_futures())
    assert "AAA" in tokens
    assert "DDD" in tokens
    # SETTLING 合约（AAAUSDT_SETTLING 里的 ZZZ）不应进入
    assert "ZZZ" not in tokens
    # TRADIFI_PERPETUAL（NVDA）不是加密永续合约，不应进入
    assert "NVDA" not in tokens
    # 非 USDT 计价（USDX/BUSD）不应进入
    assert "USDX" not in tokens
    # 缩放合约映射：只有 1000PEPEUSDT -> 映射出 PEPE
    assert "PEPE" in tokens
    assert "1000PEPE" in tokens


def test_active_futures_tokens_scaled_collision_blocked():
    """1000000BOBUSDT 是另一个币，且存在显式 BOBUSDT(SETTLING)，
    不得把已下架的 BOB 误当成活跃合约。"""
    tokens = active_futures_tokens(_mock_futures())
    assert "BOB" not in tokens
    assert "1000000BOB" in tokens


def test_coinlore_total_supply_ok():
    """AAA：CoinLore tsupply=2000 >= 币安流通 1000，价格匹配 -> 返回 2000。"""
    ts = _coinlore_total_supply("AAA", 1.0, 1000.0, _mock_coinlore())
    assert ts == 2000.0


def test_coinlore_total_supply_lt_cs():
    """CoinLore tsupply < 币安官方流通量时视为不可靠，返回 None。"""
    coinlore = {
        "AAA": [
            {"symbol": "AAA", "name": "Token AAA", "rank": "10", "price_usd": "1.0",
             "csupply": "1000", "tsupply": "500"},
        ],
    }
    ts = _coinlore_total_supply("AAA", 1.0, 1000.0, coinlore)
    assert ts is None


def test_coinlore_total_supply_price_mismatch():
    """CCC 价格交叉验证不通过（币安价 1.0 vs CoinLore 0.1，ratio=10）返回 None。"""
    coinlore = {
        "CCC": [
            {"symbol": "CCC", "name": "Token CCC wrong", "rank": "30", "price_usd": "0.1",
             "csupply": "3000", "tsupply": "3000"},
        ],
    }
    ts = _coinlore_total_supply("CCC", 1.0, 3000.0, coinlore)
    assert ts is None


def test_pick_alpha_spot_price_match():
    """有现货价时，应选出与现货价格一致的同名候选（同名冲突处理）。"""
    alpha_all = {
        "DDD": [
            {"symbol": "DDD", "name": "Other DDD", "marketCap": "999", "price": "100.0",
             "volume24h": "1000", "offline": False, "alphaId": "X"},
            {"symbol": "DDD", "name": "Token DDD", "marketCap": "2500", "price": "1.0",
             "volume24h": "100", "offline": False, "alphaId": "Y"},
        ]
    }
    picked = _pick_alpha("DDD", alpha_all, 1.0)
    assert picked is not None
    assert picked["name"] == "Token DDD"


def test_pick_alpha_no_spot_picks_highest_volume():
    """无现货时，应取成交量最高的候选（不按 offline 过滤）。"""
    alpha_all = {
        "ZZZ": [
            {"symbol": "ZZZ", "name": "Low vol", "marketCap": "100", "price": "0.1",
             "volume24h": "1", "offline": True, "alphaId": "A"},
            {"symbol": "ZZZ", "name": "High vol", "marketCap": "5000", "price": "0.5",
             "volume24h": "1000", "offline": False, "alphaId": "B"},
        ]
    }
    picked = _pick_alpha("ZZZ", alpha_all, None)
    assert picked is not None
    assert picked["name"] == "High vol"


def test_build_smallcap_list():
    """主流程：TRADING 合约 + (现货 TRADING 或 Alpha)，按流通市值升序，输出 FDV。

    - AAA (现货, 流通 1000) -> 市值 1000, FDV = 2000*1.0
    - BBB (现货, 流通 5000) -> 市值 5000, FDV = 5000*1.0
    - DDD (有现货+Alpha) -> 市值 2500 (Alpha marketCap), FDV 5000
    - CCC (价格交叉不通过) -> 剔除
    - USDC (稳定币) -> 剔除
    - EEE (无合约) -> 剔除
    - ZZZ (仅 Alpha+合约) -> 市值 2500
    """
    coinlore = _mock_coinlore()
    # CCC 价格改成 0.1，使 ratio=10 超出交叉验证区间
    coinlore["CCC"] = [
        {"symbol": "CCC", "name": "Token CCC wrong", "rank": "30", "price_usd": "0.1",
         "csupply": "3000", "tsupply": "3000"},
    ]
    # ZZZ 无现货但只有 Alpha（无合约的 ZZZ 不应入选；这里给 ZZZ 加合约）
    futures = dict(_mock_futures())
    futures["ZZZUSDT"] = {"status": "TRADING", "contractType": "PERPETUAL", "baseAsset": "ZZZ", "quoteAsset": "USDT"}

    result = build_smallcap_list(
        top=10,
        futures=futures,
        products=_mock_products(),
        spot_ticker=_mock_spot_ticker(),
        alpha_tokens=_mock_alpha(),
        coinlore=coinlore,
    )
    by_sym = {r["symbol"]: r for r in result}

    # AAA 1000 < DDD 2500 < BBB 5000；USDC/EEE 均被剔除
    assert result[0]["symbol"] == "AAA"
    assert result[0]["market_cap"] == 1000.0
    assert by_sym["AAA"]["fdv"] == 2000.0  # CoinLore ts 2000 * 1.0
    assert all(r["symbol"] not in ("USDC", "EEE") for r in result)
    # CCC：币安官方流通市值可靠（cs 3000 * 1.0），但 CoinLore 价格不匹配 -> FDV 为 None
    assert by_sym["CCC"]["market_cap"] == 3000.0
    assert by_sym["CCC"]["fdv"] is None
    # DDD 有 Alpha -> 市值用 Alpha marketCap, FDV 用 Alpha fdv
    assert by_sym["DDD"]["market_cap"] == 2500.0
    assert by_sym["DDD"]["fdv"] == 5000.0
    # ZZZ 仅 Alpha+合约 -> 市值 2500
    assert "ZZZ" in by_sym
    assert by_sym["ZZZ"]["market_cap"] == 2500.0
    # BOB 已被缩放拦截，不应出现在结果里（结果里没有 BOB）
    assert "BOB" not in by_sym
    assert "1000000BOB" not in by_sym


def test_build_smallcap_list_futures_filter():
    """无合约的标的（DDD2 只有现货、无合约）不应入选。"""
    products = dict(_mock_products())
    products["DDD2"] = {"price": 0.1, "quote_volume": 1000.0, "circulating_supply": 100.0, "status": "TRADING", "name": "No Fut DDD2"}
    coinlore = dict(_mock_coinlore())
    coinlore["DDD2"] = [
        {"symbol": "DDD2", "name": "No Fut DDD2", "rank": "50", "price_usd": "0.1",
         "csupply": "100", "tsupply": "100"},
    ]
    result = build_smallcap_list(
        top=10,
        futures=_mock_futures(),  # 不含 DDD2
        products=products,
        spot_ticker=_mock_spot_ticker(),
        alpha_tokens=_mock_alpha(),
        coinlore=coinlore,
    )
    assert all(r["symbol"] != "DDD2" for r in result)


def test_build_smallcap_list_settling_excluded():
    """SETTLING 合约（ZZZUSDT 状态改 SETTLING）的标的即使有 Alpha 也不应入选。"""
    futures = dict(_mock_futures())
    futures["ZZZUSDT"] = {"status": "SETTLING", "contractType": "PERPETUAL", "baseAsset": "ZZZ", "quoteAsset": "USDT"}
    result = build_smallcap_list(
        top=10,
        futures=futures,
        products=_mock_products(),
        spot_ticker=_mock_spot_ticker(),
        alpha_tokens=_mock_alpha(),
        coinlore=_mock_coinlore(),
    )
    assert all(r["symbol"] != "ZZZ" for r in result)
