---
title: fin-alert
emoji: 📈
colorFrom: blue
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
---

# fin-alert · v1.1 小市值信号监控（Hugging Face Space）

把仓库 `qiang26/cangyue` 的 v1.1 信号监控部署到 **Hugging Face Space**（海外免费托管）。
Space 位于海外网络，**可直连 `api.telegram.org`**，解决了 CNB 国内构建机推送被墙的问题。

## 运行逻辑

- `app.py` 启动后：
  1. 后台守护线程每 `HF_POLL_INTERVAL`（默认 15）分钟跑一轮 v1.1 信号扫描（FDV Top100 候选池）
  2. 命中信号即推送 Telegram（复用 `src/notifier/telegram_notifier.py`，支持 `TELEGRAM_API_BASE` 代理）
  3. Web 服务监听 `$PORT`（默认 7860），响应 HF 健康检查心跳，避免 Space 休眠

## 必需环境变量（Settings → Variables and secrets）

| 变量 | 说明 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | 你的 Bot token |
| `TELEGRAM_CHAT_ID` | 你自己的 chat_id（用 @userinfobot 查询，不是 Bot 自身 ID） |

可选：`TELEGRAM_API_BASE`（默认官方 `api.telegram.org`，海外环境一般无需设置）、
`HF_POLL_INTERVAL`（轮询分钟数，默认 15）、`HF_TOP`（扫描候选池数量，默认 100）。

## 部署方式（二选一）

### A. 网页手动操作

1. 打开 https://huggingface.co/new-space
2. Space name 填 `fin-alert`，License 随意，**SDK 选 Docker**，点击 Create Space
3. 把本目录下 4 个文件上传到 Space 文件区：
   `Dockerfile`、`app.py`、`README.md`（本文件），并上传仓库的
   `src/`、`scripts/`、`data/`、`config/`、`requirements.txt`、`.env.example`
4. Settings → Variables and secrets 填入 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`
5. 等 2~5 分钟构建完成，日志里出现 `listening on 7860, poll interval 15` 即部署成功

### B. 命令行一键部署（推荐）

在仓库根目录：

```bash
export HF_TOKEN=hf_xxx                 # https://huggingface.co/settings/tokens 新建（需 write 权限）
export HF_USER=你的HF用户名

# 首次：初始化 + 部署
python scripts/deploy_hf.py --space-name fin-alert --deploy

# 之后每次改代码，重新部署即可
python scripts/deploy_hf.py --space-name fin-alert --deploy
```

## 验证

- 打开 Space 主页（`https://huggingface.co/spaces/{用户名}/fin-alert`），看到 `{"status":"ok", ...}` 即存活
- 等第一轮扫描（启动即跑第一轮），或打开 `https://{用户名}-fin-alert.hf.space/` 查看最近一轮状态
- 若命中信号，Telegram 会收到推送

## 注意事项

- **免费 Space 会休眠**：48h 无外部流量会暂停。已通过健康检查心跳尽量保活；
  若仍休眠，打开一次 Space 页面即可唤醒（或升级 $0 的持续运行配置）。
- **去重状态**存在 Space 本地磁盘（`data/v11_signal_state.json`），重启后重置，
  仅影响"同信号 2h 内去重"，不影响扫描与推送。
- 候选池 `data/smallcap_top100_fdv.json` 随仓库部署，需定期刷新
  （可保留 CNB 定时任务跑"刷新候选池"再重新部署，或后续接入 HF 定时任务）。
