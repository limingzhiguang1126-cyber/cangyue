# 金融事件实时预警系统 (fin-alert)

7x24 小时运行的金融事件预警系统：实时监控重大政治事件与科技行业新闻，通过 AI 分析对金融标的（美股 + 加密货币）的影响，推送至 Telegram，并在次日早 8 点生成事件影响的实际验证报告。

> 对应 PRD：`fin-alert-prd.md`（Issue #1 附件），本项目按 PRD 第十节目录结构搭建。

## ✨ 核心差异化

1. **美股 + 加密双覆盖**：同时监控美股、加密货币、外汇、大宗商品相关事件
2. **政治事件 → 金融影响分析**：不仅推送新闻，还告诉你影响哪些标的、利多还是利空
3. **次日验证闭环**：次日 8 点自动拉取实际价格，对比预判生成准确率报告

## 🏗️ 目录结构

```
fin-alert/
├── config/config.yaml          # 主配置文件
├── src/
│   ├── main.py                 # 入口，启动所有服务
│   ├── models.py               # 统一消息模型
│   ├── collectors/             # 数据采集器
│   │   ├── base.py             # Collector 基类 + 注册表
│   │   ├── rss.py              # 原生 RSS 采集
│   │   ├── rsshub.py           # RSSHub 源采集
│   │   ├── binance.py          # 币安公告采集
│   │   └── economic_calendar.py# 经济数据日历
│   ├── screener/               # 初筛过滤器（Phase 2）
│   ├── analyzer/               # 深度分析器（Phase 2）
│   ├── notifier/               # Telegram 推送 + 消息格式化
│   ├── storage/                # SQLite 事件存储（Phase 2）
│   ├── verifier/               # 次日验证（Phase 3）
│   └── utils/                  # 配置 / 去重 / 日志
├── data/                       # SQLite 数据库目录
├── docker/                     # Dockerfile + docker-compose（含 RSSHub）
├── tests/                      # 单元测试
├── requirements.txt
├── .env.example
└── README.md
```

## 🚀 快速开始（Phase 1 MVP）

### 1. 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID
```

- 通过 [@BotFather](https://t.me/BotFather) 创建 Bot 获取 token
- 通过 [@userinfobot](https://t.me/userinfobot) 查询你的 chat_id

### 3. 启动（可选：先单次跑通）

```bash
# 单次运行（调试用）：抓取所有源并推送一次
python -m src.main --once

# 常驻运行：APScheduler 按各源 poll_interval 轮询
python -m src.main
```

### 4. （可选）自建 RSSHub

```bash
cd docker && docker compose up -d
```

默认在 `localhost:1200` 起一个 RSSHub 实例，Truth Social / Musk X 等路由依赖它。

### 5. 筛选币安小市值合约标的（可选工具）

```bash
# 输出“有 USDT-M 永续合约 + 现货/Alpha”中市值最小的 50 个标的（表格）
python -m src.screener.binance_smallcap --top 50

# JSON 输出 / 写入文件
python -m src.screener.binance_smallcap --top 50 --json
python -m src.screener.binance_smallcap --top 50 --output data/smallcap_top50.json
```

实现说明（v2）：
- 筛选条件：**有币安 USDT-M 永续合约** 且（**币安现货** 或 **Binance Alpha**）的标的，按市值升序取 Top N
- 数据源：
  - 币安现货 24h 行情：`data-api.binance.vision`（国内可达）
  - 币安 U 本位合约交易对：`data.binance.vision` S3 桶（`fapi.binance.com` 直连常被墙）
  - Binance Alpha 全 token 列表（自带权威 marketCap / circulatingSupply）：`www.binance.com/bapi/...`，经 CORS 代理转发
  - CoinLore 全市场供应量（补充现货-only 币的估算）
- 市值口径（可信到次可信）：
  1. Binance Alpha 官方 marketCap（Alpha 版本与币安现货价格一致，或该币仅在 Alpha）
  2. CoinLore 估算市值 = 币安实时价 × max(csupply, tsupply)，价格交叉验证通过（0.3~3.0）
- 可靠性处理：
  - 同名冲突（CoinLore 匹配到错误同名币）通过价格交叉验证 + 黑名单（`UNRELIABLE_COINLORE`）过滤
  - 仅 Alpha 的币要求 `offline=False`（仍在 Alpha 交易），避免已下架历史残留币
  - 用 tsupply 而非 csupply 估算，避免 BTTC 等小币供应量失真（差 1000 倍）导致市值虚低

### 6. v1.1 实时信号监控 + Telegram 推送（自动化）

在「线 1 候选池」（`data/smallcap_top100_fdv.json`，FDV 最小 Top100）上
按 **v1.1 标准** 每 15 分钟轮询一次，命中信号即推送 Telegram，
并标注命中原因 + 观察/建仓建议。

```bash
# 单次扫描并推送（调试用）
python -m src.screener.v11_signal_daemon --once

