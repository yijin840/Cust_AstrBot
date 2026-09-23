# custom_plugins — 小蜗定制插件集

本目录存放 AstrBot fork 的自定义/定制插件源码，随本体仓库一起版本管理。

## 目录

| 插件 | 说明 |
|---|---|
| `astrbot_plugin_deepread` | 分享卡片预读（小黑盒/B站/通用网页），正文+热评+配图注入上下文 |
| `astrbot_plugin_splitter` | 回复分段（简易/高级模式，均衡切分） |
| `astrbot_plugin_reply_gate` | 群聊语境感知 + 发送前拦截 + 创作类强制联网检索 |
| `astrbot_plugin_xiaowo_guard` | 输出守卫（人设约束执行） |

## 部署（服务器）

```bash
cd /path/to/Cust_AstrBot
git pull
bash deploy/sync_custom_plugins.sh
systemctl restart astrbot
```

- 插件源码同步到 `data/plugins/<插件名>/`，运行数据（`data/` 子目录）与 `.bak` 不会被覆盖
- `custom_config_templates/` 里的配置模板只在目标配置不存在时安装，绝不覆盖线上配置

## 修改流程（铁律）

本地改 → 按提交安全规范提交 → push → 服务器 `git pull` → `bash deploy/sync_custom_plugins.sh` → 重启。
**禁止直接修改服务器上的插件源码。**

## 提交安全

本仓库为公开仓库。提交前确认：无 IP / 服务器路径 / QQ 号 / 密钥 / `.bak` 文件（详见 `docs` 或提交规范文档）。
