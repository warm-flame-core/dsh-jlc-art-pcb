/**
 * jlc-art-pcb 工具集。
 *
 * 两个工具：
 *   jlc_validate_constraints  工艺约束校验（纯 JS，无外部依赖）
 *   jlc_build_artwork         一条命令跑完整流水线（调 Python 脚本）
 *
 * 为什么流水线要走 Python：
 *   图像分色、轮廓追踪、形态学、贝塞尔平滑 —— 这些用 PIL/numpy 实现简洁可靠。
 *   用 JS 重写等于把 2000 行已验证代码重来一遍，且质量更难保证。
 *   代价是用户机器要有 Python + numpy + Pillow。
 *
 *   检测顺序：配置的 pythonPath → 环境变量 JLC_PYTHON → PATH 上的 python/python3。
 */

import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { defineTool, ToolArgsError } from '@deepseek-ai/dsh-tools'
import { validateDesign } from './validate.js'

export const name = 'jlc-art-pcb-tools'
export const inject = ['tools']

const HERE = dirname(fileURLToPath(import.meta.url))
// lib/ 与 tools/ 同级，均在包根下
const TOOL_SCRIPT = join(HERE, '..', 'tools', 'jlc_art.py')

const SEVERITY_ORDER = { error: 0, warning: 1, info: 2 }

/**
 * 递归去掉 undefined / 函数 / Symbol 等非 lossless JSON 值。
 *
 * DSH 的工具返回值必须能被无损 JSON 序列化：对象里出现 undefined、
 * 数组里出现 undefined，整个值都会被判为 not lossless 而拒绝。
 * 可选字段一律在构造时兜底，再统一过一遍这个函数。
 */
function cleanJson(value) {
  if (value === null) return null
  if (Array.isArray(value)) return value.map(cleanJson)
  if (typeof value === 'object') {
    const out = {}
    for (const [k, v] of Object.entries(value)) {
      if (v === undefined || typeof v === 'function' || typeof v === 'symbol') continue
      out[k] = cleanJson(v)
    }
    return out
  }
  if (typeof value === 'function' || typeof value === 'symbol') return null
  if (typeof value === 'number' && !Number.isFinite(value)) return null
  return value
}

/** 把校验结果渲染成模型可读文本。 */
function renderValidation(value) {
  const { summary } = value
  const head = value.ok
    ? `嘉立创工艺校验通过（${summary.tier} 档，${summary.layers} 层，${summary.finish}）`
    : `嘉立创工艺校验未通过：${summary.errors} 个错误 / ${summary.warnings} 个警告`

  if (value.violations.length === 0) return head

  const sorted = [...value.violations].sort(
    (a, b) => (SEVERITY_ORDER[a.severity] ?? 9) - (SEVERITY_ORDER[b.severity] ?? 9),
  )
  const lines = sorted.map(v => {
    const tail = v.source === undefined ? '' : `\n    依据: ${v.source}`
    return `- [${v.severity}] ${v.rule}: ${v.message}${tail}`
  })
  return `${head}\n\n${lines.join('\n')}`
}

/** 解析 Python 解释器路径（同步，仅用于报错展示）。 */
function resolvePython(configured) {
  if (configured && configured.trim() && existsSync(configured.trim())) return configured.trim()
  const fromEnv = process.env.JLC_PYTHON
  if (fromEnv && fromEnv.trim() && existsSync(fromEnv.trim())) return fromEnv.trim()
  return process.platform === 'win32' ? 'python.exe' : 'python3'
}

/**
 * 找出**能真正用**的 Python：必须同时 import 成功 numpy 与 PIL。
 *
 * 为什么不能只看 PATH：Windows 上常同时存在系统 Python（无 numpy/Pillow）、
 * Microsoft Store 的 stub、以及打包运行时的 Python。实测系统 Python 3.13
 * 能启动但缺 PIL，直接跑会 ModuleNotFoundError。
 *
 * 顺序：配置 → 环境变量 → PATH 上的 python/python3 → 常见打包路径。
 */
