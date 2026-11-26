/**
 * 嘉立创（JLCPCB）PCB 工艺约束校验 —— 纯函数，无 IO、无状态。
 *
 * 所有阈值来自 `docs/jlc-process-rules.md`，该文件记录了每条规则的官方来源 URL
 * 与抓取日期（2026-11-26）。规则分三档，混用会误报：
 *   - `free`     ：免费打样券档（最严，例如孔径 >= 0.30 mm）
 *   - `standard` ：常规档（默认）
 *   - `advanced` ：加价档（更细的线宽/字高）
 *
 * 本模块不抛异常——设计问题一律以 violation 形式返回。
 */

/** @typedef {'error' | 'warning' | 'info'} Severity */
/** @typedef {{ rule: string, severity: Severity, message: string, source?: string }} Violation */

/** 工艺档位。 */
export const TIERS = ['free', 'standard', 'advanced']

/** 表面处理。 */
export const FINISHES = ['osp', 'enig', 'hasl']

/**
 * 官方工艺阈值表（单位 mm，除注明外）。
 * 每条都带 `source`，便于在输出里追责到官方页面。
 */
export const LIMITS = {
  // 线宽/线距：按外层铜厚区分（1oz 与 2oz 差异极大）
  trace: {
    signal1oz: { width: 0.10, clearance: 0.10, source: 'https://www.jlc.com/portal/vtechnology.html' },
    signal2oz: { width: 0.16, clearance: 0.16, source: 'https://www.jlc.com/portal/vtechnology.html' },
    advanced1oz: { width: 0.076, clearance: 0.076, source: 'https://www.jlc.com/portal/vtechnology.html' },
  },
  // 钻孔：工艺能力 / 免费券门槛 是两回事
  drill: {
    minCapability: 0.15,
    recommended: 0.20,
    freeCouponMin: 0.30,
    source: 'https://www.jlc.com/portal/q7i42818.html',
  },
  // 丝印
  silk: {
    lineWidth: 0.15,
    lineWidthAdvanced: 0.10,
    textHeight: 1.00,
    textHeightAdvanced: 0.80,
    // 装饰板建议值（官方下限只保证"能做"，不保证"好看"）
    recommendedLineWidth: 0.20,
    recommendedTextHeight: 1.20,
    hollowTextWidth: 0.20,
    hollowTextHeight: 1.50,
    toPad: 0.15,
    source: 'https://www.jlc.com/portal/server_guide_10417.html',
  },
  // 板边
  edge: {
    rout: 0.20,
    vcut: 0.40,
    source: 'https://www.jlc.com/portal/server_guide_112.html',
  },
  // 阻焊
  mask: {
    openingOverPad: 0, // 2025-06 起 LDI 对齐，1:1
    bridge1ozDouble: 0.20, // 绿红黄蓝紫
    bridge1ozDoubleBlackWhite: 0.23,
    bridge1ozMulti: 0.10,
    source: 'https://www.jlc.com/portal/q7i61512.html',
  },
  // 免费券
  coupon: {
    maxSide: 100,
    minSide: 10,
    pcs: 5,
    perMonth: 2,
    source: 'https://www.jlc.com/portal/q7i37595.html',
  },
  // 沉金计费
  enig: {
    freeAreaRatio: 0.20, // 免费券门槛，超出不免费
    surchargeRatio: 0.30, // 付费订单超出部分加收起收点
    source: 'https://www.jlc.com/portal/q7i58163.html',
  },
  // 彩色丝印的强约束组合
  colorSilk: {
    allowedLayers: [2, 4],
    requiredMaskColor: 'white',
    requiredFinish: 'enig',
    requiredOuterCopperOz: 1,
    requiredGoldThicknessU: 1,
    maxLogoAllowed: false,
    source: 'https://jlcpcb.com/help/article/how-to-design-multi-color-silkscreen-using-easyeda',
  },
}

/**
 * 校验一块设计。
 *
 * @param {object} input
 * @param {object} input.board
 * @param {number} input.board.widthMm
 * @param {number} input.board.heightMm
 * @param {number} [input.board.layers=2]     铜层数
 * @param {number} [input.board.thicknessMm=1.6]
 * @param {number} [input.board.outerCopperOz=1]
 * @param {string} [input.board.finish='osp']
 * @param {string} [input.board.maskColor='green']
 * @param {boolean} [input.board.vcut=false]  是否 V-CUT 拼板
 * @param {string} [input.tier='standard']    工艺档位
 * @param {number[]} [input.traceWidthsMm]    实际最细线宽列表
 * @param {number[]} [input.traceClearancesMm]
 * @param {number[]} [input.silkLineWidthsMm]
 * @param {number[]} [input.silkTextHeightsMm]
 * @param {number[]} [input.holeDiametersMm]
 * @param {number} [input.minEdgeClearanceMm] 图形/铜距板边最小值
 * @param {number} [input.goldAreaMm2]        单面沉金面积
 * @param {boolean} [input.colorSilkscreen=false]
 * @param {boolean} [input.hasBoardLogo=false]
 * @param {boolean} [input.useFreeCoupon=false]
 * @returns {{ ok: boolean, violations: Violation[], summary: object }}
 */
