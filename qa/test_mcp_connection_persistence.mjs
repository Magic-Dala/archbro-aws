import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';

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

test('OAuth completion reconciles status, connection list, cards, and visible confirmation immediately', () => {
  const reconcile = sourceBetween(
    'async function reconcileMcpProviderConnection(',
    'async function completeMcpConnectionUi(',
  );
  assert.match(reconcile, /Promise\.all\(\[/);
  assert.match(reconcile, /requestMcpProviderStatus\(providerId, \{force: true\}\)/);
  assert.match(reconcile, /loadMcpConnections\(\)/);
  assert.match(reconcile, /renderMcpOAuthStatus\(preset, status\)/);
  assert.match(reconcile, /announceMcpConnection\(providerId/);
  assert.match(reconcile, /setMcpPickerTab\('connected'\)/);
  assert.match(reconcile, /archbro:mcp-connections-changed/);

  const completion = sourceBetween(
    'async function completeMcpConnectionUi(',
    'function mcpProviderStatusEntry(',
  );
  assert.match(completion, /reconcileMcpProviderConnection\(providerId/);

  const callback = sourceBetween(
    "window.addEventListener('message'",
    "$('workspaceSwitcherBtn').addEventListener",
  );
  assert.match(callback, /await completeMcpConnectionUi\(providerId/);
  assert.doesNotMatch(callback, /setTimeout\([^)]*loadMcpConnections/);

  assert.match(page, /id="mcpConnectionNotice"[^>]*role="status"[^>]*aria-live="polite"/);
});

test('public OAuth status falls back to process memory only when the backend explicitly allows it', () => {
  const resolver = sourceBetween(
    'async function resolveMcpProviderStatus(',
    'function invalidateMcpProviderStatus(',
  );
  assert.match(resolver, /generic\?\.legacy_fallback_allowed !== true/);
  assert.match(resolver, /Boolean\(generic\?\.restore_error\)/);
  assert.ok(
    resolver.indexOf('legacy_fallback_allowed !== true')
      < resolver.indexOf('mcpLegacyProviderStatusEndpoint'),
    'legacy status lookup must be fenced before selecting its endpoint',
  );
});

test('provider status and browse cards expose durable connected and restore-error states', () => {
  const status = sourceBetween(
    'function renderMcpOAuthStatus(preset, status)',
    'function renderMcpOAuthStatusError(',
  );
  assert.match(status, /const connected = status\.connected === true/);
  assert.match(status, /const persistent = status\.persistent === true/);
  assert.match(status, /status\.restore_error/);
  assert.match(status, /Reconnect required/);
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

test('popup-close fallback performs full reconciliation and reports only a new connection', async () => {
  const watcher = sourceBetween(
    'function watchMcpOAuthPopup(',
    'function applyMcpPreset(',
  );
  assert.match(app, /watchMcpOAuthPopup\(popup, preset, \{wasConnected: status\.connected === true\}\)/);

  async function closePopup({wasConnected, connected}) {
    let intervalCallback = null;
    const toasts = [];
    const tabs = [];
    const popup = {closed: true};
    const elements = {
      mcpConnectionsDialog: {open: true},
      mcpPreset: {value: 'github-remote'},
    };
    const context = {
      popup,
      mcpOAuthProviderId: () => 'github',
      setInterval: (callback) => { intervalCallback = callback; return 1; },
      clearInterval() {},
      activeMcpOAuthPopup: null,
      handledMcpOAuthPopups: new WeakSet(),
      setTimeout: (callback) => { callback(); return 1; },
      $: (id) => elements[id],
      reconcileMcpProviderConnection: async () => ({
        connected,
        status: {name: 'GitHub'},
      }),
      toast: (message) => toasts.push(message),
      setMcpPickerTab: (tab) => tabs.push(tab),
      renderMcpOAuthStatusError() {},
    };
    runInNewContext(
      `${watcher}; watchMcpOAuthPopup(popup, 'github-remote', {wasConnected:${wasConnected}});`,
      context,
    );
    assert.equal(typeof intervalCallback, 'function');
    await intervalCallback();
    return {toasts, tabs};
  }

  assert.deepEqual(
    await closePopup({wasConnected: false, connected: true}),
    {toasts: ['GitHub connected to your ArchBro account.'], tabs: ['connected']},
  );
  assert.deepEqual(
    await closePopup({wasConnected: true, connected: true}),
    {toasts: [], tabs: []},
  );
  assert.deepEqual(
    await closePopup({wasConnected: false, connected: false}),
    {toasts: [], tabs: []},
  );
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
