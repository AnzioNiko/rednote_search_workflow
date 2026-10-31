#!/usr/bin/env python3
"""以隐藏输入管理保存在 Windows Credential Manager 中的 LLM API 密钥。"""

from __future__ import annotations

import argparse
import getpass
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.secure_credentials import delete_secret, secret_exists, store_secret, validate_credential_id


def main() -> int:
    parser = argparse.ArgumentParser(description="在 Windows Credential Manager 中管理 LLM API 密钥。")
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("set", "status", "delete"):
        command = subparsers.add_parser(action)
        command.add_argument("credential_id", help="WebUI 中设置的密钥凭据标识")
    args = parser.parse_args()
    try:
        credential_id = validate_credential_id(args.credential_id)
        if args.action == "set":
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                raise RuntimeError("请在可交互的本机终端运行，避免密钥输入回显或被重定向。")
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", getpass.GetPassWarning)
                    first = getpass.getpass("请输入 LLM API 密钥（输入不会显示）：")
                    second = getpass.getpass("请再次输入以确认：")
            except getpass.GetPassWarning:
                raise RuntimeError("当前终端不能保证隐藏输入，未读取或保存密钥。") from None
            if first != second:
                first = second = ""
                print("两次输入不一致，未保存。", file=sys.stderr)
                return 2
            store_secret(credential_id, first)
            first = second = ""
            print("密钥已写入 Windows Credential Manager；不会保存到配置文件或终端日志。")
        elif args.action == "status":
            print("已配置。" if secret_exists(credential_id) else "尚未配置。")
        else:
            print("已从 Windows Credential Manager 删除。" if delete_secret(credential_id) else "该标识没有已保存的密钥。")
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"操作失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