async function findUsablePython(configured, timeoutMs = 15000) {
  const candidates = []
  const add = p => { if (p && !candidates.includes(p)) candidates.push(p) }

  if (configured && configured.trim()) add(configured.trim())
  if (process.env.JLC_PYTHON) add(process.env.JLC_PYTHON.trim())

  if (process.platform === 'win32') {
    add('python.exe')
    add('py.exe')
    const local = process.env.LOCALAPPDATA
    if (local) {
      add(`${local}\\Programs\\Python\\Python313\\python.exe`)
      add(`${local}\\Programs\\Python\\Python312\\python.exe`)
      add(`${local}\\Programs\\Python\\Python311\\python.exe`)
    }
    const home = process.env.USERPROFILE
    if (home) add(`${home}\\.dsh\\dsh-runtimes\\dsh-primary-runtime\\dependencies\\python\\python.exe`)
  } else {
    add('python3')
    add('python')
  }

  const tried = []
  for (const c of candidates) {
    if (c.includes('\\') || c.includes('/')) {
      if (!existsSync(c)) { tried.push(`${c} (不存在)`); continue }
    }
    try {
      const r = await run(c, ['-c', 'import numpy, PIL; print("ok")'], timeoutMs)
      if (r.code === 0 && r.stdout.includes('ok')) {
        return { ok: true, python: c, tried }
      }
      tried.push(`${c} (缺 numpy/PIL)`)
    } catch (e) {
      tried.push(`${c} (${e.message.slice(0, 40)})`)
    }
  }
  return { ok: false, python: null, tried }
}

/** 跑一个子进程，收集 stdout/stderr。 */
function run(cmd, args, timeoutMs, signal) {
  return new Promise((resolvePromise, rejectPromise) => {
    const child = spawn(cmd, args, { windowsHide: true })
    let stdout = ''
    let stderr = ''
    let settled = false

    const timer = setTimeout(() => {
      if (settled) return
      settled = true
      try { child.kill() } catch { /* ignore */ }
      rejectPromise(new Error(`执行超时（${Math.round(timeoutMs / 1000)}s）`))
    }, timeoutMs)

    const onAbort = () => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      try { child.kill() } catch { /* ignore */ }
      rejectPromise(new Error('已取消'))
    }
    if (signal) {
      if (signal.aborted) { onAbort(); return }
      signal.addEventListener('abort', onAbort, { once: true })
    }

    child.stdout.on('data', d => { stdout += d.toString('utf8') })
    child.stderr.on('data', d => { stderr += d.toString('utf8') })
    child.on('error', err => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      rejectPromise(new Error(`无法启动 ${cmd}：${err.message}`))
    })
    child.on('close', code => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      if (signal) signal.removeEventListener('abort', onAbort)
      resolvePromise({ code, stdout, stderr })
    })
  })
}

