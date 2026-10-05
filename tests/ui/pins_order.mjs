// Pins and manual todo order against the live API:
//   node tests/ui/pins_order.mjs <baseUrl> <scratchSessionId> <sessionWithChildrenAndArtifacts> <screenshotDir>
// Creates its own todos in the scratch session and deletes them; pins it sets are removed again.
import { createRequire } from 'node:module';
import fs from 'node:fs';
import path from 'node:path';
const require = createRequire(import.meta.url);
const pwPath = process.env.PLAYWRIGHT_CORE || (process.env.HOME + '/.codex/skills/playwright/node_modules/playwright-core');
const { chromium } = require(pwPath);
const [base, scratch, rich, outDir] = process.argv.slice(2);
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
async function api(method, p, body) {
  const r = await fetch(base + p, { method, headers: { 'X-Actor': 'user', 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(method + ' ' + p + ': ' + (d.error || r.status));
  return d;
}
const view = (sid) => api('GET', '/api/sessions/' + sid);
const tag = 'PO' + Date.now().toString(36);
const mine = (v) => v.todos.filter((t) => t.title.startsWith(tag)).map((t) => t.priority + ' ' + t.title.slice(tag.length + 1));
const created = [];
const pinnedHere = [];

const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_PATH || cachedShell() });
const page = await browser.newPage({ viewport: { width: 1500, height: 1000 } });
const errors = [];
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
page.on('pageerror', (e) => errors.push(String(e)));
const shot = (name) => page.screenshot({ path: path.join(outDir, name) });
async function settled(pred, label) {
  for (let i = 0; i < 60; i++) { if (await pred()) return true; await page.waitForTimeout(250); }
  console.log('timed out waiting: ' + label);
  return false;
}
const card = (name) => page.locator('.todo', { has: page.locator('.todo-title', { hasText: tag + ' ' + name }) });
const domOrder = () => page.$$eval('.todo-list > .todo .todo-title', (els) => els.map((e) => e.textContent.trim()));

try {
  for (const [name, prio] of [['a', 'P1'], ['b', 'P2'], ['c', 'P2'], ['d', 'P2']]) {
    created.push((await api('POST', '/api/sessions/' + scratch + '/todos', { title: tag + ' ' + name, priority: prio })).id);
  }
  check('new todos land at the end of their priority', mine(await view(scratch)).join(',') === 'P1 a,P2 b,P2 c,P2 d', mine(await view(scratch)).join(','));

  await page.goto(base + '/s/' + scratch);
  await page.waitForSelector('.todo-list .drag-handle');
  const dom = (await domOrder()).filter((t) => t.startsWith(tag)).map((t) => t.slice(tag.length + 1));
  check('the list shows priority, then manual order', dom.join(',') === 'a,b,c,d', dom.join(','));
  check('priority groups are labelled', (await page.$$eval('.todo-list > .prio-div', (els) => els.map((e) => e.textContent))).join(',') === 'P1,P2');
  check('every open todo has a labelled drag handle', await card('a').locator('.drag-handle[aria-label^="Reorder"]').count() === 1);

  await card('c').locator('.drag-handle').dragTo(card('b'), { targetPosition: { x: 40, y: 4 } });
  await settled(async () => mine(await view(scratch)).join(',') === 'P1 a,P2 c,P2 b,P2 d', 'drag c above b');
  check('dragging c above b reorders it (saved)', mine(await view(scratch)).join(',') === 'P1 a,P2 c,P2 b,P2 d', mine(await view(scratch)).join(','));
  await settled(async () => (await domOrder()).filter((t) => t.startsWith(tag)).map((t) => t.slice(-1)).join('') === 'acbd', 'dom acbd');
  check('…and the page shows the new order', (await domOrder()).filter((t) => t.startsWith(tag)).map((t) => t.slice(-1)).join('') === 'acbd');
  await shot('20-dragged-within-priority.png');

  const box = await card('a').boundingBox();
  await card('d').locator('.drag-handle').dragTo(card('a'), { targetPosition: { x: 40, y: Math.round(box.height) - 4 } });
  await settled(async () => mine(await view(scratch)).join(',') === 'P1 a,P1 d,P2 c,P2 b', 'drag d below a');
  check('dropping d into the P1 group makes it P1, right after a', mine(await view(scratch)).join(',') === 'P1 a,P1 d,P2 c,P2 b', mine(await view(scratch)).join(','));
  await settled(async () => (await card('d').locator('select.prio').inputValue()) === 'P1', 'd shows P1');
  check('…and its priority picker shows P1', (await card('d').locator('select.prio').inputValue()) === 'P1');

  await card('c').locator('.drag-handle').focus();
  await page.keyboard.press('ArrowDown');
  await settled(async () => mine(await view(scratch)).join(',') === 'P1 a,P1 d,P2 b,P2 c', 'keyboard down');
  check('keyboard: ↓ on the grip moves c below b', mine(await view(scratch)).join(',') === 'P1 a,P1 d,P2 b,P2 c', mine(await view(scratch)).join(','));
  await settled(async () => (await domOrder()).filter((t) => t.startsWith(tag)).map((t) => t.slice(-1)).join('') === 'adbc', 'dom adbc');
  check('…and focus stays on c\'s grip', await page.evaluate((t) => document.activeElement.classList.contains('drag-handle') && document.activeElement.closest('.todo').textContent.includes(t), tag + ' c'));
  await page.keyboard.press('ArrowUp');
  await page.keyboard.press('ArrowUp');
  await settled(async () => mine(await view(scratch)).join(',') === 'P1 a,P1 d,P2 c,P2 b', 'keyboard up');
  await page.waitForTimeout(1500);
  check('a second key press while a move is saving is ignored, not applied to the old order', mine(await view(scratch)).join(',') === 'P1 a,P1 d,P2 c,P2 b', mine(await view(scratch)).join(','));
  await settled(async () => (await domOrder()).filter((t) => t.startsWith(tag)).map((t) => t.slice(-1)).join('') === 'adcb', 'dom adcb');
  await page.keyboard.press('ArrowUp');
  await settled(async () => mine(await view(scratch)).join(',') === 'P1 a,P1 d,P1 c,P2 b', 'keyboard up into P1');
  check('keyboard: ↑ from the top of P2 enters P1 at its end', mine(await view(scratch)).join(',') === 'P1 a,P1 d,P1 c,P2 b', mine(await view(scratch)).join(','));
  await shot('21-keyboard-and-cross-priority.png');

  // pins: session, child session, artifact
  const before = await view(rich);
  const wasPinned = !!before.session.pinned_at;
  await page.goto(base + '/s/' + rich);
  await page.waitForSelector('.session .s-head');
  if (!wasPinned) {
    await page.locator('.s-head button[aria-pressed]').first().click();
    pinnedHere.push(['sessions', rich]);
  }
  await settled(async () => (await page.locator('#tree li.group-h .gname', { hasText: 'Pinned' }).count()) === 1, 'pinned group');
  const firstGroup = await page.$$eval('#tree > ul > li', (els) => {
    const out = []; for (const li of els) { if (li.classList.contains('group-h')) { if (out.length) break; out.push('H:' + li.textContent); continue; } out.push(li.querySelector('.tnode').dataset.id); }
    return out;
  });
  check('a pinned session sits in the sidebar Pinned group', firstGroup[0].startsWith('H:Pinned') && firstGroup.includes(rich), firstGroup.slice(0, 3).join(' | '));
  check('the header button says Pinned and is pressed', (await page.locator('.s-head button[aria-pressed="true"]').first().textContent()).includes('Pinned'));
  check('the sidebar row shows a lit pin', await page.locator('#tree .tnode[data-id="' + rich + '"] .pin-btn.on').count() === 1);

  const kids = before.children.filter((ch) => !ch.pinned_at);
  if (kids.length >= 2) {
    const target = kids[kids.length - 1];
    const row = page.locator('.child', { has: page.locator('.child-title', { hasText: target.display_title }) }).first();
    await row.hover();
    await row.locator('.pin-btn').first().click();
    pinnedHere.push(['sessions', target.id]);
    await settled(async () => (await view(rich)).children[0].id === target.id, 'child first');
    check('a pinned child session moves to the top of Child sessions', (await view(rich)).children[0].id === target.id);
    await settled(async () => ((await page.$$eval('.session > .section .child-title', (els) => els.map((e) => e.textContent)))[0] || '') === target.display_title, 'child dom');
    check('…in the page too, with a lit pin', ((await page.$$eval('.child-title', (els) => els.map((e) => e.textContent)))[0] || '') === target.display_title &&
      await page.locator('.child-top .pin-btn.on').count() >= 1);
  } else check('child pin test needs a session with two unpinned children', false, 'skipped');

  const art = before.artifacts.find((a) => !a.pinned_at && a.kind !== 'canvas');
  if (art) {
    const row = page.locator('.arow[data-ref="artifact:' + art.id + '"]');
    await row.scrollIntoViewIfNeeded();
    await row.hover();
    await row.locator('.pin-btn').click();
    pinnedHere.push(['artifacts', art.id]);
    await settled(async () => (await page.locator('details.agroup', { has: page.locator('summary', { hasText: 'Pinned' }) }).locator('.arow[data-ref="artifact:' + art.id + '"]').count()) === 1, 'artifact pinned group');
    check('a pinned artifact shows in a Pinned group at the top of Artifacts',
      (await page.$$eval('.section details.agroup > summary', (els) => els.map((e) => e.textContent)))[0].startsWith('Pinned') &&
      await page.locator('.arow[data-ref="artifact:' + art.id + '"]').count() === 1);
    await shot('22-pinned-artifact-child-session.png');
  } else check('artifact pin test needs an unpinned artifact', false, 'skipped');
  check('no console errors', errors.length === 0, errors.slice(0, 3).join(' | '));
} catch (e) {
  check('test ran to completion', false, String(e));
} finally {
  for (const [kind, id] of pinnedHere) await api('POST', '/api/' + kind + '/' + id + '/pin', { pinned: false }).catch(() => {});
  for (const id of created) await api('DELETE', '/api/todos/' + id).catch(() => {});
  await browser.close();
}
const failed = results.filter((r) => !r.ok).length;
console.log((results.length - failed) + '/' + results.length + ' passed');
process.exit(failed ? 1 : 0);

