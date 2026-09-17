"""从国内镜像下载 sentence-transformers 模型
使用方法：
  本机：      python _download_model.py
  云服务器：  python _download_model.py

如果网络访问 huggingface.co 有问题，本脚本会自动使用 hf-mirror.com 镜像。
首次下载约 470MB，需要几分钟。
"""
import os, time, threading

os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
# 开启 huggingface_hub 详细日志，显示进度条
os.environ["HF_HUB_VERBOSITY"] = "info"
os.environ["TRANSFORMERS_VERBOSITY"] = "info"

HUB_REPO  = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
CACHE_DIR = os.path.expanduser(
    "~/.cache/huggingface/hub/models--sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2"
)

# ── 后台线程：每隔几秒报一下缓存大小 ──
_stop_monitor = False

def _monitor_cache():
    while not _stop_monitor:
        time.sleep(8)
        if os.path.isdir(CACHE_DIR):
            total = 0
            for dirpath, _, filenames in os.walk(CACHE_DIR):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    try:
                        total += os.path.getsize(fp)
                    except OSError:
                        pass
            mb = total / (1024 * 1024)
            print(f"  ⏳ 已下载: {mb:.1f} MB  ({'还早' if mb < 50 else '快要好了' if mb < 400 else '即将完成'})")
        else:
            print(f"  ⏳ 正在创建缓存目录...")

# ── 正式开始 ──
print("=" * 56)
print("  语义向量模型下载工具")
print("=" * 56)
print(f"  模型:     {HUB_REPO}")
print(f"  镜像源:   hf-mirror.com")
print(f"  大小:     ~470MB")
print(f"  缓存目录: {CACHE_DIR}")
print()

# 先检查是否已下载完成
if os.path.isdir(CACHE_DIR):
    total = 0
    for dirpath, _, filenames in os.walk(CACHE_DIR):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    mb = total / (1024 * 1024)
    if mb > 400:
        print(f"  ✅ 模型似乎已下载 ({mb:.0f} MB)，跳过下载，直接加载...")
        print()
        from sentence_transformers import SentenceTransformer
        m = SentenceTransformer(HUB_REPO, local_files_only=True)
        print(f"\n[OK] 加载完成！向量维度: {m.get_sentence_embedding_dimension()}")
        vec = m.encode("你好世界", normalize_embeddings=True)
        print(f"[OK] 编码测试通过")
        exit(0)

print("  ⏳ 正在从 hf-mirror.com 下载（进度条在下方）...")
print("  ⏳ 首次下载需要几分钟，请耐心等待")
print()

# 启动缓存监控
t = threading.Thread(target=_monitor_cache, daemon=True)
t.start()

try:
    from sentence_transformers import SentenceTransformer
    m = SentenceTransformer(HUB_REPO)
    _stop_monitor = True
    print()
    print(f"[OK] 下载完成！")
    print(f"[OK] 向量维度: {m.get_sentence_embedding_dimension()}")
    print(f"[OK] 设备: {m.device}")

    # 编码测试
    vec = m.encode("你好世界", normalize_embeddings=True)
    print(f"[OK] 编码测试通过")
    print()
    print("现在重启系统即可使用语义向量检索。")
except Exception as e:
    _stop_monitor = True
    print(f"\n[错误] 下载失败: {e}")
    print()
    print("可能的原因和解决办法：")
    print("  1. 网络无法访问 hf-mirror.com → 手动下载模型文件放到缓存目录")
    print("  2. 磁盘空间不足 → 检查 C 盘剩余空间（需 >1GB）")
    print("  3. 魔法上网冲突 → 关闭代理后再试")
    print()
    print("不影响系统运行，会自动降级为关键词匹配模式。")
    exit(1)