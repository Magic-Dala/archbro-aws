import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

function loadSurface(connections, projectServers = []) {
  const bridge = {
    getCommittedNavigation: () => ({initialized: true, project_id: 'project-a'}),
    getActiveProjectBinding: () => ({projectId: 'project-a', generation: 1, repositoryRevision: 7}),
  };
  const requests = [];
  const scope = {
    window: {ArchBroWebBridge: bridge},
    console,
    URL,
    URLSearchParams,
    AbortController,
    getFirebaseIdToken: async () => 'fixture-token',
    fetch: async (path) => {
      requests.push(path);
      const body = path === '/mcp/connections'
        ? connections
        : path === '/projects/project-a/mcp/servers'
          ? {project_id: 'project-a', servers: projectServers}
          : (() => { throw new Error(`unexpected request: ${path}`); })();
      return {ok: true, status: 200, json: async () => structuredClone(body)};
    },
  };
  vm.createContext(scope);
  let source = fs.readFileSync(new URL('../frontend/web/archbro-webmcp.js', import.meta.url), 'utf8')
    .replace(/^import .*?;\r?\n/, '')
    .replace(/\bexport\s+/g, '');
  vm.runInContext(
    `${source}\n`
      + 'globalThis.listAuthorizedProviderConnectionsForTest=listAuthorizedProviderConnections;\n'
      + 'globalThis.listConnectedMcpServersForTest=listConnectedMcpServers;',
    scope,
  );
  return {
    listAuthorized: () => scope.listAuthorizedProviderConnectionsForTest(),
    listServers: () => scope.listConnectedMcpServersForTest(bridge),
    requests,
  };
}

const restored = {
  id: 'mcp-restored-github',
  name: 'GitHub',
  provider: 'github',
  endpoint: 'GitHub remote MCP',
  tool_count: 11,
  last_probe_ok: null,
  last_error: null,
  has_credentials: true,
  auth_type: 'oauth',
  authorization_pending: false,
  persistent: true,
  restored: true,
};

test('durably restored provider remains discoverable before its first post-restart probe', async () => {
  const fixture = loadSurface([restored]);
  const connections = await fixture.listAuthorized();
  assert.equal(connections.length, 1);
  assert.equal(connections[0].id, restored.id);
  assert.deepEqual(fixture.requests, ['/mcp/connections']);
});

test('restored provider with a known failed probe stays hidden', async () => {
  const fixture = loadSurface([{...restored, last_probe_ok: false, last_error: 'provider rejected probe'}]);
  assert.deepEqual(await fixture.listAuthorized(), []);
});

test('ordinary never-probed connection is not promoted merely because credentials exist', async () => {
  const fixture = loadSurface([{...restored, restored: false, persistent: false}]);
  assert.deepEqual(await fixture.listAuthorized(), []);
});

test('restored GitHub provider is merged into the project WebMCP server inventory', async () => {
  const fixture = loadSurface([restored], [{id: 'deployment-mcp', name: 'Deployment MCP'}]);
  const inventory = await fixture.listServers();
  assert.equal(inventory.project_id, 'project-a');
  assert.deepEqual(Array.from(inventory.servers, server => server.id), ['deployment-mcp', restored.id]);
  const github = inventory.servers[1];
  assert.equal(github.provider, 'github');
  assert.equal(github.source_kind, 'authorized_provider');
  assert.equal(github.auth_configured, true);
  assert.equal(github.tool_count, 11);
  assert.deepEqual(fixture.requests, [
    '/projects/project-a/mcp/servers',
    '/mcp/connections',
  ]);
});
