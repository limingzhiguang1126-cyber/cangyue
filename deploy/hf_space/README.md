---
title: fin-alert
emoji: 📈
colorFrom: blue
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
---

# fin-alert · v1.2 小市值信号监控（Hugging Face Space）

把仓库 `qiang26/cangyue` 的 v1.2 信号监控部署到 **Hugging Face Space**（海外免费托管）。
Space 位于海外网络，**可直连 `api.telegram.org`**，解决了 CNB 国内构建机推送被墙的问题。

## 运行逻辑

- `app.py` 启动后：
  1. **自动刷新候选池**：从 CNB 仓库拉最新 `data/smallcap_top100_fdv.json`
     （默认每 24h 一次；启动时也会拉一次，失败则沿用打包进镜像的池子）
  2. 后台守护线程每 `HF_POLL_INTERVAL`（默认 15）分钟跑一轮 **v1.2** 信号扫描（FDV Top100 候选池）
  3. 命中信号即推送 Telegram（复用 `src/notifier/telegram_notifier.py`，支持 `TELEGRAM_API_BASE` 代理）
  4. Web 服务监听 `$PORT`（默认 7860），响应 HF 健康检查心跳，避免 Space 休眠

## 环境变量（Settings → Variables and secrets）

| 变量 | 说明 | 必需 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | 你的 Bot token | ✅ |
| `TELEGRAM_CHAT_ID` | 你自己的 chat_id（用 @userinfobot 查询，不是 Bot 自身 ID） | ✅ |

可选：`TELEGRAM_API_BASE`（默认官方 `api.telegram.org`，海外环境一般无需设置）、
`HF_POLL_INTERVAL`（轮询分钟数，默认 15）、`HF_TOP`（扫描候选池数量，默认 100）、
`HF_WORKERS`（并发数，默认 8）、`HF_POOL_REFRESH_INTERVAL`（候选池刷新分钟数，默认 1440=24h）、
`HF_POOL_URL`（候选池远端地址，默认 CNB 仓库 main 分支）。

## 部署方式

### A. CNB 网页按钮（最省事，推荐）

1. 在密钥仓库 `qiang26/cangyue-secrets` 的 `telegram.yml` 里加上：
   ```yaml
   TELEGRAM_BOT_TOKEN: "你的 bot token"
   TELEGRAM_CHAT_ID: "你的 chat_id"
   HF_TOKEN: "hf_你的HF令牌"
   ```
2. 回到 `qiang26/cangyue` 仓库 → **代码 → 分支详情页** → 右上角 **「🔔 手动触发」** → **「🚀 部署 HF Space」**
3. CNB 流水线会自动：创建/更新 Space → 上传全部代码 → 写入 Telegram secrets

### B. 命令行一键部署

在仓库根目录：

```bash
export HF_TOKEN=hf_xxx                 # https://huggingface.co/settings/tokens 新建（需 write 权限）

# 一键部署：自动创建 Space + 上传 + 写入 Telegram secrets（HF 用户名自动探测）
python scripts/deploy_hf.py --space-name fin-alert --deploy \
    --tg-token 你的bot_token --tg-chat 你的chat_id

# 只部署代码、不写 secrets（之后在 Space 设置里手动填）
python scripts/deploy_hf.py --space-name fin-alert --deploy

# 预览将上传的文件
python scripts/deploy_hf.py --space-name fin-alert --dry-run
```

## 验证

- 打开 Space 主页（`https://huggingface.co/spaces/{用户名}/fin-alert`），看到 `{"status":"ok", ...}` 即存活
- 或直接访问 `https://{用户名}-fin-alert.hf.space/`，会返回最近一轮扫描状态
- 启动即跑第一轮扫描；若命中 v1.2 信号，Telegram 会收到推送

## 注意事项

- **免费 Space 会休眠**：48h 无外部流量会暂停。已通过健康检查心跳尽量保活；
  若仍休眠，打开一次 Space 页面即可唤醒（或升级 $0 的持续运行配置）。
- **去重状态**存在 Space 本地磁盘（`data/v11_signal_state.json`），重启后重置，
  仅影响"同信号 2h 内去重"，不影响扫描与推送。
- **候选池**默认 24h 自动从 CNB 仓库刷新；若仓库池子更新了，也可直接重新部署 Space。
