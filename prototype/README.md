# UI 原型

打开 `index.html` 即可，纯静态，不连接任何设备。

## 四个标签页

- **连接**：RM10 地址、账号、密码、测试连接、轮询间隔。
- **动作**：连接时的动作、断开时的动作、“应用当前显示器设置”、本地快照。
- **显示器**：当前两块屏、EDID、把某块屏锁定为 GLKVM。
- **服务**：安装/停止/卸载、开机自启、登录前运行、日志。

## 可交互的地方

- 标签页切换。
- 修改任意表单项后，底部“保存/取消”会启用。
- “测试连接”会模拟一次 900 ms 的请求。
- “应用当前显示器设置”会模拟采集，并更新快照时间。
- 勾选“暂停守护”会改变底部状态。
- 右下角“预览：本地 / 远程”只用于演示状态切换；远程状态下“应用当前显示器设置”会被禁用。

## 预览单个页面

```
index.html?tab=actions
index.html?tab=displays
index.html?tab=service
index.html?tab=actions&state=remote
```

## 与最终 PyQt6 的对应

| 原型 | Qt |
|---|---|
| 标签页 | `QTabWidget` |
| 分组框 | `QGroupBox` |
| 单选 | `QRadioButton` |
| 数字输入 | `QSpinBox` |
| IP 地址 | `QLineEdit`（带输入校验） |
| 下拉 | `QComboBox` |
| 显示器表格 | `QTableWidget` |
| 日志 | `QPlainTextEdit`（只读） |
| 托盘 | `QSystemTrayIcon` + `QMenu` |
| 登录自启 | HKCU Run 或计划任务 |
| 底部按钮 | `QDialogButtonBox` |
| 状态栏 | `QStatusBar` |

没有自绘、动画、自定义标题栏。颜色和间距用一份 QSS 就能还原。

注意：这是给评审用的低保真原型，数据是写死的；真正的检测、切换、服务安装都不在这里。
用户手填 RM10 的 IP，不做设备发现；不支持设备屏两步认证。
