# GLKVM 屏幕守卫（GLKVM Screen Guard）

> English version: [README.md](README.md)

面向 **Windows 11 + GL.iNet GLKVM RM10** 的桌面工具：根据“是否有人通过 GLKVM 观看画面”自动切换显示布局，并在无人观看时恢复本地的 4K 165 Hz + HDR。

当前版本 **v1.2.0**。轮询和切换状态机运行在 LocalSystem Windows 服务中；显示变更只由 Session 1 的 SYSTEM 显示代理执行。GUI 通过命名管道配置和查看状态，不需要常驻。**界面默认英文**，可在设置中切换中文。

## 功能

- **Windows 服务** `GLKVMGuard`（LocalSystem，自动启动）轮询 RM10，维护观看检测状态机。
- **Session 1 显示代理**：复制 `winlogon` token，设置 `TokenSessionId=1`，通过 `CreateProcessAsUser` 启动。CCD/HDR 只在代理的专用无窗口线程中执行，每条命令重新绑定当前输入桌面。
- **命名管道** `\\.\pipe\GLKVMGuard-v1` 连接 GUI、服务和代理；单 I/O 线程 peek/读，避免同步管道死锁；请求/响应按 ID 匹配。
- **显示引擎**：读取当前拓扑、本地快照采集/校验、槽位锁（接口槽位或 target ID）、只显示 KVM / 复制 / 扩展 / 保持当前布局、10 秒校验、失败回滚和失败冷却。
- **自动切换**：同时读取 `/api/streamer` 与 `/api/log`，离开防抖、最小停留、总开关（关闭后只检测不切换）。
- **运行路径**：登录/锁屏/解锁、睡眠唤醒、关机前恢复；托盘常驻、浅色/深色主题、中英双语、按天日志。
- **数据分离**：用户配置/日志/快照在文档目录；服务元数据和机器级 DPAPI 密码在 ProgramData。

## 环境要求

- Windows 11、Python 3.10+、PyQt6、pywin32。
- 局域网内的 GL.iNet GLKVM RM10（kvmd 4.82 GL 分支）。
- 双输出 PC；HDR 只通过系统“使用 HDR”开关处理。
- 安装服务需要管理员权限。
- 不支持设备屏两步认证。

## 快速开始

不需要打包 exe：设置完成后直接运行本目录的 `RunGUI.bat`。下面详细说明三条安装命令。

### 1. 创建本地 Python 环境

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_env.ps1
```

创建本地 `.\py` 虚拟环境，并安装两个运行依赖：**PyQt6**（界面）和 **pywin32**（Windows 服务/命名管道）。需要联网，不需要管理员权限。只需运行一次；如果 `.\py` 已存在可以跳过。如果 Python 不在默认位置，先运行脚本并根据输出检查。

### 2. 创建桌面快捷方式

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

在当前用户的桌面创建 **GLKVM 屏幕守卫** 快捷方式，指向本目录的 Python 和 GUI。它不安装服务，不需要管理员权限。也可以跳过，直接运行 `RunGUI.bat`。

### 3. 安装 Windows 服务

```powershell
# 在管理员 PowerShell 中运行
powershell -ExecutionPolicy Bypass -File .\install_service.ps1
```

安装并启动 **GLKVMGuard** 服务（LocalSystem，自动启动），写入 `C:\ProgramData\GLKVM Screen Guard\service.json`，设置服务专用 Python 环境，并从用户配置同步机器级 KVM 密码。服务负责后台/登录前检测。重跑同一脚本可修复或升级服务；用 `uninstall_service.ps1` 卸载。

### 4. 启动 GUI

双击桌面快捷方式，或运行：

```powershell
.\RunGUI.bat
```

`RunGUI.bat` 会检查 `.\py` 是否存在，然后用 `pythonw.exe` 启动 `main.py`。不需要单独的打包版本。关闭 GUI 不会停止服务；GUI 只负责配置和查看状态。

## 首次使用

1. 打开「Connection」页，填入 RM10 的 IP（例如 `192.168.1.100`）、用户名和 KVM 网页密码。
2. 点「Test connection」。首次会显示证书 SHA-256 指纹，确认是自己的 RM10；工具只固定指纹，不依赖证书日期。
3. 打开「Displays」页确认 KVM 目标已锁定。可以选择 `Screen 2 (LGD_MP1.1_)`（按接口槽位，抗 EDID 变化）或 `Follow monitor LGD_MP1.1_ (t8448)`（按当前 target ID）；担心 EDID 变化就用 **Screen 2**。
4. 打开「Actions」页，点击 **Capture current display settings** 记录本地快照。自动进入 KVM 布局前必须有有效快照。
5. 确认 **Enable GLKVM screen guard** 总开关打开。
6. 服务会持续轮询；GUI 的 **Start/Stop** 只是暂停/恢复服务轮询。
7. 想验证自动切换：打开 KVM 网页观看几秒再关闭，约 10 秒后恢复本地布局。手动恢复用 **Apply current display snapshot**。

## 数据位置

| 内容 | 路径 |
|---|---|
| 配置 | `<文档>\GLKVM Screen Guard\config.ini` |
| 显示器快照 | `<文档>\GLKVM Screen Guard\snapshots\local.json` |
| 日志 | `<文档>\GLKVM Screen Guard\logs\YYYY-MM-DD.log` |
| 服务元数据 / 机器级密码 | `C:\ProgramData\GLKVM Screen Guard\` |
| 程序/依赖 | 本仓库 + `./py`（本地虚拟环境，不提交 git） |

## 开发

```powershell
.\py\Scripts\python.exe main.py                  # 控制台
.\RunGUI.bat                                     # 无控制台 GUI
.\py\Scripts\python.exe tests\check_i18n.py      # i18n 完整性
.\py\Scripts\python.exe tests\test_detect.py     # 检测状态机（离线）
.\py\Scripts\python.exe tests\test_displays.py   # CCD 只读 + 快照校验
.\py\Scripts\python.exe tests\smoke_ui.py        # UI 冒烟（需 GLKVM_TEST_IP）
```

- 依赖只有 PyQt6 + pywin32。
- Fusion 风格和原生控件；不自绘。
- 文案在 `langs/*.json`，主题在 `themes/*.qss`。
- 服务轮询/状态机：`app/service_runtime.py`；代理：`app/agent_runtime.py`；管道：`app/pipe.py`。
- `smoke_ui.py` 用临时配置，只做未认证探测；设置 `GLKVM_TEST_IP` 后可对真实 RM10 运行。
- 默认界面语言为英文 `en_US`，可在 Settings 中切换中文。

## 当前限制

- 无用户登录界面、锁屏/睡眠/关机的端到端实机测试仍待完成；服务已接入 SESSIONCHANGE/POWEREVENT/PRESHUTDOWN 处理。
- GUI 仍用只读 `QueryDisplayConfig` 填显示器表；切换/采集/恢复都走服务代理。
- 不支持设备屏两步认证。
- 不提供单独安装包；按上面的设置步骤运行 `RunGUI.bat`。

## 文档

- [INSTALL.md](INSTALL.md)：安装步骤与故障排查
- [AGENTS.md](AGENTS.md)：开发者交接与架构说明
- [CHANGELOG.md](CHANGELOG.md)：版本记录

## 许可

MIT，见 [LICENSE](LICENSE)。
