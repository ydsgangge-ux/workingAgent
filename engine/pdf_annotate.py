# -*- coding: utf-8 -*-
"""
pdf_annotate.py — PDF 逐页截图 + 精准标注（Hermes 同级能力）
依赖：fitz(PyMuPDF)、pdfplumber、Pillow（均已安装，无需新依赖）。

核心思路：
  - fitz 把每页渲染成高清 PNG；
  - pdfplumber 取"词级坐标"（与渲染坐标系一致，缩放=dpi/72）；
  - 在命中文本所在的那一行/词跨上，用 PIL 画半透明圆角高亮框 + 编号/标签色块；
  - 每张图顶部加信息条（标题 + 第 N 页 / 共 M 页）。
这样"精准标注"基于坐标精确计算，而非让大模型猜像素。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


# ── 主题（与 PPT 主题保持一致：深蓝 + 活力橙）─────────────────────
_PRIMARY = (0x1F, 0x4E, 0x79)
_ACCENT = (0xE8, 0x77, 0x2E)
_BAND_BG = _PRIMARY
_BAND_TXT = (255, 255, 255)
_HILITE = (0xE8, 0x77, 0x2E, 96)          # 半透明橙高亮
_CHIP_BG = (0x1F, 0x4E, 0x79, 200)        # 标签底色


def _font(size: int):
    """取一个可渲染中文的字体；找不到则回退默认字体。"""
    from PIL import ImageFont
    for fp in ("C:/Windows/Fonts/msyh.ttc",
               "C:/Windows/Fonts/simhei.ttf",
               "/System/Library/Fonts/PingFang.ttc"):
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _normalize(s: str) -> str:
    """去空白与大小写，用于宽松匹配。"""
    return re.sub(r"\s+", "", s).lower()


def _line_span(page_words: List[dict], target: str) -> Optional[dict]:
    """在某一页词列表中，找到包含 target 的那一行/词跨的包围盒。

    page_words: pdfplumber page.extract_words() 的结果。
    返回最贴近 target 的连续词跨的 {x0,y0(top),x1,y1(bottom)}；找不到返回 None。
    """
    if not page_words:
        return None
    target_n = _normalize(target)
    if not target_n:
        return None
    # 按 top 升序、再 x0 升序排列，模拟阅读顺序
    words = sorted(page_words, key=lambda w: (round(w["top"]) , w["x0"]))
    i = 0
    n = len(words)
    while i < n:
        # 从每个词开始尝试向后累加，看能否拼出目标
        acc = _normalize(words[i]["text"])
        j = i
        while j < n:
            if target_n in acc:
                # 命中：返回构成该段的词跨合并框
                seg = words[i:j + 1]
                return {
                    "x0": min(w["x0"] for w in seg),
                    "top": min(w["top"] for w in seg),
                    "x1": max(w["x1"] for w in seg),
                    "bottom": max(w["bottom"] for w in seg),
                }
            if n == j + 1 or words[j + 1]["top"] - words[j]["bottom"] > 2:
                break  # 换行
            j += 1
            acc += _normalize(words[j]["text"])
        i += 1
    # 兜底：退化为行级匹配（对 target 做行内包含）
    rows: Dict[float, List[dict]] = {}
    for w in words:
        rows.setdefault(round(w["top"]), []).append(w)
    for ws in rows.values():
        row_text = _normalize("".join(w["text"] for w in ws))
        if target_n in row_text:
            return {
                "x0": min(w["x0"] for w in ws),
                "top": min(w["top"] for w in ws),
                "x1": max(w["x1"] for w in ws),
                "bottom": max(w["bottom"] for w in ws),
            }
    return None


def _parse_pages(pages: str, page_count: int) -> List[int]:
    """把 'all' / '1,3-5' / '2' 解析为 1-based 页号列表（升序、去重、越界裁剪）。"""
    if not pages or str(pages).strip().lower() in ("all", "*", ""):
        return list(range(1, page_count + 1))
    out: List[int] = []
    for part in str(pages).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                start, end = int(a), int(b)
            except ValueError:
                continue
            out.extend(range(min(start, end), max(start, end) + 1))
        else:
            try:
                out.append(int(part))
            except ValueError:
                continue
    return sorted({p for p in out if 1 <= p <= page_count})


def render_pages(pdf_path: str, out_dir: str, dpi: int = 150,
                 title: str = "") -> Dict[str, Any]:
    """把 PDF 每页渲染成 PNG，顶部加信息条。返回 {ok, images:[{page,path,width,height}]}"""
    try:
        return annotate_pdf(pdf_path, out_dir, marks=None, title=title,
                            dpi=dpi, page_nums=None)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def annotate_pdf(pdf_path: str, out_dir: str, marks: Optional[List[dict]] = None,
                 title: str = "", dpi: int = 150,
                 page_nums: Optional[List[int]] = None) -> Dict[str, Any]:
    """
    渲染 PDF 指定页为 PNG，并按 marks 在命中的文字上做精准标注。
      marks: [{"page": 1-based, "text": 要框住的文字, "label": 可选说明}]
      page_nums: 只渲染这些页（None/空 = 全部）
    返回 {ok, images:[绝对路径], count}
    """
    try:
        import fitz          # PyMuPDF
        import pdfplumber
        from PIL import Image, ImageDraw

        pdf_path = os.path.expanduser(pdf_path)
        if not os.path.exists(pdf_path):
            return {"ok": False, "error": f"PDF 文件不存在：{pdf_path}"}

        os.makedirs(out_dir, exist_ok=True)
        doc = fitz.open(pdf_path)
        page_count = doc.page_count
        scale = dpi / 72.0

        targets = page_nums or _parse_pages("all", page_count)
        targets = [p for p in targets if 1 <= p <= page_count]
        if not targets:
            return {"ok": False, "error": "没有可处理的页面"}

        base = Path(pdf_path).stem
        ttl = title or base
        # page -> list of {text,label}
        by_page: Dict[int, List[dict]] = {}
        for m in (marks or []):
            if not isinstance(m, dict):
                continue
            pg = int(m.get("page", 0))
            if m.get("text"):
                by_page.setdefault(pg, []).append(m)

        images: List[str] = []
        with pdfplumber.open(pdf_path) as pdf:
            for pno in targets:
                page = doc[pno - 1]
                pix = page.get_pixmap(dpi=dpi)
                page_img = Image.frombytes("RGB", (pix.width, pix.height),
                                           pix.samples).convert("RGBA")

                # 顶部信息条高度
                band = max(40, int(page_img.height * 0.07))
                canvas = Image.new("RGBA", (page_img.width, page_img.height + band),
                                   (255, 255, 255, 255))
                d = ImageDraw.Draw(canvas)
                d.rectangle([0, 0, canvas.width, band], fill=_BAND_BG + (255,))
                f_big, f_small = _font(int(band * 0.42)), _font(int(band * 0.30))
                d.text((14, band * 0.12), ttl, fill=_BAND_TXT + (255,), font=f_big)
                meta = f"第 {pno} 页 / 共 {page_count} 页"
                tw = d.textlength(meta, font=f_small)
                d.text((canvas.width - tw - 14, band * 0.34), meta,
                       fill=_BAND_TXT + (255,), font=f_small)
                canvas.alpha_composite(page_img, (0, band))

                # 本页标注
                annotations = []
                if pno in by_page:
                    page_words = pdf.pages[pno - 1].extract_words() or []
                    for mi, m in enumerate(by_page[pno], 1):
                        span = _line_span(page_words, m["text"])
                        if span:
                            box = (
                                span["x0"] * scale,
                                span["top"] * scale + band,
                                span["x1"] * scale,
                                span["bottom"] * scale + band,
                            )
                            annotations.append((box, str(mi), m.get("label", "")))

                pad = max(3, int(scale * 1.5))
                for box, num, label in annotations:
                    x0, y0, x1, y1 = box
                    d.rounded_rectangle(
                        [x0 - pad, y0 - pad, x1 + pad, y1 + pad],
                        radius=max(4, int(scale * 2)), fill=_HILITE,
                        outline=_ACCENT + (255,), width=max(2, int(scale * 1.2)))
                    # 标签色块：放高亮框上方，贴近顶部则夹到顶（不越界）
                    cx0, cy0 = x0 - pad, y0 - pad
                    chip_f = _font(int(scale * 11))
                    lw = d.textlength(num, font=chip_f) + 10
                    cx1 = cx0 + lw + 8
                    cy1 = max(0, cy0 - int(scale * 22))
                    d.rounded_rectangle([cx0, cy1, cx1, y0 - pad],
                                        radius=4, fill=_CHIP_BG)
                    cx, cy = cx0 + 5, (cy1 + (y0 - pad)) / 2 - int(scale * 6)
                    d.text((cx, cy), num, fill=(255, 255, 255, 255), font=chip_f)
                    if label:
                        lb_f = _font(int(scale * 11))
                        lb_w = d.textlength(label, font=lb_f) + 12
                        lx0 = cx1 + 6
                        lx1 = min(canvas.width - 6, lx0 + lb_w)
                        d.rounded_rectangle([lx0, cy1, lx1, y0 - pad], radius=4,
                                            fill=_ACCENT + (230,))
                        d.text((lx0 + 6, (cy1 + (y0 - pad)) / 2 - int(scale * 6)),
                               label, fill=(255, 255, 255, 255), font=lb_f)

                out_path = os.path.join(out_dir, f"{base}_p{pno}.png")
                canvas.convert("RGB").save(out_path, "PNG")
                images.append(out_path)

        doc.close()
        return {"ok": True, "images": images, "count": len(images),
                "path": images[0] if images else ""}
    except ImportError as e:
        return {"ok": False, "error": f"缺少依赖：{e}（请先安装 PyMuPDF / pdfplumber / Pillow）"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        r = annotate_pdf(sys.argv[1], "downloads",
                         marks=[{"page": 1, "text": "小南", "label": "标题"}],
                         title="标注测试")
        print(r)