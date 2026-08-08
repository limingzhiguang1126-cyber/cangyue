# 🏠 本地运行指南（v1.2 信号监控 + Telegram 推送）

> 本文是「在你自己电脑/服务器上跑 v1.2 信号系统」的完整操作手册。
> 目标是：**每 15 分钟扫一次 FDV Top100 候选池，命中 v1.2 标准就推送到你的 Telegram**。
> 全程无需自己写代码，照着命令复制粘贴即可。

---

## 0️⃣ 前置条件

| 需要 | 说明 |
|---|---|
| 一台常开机的电脑/服务器 | 家里电脑 / 云服务器 / NAS / 树莓派都行（需要长期在线，否则停在你关机时段） |
| Python 3.9+ | 检查：`python3 --version`（Mac 是 `python3`，没有 `python` 命令，正常） |
| 网络能访问 Telegram | 你本地能直连 `api.telegram.org` 就行（你已实测过推送成功 ✅） |
| Telegram Bot Token / chat_id | 你之前已经调好、且测试推送成功的那套（`6541835329:xxx` + 你自己的用户 ID） |

> ⚠️ 如果你最终只想用「CNB 云端定时任务 + 海外 Telegram 代理」或「Hugging Face Space」，**不需要**按本文部署，本文只讲本地跑。

---

## 1️⃣ 拉代码（用 main 分支，最新版）

```bash
cd ~/Desktop                       # 或任意目录
git clone https://cnb.cool/qiang26/cangyue.git
cd cangyue
git checkout main                  # main 里已包含 v1.2 守护进程
ls scripts/                        # 应能看到 tg_test_push.py / v11_cloud_run.py 等
```

> 如果你本地已有旧目录，直接 `cd cangyue && git checkout main && git pull` 更新即可。

---

## 2️⃣ 创建 Python 虚拟环境 + 装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate          # 激活后终端提示符前会出现 (.venv)
python3 -m pip install -r requirements.txt
```

> Mac 系统 Python 可能限制直接装包，用虚拟环境（venv）可避开；装完每次跑之前都要先 `source .venv/bin/activate`。

---

## 3️⃣ 配置 Telegram 凭据（唯一需要手填的一步）

```bash
cp .env.example .env
```

用编辑器打开 `.env`，把两行改成**你之前测试成功的那套值**：

```bash
TELEGRAM_BOT_TOKEN=6541835329:xxxxx    # @BotFather 拿的 token
TELEGRAM_CHAT_ID=8xxxxxxx              # @userinfobot 查到的“你自己”的用户 ID
```

> ⚠️ 千万别把 chat_id 填成 token 冒号前那串数字（那是 Bot 自己的 ID，bot 不能给自己发消息，会报 `Forbidden: the bot can't send messages to the bot`）。
> `.env` 已被 .gitignore 忽略，不会提交到仓库，放心填写。

---

## 4️⃣ 先跑一次单轮（验证全链路，强烈建议）

```bash
# 方式 A：只打印不推送（dry-run，不依赖 token 也能跑，先看扫不扫得动）
python -m src.screener.v11_signal_daemon --once --dry-run --top 20

# 方式 B：真实扫描并推送一轮（能收到就说明全链路通了）
python -m src.screener.v11_signal_daemon --once
```

看到类似 `round done: scanned=100 hits=0 pushed=0 skipped=0` 就是跑通了：
- `hits=0` 表示当前没有命中 v1.2 信号（正常，属于“等信号”状态，没命中就不推）
- 如果 `hits>0` 且你没开 dry-run，Telegram 会立刻收到推送

> 💡 第一次跑会从币安 fapi 拉 K 线/OI/费率，可能遇到少量 429 限流告警，代码自带退避重试，耐心等几秒即可。

---

## 5️⃣ 正式常驻（每 15 分钟自动扫 + 推送）

```bash
nohup python -m src.screener.v11_signal_daemon > v11.log 2>&1 &
```

- 想改频率：`--interval 5` 就是每 5 分钟一次（默认 15 分钟）
- 看日志：`tail -f v11.log`
- 停掉：`pkill -f v11_signal_daemon`
- 开机自启（Linux/systemd）：仓库里有模板 `docker/fin-alert.service`，把 `ExecStart` 改成上面命令即可

### 关于去重
同标的 + 同信号线在去重窗口内（默认 2 小时）只推一次，状态存在 `data/v11_signal_state.json`，重启不重复轰炸。
想清空重来：`rm -f data/v11_signal_state.json`

---

## 6️⃣ 收到的推送长这样

```
🚨 TST 🔵 埋伏观察
📊 候选池排名：#7 | 合约：TSTUSDT
📊 现价 0.01252 | 4h +5.1% | 量能 9.2x | OI 1.32x | 费率 +0.005%
🎯 命中原因：辅助线③：4h 涨幅 +5.1% (2~8%) 且 4h 量能 9.2x (≥3x)、OI 放大 1.32x (≥1.15x)，提前埋伏观察
💡 建议：埋伏观察：提前盯住，等 OI 跟进、量能持续再确认
```

---

## 7️⃣ 候选池多久更新一次

- 本地跑时池子用的是仓库里 `data/smallcap_top100_fdv.json` 快照（100 个标的）
- 想刷新池子：`python -m src.screener.binance_smallcap --top 100 --sort fdv --output data/smallcap_top100_fdv.json`
- 或者保持仓库里的 CNB 定时任务「每天 07:35 刷新候选池」继续开，本地只跑监控即可（两者互不冲突）

---

## 🆚 三种方案对比（供你决定用哪种）

| 方案 | 常驻位置 | 优点 | 缺点 |
|---|---|---|---|
| **A. 本地守护进程**（本文） | 你自己电脑/服务器 | 直连 Telegram 无需代理、最省事、不依赖外部服务 | 需要一台常开机的机器 |
| **B. CNB 云端定时任务** | CNB 免费构建机 | 零成本零常驻，平台调度 | 国内网络直连 Telegram 被墙，需配海外 Bot API 代理 |
| **C. Hugging Face Space** | HF 免费海外 Space | 海外直连 Telegram、常驻 | 免费档 48h 无访问会休眠，需心跳保活 |

> ⚠️ 以上均为数据监控工具说明，不构成投资建议；小市值币波动大、流动性低，务必自行控制仓位与止损。
