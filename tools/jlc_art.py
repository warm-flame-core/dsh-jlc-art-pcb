#!/usr/bin/env python3
"""jlc-art —— 嘉立创 PCB 艺术板流水线（单文件 CLI）

把「一张图」变成「可打样的 EDA 脚本 + 成品预览」。

## 子命令

  layerize  <src> <out>             源图 → 分层掩码 + spec.json
  vectorize <out>                   掩码 → 多边形（追踪 + 平滑）
  emit      <out> <js>              多边形 → EasyEDA JS
  render    <out> <png>             多边形 → 成品效果图
  verify    <zip> [spec]            Gerber 独立校验
  pipeline  <src> <out> [--js out.js]   一次跑完 1→4

## 依赖

  必需: numpy, Pillow
  可选: 无（vtracer 是实验性替代引擎，本工具不依赖）

## 单位约定（重要）

  - 所有几何操作以 **mm** 为准
  - 写给 EDA 时转换为 **mil**（1 mm = 39.3701 mil）
  - `PrimitiveString` 的 height/width 例外，用 mm

## 已验证的常量

见下方 DEFAULT_* 常量。这些值在 Kuromi 案例上实测通过，
换图时需要重新标定（见 SKILL.md 的「换图检查清单」）。
"""

from __future__ import annotations

import json
import math
import sys
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

# ─── 默认参数（换图需重新标定）───────────────────────────────────────
NEAR_WHITE = 244          # 所有通道 >= 此值 → 近白
DARK_LINE_MAX = 90        # max(R,G,B) < 此值 → 黑线
SAT_THRESHOLD = 55        # 饱和度 > 此值 → 彩色块
SILK_CLEARANCE_MM = 0.20  # 丝印与金色开口最小间距
MIN_FEATURE_MM = 0.20     # 金色层最小特征
SILK_MIN_MM = 0.22        # 丝印最小特征
WHITE_MAX_SAT = 22        # 「白」的饱和度上限：排除皮肤高光等近白但有色的像素
DARK_BLOCK_RADIUS_MM = 0.45   # 能承受此粒度侵蚀的暗区 = 色块而非线
OUTLINE_CLOSE_MM = 0.30   # 剪影闭运算半径
EDGE_MARGIN_MM = 0.30     # 铜距板边
HOLE_DIAMETER_MM = 2.0    # 挂孔

# 区域合并半径：把邻近的碎片连成整块（对水彩/厚涂这类软边图尤其需要）
MERGE_MM = 0.45

# 半调参数：用点的疏密表现中间调（PCB 没有灰阶，这是唯一的路）
HALFTONE_CELL_MM = 0.45   # 网点单元尺寸
HALFTONE_DOT_RATIO = 0.42 # 点半径 / 单元边长

# ─── 工艺（券）档案 ──────────────────────────────────────────────────
# 两者都免费，但约束完全不同。这是 AI 与脚本共用的开关：
#   --finish enig  沉金券：金色好看，但金面积 ≤20%、仅 2-4 层、仅 1.6mm
#   --finish osp   OSP券：无面积限制、1-6 层、0.8-1.6mm，但只有铜色且需保留对比
FINISH_PROFILES = {
    "enig": {
        "name": "沉金 ENIG",
        "maxCopperRatio": 0.20,      # 免费沉金券硬门槛
        "surchargeRatio": 0.30,      # 付费订单超出部分加收
        "layers": [2, 4],
        "thicknessMm": [1.6],
        "color": "金色",
        "needsContrast": False,
        "note": "金面积 ≤20% 才能用免费沉金券；>30% 付费时超出部分加收",
    },
    "osp": {
        "name": "OSP 裸铜",
        "maxCopperRatio": 0.85,      # 没有硬限制，但超过就没对比了
        "surchargeRatio": None,
        "layers": [1, 2, 4, 6],
        "thicknessMm": [0.8, 1.0, 1.2, 1.6],
        "color": "铜色（偏红）",
        "needsContrast": True,
        "note": "无面积限制；但整板 100% 露铜会失去图案对比，需保留阻焊区域做暗部",
    },
}

# ─── 风格预设 ────────────────────────────────────────────────────────
# 每个预设 = 一组参数。这是**AI 与用户沟通风格的共同词汇**：
#   用户说"我要线稿风" → AI 选 lineart
#   用户说"要像丝网印刷那种" → AI 选 tritone
# 全部预设都保证：在单色免费路线（沉金券，金 ≤20%）下可用。
STYLE_PRESETS = {
    "tritone": {
        "engine": "tri",
        "goldPct": 18.0, "silkPct": 18.0, "halftone": False,
        "name": "三色调",
        "desc": "按亮度分三档：暗部→金、亮部→白丝印、中间调→板底色。"
                "对比强、辨识度高，适合绝大多数彩色插画。",
        "bestFor": "有明暗层次的彩色角色图",
        "avoid": "整体很平、明暗无层次的设计稿",
    },
    "lineart": {
        "engine": "outline",
        "goldPct": 0, "silkPct": 18.0, "halftone": False,
        "name": "线稿",
        "desc": "只把深色线条做成金色，白色区域做丝印，其余留板底色。"
                "最接近传统线稿/印章感。",
        "bestFor": "本来就是线稿、黑白漫画、有清晰描边的图",
        "avoid": "水彩/厚涂/无描边的图（线条会碎）",
    },
    "halftone": {
        "engine": "outline",
        "goldPct": 0, "silkPct": 16.0, "halftone": True,
        "name": "半调网点",
        "desc": "粗线做金色，中间调用点阵疏密表现（PCB 没有灰阶，只能靠点）。"
                "有印刷质感，适合想要'复古网点'效果。",
        "bestFor": "照片感、渐变多的图",
        "avoid": "小尺寸板（点会太细，看不清）",
    },
    "solid": {
        "engine": "solid",
        "goldPct": 0, "silkPct": 0, "halftone": False,
        "name": "实心剪影",
        "desc": "整个人物剪影做金色，只有亮部做白丝印。"
                "**金色接近 100%，超过沉金券 20% 上限，只能走 OSP 或付费沉金。**",
        "bestFor": "挂件、剪影、图标类",
        "avoid": "需要细节的角色图；想用免费沉金券的场景",
        "requiresGoldOver20": True,
    },
}

