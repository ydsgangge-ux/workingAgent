<p align="center">
  <img src="https://img.shields.io/badge/workingAgent-Work%20Agent-neon?style=for-the-badge&logo=openai&logoColor=white&labelColor=0d1117&color=00f0ff" alt="workingAgent">
</p>

<p align="center">
  <b>This isn't a chatbot. It's a work partner that gets your jobs done, one by one.</b>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/License-Apache_2.0-blue" alt="License">
  <img src="https://img.shields.io/badge/Platform-Win%20%7C%20Linux%20%7C%20macOS-lightgrey" alt="Platform">
  <img src="https://img.shields.io/badge/LLMs-12%20Providers-purple" alt="LLMs">
  <img src="https://img.shields.io/badge/Tools-30+-orange" alt="Tools">
  <img src="https://img.shields.io/badge/WeCom-Integrated-success" alt="WeCom">
  <img src="https://img.shields.io/badge/Status-Active-success" alt="Status">
</p>

<p align="center">
  <a href="README.md">简体中文</a> | English
</p>

---

## 📹 Demo Video

<video src="https://github.com/user-attachments/assets/a98dfdc5-9a1f-42cf-9d2d-47a40bcc7606" controls width="100%" style="max-width:720px;border-radius:12px;"></video>

> Full version: [Download demo video](https://github.com/user-attachments/assets/a98dfdc5-9a1f-42cf-9d2d-47a40bcc7606)

---

## 🖼️ Screenshots

| Admin Panel (18765) | Web Chat (18767) | WeCom Integration |
|:---:|:---:|:---:|
| ![Admin](docs/screenshots/18765.png) | ![Chat](docs/screenshots/18767.png) | ![WeCom](docs/screenshots/qiyeweixing.jpg) |

---

<p align="center">
  <i>Not a chatbot, not a copilot, not "I'll write a few lines for you."<br>
  workingAgent remembers your work, picks up your tasks —<br>
  organizes files, drafts plans, produces documents, tracks to-dos,<br>
  responds across devices, and hands back results, done properly.</i>
</p>

---

## 🧠 What is workingAgent?

workingAgent is a **desktop agent built to get the job done**. It separates **thinking** from **execution**: you give it a goal, it breaks it into steps and completes them, then comes back with real results instead of empty talk.

It remembers your work and project context. It can actually operate your computer, process office documents, schedule timed tasks, and stay on call across the desktop, the web, your phone, and WeChat Work.

| Layer | Role | Description |
|-------|------|-------------|
| **A-Layer** (Cognition) | Memory · Judgment · Personality | Remembers your work and project context. Decides *what* to do and *how*. |
| **B-Layer** (Executor) | Tools · LLM · Code | Calls 30+ tools, writes code, generates documents, controls devices. Gets it *done*. |

This "separation of thinking and execution" is what sets workingAgent apart from ordinary AI assistants — it doesn't stop after answering, it **thinks, plans, then actually does the work**.

---

## ✨ Core Capabilities

### 🧠 Memory Designed for Work

A three-tier hierarchical memory system modeled after human cognition — all centered around your work:

- **Summary Layer** — Semantic outlines of everything you've discussed (10,000+ entries)
- **Outline Layer** — Structured abstracts with tags
- **Detail Layer** — Full conversation fragments with associative links

Memories are **importance-weighted** — key milestones are recalled more vividly. The associative network links related work across time, so project context survives across sessions and restarts.

### 👁️ Visual Perception

Through your phone's camera or RTSP cameras, it captures scenes and stores them with GPS, timestamps, and semantic descriptions — for inspection, recording, and comparison. It can:

- Recognize faces and remember people
- Describe what's in front of the camera
- Compare current and past scenes
- Search visual memories by description ("show me the living room from last Tuesday")

### 🛠️ 30+ Built-in Tools — Actually Getting Work Done

workingAgent can really **do** things, not just "say":

| Category | Tools |
|----------|-------|
| **File System** | Read, write, search, delete, list directories |
| **Web** | Search, fetch URLs, extract articles, browse |
| **System** | Run commands, execute Python, clipboard, system info |
| **Office** | Word, Excel, PowerPoint, PDF generation & parsing |
| **Finance** | Stock quotes, search symbols, news headlines |
| **Image** | AI image generation (ComfyUI SDXL/NoobAI or pollinations.ai) |
| **Smart Home** | Home Assistant integration — lights, AC, curtains, coffee machine |
| **Desktop** | Screenshot, OCR, mouse/keyboard control, app launching |

### ⏰ Scheduled Tasks — Unattended Automation

Beyond conversation, workingAgent can run on a schedule:

- Set reminders and tasks in natural language: `remind me about the meeting at 3pm`, `auto-compile yesterday's daily report every morning`
- Trigger tools on schedule and deliver results to you on time
- Runs in the background and finishes work even when you're away

> Note: The scheduled-task scheduler builds on existing capabilities; it's a module to be refined.

### 📈 Growth Engine — Learns Your Workflow

Every task changes workingAgent. The **Growth Engine** continuously:

- **Accumulates process experience** based on your interaction patterns
- **Generalizes reusable methods** from completed tasks
- **Deduplicates and merges** cognitions into long-term memory that persists across restarts

The more you use it, the better it knows your way of working — and the more it can pick up similar tasks on its own.

### 🎙️ 12 LLM Backends

One brain, many cores. Choose your provider:

| Provider | Best For |
|----------|----------|
| **DeepSeek** | Deep reasoning, 64K context |
| **OpenAI** | GPT-4o / GPT-4o-mini |
| **Claude** | Long-form analysis, extended thinking |
| **Gemini** | Google's multimodal model |
| **Groq** | Ultra-fast inference |
| **Qwen / Zhipu / Doubao / Kimi** | Chinese-optimized models |
| **Baidu / SparkDesk** | Enterprise Chinese |
| **Ollama** | 100% local, offline, private |

### 👤 VRM 3D Avatar

A living holographic avatar that reflects the current state:

- 20 emotion mappings to facial expressions
- Breathing and blinking animations
- Lip-sync during speech
- Holographic visual style
- Supports VRM 0.x and 1.0 models

### 🌐 Dual-Port Web Management — One Brain, Everywhere

workingAgent provides multi-device access without installing any client:

**Port 18765 — Admin Panel**
- Chat, personality settings, hardware configuration
- Memory browser with user/level/modality filtering
- System settings, user registration & permission management
- AMap GPS geocoding configuration

**Port 18767 — Web Chat**
- WebSocket real-time chat with image support
- Shares the same personality and memory as desktop
- Mobile/tablet responsive interface
- Assign tasks from anywhere

### 💼 WeCom (WeChat Work) Integration

Integrated with WeCom smart robots to bring work instructions straight to your chat window:

- **Persistent connection** — real-time messaging without polling
- **5-second timeout protection** — dual-stage placeholder + final response
- **Multi-user identification** — automatically distinguishes users
- **Auto-reconnect** — recovers within 5 seconds after disconnection
- Simply create a smart bot in the WeCom admin console, fill in BotID and Secret

### 🤖 Hardware Extension — Connect the Agent to the Physical World

workingAgent isn't confined to the screen. It can inhabit physical hardware through a modular sensor bridge:

**Robot Dog / Robot Arm (MQTT)**
- Built-in **Sensor Agent** with real-time MQTT telemetry
- Monitors battery, IMU, motor temperature, joint angles, GPS, ultrasonic distance, obstacle detection
- Anomaly alerts: low battery, motor stall, overheat, obstacles within 30cm
- Supports `robot_dog`, `robot_arm`, and `custom` profiles; mock mode for development without hardware

**Xiaozhi ESP32 Voice Terminal**
- WebSocket server for ESP32-based voice devices
- Full duplex: STT → A-Layer processing → TTS → device playback
- Opus audio codec for low-latency wireless voice, wake-word listening

**Phone as Mobile Sensor Array**
- Camera: RTSP + IP Webcam live feed
- Microphone: remote audio capture
- Sensors: GPS, battery, light, accelerometer
- State machine: standby / dialog / task modes with automatic switching

### 🧑 Multi-User Identity

Multi-engine face recognition (InsightFace / face_recognition / OpenCV) for multi-user identity. workingAgent knows who's talking to it and maintains per-user memory and context.

---

## 📦 Installation

### Windows One-Click

```bash
# 1. Install Python 3.10+ from python.org (check "Add to PATH")
# 2. Double-click install.bat
# 3. Double-click launch.bat
# 4. Configure your LLM API Key in GUI Settings
# 5. Start getting work done!
```

### Manual

```bash
git clone <this-repo-url>
cd <repo-dir-name>
pip install -r requirements.txt
cp ha_config.example.json ha_config.json
python main.py
```

### Server Deployment (Standalone)

```bash
pip install -r requirements_server.txt
python server_start.py

# Open:
# http://localhost:18765  — Admin panel
# http://localhost:18767  — Web chat
```

### 📹 Configuring a Network Camera (where to get the RTSP URL)

Camera perception depends on the `rtsp_url` field in `ha_config.json`. **RTSP is the standard streaming protocol for IP cameras.** Format:

```
rtsp://username:password@camera_IP:port/stream_path
```

Default path examples by brand (username/password = your camera's admin credentials):

| Brand | Full RTSP example |
|-------|-------------------|
| Hikvision | `rtsp://admin:password@192.168.1.10:554/Streaming/Channels/101` |
| Dahua | `rtsp://admin:password@192.168.1.10:554/cam/realmonitor?channel=1&subtype=0` |
| Generic ONVIF | `rtsp://admin:password@192.168.1.10:554/h264/ch1/main/av_stream` |

**Not sure about the path? Two easiest ways:**

1. **Verify with your phone (recommended):** Install the free **VLC** player (phone or PC) and paste a guessed URL into it. If it plays, the address is correct — then put it into `rtsp_url`.
2. **Read the camera manual / use ONVIF tools:** Search "[your brand] RTSP URL", or install ONVIF Device Manager to auto-discover the streams your camera supports.

**Key prerequisites (the most common pitfalls):**
- The camera and the device running workingAgent **must be on the same LAN** and able to ping each other.
- RTSP **default port 554** must be allowed through router/firewall.
- Some cameras have **RTSP disabled by default** — enable "RTSP" in the camera's web admin first.

Once configured, set the RTSP URL in the **📱 Hardware** tab of the admin panel, or edit `rtsp_url` in `ha_config.json` and restart.

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────┐
│                  workingAgent Core                   │
│                                                      │
│  ┌──────────────┐    ┌──────────────────────────┐   │
│  │  A-Layer     │    │       B-Layer            │   │
│  │ Memory·Judge │◄──►│  LLM Client (12 backends)│   │
│  │ ·Personality │    │  Tool Executor (30+ tools)│   │
│  │ Experience   │    │  Code Generator           │   │
│  └──────┬───────┘    └──────────┬───────────────┘   │
│         │                       │                    │
│  ┌──────▼───────────────────────▼───────────────┐   │
│  │              Memory System                    │   │
│  │  Summary → Outline → Detail (3-tier)         │   │
│  │  Importance Weighting + Associative Network   │   │
│  └──────────────────────┬───────────────────────┘   │
│                         │                            │
│  ┌──────────────────────▼───────────────────────┐   │
│  │           Growth Engine                       │   │
│  │  Experience + Workflow Gists + Long-term      │   │
│  └──────────────────────────────────────────────┘   │
│                                                      │
│  ┌──────────┐ ┌──────────┐ ┌──────────────────┐    │
│  │ Multi-End │ │ VRM 3D   │ │ Hardware Bridge   │    │
│  │ Desktop/  │ │ Avatar   │ │ Phone/ESP32/HA   │    │
│  │ Web/WeCom │ │          │ │                   │    │
│  └──────────┘ └──────────┘ └──────────────────┘    │
└─────────────────────────────────────────────────────┘
```

## 📁 Project Structure

```
workingAgent/
├── engine/                  # Cognition & execution core
├── hardware/                # Hardware integration
├── simlife/                 # Virtual life simulation
├── ui/                      # PyQt6 desktop UI
├── vrm_module/              # 3D VRM avatar
├── web/                     # Mobile web client
├── server.py                # FastAPI REST server (port 18765)
├── web_server.py            # WebSocket chat server (port 18767)
├── wecom_bot.py             # WeCom smart robot
├── main.py                  # Desktop app entry
└── server_start.py          # Standalone server entry
```

---

## 🚀 Quick Start

```bash
git clone <this-repo-url>
cd <repo-dir-name>
pip install -r requirements.txt
python main.py
```

After launching, you will see:

1. **Desktop GUI** — Main chat window, floating assistant
2. **18765 Admin Panel** → `http://localhost:18765`
3. **18767 Web Chat** → `http://localhost:18767`
4. Configure WeCom in GUI settings to sync messages to WeChat Work

---

## ⭐ Support

If workingAgent makes you think "this is what AI work assistants should have been from the start" — give it a star. It helps more than you know.

---

## 📜 License

Apache-2.0 © 2025 — Built with obsession, not corporate backing.

---

<p align="center">
  <i>"If something can be automated, it should be."</i>
</p>