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

### 5. 筛选币安小市值标的（可选工具）

```bash
# 输出币安 USDT 现货中市值最小的 50 个标的（表格）
python -m src.screener.binance_smallcap --top 50

# JSON 输出 / 写入文件
python -m src.screener.binance_smallcap --top 50 --json
python -m src.screener.binance_smallcap --top 50 --output data/smallcap_top50.json
```

实现说明：
- 数据源：币安现货 24h 行情（`data-api.binance.vision`，国内可达）+ CoinLore 全市场市值排名（免费无需 key）
- 筛选逻辑：币安 USDT 现货 ∩ CoinLore 有市值数据 → 价格交叉验证剔除同名冲突币（CoinLore symbol 偶发与币安不是同一币）→ 剔除稳定币 → 按估算市值（币安实时价格 × 流通供应量）升序取 Top N
- 同名冲突黑名单见 `src/screener/binance_smallcap.py` 的 `KNOWN_MISMATCH`

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
