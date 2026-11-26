#!/usr/bin/env python3
"""工程与工作区管理：建项目、搬工程、生成下单清单。

## 为什么需要单独一个模块

EDA 的 `createProject` 不能指定路径，工程必然先落在 EDA 默认目录。
但用户不希望所有东西堆在 C 盘，也不想脱离工作区。

实测结论（见 docs/current/工程放置与工作区方案.md）：
  - `.eprj3` 是纯相对路径的**目录**，可任意搬移不断链
  - `lceda-pro open --path` 能从任意位置打开
  → 所以：创建后搬走 + 从新位置打开，是可行路径。

## 子命令

  new    <workspace> <name> [--width-mm] [--hole]   建一个完整项目
  clean  <project-dir>                              清中间产物，留源图与工程
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# art-pcb 是工作区里的唯一入口目录
ROOT_DIRNAME = "art-pcb"


def sh(cmd: list[str], timeout: int = 120) -> tuple[int, str]:
    """跑一个子进程，返回 (code, 合并输出)。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except FileNotFoundError:
        return 127, f"找不到可执行文件: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "超时"


def find_lceda() -> str | None:
    """定位 lceda-pro 可执行文件。

    顺序：环境变量 → PATH → 官方安装位置。
    官方 Windows 安装器会把目录加进 PATH，所以正常情况下第一个就命中。
    """
    for env_name in ("LCEDA_BIN", "EASYEDA_BIN"):
        v = os.environ.get(env_name)
        if v and Path(v).exists():
            return v

    which = shutil.which("lceda-pro") or shutil.which("lceda-pro.exe")
    if which:
        return which

    # 官方安装位置（不写死本机路径，按盘符常见情况探测）
    candidates: list[Path] = []
    for drive in ("C", "D", "E", "F"):
        candidates += [
            Path(f"{drive}:/Program Files/EasyEDA-Pro/easyeda-pro.exe"),
            Path(f"{drive}:/Program Files/lceda-pro/lceda-pro.exe"),
            Path(f"{drive}:/Software/lceda-pro/lceda-pro.exe"),
            Path(f"{drive}:/Software/EasyEDA-Pro/easyeda-pro.exe"),
        ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def default_eda_projects_dir() -> Path | None:
    """EDA 默认工程目录。

    实测本机在 F:\\Cache\\Documents\\LCEDA-Pro\\projects。
    不同机器可能在 Documents 下，逐个探测。
    """
    candidates = [
        Path.home() / "Documents" / "LCEDA-Pro" / "projects",
        Path(r"F:/Cache/Documents/LCEDA-Pro/projects"),
        Path(r"D:/Cache/Documents/LCEDA-Pro/projects"),
        Path.home() / "Documents" / "EasyEDA-Pro" / "projects",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def project_root(workspace: Path) -> Path:
    return workspace / ROOT_DIRNAME


def resolve_project_dir(workspace: Path, name: str) -> tuple[Path, bool]:
    """解析项目目录，处理重名：返回 (路径, 是否已存在)。"""
    base = project_root(workspace) / name
    if not base.exists():
        return base, False
    return base, True


def write_order_md(proj: Path, spec: dict, validation: dict | None) -> Path:
    """生成给人看的下单清单。"""
    w = spec.get("widthMm", 0)
    h = spec.get("heightMm", 0)
    gold = spec.get("goldRatioTwoSides", 0) * 100
    hole = spec.get("holeDiameterMm", 0)

    lines = [
        f"# 下单清单：{proj.name}",
        "",
        "## 板子",
        f"尺寸 {w} × {h} mm（异形）",
        "层数 2 层",
        "板厚 1.6 mm",
        "阻焊 哑黑",
        "表面 沉金 ENIG",
    ]
    if hole:
        lines.append(f"挂孔 Ø{hole} mm")

    lines += [
        "",
        "## 已验证",
        f"金色占比 {gold:.1f}%" + ("（免费券上限 20% ✓）" if gold <= 20 else "（⚠️ 超过免费券 20% 上限）"),
    ]
    if validation:
        if validation.get("ok"):
            lines.append("工艺校验：通过")
        else:
            lines.append(f"工艺校验：{validation.get('summary', {}).get('errors', '?')} 个错误")
            for v in validation.get("violations", [])[:5]:
                lines.append(f"  - [{v.get('severity')}] {v.get('message')}")

    lines += [
        "",
        "## 下单时这样选",
        "```",
        "板材类别     FR-4",
        "层数         2",
        "板厚         1.6mm",
        "外层铜厚     1oz",
        "阻焊颜色     哑黑色",
        "字符颜色     白色",
        "焊盘喷镀     沉金",
        "最小孔径     0.3mm（免费档）",
        "阻焊覆盖     过孔盖油",
        "出货方式     单片出货",
        "品质赔付     按标准合同常规处理     ← 默认选移植赔付会多收 10 元，艺术板无元件",
        "线路测试     AOI全测+飞针抽测        ← 默认全测是收费项",
        "交期         正常交期",
        "数量         5",
        "SMT / 钢网   都不选",
        "```",
        "",
        "## 注意",
        "- 图案含动漫角色时可能触发相似度审核",
        "- 领券：嘉立创领券中心，每月 2 张，30 天有效",
        "- 用券必须：单片出货、5 片、长宽 ≤100mm、板厚 1.6mm",
        "",
        "## 文件",
        "```",
        "source.png    原始图",
        "final.png     成品效果图",
        "gerber.zip    打样文件",
        "project/      EDA 工程目录",
        "write.js      重新写入 EDA 的脚本",
        "```",
    ]

    p = proj / "order.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def write_status(proj: Path, step: str, extra: dict | None = None) -> None:
    """断点状态：记录走到哪一步。"""
    f = proj / ".status.json"
    data = {}
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data.setdefault("steps", {})
    data["steps"][step] = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        **(extra or {}),
    }
    data["updatedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def init_gitignore(proj: Path, workspace: Path) -> None:
    """在 art-pcb 目录写 gitignore，忽略中间产物。"""
    root = project_root(workspace)
    root.mkdir(parents=True, exist_ok=True)
    g = root / ".gitignore"
    if not g.exists():
        g.write_text(
            "# 中间产物可随时重新生成\n"
            "*/out/\n"
            "*/polys*.json\n"
            "*/write*.js\n"
            "*/.status.json\n"
            "*/gerber.report.json\n",
            encoding="utf-8",
        )


def cleanup_project(proj: Path) -> list[str]:
    """清理中间产物，保留源图、效果图、工程、下单清单。"""
    removed = []
    out = proj / "out"
    if out.exists():
        shutil.rmtree(out)
        removed.append(str(out))
    for pat in ("polys*.json", "write*.js", ".status.json", "*.report.json"):
        for f in proj.glob(pat):
            f.unlink()
            removed.append(str(f))
    return removed


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if len(argv) < 2:
        print(__doc__)
        return 2

    cmd = argv[1]
    args = argv[2:]

    if cmd == "clean":
        if not args:
            print("用法: project.py clean <项目目录>")
            return 2
        proj = Path(args[0])
        if not proj.exists():
            print(f"目录不存在: {proj}")
            return 1
        removed = cleanup_project(proj)
        print(f"清理了 {len(removed)} 项：")
        for r in removed:
            print(f"  - {r}")
        return 0

    if cmd == "new":
        if len(args) < 2:
            print("用法: project.py new <workspace> <name> [--width-mm 55.6] [--hole]")
            return 2
        workspace = Path(args[0]).resolve()
        name = args[1]

        proj, exists = resolve_project_dir(workspace, name)
        if exists:
            print(f"错误: 项目已存在 {proj}")
            print("请换一个名字，或先手动删除/重命名旧项目。")
            return 1

        proj.mkdir(parents=True, exist_ok=True)
        init_gitignore(proj, workspace)
        write_status(proj, "created", {"workspace": str(workspace)})

        info = {
            "projectDir": str(proj),
            "workspace": str(workspace),
            "artPcbRoot": str(project_root(workspace)),
            "lceda": find_lceda(),
            "edaProjectsDir": str(default_eda_projects_dir() or ""),
        }
        (proj / ".project.json").write_text(
            json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    if cmd == "relocate":
        # relocate <eda_project_dir> <target_dir>
        if len(args) < 2:
            print("用法: project.py relocate <EDA工程目录> <目标目录>")
            return 2
        src = Path(args[0])
        dst = Path(args[1])
        if not src.exists():
            print(f"源工程不存在: {src}")
            return 1
        if dst.exists():
            print(f"目标已存在: {dst}（不覆盖）")
            return 1
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst)
        print(f"已复制: {src} -> {dst}")
        # 只有复制成功才删原件
        try:
            shutil.rmtree(src)
            print(f"已删除原工程目录: {src}")
        except Exception as e:
            print(f"警告：原目录删除失败（不影响使用）: {e}")
        return 0

    if cmd == "open":
        if not args:
            print("用法: project.py open <工程路径>")
            return 2
        exe = find_lceda()
        if exe is None:
            print("错误: 找不到 lceda-pro。需要 EasyEDA Pro 桌面版 ≥ 4.1.60。")
            return 1
        code, out = sh([exe, "open", "--path", str(args[0])], timeout=180)
        print(out)
        return 0 if code == 0 else 1

    print(f"未知子命令: {cmd}")
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
