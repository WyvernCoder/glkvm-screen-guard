# 安装与使用（GLKVM Screen Guard v1.2.4）

面向 **Windows 11 + GL.iNet GLKVM RM10** 的屏幕守卫。用户不需要安装包：按下面步骤准备好 Python 环境、快捷方式和 Windows 服务后，直接运行 `RunGUI.bat`（或桌面快捷方式）即可。

- 界面默认英文，可在 Settings → Language 切换中文。
- 轮询和切换状态机在 Windows 服务中运行；显示操作只由 Session 1 SYSTEM 代理执行。
- GUI 只负责配置和查看状态，关闭 GUI 不会停止服务。

## 1. 环境要求

- Windows 11、Python 3.10+。
- 局域网内可访问的 GL.iNet GLKVM RM10（kvmd 4.82 GL 分支）。
- 双输出 PC；HDR 只使用系统“使用 HDR”开关。
- 安装服务需要管理员权限。
- 不支持设备屏两步认证。

## 2. 获取项目

把仓库放在任意固定目录（路径不要包含会变化的临时目录），确认包含：

```
main.py
service.py
agent.py
RunGUI.bat
setup_env.ps1
install.ps1
install_service.ps1
uninstall_service.ps1
py\                 （运行 setup_env.ps1 后生成）
```

后续命令都在项目根目录执行。

## 3. 快速开始（三条命令）

### 3.1 创建本地 Python 环境

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_env.ps1
```

- 创建本地 `.\py` 虚拟环境。
- 安装 **PyQt6**（GUI）和 **pywin32**（Windows 服务/命名管道）。
- 需要联网，不需要管理员权限。
- 只需运行一次；如果 `.\py` 已存在可跳过。
- 如果系统 Python 不在默认位置，先运行脚本，按输出确认使用的解释器。

### 3.2 创建桌面快捷方式

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

- 在当前用户桌面创建 **GLKVM 屏幕守卫** 快捷方式。
- 指向本目录的 Python/GUI；不安装服务，不需要管理员权限。
- 可以跳过，直接运行 `RunGUI.bat`。

### 3.3 安装 Windows 服务

```powershell
# 在管理员 PowerShell 中运行
powershell -ExecutionPolicy Bypass -File .\install_service.ps1
```

- 安装并启动 `GLKVMGuard` 服务（LocalSystem，自动启动）。
- 写入 `C:\ProgramData\GLKVM Screen Guard\service.json`。
- 设置服务专用 `PYTHONPATH`/`PATH`，确保 `pythonservice.exe` 能加载 PyQt/win32。
- 从用户 `config.ini` 同步机器级 DPAPI 密码。
- 重跑可用于修复/升级；卸载用 `uninstall_service.ps1`。
- 服务是后台检测、登录前运行和关屏恢复的关键；不安装服务时 GUI 只能使用受限的独立模式。
- GUI 的 **Settings → Windows service** 也提供启动/停止/卸载按钮，会请求管理员权限；卸载保留用户配置/日志/快照。

校验：

```powershell
Get-Service GLKVMGuard | Format-List Name,Status,StartType
# Status=Running, StartType=Automatic
```

## 4. 启动

双击桌面 **GLKVM 屏幕守卫**，或运行：

```powershell
.\RunGUI.bat
```

`RunGUI.bat` 会先检查 `.\py\Scripts\pythonw.exe`；不存在时提示先运行 `setup_env.ps1`。然后启动 `main.py`，不需要打包的 exe。

调试时用：

```powershell
.\run-debug.bat
```

它保留控制台和 traceback，便于排查启动问题。

## 5. 首次使用

1. 打开 **Connection**，填 RM10 的 IP（例如 `192.168.1.100`）、用户名、KVM 网页密码。
2. 点击 **Test connection**；首次显示证书 SHA-256 指纹，确认是用户的 RM10。
3. 打开 **Displays**，确认 KVM 目标已锁定：
   - `Screen 2 (LGD_MP1.1_)`：按接口槽位，抗 EDID 变化；
   - `Follow monitor LGD_MP1.1_ (t8448)`：按当前 target ID。
   担心 EDID 变化就用 **Screen 2**。
4. 打开 **Actions**，点 **Capture current display settings** 记录本地快照。
   - 没有有效快照时，默认的“恢复本地快照”断开动作会拒绝自动进入 KVM 布局。
   - 采集要求：当前是本地布局、刷新率不低于 100 Hz、`SDC_VALIDATE` = 0。
5. 确认 **Enable GLKVM screen guard** 总开关打开。
6. 服务会自动轮询；GUI 的 **Start/Stop** 只是暂停/恢复服务轮询。
7. 验证自动切换：打开 KVM 网页观看几秒再关闭；关闭约 10 秒后恢复本地。
   手动恢复用 **Apply current display snapshot**。
8. 关闭 GUI 不会停止服务；托盘菜单只控制 GUI。

## 6. 数据位置

| 内容 | 路径 |
|---|---|
| 配置 | `%USERPROFILE%\Documents\GLKVM Screen Guard\config.ini` |
| 显示器快照 | `%USERPROFILE%\Documents\GLKVM Screen Guard\snapshots\local.json` |
| 日志 | `%USERPROFILE%\Documents\GLKVM Screen Guard\logs\YYYY-MM-DD.log` |
| 服务元数据 / 机器级密码 | `%ProgramData%\GLKVM Screen Guard\service.json`、`password.bin` |

密码字段是 DPAPI 密文，不是明文。日志不记录密码和 token。快照只含显示路径/模式/HDR。

## 7. 验证安装

```powershell
# 服务在跑
Get-Service GLKVMGuard | Format-List Name,Status,StartType
Get-Process pythonservice -ErrorAction SilentlyContinue

