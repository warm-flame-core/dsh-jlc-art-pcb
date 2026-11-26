# 参与贡献

## 报告问题

到 [Issues](https://github.com/warm-flame-core/dsh-jlc-art-pcb/issues/new/choose) 选对应模板提交。
提交前请先搜索已有 issue，避免重复。

**图形类问题请务必附原始素材图** —— 分色/造型出问题时，没有原图基本无法定位。
在模板里直接上传即可。

如果是「不确定是不是 bug」的使用问题，放 [Discussions](https://github.com/warm-flame-core/dsh-jlc-art-pcb/discussions) 更合适。

## 提交代码

1. 先开 Issue 说明你要改什么、为什么。涉及行为变化或新增依赖的，先在 Issue 里讨论。
2. Fork 本仓库，在分支上开发。
3. 改动前先跑一遍现有测试：

```bash
node tests/validate.test.mjs        # 工艺校验器，28 项
python tools/test_generic.py <图片目录> <输出目录>   # 通用性回归
```

4. 构建后再提交（`lib/` 需要入库，因为从 GitHub 安装不会跑构建）：

```bash
npm run build          # src/ -> lib/
npm pack --dry-run     # 检查打包边界，确认没有 __pycache__ / 测试产物
```

5. 开 Pull Request，说明改了什么、为什么、怎么验证的。

## 打包规范（重要）

`package.json` 的 `files` 字段是**唯一权威**，它决定用户能拿到什么：

- ✅ 应该包含：`lib/`、`tools/*.py`（明确列出）、`skills/`、`locale/`、`docs/`、`cordis.patch.yml`、`icon.svg`、`NOTICE`、`LICENSE`、`README.md`
- ❌ 不能包含：`__pycache__/`、`*.tgz`、`node_modules/`、测试产物、任何含本机绝对路径的文件

每次改完都跑 `npm pack --dry-run` 核对文件清单，并 grep 一遍本机路径：

```bash
grep -rn "F:\\\\Code\|C:\\\\Users" lib/ tools/ skills/ locale/ docs/
```

## 目录约定

| 目录 | 作用 | 入库 |
|---|---|---|
| `src/` | 插件源码（JS） | ✅ |
| `lib/` | 构建产物（`npm run build` 生成） | ✅ 必须入库 |
| `tools/` | Python 流水线脚本 | ✅ |
| `skills/` | SKILL.md 与 references | ✅ |
| `tests/` | 单测 | ✅ |
| `scripts/` | 构建脚本 | ✅ |

## 风格约定

- 代码注释写「为什么」，不写「是什么」
- Python 脚本统一 UTF-8 输出（Windows 控制台默认 GBK 会炸）
- 新增参数要在 `SKILL.md` 的对应章节说明，否则用户不知道有这个开关
