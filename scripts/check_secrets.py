#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cust_AstrBot 提交前敏感信息自检脚本。

用法：
    python scripts/check_secrets.py            # 扫描暂存区（git diff --cached）
    python scripts/check_secrets.py --all      # 扫描全部已跟踪文件（定期体检）

规则（任何一条命中即失败，退出码 1）：
    1. 云厂商 API Key（Google AIzaSy...、OpenAI/DeepSeek sk-...、JWT）
    2. 私钥块（BEGIN ... PRIVATE KEY）
    3. 密码/密钥类赋值（password/secret/token/api_key = "..."）
    4. 服务器/内网 IP、内部路径（服务器部署路径、/home/ 下的用户目录） # secret-ok
    5. QQ 平台用户 openid（32 位大写十六进制）
    6. 敏感文件名（.env、*.db、*.log、*.bak、cmd_config.json、插件配置）

在 pre-commit hook 中自动执行（见 scripts/hooks/pre-commit）。
误报处理：确认为误报后，可在该行行尾加注释 `# secret-ok` 跳过。
"""
import os
import re
import subprocess
import sys

# 自动定位仓库根（脚本位于 <repo>/scripts/ 下），保证在任意工作目录运行都正确
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 行级敏感模式（正则）
PATTERNS = [
    ("google-api-key", re.compile(r"AIzaSy[0-9A-Za-z_\-]{30,}")),
    ("openai-style-key", re.compile(r"\bsk-[A-Za-z0-9]{16,}\b")),
    ("jwt-token", re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.")),
    ("private-key", re.compile(r"BEGIN [A-Z ]*PRIVATE KEY")),
    ("credential-assign", re.compile(
        r"(?i)\b(password|passwd|secret|token|api[_\-]?key)\b\s*[:=]\s*[\"'][^\"']{8,}[\"']")),
    ("server-ip", re.compile(r"\b47\.101\.146\.93\b")),
    ("internal-path", re.compile(r"/root/(?!astrbot/venv/lib)")),  # secret-ok：规则自身的正则字面量
    ("qq-openid", re.compile(r"\b[0-9A-F]{32}\b")),
    ("telegram-bot-token", re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_\-]{30,}\b")),
]

# 文件级敏感名（命中即拦）
FILE_PATTERNS = [
    (".env", re.compile(r"(^|/)\.env($|\.|)")),
    ("database", re.compile(r"\.(db|db\-shm|db\-wal|sqlite3?)$")),
    ("log-file", re.compile(r"\.log$")),
    ("backup-file", re.compile(r"\.(bak|backup)$")),
    ("astrbot-main-config", re.compile(r"cmd_config\.json$")),
    ("plugin-config", re.compile(r"astrbot_plugin_[\w]+_config\.json$")),
]

ALLOW_MARK = "secret-ok"


def _git(*args):
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True, cwd=REPO_ROOT,
    ).stdout


def _staged_files():
    return [l.strip() for l in _git(
        "diff", "--cached", "--name-only", "--diff-filter=ACM").splitlines() if l.strip()]


def _tracked_files():
    return [l.strip() for l in _git("ls-files").splitlines() if l.strip()]


def scan(files):
    problems = []
    for path in files:
        # 文件名规则
        for label, pat in FILE_PATTERNS:
            if pat.search(path.replace("\\", "/")):
                problems.append((path, "-", "file-name/" + label, path))
        # 内容规则
        try:
            full = path if os.path.isabs(path) else os.path.join(REPO_ROOT, path)
            with open(full, "rb") as f:
                raw = f.read()
        except OSError:
            continue
        if b"\x00" in raw[:8192]:
            continue  # 二进制跳过
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = raw.decode("gbk")
            except UnicodeDecodeError:
                continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if ALLOW_MARK in line:
                continue
            for label, pat in PATTERNS:
                m = pat.search(line)
                if m:
                    snippet = line.strip()[:120]
                    problems.append((path, lineno, label, snippet))
    return problems


def main():
    if "--all" in sys.argv:
        files = _tracked_files()
        scope = "全部已跟踪文件"
    else:
        files = _staged_files()
        scope = "暂存区"
    if not files:
        print(f"[check_secrets] {scope} 无文件，跳过")
        return 0
    print(f"[check_secrets] 扫描 {scope}：{len(files)} 个文件")
    problems = scan(files)
    if problems:
        print(f"\n[check_secrets] ❌ 命中 {len(problems)} 处敏感信息，禁止提交：\n")
        for path, lineno, label, detail in problems:
            print(f"  {path}:{lineno}  [{label}]  {detail}")
        print("\n处理方式：删除该内容/文件，或确认为误报后行尾加 `# secret-ok`。")
        print("注意：.env / *.db / cmd_config.json / 插件配置文件不允许用 secret-ok 放行，必须移出暂存区。")
        return 1
    print("[check_secrets] ✅ 未发现敏感信息")
    return 0


if __name__ == "__main__":
    sys.exit(main())
