import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFile} from 'node:fs/promises';

const appSource = await readFile(new URL('../frontend/web/app.js', import.meta.url), 'utf8');

function markedBlock(startMarker, endMarker) {
  const start = appSource.indexOf(startMarker);
  const end = appSource.indexOf(endMarker);
  assert.notEqual(start, -1, `missing ${startMarker}`);
  assert.notEqual(end, -1, `missing ${endMarker}`);
  assert.ok(end > start, `${endMarker} must follow ${startMarker}`);
  return appSource.slice(start + startMarker.length, end);
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

function loadBootstrapHelpers(apiImpl) {
  const source = markedBlock(
    '// WEBMCP_BOOTSTRAP_RECONCILIATION_START',
    '// WEBMCP_BOOTSTRAP_RECONCILIATION_END',
  );
  const sandbox = {
    api: apiImpl,
    encodeURIComponent,
    Error,
    Number,
    Array,
    String,
  };
  vm.createContext(sandbox);
  vm.runInContext(`${source}\nglobalThis.__helpers = {bootstrapArchitectureVersion, classifyBootstrapReadback, reconcileBootstrapInitialization, resolveBootstrapInitialization, bootstrapOutcomeUnknownError};`, sandbox);
  return {sandbox, helpers: sandbox.__helpers};
}

function loadMutationHelpers() {
  const source = markedBlock(
    '// WEBMCP_MUTATION_CONTEXT_HELPERS_START',
    '// WEBMCP_MUTATION_CONTEXT_HELPERS_END',
  );
  const calls = [];
  const sandbox = {
    state: {projectId: 'project-a'},
    webMcpRequireProject: () => 'project-a',
    captureNavigationGuard: (projectId) => ({projectId, generation: 7}),
    committedProjectGuardIsCurrent: (guard) => guard?.generation === 7,
    webMcpContext: () => ({project: {id: sandbox.state.projectId}, architecture_version: 3}),
    refresh: async (input) => {
      calls.push(input);
      return true;
    },
    Boolean,
  };
  vm.createContext(sandbox);
  vm.runInContext(`${source}\nglobalThis.__helpers = {captureWebMcpProject, webMcpProjectStillCurrent, webMcpMutationContext, refreshWebMcpProject};`, sandbox);
  return {sandbox, calls, helpers: sandbox.__helpers};
}

const initializedBootstrap = {
  schema: 'archbro.workspace-bootstrap.v2',
  project: {id: 'project-a', architecture_version: 1},
  architecture: {version: 1, summary: 'Accepted'},
  tasks: [{id: 'task-1'}],
};

const emptyBootstrap = {
  schema: 'archbro.workspace-bootstrap.v2',
  project: {id: 'project-a', architecture_version: 0},
  architecture: {version: 0, summary: ''},
  tasks: [],
};

test('bootstrap read-back classifier distinguishes committed, absent, and ambiguous versions', () => {
  const {helpers} = loadBootstrapHelpers(async () => initializedBootstrap);
  assert.deepEqual(plain(helpers.classifyBootstrapReadback(initializedBootstrap)), {
    status: 'INITIALIZED',
    project: initializedBootstrap.project,
    architecture: initializedBootstrap.architecture,
    tasks: initializedBootstrap.tasks,
  });
  assert.equal(helpers.classifyBootstrapReadback(emptyBootstrap).status, 'NOT_INITIALIZED');
  assert.deepEqual(
    plain(helpers.classifyBootstrapReadback({
      ...initializedBootstrap,
      project: {...initializedBootstrap.project, architecture_version: 0},
    })),
    {
      status: 'UNKNOWN',
      reason: 'architecture_version_mismatch',
      project: {id: 'project-a', architecture_version: 0},
      architecture: initializedBootstrap.architecture,
    },
  );
  assert.equal(helpers.classifyBootstrapReadback({}).status, 'UNKNOWN');
});

test('direct bootstrap success does not perform a second read', async () => {
  let calls = 0;
  const {helpers} = loadBootstrapHelpers(async () => {
    calls += 1;
    throw new Error('read-back should not run');
  });
  const result = {architecture: {version: 1}, tasks: []};
  const resolution = await helpers.resolveBootstrapInitialization('project-a', result);
  assert.equal(calls, 0);
  assert.equal(resolution.status, 'INITIALIZED');
  assert.equal(resolution.architecture_version, 1);
  assert.equal(resolution.reconciled, false);
  assert.equal(resolution.result, result);
  assert.equal(helpers.bootstrapArchitectureVersion({architecture: {version: '1'}}), null);
  assert.equal(helpers.bootstrapArchitectureVersion({architecture: {version: true}}), null);
  assert.equal(helpers.bootstrapArchitectureVersion({architecture: {version: 2}}), null);
});

test('malformed success response is recovered only from canonical committed state', async () => {
  const paths = [];
  const {helpers} = loadBootstrapHelpers(async (path) => {
    paths.push(path);
    return initializedBootstrap;
  });
  const resolution = await helpers.resolveBootstrapInitialization('project a/1', {provider: 'webmcp-agent'});
  assert.equal(paths.length, 1);
  assert.equal(paths[0], '/projects/project%20a%2F1/workspace-bootstrap?reading_mode=MAP');
  assert.equal(resolution.status, 'INITIALIZED');
  assert.equal(resolution.architecture_version, 1);
  assert.equal(resolution.reconciled, true);
  assert.equal(resolution.result.provider, 'webmcp-agent');
  assert.deepEqual(plain(resolution.result.architecture), initializedBootstrap.architecture);
  assert.deepEqual(plain(resolution.result.tasks), initializedBootstrap.tasks);
});

test('read-back reports definitely absent initialization without claiming success', async () => {
  const {helpers} = loadBootstrapHelpers(async () => emptyBootstrap);
  const resolution = await helpers.resolveBootstrapInitialization('project-a', null);
  assert.equal(resolution.status, 'NOT_INITIALIZED');
  assert.equal(resolution.result, null);
});

test('read-back maps missing project to NOT_FOUND and transport failure to UNKNOWN', async () => {
  const notFound = loadBootstrapHelpers(async () => {
    throw new Error('404: project not found');
  }).helpers;
  assert.deepEqual(plain(await notFound.reconcileBootstrapInitialization('project-a')), {status: 'NOT_FOUND'});

  const generic404 = loadBootstrapHelpers(async () => {
    throw new Error('404: Not Found');
  }).helpers;
  const generic404Result = await generic404.reconcileBootstrapInitialization('project-a');
  assert.equal(generic404Result.status, 'UNKNOWN');
  assert.equal(generic404Result.reason, 'readback_failed');

  const unavailable = loadBootstrapHelpers(async () => {
    throw new Error('network disconnected');
  }).helpers;
  const result = await unavailable.reconcileBootstrapInitialization('project-a');
  assert.equal(result.status, 'UNKNOWN');
  assert.equal(result.reason, 'readback_failed');
  assert.equal(result.error, 'network disconnected');
});

test('unknown bootstrap outcome explicitly forbids automatic retry', () => {
  const {helpers} = loadBootstrapHelpers(async () => initializedBootstrap);
  const error = helpers.bootstrapOutcomeUnknownError('project-a', 'readback_failed');
  assert.equal(error.code, 'ARCHBRO_BOOTSTRAP_OUTCOME_UNKNOWN');
  assert.equal(error.project_id, 'project-a');
  assert.equal(error.mutation_outcome, 'UNKNOWN');
  assert.equal(error.may_have_written, true);
  assert.match(error.message, /do not retry automatically/i);
  assert.match(error.next_step, /before retrying/i);
});

test('mutation context stays bound to the mutated project after navigation changes', async () => {
  const {sandbox, calls, helpers} = loadMutationHelpers();
  const capture = helpers.captureWebMcpProject();
  assert.deepEqual(plain(capture), {projectId: 'project-a', guard: {projectId: 'project-a', generation: 7}});
  assert.equal(helpers.webMcpProjectStillCurrent(capture), true);
  assert.deepEqual(plain(helpers.webMcpMutationContext(capture)), {
    project: {id: 'project-a'},
    architecture_version: 3,
  });

  sandbox.state.projectId = 'project-b';
  assert.equal(helpers.webMcpProjectStillCurrent(capture), false);
  assert.deepEqual(plain(helpers.webMcpMutationContext(capture)), {
    project_id: 'project-a',
    navigation_superseded: true,
  });
  assert.equal(await helpers.refreshWebMcpProject(capture), false);
  assert.equal(calls.length, 0);

  sandbox.state.projectId = 'project-a';
  assert.equal(await helpers.refreshWebMcpProject(capture), true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].projectId, 'project-a');
});

test('instruction UI context never duplicates the raw project Goal', () => {
  const start = appSource.indexOf('function currentInstructionContext()');
  const end = appSource.indexOf('function updateInstructionContext()', start);
  assert.ok(start >= 0 && end > start);
  assert.doesNotMatch(appSource.slice(start, end), /project_goal/);
});
