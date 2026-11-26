# 更新日志

## 0.1.0 — 首个可用版本

### 工具
- `jlc_check_environment` — 环境自检：Python / numpy / Pillow / EDA CLI / 窗口连接，
  缺失时给出修复命令与官方链接
- `jlc_audit_canvas` — 画布预检：工程里有几块 PCB、各有多少图元、该新建还是复用
- `jlc_build_artwork` — 一条命令跑完：分色 → 裁板 → 矢量化平滑 → 生成 EDA 脚本 + 效果图
- `jlc_validate_constraints` — 嘉立创工艺约束校验（三档阈值 + 彩印六项强约束）

### 流水线能力
- 异形板框（贴合图形剪影）+ 可选挂孔
- 支持透明底（alpha）与白底两种素材
- 自适应暗色阈值（Otsu），不依赖固定阈值
- 四种造型风格预设：`tritone` / `lineart` / `halftone` / `solid`
- OSP / 沉金两种工艺档案，自动切换面积上限、层数、板厚校验
- 金色占比自动钳制到 ≤20%（保住免费沉金券）
- 轮廓平滑去台阶（短边合并 + Chaikin + DP）
- 成品效果图渲染（按物理材料还原）

### 已知限制
- 仅单面板（顶层）
- 彩色丝印的 Fabrication_Colorful* 文件未实现
- 水彩/厚涂类素材分色效果有限

### 致谢
分层方法与细线保护部分借鉴自 pcb-art-keychain-skill（经作者授权）；
自检体系参考 gift-pcb（MIT）。详见 NOTICE。
