// The actual dashboard script with a bounded DOM/Core double. This proves UI
// contracts only; real Editor, Game and rendered-host evidence are separate.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../Resources/live_demo.html'), 'utf8');
const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(match => match[1]).join('\n');
const status = () => ({engine_version: '5.8.1', pid: 4180, context: 'game', python_version: '3.12.15', scene: {height: 0, revision: 0}});
const plain = value => JSON.parse(JSON.stringify(value));
const flush = async () => { for (let i = 0; i < 4; i++) await new Promise(resolve => setImmediate(resolve)); };

function setup(options = {}) {
  const elements = new Map(), windowEvents = {}, documentEvents = {}, subscriptions = {}, calls = [], sends = [], timers = new Map(), animationRequests = new Map(), paints = [];
  let animationId = 0, now = 0;
  let timerId = 0, response = options.response || ((method, params) => {
    if (method === 'demo.status') return status();
    if (method === 'demo.python.multiply') return {value: params.left * params.right, python_version: '3.12.15'};
    if (method === 'demo.scene.set_height') return {height: params.height, revision: 1};
    if (method === 'demo.scene.reset') return {height: 0, revision: 2};
    throw Error('Unexpected method: ' + method);
  });
  class Element {
    constructor(tag = 'div') { this.tagName = tag.toUpperCase(); this.children = []; this.attributes = {}; this.textContent = ''; this.value = ''; this.disabled = false; this.hidden = false; }
    get firstChild() { return this.children[0] || null; }
    get lastChild() { return this.children[this.children.length - 1] || null; }
    appendChild(child) { this.children.push(child); return child; }
    insertBefore(child, before) { const index = this.children.indexOf(before); this.children.splice(index < 0 ? this.children.length : index, 0, child); return child; }
    removeChild(child) { this.children.splice(this.children.indexOf(child), 1); }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return this.attributes[name]; }
    getContext(kind) { assert.equal(kind, '2d'); return options.noCanvas ? null : {fillStyle: '', fillRect(...rect) { paints.push({color: this.fillStyle, rect}); }}; }
    set innerHTML(value) { throw Error('Unsafe HTML write: ' + value); }
  }
  // Parse actual declared IDs and values, so missing or renamed controls fail.
  for (const match of html.matchAll(/<([a-z]+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
    const element = new Element(match[1]), attrs = match[2];
    const value = attrs.match(/\bvalue="([^"]*)"/); if (value) element.value = value[1];
    element.disabled = /\bdisabled\b/.test(attrs); element.hidden = /\bhidden\b/.test(attrs);
    elements.set(match[3], element);
  }
  const document = {
    hidden: false,
    addEventListener(name, handler) { (documentEvents[name] || (documentEvents[name] = [])).push(handler); },
    getElementById(id) { assert.ok(elements.has(id), 'The actual HTML declares #' + id); return elements.get(id); },
    createElement: tag => new Element(tag)
  };
  const core = {
    whenReady: options.whenReady || (() => Promise.resolve(core)),
    on(name, handler) { subscriptions[name] = handler; return () => { delete subscriptions[name]; }; },
    call(method, params) { calls.push({route: 'call', method, params: plain(params)}); return response(method, params); },
    invoke(method, params) { calls.push({route: 'invoke', method, params: plain(params)}); return response(method, params); },
    send_event(name, detail) { sends.push({name, detail: plain(detail)}); }
  };
  const win = {addEventListener(name, handler) { (windowEvents[name] || (windowEvents[name] = [])).push(handler); }, performance: {now: () => now}};
  if (!options.noRaf) {
    win.requestAnimationFrame = callback => { const id = ++animationId; animationRequests.set(id, callback); return id; };
    win.cancelAnimationFrame = id => animationRequests.delete(id);
  }
  if (options.stub) win.auroraview = {_isStub: true, whenReady() { calls.push({route: 'stub-ready'}); return new Promise(() => {}); }};
  else if (!options.noBridge) win.auroraview = core;
  const context = vm.createContext({window: win, document, console, setTimeout(callback) { const id = ++timerId; timers.set(id, callback); return id; }, clearTimeout(id) { timers.delete(id); }});
  new vm.Script(script, {filename: 'actual-live_demo.html'}).runInContext(context);
  return {
    elements, calls, sends, subscriptions, timers, core, animationRequests, paints,
    advance(milliseconds) { now += milliseconds; },
    frame(timestamp) { assert.equal(animationRequests.size, 1); const [id, callback] = [...animationRequests][0]; animationRequests.delete(id); callback(timestamp); },
    visibility(hidden) { document.hidden = hidden; for (const handler of documentEvents.visibilitychange || []) handler(); },
    replaceBridge() { win.auroraview = core; },
    setResponse(value) { response = value; },
    async install() { win.auroraview = core; for (const handler of windowEvents.auroraviewready || []) handler(); await flush(); },
    async click(id) { assert.equal(elements.get(id).disabled, false, id + ' is enabled'); elements.get(id).onclick(); await flush(); },
    event(name, detail) { subscriptions[name](detail); },
    unload() { for (const handler of windowEvents.beforeunload || []) handler(); },
    get(id) { return elements.get(id); }
  };
}