# 造型风格
#   solid  : 整个人物剪影做金色（沉金挂件经典效果），亮部做白丝印
#   outline: 只把细线做金色（线稿风），适合本来就以线条为主的图
DEFAULT_STYLE = "tritone"

# 平滑参数
SMOOTH_MERGE_MM = 0.12
SMOOTH_CHAIKIN = 3
SMOOTH_TOL_MM = 0.02

# 追踪参数
MIN_POLY_AREA_MM2 = 0.05
MIN_POLY_WIDTH_MM = 0.18

MM2MIL = 39.3701


# ══════════════════════════════════════════════════════════════════════
# 通用工具
# ══════════════════════════════════════════════════════════════════════

def _morph(mask: np.ndarray, r: int, grow: bool) -> np.ndarray:
    """形态学膨胀/侵蚀。r 为像素半径。"""
    if r <= 0:
        return mask
    size = min(2 * r + 1, 63)
    im = Image.fromarray((mask * 255).astype(np.uint8))
    im = im.filter(ImageFilter.MaxFilter(size) if grow else ImageFilter.MinFilter(size))
    return np.asarray(im) > 127


def _flood_from_border(mask: np.ndarray) -> np.ndarray:
    """从四边向内 BFS，返回与边界连通的像素。"""
    h, w = mask.shape
    visited = np.zeros((h, w), dtype=bool)
    q: deque[int] = deque()

    for x in range(w):
        for y in (0, h - 1):
            if mask[y, x] and not visited[y, x]:
                visited[y, x] = True
                q.append(y * w + x)
    for y in range(h):
        for x in (0, w - 1):
            if mask[y, x] and not visited[y, x]:
                visited[y, x] = True
                q.append(y * w + x)

    flat_mask = mask.ravel()
    flat_vis = visited.ravel()
    while q:
        i = q.popleft()
        y, x = divmod(i, w)
        for j, ok in (
            (i - w, y > 0), (i + w, y < h - 1),
            (i - 1, x > 0), (i + 1, x < w - 1),
        ):
            if ok and flat_mask[j] and not flat_vis[j]:
                flat_vis[j] = True
                q.append(j)
    return visited


def derive_figure(rgb: np.ndarray, alpha: np.ndarray | None) -> tuple[np.ndarray, str]:
    """判定主体像素：alpha 通道优先，白底洪水填充回退。

    抠图素材（PNG 透明底）若不用 alpha，convert("RGB") 会把透明区变黑，
    导致整张图被判成主体 —— 这是实测踩到的坑。
    """
    if alpha is not None:
        transparent = alpha < 16
        if float(transparent.mean()) > 0.05:
            return alpha >= 128, "alpha"
    whiteish = rgb.max(axis=2) >= NEAR_WHITE
    bg = _flood_from_border(whiteish)
    return ~bg, "white-background"


def _otsu(gray: np.ndarray) -> tuple[int, float, float]:
    """Otsu 阈值。返回 (阈值, 分离度, 暗像素占比)。"""
    hist, _ = np.histogram(gray, bins=256, range=(0, 256))
    total = hist.sum()
    if total == 0:
        return DARK_LINE_MAX, 0.0, 0.0
    p = hist.astype(np.float64) / total
    omega = np.cumsum(p)
    mu = np.cumsum(p * np.arange(256))
    mu_t = mu[-1]

    best_t, best_var = 0, 0.0
    for t in range(1, 255):
        w0 = omega[t]
        w1 = 1.0 - w0
        if w0 <= 0 or w1 <= 0:
            continue
        m0 = mu[t] / w0
        m1 = (mu_t - mu[t]) / w1
        var = w0 * w1 * (m0 - m1) ** 2
        if var > best_var:
            best_var, best_t = var, t

    total_var = float(np.var(gray))
    sep = (best_var / total_var) if total_var > 0 else 0.0
    dark_ratio = float((gray < best_t).mean())
    return best_t, sep, dark_ratio


def adaptive_dark_threshold(rgb: np.ndarray, figure: np.ndarray) -> tuple[int, dict]:
    """自适应暗色阈值。

    只统计主体区域的灰度（背景会污染直方图）。
    Otsu 结果只在「暗像素占比 2%~40% 且分离度 >0.3」时采用，否则回退到 DARK_LINE_MAX。
    """
    gray = (0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]).astype(np.uint8)
    sample = gray[figure] if float(figure.mean()) > 0.02 else gray.ravel()
    if len(sample) == 0:
        return DARK_LINE_MAX, {"source": "fallback"}

    t, sep, dark_ratio = _otsu(sample)
    usable = (0.02 <= dark_ratio <= 0.40) and sep > 0.30
    info = {
        "otsu": int(t),
        "separation": round(float(sep), 3),
        "darkRatio": round(dark_ratio, 4),
        "usable": bool(usable),
        "chosen": int(t) if usable else DARK_LINE_MAX,
        "source": "otsu" if usable else "fallback",
    }
    return (int(t) if usable else DARK_LINE_MAX), info


# ══════════════════════════════════════════════════════════════════════
# 1. layerize —— 源图分色
# ══════════════════════════════════════════════════════════════════════

def _median(mask: np.ndarray, size: int) -> np.ndarray:
    """中值滤波：去掉孤立噪点，保留较大图形。size 为像素窗口。"""
    if size < 3:
        return mask
    if size % 2 == 0:
        size += 1
    size = min(size, 15)
    im = Image.fromarray((mask * 255).astype(np.uint8))
    im = im.filter(ImageFilter.MedianFilter(size))
    return np.asarray(im) > 127


_BAYER4 = np.array([[0, 8, 2, 10], [12, 4, 14, 6],
                    [3, 11, 1, 9], [15, 7, 13, 5]], dtype=np.float64) / 16.0


