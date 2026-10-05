// Sidebar sort + filter checks against the live API: node tests/ui/sidebar.mjs <baseUrl> <sessionId> <screenshotDir>
import { createRequire } from 'node:module';
import fs from 'node:fs';
import path from 'node:path';
const require = createRequire(import.meta.url);
const pwPath = process.env.PLAYWRIGHT_CORE || (process.env.HOME + '/.codex/skills/playwright/node_modules/playwright-core');
const { chromium } = require(pwPath);
const [base, sid, outDir] = process.argv.slice(2);
fs.mkdirSync(outDir, { recursive: true });
const results = [];
function check(name, ok, detail) { results.push({ name, ok: !!ok, detail }); console.log((ok ? 'PASS ' : 'FAIL ') + name + (detail ? ' — ' + detail : '')); }
function cachedShell() {
  const cache = process.env.HOME + '/Library/Caches/ms-playwright';
  if (!fs.existsSync(cache)) return undefined;
  const dirs = fs.readdirSync(cache).filter((d) => d.startsWith('chromium_headless_shell-')).sort().reverse();
  for (const d of dirs) {
    const exe = path.join(cache, d, 'chrome-headless-shell-mac-arm64', 'chrome-headless-shell');
    if (fs.existsSync(exe)) return exe;
  }
  return undefined;
}
const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_PATH || cachedShell() });
const page = await browser.newPage({ viewport: { width: 1500, height: 1000 } });
const errors = [];
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
page.on('pageerror', (e) => errors.push(String(e)));
const shot = (name) => page.screenshot({ path: path.join(outDir, name) });

const tree = (await (await fetch(base + '/api/tree')).json()).roots;
const flat = []; (function walk(ns) { ns.forEach((n) => { flat.push(n); walk(n.children); }); })(tree);
const byId = Object.fromEntries(flat.map((n) => [n.id, n]));
const rootIds = () => page.$$eval('#tree > ul > li > .tnode', (els) => els.map((e) => e.dataset.id));
const matchIds = () => page.$$eval('#tree .tnode:not(.ctx)', (els) => els.map((e) => e.dataset.id));
const same = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);
// Pinned sessions sit in their own group above the rest; each part keeps the chosen sort.
const sorted = (cmp) => [...tree.filter((n) => n.pinned_at).sort(cmp), ...tree.filter((n) => !n.pinned_at).sort(cmp)].map((n) => n.id);
const unpinned = tree.filter((n) => !n.pinned_at);

await page.goto(base + '/s/' + sid);
await page.waitForSelector('#tree .tnode');
await page.waitForSelector('.session .sec-head h2');
check('icons load and render as SVG', await page.locator('.sec-head h2 svg.i path').count() >= 4);
const emoji = await page.$$eval('button, h2, summary, .src, .mini, .health, .times, .s-meta, .tnode', (els) =>
  els.filter((e) => !e.closest('.md, .emoji-pop, .tt, .s-title, .entry-title, .todo-title')).map((e) => e.textContent).join(' ')
    .match(/\p{Extended_Pictographic}/gu));
check('no emoji left in UI chrome', !emoji, emoji ? emoji.join(' ') : '');
await shot('10-session-icons.png');

check('default sort is last activity, newest first', same(await rootIds(), sorted((a, b) => b.activity_at - a.activity_at)));
await page.selectOption('#sort-key', 'created');
check('sort by created, newest first', same(await rootIds(), sorted((a, b) => b.created_at - a.created_at)));
await page.click('#sort-dir');
check('direction toggle gives oldest first', same(await rootIds(), sorted((a, b) => a.created_at - b.created_at)) &&
  (await page.textContent('#sort-dir')).includes('Oldest first'));
await page.selectOption('#sort-key', 'project');
const groups = (await page.$$eval('#tree > ul > li.group-h', (els) => els.map((e) => [e.querySelector('.gname').textContent, Number(e.querySelector('.count').textContent)])))
  .filter((g, i) => !(i === 0 && g[0] === 'Pinned' && tree.some((n) => n.pinned_at)));
const names = groups.map((g) => g[0]);
const expected = [...new Set(unpinned.map((n) => n.project))].sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base', numeric: true }));
check('sort by project groups A–Z with headers and counts', same(names, expected) &&
  groups.every((g) => g[1] === unpinned.filter((n) => n.project === g[0]).length), names.slice(0, 6).join(', '));
check('project group titles are headings', (await page.locator('#tree li.group-h h3').count()) === groups.length + (tree.some((n) => n.pinned_at) ? 1 : 0));
await shot('11-sorted-by-project.png');

await page.click('#filter-btn');
check('filter panel opens as a dialog', (await page.getAttribute('#filter-btn', 'aria-expanded')) === 'true' && await page.locator('.filter-pop[role=dialog]').isVisible());
await page.locator('.filter-pop button.fchip', { hasText: 'Waiting on you' }).click();
const waiting = await matchIds();
check('status filter shows only waiting sessions (ancestors dimmed)', waiting.length > 0 && waiting.every((id) => byId[id].status === 'waiting') &&
  waiting.length === flat.filter((n) => n.status === 'waiting').length, waiting.length + ' match');
