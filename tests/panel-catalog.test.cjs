const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/feishu-groups.js'), 'utf8');
const start = source.indexOf('let catalogPromise=');
const end = source.indexOf('async function operation(', start);
assert.ok(start >= 0 && end > start, 'catalog refresh implementation must exist');

function harness(api) {
  const elements = new Map();
  const node = id => {
    if (!elements.has(id)) elements.set(id, {
      textContent: '', className: '',
      classList: { add() {}, remove() {} },
    });
    return elements.get(id);
  };
  const reports = [], events = [];
  const context = vm.createContext({
    api, vm: { catalog: null }, node,
    area: { classList: { remove() {} } },
    lists() {}, pairView() {},
    report(id, message = '', error = false) {
      const element = node(id);
      element.textContent = message;
      element.className = message ? (error ? 'error' : 'success') : 'hidden';
      reports.push({ id, message, error });
    },
    window: { dispatchEvent(event) { events.push(event); } },
    CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options.detail; } },
  });
  vm.runInContext(source.slice(start, end), context);
  return { context, node, reports, events, refresh: () => context.refreshCatalog() };
}

test('clears stale connection warning after successful automatic refresh', async () => {
  let attempts = 0;
  const catalog = { apps: [], groups: [] };
  const h = harness(async () => {
    if (++attempts === 1) throw new Error('无法连接本机 Bridge 服务');
    return catalog;
  });
  await h.refresh();
  assert.equal(h.node('groupsNotice').textContent, '无法连接本机 Bridge 服务');
  assert.equal(h.events[0].detail.error, '无法连接本机 Bridge 服务');
  await h.refresh();
  assert.equal(h.context.vm.catalog, catalog);
  assert.equal(h.node('groupsNotice').textContent, '');
  assert.equal(h.node('groupsNotice').className, 'hidden');
});

test('does not clear feedback from another operation on recovery', async () => {
  let attempts = 0;
  const h = harness(async () => {
    if (++attempts === 1) throw new Error('connection failed');
    return { apps: [], groups: [] };
  });
  await h.refresh();
  h.node('groupsNotice').textContent = '机器人设置已保存。';
  await h.refresh();
  assert.equal(h.node('groupsNotice').textContent, '机器人设置已保存。');
});

test('coalesces overlapping catalog refreshes into one request', async () => {
  let attempts = 0, finish;
  const h = harness(() => {
    attempts++;
    return new Promise(resolve => { finish = resolve; });
  });
  const first = h.refresh(), second = h.refresh();
  assert.equal(first, second);
  assert.equal(attempts, 1);
  finish({ apps: [], groups: [] });
  await first;
  const third = h.refresh();
  assert.equal(attempts, 2);
  finish({ apps: [], groups: [] });
  await third;
});

test('retains previous data and visible warning while connection still fails', async () => {
  const h = harness(async () => { throw new Error('still offline'); });
  const old = { apps: [{ name: 'existing bot' }], groups: [] };
  h.context.vm.catalog = old;
  await h.refresh();
  await h.refresh();
  assert.equal(h.context.vm.catalog, old);
  assert.equal(h.node('groupsNotice').textContent, 'still offline');
  assert.equal(h.node('groupsNotice').className, 'error');
});

test('healthy polling leaves existing operation feedback untouched', async () => {
  const h = harness(async () => ({ apps: [], groups: [] }));
  h.node('groupsNotice').textContent = '操作失败，请检查设置';
  await h.refresh();
  await h.refresh();
  assert.equal(h.node('groupsNotice').textContent, '操作失败，请检查设置');
});
