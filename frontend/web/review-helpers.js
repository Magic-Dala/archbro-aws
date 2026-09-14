const TASK_ENUM_LABELS = {
  HUMAN: 'Human',
  AGENT: 'Agent',
  ARCHITECTURE: 'Architecture',
  UNASSIGNED: 'Unassigned',
  TODO: 'To do',
  IN_PROGRESS: 'In progress',
  BLOCKED: 'Blocked',
  DONE: 'Done',
  PENDING: 'Needs review',
  ACCEPTED: 'Accepted',
  REJECTED: 'Kept current',
  SUPERSEDED: 'Superseded',
};

export function formatTaskEnum(value, fallback = 'Not provided') {
  const token = String(value ?? '').trim().toUpperCase();
  if (!token) return fallback;
  if (TASK_ENUM_LABELS[token]) return TASK_ENUM_LABELS[token];
  return token.toLowerCase().replace(/(^|_)([a-z])/g, (_match, prefix, letter) => `${prefix ? ' ' : ''}${letter.toUpperCase()}`);
}

export function formatOverviewAttentionLabel(count) {
  const numeric = Number(count);
  const normalized = Number.isFinite(numeric) ? Math.max(0, Math.trunc(numeric)) : 0;
  const item = normalized === 1 ? 'item' : 'items';
  const verb = normalized === 1 ? 'needs' : 'need';
  return `${normalized} ${item} ${verb} you ↗`;
}

export function resolveTaskDependencies(task = {}, tasks = []) {
  const byId = new Map((Array.isArray(tasks) ? tasks : []).map((item) => [String(item?.id || ''), item]));
  return (Array.isArray(task.dependencies) ? task.dependencies : []).map((rawId) => {
    const id = String(rawId ?? '').trim();
    const dependency = byId.get(id);
    return dependency
      ? {id, available: true, title: String(dependency.title || ''), status: String(dependency.status || '')}
      : {id, available: false, title: '', status: ''};
  });
}

export function taskInstructionContext({view = 'tasks', projectId = null, projectName = '', task = null} = {}) {
  const base = {view, project_id: projectId, project_name: projectName};
  if (!task) {
    return {
      label: 'Tasks · project execution',
      instruction: 'Ask about project tasks or execution state',
      placeholder: 'Select a task for focused context, or describe an execution update.',
      payload: base,
    };
  }
  return {
    label: `Task · ${task.title || task.id || 'Untitled task'}`,
    instruction: 'Ask about this task or describe what changed',
    placeholder: 'Example: This task is blocked because...',
    payload: {
      ...base,
      task_id: task.id,
      task_title: task.title || '',
      task_status: task.status || '',
      related_component: task.related_component || null,
    },
  };
}

export function bindProposalDecisionControls(buttons, getSelectedProposalId, decideProposal) {
  for (const button of Array.from(buttons || [])) {
    button.onclick = () => {
      const proposalId = getSelectedProposalId?.();
      const decision = button.dataset?.proposalDecision
        || button.getAttribute?.('data-proposal-decision')
        || button.proposalDecision;
      if (!proposalId || !decision) return undefined;
      return decideProposal(proposalId, decision);
    };
  }
  return buttons;
}

export function isProposalActionable(proposal, architectureOrVersion) {
  const version = typeof architectureOrVersion === 'object'
    ? architectureOrVersion?.version
    : architectureOrVersion;
  const baseVersion = proposal?.base_architecture_version;
  const validVersion = version !== null && version !== undefined && version !== ''
    && Number.isInteger(Number(version));
  const validBaseVersion = baseVersion !== null && baseVersion !== undefined && baseVersion !== ''
    && Number.isInteger(Number(baseVersion));
  return Boolean(
    proposal
      && proposal.status === 'PENDING'
      && validVersion
      && validBaseVersion
      && Number(baseVersion) === Number(version),
  );
}

export function reconcileWorkspaceSelection({
  tabName,
  tasks = [],
  proposals = [],
  architectureVersion = null,
  selectedTaskId = null,
  taskDetailId = null,
  taskDetailOrigin = null,
  selectedProposalId = null,
  focusedProposalId = null,
} = {}) {
  const taskIds = new Set(tasks.map((task) => task?.id).filter(Boolean));
  const actionable = proposals.filter((proposal) => isProposalActionable(proposal, architectureVersion));
  const actionableIds = new Set(actionable.map((proposal) => proposal.id));
  if (tabName === 'review') {
    const requested = focusedProposalId || selectedProposalId;
    return {
      selectedTaskId: null,
      taskDetailId: null,
      taskDetailOrigin: null,
      selectedProposalId: actionableIds.has(requested) ? requested : (actionable[0]?.id || null),
    };
  }
  const detailId = taskIds.has(taskDetailId) ? taskDetailId : null;
  const taskId = taskIds.has(selectedTaskId) ? selectedTaskId : detailId;
  return {
    selectedTaskId: taskId,
    taskDetailId: detailId,
    taskDetailOrigin: detailId ? taskDetailOrigin : null,
    selectedProposalId: focusedProposalId && actionableIds.has(focusedProposalId)
      ? focusedProposalId
      : null,
  };
}
