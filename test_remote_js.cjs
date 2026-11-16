const assert = require('node:assert/strict');
const { webcrypto } = require('node:crypto');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = readFileSync(join(__dirname, 'remote.js'), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));

function element() {
  return {
    events: {}, dataset: {}, attributes: {}, value: '',
    classList: { add() {}, remove() {}, toggle() {} },
    contains(target) { return target === this || target.parent === this; },
    addEventListener(name, handler) { this.events[name] = handler; },
    setAttribute(name, value) { this.attributes[name] = value; },
    querySelector: () => element(), querySelectorAll: () => [],
    focus() {}, setPointerCapture() {},
  };
}

async function remote({ unavailable = [], approve = false, claimReply, reply, statusReply,
                        gestureSettings = {}, pairOrigins } = {}) {
  const elements = new Map();
  const bright = element();
  const space = element();
  bright.dataset = { key: 'bright_up' };
  space.dataset = { key: 'space' };
  const left = element();
  const right = element();
  left.dataset = { click: 'left', count: '1' };
  right.dataset = { click: 'right', count: '1' };
  const documentEvents = {};
  const windowEvents = {};
  const document = {
    hidden: false,
    addEventListener(name, callback) { documentEvents[name] = callback; },
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, element());
      return elements.get(id);
    },
    querySelector: () => element(),
    querySelectorAll: selector => selector === '[data-click]' ? [left, right]
      : selector === '[data-key]:not([data-swipe])' ? [bright, space] : [],
  };
  const get = document.getElementById;
  get('speedSlider').value = '2';
  const commands = [];
  const requests = [];
  let authorized = !approve && !pairOrigins;
  let pendingPair = false;
  let approved = approve;
  let dragging = false;
  let timestamp = 1000;
  let frame;
  const scope = vm.createContext({
    document, window: { addEventListener(name, callback) { windowEvents[name] = callback; } }, navigator: {},
    localStorage: { getItem() {}, setItem() {} },
    AbortController, URL, URLSearchParams, crypto: webcrypto,
    location: { origin: 'https://remote.local:8765', hash: '', pathname: '/', search: '' },
    setTimeout() {}, clearTimeout() {}, setInterval() {},
    requestAnimationFrame(callback) { frame = callback; },
    Date: { now: () => timestamp },
    async fetch(path, options) {
      requests.push({ path, options });
      if (path === '/api/pair') {
        pendingPair = true;
        return { status: 202, json: async () => ({}) };
      }
      if (path === '/api/claim') {
        if (claimReply) await claimReply();
        approved = false;
        authorized = true;
        return { status: 200, json: async () => ({ tab: 'approved-tab' }) };
      }
      if (path === '/api/disconnect') {
        authorized = false;
        return { status: 200, json: async () => ({}) };
      }
      if (path === '/api/status') {
        if (statusReply) await statusReply();
        if (!authorized) return { status: 401, json: async () => ({
          approved, remembered: true, pairing_origins: pairOrigins,
          match: pendingPair ? '340287' : null,
        }) };
        return { status: 200, json: async () => ({
          ready: true, dragging, unavailable,
          gestures: {
            tap: false, secondary_tap: false, pinch_zoom: true,
            swipes: ['swipe_up', 'swipe_left'], ...gestureSettings,
          },
        }) };
      }
      assert.equal(path, '/api/cmd');
      const command = JSON.parse(options.body);
      commands.push(command);
      if (reply) await reply(command);
      if (command.action === 'drag') dragging = command.active;
      return { status: 200, json: async () => ({ dragging }) };
    },
  });
  vm.runInContext(source, scope);
  await settle();
  const pad = get('trackpad');
  return {
    commands, requests, get, left, right, bright, space,
    scan: link => vm.runInContext('acceptScannedPairingLink(' + JSON.stringify(link) + ')', scope),
    async visibility(hidden) {
      document.hidden = hidden;
      documentEvents.visibilitychange();
      await settle();
    },
    async reconnect() {
      windowEvents.online();
      await settle();
    },
    step: async () => { frame(timestamp); await settle(); },
    advance: milliseconds => { timestamp += milliseconds; },
    point: (identifier, clientX, clientY, target = pad) => ({ identifier, clientX, clientY, target }),
    touch(name, touches, changedTouches = touches) {
      pad.events[name]({ touches, changedTouches, preventDefault() {} });
    },
    pointer(name, clientX, clientY, button = 0) {
      pad.events[name]({ pointerType: 'mouse', pointerId: 1, clientX, clientY, button, preventDefault() {} });
    },
  };
}

const click = button => ({ action: 'click', button, count: 1 });