test('actual offline page waits for a late Core and reports real status only after whenReady', async () => {
  const h = setup({noBridge: true}); await flush();
  assert.equal(h.calls.length, 0); assert.equal(h.get('lift-cube').disabled, true); assert.equal(h.sends.length, 0);
  await h.install();
  assert.equal(h.calls[0].method, 'demo.status');
  assert.equal(h.get('engine-version').textContent, '5.8.1'); assert.equal(h.get('host-context').textContent, 'game');
  assert.equal(h.get('host-pid').textContent, '4180'); assert.equal(h.get('height-current').textContent, '0');
  assert.equal(h.get('lift-cube').disabled, false);
  assert.deepEqual(h.sends.find(item => item.name === 'demo:browser.ready').detail, status());
});

test('real Core cannot enable tools before its readiness Promise resolves', async () => {
  let resolveReady;
  const h = setup({whenReady: () => new Promise(resolve => { resolveReady = resolve; })}); await flush();
  assert.equal(h.calls.length, 0); assert.equal(h.get('multiply-call').disabled, true);
  resolveReady(h.core); await flush();
  assert.equal(h.calls.length, 1); assert.equal(h.get('python-badge').textContent, 'PYTHON · READY');
});

test('discarded startup-stub Promise is never awaited; replacement Core starts without a ready event', async () => {
  const h = setup({stub: true}); await flush();
  assert.equal(h.calls.length, 0, 'the unresolved startup-stub whenReady must not be called');
  assert.equal(h.get('lift-cube').disabled, true); assert.equal(h.timers.size, 1);
  // install() also emits the normal ready event; this test instead drives the
  // retry callback with Core installed to cover replacement without an event.
  const retry = [...h.timers.values()][0];
  h.replaceBridge(); retry(); await flush();
  assert.equal(h.calls[0].method, 'demo.status'); assert.equal(h.get('lift-cube').disabled, false);
  assert.equal(h.timers.size, 0); assert.equal(h.sends[0].name, 'demo:browser.ready');
});

test('closing the page while Core is still a stub cancels the readiness retry', async () => {
  const h = setup({stub: true}); await flush(); assert.equal(h.timers.size, 1);
  h.unload(); assert.equal(h.timers.size, 0); assert.equal(h.calls.length, 0);
});

test('call and invoke reach distinct Core APIs and preserve a zero Python answer', async () => {
  const h = setup(); await flush(); h.get('multiply-left').value = '0'; h.get('multiply-right').value = '7';
  await h.click('multiply-call'); await h.click('multiply-invoke');
  const actual = h.calls.filter(item => item.method === 'demo.python.multiply');
  assert.deepEqual(actual, [
    {route: 'call', method: 'demo.python.multiply', params: {left: 0, right: 7}},
    {route: 'invoke', method: 'demo.python.multiply', params: {left: 0, right: 7}}
  ]);
  assert.equal(h.get('multiply-result').textContent, '= 0');
  assert.deepEqual(h.sends.filter(item => item.name === 'demo:browser.result').map(item => item.detail.action), ['demo.python.multiply.call', 'demo.python.multiply.invoke']);
});

test('height controls call the native-scene tools and scene events render actual readback', async () => {
  const h = setup(); await flush(); h.get('height-range').value = '200'; h.get('height-range').oninput.call(h.get('height-range'));
  assert.equal(h.get('height-target').textContent, '200'); assert.equal(h.get('height-current').textContent, '0');
  await h.click('lift-cube'); assert.deepEqual(h.calls.at(-1), {route: 'call', method: 'demo.scene.set_height', params: {height: 200}});
  assert.equal(h.get('height-current').textContent, '200');
  h.event('demo:scene', {height: 260, revision: 7});
  assert.equal(h.get('height-current').textContent, '260'); assert.equal(h.get('scene-revision').textContent, '7');
  assert.equal(h.get('height-range').value, '200', 'host push preserves the unsubmitted target');
  await h.click('reset-scene'); assert.equal(h.calls.at(-1).method, 'demo.scene.reset'); assert.equal(h.get('height-current').textContent, '0');
});

