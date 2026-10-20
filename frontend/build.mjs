import { build } from 'esbuild';
import { copyFile, mkdir } from 'node:fs/promises';
await mkdir(new URL('./dist/', import.meta.url), { recursive: true });
await build({ entryPoints: ['src/app.ts'], bundle: true, outfile: 'dist/app.js', target: ['safari16'], minify: true, sourcemap: false });
if (process.argv.includes('--tests')) {
  await mkdir(new URL('./tests/.runtime/', import.meta.url), { recursive: true });
  await build({ entryPoints: ['src/domain.ts'], bundle: true, outfile: 'tests/.runtime/domain.mjs', platform: 'node', format: 'esm', target: ['node18'] });
}
await Promise.all(['index.html', 'styles.css'].map(file => copyFile(file, `dist/${file}`)));
