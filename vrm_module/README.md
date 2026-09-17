# VRM 虚拟形象模块

## 使用说明

### 1. 安装依赖

```bash
pip install PyQt6-WebEngine
```

### 2. 放置 VRM 模型

将你的 `.vrm` 模型文件放到本目录的 `static/` 文件夹，重命名为 `model.vrm`。

推荐获取方式：
- **VRoid Studio**（免费）: https://vroid.com/studio → 捏脸 → 导出 .vrm
- **VRoid Hub**: https://hub.vroid.com → 下载标注"可商用"的免费模型

### 3. Three.js 依赖

模块会按以下顺序尝试加载：
1. `static/three.min.js` + `static/three-vrm.min.js`（本地文件，离线可用）
2. 如果本地不存在，自动从 CDN 加载

手动下载（可选，用于离线）：
- Three.js: https://threejs.org/build/three.min.js
- three-vrm: https://github.com/pixiv/three-vrm → Releases → three-vrm.min.js

### 4. 配置

在 `config.json` 中可控制：

```json
{
  "vrm_enabled": true,
  "vrm_width": 220,
  "vrm_height": 220
}
```

- `vrm_enabled`: 一键开关，设为 `false` 隐藏 VRM 面板
- `vrm_width` / `vrm_height`: 面板尺寸（像素）

### 5. 优雅降级

以下情况自动静默跳过，不影响主程序：
- PyQt6-WebEngine 未安装
- VRM 模型文件缺失
- Three.js 加载失败
- WebGL 不支持
- config 中 `vrm_enabled: false`

## 架构

```
vrm_module/
├── __init__.py           # 安全加载入口（异常全拦截）
├── vrm_widget.py         # PyQt6 QWebEngineView 组件
├── emotion_bridge.py     # 情绪 → VRM BlendShape 映射
└── static/
    ├── vrm_viewer.html   # Three.js 渲染页面
    ├── model.vrm         # ← 你需要放一个模型文件在这里
    ├── three.min.js      # (可选) 本地 Three.js
    └── three-vrm.min.js  # (可选) 本地 three-vrm
```

## 情绪映射

| AGI-DPA 情绪 | VRM 表情 | 强度 |
|---|---|---|
| happy / love | happy | 0.8~1.0 |
| sad / anxious | sad | 0.5~0.7 |
| angry | angry | 0.6 |
| surprised / curious | surprised | 0.4~1.0 |
| neutral / calm | neutral | 0.8~1.0 |
