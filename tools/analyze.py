#!/usr/bin/env python3
"""图像适配性分析：在任何分色之前，先判断这张图能不能做好、该用什么参数。

## 为什么需要这一步

固定阈值（暗<90、彩>55、白>244）只对某类图有效。
实测 6 张真实二次元图，同样的阈值给出的金色占比从 3.5% 到 8.1%，
而示例图 Kuromi 是 15% —— 说明阈值没跟着图走。

## 三个核心判据

1. **背景是否可分离**
   用「与画布边界连通的近白像素」判定。占比过高（>95%）= 满幅无白底，无法分离主体。

2. **暗线阈值（Otsu 自动）**
   Otsu 法自动找灰度双峰之间的分割点，比固定 90 稳健。
   但只在「暗像素占比合理（3%~40%）」时可信，否则回退到固定值。

3. **边缘硬度（判断是线稿还是水彩）**
   算梯度幅值的分布：
   - 硬边（黑描边+平涂）→ 梯度集中在高值，分色可靠
   - 软边（水彩/厚涂）→ 梯度分散，没有清晰轮廓，分色会糊

## 输出

一段 JSON：推荐参数 + 警告 + 适配性评级（good / fair / poor）。

用法:
  python analyze.py <图片> [--json]
"""

from __future__ import annotations

import json
import sys
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image

NEAR_WHITE = 244
DARK_FALLBACK = 90
SAT_THRESHOLD = 55


def derive_figure_mask(rgb: np.ndarray, alpha: np.ndarray | None) -> tuple[np.ndarray, str, float]:
    """判定「主体」像素。

    两条路径，按优先级：
      1. **alpha 通道**（首选）：PNG 抠图素材的背景是透明的（alpha=0），
         这是最可靠的分离方式。若不先用 alpha，`convert("RGB")` 会把透明区
         变成黑色，导致整张图被判成主体。
      2. **白底洪水填充**（回退）：完全不透明的图（如白底插画），
         取与画布边界连通的近白像素为背景。

    返回 (figure 掩码, 依据, 透明占比)。
    """
    if alpha is not None:
        transparent = alpha < 16
        tr = float(transparent.mean())
        if tr > 0.05:
            return alpha >= 128, "alpha", tr

    whiteish = rgb.max(axis=2) >= NEAR_WHITE
    bg = flood_border(whiteish)
    return ~bg, "white-background", 0.0


def otsu_threshold(gray: np.ndarray) -> tuple[int, float]:
    """Otsu 法：最大化类间方差，返回 (阈值, 分离度)。"""
    hist, _ = np.histogram(gray, bins=256, range=(0, 256))
    total = hist.sum()
    if total == 0:
        return DARK_FALLBACK, 0.0

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

    # 分离度归一化：类间方差 / 总方差
    total_var = float(np.var(gray))
    sep = (best_var / total_var) if total_var > 0 else 0.0
    return best_t, sep


def flood_border(mask: np.ndarray) -> np.ndarray:
    """与画布边界连通的像素（BFS）。"""
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
    fm, fv = mask.ravel(), visited.ravel()
    while q:
        i = q.popleft()
        y, x = divmod(i, w)
        for j, ok in ((i - w, y > 0), (i + w, y < h - 1), (i - 1, x > 0), (i + 1, x < w - 1)):
            if ok and fm[j] and not fv[j]:
                fv[j] = True
                q.append(j)
    return visited