test('status still checks in during a slow input and tolerates a missed poll', async () => {
  let complete;
  const blocked = new Promise(resolve => { complete = resolve; });
  let fail = false;
  const pad = await remote({
    reply: () => blocked,
    statusReply: () => { if (fail) throw new Error('Temporary network failure'); },
  });
  pad.left.events.click();
  fail = true;
  await pad.reconnect();
  assert.equal(pad.requests.filter(request => request.path === '/api/status').length, 2);
  assert.equal(pad.get('remoteControls').hidden, false);
  fail = false;
  await pad.reconnect();
  complete();
  await settle();
  assert.equal(pad.get('connectionStatus').textContent, 'Connected securely');
});

test('both mouse buttons dispatch their own click immediately', async () => {
  const pad = await remote();
  pad.left.events.click();
  pad.right.events.click();
  await settle();
  assert.deepEqual(pad.commands, [click('left'), click('right')]);
});

test('one-finger taps click despite disabled Mac tap-to-click and tolerate touch jitter', async () => {
  const pad = await remote();
  const start = pad.point(1, 100, 100);
  const end = pad.point(1, 104, 103);
  pad.touch('touchstart', [start]);
  pad.touch('touchmove', [end]);
  pad.advance(130);
  pad.touch('touchend', [], [end]);
  await settle();
  assert.deepEqual(pad.commands, [click('left')]);

  pad.advance(160);
  pad.touch('touchstart', [start]);
  pad.touch('touchend', [], [start]);
  await settle();
  assert.deepEqual(pad.commands[1], { ...click('left'), continuation: true });
});

test('two-finger taps with separate targets and staggered lifts send one right click', async () => {
  const pad = await remote();
  const one = pad.point(1, 100, 100);
  const two = pad.point(2, 124, 100, { parent: pad.get('trackpad') });
  const outside = pad.point(3, 30, 30, element());
  pad.touch('touchstart', [one, outside], [one, outside]);
  pad.advance(45);
  pad.touch('touchstart', [one, two, outside], [two]);
  const nearOne = { ...one, clientY: 107 };
  const nearTwo = { ...two, clientY: 106 };
  pad.touch('touchmove', [nearOne, nearTwo, outside]);
  pad.advance(130);
  pad.touch('touchend', [nearTwo, outside], [nearOne]);
  assert.deepEqual(pad.commands, []);
  pad.touch('touchend', [outside], [nearTwo]);
  await settle();
  assert.deepEqual(pad.commands, [click('right')]);
});

test('a two-finger scroll and a cancelled contact cannot become clicks', async () => {
  const pad = await remote();
  const fingers = [pad.point(1, 100, 100), pad.point(2, 124, 100)];
  const moved = fingers.map(finger => ({ ...finger, clientY: 125 }));
  pad.touch('touchstart', fingers);
  pad.touch('touchmove', moved);
  pad.touch('touchend', [], moved);
  await pad.step();
  assert.deepEqual(pad.commands, [{ action: 'scroll', dx: 0, dy: 55 }]);

  pad.touch('touchstart', [fingers[0]]);
  pad.touch('touchcancel', []);
  pad.touch('touchend', [], [fingers[0]]);
  await settle();
  assert.equal(pad.commands.length, 1);
});

test('three-finger swipes use the final touch sample and fire once', async () => {
  const pad = await remote();
  const fingers = [pad.point(1, 100, 100), pad.point(2, 124, 100), pad.point(3, 147, 100)];
  const up = fingers.map(finger => ({ ...finger, clientY: 48 }));
  pad.touch('touchstart', fingers);
  pad.touch('touchend', up.slice(1), [up[0]]);
  pad.touch('touchmove', up.slice(1));
  pad.touch('touchend', [], up.slice(1));
  await settle();
  assert.deepEqual(pad.commands, [{ action: 'key', name: 'swipe_up' }]);
});

test('a committed swipe is not repeated when a finger is replaced', async () => {
  const pad = await remote();
  const fingers = [pad.point(1, 100, 100), pad.point(2, 124, 100), pad.point(3, 147, 100)];
  const up = fingers.map(finger => ({ ...finger, clientY: 48 }));
  pad.touch('touchstart', fingers);
  pad.touch('touchmove', up);
  pad.touch('touchend', up.slice(1), [up[0]]);
  const replacement = pad.point(4, 100, 48);
  const replaced = [replacement, ...up.slice(1)];
  pad.touch('touchstart', replaced, [replacement]);
  pad.touch('touchmove', replaced.map(finger => ({ ...finger, clientY: 0 })));
  await settle();
  assert.deepEqual(pad.commands, [{ action: 'key', name: 'swipe_up' }]);
});