# 代理在 Session 1 以 SYSTEM 运行
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'agent.py' } |
    Select-Object ProcessId,SessionId,Name

# 配置和今天的日志存在
$data = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'GLKVM Screen Guard'
Test-Path (Join-Path $data 'config.ini')
Test-Path (Join-Path $data ('logs\' + (Get-Date -Format 'yyyy-MM-dd') + '.log'))

# 采集过快照后才会存在
Test-Path (Join-Path $data 'snapshots\local.json')
```

## 8. 卸载

1. 关闭 GUI（关闭窗口/托盘退出）。
2. **管理员 PowerShell** 运行 `.\uninstall_service.ps1`（停止并删除服务、`service.json`、机器密码）。
3. 删除桌面快捷方式。
4. 需要时删除 `文档\GLKVM Screen Guard`（配置/日志/快照，先备份）。
5. 删除项目目录（或只删 `py` 虚拟环境）。

## 9. 故障排查

| 现象 | 处理 |
|---|---|
| `pythonw.exe` 不存在 | 运行 `setup_env.ps1`；检查 Python 3.10+ 是否安装 |
| GUI 启动无反应 | 用 `run-debug.bat` 看 traceback；检查 `py\Scripts\python.exe main.py` |
| 服务未安装 / GUI 显示“独立模式” | 管理员运行 `install_service.ps1` |
| 服务启动失败 1053 | 检查服务的 `Environment`（PYTHONPATH/PATH）；重新运行 `install_service.ps1` |
| 服务已连接但“显示代理未连接” | 重启服务；看日志里的 `Agent launch`/`agent starting`；确认 winlogon 在 Session 1 |
| `Test connection` 提示证书指纹不匹配 | 固件升级可能换了证书；确认设备后点「是」更新指纹 |
| 登录 403 / 认证失败 | 检查用户名/密码；确认 KVM 网页能登录 |
| 提示不支持两步认证 | 当前明确不支持；在 RM10 屏幕/网页关闭两步认证 |
| 一直“未知” | KVM 不可达或证书/网络问题；看「运行日志」和数据目录里的日志 |
| 有观众但不切屏 | 检查总开关、槽位锁、服务状态和代理状态 |
| 选「不锁定」会怎样 | 只检测观看状态，不切换显示；首次运行只有一个未激活目标时才会自动选中它 |
| 快照提示“校验失败/刷新率低” | 确认当前是本地 4K 165 Hz、不是复制/PBP 中间态，再重新采集 |
| 切过去后没回来 | 点 **Apply current display snapshot**；若仍失败，用 Windows 显示设置手动切回屏 1 |
| 关闭 GUI 后不工作 | 这是预期：服务继续工作；检查服务状态，GUI 只负责配置/状态 |
| 登录前不检测 | 服务 LocalSystem 自动启动；若未生效，检查服务 StartType 和事件日志 |

## 10. 当前限制

- 无用户登录界面、锁屏/睡眠/关机的端到端实机测试仍待完成（代码路径已接入服务的 SESSIONCHANGE/POWEREVENT/PRESHUTDOWN）。
- HDR 状态在 Windows 11 24H2+（build 26100+）是准确的；旧版 Windows 回退到 legacy advanced-color API，无法区分 HDR 与 WCG。
- 从 v1.2.0 或更早版本升级后，请重启服务并重新采集一次本地快照；旧快照没有 `hdr_api`，不会用来打开 HDR。
- GUI 仍会用只读 `QueryDisplayConfig` 填显示器表；切换/采集/恢复都走服务代理。
- 不支持设备屏两步认证。
- 不提供单独安装包；按本文步骤配置后直接运行 `RunGUI.bat`。
- RM10 证书有效期是坏的（1970–1979），只能按 SHA-256 指纹校验，不要改成常规证书校验。
