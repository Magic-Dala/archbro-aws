// A repository selection is a project setting, never an OAuth credential.
export function createProjectRepositoryController({api, getProject, showDialog, onSaved, onConnect, document: doc = document}) {
  const el = (name) => doc.getElementById(`projectRepository${name}`);
  const dialog = el('Dialog');
  let generation = 0;
  let searchGeneration = 0;
  let projectId = null;
  let revision = 0;
  let manage = false;
  let busy = false;
  let page = 1;
  let trigger = null;
  let loaded = false;
  let connected = false;
  let bound = false;
  const base = () => `/projects/${encodeURIComponent(projectId)}`;
  const capture = () => ({generation, projectId});
  const current = (token) => dialog.open && token.generation === generation
    && token.projectId === projectId && getProject()?.id === projectId;
  const error = (message = '') => { el('Error').textContent = message; };
  const controls = () => {
    for (const name of ['Name', 'Branch', 'Search', 'Save']) el(name).disabled = busy || !loaded || !manage;
    el('Remove').disabled = busy || !loaded || !manage || !bound;
    el('Connect').disabled = busy || !loaded;
    el('Save').textContent = busy ? 'Verifying…' : 'Verify and save';
    el('Next').disabled = busy || !el('Next').dataset.more;
    el('Previous').disabled = busy || page <= 1;
  };
  function invalidate() {
    generation += 1;
    searchGeneration += 1;
    projectId = null;
    loaded = false;
    busy = false;
  }
  function close() {
    invalidate();
    if (dialog.open) dialog.close();
  }
  function paint(value) {
    revision = value.repository_revision;
    manage = value.can_manage === true;
    connected = value.connected === true;
    bound = Boolean(value.source_repository);
    const repo = value.source_repository;
    el('Name').value = repo?.full_name || '';
    el('Branch').value = repo?.branch || '';
    el('Current').textContent = repo
      ? `Selected: ${repo.full_name}${repo.branch ? ` · ${repo.branch}` : ' · repository default branch'}`
      : 'No repository selected. GitHub remains optional.';
    el('Status').textContent = !manage
      ? 'Only the project owner can change this selection. GitHub reads use your own account.'
      : connected ? 'Uses your existing personal GitHub connection. Credentials are not stored in this project.'
        : 'Connect your GitHub account to search or verify a repository. The saved selection is preserved.';
    loaded = true;
    controls();
  }
  async function open(project, opener) {
    if (!project?.id) return;
    close();
    projectId = project.id;
    trigger = opener;
    revision = project.repository_revision || 0;
    manage = false;
    bound = false;
    el('Title').textContent = `GitHub repository · ${project.name}`;
    el('Current').textContent = 'Loading repository selection…';
    el('Status').textContent = '';
    el('Name').value = '';
    el('Branch').value = '';
    el('Results').replaceChildren();
    el('Pagination').hidden = true;
    error();
    controls();
    showDialog('projectRepositoryDialog', opener);
    const token = capture();
    try {
      const value = await api(`${base()}/repository`);
      if (!current(token)) return;
      paint(value);
      el('Name').focus();
    } catch (err) {
      if (!current(token)) return;
      el('Current').textContent = 'Repository selection could not be loaded.';
      error(err.message || 'Reload this dialog and try again.');
    }
  }
  async function search(nextPage = 1) {
    if (!manage || busy || !loaded) return;
    const query = el('Name').value.trim();
    if (query.length < 2) { error('Type at least two characters to search, or enter owner/repo.'); return; }
    const token = capture();
    const requestId = ++searchGeneration;
    page = nextPage;
    error();
    el('Results').textContent = 'Searching your accessible repositories…';
    el('Pagination').hidden = true;
    try {
      const result = await api(`${base()}/repository-options?q=${encodeURIComponent(query)}&page=${page}`);
      if (!current(token) || requestId !== searchGeneration) return;
      el('Results').replaceChildren();
      const items = Array.isArray(result.items) ? result.items : [];
      if (!items.length) el('Results').textContent = 'No results. You may enter the exact owner/repo and verify it directly.';
      for (const item of items) {
        const button = doc.createElement('button');
        button.type = 'button';
        button.className = 'repository-option';
        button.textContent = `${item.full_name}${item.private ? ' · Private' : ''}`;
        button.addEventListener('click', () => {
          if (!current(token) || busy) return;
          el('Name').value = item.full_name;
          el('Branch').value = '';
          searchGeneration += 1;
          el('Results').replaceChildren();
          el('Pagination').hidden = true;
          el('Branch').focus();
        });
        el('Results').append(button);
      }
      el('Next').dataset.more = result.has_more ? 'yes' : '';
      el('Pagination').hidden = page <= 1 && !result.has_more;
      controls();
    } catch (err) {
      if (!current(token) || requestId !== searchGeneration) return;
      el('Results').replaceChildren();
      error(err.message || 'Repository search failed.');
    }
  }
  async function save(remove = false) {
    if (!manage || busy || !loaded) return;
    const name = el('Name').value.trim();
    if (!remove && !name) { error('Enter owner/repo or a GitHub repository URL.'); return; }
    const token = capture();
    const path = `${base()}/repository`;
    searchGeneration += 1;
    busy = true;
    error();
    controls();
    try {
      const value = await api(remove ? `${path}?expected_revision=${revision}` : path, remove
        ? {method: 'DELETE'}
        : {method: 'PUT', body: JSON.stringify({full_name: name, branch: el('Branch').value.trim() || null, expected_revision: revision})});
      if (!current(token)) return;
      await onSaved(token.projectId, value);
      if (current(token)) close();
    } catch (err) {
      if (current(token)) error(err.message || 'Repository change was not saved.');
    } finally {
      if (current(token)) { busy = false; controls(); }
    }
  }
  el('Form').addEventListener('submit', (event) => { event.preventDefault(); void save(); });
  el('Search').addEventListener('click', () => { void search(1); });
  el('Previous').addEventListener('click', () => { void search(page - 1); });
  el('Next').addEventListener('click', () => { void search(page + 1); });
  el('Remove').addEventListener('click', () => { void save(true); });
  el('Name').addEventListener('input', () => {
    searchGeneration += 1;
    el('Results').replaceChildren();
    el('Pagination').hidden = true;
  });
  el('Connect').addEventListener('click', () => {
    const id = projectId;
    close();
    onConnect(id, trigger);
  });
  // Native close/cancel/backdrop all retire the same generation.
  dialog.addEventListener('close', () => { if (!dialog.open) invalidate(); });
  dialog.addEventListener('cancel', invalidate);
  return {open, close, search, save};
}
