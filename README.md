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

## ☁️ 云端部署（免费 · 无需自购服务器）

不想本地跑 / 不想买服务器？直接用 **CNB 云原生构建的定时任务**即可：
代码跑在 CNB 云端构建机上，由平台调度，**免费稳定**，无需 7x24 常驻进程。

### 已配置的云端任务（`.cnb.yml`）

| 任务 | 频率 | 说明 |
|---|---|---|
| `refresh-pool` | 每天 07:35 | 刷新 `data/smallcap_top100_fdv.json`（FDV 最小 Top100）并自动提交 |
| `v11-signal-scan` | 每 15 分钟 | 扫描候选池，命中 v1.1 信号推送 Telegram，并把去重状态回推仓库 |

### 网页手动触发（Web Trigger）

在仓库 **代码 → 分支详情页** 右上角有 **「🔔 手动触发」** 按钮组，点击即可手动触发流水线，**无需等 15 分钟定时任务**：

| 按钮 | 作用 | 可调参数 |
|---|---|---|
| **立即扫描信号** | 手动跑一轮 v1.1 信号扫描，命中即推送 Telegram（默认按钮） | ① 扫描数量：Top 20 / 50 / 100 / 200；② 只扫描不推送（dry-run 调试开关） |
| **立即刷新候选池** | 重新抓取币安数据，刷新 FDV Top100 候选池并自动提交 | — |
| **📨 发送测试消息** | 向你的 Telegram 发一条测试消息，验证推送链路是否打通（同时诊断云端能否访问 api.telegram.org） | — |

- 配置：`.cnb/web_trigger.yml`（按钮定义）+ `.cnb.yml` 的 `$` 兜底分支下 `web_trigger_*` 流水线
- 权限：有仓库写权限即可点击；如需限制可在 `web_trigger.yml` 的 `permissions` 中指定用户/角色
- 开启「只扫描不推送」时不需要 Telegram 密钥，适合随手点一下看当前命中情况（仅打印日志）

> 手动触发与定时任务共用同一份候选池和去重状态，互不冲突。

### 启用步骤（约 3 分钟）

