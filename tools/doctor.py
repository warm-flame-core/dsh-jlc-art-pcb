#!/usr/bin/env python3
"""环境自检：检查插件运行所需的外部依赖。

用户侧的 DSH 不一定装了这些东西，装插件时就应该能看出来缺什么、怎么补。

## 检查项

| 依赖 | 必需性 | 缺失后果 | 补救 |
|---|---|---|---|
| Python 3.9+ | **必需**（流水线） | 无法生成图形 | 装 Python / 配 pythonPath |
| numpy | 必需 | 分色失败 | pip install numpy |
| Pillow | 必需 | 分色失败 | pip install Pillow |
| lceda-pro CLI | 可选（落板） | 只能手工粘脚本 | 装 EasyEDA Pro ≥4.1.60 |
| 已打开 EDA 窗口 | 可选（落板） | 无法自动写入 | 打开 EDA |

## 输出

JSON：每项含 ok / version / path / fix（怎么修）/ action（按钮跳哪里）。

用法:
  python doctor.py [--json]
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

MIN_PY = (3, 9)


def _run(cmd: list[str], timeout: int = 20) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, (r.stdout or "").strip()
    except Exception as e:
        return 1, str(e)


def check_python() -> dict:
    v = sys.version_info
    ok = (v.major, v.minor) >= MIN_PY
    return {
        "id": "python",
        "name": "Python 运行时",
        "required": True,
        "ok": ok,
        "version": f"{v.major}.{v.minor}.{v.micro}",
        "path": sys.executable,
        "detail": "" if ok else f"需要 >= {MIN_PY[0]}.{MIN_PY[1]}",
        "fix": "" if ok else "安装 Python 3.9+，或在插件配置里设置 pythonPath",
        "action": "" if ok else "https://www.python.org/downloads/",
    }


def check_module(mod: str, pip_name: str, required: bool) -> dict:
    try:
        m = __import__(mod)
        ver = getattr(m, "__version__", "?")
        return {
            "id": mod, "name": f"{pip_name}", "required": required,
            "ok": True, "version": str(ver), "path": getattr(m, "__file__", "") or "",
            "detail": "", "fix": "", "action": "",
        }
    except ImportError:
        return {
            "id": mod, "name": pip_name, "required": required,
            "ok": False, "version": "", "path": "",
            "detail": f"未安装 {pip_name}",
            "fix": f"{sys.executable} -m pip install {pip_name}",
            "action": f"https://pypi.org/project/{pip_name}/",
        }


def find_lceda() -> str | None:
    for env in ("LCEDA_BIN", "EASYEDA_BIN"):
        v = os.environ.get(env)
        if v and Path(v).exists():
            return v
    for n in ("lceda-pro", "lceda-pro.exe", "easyeda-pro", "easyeda-pro.exe"):
        w = shutil.which(n)
        if w:
            return w
    cands = []
    for drive in ("C", "D", "E", "F"):
        cands += [
            Path(f"{drive}:/Program Files/EasyEDA-Pro/easyeda-pro.exe"),
            Path(f"{drive}:/Program Files/lceda-pro/lceda-pro.exe"),
            Path(f"{drive}:/Software/lceda-pro/lceda-pro.exe"),
            Path(f"{drive}:/Software/EasyEDA-Pro/easyeda-pro.exe"),
        ]
    for c in cands:
        if c.exists():
            return str(c)
    return None


def check_lceda() -> list[dict]:
    exe = find_lceda()
    if exe is None:
        return [{
            "id": "lceda", "name": "嘉立创 EDA CLI", "required": False,
            "ok": False, "version": "", "path": "",
            "detail": "未找到 lceda-pro（需要 EasyEDA Pro 桌面版 ≥4.1.60）",
            "fix": "安装嘉立创 EDA 专业版桌面端；在线版不支持 CLI",
            "action": "https://pro.lceda.cn/editor",
        }]

    rows = [{
        "id": "lceda", "name": "嘉立创 EDA CLI", "required": False,
        "ok": True, "version": "", "path": exe, "detail": "", "fix": "", "action": "",
    }]

    # doctor 拿版本与连接状态
    code, out = _run([exe, "doctor"], timeout=30)
    connected = False
    bridge = ""
    try:
        data = json.loads(out)
        val = data.get("value", {})
        connected = bool(val.get("connected"))
        bridge = val.get("bridgeVersion") or ""
    except Exception:
        pass

    rows[0]["version"] = bridge
    rows.append({
        "id": "lceda-session", "name": "EDA 窗口连接", "required": False,
        "ok": connected, "version": "已连接" if connected else "", "path": "",
        "detail": "" if connected else "没有已打开的 EDA 窗口",
        "fix": "" if connected else "启动嘉立创 EDA 专业版并打开一个工程",
        "action": "",
    })
    return rows


def check_env() -> dict:
    rows: list[dict] = []
    rows.append(check_python())
    rows.append(check_module("numpy", "numpy", True))
    rows.append(check_module("PIL", "Pillow", True))
    rows.extend(check_lceda())

    required_ok = all(r["ok"] for r in rows if r["required"])
    hints = [r for r in rows if not r["ok"]]

    return {
        "ok": required_ok,
        "platform": f"{platform.system()} {platform.release()}",
        "pythonExecutable": sys.executable,
        "checks": rows,
        "blocking": [r["id"] for r in rows if r["required"] and not r["ok"]],
        "hints": [
            {"id": r["id"], "detail": r["detail"], "fix": r["fix"], "action": r["action"]}
            for r in hints
        ],
    }


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    report = check_env()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())