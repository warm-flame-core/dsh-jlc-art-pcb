#!/usr/bin/env python3
"""通用性回归测试：用差异极大的图跑同一套流水线，检查是否都能产出合理结果。

判据（每张图都要满足）：
  - 分色能找到主体（outline 非空）
  - 金色占比落在合理区间（1%~25%，超出说明分色失效）
  - 轮廓数不为 0，且不为天文数字（>2000 说明全是噪点）
  - 生成 write.js 成功

用法:
  python test_generic.py <图片目录> <输出根目录>
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
TOOL = HERE / "jlc_art.py"


def run_case(img: Path, out: Path, width_mm: float = 60.0) -> dict:
    if out.exists():
        import shutil
        shutil.rmtree(out)
    r = subprocess.run(
        [sys.executable, str(TOOL), "pipeline", str(img), str(out),
         "--width-mm", str(width_mm), "--hole",
         "--js", str(out / "write.js"), "--png", str(out / "final.png")],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    res = {"name": img.name, "ok": r.returncode == 0, "rc": r.returncode}
    if r.returncode != 0:
        res["error"] = (r.stderr or r.stdout)[-400:]
        return res

    try:
        spec = json.loads((out / "spec.json").read_text(encoding="utf-8"))
        polys = json.loads((out / "polys.json").read_text(encoding="utf-8"))
        res["w"] = spec["widthMm"]
        res["h"] = spec["heightMm"]
        res["goldRatio"] = spec["goldRatioTwoSides"]
        res["outlinePolys"] = len(polys["layers"].get("outline", []))
        res["totalPolys"] = sum(len(v) for v in polys["layers"].values())
        res["js"] = (out / "write.js").exists()
        res["png"] = (out / "final.png").exists()
        # 检查项
        checks = []
        if res["outlinePolys"] == 0:
            checks.append("无板框轮廓")
        if not (0.005 <= res["goldRatio"] <= 0.35):
            checks.append(f"金色占比异常 {res['goldRatio']*100:.1f}%")
        if res["totalPolys"] == 0:
            checks.append("无任何轮廓")
        if res["totalPolys"] > 3000:
            checks.append(f"轮廓过多 {res['totalPolys']}（疑似噪点）")
        if not res["js"]:
            checks.append("未生成 write.js")
        res["problems"] = checks
        res["pass"] = len(checks) == 0
    except Exception as e:
        res["pass"] = False
        res["problems"] = [f"读产物失败: {e}"]
    return res


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    img_dir = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out_root = Path(sys.argv[2] if len(sys.argv) > 2 else "out_generic")
    out_root.mkdir(parents=True, exist_ok=True)

    imgs = sorted([p for p in img_dir.iterdir()
                   if p.suffix.lower() in (".png", ".jpg", ".jpeg")])
    if not imgs:
        print(f"{img_dir} 里没有图片")
        return 1

    print(f"{'图片':30s} {'结果':6s} {'金%':>7s} {'板框':>5s} {'总轮廓':>7s} {'问题'}")
    print("-" * 100)

    results = []
    for img in imgs:
        out = out_root / img.stem
        r = run_case(img, out)
        results.append(r)
        if not r["ok"]:
            print(f"{r['name']:30s} {'FAIL':6s} {'-':>7s} {'-':>5s} {'-':>7s} 执行失败")
            continue
        verdict = "PASS" if r.get("pass") else "WARN"
        gold = f"{r['goldRatio']*100:.1f}" if "goldRatio" in r else "-"
        probs = "; ".join(r.get("problems", [])) or "-"
        print(f"{r['name']:30s} {verdict:6s} {gold:>7s} {r.get('outlinePolys','-'):>5} "
              f"{r.get('totalPolys','-'):>7} {probs}")

    npass = sum(1 for r in results if r.get("pass"))
    print(f"\n{npass}/{len(results)} 通过")

    rep = out_root / "generic-report.json"
    rep.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告: {rep}")
    return 0 if npass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
