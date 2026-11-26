import { validateDesign, LIMITS } from '../src/validate.js'

let pass = 0
let fail = 0

function check(name, cond, detail) {
  if (cond) { pass++; console.log(`  ✓ ${name}`) }
  else { fail++; console.log(`  ✗ ${name}${detail ? ' — ' + detail : ''}`) }
}

const has = (r, rule) => r.violations.some(v => v.rule === rule)

console.log('\n[1] 合法的小艺术板（不加任何限制）')
{
  const r = validateDesign({
    board: { widthMm: 60, heightMm: 80 },
    traceWidthsMm: [0.2], traceClearancesMm: [0.2],
    silkLineWidthsMm: [0.25], silkTextHeightsMm: [1.5],
    holeDiametersMm: [2.0], minEdgeClearanceMm: 0.35,
  })
  check('ok=true', r.ok, JSON.stringify(r.violations))
  check('无 error', r.summary.errors === 0)
}

console.log('\n[2] 线宽过细')
{
  const r = validateDesign({ board: { widthMm: 50, heightMm: 50 }, traceWidthsMm: [0.08] })
  check('报 trace.width', has(r, 'trace.width'))
  check('ok=false', !r.ok)
}

console.log('\n[3] 2oz 铜的线宽阈值更严')
{
  const r1 = validateDesign({ board: { widthMm: 50, heightMm: 50 }, board2: {}, traceWidthsMm: [0.12], })
  check('1oz 下 0.12mm 通过', r1.ok, JSON.stringify(r1.violations))

  const r2 = validateDesign({ board: { widthMm: 50, heightMm: 50, outerCopperOz: 2 }, traceWidthsMm: [0.12] })
  check('2oz 下 0.12mm 报错', has(r2, 'trace.width'))
}

console.log('\n[4] 免费券尺寸超限')
{
  const r = validateDesign({ board: { widthMm: 120, heightMm: 80 }, useFreeCoupon: true })
  check('报 coupon.maxSide', has(r, 'coupon.maxSide'))
}
{
  const r = validateDesign({ board: { widthMm: 100, heightMm: 90 }, useFreeCoupon: true })
  check('100x90 恰好合规', r.ok, JSON.stringify(r.violations))
}

console.log('\n[5] 免费券最小边 <10mm')
{
  const r = validateDesign({ board: { widthMm: 8, heightMm: 60 }, useFreeCoupon: true })
  check('报 coupon.minSide', has(r, 'coupon.minSide'))
}

console.log('\n[6] 免费券孔径要求 0.30')
{
  const r = validateDesign({ board: { widthMm: 50, heightMm: 50 }, holeDiametersMm: [0.2], useFreeCoupon: true })
  check('报 coupon.drill', has(r, 'coupon.drill'))
}
{
  const r = validateDesign({ board: { widthMm: 50, heightMm: 50 }, holeDiametersMm: [0.35], useFreeCoupon: true })
  check('0.35 孔通过', r.ok, JSON.stringify(r.violations))
}

console.log('\n[7] 丝印字高：下限 vs 建议值')
{
  const r = validateDesign({ board: { widthMm: 50, heightMm: 50 }, silkTextHeightsMm: [1.05] })
  check('1.05 达标但有 warning', r.ok && has(r, 'silk.textHeight.recommended'))
}
{
  const r = validateDesign({ board: { widthMm: 50, heightMm: 50 }, silkTextHeightsMm: [0.9] })
  check('0.9 报 error', has(r, 'silk.textHeight'))
}
{
  const r = validateDesign({ board: { widthMm: 50, heightMm: 50 }, silkTextHeightsMm: [0.9], tier: 'advanced' })
  check('高精档 0.9 通过', r.ok, JSON.stringify(r.violations))
}

console.log('\n[8] 沉金面积：20% 免费线 vs 30% 加收线')
{
  // 50x50 板，两面面积 5000 mm²。20% = 1000 mm²
  const r1 = validateDesign({
    board: { widthMm: 50, heightMm: 50, finish: 'enig' },
    goldAreaMm2: 900, useFreeCoupon: true,
  })
  check('18% 免费券内通过', r1.ok, JSON.stringify(r1.violations))

  const r2 = validateDesign({
    board: { widthMm: 50, heightMm: 50, finish: 'enig' },
    goldAreaMm2: 1200, useFreeCoupon: true,
  })
  check('24% 报免费券超限', has(r2, 'enig.freeAreaRatio'))

  const r3 = validateDesign({
    board: { widthMm: 50, heightMm: 50, finish: 'enig' },
    goldAreaMm2: 1600, useFreeCoupon: false,
  })
  check('32% 报加收 warning', has(r3, 'enig.surchargeRatio'))
}

console.log('\n[9] 彩色丝印强约束组合')
{
  const base = { widthMm: 60, heightMm: 60, finish: 'enig', maskColor: 'white', outerCopperOz: 1 }
  const ok = validateDesign({ board: { ...base, layers: 2 }, colorSilkscreen: true })
  check('2层+白阻焊+沉金+1oz 通过', ok.ok, JSON.stringify(ok.violations))

  const bad1 = validateDesign({ board: { ...base, layers: 4, maskColor: 'black' }, colorSilkscreen: true })
  check('黑阻焊报错', has(bad1, 'colorSilk.maskColor'))

  const bad2 = validateDesign({ board: { ...base, layers: 6 }, colorSilkscreen: true })
  check('6层报错', has(bad2, 'colorSilk.layers'))

  const bad3 = validateDesign({ board: { ...base, layers: 2, finish: 'osp' }, colorSilkscreen: true })
  check('OSP 报错', has(bad3, 'colorSilk.finish'))

  const bad4 = validateDesign({ board: { ...base, layers: 2, outerCopperOz: 2 }, colorSilkscreen: true })
  check('2oz 报错', has(bad4, 'colorSilk.outerCopper'))
}

console.log('\n[10] 板边距离：锣边 0.20 / V-CUT 0.40')
{
  const r1 = validateDesign({ board: { widthMm: 50, heightMm: 50 }, minEdgeClearanceMm: 0.25 })
  check('锣边 0.25 通过', r1.ok, JSON.stringify(r1.violations))

  const r2 = validateDesign({ board: { widthMm: 50, heightMm: 50, vcut: true }, minEdgeClearanceMm: 0.25 })
  check('V-CUT 0.25 报错', has(r2, 'edge.clearance'))
}

console.log('\n[11] 板厚约束')
{
  const r1 = validateDesign({ board: { widthMm: 50, heightMm: 50, thicknessMm: 1.2 } })
  check('1.2mm 通过', r1.ok, JSON.stringify(r1.violations))

  const r2 = validateDesign({ board: { widthMm: 50, heightMm: 50, thicknessMm: 2.5, layers: 2 } })
  check('2.5mm + 2层 报错', has(r2, 'board.thickness'))

  const r3 = validateDesign({ board: { widthMm: 50, heightMm: 50, thicknessMm: 1.2, finish: 'enig' }, useFreeCoupon: true })
  check('免费沉金券非1.6mm 报错', has(r3, 'coupon.enigThickness'))
}

console.log('\n[12] 非法输入')
{
  const r = validateDesign({ board: { widthMm: 0, heightMm: 50 } })
  check('零宽度被拒', !r.ok && has(r, 'board.dimensions'))
}

console.log(`\n${'='.repeat(50)}`)
console.log(`结果: ${pass} 通过, ${fail} 失败`)
if (fail > 0) process.exit(1)