test('event success requires the matching Python nonce and preserves false and zero', async () => {
  const h = setup(); await flush(); await h.click('event-send');
  const sent = h.sends.find(item => item.name === 'demo:event.request');
  assert.equal(sent.detail.false_value, false); assert.equal(sent.detail.zero, 0);
  assert.equal(h.get('event-state').textContent, 'AWAITING PYTHON'); assert.equal(h.get('event-send').disabled, true);
  h.event('demo:event.reply', {...sent.detail, nonce: 'wrong', source: 'python'});
  assert.equal(h.get('event-state').textContent, 'AWAITING PYTHON');
  const reply = {...sent.detail, source: 'python'}; h.event('demo:event.reply', reply);
  assert.equal(h.get('event-state').textContent, 'PYTHON REPLIED'); assert.equal(h.get('event-send').disabled, false); assert.equal(h.timers.size, 0);
  assert.deepEqual(JSON.parse(h.get('event-result').textContent), reply);
  assert.deepEqual(h.sends.find(item => item.name === 'demo:browser.result').detail, {action: 'demo:event.reply', result: reply});
});

test('an invalid event acknowledgement is visibly rejected instead of claiming success', async () => {
  const h = setup(); await flush(); await h.click('event-send'); const sent = h.sends.find(item => item.name === 'demo:event.request');
  h.event('demo:event.reply', {...sent.detail, false_value: true, source: 'python'});
  assert.equal(h.get('event-state').textContent, 'INVALID REPLY'); assert.equal(h.get('error-banner').hidden, false);
  assert.equal(h.sends.some(item => item.name === 'demo:browser.result'), false);
});

test('event send is not acknowledgement; a missing reply times out visibly', async () => {
  const h = setup(); await flush(); await h.click('event-send'); const timer = [...h.timers.values()][0]; timer();
  assert.equal(h.get('event-state').textContent, 'NO REPLY'); assert.match(h.get('event-result').textContent, /8 seconds/);
  assert.match(h.get('error-banner').textContent, /acknowledgement timed out/);
  assert.equal(h.sends.some(item => item.name === 'demo:browser.result'), false);
});

test('tool failure remains visible after later success and native scene updates', async () => {
  const h = setup(); await flush(); h.setResponse(() => Promise.reject({code: 'TOOL_UNAVAILABLE', message: 'Python tool disconnected'}));
  await h.click('multiply-call');
  assert.equal(h.get('error-banner').hidden, false); assert.match(h.get('error-banner').textContent, /TOOL_UNAVAILABLE: Python tool disconnected/);
  assert.equal(h.sends.some(item => item.name === 'demo:browser.result'), false); assert.equal(h.get('multiply-call').disabled, false);
  h.setResponse(() => ({value: 42, python_version: '3.12.15'})); await h.click('multiply-call');
  h.event('demo:scene', {height: 70, revision: 9});
  assert.match(h.get('error-banner').textContent, /Python tool disconnected/); assert.equal(h.get('multiply-result').textContent, '= 42');
});

test('malformed status never marks Python ready or emits browser-ready evidence', async () => {
  const h = setup({response: () => ({...status(), python_version: ''})}); await flush();
  assert.equal(h.get('python-badge').textContent, 'PYTHON · ERROR'); assert.equal(h.get('lift-cube').disabled, true);
  assert.match(h.get('error-banner').textContent, /Incomplete Unreal/); assert.equal(h.sends.length, 0);
});

test('backend disconnect disables every mutation and clears pending event timer', async () => {
  const h = setup(); await flush(); await h.click('event-send'); h.event('backend_error', {message: 'connection closed'});
  assert.equal(h.get('bridge-badge').textContent, 'BRIDGE · ERROR');
  for (const id of ['lift-cube', 'reset-scene', 'multiply-call', 'multiply-invoke', 'event-send']) assert.equal(h.get(id).disabled, true);
  assert.equal(h.timers.size, 0); assert.match(h.get('error-banner').textContent, /connection closed/);
});

test('busy tools cannot queue duplicate scene writes and invalid inputs do not call Python', async () => {
  const h = setup(); await flush(); let resolve;
  h.setResponse(() => new Promise(done => { resolve = done; }));
  h.get('lift-cube').onclick(); h.get('lift-cube').onclick(); await flush();
  assert.equal(h.calls.filter(item => item.method === 'demo.scene.set_height').length, 1);
  resolve({height: 150, revision: 1}); await flush();
  const before = h.calls.length; h.get('multiply-left').value = ''; await h.click('multiply-call');
  assert.equal(h.calls.length, before); assert.match(h.get('error-banner').textContent, /two finite numbers/);
});

test('payload text is rendered safely and page shutdown removes Core subscriptions', async () => {
  const h = setup(); await flush(); h.get('event-message').value = '<img src=x onerror=bad()>'; await h.click('event-send');
  const sent = h.sends.find(item => item.name === 'demo:event.request'); h.event('demo:event.reply', {...sent.detail, source: 'python'});
  assert.match(h.get('event-result').textContent, /<img src=x onerror=bad\(\)>/);
  h.unload(); assert.equal(Object.keys(h.subscriptions).length, 0); assert.equal(h.get('lift-cube').disabled, true);
});

