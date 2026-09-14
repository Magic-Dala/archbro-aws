import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

import {formatOverviewAttentionLabel} from '../frontend/web/review-helpers.js';

const webRoot = new URL('../frontend/web/', import.meta.url);
const [appSource, htmlSource, helperSource] = await Promise.all([
  readFile(new URL('app.js', webRoot), 'utf8'),
  readFile(new URL('index.html', webRoot), 'utf8'),
  readFile(new URL('review-helpers.js', webRoot), 'utf8'),
]);
const overviewStart = htmlSource.indexOf('id="view-overview"');
const overviewEnd = htmlSource.indexOf('id="workspaceTabTasksPanel"', overviewStart);
const overviewHtml = htmlSource.slice(overviewStart, overviewEnd);

function extractFunction(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `${name} must exist`);
  const brace = source.indexOf('{', start);
  let depth = 0;
  for (let index = brace; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1;
    if (source[index] === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  throw new Error(`Could not extract ${name}`);
}

class FakeElement {
  constructor(dataset = {}) {
    this.dataset = dataset;
    this.onclick = null;
    this.onkeydown = null;
  }

  click() {
    return this.onclick?.({target: {closest: () => null}});
  }
}

function loadNavigation(elements, switchView) {
  const document = {
    querySelectorAll(selector) {
      if (selector === '[data-go]') return elements.filter((element) => element.dataset.go);
      if (selector === '[data-go-card]') return elements.filter((element) => element.dataset.goCard);
      return [];
    },
  };
  const context = {document, switchView, workspaceTabNames: ['tasks', 'review']};
  vm.runInNewContext([
    extractFunction(appSource, 'goTargetOptions'),
    extractFunction(appSource, 'wireGoButtons'),
    'this.wireGoButtons = wireGoButtons;',
  ].join('\n'), context);
  return context.wireGoButtons;
}

test('Overview preserves the approved sequential information architecture', () => {
  const order = ['overview-status', 'overview-goal', 'overview-review', 'overview-architecture', 'overview-next-tasks', 'overview-activity'];
  let previous = -1;
  for (const className of order) {
    const index = overviewHtml.indexOf(className);
    assert.ok(index > previous, `${className} must follow the approved Overview order`);
    previous = index;
  }
  const architectureStart = overviewHtml.indexOf('overview-section overview-architecture');
  const architectureEnd = overviewHtml.indexOf('overview-section overview-next-tasks', architectureStart);
  const architectureSection = overviewHtml.slice(architectureStart, architectureEnd);
  assert.ok(!architectureSection.includes('data-go-card='), 'Architecture section must not be a nested persistent card link');
  assert.ok(architectureSection.includes('data-go="architecture">Open Living Architecture ↗</button>'));
});

test('task shortcuts own the Tasks tab and repeated wiring stays idempotent', () => {
  for (const id of ['readySummary', 'runningSummary']) {
    const start = overviewHtml.indexOf(`id="${id}"`);
    const end = overviewHtml.indexOf('>', start);
    const tag = overviewHtml.slice(start, end);
    assert.ok(tag.includes('data-go="tasks"'));
    assert.ok(tag.includes('data-workspace-tab-target="tasks"'));
  }
  const viewAll = overviewHtml.indexOf('>View all →</button>');
  const viewAllStart = overviewHtml.lastIndexOf('<button', viewAll);
  const viewAllTag = overviewHtml.slice(viewAllStart, viewAll);
  assert.ok(viewAllTag.includes('data-go="tasks"'));
  assert.ok(viewAllTag.includes('data-workspace-tab-target="tasks"'));

  const ready = new FakeElement({go: 'tasks', workspaceTabTarget: 'tasks'});
  const running = new FakeElement({go: 'tasks', workspaceTabTarget: 'tasks'});
  const all = new FakeElement({go: 'tasks', workspaceTabTarget: 'tasks'});
  const architecture = new FakeElement({go: 'architecture'});
  const genericCard = new FakeElement({goCard: 'architecture'});
  const elements = [ready, running, all, architecture, genericCard];
  const navigations = [];
  const wire = loadNavigation(elements, (view, options) => navigations.push({view, options}));

  wire();
  wire();
  ready.click();
  running.click();
  all.click();
  architecture.click();
  genericCard.click();

  assert.equal(navigations.length, 5, 'repeated rendering must not accumulate navigation handlers');
  for (const navigation of navigations.slice(0, 3)) {
    assert.equal(navigation.view, 'tasks');
    assert.equal(navigation.options.workspaceTab, 'tasks');
  }
  assert.equal(navigations[3].view, 'architecture');
  assert.equal(navigations[4].view, 'architecture');
});

test('mixed attention labels are honest and require no DOM repair observer', () => {
  assert.equal(formatOverviewAttentionLabel(0), '0 items need you ↗');
  assert.equal(formatOverviewAttentionLabel(1), '1 item needs you ↗');
  assert.equal(formatOverviewAttentionLabel(2), '2 items need you ↗');
  assert.equal(formatOverviewAttentionLabel('invalid'), '0 items need you ↗');
  assert.ok(!helperSource.includes('MutationObserver'));
  assert.ok(!helperSource.includes('prepareOverviewControls'));
  assert.ok(!helperSource.includes('observeOverviewAttentionLabel'));
});
