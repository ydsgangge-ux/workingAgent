"""
ComfyUI 自动检测与配置工具
双击运行即可自动检测 ComfyUI 安装路径、端口、模型，并写入配置。
无需手动修改任何代码。
"""

import sys
import os
import json
import glob
import urllib.request
import urllib.error
from pathlib import Path


# ── 配置文件路径 ─────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent

# 数据目录（与 desktop/config.py 保持一致）
if sys.platform == "win32":
    DATA_ROOT = Path(os.environ.get("APPDATA", str(Path.home()))) / "workingAgent"
else:
    DATA_ROOT = Path.home() / ".workingagent"

DATA_ROOT.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = DATA_ROOT / "config.json"


# ── 常见 ComfyUI 安装路径 ────────────────────────────
SEARCH_PATHS = []

if sys.platform == "win32":
    # 盘符 C-Z
    for drive in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        base = f"{drive}:\\"
        if os.path.isdir(base):
            SEARCH_PATHS.extend([
                f"{base}ComfyUI_windows_portable",
                f"{base}ComfyUI",
                f"{base}Tools\\ComfyUI",
                f"{base}AI\\ComfyUI",
                f"{base}AI\\ComfyUI_windows_portable",
                f"{base}Programs\\ComfyUI",
                f"{base}Users\\{os.environ.get('USERNAME', '')}\\ComfyUI",
                f"{base}Users\\{os.environ.get('USERNAME', '')}\\Desktop\\ComfyUI_windows_portable",
            ])
elif sys.platform == "darwin":
    SEARCH_PATHS.extend([
        str(Path.home() / "ComfyUI"),
        str(Path.home() / "Applications" / "ComfyUI"),
        "/Applications/ComfyUI",
    ])
else:
    SEARCH_PATHS.extend([
        str(Path.home() / "ComfyUI"),
        str(Path.home() / "ai" / "ComfyUI"),
        "/opt/ComfyUI",
    ])


def print_header(title: str):
    width = 50
    print()
    print("=" * width)
    print(f"  {title}")
    print("=" * width)


def print_step(msg: str):
    print(f"  [·] {msg}")


def print_ok(msg: str):
    print(f"  [OK] {msg}")


def print_fail(msg: str):
    print(f"  [X]  {msg}")


def print_warn(msg: str):
    print(f"  [!] {msg}")


def find_comfyui_install() -> list:
    """搜索本机所有可能的 ComfyUI 安装路径"""
    print_header("第一步：检测 ComfyUI 安装路径")
    found = []

    for p in SEARCH_PATHS:
        path = Path(p)
        if not path.exists():
            continue

        # 检测特征：存在 main.py 或 run_nvidia_gpu.bat 等
        indicators = ["main.py", "run_nvidia_gpu.bat", "run_cpu.bat",
                       "update", "ComfyUI"]
        has_indicator = any((path / ind).exists() for ind in indicators)

        if has_indicator:
            found.append(path)
            print_ok(f"找到: {path}")

    # 额外：搜索 ComfyUI_windows_portable 的特征目录
    if not found:
        print_step("在常见位置未找到，尝试扩大搜索...")
        for drive in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            base = Path(f"{drive}:\\") if sys.platform == "win32" else Path("/")
            if not base.exists():
                continue
            try:
                for d in base.glob("*/ComfyUI_windows_portable"):
                    if d.exists() and not d in found:
                        found.append(d)
                        print_ok(f"找到: {d}")
            except PermissionError:
                pass

    if not found:
        print_fail("未自动检测到 ComfyUI 安装。")

    return found


def detect_output_dir(comfyui_root: Path) -> str:
    """检测 output 目录"""
    candidates = [
        comfyui_root / "output",
        comfyui_root / "ComfyUI" / "output",
    ]
    for c in candidates:
        if c.is_dir():
            return str(c)

    # 兜底：创建 output 目录
    fallback = comfyui_root / "output"
    try:
        fallback.mkdir(parents=True, exist_ok=True)
        return str(fallback)
    except Exception:
        return str(fallback)


def detect_comfyui_port() -> int:
    """尝试检测 ComfyUI 正在使用的端口"""
    common_ports = [8188, 8189, 8000, 8080, 8888]

    for port in common_ports:
        try:
            url = f"http://127.0.0.1:{port}/system_stats"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    return port
        except (urllib.error.URLError, OSError, Exception):
            continue

    return 8188  # 默认


def detect_models(comfyui_root: Path) -> list:
    """检测可用的 checkpoint 模型"""
    model_dirs = [
        comfyui_root / "ComfyUI" / "models" / "checkpoints",
        comfyui_root / "models" / "checkpoints",
        comfyui_root / "ComfyUI" / "models" / "unet",
        comfyui_root / "models" / "unet",
    ]

    models = []
    for d in model_dirs:
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if f.suffix.lower() in (".safetensors", ".pt", ".pth", ".ckpt", ".bin"):
                models.append({
                    "name": f.name,
                    "size_mb": round(f.stat().st_size / 1024 / 1024, 1),
                    "dir": str(d),
                })

    return models


def check_workflow_model() -> str:
    """检查 workflow_api.json 里指定的模型"""
    wf_path = SCRIPT_DIR / "workflow_api.json"
    if not wf_path.exists():
        return ""

    try:
        data = json.loads(wf_path.read_text(encoding="utf-8"))
        for node in data.values():
            if isinstance(node, dict):
                cls = node.get("class_type", "")
                if cls == "CheckpointLoaderSimple":
                    return node.get("inputs", {}).get("ckpt_name", "")
    except Exception:
        pass

    return ""


