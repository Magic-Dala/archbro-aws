import assert from 'node:assert/strict';
import test from 'node:test';

import {isProposalActionable, reconcileWorkspaceSelection} from '../frontend/web/review-helpers.js';

const tasks = [{id:'task-a'}, {id:'task-b'}];
const proposals = [
  {id:'proposal-current', status:'PENDING', base_architecture_version:4},
  {id:'proposal-stale', status:'PENDING', base_architecture_version:3},
  {id:'proposal-resolved', status:'REJECTED', base_architecture_version:4},
];

test('proposal actionability requires pending status and the current accepted version', () => {
  assert.equal(isProposalActionable(proposals[0], 4), true);
  assert.equal(isProposalActionable(proposals[0], {version:4}), true);
  assert.equal(isProposalActionable(proposals[1], 4), false);
  assert.equal(isProposalActionable(proposals[2], 4), false);
  assert.equal(isProposalActionable({id:'missing-base', status:'PENDING'}, null), false);
  assert.equal(isProposalActionable({id:'missing-base', status:'PENDING'}, 0), false);
  assert.equal(isProposalActionable({id:'zero-base', status:'PENDING', base_architecture_version:0}, null), false);
});

test('review transition clears every task-owned state and chooses canonical actionable state', () => {
  assert.deepEqual(reconcileWorkspaceSelection({
    tabName:'review', tasks, proposals, architectureVersion:4,
    selectedTaskId:'task-a', taskDetailId:'task-a',
    taskDetailOrigin:{view:'tasks', taskId:'task-a'}, selectedProposalId:'proposal-stale',
  }), {
    selectedTaskId:null, taskDetailId:null, taskDetailOrigin:null,
    selectedProposalId:'proposal-current',
  });
});

test('review focus accepts only canonical actionable state and never reads rendered DOM', () => {
  assert.equal(reconcileWorkspaceSelection({
    tabName:'review', tasks, proposals, architectureVersion:4, focusedProposalId:'proposal-current',
  }).selectedProposalId, 'proposal-current');
  assert.equal(reconcileWorkspaceSelection({
    tabName:'review', tasks, proposals, architectureVersion:4, focusedProposalId:'proposal-stale',
  }).selectedProposalId, 'proposal-current');
  assert.doesNotMatch(reconcileWorkspaceSelection.toString(), /querySelector|aria-pressed|\.click\(/);
});

test('tasks transition owns task state, clears proposals, and reconciles vanished tasks', () => {
  assert.deepEqual(reconcileWorkspaceSelection({
    tabName:'tasks', tasks, proposals, architectureVersion:4,
    selectedTaskId:'task-a', taskDetailId:'task-a',
    taskDetailOrigin:{view:'tasks', taskId:'task-a'}, selectedProposalId:'proposal-current',
  }), {
    selectedTaskId:'task-a', taskDetailId:'task-a',
    taskDetailOrigin:{view:'tasks', taskId:'task-a'}, selectedProposalId:null,
  });
  assert.deepEqual(reconcileWorkspaceSelection({
    tabName:'tasks', tasks:[], proposals, architectureVersion:4,
    selectedTaskId:'task-a', taskDetailId:'task-a',
    taskDetailOrigin:{view:'tasks', taskId:'task-a'}, selectedProposalId:'proposal-current',
  }), {selectedTaskId:null, taskDetailId:null, taskDetailOrigin:null, selectedProposalId:null});
});
