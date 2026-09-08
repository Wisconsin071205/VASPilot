import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { chromium } = require('playwright');

const url = process.argv[2];
const outDir = process.argv[3];
if (!url || !outDir) throw new Error('usage: capture_ui.mjs URL OUT_DIR');

const browser = await chromium.launch({
  headless: true,
  executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
});
const page = await browser.newPage({
  viewport: { width: 1600, height: 1000 },
  deviceScaleFactor: 1,
});
page.setDefaultTimeout(8_000);

await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30_000 });
await page.waitForTimeout(4_000);

const captures = [
  ['chat', 'ui-01-chat.png'],
  ['files', 'ui-02-files.png'],
  ['jobs', 'ui-03-jobs.png'],
  ['flow', 'ui-04-workflow.png'],
  ['monitor', 'ui-05-monitor.png'],
  ['cfg', 'ui-06-settings.png'],
];

for (const [view, file] of captures) {
  await page.evaluate((name) => {
    document.querySelector(`.tab[data-v="${name}"]`)?.click();
  }, view);
  await page.waitForTimeout(view === 'monitor' ? 2_000 : 700);
  await page.screenshot({ path: `${outDir}/${file}`, fullPage: false });
}

await browser.close();
