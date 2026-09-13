import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import {runInNewContext} from 'node:vm';

const webRoot = new URL('../frontend/web/', import.meta.url);

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#039;',
  })[character]);
}

test('Agent composer preserves multiline prompts and defines an explicit send shortcut', async () => {
  const [page, app, css] = await Promise.all([
    readFile(new URL('index.html', webRoot), 'utf8'),
    readFile(new URL('app.js', webRoot), 'utf8'),
    readFile(new URL('styles.css', webRoot), 'utf8'),
  ]);
  assert.match(page, /<textarea id="instruction"[^>]*rows="2"/);
  assert.doesNotMatch(page, /<input id="instruction"/);
  assert.match(page, /Enter adds a new line · Ctrl\/⌘ \+ Enter sends/);
  assert.match(page, /id="agentConversationDialog"/);
  assert.match(page, /data-expand-agent-conversation/);
  assert.match(app, /event\.key === 'Enter' && \(event\.ctrlKey \|\| event\.metaKey\)/);
  assert.match(app, /requestSubmit\(\)/);
  assert.match(app, /syncInstructionTextareaRows/);
  assert.match(css, /\.instruction-input textarea/);
  assert.match(css, /max-height:184px/);
});

test('Agent rich response renderer safely keeps paragraphs lists tables code and evidence', async () => {
  const app = await readFile(new URL('app.js', webRoot), 'utf8');
  const start = app.indexOf('function renderAgentInline(');
  const end = app.indexOf('function syncInstructionTextareaRows(', start);
  assert.ok(start >= 0 && end > start);
  const renderer = app.slice(start, end);
  const value = `## Progress\n\n- Frontend: VERIFIED\n- Runtime: UNVERIFIED\n\n| Component | Status |\n| --- | --- |\n| MCP | PARTIAL |\n\nVerified sources: GitHub MCP README.md\n\n\`safe\` <script>alert(1)</script>`;
  const html = runInNewContext(`${renderer};renderAgentRichText(value)`, {value, escapeHtml});
  assert.match(html, /<h3>Progress<\/h3>/);
  assert.match(html, /<ul>/);
  assert.match(html, /<table>/);
  assert.match(html, /agent-evidence-reference/);
  assert.match(html, /<code>safe<\/code>/);
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /&lt;script&gt;/);
});

test('static asset query hashes match the multiline composer bundle', async () => {
  const page = await readFile(new URL('index.html', webRoot), 'utf8');
  for (const asset of ['app.js', 'styles.css']) {
    const body = await readFile(new URL(asset, webRoot));
    const normalized = body.toString('utf8').replace(/\r\n/g, '\n');
    const hash = createHash('sha256').update(normalized).digest('hex').slice(0, 16);
    assert.ok(page.includes(`/static/${asset}?v=${hash}`), `${asset} must use its current content hash`);
  }
});

async function render(value) {
  const app = await readFile(new URL('app.js', webRoot), 'utf8');
  const renderer = app.slice(app.indexOf('function renderAgentInline('), app.indexOf('function syncInstructionTextareaRows('));
  return runInNewContext(`${renderer};renderAgentRichText(value)`, {value, escapeHtml});
}

test('live progress response preserves seven section numbers and their nested evidence', async () => {
  const value = Array.from({length: 7}, (_, index) => `${index + 1}. **Boundary ${index + 1}**: PARTIAL\n   - Evidence: \`src/${index + 1}\`\n`).join('\n');
  const html = await render(value);
  assert.deepEqual([...html.matchAll(/<li value="(\d+)">/g)].map(match => Number(match[1])), [1, 2, 3, 4, 5, 6, 7]);
  assert.equal((html.match(/<ol>/g) || []).length, 1);
  for (let number = 1; number <= 7; number++) {
    assert.ok(html.includes(`<li value="${number}"><strong>Boundary ${number}</strong>: PARTIAL<ul><li>Evidence: <code>src/${number}</code></li></ul></li>`));
  }
});

test('list numbering survives intervening paragraphs and mixed nested lists remain escaped', async () => {
  const html = await render('4. First\n   More context.\n   - Evidence <script>bad()</script>\n     2. Nested step\n\nIndependent paragraph.\n\n7) Last');
  assert.match(html, /<li value="4">First<p>More context\.<\/p><ul><li>Evidence &lt;script&gt;bad\(\)&lt;\/script&gt;<ol><li value="2">Nested step<\/li><\/ol><\/li><\/ul><\/li>/);
  assert.match(html, /<p>Independent paragraph\.<\/p><ol><li value="7">Last<\/li><\/ol>/);
  assert.doesNotMatch(html, /<script>/);
});

test('deeply nested untrusted lists remain bounded and escaped', async () => {
  const html = await render(Array.from({length: 100}, (_, index) => `${'  '.repeat(index)}- <script>nested</script>`).join('\n'));
  assert.match(html, /<pre>/);
  assert.doesNotMatch(html, /<script>/);
});
