# Strata 控制台（StrataLauncher）

Windows 桌面端的本地大模型控制台：一键启动 / 关闭 Strata 推理引擎，内置 **OpenAI 兼容 API 网关**与**公网访问**能力。全部界面使用 wxPython **自绘控件**，无需任何额外 UI 依赖，开箱即用的深色主题。

![当前版本](https://img.shields.io/badge/version-0.2.19-blue) ![平台](https://img.shields.io/badge/platform-Windows%2010%2F11%20x64-lightgrey) ![License](https://img.shields.io/badge/license-MIT-green)

## 功能特性

### 模型管理（控制台页）
- 一键启动 / 关闭 / 卸载模型服务，实时显示加载进度与资源占用
- 启动前自动体检：引擎或模型没装好会**自动跳转到下载页**引导安装
- 上下文用满后自动清理缓存并重新驻留显存（可开关）

### API 网关（设置页）
- OpenAI 兼容接口（`/v1/chat/completions`、`/v1/models` 等），任何 OpenAI SDK / 客户端都能直连
- 多 API Key 管理：每把 Key 独立的 token 上限、单次上下文、QPS / 秒数限速，可单独启停
- 随机后缀"暗门"地址：外人拿不到完整地址就访问不了；「重新生成」一键换新
- 按 Key 的实时监控页：请求来源 IP、用量、内容预览、耗时、限速情况
- Key 码一键复制、接口地址点击即复制

### 公网访问（三种链路自动选择）
1. **路由器 UPnP** 自动映射端口（直连，最快）
2. **自动隧道**：UPnP 不通时自动兜底 trycloudflare 快速隧道，断线自动重连
3. **Cloudflare 命名隧道**：填入你自己的 Zero Trust Tunnel Token + 自定义域名，公网地址**固定不变**、不依赖路由器设置（界面里「Cloudflare 隧道配置」按钮）

### 引擎与模型下载（连接页）
- 自动适配本机内存的型号列表，弹命令行向导完成下载与配置
- 查找本机已有的 Strata 安装目录并一键切换
- 引擎在线检查更新

## 快速开始

### 方式一：安装包（推荐）

下载 `StrataConsole-Setup-0.2.19.exe`，双击安装：

1. 选择安装位置（默认装到用户目录，**不需要管理员权限**）
2. 点「安装」——桌面快捷方式自动创建
3. 从桌面「Strata 控制台」启动

### 方式二：源码运行

要求：Windows 10/11 x64、Python 3.10+

```bash
git clone https://github.com/<你的用户名>/StrataLauncher.git
cd StrataLauncher
pip install -r requirements.txt
python app.py
```

> 首次使用需要先在本机安装 Strata 引擎与模型：在「连接」页的「Strata 引擎 与 模型下载」卡片里选择型号，点「下载选中的模型」，按命令行向导完成即可。默认安装目录写在 `strata_ctl.py` 的 `STRATA_DIR`（可改），也可以直接在「连接」页查找并切换本机已有目录。

## 打包发布

```bash
# 1) 打包主程序（单文件 exe）
pyinstaller --onefile --windowed --name StrataControl \
    --hidden-import strata_ctl --hidden-import api_gate \
    --icon strata.ico --add-data "strata.ico;." app.py

# 2) 制作中文安装程序（需安装 Inno Setup 6，把 ChineseSimplified.isl 放在同目录）
ISCC.exe StrataInstaller.iss
# 产出 installer/StrataConsole-Setup-<版本>.exe
```

发新版本时同步修改 `StrataInstaller.iss` 里的 `AppVersion` / `OutputBaseFilename` 和 `CHANGELOG.md`。

## 项目结构

```
StrataLauncher/
├── app.py                 # 主程序：wxPython 全自绘界面（控制台 / 仪表盘 / 连接 / 设置 四页）
├── api_gate.py            # OpenAI 兼容 API 网关：多 Key 鉴权、限额、监控、公网入口、Cloudflare 隧道
├── strata_ctl.py          # Strata 引擎控制层：启动/停止/健康检查/安装目录管理/引擎更新
├── start_service.py       # 独立拉起模型服务的辅助脚本（进程脱钩）
├── shot.py                # 截图工具（模拟数据版，用于生成宣传图）
├── make_icon.py           # 图标生成工具
├── strata.ico             # 应用图标
├── StrataInstaller.iss    # Inno Setup 6 安装包脚本（中文向导 + 自动桌面快捷方式）
├── ChineseSimplified.isl  # Inno Setup 简体中文语言包
├── CHANGELOG.md           # 更新日志
├── requirements.txt       # Python 依赖（仅 wxPython）
└── tests/                 # 无头测试与端到端验证
    ├── test_clipboard.py          # 剪贴板复制功能
    ├── test_tunnel_persist.py     # 隧道地址跨开关/跨重启稳定
    ├── test_cloudflare_tunnel.py  # Cloudflare 命名隧道全链路
    ├── test_start_guard.py        # 启动前体检 + 自动跳转下载页
    └── e2e_test.py                # 端到端：启动 → 加载 → 卸载 → 关闭
```

## 运行测试

```bash
# 无头测试（不需要真实模型）
python tests/test_clipboard.py
python tests/test_cloudflare_tunnel.py
python tests/test_start_guard.py

# 端到端（需要已安装引擎与模型）
python tests/e2e_test.py
```

## 配置与数据位置

| 内容 | 位置 |
|---|---|
| 程序设置 | `%APPDATA%\StrataControl\settings.json` |
| API Key（含密钥） | `%APPDATA%\StrataControl\api_keys.json` |
| 请求历史 | `%APPDATA%\StrataControl\api_requests.jsonl` |
| cloudflared 程序 | `%APPDATA%\StrataControl\bin\cloudflared.exe`（首次用隧道时自动下载） |

卸载程序不会删除以上用户数据；重装后配置原样恢复。

## 安全说明

- API 网关的公网地址带随机后缀，相当于访问暗门；请只把完整地址 + Key 发给信任的人
- 所有 Key 与配置只保存在本机 `%APPDATA%`，不上传任何服务器
- 公网链路的流量经 Cloudflare 中继时为 HTTPS 加密

## 许可证

[MIT](LICENSE)