export function apply(ctx, config = {}) {
  const pythonPath = config.pythonPath ?? ''


  // ── 工具 0：环境自检 ───────────────────────────────────────────
  // 用户侧的 DSH 不一定装了 Python / numpy / Pillow / EDA。
  // 这一条用来在动手前把「缺什么、怎么补」一次说清。
  ctx.tools.register(defineTool({
    name: 'jlc_check_environment',
    description:
      '检查本插件运行所需的外部依赖是否齐全：Python 3.9+、numpy、Pillow'
      + '（图像流水线必需），以及嘉立创 EDA 的 lceda-pro CLI 与已打开窗口（落板用）。'
      + '返回每项的状态、版本、路径，以及缺失时的修复建议与官方下载链接。'
      + '在第一次生成艺术板之前跑一次。',
    parameters: {
      timeoutMs: { type: 'integer', description: '超时毫秒，默认 60000。' },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          ok: { type: 'boolean', required: true },
          platform: { type: 'string' },
          checks: {
            type: 'array',
            required: true,
            items: {
              type: 'object',
              additionalProperties: false,
              properties: {
                id: { type: 'string', required: true },
                name: { type: 'string', required: true },
                required: { type: 'boolean', required: true },
                ok: { type: 'boolean', required: true },
                version: { type: 'string' },
                path: { type: 'string' },
                detail: { type: 'string' },
                fix: { type: 'string' },
                action: { type: 'string' },
              },
            },
          },
          hints: {
            type: 'array',
            items: {
              type: 'object',
              additionalProperties: false,
              properties: {
                id: { type: 'string', required: true },
                detail: { type: 'string' },
                fix: { type: 'string' },
                action: { type: 'string' },
              },
            },
          },
          error: { type: 'string' },
        },
      },
      render: (_a, v) => {
        if (v.ok === false && v.error) return [{ type: 'text', text: `环境自检失败：${v.error}` }]
        const lines = [`环境自检（${v.platform ?? ''}）`]
        for (const c of v.checks ?? []) {
          const mark = c.ok ? '✓' : (c.required ? '✗ 必需' : '○ 可选')
          const ver = c.version ? ` ${c.version}` : ''
          lines.push(`${mark} ${c.name}${ver}`)
          if (!c.ok && c.fix) lines.push(`    修复：${c.fix}`)
          if (!c.ok && c.action) lines.push(`    链接：${c.action}`)
        }
        const blocking = (v.checks ?? []).filter(c => c.required && !c.ok)
        lines.push('', blocking.length === 0
          ? '必需依赖齐全，可以生成艺术板。'
          : `缺少 ${blocking.length} 项必需依赖，流水线无法运行。`)
        return [{ type: 'text', text: lines.join('\n') }]
      },
    },
    async execute(_args, exec) {
      const found = await findUsablePython(pythonPath)
      if (!found.ok) {
        return cleanJson({
          ok: false,
          platform: '',
          checks: [],
          hints: [],
          error: `找不到任何可用的 Python。已尝试：${found.tried.join('; ')}`,
        })
      }
      const script = join(HERE, '..', 'tools', 'doctor.py')
      if (!existsSync(script)) {
        return cleanJson({ ok: false, platform: '', checks: [], hints: [], error: `缺少 doctor.py: ${script}` })
      }
      let res
      try {
        res = await run(found.python, [script], 60000, exec?.signal)
      } catch (err) {
        return cleanJson({ ok: false, platform: '', checks: [], hints: [], error: err.message })
      }
      let data
      try {
        data = JSON.parse(res.stdout.trim())
      } catch {
        return cleanJson({
          ok: false, platform: '', checks: [], hints: [],
          error: `解析 doctor 输出失败：${(res.stdout || res.stderr).slice(-300)}`,
        })
      }
      return cleanJson({
        ok: data.ok === true,
        platform: String(data.platform ?? ''),
        checks: (data.checks ?? []).map(c => ({
          id: String(c.id ?? ''),
          name: String(c.name ?? ''),
          required: c.required === true,
          ok: c.ok === true,
          version: String(c.version ?? ''),
          path: String(c.path ?? ''),
          detail: String(c.detail ?? ''),
          fix: String(c.fix ?? ''),
          action: String(c.action ?? ''),
        })),
        hints: (data.hints ?? []).map(h => ({
          id: String(h.id ?? ''),
          detail: String(h.detail ?? ''),
          fix: String(h.fix ?? ''),
          action: String(h.action ?? ''),
        })),
      })
    },
    presentCall: () => ({ card: 'generic', title: '检查插件运行环境', kind: 'other' }),
  }))

  // ── 工具 0：画布预检（纯 JS 生成 JS，交给 Python 跑 EDA）──────
  // 目的是在落板前回答：工程里有几块板？目标板上有没有东西？该新建还是复用？
  // 不预检就写入，可能覆盖用户已有的作品。
  ctx.tools.register(defineTool({
    name: 'jlc_audit_canvas',
    description:
      '检查当前 EasyEDA 工程与各 PCB 的画布状态：每块板有多少图元、有没有板框。'
      + '在往 EDA 写入艺术板之前必须先跑一次，用来决定「新建 PCB / 复用空板 / 询问用户」。'
      + '直接写入已存在的板会破坏用户已有作品。'
      + '需要本机 EasyEDA Pro 桌面版 ≥4.1.60 与一个已打开的 session。',
    parameters: {
      sessionId: {
        type: 'string',
        required: true,
        description: 'EasyEDA session id（用 `lceda-pro session list` 取）。',
      },
      timeoutMs: { type: 'integer', description: '超时毫秒，默认 180000。' },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          ok: { type: 'boolean', required: true },
          projectName: { type: 'string' },
          pcbCount: { type: 'integer' },
          pcbs: {
            type: 'array',
            items: {
              type: 'object',
              additionalProperties: false,
              properties: {
                name: { type: 'string', required: true },
                uuid: { type: 'string', required: true },
                primitives: { type: 'integer', required: true },
                hasBoardOutline: { type: 'boolean', required: true },
                detail: { type: 'string' },
              },
            },
          },
          action: { type: 'string', description: 'create_pcb / reuse_pcb / ask_user' },
          pcbUuid: { type: 'string' },
          reason: { type: 'string' },
          error: { type: 'string' },
        },
      },
      render: (_args, v) => {
        if (!v.ok) return [{ type: 'text', text: `画布预检失败：${v.error ?? '未知错误'}` }]
        const lines = [`工程「${v.projectName}」共 ${v.pcbCount} 块 PCB：`]
        for (const p of v.pcbs) {
          const flags = [p.hasBoardOutline ? '有板框' : '无板框']
          if (p.detail) flags.push(p.detail)
          lines.push(`- ${p.name}: ${p.primitives} 个图元（${flags.join(', ')}）`)
        }
        const label = { create_pcb: '新建 PCB', reuse_pcb: '复用空板', ask_user: '需询问用户' }[v.action] ?? v.action
        lines.push('', `建议：${label} —— ${v.reason ?? ''}`)
        return [{ type: 'text', text: lines.join('\n') }]
      },
    },
    async execute(args, exec) {
      const found = await findUsablePython(pythonPath)
      if (!found.ok) {
        return cleanJson({
          ok: false,
          error: `找不到可用的 Python（需要 numpy 与 Pillow）。已尝试：${found.tried.join('; ')}。`
            + '请在插件配置里设置 pythonPath，或设环境变量 JLC_PYTHON。',
        })
      }
      const py = found.python
      const script = join(HERE, '..', 'tools', 'canvas_audit.py')
      if (!existsSync(script)) {
        return cleanJson({ ok: false, error: `找不到 canvas_audit.py：${script}` })
      }
      let res
      try {
        res = await run(py, [script, args.sessionId], args.timeoutMs ?? 180000, exec?.signal)
      } catch (err) {
        return cleanJson({ ok: false, error: err.message })
      }
      if (res.code !== 0 && !res.stdout.trim()) {
        return cleanJson({ ok: false, error: (res.stderr || '无输出').slice(-400) })
      }
      let data
      try {
        data = JSON.parse(res.stdout.trim())
      } catch {
        return cleanJson({ ok: false, error: `解析失败：${res.stdout.slice(-300)}` })
      }
      if (!data.ok) return cleanJson({ ok: false, error: data.error ?? '预检返回失败' })

      const rec = data.recommendation ?? {}
      const pcbs = (data.pcbs ?? []).map(p => {
        const byType = p.byType ?? {}
        const detail = Object.keys(byType).length > 0
          ? Object.entries(byType).map(([k, n]) => `${k}×${n}`).join(' ')
          : ''
        return {
          name: String(p.name ?? ''),
          uuid: String(p.uuid ?? ''),
          primitives: Number(p.primitives ?? 0),
          hasBoardOutline: p.hasBoardOutline === true,
          detail,
        }
      })

      return cleanJson({
        ok: true,
        projectName: String(data.project?.name ?? ''),
        pcbCount: Number(data.pcbCount ?? pcbs.length),
        pcbs,
        action: String(rec.action ?? 'ask_user'),
        pcbUuid: rec.pcbUuid === undefined ? '' : String(rec.pcbUuid),
        reason: String(rec.reason ?? ''),
      })
    },
    presentCall: () => ({ card: 'generic', title: '检查 EDA 画布状态', kind: 'other' }),
  }))

  // ── 工具 1：工艺校验（纯 JS）──────────────────────────────────
  ctx.tools.register(defineTool({
    name: 'jlc_validate_constraints',
    description:
      '校验 PCB 艺术板设计是否满足嘉立创（JLCPCB）可制造性约束：线宽/线距、钻孔、'
      + '丝印线宽与字高、板边距离、沉金面积占比、免费打样券规格，'
      + '以及彩色丝印的强约束组合（2/4 层 + 白阻焊 + 沉金 + 1oz 铜 + 1U" 金厚 + 板面无标志）。'
      + '设计问题一律以 violations 返回，不会因为设计不合规而抛错。'
      + '工艺阈值分 free / standard / advanced 三档，按实际下单渠道选择。',
    parameters: {
      board: {
        type: 'object',
        required: true,
        additionalProperties: false,
        description: '板子的基本信息。',
        properties: {
          widthMm: { type: 'number', required: true, description: '板宽（毫米）。' },
          heightMm: { type: 'number', required: true, description: '板高（毫米）。' },
          layers: { type: 'integer', enum: [1, 2, 4, 6], description: '铜层数，默认 2。' },
          thicknessMm: { type: 'number', description: '板厚（毫米），默认 1.6。' },
          outerCopperOz: { type: 'number', description: '外层铜厚（oz），默认 1。' },
          finish: {
            type: 'string',
            enum: ['osp', 'enig', 'hasl'],
            description: '表面处理。免费券默认 osp；想要金色必须 enig；彩色丝印必须 enig。',
          },
          maskColor: {
            type: 'string',
            enum: ['green', 'red', 'yellow', 'blue', 'white', 'black', 'purple'],
            description: '阻焊颜色。彩色丝印必须 white。',
          },
          vcut: { type: 'boolean', description: '是否 V-CUT 拼板；V-CUT 的板边距要求更严。' },
        },
      },
      tier: {
        type: 'string',
        enum: ['free', 'standard', 'advanced'],
        description: '工艺档位：free=免费券档（最严）、standard=常规档（默认）、advanced=加价高精档。',
      },
      useFreeCoupon: { type: 'boolean', description: '是否用免费打样券；为 true 时额外校验尺寸/孔径/板厚限制。' },
      colorSilkscreen: { type: 'boolean', description: '是否用彩色丝印；为 true 时校验六项强约束。' },
      hasBoardLogo: { type: 'boolean', description: '板面是否带标志；彩色丝印工艺对此收费。' },
      traceWidthsMm: { type: 'array', items: { type: 'number' }, description: '最细线宽列表（毫米）。' },
      traceClearancesMm: { type: 'array', items: { type: 'number' }, description: '最小线距列表（毫米）。' },
      silkLineWidthsMm: { type: 'array', items: { type: 'number' }, description: '丝印最细线宽列表（毫米）。' },
      silkTextHeightsMm: { type: 'array', items: { type: 'number' }, description: '丝印最小字高列表（毫米）。' },
      holeDiametersMm: { type: 'array', items: { type: 'number' }, description: '所有钻孔直径（毫米）。' },
      minEdgeClearanceMm: { type: 'number', description: '图形/铜距板边最小距离（毫米）。' },
      goldAreaMm2: { type: 'number', description: '顶底层沉金焊盘面积之和（平方毫米）。' },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          ok: { type: 'boolean', required: true },
          summary: {
            type: 'object',
            required: true,
            additionalProperties: false,
            properties: {
              errors: { type: 'integer', required: true },
              warnings: { type: 'integer', required: true },
              infos: { type: 'integer', required: true },
              tier: { type: 'string', required: true },
              finish: { type: 'string', required: true },
              layers: { type: 'integer', required: true },
            },
          },
          violations: {
            type: 'array',
            required: true,
            items: {
              type: 'object',
              additionalProperties: false,
              properties: {
                rule: { type: 'string', required: true },
                severity: { type: 'string', required: true, enum: ['error', 'warning', 'info'] },
                message: { type: 'string', required: true },
                source: { type: 'string' },
              },
            },
          },
        },
      },
      render: (_args, value) => [{ type: 'text', text: renderValidation(value) }],
    },
    execute(args) {
      const result = validateDesign({
        ...args,
        tier: args.tier ?? 'standard',
        board: {
          layers: 2, thicknessMm: 1.6, outerCopperOz: 1, finish: 'osp', maskColor: 'green',
          ...args.board,
        },
      })
      return Promise.resolve({
        ok: result.ok,
        summary: result.summary,
        violations: result.violations.map(v =>
          v.source === undefined
            ? { rule: v.rule, severity: v.severity, message: v.message }
            : { rule: v.rule, severity: v.severity, message: v.message, source: v.source },
        ),
      })
    },
    presentCall: () => ({ card: 'generic', title: '校验嘉立创工艺约束', kind: 'other' }),
  }))

  // ── 工具 2：流水线（调 Python）────────────────────────────────
  ctx.tools.register(defineTool({
    name: 'jlc_build_artwork',
    description:
      '把一张插画转成可打样的 PCB 艺术板：分色 → 裁到板框 → 矢量化平滑 → '
      + '生成 EasyEDA 脚本 + 成品效果图。产物写入 workDir。'
      + '生成的是异形板（板框贴合图形剪影）+ 可选挂孔。'
      + '需要本机有 Python 3 与 numpy、Pillow。'
      + '不会自动写入 EDA——生成的 JS 需用 lceda-pro invoke 执行。',
    parameters: {
      sourceImage: { type: 'string', required: true, description: '源图绝对路径（PNG/JPG）。' },
      workDir: { type: 'string', required: true, description: '输出目录绝对路径。' },
      widthMm: { type: 'number', description: '板宽（毫米），默认 55.6。高度按原图比例自动算。' },
      style: {
        type: 'string',
        enum: ['tritone', 'lineart', 'halftone', 'solid'],
        description:
          '造型风格（PCB 只有板底色/金/白三个色调，这是"如何把彩色图压成三色"的开关）。'
          + 'tritone=三色调（默认，适合大多数彩色插画）；lineart=线稿（只让深色线条成金）；'
          + 'halftone=半调网点（中间调用点阵）；solid=实心剪影（整块金，会超免费沉金券 20% 上限）。',
      },
      maxGoldRatio: {
        type: 'number',
        description: '金色面积占比上限，默认 0.20（免费沉金券门槛）。超过会自动下调分档。',
      },
      addHole: { type: 'boolean', description: '是否加挂孔（挂件类需要）。可选，不影响板框生成。' },
      holeDiameterMm: { type: 'number', description: '挂孔直径（毫米），默认 2.0。' },
      maskColor: {
        type: 'string',
        enum: ['black', 'white', 'green', 'purple', 'blue', 'red', 'yellow'],
        description: '效果图用的阻焊色，默认 black。不影响生成的文件，只影响预览。',
      },
      finish: {
        type: 'string',
        enum: ['enig', 'osp'],
        description:
          '工艺/券开关，决定创作约束：enig=沉金（金色，但铜/金面积自动钳制到 ≤20% 以保住免费券，'
          + '仅 2-4 层、仅 1.6mm）；osp=（铜色，无面积限制、1-6 层、0.8-1.6mm，'
          + '但整板露铜会失去对比）。solid 风格只能用 osp。',
      },
      timeoutMs: { type: 'integer', description: '超时毫秒，默认 180000。' },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          ok: { type: 'boolean', required: true },
          workDir: { type: 'string', required: true },
          scriptPath: { type: 'string' },
          previewPath: { type: 'string' },
          boardWidthMm: { type: 'number' },
          boardHeightMm: { type: 'number' },
          goldRatio: { type: 'number' },
          polygonCount: { type: 'integer' },
          style: { type: 'string' },
          stdoutTail: { type: 'string' },
          error: { type: 'string' },
        },
      },
      render: (_args, v) => {
        if (!v.ok) return [{ type: 'text', text: `构建失败：${v.error ?? '未知错误'}\n${v.stdoutTail ?? ''}` }]
        const gold = v.goldRatio === undefined ? '' : `\n金色占比: ${(v.goldRatio * 100).toFixed(1)}%`
        return [{
          type: 'text',
          text: `艺术板已生成${gold}\n`
            + `板面: ${v.boardWidthMm} x ${v.boardHeightMm} mm（异形）\n`
            + `风格: ${v.style ?? '三色调'}\n`
            + `轮廓: ${v.polygonCount} 个\n`
            + `EDA 脚本: ${v.scriptPath}\n`
            + `效果图: ${v.previewPath ?? '未生成'}\n`
            + `产物目录: ${v.workDir}`,
        }]
      },
    },
    async execute(args, exec) {
      if (!existsSync(args.sourceImage)) {
        throw new ToolArgsError([`sourceImage 不存在: ${args.sourceImage}`])
      }
      if (!existsSync(TOOL_SCRIPT)) {
        return {
          ok: false, workDir: args.workDir,
          error: `找不到流水线脚本: ${TOOL_SCRIPT}。这是包安装不完整，请重装插件。`,
        }
      }

      const found = await findUsablePython(pythonPath)
      if (!found.ok) {
        return cleanJson({
          ok: false, workDir: args.workDir,
          error: `找不到可用的 Python（需要 numpy 与 Pillow）。已尝试：${found.tried.join('; ')}。`
            + '请在插件配置里设置 pythonPath，或设环境变量 JLC_PYTHON。',
        })
      }
      const py = found.python
      const timeout = args.timeoutMs ?? 180000
      const workDir = resolve(args.workDir)
      const script = join(workDir, 'write.js')
      const preview = join(workDir, 'final.png')

      const argv = [
        TOOL_SCRIPT, 'pipeline', args.sourceImage, workDir,
        '--width-mm', String(args.widthMm ?? 55.6),
        '--js', script,
        '--png', preview,
        '--mask', args.maskColor ?? 'black',
        '--finish', args.finish ?? 'enig',
        '--style', args.style ?? 'tritone',
        '--max-gold', String(args.maxGoldRatio ?? 0.20),
      ]
      if (args.addHole) {
        argv.push('--hole', '--hole-dia', String(args.holeDiameterMm ?? 2.0))
      }

      let res
      try {
        res = await run(py, argv, timeout, exec?.signal)
      } catch (err) {
        return cleanJson({
          ok: false, workDir, scriptPath: script,
          error: `${err.message}。若提示找不到 Python，请设置插件配置 pythonPath，`
            + `或环境变量 JLC_PYTHON 指向 python.exe。`,
        })
      }

      const tail = (res.stdout + (res.stderr ? '\n[stderr]\n' + res.stderr : '')).slice(-1200)

      if (res.code !== 0) {
        return cleanJson({ ok: false, workDir, scriptPath: script, stdoutTail: tail, error: `Python 退出码 ${res.code}` })
      }

      // 从产物里读真实尺寸
      let boardWidthMm, boardHeightMm, goldRatio, polygonCount, usedStyle
      try {
        const specPath = join(workDir, 'spec.json')
        const polysPath = join(workDir, 'polys.json')
        const { readFileSync } = await import('node:fs')
        const spec = JSON.parse(readFileSync(specPath, 'utf8'))
        boardWidthMm = spec.widthMm
        boardHeightMm = spec.heightMm
        goldRatio = spec.goldRatioTwoSides
        usedStyle = spec.styleName ?? spec.style ?? '' 
        const polys = JSON.parse(readFileSync(polysPath, 'utf8'))
        polygonCount = Object.values(polys.layers).reduce((n, a) => n + a.length, 0)
      } catch { /* 尺寸读不到不影响主流程 */ }

      return cleanJson({
        ok: true,
        workDir,
        scriptPath: script,
        previewPath: existsSync(preview) ? preview : '',
        boardWidthMm,
        boardHeightMm,
        goldRatio,
        polygonCount,
        style: usedStyle ?? '',
        stdoutTail: tail.slice(-600),
      })
    },
    presentCall: args => ({
      card: 'generic',
      title: `生成艺术板 ${args.widthMm ?? 55.6}mm`,
      kind: 'other',
      rawInput: args.sourceImage,
    }),
    timeoutMs: 240000,
  }))
}