test('the new page is self-contained, uses no remote resources or modern-script syntax', () => {
  assert.doesNotMatch(html, /<(?:script|link)\b[^>]*(?:src|href)=/i);
  assert.doesNotMatch(script, /\b(?:async|await|const|let)\b|=>|\?\.|\.\.\./);
  assert.match(html, /min="0" max="300"/); assert.match(script, /window\.auroraview\.whenReady\(\)/);
});

test('offline animation is default off and runs a bounded workload without host calls or events', async () => {
  const h = setup({noBridge: true}); await flush();
  assert.equal(h.animationRequests.size, 0); assert.equal(h.paints.length, 0);
  await h.click('animation-light'); h.frame(0); h.frame(1000);
  assert.equal(h.paints.length, 66, 'one clear and exactly 32 marks per callback');
  assert.match(h.get('browser-fps').textContent, /^1\.0 · 32 marks/);
  assert.equal(h.animationRequests.size, 1);
  await h.click('animation-heavy'); assert.equal(h.animationRequests.size, 1, 'switch replaces the one pending callback');
  const before = h.paints.length; h.frame(2000);
  assert.equal(h.paints.length - before, 257);
  assert.equal(h.calls.length, 0); assert.equal(h.sends.length, 0);
  assert.equal(h.get('lift-cube').disabled, true);
  await h.click('animation-off'); assert.equal(h.animationRequests.size, 0);
  assert.equal(h.get('browser-fps').textContent, 'Not sampled'); h.unload();
});

test('browser FPS excludes hidden time and restarts its visible sample after mode changes', async () => {
  const h = setup({noBridge: true}); await h.click('animation-light');
  h.frame(0); h.frame(500); h.visibility(true);
  assert.equal(h.animationRequests.size, 0); assert.equal(h.get('animation-state').textContent, 'Paused · page hidden');
  h.visibility(false); h.frame(10000);
  assert.equal(h.get('browser-fps').textContent, 'Waiting for a visible 1 s sample');
  h.frame(10500); h.frame(11000); assert.match(h.get('browser-fps').textContent, /^2\.0 · 32 marks/);
  await h.click('animation-heavy'); h.frame(12000);
  assert.equal(h.get('browser-fps').textContent, 'Waiting for a visible 1 s sample');
  h.frame(13000); assert.match(h.get('browser-fps').textContent, /^1\.0 · 256 marks/);
  h.unload(); assert.equal(h.animationRequests.size, 0); assert.equal(h.get('animation-light').disabled, true);
});

test('local animation survives backend disconnect and never schedules a timer fallback', async () => {
  const h = setup(); await flush(); await h.click('animation-light'); h.frame(0);
  h.event('backend_error', {message: 'owned provider stopped'}); h.frame(1000);
  assert.match(h.get('animation-state').textContent, /Running/); assert.equal(h.timers.size, 0);
  const calls = h.calls.length; await h.click('animation-heavy'); h.frame(2000); assert.equal(h.calls.length, calls);
  h.unload(); assert.equal(h.animationRequests.size, 0);
  for (const options of [{noRaf: true}, {noCanvas: true}]) {
    const unavailable = setup({...options, noBridge: true}); await flush();
    assert.equal(unavailable.get('animation-light').disabled, true); assert.equal(unavailable.animationRequests.size, 0);
    assert.match(unavailable.get('animation-state').textContent, /Unavailable/); unavailable.unload();
  }
});

test('RTT measures successful Core response time separately from RAF and retains last success on failure', async () => {
  const h = setup(); await flush(); let complete;
  h.setResponse(() => new Promise(resolve => { complete = resolve; }));
  const call = h.click('refresh-status'); await flush(); h.advance(27.5); complete(status()); await call; await flush();
  assert.equal(h.get('bridge-rtt').textContent, '27.5 ms · demo.status');
  await h.click('animation-light'); h.frame(0); h.frame(1000);
  assert.equal(h.get('bridge-rtt').textContent, '27.5 ms · demo.status');
  h.setResponse(() => Promise.reject(new Error('response failed'))); h.advance(900); await h.click('refresh-status');
  assert.equal(h.get('bridge-rtt').textContent, '27.5 ms · demo.status');
  assert.match(h.get('error-banner').textContent, /response failed/);
  assert.match(html, /UNREAL \/ SLATE FRAME RATE<\/dt><dd>Not measured/);
  assert.match(html, /CAPTURE FRAME RATE<\/dt><dd>Not measured/); h.unload();
});
