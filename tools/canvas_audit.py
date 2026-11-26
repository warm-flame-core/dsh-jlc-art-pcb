#!/usr/bin/env python3
"""画布预检：在写入前判断当前工程/板子是否适合落板。

## 要回答的三个问题

1. **工程里有几块 PCB？** 该用哪一块？
2. **目标 PCB 上有没有已有图元？** 有的话是用户的还是上次的残留？
3. **该新建 PCB，还是复用现有的？**

## 判据（决定行为）

| 情况 | 行为 |
|---|---|
| 空工程（0 块 PCB） | 新建 PCB |
| 有 PCB，但都是空的 | 复用空的那块 |
| 有 PCB，且有图元，用户没说明 | **停下问用户**，不擅自覆盖 |
| 用户明确说要新建 | 新建，不动旧的 |

## 用法

  python canvas_audit.py <session-id>              # 只检查，输出 JSON
  python canvas_audit.py <session-id> --decide      # 检查并给出建议动作
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

AUDIT_JS = r"""
const out = { ok: true, project: null, pcbs: [], recommendation: null, warnings: [] };
try {
  const cur = await eda.dmt_Project.getCurrentProjectInfo();
  out.project = cur ? { name: cur.friendlyName, uuid: cur.uuid } : null;
} catch (e) {
  out.projectError = String(e && e.message).slice(0, 120);
}

const pcbs = await eda.dmt_Pcb.getAllPcbsInfo();
out.pcbCount = pcbs.length;

for (const p of pcbs) {
  const info = { name: p.name, uuid: p.uuid, primitives: 0, byType: {}, hasBoardOutline: false };
  try {
    await eda.dmt_EditorControl.openDocument(p.uuid);
    await new Promise(r => setTimeout(r, 900));
    const kinds = ['Fill','Image','Polyline','Line','String','Pad','Via','Component','Region'];
    for (const k of kinds) {
      try {
        const a = await eda['pcb_Primitive' + k].getAll();
        const n = (a || []).length;
        if (n > 0) { info.byType[k] = n; info.primitives += n; }
      } catch (e) { /* 该类型无 getAll 就当 0 */ }
    }
    // 板框：查 11 层有没有 Polyline
    try {
      const pl = await eda.pcb_PrimitivePolyline.getAll();
      if (Array.isArray(pl)) {
        info.hasBoardOutline = pl.some(o => o.getState_Layer && o.getState_Layer() === 11);
      }
    } catch (e) {}
  } catch (e) {
    info.error = String(e && e.message).slice(0, 100);
  }
  out.pcbs.push(info);
}

// 给出建议
const nonEmpty = out.pcbs.filter(p => p.primitives > 0);
const empty = out.pcbs.filter(p => p.primitives === 0);

if (out.pcbCount === 0) {
  out.recommendation = { action: 'create_pcb', reason: '工程里没有 PCB' };
} else if (nonEmpty.length === 0) {
  out.recommendation = { action: 'reuse_pcb', pcb: empty[0].name, pcbUuid: empty[0].uuid,
                         reason: '有 PCB 但都是空的，直接复用' };
} else if (nonEmpty.length === 1 && empty.length === 0) {
  out.recommendation = { action: 'ask_user', pcb: nonEmpty[0].name, pcbUuid: nonEmpty[0].uuid,
                         reason: `PCB「${nonEmpty[0].name}」已有 ${nonEmpty[0].primitives} 个图元，需确认是否覆盖` };
} else {
  out.recommendation = { action: 'ask_user',
                         reason: `有 ${nonEmpty.length} 块非空 PCB，建议新建一块以免破坏已有内容`,
                         candidates: nonEmpty.map(p => ({ name: p.name, primitives: p.primitives })) };
}
return out;
"""


def find_lceda() -> str | None:
    import os
    for env in ("LCEDA_BIN", "EASYEDA_BIN"):
        v = os.environ.get(env)
        if v and Path(v).exists():
            return v
    for name in ("lceda-pro", "lceda-pro.exe"):
        w = shutil.which(name)
        if w:
            return w
    for drive in ("C", "D", "E", "F"):
        for p in (f"{drive}:/Program Files/EasyEDA-Pro/easyeda-pro.exe",
                  f"{drive}:/Program Files/lceda-pro/lceda-pro.exe",
                  f"{drive}:/Software/lceda-pro/lceda-pro.exe"):
            if Path(p).exists():
                return p
    return None


def audit(session_id: str) -> dict:
    exe = find_lceda()
    if exe is None:
        return {"ok": False, "error": "找不到 lceda-pro"}

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(AUDIT_JS)
        tmp = f.name

    try:
        r = subprocess.run(
            [exe, "invoke", "--session", session_id, "--ext-uuid", "eda",
             "--timeout", "120000", "--code-file", tmp],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
        )
        out = (r.stdout or "").strip()
        if not out:
            return {"ok": False, "error": (r.stderr or "无输出")[:300]}
        # 取最后一行 JSON
        for line in reversed(out.splitlines()):
            line = line.strip()
            if line.startswith("{") and '"ok"' in line:
                try:
                    data = json.loads(line)
                    # invoke 外层包了一层 {"ok":..., "value":{...}}
                    if "value" in data and isinstance(data["value"], dict):
                        return data["value"]
                    return data
                except json.JSONDecodeError:
                    continue
        return {"ok": False, "error": out[:400]}
    finally:
        try:
            Path(tmp).unlink()
        except Exception:
            pass


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    sid = sys.argv[1]
    result = audit(sid)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
