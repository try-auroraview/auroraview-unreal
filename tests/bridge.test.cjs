const nativeTest = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '..');
for (const variant of ['pinned', 'chrome59']) {
const test = (name, callback) => nativeTest(variant + ': ' + name, callback);
const coreDirectory = variant === 'chrome59' ? 'Resources/legacy' : 'ThirdParty/AuroraViewCore';
const core = fs.readFileSync(path.join(root, coreDirectory, 'event_bridge.js'), 'utf8');
const stub = fs.readFileSync(path.join(root, coreDirectory, 'bridge_stub.js'), 'utf8');
const transport = fs.readFileSync(path.join(root, 'Resources/ue_transport.js'), 'utf8');
const bootstrap = fs.readFileSync(path.join(root, 'Resources/ue_bootstrap.js'), 'utf8');

function host(native = () => Promise.resolve(true), options = {}) {
  const events = new Map(), timers = new Map(), wire = [], readySignals = [];
  let timer = 0;
  const win = {
    location: {href: 'https://auroraview.invalid/test/index.html'},
    ue: {auroraview: {postmessage(payload, token) {
      wire.push({payload: JSON.parse(payload), token});
      return native(payload, token);
    }, markready(token) {
      readySignals.push(token);
      return options.nativeReady ? options.nativeReady(token) : Promise.resolve(true);
    }}},
    addEventListener(name, fn) { (events.get(name) || (events.set(name, []), events.get(name))).push(fn); },
    removeEventListener(name, fn) {events.set(name, (events.get(name) || []).filter(x=>x!==fn));},
    dispatchEvent(event) { for (const fn of events.get(event.type) || []) fn(event); },
  };
  win.top = options.frame ? {} : win;
  const context = vm.createContext({window: win,
    document: {readyState: 'complete', addEventListener() {}, documentElement: {}},
    console: {log(){}, warn(){}, error(){}, debug(){}},
    CustomEvent: class {constructor(type, detail){this.type=type;this.detail=detail;}},
    setTimeout(fn, delay) {const id=++timer;timers.set(id,{fn,delay});return id;},
    clearTimeout(id) {timers.delete(id);}, setInterval(){}, clearInterval(){},
    Element: class {}, getComputedStyle(){return {getPropertyValue(){return '';}};}
  });
  function installTransport() {
    vm.runInContext(transport, context);
    return win.__auroraviewInstallUETransport('session-token');
  }
  if (!options.noTransport) assert.equal(installTransport(), !options.frame);
  if (!options.noCore) vm.runInContext(core, context);
  return {win, wire, readySignals, timers, context, installTransport,
    result(id, result) {win.auroraview.trigger('__auroraview_call_result', {id,ok:true,result});},
    event(name) {win.dispatchEvent({type:name});}
  };
}
function callMessage(h) { return h.wire.filter(x => x.payload.type === 'call').at(-1).payload; }

test('uses exact Core call envelope and native session token; JSON values round trip', async () => {
  for (const value of [{unicode:'你好', quote:'"\\\n'}, [1,true,null], 42, null]) {
    const h=host(); const pending=h.win.auroraview.call('api.echo', value);
    const msg=callMessage(h);
    assert.equal(msg.method,'api.echo'); assert.deepEqual(msg.params,value);
    assert.equal(h.wire.at(-1).token,'session-token');
    h.result(msg.id,value); assert.deepEqual(await pending,value);
  }
});
test('upstream error name, code and data survive native response', async () => {
  const h=host(); const pending=h.win.auroraview.call('demo.error');
  h.win.auroraview.trigger('__auroraview_call_result', {id:callMessage(h).id,ok:false,
    error:{name:'DemoError',message:'expected',code:'E_DEMO',data:{field:1}}});
  await assert.rejects(pending,e=>e.name==='DemoError'&&e.code==='E_DEMO'&&e.data.field===1);
});
test('rejected native queue settles the Core promise', async () => {
  const h=host(()=>Promise.resolve(false));
  await assert.rejects(h.win.auroraview.call('api.echo'),e=>e.code==='TRANSPORT_REJECTED');
});
test('failed native future settles the Core promise', async () => {
  const h=host(()=>Promise.reject(new Error('CEF unavailable')));
  await assert.rejects(h.win.auroraview.call('api.echo'),e=>e.name==='TransportError');
});
test('synchronous transport failure is handled by unmodified Core', async () => {
  const h=host(()=>{throw new Error('binding gone');});
  await assert.rejects(h.win.auroraview.call('api.echo'),/binding gone/);
});
test('Core timeout clears pending state and ignores late response', async () => {
  const h=host(); const pending=h.win.auroraview.call('api.echo',{}, {timeout:7});
  const timeout=[...h.timers.values()].find(x=>x.delay===7); assert.ok(timeout); timeout.fn();
  await assert.rejects(pending,e=>e.code==='TIMEOUT'); h.result(callMessage(h).id,'too late');
});
test('close signal cancels pending calls; later calls fail fast', async () => {
  const h=host(); const pending=h.win.auroraview.call('api.echo');
  h.win.auroraview.trigger('backend_error',{message:'connection lost: Unreal view closed'});
  await assert.rejects(pending,e=>e.code==='CANCELLED');
  await assert.rejects(h.win.auroraview.call('api.echo'),e=>e.code==='BACKEND_UNAVAILABLE');
});
test('unload cancels pending calls', async () => {
  const h=host(); const pending=h.win.auroraview.call('api.echo'); h.event('beforeunload');
  await assert.rejects(pending,e=>e.code==='CANCELLED');
});
test('independent contexts do not share pending results', async () => {
  const a=host(), b=host();
  const pa=a.win.auroraview.call('api.echo','a'), pb=b.win.auroraview.call('api.echo','b');
  a.result(callMessage(a).id,'result-a'); b.result(callMessage(b).id,'result-b');
  assert.equal(await pa,'result-a'); assert.equal(await pb,'result-b');
});
test('registered Core API methods use the same wire', async () => {
  const h=host(); h.win.auroraview._registerApiMethods('api',['echo']);
  const pending=h.win.auroraview.api.echo({value:1});
  assert.equal(callMessage(h).method,'api.echo'); h.result(callMessage(h).id,true);
  assert.equal(await pending,true);
});
test('stub call survives delayed Core installation', async () => {
  const h=host(undefined,{noCore:true}); vm.runInContext(stub,h.context);
  vm.runInContext(bootstrap,h.context);
  const pending=h.win.auroraview.call('api.echo',123);
  vm.runInContext(core,h.context); h.result(callMessage(h).id,123);
  assert.equal(await pending,123);
});
test('transport refuses subframes and missing native binding', () => {
  host(undefined,{noCore:true,frame:true});
  const h=host(undefined,{noCore:true}); delete h.win.ue;
  assert.equal(h.win.__auroraviewInstallUETransport('x'),false);
});
test('Core events use trigger and unsubscribe correctly', () => {
  const h=host(); let count=0;
  const off=h.win.auroraview.on('selection',()=>count++);
  h.win.auroraview.trigger('selection',{}); off(); h.win.auroraview.trigger('selection',{});
  assert.equal(count,1);
});
test('256 startup calls cannot consume the reserved ready handshake', async () => {
  const admitted=[];
  const h=host(payload=>{
    if(admitted.length>=256) return Promise.resolve(false);
    admitted.push(JSON.parse(payload)); return Promise.resolve(true);
  }, {noCore:true});
  vm.runInContext(stub,h.context); vm.runInContext(bootstrap,h.context);
  const pending=Array.from({length:256},(_,n)=>h.win.auroraview.call('api.echo',n));
  vm.runInContext(core,h.context);
  assert.equal(admitted.length,256); assert.equal(h.readySignals.length,1);
  assert.equal(h.win.auroraview.isReady(),true);
  for(const msg of admitted) h.result(msg.id,msg.params);
  assert.equal((await Promise.all(pending)).length,256);
});
test('failed ready acknowledgement cancels pending Core calls visibly', async () => {
  const h=host(undefined,{nativeReady:()=>Promise.resolve(false)});
  const pending=h.win.auroraview.call('api.echo');
  await assert.rejects(pending,e=>e.code==='CANCELLED');
  await assert.rejects(h.win.auroraview.call('api.echo'),e=>e.code==='BACKEND_UNAVAILABLE');
});
test('ready native future rejection is a fatal backend error', async () => {
  const h=host(undefined,{nativeReady:()=>Promise.reject(new Error('ready failed'))});
  await assert.rejects(h.win.auroraview.call('api.echo'),e=>e.code==='CANCELLED');
});
test('early invoke is rejected before Core can replay it as a host call', async () => {
  const h=host(undefined,{noCore:true});
  vm.runInContext(stub,h.context); vm.runInContext(bootstrap,h.context);
  await assert.rejects(h.win.auroraview.invoke('api.echo',{value:1}),e=>e.code==='UNSUPPORTED');
  vm.runInContext(core,h.context);
  assert.equal(h.wire.filter(x=>x.payload.type==='call').length,0);
});
test('post-ready invoke receives the canonical invoke result', async () => {
  const h=host(); const pending=h.win.auroraview.invoke('api.echo',{value:1});
  const message=h.wire.at(-1).payload; assert.equal(message.type,'invoke');
  h.win.auroraview.trigger('__invoke_result__',{id:message.id,ok:true,result:42});
  assert.equal(await pending,42);
});
test('rejected invoke queue uses the canonical result event', async () => {
  const h=host(()=>Promise.resolve(false));
  const original=h.win.auroraview.trigger;
  const names=[];
  h.win.auroraview.trigger=function(name,data){names.push(name);return original.call(this,name,data);};
  await assert.rejects(h.win.auroraview.invoke('tools.echo'),e=>e.code==='TRANSPORT_REJECTED');
  assert.ok(names.includes('__invoke_result__'));
});
test('early whenReady resolves to the installed Core bridge', async () => {
  const h=host(undefined,{noCore:true});
  vm.runInContext(stub,h.context); vm.runInContext(bootstrap,h.context);
  const ready=h.win.auroraview.whenReady(); vm.runInContext(core,h.context);
  for(const task of h.timers.values()) if(task.delay===0) task.fn();
  assert.equal(await ready,h.win.auroraview);
});
test('close before Core ready rejects the adapted ready waiter', async () => {
  const h=host(undefined,{noCore:true});
  vm.runInContext(stub,h.context); vm.runInContext(bootstrap,h.context);
  const ready=h.win.auroraview.whenReady(); h.event('beforeunload');
  await assert.rejects(ready,e=>e.code==='CANCELLED');
});
test('real startup order tolerates fragment calls before transport installation', async () => {
  const h=host(undefined,{noCore:true,noTransport:true});
  vm.runInContext(stub,h.context); vm.runInContext(bootstrap,h.context);
  const pending=h.win.auroraview.call('api.echo','queued by fragment');
  assert.equal(h.win.ipc,undefined); assert.equal(h.wire.length,0);
  assert.equal(h.installTransport(),true); vm.runInContext(core,h.context);
  h.result(callMessage(h).id,'round trip'); assert.equal(await pending,'round trip');
});
test('synchronous MarkReady failure cancels startup replay calls', async () => {
  const h=host(undefined,{noCore:true,nativeReady:()=>{throw new Error('ready binding gone');}});
  vm.runInContext(stub,h.context); vm.runInContext(bootstrap,h.context);
  const pending=h.win.auroraview.call('api.echo'); vm.runInContext(core,h.context);
  await assert.rejects(pending,e=>e.code==='CANCELLED');
});
test('missing MarkReady prevents installation of the transport', () => {
  const h=host(undefined,{noCore:true,noTransport:true}); delete h.win.ue.auroraview.markready;
  assert.equal(h.installTransport(),false); assert.equal(h.win.ipc,undefined);
});
test('multiple early ready waiters resolve once and survive subsequent unload', async () => {
  const h=host(undefined,{noCore:true});
  vm.runInContext(stub,h.context); vm.runInContext(bootstrap,h.context);
  const pending=[h.win.auroraview.whenReady(),h.win.auroraview.whenReady()];
  vm.runInContext(core,h.context);
  for(const task of h.timers.values()) if(task.delay===0) task.fn();
  const results=await Promise.all(pending);
  assert.equal(results[0],h.win.auroraview); assert.equal(results[1],h.win.auroraview);
  h.event('beforeunload'); assert.deepEqual(await Promise.all(pending),results);
});
test('retained startup whenReady resolves immediately after Core is ready', async () => {
  const h=host(undefined,{noCore:true});
  vm.runInContext(stub,h.context); vm.runInContext(bootstrap,h.context);
  const wait=h.win.auroraview.whenReady; vm.runInContext(core,h.context);
  assert.equal(await wait(),h.win.auroraview);
});
}
