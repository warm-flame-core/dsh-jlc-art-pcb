// 构建脚本：把 src/ 编译为可直接运行的 lib/。
//
// 本包不依赖 TypeScript 或打包器 —— 源码本来就是标准 ESM，只需复制并保证
// Node 能解析 `@deepseek-ai/dsh-tools` 这类宿主提供的 peer 依赖（linked 安装时
// 由 DSH 的 peer-aware 解析接管，无需随包安装）。

import { mkdir, readdir, copyFile, rm, stat } from 'node:fs/promises'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = dirname(fileURLToPath(import.meta.url)).replace(/[\\/]scripts$/, '')
const srcDir = join(root, 'src')
const libDir = join(root, 'lib')

await rm(libDir, { recursive: true, force: true })
await mkdir(libDir, { recursive: true })

const entries = await readdir(srcDir, { withFileTypes: true })
let count = 0
for (const entry of entries) {
  if (!entry.isFile() || !entry.name.endsWith('.js')) continue
  await copyFile(join(srcDir, entry.name), join(libDir, entry.name))
  count++
}

const entry = join(libDir, 'index.js')
try {
  await stat(entry)
} catch {
  console.error(`build failed: ${entry} not produced`)
  process.exit(1)
}

console.log(`built ${count} file(s) -> lib/`)