def gradient_hardness(gray: np.ndarray) -> dict:
    """用 Sobel 梯度评估边缘硬度。"""
    g = gray.astype(np.float32)
    # 简单差分近似 Sobel
    gx = np.abs(np.diff(g, axis=1)[:-1, :])
    gy = np.abs(np.diff(g, axis=0)[:, :-1])
    mag = np.sqrt(gx ** 2 + gy ** 2)

    if mag.max() <= 0:
        return {"hardRatio": 0.0, "meanMag": 0.0, "p99Mag": 0.0}

    # 边缘像素：梯度 > 30
    edge = mag > 30
    if edge.sum() == 0:
        return {"hardRatio": 0.0, "meanMag": float(mag.mean()), "p99Mag": float(np.percentile(mag, 99))}

    em = mag[edge]
    # 硬边特征：梯度幅值高且集中（p50 高）
    hard = float((em > 120).mean())
    return {
        "hardRatio": round(hard, 3),
        "meanMag": round(float(em.mean()), 1),
        "p99Mag": round(float(np.percentile(mag, 99)), 1),
        "edgePixelRatio": round(float(edge.mean()), 4),
    }


def analyze(path: Path) -> dict:
    im = Image.open(path)
    has_alpha = im.mode in ("RGBA", "LA") or "transparency" in im.info
    rgba = im.convert("RGBA")
    arr = np.asarray(rgba).astype(np.int16)

    alpha = arr[..., 3] if has_alpha else None
    # 透明区的 RGB 常是 (0,0,0)，先铺白底再取 RGB，避免黑色污染直方图
    if has_alpha:
        a = arr[..., 3:4] / 255.0
        rgb = (arr[..., :3] * a + 255 * (1 - a)).astype(np.int16)
    else:
        rgb = arr[..., :3]

    H, W = rgb.shape[:2]

    gray = (0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]).astype(np.uint8)
    vmax = rgb.max(axis=2)
    vmin = rgb.min(axis=2)
    sat = vmax - vmin

    # 1. 主体判定（alpha 优先，白底回退）
    figure, basis, transparent_ratio = derive_figure_mask(rgb, alpha)
    bg_ratio = float(1.0 - figure.mean())
    fig_ratio = float(figure.mean())

    ys, xs = np.where(figure)
    bbox = None
    if len(ys):
        bbox = {"x0": int(xs.min()), "y0": int(ys.min()), "x1": int(xs.max()), "y1": int(ys.max())}
        bw = bbox["x1"] - bbox["x0"] + 1
        bh = bbox["y1"] - bbox["y0"] + 1
        bbox["widthPx"] = bw
        bbox["heightPx"] = bh
        bbox["aspect"] = round(bw / bh, 3)

    # 2. Otsu 暗线阈值（只在人物区域内算，背景会污染直方图）
    fig_gray = gray[figure] if fig_ratio > 0.02 else gray.ravel()
    otsu_t, otsu_sep = otsu_threshold(fig_gray)
    dark_ratio = float((fig_gray < otsu_t).mean()) if len(fig_gray) else 0.0

    # Otsu 可信度：暗像素占比在 2%~40% 且分离度 > 0.3
    otsu_ok = (0.02 <= dark_ratio <= 0.40) and otsu_sep > 0.30
    dark_threshold = int(otsu_t) if otsu_ok else DARK_FALLBACK

    # 3. 边缘硬度
    hardness = gradient_hardness(gray)

    # 4. 饱和度分布（判断有多少彩色块）
    fig_sat = sat[figure] if fig_ratio > 0.02 else sat.ravel()
    sat_ratio = float((fig_sat > SAT_THRESHOLD).mean()) if len(fig_sat) else 0.0

    # ── 适配性评级 ────────────────────────────────────────────────
    warnings: list[str] = []
    score = 0  # 0~100

    # 背景可分离
    if basis == "alpha":
        score += 35   # 透明通道是最可靠的分离方式
    elif fig_ratio < 0.05:
        warnings.append("几乎整张图都是背景色，找不到主体")
        score += 0
    elif fig_ratio > 0.95:
        warnings.append(
            f"检测不到可分离的背景（背景仅占 {bg_ratio*100:.1f}%），且没有透明通道。"
            "这张图是满幅构图，无法自动分离主体轮廓。"
            "建议先用抠图工具去掉背景，或改用带白底/透明底的素材。"
        )
        score += 10
    else:
        score += 35

    # 边缘硬度
    hr = hardness["hardRatio"]
    if hr >= 0.35:
        score += 35
    elif hr >= 0.15:
        score += 20
        warnings.append(
            f"边缘偏软（硬边比例 {hr*100:.0f}%）。可能是水彩/厚涂风格，"
            "分色后细节会糊。线稿或平涂素材效果更好。"
        )
    else:
        score += 5
        warnings.append(
            f"边缘很软（硬边比例 {hr*100:.0f}%）。这张图没有清晰轮廓线，"
            "分色结果会接近色块拼接，不适合做主图线稿。"
            "建议换用有线稿感的素材，或降低预期。"
        )

    # 分辨率
    if bbox:
        min_side = min(bbox["widthPx"], bbox["heightPx"])
        if min_side < 600:
            score += 5
            warnings.append(
                f"主体分辨率只有 {min_side}px，缩放后细节会丢失。建议短边 ≥1000px。"
            )
        elif min_side < 1000:
            score += 15
            warnings.append(f"主体分辨率 {min_side}px 偏低，建议 ≥1000px。")
        else:
            score += 30

    # 暗线占比（太少说明没有线稿可提取）
    if dark_ratio < 0.02:
        warnings.append(
            f"暗色像素只占 {dark_ratio*100:.1f}%，几乎没有可提取的线稿。"
            "金色图案会非常稀疏。"
        )
    elif dark_ratio > 0.45:
        warnings.append(f"暗色像素占 {dark_ratio*100:.1f}%，比重过大，金色会成片。")

    grade = "good" if score >= 70 else ("fair" if score >= 40 else "poor")

    return {
        "file": path.name,
        "sizePx": [W, H],
        "grade": grade,
        "score": score,
        "warnings": warnings,
        "detected": {
            "figureBasis": basis,
            "transparentRatio": round(transparent_ratio, 4),
            "backgroundRatio": round(bg_ratio, 4),
            "figureRatio": round(fig_ratio, 4),
            "subjectBBox": bbox,
            "otsuThreshold": int(otsu_t),
            "otsuSeparation": round(otsu_sep, 3),
            "otsuUsable": otsu_ok,
            "darkPixelRatio": round(dark_ratio, 4),
            "saturationRatio": round(sat_ratio, 4),
            "hardEdgeRatio": hardness["hardRatio"],
            "edgePixelRatio": hardness.get("edgePixelRatio", 0),
        },
        "recommended": {
            "darkLineMax": dark_threshold,
            "satThreshold": SAT_THRESHOLD,
            "nearWhite": NEAR_WHITE,
            "silkClearanceMm": 0.20,
            "minFeatureMm": 0.20,
        },
    }


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    targets = []
    p = Path(sys.argv[1])
    if p.is_dir():
        targets = sorted([f for f in p.iterdir() if f.suffix.lower() in (".png", ".jpg", ".jpeg")])
    else:
        targets = [p]

    results = []
    for f in targets:
        try:
            r = analyze(f)
        except Exception as e:
            r = {"file": f.name, "grade": "error", "warnings": [str(e)]}
        results.append(r)

    if len(results) == 1:
        print(json.dumps(results[0], ensure_ascii=False, indent=2))
    else:
        print(f"{'文件':38s} {'评级':6s} {'分数':>4s}  关键指标")
        print("-" * 96)
        for r in results:
            d = r.get("detected", {})
            print(f"{r['file'][:38]:38s} {r['grade']:6s} {r.get('score', 0):>4}  "
                  f"主体{d.get('figureRatio', 0)*100:4.1f}% "
                  f"硬边{d.get('hardEdgeRatio', 0)*100:3.0f}% "
                  f"暗{d.get('darkPixelRatio', 0)*100:3.1f}% "
                  f"Otsu={d.get('otsuThreshold', '-')}")
            for w in r.get("warnings", [])[:2]:
                print(f"{'':50s}  ⚠ {w[:64]}")
        print()
        print(json.dumps(results, ensure_ascii=False, indent=2)[:3000])
    return 0


if __name__ == "__main__":
    sys.exit(main())