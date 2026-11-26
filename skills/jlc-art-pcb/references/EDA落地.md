# EDA 落地（EasyEDA Pro）

## 一、通道

用 `lceda-pro` 内置 CLI（EDA ≥ 4.1.60 桌面版自带，无需装扩展）。

```bash
lceda-pro session list                                     # 取 sessionId
lceda-pro invoke --session <id> --ext-uuid eda --timeout 45000 --code "<js>"
```

也可用 MCP：`lceda-pro --mcp stdio`（暴露 `help` / `discover` / `call`）。

**PowerShell 注意**：`lceda-pro` 是 GUI 子系统程序，`$x = & lceda-pro doctor` **拿不到 stdout**。
必须 `(& $exe doctor 2>&1 | Out-String)` 或 `| ConvertFrom-Json`。

## 二、生命周期（顺序不能错）

```
createProject(name) → openProject(uuid) → createPcb() → openDocument(pcbUuid) → 写图元 → save → 导出
```

**陷阱**：

- `createProject` 后**不 `openProject` 直接 `createPcb` 会返回 `undefined`**。
- `createPcb()` 不传 boardName 会**自动建一块 Board**；工程默认已有 `PCB1`，新建得到 `PCB2`。
- `getAllProjectsUuid()` 创建后可能返回 `[]`（缓存滞后）。用 `getCurrentProjectInfo()` 判断更可靠。
- `openDocument` 会**拉可见窗口**，可能超过默认 60 s 超时，需显式 `--timeout 45000`。

## 三、图元创建

### 3.1 走线 —— 只能信号层

```js
eda.pcb_PrimitiveLine.create(net, layer, x1, y1, x2, y2, width)
```

| 层 | 结果 |
|---|---|
| 1 (Top) / 2 (Bottom) | ✅ |
| 3 / 4（丝印）、5 / 11 / 12 | ❌ `无法创建直线图元` |

### 3.2 填充 —— 丝印与铜都能用

```js
const poly = eda.pcb_MathPolygon.createPolygon(['R', x, y, w, h, 0, 0]);   // 矩形
const poly = eda.pcb_MathPolygon.createPolygon(['CIRCLE', cx, cy, r]);      // 圆
eda.pcb_PrimitiveFill.create(layer, polygon, net, 0, width, false)
```

实测结果：**1–8 层、12 层都能用 Fill**，只有 **11（板框层）不行**。

| 层 | Fill |
|---|---|
| 1–8（铜/丝印/阻焊/锡膏/装配） | ✅ |
| 11（板框） | ❌ 用 Polyline |
| 12（多层） | ✅ |

> **关键**：丝印图形必须用「多边形填充」写，不能用线段拼。
>
> ⚠️ **写图元前必须先 `openDocument()` 打开该 PCB。** 没有打开的文档时，
> 所有 `create` 都会失败，而且错误信息只报「参数不正确」，极具误导性。
> 判断依据：`getCurrentPcbInfo()` 返回 `undefined` 就是没打开。

### 3.3 文字

```js
eda.pcb_PrimitiveString.create(layer, x, y, text, font, rotation, height, width, 0, false, 0, false, false)
```

- **height / width 单位是 mm**（例：`6, 3` = 6 mm 字高 / 3 mm 宽）。
- `font` 必须是字体表里存在的名字，传错报「字体列表里没有这种字体」。

### 3.4 板框

```js
const outline = eda.pcb_MathPolygon.createPolygon(['R', 0, 0, 2000, 1500, 0, 0]);
eda.pcb_PrimitivePolyline.create('', 11, outline, 10, false)
//                                  ^层  ^多边形对象（不是点数组！）
```

## 四、单位混用

| 场景 | 单位 |
|---|---|
| `pcb_MathPolygon` 与图元坐标 | **mil** |
| `PrimitiveString` 的 height/width | **mm** |

**1 mm = 39.3701 mil。** 不显式换算，图形会差 39 倍。

## 五、导出

