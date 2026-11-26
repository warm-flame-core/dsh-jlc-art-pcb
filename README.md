# dsh-jlc-art-pcb

把一张插画变成**可打样的 PCB 艺术板** —— 嘉立创（JLCPCB）打样券驱动的 DeepSeek Harness 插件。

给一张图，产出异形板框、分层几何、EasyEDA 可直接执行的脚本、成品效果图和下单清单。

面向**二次元 / 图案装饰板**，不是功能板（不涉及原理图、BOM、布线、贴片）。

## 装什么

| 组件 | 说明 |
|---|---|
| `jlc_validate_constraints` 工具 | 校验设计是否满足嘉立创工艺约束 |
| `jlc_build_artwork` 工具 | 一条命令跑完整流水线 |
| `jlc-art-pcb` Skill | 分支决策、八阶段门控、分层方法、EDA 陷阱 |

## 两条工艺分支

| | 分支 A：OSP 露铜 | 分支 B：沉金 + 彩色丝印 |
|---|---|---|
| 成本 | **免费**（每月 2 张券） | 付费 |
| 视觉 | 铜色 + 板底色 | 全彩 |
| 适合 | 线稿、剪影、单色徽记 | 二次元角色、多彩插画 |
| 硬约束 | — | 仅 2/4 层、白阻焊、沉金、1oz 铜、1U" 金厚、板面无标志 |

**选错分支整块板要重做**，所以 Skill 强制在 P0 定分支。

## 用法

```
jlc_build_artwork
  sourceImage: F:/我的图/character.png
  workDir:     F:/projects/art-pcb/my-board
  widthMm:     60
  addHole:     true
  maskColor:   black
  finish:      enig
```

产出：

```
my-board/
├── source.png     原始图
├── final.png      成品效果图
├── write.js       写入 EasyEDA 的脚本
├── polys.json     多边形数据
├── spec.json      尺寸与统计
└── out/           分层掩码
```

然后用 EDA 的 CLI 落板：

```bash
lceda-pro open --path <工程路径>
lceda-pro invoke --session <id> --ext-uuid eda --timeout 180000 --code-file write.js
```

## 环境要求

- **DeepSeek Harness** ≥ 0.2.2
- **Python 3.9+** 与 `numpy`、`Pillow`（用于图像处理）
- **嘉立创 EDA 专业版 ≥ 4.1.60 桌面版**（落板用；在线版只能手工粘贴脚本）

Python 找不到时：
- 插件配置 `pythonPath` 指向解释器
- 或环境变量 `JLC_PYTHON`

## 为什么流水线走 Python

图像分色、轮廓追踪、形态学、贝塞尔平滑 —— 用 PIL/numpy 实现简洁可靠。
用 JS 重写等于把 2000 行已验证代码重来一遍，质量更难保证。
代价是用户机器要有 Python。

## 已知限制

- **单面板**（顶层）。背面镜像尚未实现。
- **彩色丝印的 `Fabrication_Colorful*` 四文件尚未生成**，彩印分支仍需手工操作。
- 分色阈值按示例图标定，风格差异大的图需重新调参。
- 不自动下单（嘉立创下单页需人工确认与付款）。

## 致谢

分层方法、细线保护与去毛刺边界部分借鉴自
[pcb-art-keychain-skill](https://github.com/xxhhwtxx/pcb-art-keychain-skill)（作者 Wu tai，经授权）。
自检体系参考 [gift-pcb](https://github.com/YHLLXY/pcb-art-skill)（MIT）。详见 `NOTICE`。

## 许可证

MIT
