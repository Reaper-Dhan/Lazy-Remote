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

async function remote({ unavailable = [] } = {}) {
  const elements = new Map();
  const bright = element();
  const space = element();
  bright.dataset = { key: 'bright_up' };
  space.dataset = { key: 'space' };
  const left = element();
  const right = element();
  left.dataset = { click: 'left', count: '1' };
  right.dataset = { click: 'right', count: '1' };
  const document = {
    hidden: false,
    addEventListener() {},
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
  let dragging = false;
  let timestamp = 1000;
  let frame;
  vm.runInNewContext(source, {
    document, window: { addEventListener() {} }, navigator: {},
    localStorage: { getItem() {}, setItem() {} },
    AbortController, URLSearchParams, crypto: webcrypto,
    location: { hash: '', pathname: '/', search: '' },
    setTimeout() {}, clearTimeout() {}, setInterval() {},
    requestAnimationFrame(callback) { frame = callback; },
    Date: { now: () => timestamp },
    async fetch(path, options) {
      if (path === '/api/status') {
        return { status: 200, json: async () => ({
          ready: true, dragging, unavailable,
          gestures: { tap: false, secondary_tap: false, pinch_zoom: true, swipes: ['swipe_up', 'swipe_left'] },
        }) };
      }
      assert.equal(path, '/api/cmd');
      const command = JSON.parse(options.body);
      commands.push(command);
      if (command.action === 'drag') dragging = command.active;
      return { status: 200, json: async () => ({ dragging }) };
    },
  });
  await settle();
  const pad = get('trackpad');
  return {
    commands, get, left, right, bright, space,
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

test('Mac swipes dispatch once and the two-finger mode preserves secondary tap', async () => {
  const pad = await remote();
  const fingers = [pad.point(1, 100, 100), pad.point(2, 124, 100), pad.point(3, 147, 100)];
  const up = fingers.map(finger => ({ ...finger, clientY: 48 }));
  pad.touch('touchstart', fingers);
  pad.touch('touchmove', up);
  pad.touch('touchmove', up);
  pad.touch('touchend', [], up);
  await settle();

  pad.get('swipeModeBtn').events.click();
  const two = fingers.slice(0, 2);
  const left = two.map(finger => ({ ...finger, clientX: finger.clientX - 50 }));
  pad.touch('touchstart', two);
  pad.touch('touchmove', left);
  pad.touch('touchend', [], left);
  await settle();
  pad.touch('touchstart', two);
  pad.touch('touchend', [], two);
  await settle();

  assert.deepEqual(pad.commands, [
    { action: 'key', name: 'swipe_up' },
    { action: 'key', name: 'swipe_left' },
    click('right'),
  ]);
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
