<p align="center">
  <img src="https://img.shields.io/badge/workingAgent-工作智能体-neon?style=for-the-badge&logo=openai&logoColor=white&labelColor=0d1117&color=00f0ff" alt="workingAgent">
</p>

<p align="center">
  <b>这不是聊天机器人。这是把你手上的活，一件件干完的工作搭档。</b>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/License-Apache_2.0-blue" alt="License">
  <img src="https://img.shields.io/badge/平台-Win%20%7C%20Linux%20%7C%20macOS-lightgrey" alt="Platform">
  <img src="https://img.shields.io/badge/LLM-12%20供应商-purple" alt="LLMs">
  <img src="https://img.shields.io/badge/工具-30+-orange" alt="Tools">
  <img src="https://img.shields.io/badge/企业微信-集成-success" alt="WeCom">
  <img src="https://img.shields.io/badge/状态-活跃-success" alt="Status">
</p>

<p align="center">
  简体中文 | <a href="README_EN.md">English</a>
</p>

---

## 📹 演示视频

<video src="https://github.com/user-attachments/assets/a98dfdc5-9a1f-42cf-9d2d-47a40bcc7606" controls width="100%" style="max-width:720px;border-radius:12px;"></video>

> 完整版：[下载演示视频](https://github.com/user-attachments/assets/a98dfdc5-9a1f-42cf-9d2d-47a40bcc7606)

---

## 🖼️ 精彩截图

| 18765 管理后台 | 18767 聊天页 | 企业微信集成 |
|:---:|:---:|:---:|
| ![管理后台](docs/screenshots/18765.png) | ![聊天页](docs/screenshots/18767.png) | ![企业微信](docs/screenshots/qiyeweixing.jpg) |

---

<p align="center">
  <i>不是聊天机器人，不是代码补全插件，不是"帮你写几行"的工具。<br>
  workingAgent 把你的工作记住、把你的活儿接过——<br>
  整理文件、写方案、做文档、盯任务、跨端随时响应，<br>
  每做完一件事，把结果整整齐齐地交回你手上。</i>
</p>

---

## 🧠 workingAgent 是什么？

workingAgent 是一个**为"把活干完"而生的桌面智能体**。它把「思考」和「执行」分开：你给目标，它负责把步骤一步步做完，再拿着真实结果回来交差，而不是用空话敷衍你。

它记得你的工作和项目上下文，能真正操作你的电脑、处理办公文档、调度定时任务，还能在桌面、网页、手机和企业微信里随时待命。

| 层级 | 角色 | 描述 |
|------|------|------|
| **A 层**（认知层） | 记忆 · 判断 · 人格 | 记住你做过的、你所在的项目的上下文。决定**做什么、怎么做**。 |
| **B 层**（执行层） | 工具 · LLM · 代码 | 调用 30+ 工具，写代码、生成文档、控制设备。负责**做完**。 |

这套"思考与执行分离"的双层结构，正是 workingAgent 区别于普通 AI 助手的地方——它不是回答完就停，而是**想清楚、计划好，然后真正动手做完**。

---

## ✨ 核心能力

### 🧠 为工作设计的记忆系统

模仿人类认知的三级分层记忆架构，但一切都围绕你的工作：

- **大纲层** — 你所有对话的语义摘要（10000+ 条）
- **细纲层** — 带标签的结构化摘要
- **细节层** — 完整的对话片段，含关联链接

记忆带有**重要性权重**，关键的工作里程碑更容易被想起；关联网络把不同时间的相关工作链接起来，让项目上下文跨会话延续——重启电脑也不会忘。

### 👁️ 视觉感知

通过你的手机摄像头或 RTSP 摄像头，它捕捉场景并存储带 GPS、时间戳和语义描述的图像，用于巡检、记录、对比。它可以：

- 识别人脸并记住谁是谁
- 描述摄像头前正在发生什么
- 对比当前和过去的场景
- 用自然语言搜索视觉记忆（"给我看上周二客厅的样子"）

### 🛠️ 30+ 内置工具 — 真正把活做完

workingAgent 能真正地**做事**，而不只是"说"：

| 分类 | 工具 |
|------|------|
| **文件系统** | 读取、写入、搜索、删除、列出目录 |
| **网络** | 搜索、抓取网页、提取文章、浏览器操作 |
| **系统** | 运行命令、执行 Python、剪贴板、系统信息 |
| **办公** | Word、Excel、PPT、PDF 生成与解析 |
| **金融** | 股票行情、股票搜索、新闻头条 |
| **图像** | AI 图片生成（ComfyUI SDXL/NoobAI 或 pollinations.ai） |
| **智能家居** | Home Assistant 集成——灯、空调、窗帘、咖啡机 |
| **桌面** | 截图、OCR、鼠标键盘控制、启动应用 |

### ⏰ 定时任务 — 无人值守的自动化

除了对话，workingAgent 还能按计划自动执行：

- 自然语言设定提醒与任务：`3 点提醒我开会`、`每天上午自动整理昨天的日报`
- 定时调用工具，到点把结果送到你手上
- 后台常驻，无人值守也能把活干完

> 注：定时任务调度器基于现有能力，属待完善模块。

### 📈 成长引擎 — 越用越懂你的做事套路

每次任务都在改变 workingAgent。**成长引擎**持续：

- 基于你的互动习惯**沉淀做事经验**
- 从一次次完成任务中**归纳可复用方法**
- **去重合并**认知，形成长期记忆，跨重启持久化

你用得越久，它越懂你的工作方式，越能自己把相似的活接着干。

### 🎙️ 12 个 LLM 后端

一个大脑，多种核心。选择你的供应商：

| 供应商 | 最佳场景 |
|--------|----------|
| **DeepSeek** | 深度推理，64K 上下文 |
| **OpenAI** | GPT-4o / GPT-4o-mini |
| **Claude** | 长文分析，扩展思维 |
| **Gemini** | Google 多模态模型 |
| **Groq** | 超快推理 |
| **通义千问 / 智谱 / 豆包 / Kimi** | 中文优化 |
| **文心一言 / 讯飞星火** | 企业级中文 |
| **Ollama** | 100% 本地，离线，隐私 |

### 👤 VRM 3D 虚拟形象

一个会呼吸的全息虚拟形象，实时反映当前状态：

- 20 种情绪映射到面部表情
- 呼吸和眨眼动画
- 说话时唇形同步
- 全息视觉风格
- 支持 VRM 0.x 和 1.0 模型

### 🌐 双端口 Web 管理 — 一处大脑，处处可用

workingAgent 提供完整的多端接入，无需安装任何客户端：

**18765 端口 — 管理后台**
- 聊天对话、人格设定、硬件配置
- 记忆库查看与管理（按用户/层级/模态筛选）
- 系统设置、用户注册与权限管理
- 高德地图 GPS 地址解析配置

**18767 端口 — 网页聊天**
- WebSocket 实时聊天，支持图片发送
- 与桌面端共享同一个人格和记忆库
- 手机/平板自适应界面
- 随时随地安排它干活

### 💼 企业微信集成

已接入企业微信智能机器人，让工作指令直达你的聊天窗口：

- **长连接模式** — 实时接收消息，无需轮询
- **5 秒超时保障** — 占位消息+最终回复双段式响应
- **多用户识别** — 自动区分不同用户
- **自动重连** — 断网后 5 秒自动恢复
- 在企业微信管理后台创建智能机器人，填入 BotID 和 Secret 即可

### 🤖 硬件扩展 — 把 agent 接到物理世界

workingAgent 不只活在屏幕里，它可以通过模块化传感器桥接层，真正进入物理世界：

**机器狗 / 机械臂（MQTT 协议）**
- 内置 **Sensor Agent（传感器代理）**，实时 MQTT 遥测
- 监控电量、IMU 姿态、电机温度、关节角度、GPS、超声波测距、障碍物检测
- 异常告警：电量过低、电机堵转、温度过高、30cm 内障碍物
- 支持 `robot_dog` / `robot_arm` / `custom` 三种配置，无硬件时可用模拟模式调试

**小智 ESP32 语音终端**
- 为 ESP32 语音设备提供 WebSocket 服务端
- 全双工链路：STT → A 层理解 → TTS → 设备播放
- Opus 音频编解码，低延迟无线语音，唤醒词监听

**手机变身移动传感器阵列**
- 摄像头：RTSP + IP Webcam 实时画面
- 麦克风：远程音频采集
- 传感器：GPS、电量、光线、加速度计
- 状态机：待机 / 对话 / 任务三种模式自动切换

### 🧑 多用户身份

多引擎人脸识别（InsightFace / face_recognition / OpenCV），多用户身份。workingAgent 知道现在是谁在跟它说话，按人维护记忆与上下文。

---

## 📦 安装

### Windows 一键安装

```bash
# 1. 从 python.org 安装 Python 3.10+（勾选"Add to PATH"）
# 2. 双击 install.bat
# 3. 双击 launch.bat
# 4. 在 GUI 设置中配置你的 LLM API Key
# 5. 开始干活！
```

### 手动安装

```bash
git clone <本仓库地址>
cd <仓库目录名>
pip install -r requirements.txt
cp ha_config.example.json ha_config.json  # 编辑你的配置
python main.py
```

### 云服务器部署（独立模式）

```bash
# 安装服务器依赖
pip install -r requirements_server.txt

# 启动（同时启动 18765 管理后台 + 18767 聊天页）
python server_start.py

# 浏览器打开:
# http://localhost:18765  — 管理后台（对话、设置、记忆库）
# http://localhost:18767  — 网页聊天
```

### 📹 配置网络摄像头（RTSP 地址怎么来）

摄像头感知依赖 `ha_config.json` 里的 `rtsp_url`。**RTSP 是监控摄像头的标准取流协议**，格式如下：

```
rtsp://用户名:密码@摄像头IP:端口/取流路径
```

常见品牌默认路径（用户名/密码即摄像头后台登录账密）：

| 品牌 | RTSP 完整示例 |
|------|--------------|
| 海康威视 | `rtsp://admin:密码@192.168.1.10:554/Streaming/Channels/101` |
| 大华 | `rtsp://admin:密码@192.168.1.10:554/cam/realmonitor?channel=1&subtype=0` |
| 通用 ONVIF | `rtsp://admin:密码@192.168.1.10:554/h264/ch1/main/av_stream` |

**不确定路径？两种最省事的办法：**

1. **用手机验一下**（推荐）：装个免费的 **VLC** 播放器（手机/电脑都行），把上面猜测的地址粘进去播放，能出画面就说明地址对了，再把 `rtsp_url` 填进配置。
2. **读摄像头官方手册 / ONVIF 工具**：搜你的品牌 + "RTSP 地址"，或下载 ONVIF Device Manager 自动扫摄像头支持的流。

**关键前提（最容易踩的坑）：**
- 摄像头和 workingAgent 跑的那台设备**必须在同一局域网**，且能互相 ping 通。
- RTSP 默认端口 **554**，需要路由/防火墙放行。
- 有些摄像头默认**没开 RTSP**，要去摄像头后台把 "RTSP 使能" 打开。

写好后，在后台「📱 硬件」页填 RTSP URL，或在 `ha_config.json` 里设 `rtsp_url` 并重启即可。

---

## 🏗️ 系统架构

```
┌─────────────────────────────────────────────────────┐
│                  workingAgent 核心                    │
│                                                      │
│  ┌──────────────┐    ┌──────────────────────────┐   │
│  │   A 层（认知） │    │      B 层（执行）         │   │
│  │  记忆·判断·人格 │◄──►│  LLM 客户端（12 后端）    │   │
│  │  工作经验积累   │    │  工具执行器（30+ 工具）   │   │
│  └──────┬───────┘    └──────────┬───────────────┘   │
│         │                       │                    │
│  ┌──────▼───────────────────────▼───────────────┐   │
│  │              记忆系统                          │   │
│  │  大纲 → 细纲 → 细节（三级）                    │   │
│  │  重要性加权 + 关联网络                         │   │
│  └──────────────────────┬───────────────────────┘   │
│                         │                            │
│  ┌──────────────────────▼───────────────────────┐   │
│  │              成长引擎                          │   │
│  │  经验沉淀 + 做事方法归纳 + 长期认知             │   │
│  └──────────────────────────────────────────────┘   │
│                                                      │
│  ┌──────────┐ ┌──────────┐ ┌──────────────────┐    │
│  │ 多端接入  │ │ VRM 3D   │ │  硬件桥接          │    │
│  │ 桌面/Web/ │ │ 虚拟形象 │ │  手机/ESP32/HA    │    │
│  │ 企业微信  │ │          │ │                   │    │
│  └──────────┘ └──────────┘ └──────────────────┘    │
└─────────────────────────────────────────────────────┘
```

## 📁 项目结构

```
workingAgent/
├── engine/                  # 认知与执行核心
│   ├── agent.py             # A 层：记忆、判断、人格
│   ├── executor.py          # B 层：工具执行、LLM 编排
│   ├── memory.py            # SQLite + 向量记忆存储
│   ├── memory_manager.py    # 分层记忆检索
│   ├── learner.py           # 成长引擎与认知形成
│   ├── llm_client.py        # 12 个 LLM 供应商适配器
│   ├── tools.py             # 30+ 内置工具
│   ├── office_tools.py      # Word/Excel/PPT/PDF 工具
│   └── ...
├── hardware/                # 硬件集成
│   ├── bridge.py            # Home Assistant + RTSP 摄像头
│   ├── vision_pipeline.py   # 视觉记忆流水线
│   ├── phone_ws_server.py   # 手机 WebSocket 服务器
│   └── ...
├── simlife/                 # 虚拟生活模拟
├── ui/                      # PyQt6 桌面 UI
├── vrm_module/              # 3D VRM 虚拟形象
├── web/                     # 手机端 Web 客户端
├── server.py                # FastAPI REST 服务（端口 18765）
├── web_server.py            # WebSocket 聊天服务（端口 18767）
├── wecom_bot.py             # 企业微信智能机器人
├── main.py                  # 桌面应用入口
└── server_start.py          # 独立服务器入口
```

---

## 🚀 快速开始

```bash
# 克隆仓库
git clone <本仓库地址>
cd <仓库目录名>

# 安装依赖
pip install -r requirements.txt

# 启动桌面应用
python main.py
```

启动后你将看到：

1. **桌面 GUI** — 主聊天窗口，悬浮助手窗口
2. **18765 管理后台** → `http://localhost:18765`
3. **18767 网页聊天** → `http://localhost:18767`
4. 在 GUI 设置中配置企业微信后，消息将同步到企业微信

---

## ⭐ 支持项目

如果 workingAgent 让你觉得"这才是 AI 工作助手本该有的样子"——给它一颗星。这颗星比你想的更重要。

---

## 📜 许可证

Apache-2.0 © 2025 — 用热爱构建，不为资本。

---

<p align="center">
  <i>"复杂的事情，如果有没有、能不能只要它足够自动就好了。"</i>
</p>