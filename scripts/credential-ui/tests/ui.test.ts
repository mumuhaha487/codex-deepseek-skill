import assert from 'node:assert/strict';
import { test } from 'node:test';
import { runInNewContext } from 'node:vm';
import { readFile } from 'node:fs/promises';

// 运行实际构建产物的 DOM 契约测试，不启动浏览器，不接触真实凭据。
class Element {
  children: Element[] = [];
  value = ''; textContent = ''; hidden = false; disabled = false; type = ''; required = false; placeholder = '';
  className = ''; id = ''; htmlFor = ''; autocomplete = '';
  listeners: Record<string, (event: object) => unknown> = {};
  classes = new Set<string>();
  classList = { toggle: (key: string, enabled: boolean) => enabled ? this.classes.add(key) : this.classes.delete(key) };
  append(...children: Element[]) { this.children.push(...children); }
  replaceChildren() { this.children = []; }
  setAttribute(name: string, value: string) { if (name === 'autocomplete') this.autocomplete = value; }
  addEventListener(event: string, callback: (event: object) => unknown) { this.listeners[event] = callback; }
}
async function harness(configured = false, fail = false, inputTypes: Array<'password' | 'url' | 'text' | 'select' | undefined> = [undefined, undefined],
  page: { title?: string; label?: string; saveLabel?: string } = { title: '连接服务', label: '两个服务', saveLabel: '确认保存' }, fieldTitle?: string) {
  const nodes = Object.fromEntries(['credential-form', 'fields', 'save', 'heading', 'context', 'hint', 'message'].map(key => [key, new Element()]));
  const fields = inputTypes.map((inputType, index) => ({ id: 'sample', label: index ? '<img src=x>' : '语音服务', credential: 'sample/' + index,
    configured: index === 0 && configured, revision: 'revision-' + index, storage: '测试凭据库',
    ui: { placeholder: '测试输入', saveLabel: '更新', ...(inputType ? { inputType } : {}),
      ...(inputType === 'select' ? { options: ['low', 'high'] } : {}), ...(fieldTitle ? { title: fieldTitle } : {}) } }));
  const metadata = { fields, page, outcome: 'waiting' };
  const submitted: any[] = [];
  const lifecycle: Record<string, () => void> = {};
  const context = { document: { getElementById: (id: string) => nodes[id], createElement: () => new Element(), title: '' },
    location: { hash: '', pathname: '/' }, history: { replaceState() {} }, AbortSignal,
    window: { addEventListener: (key: string, callback: () => void) => { lifecycle[key] = callback; } },
    fetch: async (url: string, options: any) => {
      if (url === '/api/meta') return { ok: true, json: async () => structuredClone(metadata) };
      assert.equal(url, '/api/save'); submitted.push(JSON.parse(options.body));
      if (fail) {
        metadata.fields[0].configured = true; metadata.outcome = 'partial';
        return { ok: true, json: async () => ({ status: 'partial', results: [{ credential: 'sample/0', status: 'saved' }, { credential: 'sample/1', status: 'failed' }] }) };
      }
      metadata.outcome = 'saved'; return { ok: true, json: async () => ({ status: 'saved' }) };
    } };
  runInNewContext(await readFile(new URL('../public/app.js', import.meta.url), 'utf8'), context);
  await new Promise(resolve => setImmediate(resolve));
  const inputs = () => nodes.fields.children.map(field => field.children[1]);
  const input = () => nodes['credential-form'].listeners.input({});
  const submit = () => nodes['credential-form'].listeners.submit({ preventDefault() {} });
  return { nodes, inputs, input, submit, submitted, lifecycle };
}
test('真实前端产物：配置生成两个密码框、纯文本标签、必填与成功清空', async () => {
  const h = await harness();
  assert.equal(h.inputs().length, 2); assert.ok(h.inputs().every(input => input.type === 'password' && input.required));
  assert.equal(h.nodes.fields.children[1].children[0].textContent, '<img src=x>');
  assert.equal(h.nodes.heading.textContent, '连接服务');
  assert.equal(h.nodes.context.textContent, '两个服务');
  h.inputs()[0].value = 'FAKE_ONE'; h.input(); assert.equal(h.nodes.save.disabled, true);
  h.inputs()[1].value = 'FAKE_TWO'; h.input(); assert.equal(h.nodes.save.disabled, false);
  await h.submit(); assert.equal(h.submitted[0].entries.length, 2);
  assert.ok(h.inputs().every(input => !input.value)); assert.equal(h.nodes['credential-form'].hidden, true);
});
test('真实前端产物：缺省为密码框，URL 和文本字段明文显示且不使用密码自动完成', async () => {
  const h = await harness(false, false, [undefined, 'url', 'text']);
  assert.deepEqual(h.inputs().map(input => input.type), ['password', 'url', 'text']);
  assert.deepEqual(h.inputs().map(input => input.autocomplete), ['new-password', 'url', 'off']);
  assert.ok(h.inputs().every(input => input.value === ''));
});
test('真实前端产物：下拉字段渲染声明选项且不依赖 HTMLInputElement 全局', async () => {
  const h = await harness(false, false, ['text', 'select', 'select'], {});
  assert.equal(h.nodes.save.textContent, '更新');
  assert.equal(h.inputs()[1].children[0].textContent, '请选择');
  assert.deepEqual(h.inputs()[1].children.slice(1).map(option => option.value), ['low', 'high']);
  h.inputs()[0].value = 'child-model'; h.inputs()[1].value = 'high'; h.inputs()[2].value = 'low'; h.input();
  assert.equal(h.nodes.save.disabled, false);
});
test('真实前端产物：混合配置页使用共同字段标题与配置语义的 context', async () => {
  const h = await harness(false, false, ['url', 'password', 'text'], {}, '配置自定义 Codex 子 Agent');
  assert.equal(h.nodes.heading.textContent, '配置自定义 Codex 子 Agent');
  assert.equal(h.nodes.context.textContent, '3 项配置');
  const fallback = await harness(false, false, ['url', 'password', 'text'], {});
  assert.equal(fallback.nodes.heading.textContent, '输入配置');
  assert.equal(fallback.nodes.context.textContent, '3 项配置');
  const secrets = await harness(false, false, [undefined, 'password'], {});
  assert.equal(secrets.nodes.heading.textContent, '输入密钥');
  assert.equal(secrets.nodes.context.textContent, '2 项凭据');
});
test('真实前端产物：已有项留空保留，替换按钮明确，离开清空', async () => {
  const h = await harness(true);
  assert.equal(h.inputs()[0].required, false); assert.match(h.inputs()[0].placeholder, /留空保留/);
  h.inputs()[0].value = 'FAKE_REPLACE'; h.input(); assert.equal(h.nodes.save.textContent, '替换并保存');
  h.inputs()[0].value = ''; h.inputs()[1].value = 'FAKE_SECOND'; h.input();
  await h.submit(); assert.equal(h.submitted[0].entries.length, 1); assert.equal(h.submitted[0].entries[0].credential, 'sample/1');
  h.inputs()[0].value = 'FAKE_LEAVE'; h.lifecycle.pagehide(); assert.equal(h.inputs()[0].value, '');
});
test('真实前端产物：部分失败不显示全部保存，刷新状态并允许补填', async () => {
  const h = await harness(false, true);
  h.inputs().forEach(input => { input.value = 'FAKE_ONLY'; }); h.input(); await h.submit();
  assert.equal(h.nodes['credential-form'].hidden, false); assert.match(h.nodes.message.textContent, /未确认成功/);
  assert.ok(h.inputs().every(input => !input.value)); assert.equal(h.inputs()[0].required, false);
  assert.equal(h.inputs()[1].required, true); assert.equal(h.nodes.save.disabled, true);
  h.inputs()[1].value = 'FAKE_RETRY'; h.input(); assert.equal(h.nodes.save.disabled, false);
});