test('four fingers are accepted only for directions enabled on the Mac', async () => {
  const pad = await remote({ gestureSettings: { swipe_fingers: { swipe_up: [4], swipe_left: [3] } } });
  const four = Array.from({ length: 4 }, (_, index) => pad.point(index + 1, 65 + index * 24, 100));
  const three = four.slice(0, 3);
  for (const fingers of [three, four]) {
    const moved = fingers.map(finger => ({ ...finger, clientY: 50 }));
    pad.touch('touchstart', fingers);
    pad.touch('touchmove', moved);
    pad.touch('touchend', [], moved);
  }
  await settle();
  assert.deepEqual(pad.commands, [{ action: 'key', name: 'swipe_up' }]);
});

test('Mac swipe mode uses one finger and keeps two-finger scroll and right click', async () => {
  const pad = await remote();
  pad.get('swipeModeBtn').events.click();
  const one = pad.point(1, 100, 100);
  const left = { ...one, clientX: 48 };
  pad.touch('touchstart', [one]);
  pad.touch('touchmove', [left]);
  pad.touch('touchend', [], [left]);
  await settle();

  const two = [one, pad.point(2, 124, 100)];
  const scroll = two.map(finger => ({ ...finger, clientY: 125 }));
  pad.touch('touchstart', two);
  pad.touch('touchmove', scroll);
  pad.touch('touchend', [], scroll);
  await pad.step();
  pad.touch('touchstart', two);
  pad.touch('touchend', [], two);
  await settle();
  assert.deepEqual(pad.commands, [
    { action: 'key', name: 'swipe_left' },
    { action: 'scroll', dx: 0, dy: 55 },
    click('right'),
  ]);

  pad.pointer('pointerdown', 100, 100);
  pad.pointer('pointerup', 100, 48);
  await settle();
  assert.deepEqual(pad.commands.at(-1), { action: 'key', name: 'swipe_up' });
});

test('a short pinch with widely spaced fingers sends a zoom step', async () => {
  const pad = await remote();
  const fingers = [pad.point(1, 80, 100), pad.point(2, 250, 100)];
  const spread = [{ ...fingers[0], clientX: 70 }, { ...fingers[1], clientX: 260 }];
  pad.touch('touchstart', fingers);
  pad.touch('touchmove', spread);
  pad.touch('touchend', [], spread);
  await settle();
  assert.deepEqual(pad.commands, [{ action: 'key', name: 'zoom_in' }]);
});

test('minor mouse movement while pressing the pad still clicks', async () => {
  const pad = await remote();
  pad.pointer('pointerdown', 100, 100);
  pad.pointer('pointermove', 102, 101);
  pad.pointer('pointerup', 102, 101);
  await settle();
  assert.deepEqual(pad.commands, [click('left')]);
});

test('buttons for keys the computer cannot send are hidden', async () => {
  const pad = await remote({ unavailable: ['bright_up'] });
  assert.equal(pad.bright.hidden, true);
  assert.equal(pad.space.hidden, false);
});

test('small drift stays a tap for primary and secondary clicks', async () => {
  const pad = await remote();
  for (const fingers of [1, 2]) {
    const contacts = Array.from({ length: fingers }, (_, index) => pad.point(index + 1, 30 + 50 * index, 60));
    const shifted = contacts.map(point => ({ ...point, clientY: point.clientY + (fingers === 1 ? 8 : 11) }));
    pad.touch('touchstart', contacts);
    pad.touch('touchmove', shifted);
    pad.touch('touchend', [], shifted);
    await pad.step();
  }
  assert.deepEqual(pad.commands, [click('left'), click('right')]);
});

test('a short app switch pauses input and resumes the same approved tab', async () => {
  const pad = await remote({ approve: true });
  await pad.visibility(true);
  assert.equal(pad.get('remoteControls').hidden, true);
  pad.left.events.click();
  assert.equal(pad.commands.length, 0);
  await pad.visibility(false);
  const status = pad.requests.at(-1);
  assert.equal(status.path, '/api/status');
  assert.equal(status.options.headers['X-Lazy-Remote-Tab'], 'approved-tab');
  assert.equal(pad.get('remoteControls').hidden, false);
  assert.equal(pad.requests.some(item => item.path === '/api/disconnect'), false);
  await pad.get('disconnectBtn').events.click();
  await settle();
  assert.equal(pad.get('pairTitle').textContent, 'Reconnect');
});

test('foreground reconnect takes over if the browser freezes an old status request', async () => {
  let finish;
  const blocked = new Promise((_, reject) => { finish = reject; });
  let holdStatus = false;
  const pad = await remote({ approve: true, statusReply: () => holdStatus ? blocked : undefined });
  holdStatus = true;
  await pad.reconnect();
  await pad.visibility(true);
  holdStatus = false;
  await pad.visibility(false);
  assert.equal(pad.get('connectionStatus').textContent, 'Connected securely');
  finish(new Error('Background request aborted'));
  await settle();
  assert.equal(pad.get('connectionStatus').textContent, 'Connected securely');
});