```js
const gerber = await eda.pcb_ManufactureData.getGerberFile();   // 必须不传参
await eda.sys_FileSystem.saveFileToFileSystem('C:/path/out.zip', gerber);
```

### ⚠️ 两个 save API 截然不同

| API | 行为 | 自动化可用性 |
|---|---|---|
| `saveFile(fileData, fileName)` | **弹原生保存对话框** | ❌ **绝不能用** |
| `saveFileToFileSystem(uri, fileData, fileName)` | 直接写指定路径，静默返回 `true` | ✅ **只能用这个** |

`uri` 规则：**以 `/` 结尾视为目录**（用 `fileName` 命名）；**不以 `/` 结尾视为完整文件路径**（`fileName` 被忽略）。

### 导出 API 清单

`getGerberFile` / `getPdfFile` / `getDxfFile` / `get3DFile` / `get3DShellFile` /
`getBomFile` / `getNetlistFile` / `getPickAndPlaceFile` / `getIpc2581CFile` /
`getFlyingProbeTestFile` / `getAltiumDesignerFile` …

**空板导出返回 `{}`** —— 板上必须有实际图元。

Gerber 包结构：

```
Gerber_TopLayer.GTL              Gerber_BottomLayer.GBL
Gerber_TopSilkscreenLayer.GTO    Gerber_BottomSilkscreenLayer.GBO
Gerber_TopSolderMaskLayer.GTS    Gerber_BottomSolderMaskLayer.GBS
Gerber_BoardOutlineLayer.GKO     Gerber_DrillDrawingLayer.GDD
FlyingProbeTesting.json           PCB下单必读.txt
```

## 六、其他能力

- **截图**：`eda.dmt_EditorControl.getCurrentRenderedAreaImage()` → 返回 `Blob`（PNG）
- **3D**：`eda.pcb_RayTracerEngine`
- **DRC / DFM**：`eda.pcb_Drc` / `eda.pcb_Dfm`
- **命令组**：`beginCommandGroup()` / `commitCommandGroup()` / `rollbackCommandGroup()`
- **层查询**：`pcb_Layer.getAllLayers()` → `id / name / type / layerStatus`

## 七、层 ID

| ID | 名称 | 类型 | Fill | Line |
|---|---|---|---|---|
| 1 | Top Layer | SIGNAL | ✅ | ✅ |
| 2 | Bottom Layer | SIGNAL | ✅ | ✅ |
| **3** | **Top Silkscreen** | SILKSCREEN | ✅ | ❌ |
| 4 | Bottom Silkscreen | SILKSCREEN | ✅ | ❌ |
| 5 | Top Solder Mask | SOLDER_MASK | ✅ | ❌ |
| 6 | Bottom Solder Mask | SOLDER_MASK | ✅ | ❌ |
| 7 / 8 | Paste Mask | PASTE_MASK | ✅ | — |
| 9 / 10 | Assembly | ASSEMBLY | — | — |
| 11 | Board Outline | OTHER | ❌ | ✅（Polyline） |
| 12 | Multi-Layer | OTHER | ✅ | ❌ |
| 47 | Hole Layer | OTHER | — | — |

## 八、批量与落盘

- 单次 `invoke` 有**巨大固定开销**（进程启动 + IPC）。**必须一次 invoke 批量建图元**。
- 批量操作用 `beginCommandGroup` 包裹，失败可整体回滚。
- 写完必须 `pcb_Document.save()`，否则不落盘。
- `setState_*` 在同步模式下**不落盘**；改几何用 `toAsync().done()`，或重新 `get()` 后 `modify()`。

## 九、自动化安全检查表

1. **永不调用 `saveFile()`**（弹对话框）。
2. `openDocument` 可能超时，显式设 `--timeout`。
3. 批量图元用 command group 包裹。
4. 导出前先 `pcb_Document.save()`。
5. 用 `getCurrentPcbInfo()` 而非 `getAllProjectsUuid()` 判断当前状态。
