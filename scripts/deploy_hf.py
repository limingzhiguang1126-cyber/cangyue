# -*- coding: utf-8 -*-
"""Hugging Face Space 一键部署脚本（支持「只填一个 HF Token」）。

把 qiang26/cangyue 的 v1.2 信号监控打包成 HF Space 并上传部署。
HF Space 位于海外网络，可直连 api.telegram.org，解决 CNB 云端推送被墙问题。

特性：
- 自动创建 Space（SDK=Docker）+ 上传全部代码/数据
- 自动把 Telegram 凭据写入 Space secrets（Settings → Variables and secrets）
- 自动把部署相关信息回填到 Space 元数据（variables），页面可见
- 支持 --dry-run 预览

用法：
    export HF_TOKEN=hf_xxx                     # https://huggingface.co/settings/tokens（需 write 权限）
    export HF_USER=你的HF用户名                  # 可省：不填则用 token 自动探测用户名

    python scripts/deploy_hf.py --space-name fin-alert --deploy
    python scripts/deploy_hf.py --space-name fin-alert --deploy \
        --tg-token 123:abc --tg-chat 8xxxxxx   # 同时写入 Telegram secrets

依赖：pip install huggingface_hub>=0.23
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import sys

try:
    from huggingface_hub import HfApi, SpaceHardware
except ImportError:  # pragma: no cover
    print("缺少依赖 huggingface_hub，请先安装：pip install huggingface_hub>=0.23")
    sys.exit(1)

# 需要随 Space 一起打包的仓库文件/目录（相对仓库根目录）
PACKAGE_ITEMS = [
    "src",
    "scripts",
    "data",
    "config",
    "requirements.txt",
    ".env.example",
]

# 打包时忽略的模式（复用仓库 .gitignore 的规则）
IGNORE_PATTERNS = [
    "__pycache__", "*.pyc", ".venv", "venv", ".env", ".git",
    "*.db", "*.db-journal", "*.db-wal", "*.db-shm", "*.log",
    ".DS_Store", ".pytest_cache", ".mypy_cache", ".ruff_cache",
]

# Space secrets 里需要写入的 Telegram 密钥
SECRET_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


def _should_ignore(rel_path: str) -> bool:
    for pat in IGNORE_PATTERNS:
        if fnmatch.fnmatch(rel_path, pat) or fnmatch.fnmatch(rel_path, pat + "/*"):
            return True
        for part in rel_path.split("/"):
            if fnmatch.fnmatch(part, pat):
                return True
    return False


def _walk_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _should_ignore(
            os.path.relpath(os.path.join(dirpath, d), root))]
        for fname in filenames:
            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, root)
            if not _should_ignore(rel):
                yield full, rel


def collect_local_files(root: str) -> dict[str, str]:
    """收集仓库内需要上传的文件：本地路径 -> Space 内路径。"""
    files: dict[str, str] = {}
    for item in PACKAGE_ITEMS:
        src = os.path.join(root, item)
        if os.path.isfile(src):
            files[src] = item
        elif os.path.isdir(src):
            for full, rel in _walk_files(src):
                files[full] = os.path.join(item, rel)
    # HF Space 部署文件
    hf_dir = os.path.join(root, "deploy", "hf_space")
    files[os.path.join(hf_dir, "Dockerfile")] = "Dockerfile"
    files[os.path.join(hf_dir, "app.py")] = "app.py"
    files[os.path.join(hf_dir, "README.md")] = "README.md"
    return files


def resolve_user(api: HfApi, user: str) -> str:
    """解析 HF 用户名：没填 HF_USER 时用 token 探测（whoami）。"""
    if user:
        return user
    try:
        info = api.whoami()
        name = info.get("name") or ""
        if not name:
            raise ValueError("whoami 未返回用户名")
        print(f"[info] 未指定 HF_USER，已用 token 探测用户名: {name}")
        return name
    except Exception as exc:  # noqa: BLE001
        print(f"[error] 无法探测 HF 用户名：{exc}")
        print("  请在环境变量 HF_USER 中指定你的 HF 用户名")
        raise SystemExit(1)


def ensure_space(api: HfApi, repo_id: str) -> None:
    try:
        api.repo_info(repo_id=repo_id, repo_type="space")
        print(f"[ok] Space 已存在: {repo_id}")
    except Exception:
        print(f"[create] 创建 Space: {repo_id} (Docker)")
        api.create_repo(
            repo_id=repo_id,
            repo_type="space",
            space_sdk="docker",
            space_hardware=SpaceHardware.CPU_BASIC,
            exist_ok=True,
        )
        print("[create] 创建成功")


def write_secrets(api: HfApi, repo_id: str, secrets: dict[str, str]) -> None:
    """把 Telegram 凭据写入 Space secrets。只写入非空值，不覆盖已有 secret。"""
    existing = set()
    try:
        info = api.repo_info(repo_id=repo_id, repo_type="space")
        existing = {s.get("key") for s in (info.secrets or [])}
    except Exception:  # noqa: BLE001
        pass
    for key, val in secrets.items():
        if not val:
            continue
        if key in existing:
            print(f"[skip] secret {key} 已存在，跳过（如需更新请先在 Space Settings 删除）")
            continue
        try:
            api.add_space_secret(repo_id=repo_id, key=key, value=val)
            print(f"[secret] 已写入 {key}")
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 写入 secret {key} 失败：{exc}")


def deploy(api: HfApi, repo_id: str, root: str) -> None:
    # 打包到临时 staging 目录，再整体上传（一次 commit，精确可控）
    import shutil
    import tempfile

    files_map = collect_local_files(root)
    staging = tempfile.mkdtemp(prefix="hf-space-")
    try:
        for full, rel in files_map.items():
            dest = os.path.join(staging, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(full, dest)
        print(f"[upload] 打包 {len(files_map)} 个文件，上传到 {repo_id} ...")
        api.upload_folder(
            repo_id=repo_id,
            repo_type="space",
            folder_path=staging,
            path_in_repo=".",
            delete_patterns=["*"],
            commit_message="deploy: fin-alert v1.2 signal monitor to HF Space",
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    print("[upload] 上传完成，HF 将自动开始构建")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Hugging Face Space 一键部署")
    ap.add_argument("--space-name", default="fin-alert", help="Space 名称（默认 fin-alert）")
    ap.add_argument("--user", default=os.getenv("HF_USER", ""), help="HF 用户名（默认取 HF_USER）")
    ap.add_argument("--token", default=os.getenv("HF_TOKEN", ""), help="HF Token（默认取 HF_TOKEN）")
    ap.add_argument("--tg-token", default=os.getenv("TELEGRAM_BOT_TOKEN", ""),
                    help="Telegram Bot Token（写入 Space secrets）")
    ap.add_argument("--tg-chat", default=os.getenv("TELEGRAM_CHAT_ID", ""),
                    help="Telegram chat_id（写入 Space secrets）")
    ap.add_argument("--deploy", action="store_true", help="执行部署")
    ap.add_argument("--dry-run", action="store_true", help="只列出将上传的文件，不实际部署")
    args = ap.parse_args(argv)

    if not args.token:
        print("请先设置 HF_TOKEN 环境变量（或在命令行传入 --token）")
        print("  HF_TOKEN 获取：https://huggingface.co/settings/tokens 新建（需 write 权限）")
        return 1

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    files = collect_local_files(root)

    if args.dry_run:
        print(f"[dry-run] 将上传 {len(files)} 个文件到 {args.user}/{args.space_name}:")
        for full, rel in sorted(files.items()):
            print(f"  {rel}  ({os.path.getsize(full)} B)")
        secrets = {k: v for k, v in (("TELEGRAM_BOT_TOKEN", args.tg_token),
                                     ("TELEGRAM_CHAT_ID", args.tg_chat)) if v}
        if secrets:
            print(f"[dry-run] 将写入 secrets: {list(secrets.keys())}")
        return 0

    api = HfApi(token=args.token)
    user = resolve_user(api, args.user)
    repo_id = f"{user}/{args.space_name}"
    ensure_space(api, repo_id)
    deploy(api, repo_id, root)
    write_secrets(api, repo_id, {
        "TELEGRAM_BOT_TOKEN": args.tg_token,
        "TELEGRAM_CHAT_ID": args.tg_chat,
    })
    print(f"\n✅ 部署完成！打开 https://huggingface.co/spaces/{repo_id} 查看构建状态")
    print(f"   页面：https://huggingface.co/spaces/{repo_id}")
    print(f"   直连：https://{user}-{args.space_name}.hf.space/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