test('a desktop approval arriving during an app switch is retained', async () => {
  let finish;
  const blocked = new Promise(resolve => { finish = resolve; });
  const pad = await remote({ approve: true, claimReply: () => blocked });
  await pad.visibility(true);
  finish();
  await settle();
  await pad.visibility(false);
  assert.equal(pad.get('connectionStatus').textContent, 'Connected securely');
  assert.equal(pad.requests.filter(item => item.path === '/api/claim').length, 1);
});

test('a resumed tab keeps one ordered motion sequence', async () => {
  const pad = await remote({ approve: true });
  const finger = pad.point(1, 30, 60);
  const moved = { ...finger, clientX: 60 };
  for (let index = 0; index < 2; index++) {
    pad.advance(20);
    pad.touch('touchstart', [finger]);
    pad.touch('touchmove', [moved]);
    pad.touch('touchend', [], [moved]);
    await pad.step();
    await pad.visibility(true);
    await pad.visibility(false);
  }
  assert.deepEqual(pad.commands.map(command => command.seq), [1, 2]);
  assert.equal(pad.requests.some(item => item.path === '/api/disconnect'), false);
});

test('interrupted typing is restored for explicit review and retry', async () => {
  let fail = true;
  const pad = await remote({ reply: async () => {
    if (fail) throw new Error('Disconnected');
  } });
  const input = pad.get('kbInput');
  input.value = 'draft';
  input.events.input({ isComposing: false });
  await settle();
  assert.equal(input.value, 'draft');
  assert.match(pad.get('typingNotice').textContent, /may already be there/);
  await pad.reconnect();
  fail = false;
  input.value += '!';
  input.events.input({ isComposing: false });
  assert.equal(pad.commands.length, 1);
  pad.get('sendTextBtn').events.click();
  await settle();
  assert.equal(pad.commands.at(-1).text, 'draft!');
  assert.equal(input.value, '');
});

test('in-app scanning accepts this Mac’s certified aliases and waits for Request access', async () => {
  const pad = await remote({ pairOrigins: ['https://192.168.1.7:8765'] });
  const ticket = 'a'.repeat(43);
  assert.throws(() => pad.scan('https://other.local:8765/#pair=' + ticket), /another address/);
  assert.throws(() => pad.scan('https://remote.local:8765/?bad=1#pair=' + ticket), /not a Lazy Remote/);
  assert.throws(() => pad.scan('http://remote.local:8765/#pair=' + ticket), /not a Lazy Remote/);

  pad.scan('https://192.168.1.7:8765/#pair=' + ticket);
  assert.equal(pad.get('pairCode').required, false);
  assert.equal(pad.get('connectionStatus').textContent, 'QR code ready');
  assert.equal(pad.requests.some(request => request.path === '/api/pair'), false);

  await pad.get('pairForm').events.submit({ preventDefault() {} });
  const request = pad.requests.find(request => request.path === '/api/pair');
  assert.equal(JSON.parse(request.options.body).qr, ticket);
  await settle();
  assert.equal(pad.get('matchNumber').textContent, '340287');
});

test('PWA offline fallback only handles the public offline shell', async () => {
  const handlers = new Map();
  const cache = new Map();
  const offline = { body: 'Computer unavailable' };
  const live = { body: 'live page' };
  let offlineNetwork = false;
  vm.runInNewContext(readFileSync(join(__dirname, 'service-worker.js'), 'utf8'), {
    URL,
    self: {
      location: { origin: 'https://remote.local' },
      addEventListener: (name, handler) => handlers.set(name, handler),
    },
    caches: {
      open: async () => ({
        addAll: paths => paths.forEach(path => cache.set(path, offline)),
        match: async path => cache.get(path),
      }),
    },
    fetch: async () => {
      if (offlineNetwork) throw new Error('offline');
      return live;
    },
  });
  let installed;
  handlers.get('install')({ waitUntil: promise => { installed = promise; } });
  await installed;
  assert.deepEqual([...cache.keys()], ['/offline.html', '/remote.css', '/assets/logo.svg']);

  function fetchFromWorker(path, mode = 'cors', method = 'GET') {
    let response;
    handlers.get('fetch')({
      request: { url: 'https://remote.local' + path, mode, method },
      respondWith: promise => { response = promise; },
    });
    return response;
  }

  assert.equal(await fetchFromWorker('/', 'navigate'), live);
  offlineNetwork = true;
  assert.equal(await fetchFromWorker('/', 'navigate'), offline);
  assert.equal(fetchFromWorker('/api/status', 'navigate'), undefined);
  assert.equal(fetchFromWorker('/api/cmd', 'cors', 'POST'), undefined);
  assert.equal(fetchFromWorker('/remote.js'), undefined);
});
