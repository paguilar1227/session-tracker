// UI journey: node tests/ui/journey.mjs <baseUrl> <sessionId> <screenshotDir>
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
const shot = (page, name, full) => page.screenshot({ path: path.join(outDir, name), fullPage: !!full });

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
const consoleErrors = [];
page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });
page.on('pageerror', (e) => consoleErrors.push(String(e)));
const revealed = [];
await page.route('**/api/artifacts/*/reveal', (route) => { revealed.push(route.request().url()); route.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' }); });

// 1. Home
await page.goto(base + '/');
await page.waitForSelector('.home .stat');
check('home shows status overview', await page.locator('.home .stat').count() >= 5);
await shot(page, '01-home.png');

// 2. Session view
await page.goto(base + '/s/' + sid);
await page.waitForSelector('.session .s-title');
const sections = await page.locator('.session > .grid2 h2, .session > section h2').allTextContents();
check('session view has todos, ledger, artifacts, child sessions', ['Todos', 'Ledger', 'Artifacts', 'Child sessions'].every((s) => sections.some((t) => t.includes(s))), sections.join(' | '));
check('tree highlights current session', await page.locator('.tnode.active').count() === 1);
await shot(page, '02-session.png');

// 3. Todo with markdown + emoji
const title = 'UI journey todo ' + Date.now();
await page.fill('.add-row input[type=text]', title);
await page.selectOption('.add-row select.prio', 'P1');
await page.locator('.add-row select').nth(1).selectOption('in_progress');
await page.click('.add-row .btn.primary');
const card = page.locator('.todo', { hasText: title });
await card.waitFor();
const todoId = (await (await page.request.get(base + '/api/sessions/' + sid)).json()).todos.find((t) => t.title === title).id;
check('todo created with priority + state', (await card.locator('select.prio').inputValue()) === 'P1' && (await card.locator('select.state').inputValue()) === 'in_progress');
check('todo shows created/updated timestamps', /created .* · updated /.test(await card.locator('.times').textContent()));
await card.locator('button', { hasText: 'Notes' }).click();
const ta = card.locator('.editor textarea');
await ta.fill('Ship it **now** :rocket: and remember:\n- [ ] check the ');
await card.locator('.ed-bar .emoji-btn').click();
await page.fill('.emoji-pop input', 'tada');
await page.locator('.emoji-grid button').first().click();
await card.locator('.ed-bar button', { hasText: 'Preview' }).click();
const previewText = await card.locator('.ed-preview').textContent();
check('editor preview renders shortcode + picked emoji', previewText.includes('🚀') && previewText.includes('🎉'), previewText.trim().slice(0, 80));
await shot(page, '03-editor-preview.png');
await card.locator('.ed-save').click();
await page.waitForFunction((t) => { const c = [...document.querySelectorAll('.todo')].find((x) => x.textContent.includes(t)); return c && c.querySelector('.notes strong'); }, title);
const saved = page.locator('.todo', { hasText: title });
check('saved notes render markdown bold + emoji', (await saved.locator('.notes strong').textContent()) === 'now' && (await saved.locator('.notes').textContent()).includes('🚀'));

// 4. State change, URL attachment, reference upload
// Mark the card, change state, then wait for the server response AND the re-rendered card (the live-update
// stream keeps the network busy forever, so 'networkidle' is not usable).
await page.evaluate((t) => { [...document.querySelectorAll('.todo')].find((x) => x.textContent.includes(t)).dataset.stale = '1'; }, title);
const patched = page.waitForResponse((r) => r.url().includes('/api/todos/') && r.request().method() === 'PATCH');
await saved.locator('select.state').selectOption('blocked');
await patched;
await page.waitForFunction((t) => { const c = [...document.querySelectorAll('.todo')].find((x) => x.textContent.includes(t)); return c && !c.dataset.stale && c.querySelector('select.state').value === 'blocked'; }, title);
check('state change persists (blocked)', true);
await page.locator('.todo', { hasText: title }).locator('button', { hasText: 'Attach' }).click();
await page.fill('.popover input[type=url]', 'https://example.com/design');
await page.locator('.popover .btn.primary').first().click();
await page.locator('.todo', { hasText: title }).locator('.chip a[href="https://example.com/design"]').waitFor();
check('URL attachment added', true);
await page.locator('.todo', { hasText: title }).locator('button', { hasText: 'Attach' }).click();
await page.locator('.popover select').selectOption({ index: 1 });
await page.locator('.popover .btn.primary').nth(1).click();
await page.waitForFunction((t) => { const c = [...document.querySelectorAll('.todo')].find((x) => x.textContent.includes(t)); return c && c.querySelectorAll('.chip').length >= 2; }, title);
const attachKinds = (await (await page.request.get(base + '/api/sessions/' + sid)).json()).todos.find((t) => t.title === title).attachments.map((a) => a.kind);
check('existing artifact attached to todo (artifact link)', attachKinds.includes('artifact'), attachKinds.join(','));
const refName = 'journey-ref-' + Date.now() + '.txt';
const refPath = path.join(outDir, refName);
fs.writeFileSync(refPath, 'reference document body');
await page.locator('.todo', { hasText: title }).locator('input[type=file]').setInputFiles(refPath);
await page.locator('.todo', { hasText: title }).locator('.chip', { hasText: refName }).waitFor();
check('reference upload attached to todo', true);
const view = await (await page.request.get(base + '/api/sessions/' + sid)).json();
const ref = view.artifacts.find((a) => a.title === 'references/' + refName);
check('reference stored under <artifacts folder>/references/', ref && ref.path.endsWith('/references/' + refName) && fs.existsSync(ref.path), ref && ref.path);
await shot(page, '04-todo-attachments.png');

// 5. Ledger: record a mistake
await page.locator('.tab', { hasText: 'Mistakes' }).click();
await page.locator('.section .sec-head .btn', { hasText: 'Record mistake' }).click();
const mTitle = 'Polled the API every 100ms ' + Date.now();
await page.fill('.entry.editing input[placeholder^="Mistake"]', mTitle);
await page.fill('.entry.editing input[placeholder^="Why it was"]', 'Assumed polling was cheap without measuring');
await page.fill('.entry.editing input[placeholder^="Lesson"]', 'Use the SSE stream for live updates :zap:');
await page.fill('.entry.editing textarea', 'It **hammered** the CPU');
await page.locator('.entry.editing .ed-save').click();
const mCard = page.locator('.entry', { hasText: mTitle });
await mCard.waitFor();
check('mistake recorded with lesson + markdown body', (await mCard.textContent()).includes('Lesson') && (await mCard.locator('.notes strong').textContent()) === 'hammered');
check('lesson renders emoji shortcode', (await mCard.locator('.lesson').textContent()).includes('⚡'));
check('mistakes are permanent in the UI (no edit, lock shown)', (await mCard.locator('button[title^="Edit"]').count()) === 0 && (await mCard.textContent()).includes('permanent'));
const mId = (await mCard.getAttribute('data-ref')).split(':')[1];
const patchMistake = await page.request.patch(base + '/api/ledger/' + mId, { data: { title: 'rewritten' } });
check('mistakes are permanent in the API (PATCH rejected)', patchMistake.status() === 400);
const USER = { 'X-Actor': 'user' };
const created = { todos: [], ledger: [mId], feedback: [] };
// Correction: appended on top, original kept
await mCard.locator('button', { hasText: 'Correct' }).click();
await page.fill('input[aria-label="Corrected cause"]', 'Polling was chosen without checking that an event stream existed');
await page.fill('input[aria-label="Corrected lesson"]', 'Check for an event stream before writing any polling loop :zap:');
await mCard.locator('.editing .ed-save').click();
await page.waitForFunction((t) => { const c = [...document.querySelectorAll('.entry')].find((x) => x.textContent.includes(t)); return c && c.textContent.includes('Lesson (corrected)'); }, mTitle);
const mFixed = page.locator('.entry', { hasText: mTitle });
const mApi = (await (await page.request.get(base + '/api/sessions/' + sid)).json()).ledger.mistake.find((e) => String(e.id) === mId);
check('correction appended: corrected lesson shown, original kept in history',
  (await mFixed.locator('.lesson').textContent()).includes('event stream') && (await mFixed.locator('details.corr-history').count()) === 1 &&
  mApi.lesson.includes('SSE stream') && mApi.effective_lesson.startsWith('Check for an event stream'));
const agentCorr = await page.request.post(base + '/api/ledger/' + mId + '/corrections', { data: { lesson: 'agent rewrite' } });
check('only the user can add corrections (agent request rejected)', agentCorr.status() === 403);
await mFixed.locator('details.corr-history summary').click();
await shot(page, '05b-mistake-corrected.png');
// Issue: title is editable in the UI
await page.locator('.tab', { hasText: 'Issues' }).click();
await page.locator('.section .sec-head .btn', { hasText: 'Record issue' }).click();
const iTitle = 'Flaky upload test ' + Date.now();
await page.fill('.entry.editing input[placeholder^="Issue"]', iTitle);
await page.locator('.entry.editing .ed-save').click();
const iCard = page.locator('.entry', { hasText: iTitle });
await iCard.waitFor();
created.ledger.push((await iCard.getAttribute('data-ref')).split(':')[1]);
await iCard.locator('button[title="Edit issue"]').click();
await page.fill('.issue-form input[aria-label="Issue title"]', iTitle + ' (renamed)');
await page.locator('.issue-form .ed-save').click();
await page.locator('.entry', { hasText: iTitle + ' (renamed)' }).waitFor();
check('issue title is editable in the UI', true);
// Missed a mistake: a labelled turn for the scribe's test set
await page.locator('.section .sec-head .btn', { hasText: 'Missed a mistake' }).click();
await page.waitForFunction(() => document.querySelectorAll('.entry.editing select option').length > 1);
await page.locator('.entry.editing select').selectOption({ index: 1 });
const missedNote = 'Journey check ' + Date.now() + ': the agent skipped the tests';
await page.fill('.entry.editing textarea', missedNote);
await page.locator('.entry.editing .ed-save').click();
await page.waitForTimeout(500);
const fb = (await (await page.request.get(base + '/api/feedback?session=' + sid)).json()).feedback.filter((f) => f.note === missedNote);
created.feedback.push(...fb.map((f) => f.id));
check('missed-mistake report saved as feedback for a turn', fb.length === 1 && fb[0].kind === 'missed' && !!fb[0].turn_id);
await page.locator('.tab', { hasText: 'Decisions' }).click();
await page.locator('.section .sec-head .btn', { hasText: 'Record decision' }).click();
const dTitle = 'Use SQLite for local state ' + Date.now();
await page.fill('.entry.editing input[placeholder^="What was decided"]', dTitle);
await page.fill('.entry.editing input[placeholder^="Why"]', 'local and fast');
await page.locator('.entry.editing .ed-save').click();
const dCard = page.locator('.entry', { hasText: dTitle });
await dCard.waitFor();
created.ledger.push((await dCard.getAttribute('data-ref')).split(':')[1]);
await dCard.locator('button[title="Edit decision"]').click();
await page.fill('.decision-form input[aria-label="Decision title"]', dTitle + ' (WAL)');
await page.fill('.decision-form input[aria-label="Rationale"]', 'local, fast, durable :rocket:');
await page.fill('.decision-form input[aria-label="Rejected alternatives"]', 'Postgres · JSON files');
await page.locator('.decision-form .ed-save').click();
const dEdited = page.locator('.entry', { hasText: dTitle + ' (WAL)' });
await dEdited.waitFor();
const dText = await dEdited.textContent();
check('decision is editable (title, rationale with emoji, alternatives)', dText.includes('🚀') && dText.includes('Postgres · JSON files'));
await page.locator('.tab', { hasText: 'Mistakes' }).click();
await page.locator('.entry', { hasText: mTitle }).waitFor();
await shot(page, '05-ledger-mistakes.png');
check('artifact rows render as elements (regression)', (await page.locator('.arow').count()) >= 3);

// 6. Artifacts: HTML, Markdown, unrenderable, Show in Finder, canvas
await page.locator('.arow', { hasText: 'spec.html' }).locator('button', { hasText: 'View' }).click();
const frame = page.frameLocator('.viewer-body iframe');
await frame.locator('h1').waitFor();
check('HTML artifact renders in sandboxed iframe', (await page.locator('.viewer-body iframe').getAttribute('sandbox')).includes('allow-scripts') && (await frame.locator('h1').textContent()).includes('Session Tracker'));
const htmlSrc = await page.locator('.viewer-body iframe').getAttribute('src');
check('HTML artifact loads from its own origin (a<id>.localhost)', /^http:\/\/a\d+\.localhost:\d+\/raw\/\d+\//.test(htmlSrc), htmlSrc);
await shot(page, '06-viewer-html.png');
await page.locator('.viewer-head button', { hasText: 'Show in Finder' }).click();
await page.waitForTimeout(300);
check('Show in Finder calls the reveal endpoint', revealed.length >= 1, revealed[0]);
await page.keyboard.press('Escape');
await page.locator('.arow', { hasText: 'pilot-summary.md' }).locator('button', { hasText: 'View' }).click();
await page.locator('.viewer-body .md table').waitFor();
check('Markdown artifact renders (table + emoji shortcode)', (await page.locator('.viewer-body .md').textContent()).includes('🎉'));
await shot(page, '07-viewer-markdown.png');
await page.keyboard.press('Escape');
const refRow = page.locator('.arow', { hasText: refName });
check('unrenderable file offers Show in Finder (no View)', (await refRow.locator('button', { hasText: 'View' }).count()) === 0 && (await refRow.locator('button', { hasText: 'Show in Finder' }).count()) === 1);
await page.locator('.arow', { hasText: 'Product launch plan' }).locator('button', { hasText: 'View' }).click();
await page.waitForTimeout(2500);
const canvasSrc = await page.locator('.viewer-body iframe').getAttribute('src');
check('Workflow Canvas embeds the linked document', canvasSrc.includes('?doc=launch-plan'), canvasSrc);
await shot(page, '08-viewer-canvas.png');
await page.keyboard.press('Escape');

// 7. Child session expands recursively
const child = page.locator('.child').first();
await child.locator('button', { hasText: 'Expand' }).click();
await child.locator('.nested .session .s-title').waitFor();
const nestedSections = await child.locator('.nested .session h2').allTextContents();
check('child session expands into the same view', ['Todos', 'Ledger', 'Artifacts', 'Child sessions'].every((s) => nestedSections.some((t) => t.includes(s))));
await child.scrollIntoViewIfNeeded();
await shot(page, '09-child-expanded.png');

// 8. Search
await page.fill('#search', 'polling');  // the mistake recorded earlier in this run
await page.locator('.search-hit').first().waitFor();
check('full-text search returns hits', (await page.locator('.search-hit').count()) >= 1);
await shot(page, '10-search.png');
await page.keyboard.press('Escape');

// 9. Dark mode
await page.click('#theme');
await shot(page, '11-dark.png');
check('dark mode toggles', await page.evaluate(() => document.documentElement.classList.contains('dark')));

check('no console errors', consoleErrors.length === 0, consoleErrors.slice(0, 3).join(' | '));
// Clean up everything this run created in the session
await page.request.delete(base + '/api/todos/' + todoId, { headers: USER });
for (const id of created.ledger) await page.request.delete(base + '/api/ledger/' + id, { headers: USER });
for (const id of created.feedback) await page.request.delete(base + '/api/feedback/' + id, { headers: USER });
fs.unlinkSync(refPath);
if (ref && fs.existsSync(ref.path)) fs.unlinkSync(ref.path);
await browser.close();
fs.writeFileSync(path.join(outDir, 'results.json'), JSON.stringify(results, null, 1));
const failed = results.filter((r) => !r.ok);
console.log(results.length - failed.length + '/' + results.length + ' checks passed');
process.exit(failed.length ? 1 : 0);