const proj = tree.find((n) => n.status === 'waiting').project;
await page.locator('.filter-pop .fopt', { hasText: proj }).first().locator('input').check();
const both = await matchIds();
check('project filter combines with status', both.length > 0 && both.every((id) => byId[id].status === 'waiting' && byId[id].project === proj), proj + ': ' + both.length);
await shot('12-filter-panel.png');
await page.keyboard.press('Escape');
check('Escape closes the panel and returns focus', (await page.locator('.filter-pop').count()) === 0 && await page.evaluate(() => document.activeElement.id) === 'filter-btn');
check('active filters show as chips with a count and badge', (await page.locator('#filter-bar .achip').count()) === 2 &&
  (await page.textContent('#filter-bar .fcount')).includes('match') && (await page.textContent('#filter-btn .n')) === '2');

await page.reload();
await page.waitForSelector('#tree .tnode');
check('sort and filters persist across reload', (await page.inputValue('#sort-key')) === 'project' && (await page.locator('#filter-bar .achip').count()) === 2);
await page.locator('#filter-bar .linkish', { hasText: 'Clear all' }).click();
await page.click('#filter-btn');
await page.locator('.filter-pop label.fchip', { hasText: 'Last 7 days' }).click();
await page.keyboard.press('ArrowLeft');
const viaArrow = (await page.textContent('#filter-bar')).includes('Active today');
await page.keyboard.press('ArrowRight');
check('arrow keys move between Last active options', viaArrow && (await page.textContent('#filter-bar')).includes('Active in the last 7 days'));
await page.locator('.filter-pop button.fchip', { hasText: 'Open todos' }).click();
const recent = await matchIds();
const since = Date.now() - 7 * 86400000;
check('activity + open-todos filters', recent.every((id) => byId[id].updated_at >= since - 60000 && byId[id].counts.todos_open > 0), recent.length + ' match');
await page.focus('.filter-pop .fpop-foot .btn.primary');
await page.keyboard.press('Tab');
check('tabbing out of the panel closes it', (await page.locator('.filter-pop').count()) === 0);
await page.locator('#filter-bar .linkish', { hasText: 'Clear all' }).click();
check('clear all restores every session', (await page.locator('#filter-bar .achip').count()) === 0 && (await rootIds()).length === tree.length);
await page.fill('#tree-filter', proj.toLowerCase());
const typed = await matchIds();
check('text filter matches project names too', typed.length > 0 && typed.every((id) => byId[id].project === proj || byId[id].title.toLowerCase().includes(proj.toLowerCase())));
await page.fill('#tree-filter', '');
await page.selectOption('#sort-key', 'activity');
await page.click('#filter-btn');
await page.locator('.filter-pop .fopt.arch input').check();
await page.locator('.filter-pop button.fchip', { hasText: 'Archived' }).waitFor({ timeout: 5000 }).catch(() => {});
check('including archived refreshes the panel counts', (await page.locator('.filter-pop button.fchip', { hasText: 'Archived' }).count()) === 1);
await page.locator('.filter-pop .fopt.arch input').uncheck();
await page.keyboard.press('Escape');

const sideW = () => page.$eval('#sidebar', (e) => Math.round(e.getBoundingClientRect().width));
async function drag(dx) {
  const box = await page.locator('#side-resizer').boundingBox();
  const x = box.x + box.width / 2, y = box.y + 300;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + dx, y, { steps: 6 });
  await page.mouse.up();
  await page.waitForTimeout(50);
}
const w0 = await sideW();
check('resize handle is an accessible separator at the default width', w0 === 330 &&
  (await page.getAttribute('#side-resizer', 'role')) === 'separator' && (await page.getAttribute('#side-resizer', 'aria-valuenow')) === '330');
await drag(200);
const w1 = await sideW();
check('dragging the handle widens the session list', Math.abs(w1 - 530) <= 2, String(w1));
await page.reload();
await page.waitForSelector('#tree .tnode');
check('width is remembered after reload', Math.abs((await sideW()) - w1) <= 1);
await drag(-2000);
const wMin = await sideW();
const fits = await page.$$eval('.side-tools > .side-row', (rows) => rows.every((r) => r.scrollWidth <= r.clientWidth + 1));
check('narrowest width still fits the sort and filter controls', fits && String(wMin) === (await page.getAttribute('#side-resizer', 'aria-valuemin')), String(wMin));
await drag(3000);
const mainW = await page.$eval('main', (e) => e.getBoundingClientRect().width);
check('widest width leaves the session pane at least 570px', mainW >= 569, Math.round(mainW) + 'px');
await page.focus('#side-resizer');
const before = await sideW();
await page.keyboard.press('ArrowLeft');
check('arrow keys resize from the keyboard', (await sideW()) === before - 10);
await page.dblclick('#side-resizer');
check('double-click resets to the default width', (await sideW()) === 330 && (await page.evaluate(() => localStorage.getItem('st-side-width'))) === null);
await page.click('#theme');
await page.click('#filter-btn');
await shot('13-filter-panel-dark.png');
await page.keyboard.press('Escape');
await page.click('#theme');
check('no console errors', errors.length === 0, errors.join(' | '));
await browser.close();
const failed = results.filter((r) => !r.ok);
console.log(JSON.stringify({ passed: results.length - failed.length, failed: failed.length }));
process.exit(failed.length ? 1 : 0);
