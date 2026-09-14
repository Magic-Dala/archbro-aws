import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';

import {
  bindProposalDecisionControls,
  formatTaskEnum,
  resolveTaskDependencies,
  taskInstructionContext,
} from '../frontend/web/review-helpers.js';

const webRoot = new URL('../frontend/web/', import.meta.url);

test('task enum labels preserve the Human/Agent/Architecture distinctions', () => {
  assert.equal(formatTaskEnum('HUMAN'), 'Human');
  assert.equal(formatTaskEnum('AGENT'), 'Agent');
  assert.equal(formatTaskEnum('ARCHITECTURE'), 'Architecture');
  assert.equal(formatTaskEnum('UNASSIGNED'), 'Unassigned');
  assert.equal(formatTaskEnum('', 'Not provided'), 'Not provided');
});

test('status legend uses the same human-readable labels as task rows', async () => {
  const html = await readFile(new URL('index.html', webRoot), 'utf8');
  const legend = html.match(/<div class="legend">([\s\S]*?)<\/div>/)?.[1] || '';
  const legendLabels = [...legend.matchAll(/<span><i[^>]*><\/i>([^<]+)<\/span>/g)]
    .map((match) => match[1].trim());

  assert.deepEqual(
    ['TODO', 'IN_PROGRESS', 'BLOCKED', 'DONE'].map((status) => formatTaskEnum(status)),
    ['To do', 'In progress', 'Blocked', 'Done'],
  );
  assert.deepEqual(legendLabels, ['To do', 'In progress', 'Blocked', 'Done']);
});

test('dependency resolution returns task title/status and honest missing references', () => {
  const task = {dependencies: ['task-api', 'task-missing']};
  const tasks = [{id: 'task-api', title: 'Build API', status: 'DONE'}];

  assert.deepEqual(resolveTaskDependencies(task, tasks), [
    {id: 'task-api', available: true, title: 'Build API', status: 'DONE'},
    {id: 'task-missing', available: false, title: '', status: ''},
  ]);
});

test('task composer context includes the complete focused task payload and switches tasks', () => {
  const taskA = {id: 'task-a', title: 'Plan API', status: 'TODO', related_component: 'api'};
  const taskB = {id: 'task-b', title: 'Ship UI', status: 'IN_PROGRESS', related_component: 'web'};
  const contextA = taskInstructionContext({view: 'tasks', projectId: 'project-1', projectName: 'Archbro', task: taskA});
  const contextB = taskInstructionContext({view: 'tasks', projectId: 'project-1', projectName: 'Archbro', task: taskB});
  assert.equal(contextA.label, 'Task · Plan API');
  assert.deepEqual(contextA.payload, {
    view: 'tasks', project_id: 'project-1', project_name: 'Archbro',
    task_id: 'task-a', task_title: 'Plan API', task_status: 'TODO', related_component: 'api',
  });
  assert.equal(contextB.payload.task_id, 'task-b');
  assert.equal(contextB.payload.task_title, 'Ship UI');
});

test('persistent proposal decision controls read the selected proposal at click time exactly once', async () => {
  const requests = [];
  let selectedProposalId = 'proposal-a';
  class FakeButton {
    constructor(decision) { this.dataset = {proposalDecision: decision}; this.onclick = null; this.listeners = []; }
    addEventListener(_type, listener) { this.listeners.push(listener); }
    async click() { await this.onclick?.(); for (const listener of this.listeners) await listener(); }
  }
  const controls = [new FakeButton('reject'), new FakeButton('accept')];
  const decide = async (proposalId, decision) => requests.push({proposalId, decision});
  bindProposalDecisionControls(controls, () => selectedProposalId, decide);
  selectedProposalId = 'proposal-b';
  bindProposalDecisionControls(controls, () => selectedProposalId, decide);
  await controls[1].click();
  assert.deepEqual(requests, [{proposalId: 'proposal-b', decision: 'accept'}]);
});

test('task and proposal surfaces expose direct accessible review controls', async () => {
  const [html, js, css] = await Promise.all([
    readFile(new URL('index.html', webRoot), 'utf8'),
    readFile(new URL('app.js', webRoot), 'utf8'),
    readFile(new URL('styles.css', webRoot), 'utf8'),
  ]);

  for (const id of ['taskDetailPanel', 'taskDetailBody', 'taskDetailClose', 'taskDetailAction', 'proposalDecisionBar']) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
  for (const id of ['workspaceTabs', 'workspaceTabTasks', 'workspaceTabReview', 'workspaceTabTasksPanel', 'workspaceTabReviewPanel', 'taskTabCount', 'reviewTabCount']) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
  assert.match(html, /role="tablist"/);
  assert.match(html, /role="tab"/);
  assert.match(html, /role="tabpanel"/);
  assert.match(html, /aria-selected/);
  assert.doesNotMatch(html, /taskDetailAskAgent/);
  assert.doesNotMatch(js, /focusAgentForTask|taskDetailAskAgent/);
  assert.doesNotMatch(js, /task-context-button|data-task-select|Set Agent context/);
  assert.doesNotMatch(js, /showDialog\('proposalReviewDialog'/);
  assert.match(js, /workspaceTab/);
  assert.match(js, /workspaceTabMemory/);
  assert.match(js, /workspaceTabScrollMemory/);
  assert.match(js, /isProposalActionable/);
  assert.match(js, /ArrowRight|ArrowLeft/);
  assert.match(js, /switchWorkspaceTab\(['"]review['"],/);
  assert.match(js, /data-task-open/);
  assert.match(js, /function openTaskDetails[\s\S]{0,900}state\.selectedTaskId = task\.id/);
  assert.match(js, /async focusItem\(\{kind, id = null\} = \{\}\)/);
  assert.match(js, /openTaskDetails\(task\.id, 'tasks'\)/);
  assert.match(js, /task\.status === 'DONE'[\s\S]*action: 'reopen'/);
  assert.match(js, /Open component/);
  assert.match(html, /ASK AGENT/);
  assert.match(js, /proposal-card-select/);
  assert.doesNotMatch(js, /data-proposal-ask|proposal-context-button|Use this proposal as Agent context/);
  assert.match(js, /acceptance-preview/);
  assert.doesNotMatch(js, /deriveProposalChanges/);
  assert.match(js, /evidence_event_ids/);
  assert.match(js, /Server acceptance plan/);
  assert.match(js, /Architecture after acceptance/);
  assert.doesNotMatch(js, /data-task-navigate|dblclick[^\n]*(task|task-row)/i);
  assert.match(css, /\.task-detail-panel/);
  assert.match(css, /\.task-row \.status-pill\{[^}]*justify-self:start/);
  assert.match(css, /\.proposal-decision-bar/);
  assert.match(css, /safe-area-inset-bottom/);
});