export function validateDesign(input) {
  /** @type {Violation[]} */
  const violations = []
  const board = input.board

  if (!board || !(board.widthMm > 0) || !(board.heightMm > 0)) {
    return {
      ok: false,
      violations: [{ rule: 'board.dimensions', severity: 'error', message: 'board.widthMm 与 board.heightMm 必须是正数' }],
      summary: { errors: 1, warnings: 0, infos: 0 },
    }
  }

  const layers = board.layers ?? 2
  const thickness = board.thicknessMm ?? 1.6
  const outerOz = board.outerCopperOz ?? 1
  const finish = board.finish ?? 'osp'
  const maskColor = (board.maskColor ?? 'green').toLowerCase()
  const tier = input.tier ?? 'standard'
  const useFree = input.useFreeCoupon === true

  const push = (rule, severity, message, source) =>
    violations.push(source === undefined ? { rule, severity, message } : { rule, severity, message, source })

  // ── 1. 尺寸与免费券 ────────────────────────────────────────────
  const w = board.widthMm
  const h = board.heightMm
  const longest = Math.max(w, h)
  const shortest = Math.min(w, h)

  if (useFree) {
    if (longest > LIMITS.coupon.maxSide) {
      push('coupon.maxSide', 'error',
        `免费券要求单片长宽均 <= ${LIMITS.coupon.maxSide} mm，当前 ${w}×${h} mm 超限，无法使用免费券`,
        LIMITS.coupon.source)
    }
    if (shortest < LIMITS.coupon.minSide) {
      push('coupon.minSide', 'error',
        `免费券要求长或宽 >= ${LIMITS.coupon.minSide} mm，当前最短边 ${shortest} mm 不在免费范围`,
        LIMITS.coupon.source)
    }
  }
  if (shortest < 3) {
    push('board.minSize', 'error', `单板最小尺寸为 3×3 mm，当前最短边 ${shortest} mm`, LIMITS.edge.source)
  }

  // ── 2. 板厚 ───────────────────────────────────────────────────
  const thicknesses = [0.4, 0.6, 0.8, 1.0, 1.2, 1.6, 2.0]
  if (!thicknesses.includes(thickness)) {
    if (thickness >= 2.5 && layers < 12) {
      push('board.thickness', 'error',
        `板厚 ${thickness} mm 仅支持 12 层以上，当前 ${layers} 层`, LIMITS.edge.source)
    } else if (thickness > 4.5) {
      push('board.thickness', 'error', `板厚上限 4.5 mm，当前 ${thickness} mm`, LIMITS.edge.source)
    } else {
      push('board.thickness', 'warning',
        `板厚 ${thickness} mm 不在常规档 (0.4/0.6/0.8/1.0/1.2/1.6/2.0)，可能加价或需定制`, LIMITS.edge.source)
    }
  }
  if (useFree && finish === 'enig' && thickness !== 1.6) {
    push('coupon.enigThickness', 'error',
      `免费沉金券板厚仅限 1.6 mm，当前 ${thickness} mm`, LIMITS.coupon.source)
  }

  // ── 3. 线宽 / 线距 ────────────────────────────────────────────
  const traceLimit = outerOz <= 1
    ? (tier === 'advanced' ? LIMITS.trace.advanced1oz : LIMITS.trace.signal1oz)
    : LIMITS.trace.signal2oz
  for (const width of input.traceWidthsMm ?? []) {
    if (width < traceLimit.width) {
      push('trace.width', 'error',
        `最小线宽 ${width} mm 低于 ${outerOz}oz 铜 ${tier} 档下限 ${traceLimit.width} mm`, traceLimit.source)
    }
  }
  for (const clearance of input.traceClearancesMm ?? []) {
    if (clearance < traceLimit.clearance) {
      push('trace.clearance', 'error',
        `最小线距 ${clearance} mm 低于 ${outerOz}oz 铜 ${tier} 档下限 ${traceLimit.clearance} mm`, traceLimit.source)
    }
  }

  // ── 4. 钻孔 ───────────────────────────────────────────────────
  for (const d of input.holeDiametersMm ?? []) {
    if (d < LIMITS.drill.minCapability) {
      push('drill.min', 'error',
        `孔径 ${d} mm 低于工艺能力下限 ${LIMITS.drill.minCapability} mm`, LIMITS.drill.source)
    } else if (d < LIMITS.drill.recommended) {
      push('drill.recommended', 'warning',
        `孔径 ${d} mm 属非常规小孔（工艺能做但费用高），建议 >= ${LIMITS.drill.recommended} mm`, LIMITS.drill.source)
    }
    if (useFree && d < LIMITS.drill.freeCouponMin) {
      push('coupon.drill', 'error',
        `免费券要求孔径 >= ${LIMITS.drill.freeCouponMin} mm，当前 ${d} mm 会产生小孔难度费`, LIMITS.drill.source)
    }
  }

  // ── 5. 丝印 ───────────────────────────────────────────────────
  const silkLineMin = tier === 'advanced' ? LIMITS.silk.lineWidthAdvanced : LIMITS.silk.lineWidth
  const silkTextMin = tier === 'advanced' ? LIMITS.silk.textHeightAdvanced : LIMITS.silk.textHeight

  for (const lw of input.silkLineWidthsMm ?? []) {
    if (lw < silkLineMin) {
      push('silk.lineWidth', 'error',
        `丝印线宽 ${lw} mm 低于 ${tier} 档下限 ${silkLineMin} mm（低于此值会被印糊或丢失）`, LIMITS.silk.source)
    } else if (lw < LIMITS.silk.recommendedLineWidth) {
      push('silk.lineWidth.recommended', 'warning',
        `丝印线宽 ${lw} mm 虽达标，但装饰板建议 >= ${LIMITS.silk.recommendedLineWidth} mm`, LIMITS.silk.source)
    }
  }
  for (const th of input.silkTextHeightsMm ?? []) {
    if (th < silkTextMin) {
      push('silk.textHeight', 'error',
        `丝印字高 ${th} mm 低于 ${tier} 档下限 ${silkTextMin} mm`, LIMITS.silk.source)
    } else if (th < LIMITS.silk.recommendedTextHeight) {
      push('silk.textHeight.recommended', 'warning',
        `丝印字高 ${th} mm 虽达标，但装饰板建议 >= ${LIMITS.silk.recommendedTextHeight} mm（官方只保证能印，不保证清晰）`,
        LIMITS.silk.source)
    }
  }

  // ── 6. 板边距离 ───────────────────────────────────────────────
  const edgeMin = board.vcut === true ? LIMITS.edge.vcut : LIMITS.edge.rout
  if (input.minEdgeClearanceMm !== undefined && input.minEdgeClearanceMm < edgeMin) {
    push('edge.clearance', 'error',
      `图形距板边 ${input.minEdgeClearanceMm} mm 低于 ${board.vcut ? 'V-CUT' : '锣边'} 下限 ${edgeMin} mm`,
      LIMITS.edge.source)
  }

  // ── 7. 沉金面积 ───────────────────────────────────────────────
  const goldArea = input.goldAreaMm2
  if (goldArea !== undefined && goldArea > 0) {
    // 官方口径：顶底两面沉金焊盘面积之和 ÷ 板两面面积
    const twoSideBoardArea = w * h * 2
    const ratio = goldArea / twoSideBoardArea

    if (useFree && finish === 'enig' && ratio > LIMITS.enig.freeAreaRatio) {
      push('enig.freeAreaRatio', 'error',
        `沉金面积占比 ${(ratio * 100).toFixed(1)}% 超过免费券上限 ${LIMITS.enig.freeAreaRatio * 100}%，将无法免费`,
        LIMITS.enig.source)
    }
    if (ratio > LIMITS.enig.surchargeRatio) {
      push('enig.surchargeRatio', 'warning',
        `沉金面积占比 ${(ratio * 100).toFixed(1)}% 超过 ${LIMITS.enig.surchargeRatio * 100}% 起收点，超出部分将加收大面积沉金费`,
        LIMITS.enig.source)
    }
  }

  // ── 8. 彩色丝印强约束组合 ────────────────────────────────────
  if (input.colorSilkscreen === true) {
    const cs = LIMITS.colorSilk
    if (!cs.allowedLayers.includes(layers)) {
      push('colorSilk.layers', 'error',
        `彩色丝印仅支持 2 层或 4 层，当前 ${layers} 层`, cs.source)
    }
    if (maskColor !== cs.requiredMaskColor) {
      push('colorSilk.maskColor', 'error',
        `彩色丝印必须使用白色阻焊，当前 ${maskColor}`, cs.source)
    }
    if (finish !== cs.requiredFinish) {
      push('colorSilk.finish', 'error',
        `彩色丝印必须搭配沉金(ENIG)，当前 ${finish}`, cs.source)
    }
    if (outerOz !== cs.requiredOuterCopperOz) {
      push('colorSilk.outerCopper', 'error',
        `彩色丝印要求外层铜厚 ${cs.requiredOuterCopperOz} oz，当前 ${outerOz} oz`, cs.source)
    }
    if (useFree) {
      push('colorSilk.coupon', 'warning',
        `彩色丝印不能用普通沉金免费券，需专门的彩色丝印券`, LIMITS.coupon.source)
    }
    if (input.hasBoardLogo === true) {
      push('colorSilk.logo', 'warning',
        `彩色丝印板面不加标志（加标志收 10 元）`, cs.source)
    }
  }

  const errors = violations.filter(v => v.severity === 'error').length
  const warnings = violations.filter(v => v.severity === 'warning').length
  const infos = violations.filter(v => v.severity === 'info').length

  return {
    ok: errors === 0,
    violations,
    summary: { errors, warnings, infos, tier, finish, layers },
  }
}
