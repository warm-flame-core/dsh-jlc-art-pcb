import * as fs from 'node:fs';
const src = fs.readFileSync('lib/index.js', 'utf8');
// ESM 语法检查：用动态 import data URL
const url = 'data:text/javascript;base64,' + Buffer.from(src).toString('base64');
try {
  await import(url);
  console.log('ESM 解析 OK');
} catch (e) {
  console.log('ESM 解析失败:', e.message);
  process.exit(1);
}
