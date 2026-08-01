# SynthPay Windows WeChat Receipt Watcher

该研究型插件读取 Windows 微信中独立打开的收款通知窗口，并通过 SynthPay v2 签名协议回调。微信 3.x 使用 UI Automation；微信 4.x 使用 Win32 `PrintWindow` 后台窗口截图、RapidOCR 主识别和 Tesseract 金额复核。

它面向受控 Windows 桌面会话中的收款通知观察，不调用微信支付 API，不读取账号凭据，不做浏览器自动化，也不依赖手机 SMS 转发器。

## 设计要点

- 后台观察：收款助手窗口可移动到屏幕外，不抢占前台焦点。
- 双 OCR 引擎：RapidOCR 负责速度，Tesseract 用于金额交叉校验。
- 可靠投递：SQLite 队列、指数退避、稳定事件 ID 和重启去重。
- 可验证回调：HMAC-SHA256 v2 覆盖发送时间、消息内容、事件 ID 与观察时间。
- 隐私最小化：日志不保存微信通知原文；真实回调密钥仅保存在本机受 ACL 保护的配置文件中。

该项目是收款通知研究与集成样例。使用者应确保自身场景符合微信、支付服务及当地法律法规的适用要求。

## 要求

- Windows 10/11 或 Windows Server 带交互式桌面。
- Python 3.10 以上。
- Windows 微信已登录，且“微信收款助手”需要作为独立聊天窗口保持打开；其他收款类型需要明确配置对应窗口标题后再启用。
- 微信 4.x 使用常驻 RapidOCR 做快速识别，并在疑似收款时用 Tesseract `chi_sim`、`eng` 模型交叉校验金额；默认 Tesseract 路径为 `%ProgramFiles%\Tesseract-OCR`。
- watcher 必须运行在登录微信的同一 Windows 用户会话，不能作为 SYSTEM 服务运行。

## 安装

以普通 Windows 用户打开 PowerShell：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
notepad "$env:APPDATA\SynthPay\wechat-watcher.ini"
Start-ScheduledTask -TaskName "SynthPay WeChat Watcher"
```

首次保持 `dry_run=1`，支付 `0.01` 元并检查 `%LOCALAPPDATA%\SynthPay\wechat-watcher\state\watcher.log`。确认只生成一条事件后，再配置真实通道 ID 和密钥并切换 `dry_run=0`。

本地 SQLite 会保存待发送事件并指数退避重试；同一通知使用稳定事件 ID，重启不会重复入账。日志不保存微信通知原文。
默认 `use_system_proxy=0`，回调不读取 Windows 用户环境中的代理设置；仅在云端必须经过系统代理时才改为 `1`。
默认 `background_window=1`，检测到“微信收款助手”独立窗口后会将它移到屏幕外，保持后台渲染供 OCR 使用，不抢占前台焦点。
