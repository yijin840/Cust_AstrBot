# Cust_AstrBot 提交安全注意事项

> 本仓库是公开 fork，**不能转私有**——任何推上去的内容都视同公开。
> 配套自动自检：`scripts/check_secrets.py`（pre-commit 自动运行，详见文末）。
> 最后更新：2026-09-22

---

## 一、本仓库的定位与红线

- 本仓库 = 我们自己的 AstrBot 开发主线，所有本地化改动（补丁、定制功能）**必须提交到这里**，便于回滚。
- **服务器生产配置、密钥、对话数据永远不进仓库。** 它们只存在于：
  - 服务器运行时数据目录（gitignore 已覆盖 `data`）
  - 本地工具目录（server-manager-py，独立仓库/目录，不进本仓库）

## 二、允许 / 禁止提交清单

| ✅ 允许 | ❌ 禁止 |
| --- | --- |
| 源码改动（补丁、定制功能） | `data/` 下任何文件（数据库、对话记录、插件配置） |
| 补丁脚本、部署辅助脚本（不含凭据） | `cmd_config.json`（含 API key）、`.env` |
| 配置模板（占位符形式） | 各插件配置 `astrbot_plugin_*_config.json`（含 key） |
| 通用文档、使用说明 | 服务器 IP / SSH 信息 / 密码 / 密钥 |
| `.gitignore`、自检脚本 | 日志、`.bak` 备份、`*.db` |
| | 对话记录、截图（含用户信息/IP） |

## 三、提交前自检（自动）

```bash
# pre-commit 已挂（首次 clone 后启用一次）：
git config core.hooksPath scripts/hooks

# 也可手动运行：
python scripts/check_secrets.py        # 扫描暂存区
python scripts/check_secrets.py --all  # 全仓库体检（建议每月一次）
```

自检覆盖：API Key（Google/OpenAI/TG）、私钥块、密码类赋值、服务器 IP、内部路径、
QQ openid（32 位十六进制）、敏感文件名（.env/*.db/*.log/*.bak/cmd_config.json/插件配置）。
误报：行尾加 `# secret-ok` 可跳过；**文件级命中（.env/.db/配置文件）不允许放行，必须移出暂存区**。

## 四、提交流程规范

1. **永远不用 `git add -A` / `git add .`**，用 `git add <具体文件>`。
2. add 后先预览：`git diff --cached --name-only`，再 `git diff --cached` 扫一眼内容。
3. Commit message 不含 IP / 密码 / Token / 真实姓名 / 内部路径。
4. 涉及 site-packages 补丁的改动：**先在服务器改并验证，再把同样改动落到本仓库源码，两个地方都要动**（见 `scripts/_fix_*.py` 系列脚本的对应关系）。AstrBot 官方更新覆盖补丁后，从本仓库找回。

## 五、泄露应急

1. 立即轮换泄露的 API Key / Token（先轮换，再清理历史）。
2. `git filter-branch` 或 `git filter-repo` 从历史抹除敏感文件 → `git push --force`。
3. 严重泄露联系 GitHub Support 清缓存。
4. 记住：**force push 后旧 commit 仍可能被缓存**，轮换凭证才是根治。

---

> 一句话版：**add 指定文件 → diff 预览 → 自检绿了再 commit**。30 秒检查胜过 30 天补救。
