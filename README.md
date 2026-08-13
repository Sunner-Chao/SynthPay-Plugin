# SynthPay Windows WeChat Receipt Watcher

该研究型插件读取 Windows 微信中独立打开的收款通知窗口，并通过 SynthPay v2 签名协议回调。微信 3.x 使用 UI Automation；微信 4.x 使用 Win32 `PrintWindow` 后台窗口截图、RapidOCR 主识别和 Tesseract 金额复核。

它面向受控 Windows 桌面会话中的收款通知观察，不调用微信支付 API，不读取账号凭据，不做浏览器自动化，也不依赖手机 SMS 转发器。

## 设计要点

- 后台观察：收款助手窗口可移动到屏幕外，不抢占前台焦点。
- 双 OCR 引擎：RapidOCR 负责速度，Tesseract 用于金额交叉校验。
- OCR 自检：启动时验证 `chi_sim`/`eng` 文件和 Tesseract 实际加载结果；RapidOCR 初始化失败时自动使用 Tesseract。
- 可靠投递：SQLite 队列、指数退避、稳定事件 ID 和重启去重。
- 单实例运行：Windows 命名互斥锁阻止计划任务与手动启动产生重复监听进程。
- 可验证回调：HMAC-SHA256 v2 覆盖发送时间、消息内容、事件 ID 与观察时间。
- 隐私最小化：日志不保存微信通知原文；真实回调密钥仅保存在本机受 ACL 保护的配置文件中。

该项目是收款通知研究与集成样例。使用者应确保自身场景符合微信、支付服务及当地法律法规的适用要求。

## 要求

- Windows 10/11 或 Windows Server 带交互式桌面。
- Python 3.10 以上。
- Windows 微信已登录，且“微信收款助手”需要作为独立聊天窗口保持打开；其他收款类型需要明确配置对应窗口标题后再启用。
- 微信 4.x 使用常驻 RapidOCR 做快速识别，并在疑似收款时用 Tesseract `chi_sim`、`eng` 模型交叉校验金额；默认 Tesseract 路径为 `%ProgramFiles%\Tesseract-OCR`。
- watcher 必须运行在登录微信的同一 Windows 用户会话，不能作为 SYSTEM 服务运行。

## 在全新 Windows 设备一键安装

不能无条件保证任意 Windows 设备可用：设备必须有 Windows 10/11（或带交互桌面的 Windows Server）、网络、`winget`（App Installer）、已登录的 Windows 用户会话和已登录微信；生产模式还必须由操作者提供 SynthPay 回调 URL 与密钥。脚本不会也不能替用户登录微信、生成服务器授权，或绕过微信客户端的窗口行为。

下载或克隆仓库后，双击 `setup-and-start.cmd`。它会自动检查或安装 Python 3.10、Tesseract，并将 `chi_sim`/`eng` 识别数据放到当前用户目录，创建隔离运行环境，首次交互式配置生产回调（直接回车则按 `dry_run=1` 启动），注册当前用户登录时自动运行的任务并立即启动 watcher。

OCR 模型下载后会进行固定 SHA-256 校验，并通过 `tesseract --list-langs` 确认实际可加载。离线安装时，可将 `chi_sim.traineddata` 和 `eng.traineddata` 放入仓库的 `models` 目录；模型二进制不会提交到 Git。

也可在 PowerShell 中运行：

```powershell
.\setup-and-start.ps1
```

无界面部署可传入回调配置：

```powershell
.\setup-and-start.ps1 -NonInteractive -CallbackUrl "https://example.com/api/pay/1/notify" -CallbackSecret "your-secret"
```

脚本只写入当前 Windows 用户的 `%APPDATA%\SynthPay\wechat-watcher.ini`，不会将密钥提交进仓库。若配置已经存在，会保留它。

若 Tesseract 升级或模型文件损坏，可运行：

```powershell
.\repair-ocr.ps1
```

修复脚本会将官方模型安装到 `%LOCALAPPDATA%\SynthPay\wechat-watcher\tessdata`，完成哈希与语言加载验证。监听器启动日志中的 `tesseract_ready=True` 表示双 OCR 校验可用。

## 手动安装

以普通 Windows 用户打开 PowerShell：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
notepad "$env:APPDATA\SynthPay\wechat-watcher.ini"
Start-ScheduledTask -TaskName "SynthPay WeChat Watcher"
```

首次保持 `dry_run=1`，支付 `0.01` 元并检查 `%LOCALAPPDATA%\SynthPay\wechat-watcher\state\watcher.log`。确认只生成一条事件后，再配置真实通道 ID 和密钥并切换 `dry_run=0`。

本地 SQLite 会保存待发送事件并指数退避重试；同一通知使用稳定事件 ID，重启不会重复入账。日志不保存微信通知原文。
默认会补扫当前微信收款助手窗口中最近 24 小时的可见通知，并按通知时间与金额去重；可通过 `receipt_lookback_minutes` 调整。即使卡片顶部在滚动时被裁掉，只要原始通知时间、金额、当日收款序号和入账状态均可识别，也会安全补录。
默认 `use_system_proxy=0`，回调不读取 Windows 用户环境中的代理设置；仅在云端必须经过系统代理时才改为 `1`。
默认 `background_window=1`，检测到“微信收款助手”独立窗口后会将它移到屏幕外，保持后台渲染供 OCR 使用，不抢占前台焦点。
