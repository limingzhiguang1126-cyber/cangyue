"""币安小市值合约筛选器测试（使用 mock 数据，不访问真实网络）。"""

from src.screener.binance_smallcap import (
    _coinlore_best,
    _pick_alpha,
    _to_float,
    build_smallcap_list,
    normalize_futures_base,
)


# ----------------------------------------------------------------------
# 测试数据
# ----------------------------------------------------------------------
def _mock_futures():
    """合约符号：AAA/BBB/CCC/DDD 有合约；USDX 有合约但不是 USDT 计价（应忽略）。"""
    return ["AAAUSDT", "BBBUSDT", "CCCUSDT", "1000CCCUSDT", "DDDUSDT", "USDXBUSD"]


def _mock_spot():
    return {
        "AAA": {"price": 1.0, "quote_volume": 50000.0},
        "BBB": {"price": 1.0, "quote_volume": 50000.0},
        "CCC": {"price": 1.0, "quote_volume": 50000.0},
        "DDD": {"price": 1.0, "quote_volume": 50000.0},
        "USDC": {"price": 1.0, "quote_volume": 100000.0},
    }


def _mock_alpha():
    """Alpha token：DDD 无现货但有 Alpha；EEE 同名冲突（应被价格交叉过滤）。"""
    return [
        {
            "symbol": "DDD",
            "name": "Token DDD",
            "marketCap": "2500",
            "price": "0.5",
            "circulatingSupply": "5000",
            "volume24h": "100",
            "chainName": "BSC",
            "offline": False,
            "alphaId": "ALPHA_1",
        },
        {
            "symbol": "EEE",
            "name": "Other EEE",
            "marketCap": "999999",
            "price": "100.0",
            "circulatingSupply": "9999",
            "volume24h": "1000",
            "chainName": "BSC",
            "offline": False,
            "alphaId": "ALPHA_2",
        },
    ]


def _mock_coinlore():
    """CoinLore 数据：AAA/BBB/CCC 有同名候选。

    AAA: csupply 失真(10)，tsupply 正确(1000) -> 估算市值应使用 tsupply
    BBB: 同名候选 BBB(小供应) 与 BBB2(大供应)，价格一致 -> 应选供应大的
    CCC: 同名候选，价格与币安偏差大 -> 应被价格交叉验证剔除
    USDC: 稳定币，应被剔除
    """
    return {
        "AAA": [
            {"symbol": "AAA", "name": "Token AAA", "rank": "10", "price_usd": "1.0",
             "csupply": "10", "tsupply": "1000"},
        ],
        "BBB": [
            {"symbol": "BBB", "name": "Token BBB small", "rank": "20", "price_usd": "1.0",
             "csupply": "100", "tsupply": "100"},
            {"symbol": "BBB", "name": "Token BBB big", "rank": "21", "price_usd": "1.0",
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


def test_normalize_futures_base():
    assert normalize_futures_base("PEPE") == {"PEPE"}
    assert normalize_futures_base("1000PEPE") == {"1000PEPE", "PEPE"}
    assert normalize_futures_base("1000000BOB") == {"1000000BOB", "BOB"}


def test_coinlore_best_picks_tsupply():
    """AAA 的 csupply 失真，应使用 max(cs,ts) = 1000 估算市值。"""
    res = _coinlore_best("AAA", 1.0, _mock_coinlore())
    assert res is not None
    assert res["supply"] == 1000.0
    assert res["est_market_cap"] == 1000.0


def test_coinlore_best_picks_larger_supply():
    """同名候选价格相同时，选供应量更大的（避免小盘同名错配）。"""
    res = _coinlore_best("BBB", 1.0, _mock_coinlore())
    assert res is not None
    assert res["coin"]["name"] == "Token BBB big"
    assert res["est_market_cap"] == 5000.0


def test_coinlore_best_price_mismatch():
    """价格交叉验证不通过（币安价 1.0 vs CoinLore 0.1，ratio=10 超出 0.3~3.0）应返回 None。"""
    coinlore = {
        "CCC": [
            {"symbol": "CCC", "name": "Token CCC wrong", "rank": "30", "price_usd": "0.1",
             "csupply": "3000", "tsupply": "3000"},
        ],
    }
    res = _coinlore_best("CCC", 1.0, coinlore)
    assert res is None


def test_pick_alpha_offline_only_filtered():
    """仅 Alpha（无现货）且 offline=True 的候选不应被选中。"""
    alpha_all = {
        "ZZZ": [
            {"symbol": "ZZZ", "name": "Offline Z", "marketCap": "100",
             "price": "0.1", "volume24h": "0", "offline": True, "alphaId": "A"},
            {"symbol": "ZZZ", "name": "Active Z", "marketCap": "5000",
             "price": "0.5", "volume24h": "100", "offline": False, "alphaId": "B"},
        ]
    }
    picked = _pick_alpha("ZZZ", alpha_all, None)
    assert picked is not None
    assert picked["offline"] is False
    assert picked["name"] == "Active Z"


def test_build_smallcap_list():
    """主流程：有合约 + 现货/Alpha，按市值升序。

    - AAA (现货, CoinLore 1000) -> 市值 1000
    - BBB (现货, CoinLore 5000) -> 市值 5000
    - DDD (仅 Alpha, marketCap 2500, 有合约) -> 市值 2500
    - CCC (价格交叉不通过 ratio=10) -> 剔除
    - USDC (稳定币) -> 剔除
    - EEE (无合约) -> 剔除
    """
    coinlore = _mock_coinlore()
    # CCC 价格改成 0.1，使 ratio=10 超出交叉验证区间
    coinlore["CCC"] = [
        {"symbol": "CCC", "name": "Token CCC wrong", "rank": "30", "price_usd": "0.1",
         "csupply": "3000", "tsupply": "3000"},
    ]
    # EEE 无合约，不应入选（验证合约过滤）
    result = build_smallcap_list(
        top=4,
        futures=_mock_futures(),
        spot=_mock_spot(),
        alpha_tokens=_mock_alpha(),
        coinlore=coinlore,
    )

    # AAA 1000 < DDD 2500 < BBB 5000；CCC/USDC/EEE 均被剔除
    assert result[0]["symbol"] == "AAA"
    assert result[0]["market_cap"] == 1000.0
    assert all(r["symbol"] not in ("USDC", "CCC", "EEE") for r in result)
    assert any(r["symbol"] == "DDD" for r in result)  # 仅 Alpha+合约


def test_build_smallcap_list_futures_filter():
    """无合约的标的（DDD2 只有现货、无合约）不应入选。"""
    spot = dict(_mock_spot())
    spot["DDD2"] = {"price": 0.1, "quote_volume": 1000.0}
    coinlore = dict(_mock_coinlore())
    coinlore["DDD2"] = [
        {"symbol": "DDD2", "name": "No Fut DDD2", "rank": "50", "price_usd": "0.1",
         "csupply": "100", "tsupply": "100"},
    ]
    result = build_smallcap_list(
        top=10,
        futures=_mock_futures(),  # 不含 DDD2
        spot=spot,
        alpha_tokens=_mock_alpha(),
        coinlore=coinlore,
    )
    assert all(r["symbol"] != "DDD2" for r in result)