def halftone(rgb: np.ndarray, region: np.ndarray, px: float,
             cell_mm: float = HALFTONE_CELL_MM,
             gain: float = 1.0) -> np.ndarray:
    """把「中间调区域」转成点阵。

    PCB 只有「有铜 / 无铜」两种状态，没有灰阶。要表现阴影渐变，
    只能用点的疏密（有序抖动）。每个单元取平均亮度，与 Bayer 阈值比较决定是否放点。

    gain > 1 会加深（点多），< 1 变浅（点少）——用来控制面积占比。
    """
    h, w = region.shape
    cell = max(2, int(round(cell_mm * px)))
    lum = (0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2])

    out = np.zeros((h, w), dtype=bool)
    yy, xx = np.ogrid[0:cell, 0:cell]
    cy = cx = (cell - 1) / 2.0
    rad = cell * HALFTONE_DOT_RATIO
    dot = ((yy - cy) ** 2 + (xx - cx) ** 2) <= rad * rad

    for by in range(0, h, cell):
        for bx in range(0, w, cell):
            blk = region[by:by + cell, bx:bx + cell]
            if not blk.any():
                continue
            vals = lum[by:by + cell, bx:bx + cell][blk]
            if vals.size == 0:
                continue
            darkness = float(np.clip((1.0 - vals.mean() / 255.0) * gain, 0.0, 1.0))
            t = _BAYER4[(by // cell) % 4, (bx // cell) % 4]
            if darkness > t:
                sub = out[by:by + cell, bx:bx + cell]
                d = dot[:sub.shape[0], :sub.shape[1]]
                sub |= d
    return out


def tritone(rgb: np.ndarray, fig: np.ndarray,
            gold_pct: float, silk_pct: float) -> tuple[np.ndarray, np.ndarray]:
    """三色调色阶化：只按亮度百分位分档，不依赖颜色。

    为什么这样做：
      彩色角色有十几个色阶，单色 PCB 只有「铜 / 丝印 / 板底」三档。
      按饱和度/色相分类在各路素材上表现差异极大（实测 6 张图金色占比从 3% 到 98%），
      而按亮度百分位分档结果稳定，且天然符合丝网印刷的分色逻辑。

    参数用**百分位**而非阈值，所以自动适配任意明暗分布的图。

    返回 (gold_mask, silk_mask)，都限制在 fig 内。
    """
    lum = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
    vals = lum[fig]
    if vals.size == 0:
        z = np.zeros_like(fig)
        return z, z

    lo = float(np.percentile(vals, gold_pct))
    hi = float(np.percentile(vals, 100.0 - silk_pct))
    gold = fig & (lum <= lo)
    silk = fig & (lum >= hi)
    return gold, silk


def resolve_preset(style: str) -> dict:
    """把风格名解析成参数。未知名字回退 tritone 并保留原名。"""
    key = (style or "").strip().lower()
    preset = STYLE_PRESETS.get(key)
    if preset is not None:
        return dict(preset, key=key)
    # 兼容手工参数名
    if key in ("oui", "outline", "tri", "solid"):
        return dict(STYLE_PRESETS["tritone"], engine=key, key=key)
    return dict(STYLE_PRESETS["tritone"], key="tritone")


def list_styles() -> dict:
    return {k: {kk: vv for kk, vv in v.items()} for k, v in STYLE_PRESETS.items()}


def cmd_layerize(src: Path, out: Path, width_mm: float = 55.6,
                 dark_threshold: int | None = None,
                 style: str = DEFAULT_STYLE,
                 halftone_on: bool | None = None, gain: float = 1.0,
                 gold_pct: float | None = None, silk_pct: float | None = None,
                 max_gold: float | None = None,
                 finish: str = "enig",
                 merge_mm: float | None = None) -> int:
    """把彩色插画分离成 PCB 可制造的图层。

    输出（同画布，白=有图形）:
      outline.png  板框剪影
      gold.png     露铜/沉金
      silk.png     白丝印
      colored.png  彩色块（阻焊覆盖）
      gray.png     深色块（阻焊覆盖）
      preview.png  三层合成预览
      spec.json    尺寸与统计

    **主体判定优先用 alpha 通道**：抠图素材的背景是透明的，
    若直接 convert("RGB") 会把透明区变黑，导致整张图被当成主体。
    """
    out.mkdir(parents=True, exist_ok=True)

    # 解析风格预设，未显式传参则用预设值
    profile = FINISH_PROFILES.get((finish or "enig").lower(), FINISH_PROFILES["enig"])
    if max_gold is None:
        max_gold = float(profile["maxCopperRatio"])

    if merge_mm is None:
        merge_mm = MERGE_MM

    preset = resolve_preset(style)
    engine = preset["engine"]
    if gold_pct is None:
        gold_pct = float(preset.get("goldPct", 18.0))
    if silk_pct is None:
        silk_pct = float(preset.get("silkPct", 18.0))
    if halftone_on is None:
        halftone_on = bool(preset.get("halftone", False))
    needs_high_gold = bool(preset.get("requiresGoldOver20", False))

    im = Image.open(src)
    has_alpha = im.mode in ("RGBA", "LA") or "transparency" in im.info
    arr = np.asarray(im.convert("RGBA")).astype(np.int16)

    alpha = arr[..., 3] if has_alpha else None
    if has_alpha:
        # 透明区铺白，避免 (0,0,0) 的透明底色污染直方图与分色
        a = arr[..., 3:4] / 255.0
        rgb_full = (arr[..., :3] * a + 255 * (1 - a)).astype(np.int16)
    else:
        rgb_full = arr[..., :3]

    H, W = rgb_full.shape[:2]

    vmax = rgb_full.max(axis=2)
    vmin = rgb_full.min(axis=2)
    sat = vmax - vmin

    # ── 主体判定：alpha 优先，白底回退 ──────────────────────────
    figure, basis = derive_figure(rgb_full, alpha)

    ys, xs = np.where(figure)
    if len(ys) == 0:
        print("错误: 未找到主体（整张图都是背景色？）")
        return 1
    y0, y1, x0, x1 = ys.min(), ys.max(), xs.min(), xs.max()

    pad = int(0.01 * max(x1 - x0, y1 - y0))
    y0 = max(0, y0 - pad); y1 = min(H - 1, y1 + pad)
    x0 = max(0, x0 - pad); x1 = min(W - 1, x1 + pad)

    crop = lambda m: m[y0:y1 + 1, x0:x1 + 1]
    vmaxc, vminc, satc = crop(vmax), crop(vmin), crop(sat)
    rgbc = np.moveaxis(np.stack([crop(rgb_full[..., i]) for i in range(3)]), 0, -1)
    fig = crop(figure)
    h, w = fig.shape

    height_mm = width_mm * h / w
    px = w / width_mm

    # ── 暗色阈值：自动（Otsu）或指定 ────────────────────────────
    auto_dark, otsu_info = adaptive_dark_threshold(rgb_full, figure)
    dark_max = dark_threshold if dark_threshold is not None else auto_dark

    # 分色
    dark = (vmaxc < dark_max) & fig
    colored = (satc > SAT_THRESHOLD) & ~dark & fig
    # 「白」必须是**低饱和的亮色**，否则皮肤高光（250,220,190）也会被判成白，
    # 让整张脸变成白丝印 —— 实测踩过。
    light = (vmaxc >= NEAR_WHITE) & (satc < WHITE_MAX_SAT) & fig & ~colored
    gray_block = fig & ~dark & ~colored & ~light

    gold_limit_hits = 0
    if engine == "tri":
        # 三色调：暗部→金、亮部→白丝印、中间调→板底
        gp = float(gold_pct)
        sp = float(silk_pct)
        gold, silk = tritone(rgbc, fig, gp, sp)
        # 中值滤波去掉孤立的明/暗噪点（皮肤高光、压缩噪点）
        med = max(3, int(round(0.35 * px)) | 1)
        gold = _median(gold, med)
        silk = _median(silk, med)
        # 闭运算合并邻近碎片：软边图（水彩/厚涂）分色后容易碎成一堆小岛，
        # 按物理尺度做闭运算能把它们连成可识别的块。
        merge_r = int(round(float(merge_mm) * px))
        if merge_r > 0:
            gold = _morph(_morph(gold, merge_r, True), merge_r, False)
            silk = _morph(_morph(silk, merge_r, True), merge_r, False)
        colored = colored & ~gold & ~silk
        gray_block = gray_block & ~gold & ~silk
        mask_dark_blocks = fig & ~gold & ~silk
    elif engine == "solid":
        # 剪影做金色：整个主体都是露铜/沉金，亮部再叠白丝印
        # 这是沉金挂件最经典的观感，且对不同素材最稳健
        gold = fig & ~light
        silk = light
        colored = colored & ~gold
        gray_block = gray_block & ~gold & ~silk
        dark_blocks_layer = dark & ~gold
    else:
        # 线稿风：只有细线做金色
        dark_blocks_layer = dark

    outline = fig
    if engine == "outline":
        # 深色细分：细线 vs 大色块（大色块归阻焊，避免整体铺金）
        block_r = max(1, int(round(DARK_BLOCK_RADIUS_MM * px)))
        dark_blocks = _morph(_morph(dark, block_r, False), block_r, True)
        # 中间调用半调表现（否则皮肤等中间调会整片变成板底色，脸就"消失"了）
        mid = gray_block | colored
        dots = halftone(rgbc, mid, px, HALFTONE_CELL_MM, gain) if halftone_on else np.zeros_like(mid)
        gold = (dark & ~dark_blocks) | dots
        mask_dark_blocks = dark_blocks & fig
        silk = light & ~gold
    else:
        mask_dark_blocks = gray_block

    # 去噪
    min_px_area = (MIN_FEATURE_MM * px) ** 2
    min_r = 1 if min_px_area > 4 else 0
    if min_r:
        gold = _morph(_morph(gold, min_r, False), min_r, True)

    # 丝印专项去细线
    silk_r = max(1, int(round(SILK_MIN_MM / 2 * px)))
    silk = _morph(_morph(silk, silk_r, False), silk_r, True)
    silk_r2 = max(1, int(round(SILK_MIN_MM * px)))
    silk = _morph(_morph(silk, silk_r2, False), silk_r2, True)

    # 丝印避让金色
    clr = int(round(SILK_CLEARANCE_MM * px))
    if clr > 0:
        silk = silk & ~_morph(gold, clr, True)

    # 剪影闭合：填补蕾丝扇贝之间的缝隙，避免剪影被切成上百块
    close_r = max(1, int(round(OUTLINE_CLOSE_MM * px)))
    outline = _morph(_morph(outline, close_r, True), close_r, False)

    # 写出
    layers = {
        "outline": outline, "gold": gold, "silk": silk,
        "colored": colored, "gray": gray_block | mask_dark_blocks,
    }
    for name, m in layers.items():
        Image.fromarray((m * 255).astype(np.uint8), mode="L").save(out / f"{name}.png")

    # 合成预览
    prev = np.zeros((h, w, 3), dtype=np.uint8)
    prev[outline] = (10, 40, 32)
    prev[layers["gray"]] = (16, 58, 46)
    prev[colored] = (110, 80, 160)
    prev[gold] = (212, 160, 23)
    prev[silk] = (245, 245, 245)
    Image.fromarray(prev).save(out / "preview.png")

    # 统计
    board_px = int(outline.sum())
    stats = {}
    for name, m in layers.items():
        n = int(m.sum())
        stats[name] = {
            "pixels": n,
            "area_mm2": round(n / (px * px), 2),
            "ratio": round(n / board_px, 4) if board_px else 0.0,
        }

    board_area = board_px / (px * px)
    gold_ratio = (stats["gold"]["area_mm2"] * 2) / (board_area * 2) if board_area else 0

    coupon_warnings = []
    if profile["needsContrast"] and gold_ratio > 0.90:
        coupon_warnings.append(
            f"露铜面积 {gold_ratio*100:.0f}%，几乎整板都是铜，图案会失去对比。"
            "OSP 分支要靠「露铜 vs 阻焊」形成明暗，建议把 --gold-pct 降到 30 左右。"
        )
    if not profile["needsContrast"] and gold_ratio > profile["maxCopperRatio"]:
        coupon_warnings.append(
            f"金面积 {gold_ratio*100:.1f}% 超过免费沉金券 {profile['maxCopperRatio']*100:.0f}% 门槛，"
            "超出将无法免费。调低 --gold-pct 或改用 --finish osp。"
        )

    spec = {
        "source": src.name,
        "sourceCanvasPx": [W, H],
        "cropBoxPx": [int(x0), int(y0), int(x1), int(y1)],
        "boardCanvasPx": [w, h],
        "widthMm": round(width_mm, 3),
        "heightMm": round(height_mm, 3),
        "boardAreaMm2": round(board_area, 2),
        "pxPerMm": round(px, 3),
        "holeDiameterMm": HOLE_DIAMETER_MM,
        "edgeMarginMm": EDGE_MARGIN_MM,
        "style": preset["key"],
        "styleName": preset.get("name", preset["key"]),
        "engine": engine,
        "goldLimitHits": gold_limit_hits,
        "goldPct": round(float(gold_pct), 2),
        "mergeMm": round(float(merge_mm), 2),
        "silkPct": round(float(silk_pct), 2),
        "figureBasis": basis,
        "darkThreshold": otsu_info,
        "thresholds": {
            "nearWhite": NEAR_WHITE, "darkLineMax": dark_max,
            "satThreshold": SAT_THRESHOLD, "silkClearanceMm": SILK_CLEARANCE_MM, "whiteMaxSat": WHITE_MAX_SAT,
            "minFeatureMm": MIN_FEATURE_MM, "silkMinMm": SILK_MIN_MM,
            "darkBlockRadiusMm": DARK_BLOCK_RADIUS_MM, "outlineCloseMm": OUTLINE_CLOSE_MM,
        },
        "layers": stats,
        "goldRatioTwoSides": round(gold_ratio, 4),
        "finish": finish,
        "finishName": profile["name"],
        "coupon": {
            "maxCopperRatio": profile["maxCopperRatio"],
            "ratioPass": bool(gold_ratio <= profile["maxCopperRatio"]),
            "layersAllowed": profile["layers"],
            "thicknessAllowedMm": profile["thicknessMm"],
            "note": profile["note"],
            "warnings": coupon_warnings,
        },
    }
    (out / "spec.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"板面 {w}x{h}px = {width_mm}x{height_mm:.2f}mm, {px:.2f} px/mm")
    for name in ("gold", "silk", "colored", "gray"):
        print(f"  {name:8s}: {stats[name]['ratio']*100:5.1f}% of board")
    print(f"金色占比(双面): {gold_ratio*100:.1f}%")
    print(f"写出: {out}")
    return 0


# ══════════════════════════════════════════════════════════════════════
# 2. clip —— 裁到板框内（防铜溢出板边）
# ══════════════════════════════════════════════════════════════════════

def cmd_clip(out: Path, edge_mm: float = EDGE_MARGIN_MM) -> int:
    spec = json.loads((out / "spec.json").read_text(encoding="utf-8"))
    px = spec["pxPerMm"]
    edge_px = max(1, int(round(edge_mm * px)))

    outline = np.asarray(Image.open(out / "outline.png").convert("L")) > 127
    inner = _morph(outline, edge_px, False)

    report = {}
    for name in ("gold", "silk", "colored", "gray"):
        f = out / f"{name}.png"
        if not f.exists():
            continue
        m = np.asarray(Image.open(f).convert("L")) > 127
        before = int(m.sum())
        m = m & inner
        after = int(m.sum())
        Image.fromarray((m * 255).astype(np.uint8), mode="L").save(f)
        report[name] = before - after
        if before:
            print(f"{name:8s}: 裁掉 {before-after:6d} px ({(before-after)/before*100:.1f}%)")

    Image.fromarray((inner * 255).astype(np.uint8), mode="L").save(out / "inner.png")
    spec["edgeMarginMm"] = edge_mm
    spec["edgeMarginPx"] = edge_px
    spec["clip"] = report
    (out / "spec.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"板边内缩 {edge_mm}mm = {edge_px}px")
    return 0


# ══════════════════════════════════════════════════════════════════════
# 3. vectorize —— 追踪 + 平滑
# ══════════════════════════════════════════════════════════════════════

def _build_edge_loops(mask: np.ndarray) -> list[list[tuple[int, int]]]:
    """像素边界边链：对每个 True 像素加入朝向 False 的有向边，串成闭合环。

    比「按角度排序边界像素」正确：后者只对凸形成立，凹形会自相交成放射星形。
    """
    h, w = mask.shape
    pad = np.zeros((h + 2, w + 2), dtype=bool)
    pad[1:-1, 1:-1] = mask
    m = pad

    edges: dict[tuple[int, int], tuple[int, int]] = {}
    ys, xs = np.where(m)
    for y, x in zip(ys.tolist(), xs.tolist()):
        if not m[y - 1, x]:
            edges[(x, y)] = (x + 1, y)
        if not m[y, x + 1]:
            edges[(x + 1, y)] = (x + 1, y + 1)
        if not m[y + 1, x]:
            edges[(x + 1, y + 1)] = (x, y + 1)
        if not m[y, x - 1]:
            edges[(x, y + 1)] = (x, y)

    loops = []
    while edges:
        start = next(iter(edges))
        loop = [start]
        cur = start
        for _ in range(len(edges) + 4):
            nxt = edges.pop(cur, None)
            if nxt is None or nxt == start:
                break
            loop.append(nxt)
            cur = nxt
        if len(loop) >= 4:
            loops.append(loop)
    return [[(x - 1, y - 1) for (x, y) in lp] for lp in loops]


def _dp(points: list, tol: float, closed: bool = True) -> list:
    n = len(points)
    if n < 4 or tol <= 0:
        return points

    def _dp_seg(seg):
        if len(seg) < 3:
            return seg
        keep = [False] * len(seg)
        keep[0] = keep[-1] = True
        stack = [(0, len(seg) - 1)]
        while stack:
            i0, i1 = stack.pop()
            if i1 <= i0 + 1:
                continue
            ax, ay = seg[i0]
            bx, by = seg[i1]
            dx, dy = bx - ax, by - ay
            norm = math.hypot(dx, dy)
            best_i, best_d = -1, 0.0
            for i in range(i0 + 1, i1):
                px_, py_ = seg[i]
                d = (math.hypot(px_ - ax, py_ - ay) if norm == 0
                     else abs(dy * px_ - dx * py_ + bx * ay - by * ax) / norm)
                if d > best_d:
                    best_d, best_i = d, i
            if best_d > tol and best_i > 0:
                keep[best_i] = True
                stack.append((i0, best_i))
                stack.append((best_i, i1))
        return [p for p, k in zip(seg, keep) if k]

    if not closed:
        return _dp_seg(points)
    x0, y0 = points[0]
    far_i, far_d = 0, -1.0
    for i in range(1, n):
        d = (points[i][0] - x0) ** 2 + (points[i][1] - y0) ** 2
        if d > far_d:
            far_d, far_i = d, i
    if far_i <= 1:
        return points
    a = _dp_seg(points[: far_i + 1])
    b = _dp_seg(points[far_i:] + [points[0]])
    out = a[:-1] + b[:-1]
    return out if len(out) >= 3 else points


def _chaikin(points: list, iterations: int, closed: bool = True) -> list:
    pts = [list(p) for p in points]
    for _ in range(iterations):
        if len(pts) < 3:
            break
        out = []
        n = len(pts)
        rng = range(n) if closed else range(n - 1)
        for i in rng:
            p0 = pts[i]
            p1 = pts[(i + 1) % n]
            out.append([0.75 * p0[0] + 0.25 * p1[0], 0.75 * p0[1] + 0.25 * p1[1]])
            out.append([0.25 * p0[0] + 0.75 * p1[0], 0.25 * p0[1] + 0.75 * p1[1]])
        pts = out
    return pts


def _merge_short(poly: list, merge_mm: float) -> list:
    """折叠短边——台阶的特征是极短边反复转折。

    这是平滑的关键：按「边长」而非「角度」区分台阶与真拐角。
    （按角度会把台阶的 90° 也判成拐角，导致平滑无效。）
    """
    if len(poly) < 4:
        return poly
    pts = [list(p) for p in poly]
    for _ in range(20):
        if len(pts) < 4:
            break
        out = []
        n = len(pts)
        for i in range(n):
            p = pts[i]
            q = pts[(i + 1) % n]
            if math.hypot(q[0] - p[0], q[1] - p[1]) >= merge_mm:
                out.append(p)
        if len(out) < 3 or len(out) == len(pts):
            break
        pts = out
    return pts


def _poly_area(poly: list) -> float:
    s = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def _poly_perimeter(poly: list) -> float:
    s = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        s += math.hypot(x2 - x1, y2 - y1)
    return s


def cmd_vectorize(out: Path) -> int:
    spec = json.loads((out / "spec.json").read_text(encoding="utf-8"))
    px = spec["pxPerMm"]
    Hpx = spec["boardCanvasPx"][1]
    tol_px = SMOOTH_TOL_MM * px

    result = {"widthMm": spec["widthMm"], "heightMm": spec["heightMm"], "layers": {}}

    for name in ("gold", "silk", "colored", "outline"):
        f = out / f"{name}.png"
        if not f.exists():
            continue
        mask = np.asarray(Image.open(f).convert("L")) > 127
        loops = _build_edge_loops(mask)

        polys = []
        dropped = 0
        for loop in loops:
            loop = _dp(loop, tol_px)
            if len(loop) < 3:
                dropped += 1
                continue
            mm_poly = [[round(x / px, 3), round((Hpx - y) / px, 3)] for (x, y) in loop]

            # 按线宽过滤过细轮廓
            min_mm = 0.0 if name == "outline" else MIN_POLY_WIDTH_MM
            if min_mm > 0:
                area = _poly_area(mm_poly)
                perim = _poly_perimeter(mm_poly)
                approx_w = (2.0 * area / perim) if perim > 0 else 0.0
                if approx_w < min_mm and area < (min_mm ** 2) * 4:
                    dropped += 1
                    continue

            # 平滑：短边合并 → Chaikin → DP
            sp = _merge_short(mm_poly, SMOOTH_MERGE_MM)
            if len(sp) >= 4:
                sp = _chaikin(sp, SMOOTH_CHAIKIN)
                sp = _dp(sp, SMOOTH_TOL_MM)
            sp = [[round(q[0], 3), round(q[1], 3)] for q in sp]
            if len(sp) >= 3:
                polys.append(sp)

        if name == "outline":
            polys.sort(key=_poly_area, reverse=True)
            polys = polys[:1]

        result["layers"][name] = polys
        v = sum(len(p) for p in polys)
        print(f"{name:8s}: {len(polys):4d} 轮廓, {v:6d} 顶点 (丢弃 {dropped})")

    (out / "polys.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    print(f"写出: {out / 'polys.json'}")
    return 0


# ══════════════════════════════════════════════════════════════════════
# 4. emit —— 生成 EasyEDA JS
# ══════════════════════════════════════════════════════════════════════

def _point_in_poly(px_: float, py_: float, poly: list) -> bool:
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > py_) != (yj > py_)) and (px_ < (xj - xi) * (py_ - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def _to_path(poly: list, ox: float, oy: float) -> str:
    out = []
    for i, (x, y) in enumerate(poly):
        mx = round((ox + x) * MM2MIL, 2)
        my = round((oy + y) * MM2MIL, 2)
        out.append(f"{mx},{my}" if i == 0 else f"'L',{mx},{my}")
    return "[" + ",".join(out) + "]"


def cmd_emit(dst: Path, out: Path, offset_x: float = 100.0, offset_y: float = 100.0,
             hole: bool = False, hole_dia: float = HOLE_DIAMETER_MM) -> int:
    data = json.loads((out / "polys.json").read_text(encoding="utf-8"))
    w_mm, h_mm = data["widthMm"], data["heightMm"]
    layers = data["layers"]
    outline = layers.get("outline", [])
    if not outline:
        print("错误: 缺少 outline")
        return 1
    board = outline[0]

    hole_pos = None
    if hole:
        cx = w_mm / 2
        for i in range(95, 50, -1):
            y = h_mm * i / 100
            if _point_in_poly(cx, y, board):
                hole_pos = (round(cx, 2), round(y, 2))
                break
        if hole_pos is None:
            hole_pos = (round(cx, 2), round(h_mm * 0.9, 2))

    L = []
    A = L.append
    A("const out = { drawn: 0, failed: 0, byLayer: {}, errors: [] };")
    A("")
    A("// Polyline → convertToFill：唯一能写任意多边形的方式")
    A("// 不用 PrimitiveFill（只支持预设形状）、不用 PrimitiveImage（会归一化缩放）")
    A("const fillPoly = async (layer, pathArr, strokeW) => {")
    A("  try {")
    A("    const poly = eda.pcb_MathPolygon.createPolygon(pathArr);")
    A("    const pl = await eda.pcb_PrimitivePolyline.create('', layer, poly, strokeW, false);")
    A("    await pl.convertToFill();")
    A("    out.drawn++;")
    A("    out.byLayer[layer] = (out.byLayer[layer] || 0) + 1;")
    A("  } catch (e) {")
    A("    out.failed++;")
    A("    if (out.errors.length < 5) out.errors.push(String(e && e.message).slice(0, 100));")
    A("  }")
    A("};")
    A("")

    for name, targets in (
        ("outline", [(1, "铜基底")]),
        ("gold", [(1, "铜"), (5, "开窗")]),
        ("silk", [(3, "丝印")]),
    ):
        polys = layers.get(name, [])
        if not polys:
            continue
        for layer, label in targets:
            A(f"// {name} → layer {layer} ({label}), {len(polys)} 轮廓")
            for poly in polys:
                A(f"await fillPoly({layer}, {_to_path(poly, offset_x, offset_y)}, 1);")
        if name == "outline":
            A("// 板框（异形）")
            A("try {")
            A(f"  const bo = eda.pcb_MathPolygon.createPolygon({_to_path(board, offset_x, offset_y)});")
            A("  const bp = await eda.pcb_PrimitivePolyline.create('', 11, bo, 10, false);")
            A("  out.boardOutline = !!bp;")
            A("} catch (e) { out.outlineError = String(e && e.message).slice(0, 140); }")

    if hole_pos is not None:
        hx = round((offset_x + hole_pos[0]) * MM2MIL, 2)
        hy = round((offset_y + hole_pos[1]) * MM2MIL, 2)
        hr = round((hole_dia / 2) * MM2MIL, 2)
        A("")
        A("// 挂孔 NPTH")
        A("try {")
        A(f"  const c = eda.pcb_MathPolygon.createPolygon(['CIRCLE', {hx}, {hy}, {hr}]);")
        A("  await eda.pcb_PrimitiveFill.create(12, c, '', 0, 0, false);")
        A(f"  out.hole = {{ x: {hole_pos[0]}, y: {hole_pos[1]}, dia: {hole_dia} }};")
        A("} catch (e) { out.holeError = String(e && e.message).slice(0, 140); }")

    A("")
    A("await eda.pcb_Document.save();")
    A("return out;")

    dst.write_text("\n".join(L), encoding="utf-8")
    total = (len(layers.get("outline", [])) + len(layers.get("gold", [])) * 2
             + len(layers.get("silk", [])))
    print(f"生成 {dst}  ({total} 图元 + 板框 1" + (" + 挂孔 1" if hole_pos else "") + ")")
    return 0


# ══════════════════════════════════════════════════════════════════════
# 5. render —— 成品效果图
# ══════════════════════════════════════════════════════════════════════

MASK_COLORS = {
    "black": (26, 26, 30), "white": (238, 238, 235), "green": (13, 59, 46),
    "purple": (74, 48, 110), "blue": (22, 48, 96), "red": (110, 26, 26),
    "yellow": (150, 130, 40),
}
GOLD = (208, 162, 42)
BARE_COPPER = (176, 106, 62)
SILK_WHITE = (242, 242, 238)
SILK_BLACK = (30, 30, 32)


def cmd_render(dst: Path, out: Path, mask_color: str = "black",
               finish: str = "enig", dpi: float = 26, hole: float = 0.0,
               hole_x: float = 0.0, hole_y: float = 0.0) -> int:
    """按物理材料渲染成品：阻焊色 + 金属色 + 丝印色。"""
    d = json.loads((out / "polys.json").read_text(encoding="utf-8"))
    spec = json.loads((out / "spec.json").read_text(encoding="utf-8"))
    w_mm, h_mm = d["widthMm"], d["heightMm"]

    board_polys = d["layers"].get("outline", [])
    gold_polys = d["layers"].get("gold", [])
    silk_polys = d["layers"].get("silk", [])

    W = int(w_mm * dpi) + 2
    H = int(h_mm * dpi) + 2
    mask_rgb = MASK_COLORS.get(mask_color, MASK_COLORS["black"])
    metal = GOLD if finish == "enig" else BARE_COPPER
    silk_rgb = SILK_BLACK if mask_color == "white" else SILK_WHITE

    def to_px(poly):
        return [(x * dpi, (h_mm - y) * dpi) for (x, y) in poly]

    board_mask = Image.new("L", (W, H), 0)
    bd = ImageDraw.Draw(board_mask)
    for poly in board_polys:
        bd.polygon(to_px(poly), fill=255)

    base = Image.new("RGB", (W, H), (18, 18, 20))
    base.paste(Image.new("RGB", (W, H), mask_rgb), (0, 0), board_mask)

    gold_mask = Image.new("L", (W, H), 0)
    gd = ImageDraw.Draw(gold_mask)
    for poly in gold_polys:
        gd.polygon(to_px(poly), fill=255)
    gold_mask = Image.composite(gold_mask, Image.new("L", (W, H), 0), board_mask)
    base.paste(Image.new("RGB", (W, H), metal), (0, 0), gold_mask)

    silk_mask = Image.new("L", (W, H), 0)
    sd = ImageDraw.Draw(silk_mask)
    for poly in silk_polys:
        sd.polygon(to_px(poly), fill=255)
    silk_mask = Image.composite(silk_mask, Image.new("L", (W, H), 0), board_mask)
    base.paste(Image.new("RGB", (W, H), silk_rgb), (0, 0), silk_mask)

    if hole > 0:
        hd = ImageDraw.Draw(base)
        cx, cy = hole_x * dpi, (h_mm - hole_y) * dpi
        r = (hole / 2) * dpi
        hd.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(18, 18, 20))
        hd.ellipse([cx - r * 1.15, cy - r * 1.15, cx + r * 1.15, cy + r * 1.15],
                   outline=metal, width=max(2, int(r * 0.3)))

    # 金属高光
    try:
        hl = gold_mask.filter(ImageFilter.GaussianBlur(1)).point(lambda v: int(v * 0.25))
        base = Image.composite(
            Image.new("RGB", (W, H), tuple(min(255, c + 26) for c in metal)), base, hl)
    except Exception:
        pass

    base.save(dst)
    print(f"渲染: {dst} ({W}x{H}px) 阻焊={mask_color} 表面={finish}")
    return 0


# ══════════════════════════════════════════════════════════════════════
# 6. verify —— Gerber 独立校验
# ══════════════════════════════════════════════════════════════════════

def cmd_verify(zpath: Path, spec_path: Path | None = None) -> int:
    import re
    import zipfile

    spec = None
    if spec_path and spec_path.exists():
        spec = json.loads(spec_path.read_text(encoding="utf-8"))

    report = {"file": zpath.name, "size": zpath.stat().st_size, "layers": {}}
    with zipfile.ZipFile(zpath) as z:
        names = z.namelist()
        report["entries"] = names
        for n in names:
            if not n.lower().endswith((".gtl", ".gbl", ".gto", ".gbo", ".gts", ".gbs", ".gko")):
                continue
            text = z.read(n).decode("utf-8", errors="replace")
            lines = text.replace("\r", "").split("\n")
            d01 = d03 = 0
            xs, ys = [], []
            fmt_dec = 5
            for raw in lines:
                s = raw.strip()
                if s.startswith("%FS"):
                    m = re.search(r"X\d(\d)Y", s)
                    if m:
                        fmt_dec = int(m.group(1))
                    continue
                if s.endswith("D03*"):
                    d03 += 1
                elif s.endswith("D01*"):
                    d01 += 1
                mx = re.search(r"X(-?\d+)", s)
                my = re.search(r"Y(-?\d+)", s)
                if mx and s.endswith(("D01*", "D02*")):
                    xs.append(int(mx.group(1)))
                if my and s.endswith(("D01*", "D02*")):
                    ys.append(int(my.group(1)))
            scale = 10 ** fmt_dec
            bbox = None
            if xs and ys:
                bbox = {
                    "w": round((max(xs) - min(xs)) / scale, 3),
                    "h": round((max(ys) - min(ys)) / scale, 3),
                }
            report["layers"][n] = {"draw": d01, "flash": d03, "bbox": bbox}

    print(f"=== {zpath.name} ({report['size']} bytes, {len(names)} files) ===")
    for n, info in report["layers"].items():
        bb = info["bbox"]
        bb_s = f"{bb['w']}x{bb['h']}mm" if bb else "空"
        print(f"  {n:38s} draw={info['draw']:6d} bbox={bb_s}")

    if spec:
        board_bb = report["layers"].get("Gerber_BoardOutlineLayer.GKO", {}).get("bbox")
        if board_bb:
            print(f"\n板框 bbox: {board_bb['w']}x{board_bb['h']}mm "
                  f"(外接矩形 {spec['widthMm']}x{spec['heightMm']}mm)")
            ok = board_bb["w"] <= spec["widthMm"] + 0.1 and board_bb["h"] <= spec["heightMm"] + 0.1
            print("板框在外接矩形内: " + ("是" if ok else "否 —— 异常"))

    out_json = zpath.with_suffix(".report.json")
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


# ══════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════

def _usage() -> None:
    print(__doc__)


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if len(argv) < 2:
        _usage()
        return 2

    cmd = argv[1]
    args = argv[2:]

    def opt(flag, default):
        if flag in args:
            i = args.index(flag)
            if i + 1 < len(args):
                return args[i + 1]
        return default

    def flag_on(name):
        return name in args

    if cmd == "layerize":
        if len(args) < 2:
            _usage(); return 2
        return cmd_layerize(Path(args[0]), Path(args[1]),
                            float(opt("--width-mm", 55.6)),
                            None,
                            opt("--style", DEFAULT_STYLE),
                            None, 1.0,
                            float(opt("--gold-pct", 0)) if "--gold-pct" in args else None,
                            float(opt("--silk-pct", 0)) if "--silk-pct" in args else None,
                            float(opt("--max-gold", 0)) if "--max-gold" in args else None,
                            opt("--finish", "enig"),
                            float(opt("--merge-mm", 0)) if "--merge-mm" in args else None)

    if cmd == "clip":
        return cmd_clip(Path(args[0]), float(opt("--edge-mm", EDGE_MARGIN_MM)))

    if cmd == "vectorize":
        return cmd_vectorize(Path(args[0]))

    if cmd == "emit":
        if len(args) < 2:
            _usage(); return 2
        return cmd_emit(
            Path(args[0]), Path(args[1]),
            float(opt("--offset-x", 100.0)), float(opt("--offset-y", 100.0)),
            flag_on("--hole"), float(opt("--hole-dia", HOLE_DIAMETER_MM)),
        )

    if cmd == "render":
        if len(args) < 2:
            _usage(); return 2
        return cmd_render(
            Path(args[0]), Path(args[1]),
            opt("--mask", "black"), opt("--finish", "enig"),
            float(opt("--dpi", 26)),
            float(opt("--hole", 0)), float(opt("--hole-x", 0)), float(opt("--hole-y", 0)),
        )

    if cmd == "verify":
        if len(args) < 1:
            _usage(); return 2
        sp = Path(args[1]) if len(args) > 1 else None
        return cmd_verify(Path(args[0]), sp)

    if cmd == "pipeline":
        if len(args) < 2:
            _usage(); return 2
        src, out = Path(args[0]), Path(args[1])
        width_mm = float(opt("--width-mm", 55.6))
        hole = flag_on("--hole")
        print("── 1/4 分色 ─────────────────────────────")
        style = opt("--style", DEFAULT_STYLE)
        if cmd_layerize(src, out, width_mm, None, style,
                        False if flag_on("--no-halftone") else None,
                        float(opt("--halftone-gain", 1.0)),
                        float(opt("--gold-pct", 0)) if "--gold-pct" in args else None,
                        float(opt("--silk-pct", 0)) if "--silk-pct" in args else None,
                        float(opt("--max-gold", 0)) if "--max-gold" in args else None,
                        opt("--finish", "enig"),
                        float(opt("--merge-mm", 0)) if "--merge-mm" in args else None) != 0:
            return 1
        print("── 2/4 裁到板框 ─────────────────────────")
        cmd_clip(out)
        print("── 3/4 矢量化 + 平滑 ────────────────────")
        if cmd_vectorize(out) != 0:
            return 1
        js = Path(opt("--js", str(out / "write.js")))
        print("── 4/4 生成 EDA 脚本 ────────────────────")
        if cmd_emit(js, out, 100.0, 100.0, hole) != 0:
            return 1
        if flag_on("--png"):
            png = Path(opt("--png", str(out / "final.png")))
            hx = hy = 0.0
            if hole:
                d = json.loads((out / "polys.json").read_text(encoding="utf-8"))
                w_mm, h_mm = d["widthMm"], d["heightMm"]
                board = d["layers"]["outline"][0]
                cx = w_mm / 2
                for i in range(95, 50, -1):
                    y = h_mm * i / 100
                    if _point_in_poly(cx, y, board):
                        hx, hy = round(cx, 2), round(y, 2)
                        break
            cmd_render(png, out, opt("--mask", "black"), opt("--finish", "enig"),
                       26, HOLE_DIAMETER_MM if hole else 0, hx, hy)
        print("\n完成。")
        return 0

    if cmd in ("styles", "list-styles"):
        import json as _json
        print(_json.dumps(list_styles(), ensure_ascii=False, indent=2))
        return 0

    if cmd in ("-h", "--help", "help"):
        _usage()
        return 0

    print(f"未知子命令: {cmd}")
    _usage()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
