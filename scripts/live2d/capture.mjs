// Deterministic frame capture from the real Cubism 5 moc3 in the dragon repo.
// Uses the page's own QA hooks: setAmbientSample(t) freezes the rAF clock and
// drives breath/blink from an explicit time, setReactionSample(id,age) pins a
// reaction channel sample. Screenshots keep page alpha (omitBackground).
import { createRequire } from 'node:module';
import { writeFile, mkdir, rm } from 'node:fs/promises';
import path from 'node:path';

const REPO = 'C:/Users/35336/AppData/Local/Temp/live2d-dragon/outputs/live2d-preview-v4';
const PW = 'C:/Users/35336/AppData/Local/npm-cache/_npx/9833c18b2d85bc59/node_modules/playwright';
const CHROME = 'C:/Users/35336/AppData/Local/ms-playwright/chromium-1243/chrome-win64/chrome.exe';
const OUT = 'C:/Users/35336/AppData/Local/Temp/live2d-out/raw';

const argv = process.argv.slice(2);
const opt = (n, d) => { const i = argv.indexOf(n); return i < 0 ? d : argv[i + 1]; };
const FPS = Number(opt('--fps', 24));
const IDLE_S = Number(opt('--idle', 8));
const REACT_S = Number(opt('--react', 3));
const ONLY = opt('--only', 'both');

const require = createRequire(import.meta.url);
const { chromium } = require(PW);
const { startServer } = await import('file:///' + REPO + '/server.mjs');

await mkdir(OUT, { recursive: true });
const server = await startServer({ port: 0 });
const base = `http://127.0.0.1:${server.address().port}`;
const started = Date.now();
const report = {};
let browser;
try {
  browser = await chromium.launch({
    headless: true, executablePath: CHROME,
    args: ['--enable-unsafe-swiftshader', '--use-gl=angle', '--use-angle=swiftshader', '--disable-gpu-sandbox'],
  });
  const page = await browser.newPage({ viewport: { width: 960, height: 540 }, deviceScaleFactor: Number(opt('--dsf', 1)) });
  page.on('pageerror', e => console.log('pageerror: ' + e.message));
  await page.goto(`${base}/?model=working_thinking&view=pet`, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => ['rendering', 'error', 'missing_model'].includes(window.dragonPreview?.status), null, { timeout: 120000 });
  const ev = await page.evaluate(() => window.dragonPreview);
  if (ev.status !== 'rendering') throw new Error('model did not render: ' + ev.status + ' ' + ev.error);
  report.render = { status: ev.status, mocVersion: ev.mocVersion, drawableCount: ev.drawableCount, nonTransparentPixels: ev.nonTransparentPixels, textureMode: ev.textureMode, activeRenderer: ev.activeRenderer };
  const catalog = await page.evaluate(() => window.dragonModelQA.reactionCatalog);
  report.reactionCatalog = catalog;

  await page.evaluate(() => window.dragonModelQA.reset());
  await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))));

  const shoot = async (file) => {
    await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))));
    await page.screenshot({ path: file, omitBackground: true, animations: 'allow', caret: 'initial' });
  };

  const run = async (dir, n, step, apply) => {
    await rm(dir, { recursive: true, force: true });
    await mkdir(dir, { recursive: true });
    const samples = [];
    for (let i = 0; i < n; i++) {
      const t = i * step;
      const meta = await apply(t, i);
      samples.push(meta);
      await shoot(path.join(dir, String(i).padStart(4, '0') + '.png'));
    }
    return samples;
  };

  if (ONLY !== 'react') {
    const dir = path.join(OUT, 'idle');
    const t0 = Date.now();
    const samples = await run(dir, Math.round(FPS * IDLE_S), 1 / FPS, async (t) => {
      await page.evaluate((time) => window.dragonModelQA.setAmbientSample(time, false), t);
      const s = await page.evaluate(() => {
        const e = window.dragonPreview;
        return { breath: e.ambient?.breath, lift: e.ambient?.breathLiftPx, disp: e.ambient?.maxDisplacementPx, blink: e.blink?.leftClosure, reaction: e.reaction?.active ?? false, leg: e.legSample?.offsetPx ?? e.legSample?.y ?? null };
      });
      return { t, ...s };
    });
    report.idle = { frames: samples.length, dir, ms: Date.now() - t0, first: samples[0], mid: samples[Math.floor(samples.length / 2)] };
  }

  if (ONLY !== 'idle') {
    const tail = catalog.filter(r => r.target === 'tail').sort((a, b) => b.duration - a.duration);
    const pick = tail[0] || catalog.sort((a, b) => b.duration - a.duration)[0];
    const n = Math.max(Math.round(FPS * REACT_S), Math.round(pick.duration * FPS));
    const dir = path.join(OUT, 'react');
    const t0 = Date.now();
    const samples = await run(dir, n, 1 / FPS, async (t) => {
      const age = Math.min(t, pick.duration);
      await page.evaluate((time) => window.dragonModelQA.setAmbientSample(time, false), t);
      await page.evaluate(([id, a]) => window.dragonModelQA.setReactionSample(id, a), [pick.id, age]);
      const s = await page.evaluate(() => {
        const e = window.dragonPreview;
        return { active: e.reaction?.active ?? false, id: e.reaction?.record?.id, pose: e.reaction?.record?.pose, progress: Number((e.reaction?.progress ?? 0).toFixed(3)), channels: e.reaction?.channels ?? null, disp: e.ambient?.maxDisplacementPx, blink: e.blink?.leftClosure };
      });
      return { t, ...s };
    });
    report.react = { id: pick.id, duration: pick.duration, target: pick.target, frames: samples.length, dir, ms: Date.now() - t0, first: samples[0], mid: samples[Math.floor(samples.length / 2)], peak: samples.reduce((a, b) => ((b.disp ?? 0) > (a.disp ?? 0) ? b : a), samples[0]) };
  }
  report.totalMs = Date.now() - started;
  await writeFile('C:/Users/35336/AppData/Local/Temp/live2d-out/capture-report.json', JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
} finally {
  if (browser) await browser.close();
  await new Promise(r => server.close(r));
}
