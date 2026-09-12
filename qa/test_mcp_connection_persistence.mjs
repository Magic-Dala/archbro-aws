import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';

const webRoot = new URL('../frontend/web/', import.meta.url);
const [app, page, styles] = await Promise.all([
  readFile(new URL('app.js', webRoot), 'utf8'),
  readFile(new URL('index.html', webRoot), 'utf8'),
  readFile(new URL('styles.css', webRoot), 'utf8'),
]);

function sourceBetween(start, end) {
  const first = app.indexOf(start);
  const last = app.indexOf(end, first + start.length);
  assert.notEqual(first, -1, `missing ${start}`);
  assert.notEqual(last, -1, `missing ${end}`);
  return app.slice(first, last);
}

test('OAuth completion updates status, connected count, cards, and visible confirmation immediately', () => {
  const completion = sourceBetween(
    'async function completeMcpConnectionUi(',
    'function openMcpOAuthPopup(',
  );
  assert.match(completion, /refreshMcpProviderStatusAfterMutation\(providerId\)/);
  assert.match(completion, /await loadMcpConnections\(\)/);
  assert.match(completion, /announceMcpConnection\(providerId/);
  assert.match(completion, /setMcpPickerTab\('connected'\)/);

  const callback = sourceBetween(
    "window.addEventListener('message'",
    "$('workspaceSwitcherBtn').addEventListener",
  );
  assert.match(callback, /await completeMcpConnectionUi\(providerId/);
  assert.doesNotMatch(callback, /setTimeout\([^)]*loadMcpConnections/);

  assert.match(page, /id="mcpConnectionNotice"[^>]*role="status"[^>]*aria-live="polite"/);
});

test('provider status and browse cards expose durable connected state without changing tabs', () => {
  const status = sourceBetween(
    'function renderMcpOAuthStatus(preset, status)',
    'function renderMcpOAuthStatusError(',
  );
  assert.match(status, /const connected = status\.connected === true/);
  assert.match(status, /const persistent = status\.persistent === true/);
  assert.match(status, /Encrypted at rest/);
  assert.match(status, /Reconnect/);
  assert.match(status, /syncMcpProviderCards\(\)/);

  const cards = sourceBetween(
    'function mcpConnectionForProvider(',
    'function mcpProviderStatusEntry(',
  );
  assert.match(cards, /card\.classList\.toggle\('connected', connected\)/);
  assert.match(cards, /chevron\.textContent = connected \? '✓' : '›'/);
  assert.match(styles, /\.mcp-provider-card\.connected/);
  assert.match(styles, /\.mcp-provider-chevron\.connected/);
});

test('connected rows distinguish encrypted persistence from session-only helpers', () => {
  const loader = sourceBetween(
    'async function loadMcpConnections()',
    'function openMcpConnections()',
  );
  assert.match(loader, /mcpConnectionsSnapshot = Array\.isArray\(connections\)/);
  assert.match(loader, /syncMcpProviderCards\(mcpConnectionsSnapshot\)/);
  assert.match(loader, /connection\.persistent/);
  assert.match(loader, /Saved securely/);
  assert.match(loader, /Session only/);

  assert.match(page, /id="mcpStoragePill"/);
  assert.match(page, /encrypted in PostgreSQL/);
  assert.doesNotMatch(page, /Access and refresh tokens remain backend-only and memory-only/);
});
