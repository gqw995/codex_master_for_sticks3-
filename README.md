# CodeX Master

CodeX Master 是运行在 M5Stack StickS3 上的个人 AI 编程用量显示器，同时显示：

- Codex 5 小时与 7 天剩余额度及各自的重置倒计时。
- Vibe Usage 的 24 小时、7 天、30 天预估费用和总 Token。
- Codex 雷达“智力效率”的模型最新分数和网站更新时间。
- USB 热插拔、Wi-Fi 备用连接和多台 Mac 自动切换。
- 右上角四档设备电量图标、80% 亮度和统一的数据更新时间。
- 15 秒无按键后进入低功耗睡眠，按键唤醒后通过 USB 或 Wi-Fi 自动刷新。

项目中的账号凭据始终保留在 Mac 上。StickS3 只接收已经汇总的数字。

完整安装、按键、配网、烧录和故障排查说明请阅读 [使用说明.md](使用说明.md)。

## 项目结构

```text
CodeX Master/
├── src/main.cpp                 StickS3 固件源码
├── tools/                       macOS 后台桥接与安装脚本
├── platformio.ini               PlatformIO 构建配置
├── requirements.txt             Python/PlatformIO 依赖
├── firmware/                    可直接烧录的完整固件
├── installers/                  另一台 Mac 使用的桥接安装包
├── README.md
└── 使用说明.md
```

## 数据来源

- Codex：本机 Codex app-server 的只读额度接口。
- Vibe Usage：本机现有 Vibe Usage 配置对应的统计接口。
- Codex 雷达：[codexradar.com](https://codexradar.com/) 的“智力效率”结构化数据接口；不使用截图或 OCR。