1. **创建密钥仓库**（存 Telegram 凭据，绝不写进公开仓库）
   - 打开 [https://cnb.cool/new/repos](https://cnb.cool/new/repos)，仓库类型选 **`密钥仓库`**，名称如 `cangyue-secrets`
   - 新建文件 `telegram.yml`，内容：
     ```yaml
     TELEGRAM_BOT_TOKEN: "你的 bot token"
     TELEGRAM_CHAT_ID: "你的 chat_id"
     ```
2. **修改 `.cnb.yml`**：把两处 `imports` 里的
   `https://cnb.cool/qiang26/cangyue-secrets/-/blob/main/telegram.yml`
   替换成你自己的密钥仓库路径后合并到 `main`。
3. **确认设置**：仓库 `设置 → 云原生构建` 中「允许定时任务自动触发」已开启
   （本仓库默认已开启，见 `cnb git-settings get-pipeline-settings`）。
4. 定时任务配置合并后即生效，无需任何常驻进程。

### 本地调试

```bash
# 单轮扫描 + 推送（不常驻，等价于云端每次拉起）
python scripts/v11_cloud_run.py --top 100 --push-state

# 只打印不推送
TELEGRAM_BOT_TOKEN= TELEGRAM_CHAT_ID= python scripts/v11_cloud_run.py --top 10
```

> ⚠️ 说明：CNB 定时任务最小间隔为 5 分钟；此处按 v1.1 标准设计为 15 分钟轮询。
> 状态持久化依赖流水线把 `data/v11_signal_state.json` 推回仓库；
> 若回推失败（如并发冲突）仅影响「去重」，不影响「推送」，属优雅降级。

## 🔗 相关链接

- 需求 Issue：[qiang26/cangyue#1](https://cnb.cool/qiang26/cangyue/-/issues/1)

## ⚠️ 电报推送网络问题（重要）

> **CNB 云原生构建机位于国内网络，直连 `api.telegram.org` 大概率超时（被墙）**，
> 因此云端定时任务虽然扫描正常，但推送 Telegram 可能失败。这是网络环境限制，不是代码问题。

### 如何确认能否收到推送

在**你自己的电脑/服务器**（能访问 Telegram 的网络，或已开代理）上运行一键测试：

```bash
# 方式一：配好 .env 后直接跑
python scripts/tg_test_push.py

# 方式二：环境变量直接传参（不写文件）
TELEGRAM_BOT_TOKEN=123456:ABC... TELEGRAM_CHAT_ID=654321 python scripts/tg_test_push.py

# 方式三：参数指定
python scripts/tg_test_push.py --token 123456:ABC... --chat 654321
```

- 看到 `✓ 测试消息发送成功！` 且 Telegram 收到消息 → **Token / chat_id / 推送链路全部正常**
- 若报 `无法访问 api.telegram.org` → 当前网络连不上 Telegram，需开代理或用海外代理地址

### 云端（CNB）如何打通推送

由于 CNB 构建机无法直连 `api.telegram.org`，需要**让 Telegram API 走一条国内可达的通道**：

**方案 A：海外 Telegram Bot API 代理（推荐，改动最小）**

1. 在能访问 Telegram 的海外服务器/免费 PaaS（如 Render/Railway/Fly）上部署一个 Telegram Bot API 代理
2. 在密钥仓库 `telegram.yml` 里加一行：
   ```yaml
   TELEGRAM_API_BASE: "https://你的代理域名"
   ```
3. 云端定时任务会自动读取该变量，推送走代理到达 Telegram

> 仓库代码已支持 `TELEGRAM_API_BASE`（见 `src/notifier/telegram_notifier.py`），
> 一键测试脚本 `scripts/tg_test_push_cloud.py` 也会优先用它。

**方案 B：海外自托管 Runner**

将 CNB 构建任务调度到一台能访问 Telegram 的海外自托管构建机上执行（见 CNB 文档「自定义构建机」），
直连 `api.telegram.org` 即可。

**方案 C：本地常驻守护进程**

在自己能访问 Telegram 的电脑/服务器上常驻运行 `v11_signal_daemon`（每 15 分钟轮询 + 推送），
不依赖 CNB 云端网络。

**方案 D：Hugging Face Space（海外免费托管，推荐）**

把整个信号监控部署到 Hugging Face Space（海外免费服务器），它位于海外网络，**可直连 `api.telegram.org`**，
天然解决被墙问题，无需自购服务器、无需长开本地电脑。

> 🚀 最省事的方式：在 CNB 仓库「代码 → 分支详情页」点 **「🚀 部署 HF Space」** 按钮，
> 只需在密钥仓库 `cangyue-secrets/telegram.yml` 里加一行 `HF_TOKEN: "hf_xxx"`，
> 平台会自动创建 Space、上传代码、并把 Telegram 凭据写入 Space secrets，无需在 HF 网页上手动操作。

### 方式一：CNB 网页按钮（推荐，只需一个 HF Token）

1. 打开 https://huggingface.co/settings/tokens 新建一个 **Read/write** 权限的 token（`hf_...`）
2. 在密钥仓库 `qiang26/cangyue-secrets` 的 `telegram.yml` 里加一行：
   ```yaml
   TELEGRAM_BOT_TOKEN: "你的 bot token"
   TELEGRAM_CHAT_ID: "你的 chat_id"
   HF_TOKEN: "hf_你的HF令牌"
   ```
3. 回到本仓库 **代码 → 分支详情页**，点右上角 **「🔔 手动触发 → 🚀 部署 HF Space」**
4. 等 3~5 分钟，构建日志出现 `✅ 部署完成` 即成功，HF 会自动开始构建
5. 打开 `https://huggingface.co/spaces/{你的用户名}/fin-alert` 查看状态

### 方式二：本地命令行一键部署

```bash
# 1) 安装部署依赖
pip install huggingface_hub>=0.23

# 2) 配置 HF 凭据
#    HF_TOKEN: https://huggingface.co/settings/tokens 新建（需 write 权限）
export HF_TOKEN=hf_xxx

# 3) 一键部署（首次自动创建 Space + 上传全部文件 + 写入 Telegram secrets）
python scripts/deploy_hf.py --space-name fin-alert --deploy \
    --tg-token 你的bot_token --tg-chat 你的chat_id

# 不想在命令行传 Telegram 凭据也可以：HF 用户名可自动探测，之后在 Space 设置里手动填
```

### 运行逻辑

- `app.py` 启动后：
  1. **自动刷新候选池**（从本 CNB 仓库拉最新 `data/smallcap_top100_fdv.json`，默认每 24h 一次）
  2. 后台守护线程每 `HF_POLL_INTERVAL`（默认 15）分钟跑一轮 **v1.2** 信号扫描（FDV Top100 候选池）
  3. 命中信号即推送 Telegram（复用 `src/notifier/telegram_notifier.py`，海外直连 `api.telegram.org`）
  4. Web 服务监听 `$PORT`（默认 7860），响应 HF 健康检查心跳，避免 Space 休眠

部署文件位于 `deploy/hf_space/`（`Dockerfile` + `app.py` + `README.md`），详见 [deploy/hf_space/README.md](deploy/hf_space/README.md)。

> ⚠️ 免费 Space 有 48h 无流量休眠策略，页面已内置心跳尽量保活；休眠后打开一次页面即可唤醒。
> 候选池默认 24h 自动从仓库刷新；Space 重启（更新部署）时也会重新拉取。
> Telegram 凭据只写入 Space secrets，绝不进入公开仓库，安全可靠。