def save_config(comfyui_url: str, comfyui_output: str, comfyui_model: str = "", comfyui_style: str = ""):
    """将 ComfyUI 配置写入 config.json"""
    cfg = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    cfg["comfyui_url"] = comfyui_url
    cfg["comfyui_output"] = comfyui_output
    if comfyui_model:
        cfg["comfyui_model"] = comfyui_model
    if comfyui_style:
        cfg["comfyui_style"] = comfyui_style
    else:
        cfg.pop("comfyui_style", None)

    CONFIG_FILE.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print_ok(f"配置已保存: {CONFIG_FILE}")


def main():
    print()
    print("  ComfyUI 自动检测与配置工具")
    print("  双击运行即可，无需手动改代码")
    print()

    # 1. 检测 ComfyUI 是否正在运行
    print_header("第零步：检测 ComfyUI 是否运行中")
    port = detect_comfyui_port()
    if port != 8188:
        try:
            url = f"http://127.0.0.1:{port}/system_stats"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    print_ok(f"ComfyUI 正在运行，端口: {port}")
                else:
                    print_warn(f"端口 {port} 有响应但非 ComfyUI")
        except (urllib.error.URLError, OSError):
            if port == 8188:
                print_warn("ComfyUI 似乎未运行（这不影响配置，配置好后启动即可）")
            else:
                print_warn("ComfyUI 似乎未运行（这不影响配置，配置好后启动即可）")

    # 2. 搜索安装路径
    found = find_comfyui_install()

    if not found:
        print()
        print_warn("自动检测未找到 ComfyUI，你可以手动输入路径。")
        print("  输入 ComfyUI 安装目录的完整路径（或直接回车使用默认值）:")
        print(f"  默认: C:\\ComfyUI_windows_portable")
        manual = input("  路径: ").strip().strip('"')
        if manual:
            found = [Path(manual)]
        else:
            found = [Path("C:\\ComfyUI_windows_portable")]

    # 3. 选择（如果有多个）
    chosen = found[0]
    if len(found) > 1:
        print()
        print("  检测到多个安装，请选择:")
        for i, p in enumerate(found):
            print(f"    {i+1}. {p}")
        try:
            idx = int(input(f"  请输入编号 (1-{len(found)})，默认1: ").strip() or "1") - 1
            chosen = found[max(0, min(idx, len(found) - 1))]
        except (ValueError, KeyboardInterrupt):
            chosen = found[0]

    print()
    print_ok(f"使用路径: {chosen}")

    # 4. 检测 output 目录
    print_header("第二步：检测 output 目录")
    output_dir = detect_output_dir(chosen)
    print_ok(f"Output 目录: {output_dir}")

    # 5. 检测可用模型
    print_header("第三步：检测可用模型")
    models = detect_models(chosen)
    if models:
        for m in models:
            print_ok(f"  {m['name']} ({m['size_mb']} MB)")
    else:
        print_warn("未在 models/checkpoints 目录找到模型文件")

    # 6. 检查 workflow 中的模型是否匹配
    print_header("第四步：检查 workflow 匹配")
    wf_model = check_workflow_model()
    if wf_model:
        print_step(f"workflow_api.json 指定的模型: {wf_model}")
        model_names = [m["name"] for m in models]
        if wf_model in model_names:
            print_ok("模型文件匹配")
        elif models:
            print_warn(f"模型不匹配! workflow 需要 '{wf_model}'，但未找到。")
            print("  可用模型:")
            for m in models:
                print(f"    - {m['name']}")
            print()
            print("  提示: 你可以下载 SDXL Turbo 模型到 checkpoints 目录")
            print("        或在 ComfyUI 中加载 workflow 后手动更换模型节点并导出 workflow_api.json")
        else:
            print_warn("未检测到模型，请确认已下载模型到 checkpoints 目录")
    else:
        print_warn("无法读取 workflow_api.json")

    # 8. 选择生成风格
    print_header("第五步：选择生成风格")
    print("  1. anime    - 二次元/动漫风格（追加 illustration, anime style, pixiv）")
    print("  2. realistic - 写实/照片风格（追加 photorealistic, 8k uhd, dslr）")
    print("  3. 无       - 不追加任何风格词")
    try:
        style_input = input("  请选择 (1/2/3，默认1): ").strip()
        style_map = {"1": "anime", "2": "realistic", "3": "", "anime": "anime", "realistic": "realistic"}
        comfyui_style = style_map.get(style_input, "anime")
    except (ValueError, KeyboardInterrupt):
        comfyui_style = "anime"
    style_label = {"anime": "二次元", "realistic": "写实", "": "无"}.get(comfyui_style, "未知")
    print_ok(f"生成风格: {style_label}")

    # 9. 确定端口
    print_header("第六步：确定连接端口")
    print_ok(f"端口: {port}")
    comfyui_url = f"http://127.0.0.1:{port}"

    # 10. 保存配置
    print_header("保存配置")
    comfyui_model = ""
    if models and wf_model in [m["name"] for m in models]:
        comfyui_model = wf_model

    save_config(comfyui_url, output_dir, comfyui_model, comfyui_style)

    # 11. 最终确认
    print_header("配置完成!")
    print(f"  ComfyUI URL:    {comfyui_url}")
    print(f"  Output 目录:    {output_dir}")
    print(f"  生成风格:       {style_label}")
    if comfyui_model:
        print(f"  使用模型:       {comfyui_model}")
    print()
    print("  使用前请确保:")
    print("    1. ComfyUI 已启动并加载了对应模型")
    print("    2. workflow_api.json 中的模型名与实际匹配")
    print("    3. 重启 workingAgent 使配置生效")
    print()

    input("  按 Enter 键退出...")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  已取消。")
    except Exception as e:
        print(f"\n  发生错误: {e}")
        input("\n  按 Enter 键退出...")