# 只打印不推送（dry-run，不依赖 Telegram token）
python -m src.screener.v11_signal_daemon --once --dry-run

# 常驻轮询：默认每 15 分钟一次（Ctrl-C 退出）
python -m src.screener.v11_signal_daemon

# 自定义轮询间隔（分钟）
python -m src.screener.v11_signal_daemon --interval 15
```

**v1.1 标准**（2026-08-07 讨论定稿 + 回测修正）：

| 信号线 | 触发条件 | 动作 |
|---|---|---|
| 🔔 通知线① | 5m 涨幅 ≥ +10%（收盘口径） | 通知 + 进观察；叠加量能≥3x + OI≥1.15x + 费率正常 → 可小仓 |
| 🔔 主信号② | 4h 涨幅 ≥ +30% 且距本波高点回撤 < 20% | OI 同步放大≥1.15x → **可建仓**；否则降级观察 |
| 📡 辅助线③ | 4h 涨幅 3%~10% 且 4h 量能 ≥ 5x | 提前埋伏观察 |
| ⛔ 一票否决 | 资金费率 > +0.3% 或 < -0.1%（v1.1 放宽上限）/ OI 较峰值回落 > 30% | 命中信号也不碰 |

**去重机制**：同标的 + 同信号线在去重窗口内（默认 2h）只推一次，
状态持久化到 `data/v11_signal_state.json`，重启不重复轰炸。

**依赖环境变量**：`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`（见 `.env.example`）。

## 🧪 运行测试

```bash
python -m pytest tests/ -v
```

## 📅 开发分期

| 阶段 | 内容 | 状态 |
|------|------|------|
| **Phase 1** | 数据采集 + 去重 + Telegram 推送（MVP） | ✅ 已完成骨架 |
| **Phase 2** | DeepSeek 初筛 + 深度分析 + SQLite 入库 + 分级推送 | 🚧 骨架已预留 |
| **Phase 3** | 次日 8:00 验证闭环（yfinance + Binance + 报告推送） | 🚧 骨架已预留 |
| **Phase 4** | 准确率反馈优化、watchlist、静默时段、Dashboard | ⏳ 待开发 |

## ⚠️ 关键风险与依赖验证（上线前必须做）

1. **DeepSeek-V4-Flash 连通性**：免费期截至 2026 年底，Phase 1 期间先验证模型是否存在、API 是否可调用。
2. **Truth Social 源**：无官方公开 API，RSSHub 路由易失效，需提前准备备用抓取方案。
3. **Reuters RSS**：公开免费 feed 不稳定，不行先用 AP News 兜底。
4. **Binance API 地域限制**：`api.binance.com` 部分地区受限，确认部署区域可用。
5. **yfinance 偶发失效**：需异常重试 + 备用数据源。

## 🛠️ 部署（Oracle 免费 ARM / Hetzner）

- 方式 A：`python -m src.main` + systemd 服务保活（推荐）
- 方式 B：`docker compose up -d`（含 RSSHub）
- 进程管理：systemd unit 示例见 `docker/fin-alert.service`

## 🔗 相关链接

- 需求 Issue：[qiang26/cangyue#1](https://cnb.cool/qiang26/cangyue/-/issues/1)
