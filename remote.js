const trackpad = document.getElementById('trackpad');
const scrollStrip = document.getElementById('scrollStrip');
const speedSlider = document.getElementById('speedSlider');
const speedValue = document.getElementById('speedValue');
const gestureHint = document.getElementById('gestureHint');
const kbInput = document.getElementById('kbInput');
const dragBtn = document.getElementById('dragBtn');
const dragLabel = document.getElementById('dragLabel');
const toast = document.getElementById('toast');
const statusDot = document.getElementById('statusDot');
const connectionStatus = document.getElementById('connectionStatus');
const pairPanel = document.getElementById('pairPanel');
const pairForm = document.getElementById('pairForm');
const pairCode = document.getElementById('pairCode');
const pairTitle = document.getElementById('pairTitle');
const pairHelp = document.getElementById('pairHelp');
const pairCodeFields = document.getElementById('pairCodeFields');
const rememberFields = document.getElementById('rememberFields');
const rememberBrowser = document.getElementById('rememberBrowser');
const enterCodeInstead = document.getElementById('enterCodeInstead');
const pendingPanel = document.getElementById('pendingPanel');
const matchNumber = document.getElementById('matchNumber');
const pairError = document.getElementById('pairError');
const pairSubmit = document.getElementById('pairSubmit');
const remoteControls = document.getElementById('remoteControls');
const controlSettings = document.getElementById('controlSettings');
const disconnectBtn = document.getElementById('disconnectBtn');
const drawer = document.getElementById('drawerBackdrop');
const drawerTrigger = document.querySelector('[data-drawer="open"]');

let sensitivity = Number(speedSlider.value);
try {
  const saved = Number(localStorage.getItem('remote_speed'));
  if (saved >= 0.8 && saved <= 4) sensitivity = saved;
} catch {
  // The remote can run with browser storage disabled.
}
speedSlider.value = sensitivity;
function showSpeed() {
  const value = `${sensitivity.toFixed(1)}×`;
  speedValue.value = value;
  speedSlider.setAttribute('aria-valuetext', `${sensitivity.toFixed(1)} times`);
}

function changeSpeed() {
  sensitivity = Number(speedSlider.value);
  showSpeed();
  try {
    localStorage.setItem('remote_speed', sensitivity);
  } catch {
    // Keep the new speed for this page.
  }
}
showSpeed();
speedSlider.addEventListener('input', changeSpeed);
speedSlider.addEventListener('change', changeSpeed);

const qrValue = new URLSearchParams(location.hash.slice(1)).get('pair');
let qrTicket = /^[A-Za-z0-9_-]{43}$/.test(qrValue ?? '') ? qrValue : null;
if (qrValue !== null) history.replaceState(null, '', location.pathname + location.search);

let paired = false;
let remembered = false;
let awaitingApproval = false;
let manualPairing = false;
let ready = false;
let updatingSession = false;
let checking = false;
let suspended = false;
let releaseDragOnResume = false;
let revision = 0;
let dragLocked = false;
let dragPending = false;
let busy = false;
let motionInFlight = 0;
let motionStream;
let motionSequence = 0;
let motionX = 0;
let motionY = 0;
let lastMotionFrame = -Infinity;
let accumDx = 0;
let accumDy = 0;
let accumScrollX = 0;
let accumScrollY = 0;
let toastTimer;
const pending = [];
const foregroundRequests = new Set();
const gestures = {
  tap: true, secondary_tap: true, scroll: true, horizontal_scroll: true,
  natural_scroll: true, pinch_zoom: false, three_finger_drag: false, swipes: []
};

function updateGestureSettings(settings) {
  Object.assign(gestures, settings);
  const hints = ['One finger moves the pointer'];
  if (gestures.tap) hints.push('Tap or double-tap to click');
  if (gestures.scroll) hints.push('Two-finger scroll');
  if (gestures.secondary_tap) hints.push('Two-finger tap for right-click');
  if (gestures.pinch_zoom) hints.push('Pinch to zoom in supported apps');
  if (gestures.three_finger_drag) hints.push('Three-finger drag');
  if (gestures.swipes.length) hints.push('Three-finger swipes');
  for (const button of drawer.querySelectorAll('[data-swipe]')) {
    button.hidden = !gestures.swipes.includes(button.dataset.key);
  }
  const hint = hints.join(' · ');
  if (gestureHint.textContent !== hint) gestureHint.textContent = hint;
  scrollStrip.hidden = !gestures.scroll;
}

function showToast(message) {
  if (!message) return;
  toast.textContent = message;
  toast.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('show'), 1800);
}

function toggleDrawer(open, restoreFocus = true) {
  if (open && !ready) return;
  drawer.hidden = !open;
  drawerTrigger.setAttribute('aria-expanded', String(open));
  if (open) {
    drawer.querySelector('[data-drawer="close"]').focus();
  } else if (restoreFocus) {
    drawerTrigger.focus();
  }
}

function vibrate(ms = 12) {
  navigator.vibrate?.(ms);
}

function updateDrag(active) {
  dragLocked = active;
  dragBtn.classList.toggle('drag-locked', active);
  dragBtn.setAttribute('aria-pressed', String(active));
  dragLabel.textContent = active ? 'Release' : 'Hold';
}

function renderState(state, message) {
  ready = state === 'ready';
  statusDot.dataset.state = state;
  if (connectionStatus.textContent !== message) connectionStatus.textContent = message;
  disconnectBtn.hidden = !paired;
  remoteControls.hidden = !paired;
  remoteControls.inert = !ready;
  controlSettings.hidden = !ready;
  drawerTrigger.hidden = !ready;
  pairPanel.hidden = state !== 'pair' && state !== 'pending';
  pairForm.hidden = state === 'pending';
  pendingPanel.hidden = state !== 'pending';
  if (!ready) {
    pending.length = 0;
    accumDx = accumDy = accumScrollX = accumScrollY = 0;
    dragPending = false;
    motionStream = undefined;
    resetGesture();
    updateDrag(false);
    toggleDrawer(false, false);
  }
}

function showPairing(message = 'Access locked') {
  paired = false;
  const returning = remembered && !qrTicket && !manualPairing;
  const needsCode = !returning && !qrTicket;
  pairTitle.textContent = returning ? 'Reconnect' : 'Pair this browser';
  pairHelp.textContent = returning
    ? 'Request access, then approve it on your computer.'
    : qrTicket ? 'Approve this session on your computer after sending the request.'
      : 'Scan the QR code on your computer or enter the pairing code.';
  pairCodeFields.hidden = !needsCode;
  pairCode.required = needsCode;
  rememberFields.hidden = returning;
  enterCodeInstead.hidden = needsCode;
  renderState('pair', message);
}

function showPending(match) {
  paired = false;
  if (matchNumber.textContent !== match) matchNumber.textContent = match;
  renderState('pending', 'Waiting for approval');
}

function showOffline(message = 'Computer unreachable. Reconnecting…') {
  renderState('offline', message);
}

async function api(path, body, timeout = 7500) {
  const abort = new AbortController();
  const foregroundOnly = path === '/api/status' || path === '/api/cmd';
  if (foregroundOnly) foregroundRequests.add(abort);
  const timer = setTimeout(() => abort.abort(), timeout);
  try {
    const response = await fetch(path, {
      method: body === undefined ? 'GET' : 'POST',
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: 'same-origin',
      cache: 'no-store',
      signal: abort.signal
    });
    return { status: response.status, ...(await response.json()) };
  } finally {
    clearTimeout(timer);
    if (foregroundOnly) foregroundRequests.delete(abort);
  }
}

async function checkConnection() {
  if (updatingSession || checking || suspended || busy || motionInFlight || document.hidden) return;
  checking = true;
  const started = revision;
  try {
    let status = await api('/api/status');
    if (started !== revision || document.hidden) return;
    if (status.status === 401 && status.approved) {
      const claim = await api('/api/claim', {});
      if (claim.status !== 200) throw new Error('Approval expired');
      if (started !== revision || document.hidden) return;
      status = await api('/api/status');
    }
    if (started !== revision || document.hidden) return;
    if (status.status === 401) {
      releaseDragOnResume = false;
      remembered = status.remembered;
      if (status.match) {
        awaitingApproval = true;
        showPending(status.match);
      } else {
        if (awaitingApproval) pairError.textContent = 'Approval ended. Request access again.';
        awaitingApproval = false;
        showPairing();
      }
    } else if (status.status === 200) {
      awaitingApproval = false;
      manualPairing = false;
      paired = true;
      updateGestureSettings(status.gestures);
      renderState(status.ready ? 'ready' : 'permission', status.ready
        ? 'Connected securely'
        : 'Open Accessibility settings from Lazy Remote on the computer.');
      if (!busy && !motionInFlight) {
        updateDrag(status.dragging);
        if (releaseDragOnResume && status.ready && status.dragging) {
          enqueue([{ action: 'drag', active: false }]);
        }
      }
      releaseDragOnResume = false;
    } else {
      showOffline(status.error || 'Computer input is unavailable. Reconnecting…');
    }
  } catch {
    if (started === revision) showOffline();
  } finally {
    checking = false;
    if (started !== revision) checkConnection();
  }
}

pairSubmit.disabled = false;
pairCode.addEventListener('input', () => {
  pairError.textContent = '';
  pairCode.removeAttribute('aria-invalid');
});
enterCodeInstead.addEventListener('click', () => {
  qrTicket = null;
  manualPairing = true;
  pairError.textContent = '';
  showPairing();
  pairCode.focus();
});
pairForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (updatingSession) return;
  updatingSession = true;
  revision++;
  pairSubmit.disabled = true;
  pairSubmit.textContent = 'Sending request…';
  pairError.textContent = '';
  const returning = pairCodeFields.hidden && !qrTicket;
  const method = qrTicket ? { qr: qrTicket } : { code: pairCode.value };
  try {
    const response = await api(returning ? '/api/resume' : '/api/pair',
      returning ? {} : { ...method, remember: rememberBrowser.checked });
    if (response.status === 202) {
      qrTicket = null;
      pairCode.value = '';
      awaitingApproval = true;
      showPending('');
    } else {
      pairError.textContent = response.error || 'Request failed. Try again.';
      if (response.status === 403 && !qrTicket) pairCode.setAttribute('aria-invalid', 'true');
    }
  } catch {
    pairError.textContent = 'Could not reach the computer. Try again.';
  } finally {
    updatingSession = false;
    pairSubmit.disabled = false;
    pairSubmit.textContent = 'Request access';
    if (awaitingApproval) checkConnection();
  }
});

disconnectBtn.addEventListener('click', async () => {
  if (updatingSession) return;
  updatingSession = true;
  revision++;
  disconnectBtn.disabled = true;
  renderState('checking', 'Locking remote…');
  try {
    const response = await api('/api/disconnect', {});
    if (response.status === 200 || response.status === 401) {
      paired = false;
      renderState('checking', 'Access locked');
    } else {
      showOffline(response.error || 'Could not lock the remote.');
    }
  } catch {
    showOffline('Lock could not be confirmed. You can stop the server from the computer.');
  } finally {
    updatingSession = false;
    disconnectBtn.disabled = false;
    checkConnection();
  }
});

function suspendControls() {
  if (suspended) return;
  suspended = true;
  revision++;
  releaseDragOnResume ||= paired;
  // Keep a settled stream. Start fresh if a movement may have been lost on suspension.
  const settledStream = motionInFlight === 0 ? motionStream : undefined;
  for (const request of foregroundRequests) request.abort();
  renderState('checking', 'Checking connection…');
  motionStream = settledStream;
}

function resumeControls() {
  if (document.hidden) return;
  suspended = false;
  checkConnection();
}

function boundedDelta(value) {
  return Math.max(-16384, Math.min(16384, value));
}

function takeMotion() {
  if (Math.abs(accumDx) >= 0.4 || Math.abs(accumDy) >= 0.4) {
    const dx = boundedDelta(accumDx);
    const dy = boundedDelta(accumDy);
    accumDx = accumDy = 0;
    return { action: 'move', dx, dy };
  }
  if (Math.abs(accumScrollX) >= 1 || Math.abs(accumScrollY) >= 1) {
    const dx = boundedDelta(Math.round(accumScrollX));
    const dy = boundedDelta(Math.round(accumScrollY));
    accumScrollX = accumScrollY = 0;
    return { action: 'scroll', dx, dy };
  }
  return null;
}

function enqueue(commands, message = '') {
  if (!ready) return false;
  const releasingDrag = commands.length === 1 && commands[0].action === 'drag' && !commands[0].active;
  if (pending.length + commands.length > 16 && !releasingDrag) {
    showToast('Remote busy. Try again.');
    return false;
  }
  const motion = takeMotion();
  if (motion) pending.push({ command: motion });
  commands.forEach((command, index) => pending.push({
    command,
    message: index === commands.length - 1 ? message : ''
  }));
  return true;
}

async function sendCommand(command, message) {
  busy = true;
  const started = revision;
  try {
    const response = await api('/api/cmd', command);
    if (started !== revision) return;
    if (response.status === 200) {
      updateDrag(response.dragging);
      showToast(message);
    } else if (response.status === 401) {
      paired = false;
      renderState('checking', 'Access locked');
    } else if (response.status === 503) {
      showOffline(response.error);
    } else {
      pending.length = 0;
      dragPending = false;
      showToast(response.error || 'Command failed.');
    }
  } catch {
    if (started === revision) showOffline('Command could not be confirmed. Reconnecting…');
  } finally {
    if (command.action === 'drag') dragPending = false;
    busy = false;
    if (!paired || started !== revision) checkConnection();
  }
}

async function sendMotion(dx, dy) {
  if (!motionStream) {
    motionStream = Array.from(crypto.getRandomValues(new Uint8Array(16)),
      byte => byte.toString(16).padStart(2, '0')).join('');
    motionSequence = motionX = motionY = 0;
  }
  motionX += dx;
  motionY += dy;
  const started = revision;
  const stream = motionStream;
  motionInFlight++;
  try {
    const response = await api('/api/cmd', {
      action: 'motion', stream, seq: ++motionSequence, dx: motionX, dy: motionY
    }, 750);
    if (started !== revision || stream !== motionStream) return;
    if (response.status === 401) {
      paired = false;
      renderState('checking', 'Access locked');
    } else if (response.status === 503 || response.status === 409) {
      showOffline(response.error);
    }
  } catch {
    // The next sample includes this movement. Keep transient Wi-Fi delays off the status bar.
  } finally {
    motionInFlight--;
    if (!paired || started !== revision) checkConnection();
  }
}

function flushLoop(timestamp) {
  if (ready && !busy && !document.hidden) {
    if (pending.length) {
      if (motionInFlight === 0) {
        const next = pending.shift();
        sendCommand(next.command, next.message);
      }
    } else if (timestamp - lastMotionFrame >= 15) {
      const hasMovement = Math.abs(accumDx) >= 0.4 || Math.abs(accumDy) >= 0.4;
      if ((hasMovement && motionInFlight < 4) || (!hasMovement && motionInFlight === 0)) {
        const command = takeMotion();
        if (command) {
          lastMotionFrame = timestamp;
          if (command.action === 'move') sendMotion(command.dx, command.dy);
          else sendCommand(command);
        }
      }
    }
  }
  requestAnimationFrame(flushLoop);
}

function sendClick(button = 'left', count = 1, message = '') {
  if (enqueue([{ action: 'click', button, count }], message)) vibrate(15);
}

function sendKey(name, message = '') {
  if (enqueue([{ action: 'key', name }], message)) vibrate(15);
}

dragBtn.addEventListener('click', () => {
  if (!gestureDrag && !dragPending && enqueue([{ action: 'drag', active: !dragLocked }])) {
    dragPending = true;
    vibrate(20);
  }
});
document.querySelectorAll('[data-key]').forEach(button => {
  button.addEventListener('click', () => sendKey(button.dataset.key, button.dataset.message));
});
document.querySelectorAll('[data-click]').forEach(button => {
  button.addEventListener('click', () => sendClick(
    button.dataset.click, Number(button.dataset.count), button.dataset.message
  ));
});
drawerTrigger.addEventListener('click', () => toggleDrawer(true));
drawer.querySelector('[data-drawer="close"]').addEventListener('click', () => toggleDrawer(false));
drawer.addEventListener('click', event => {
  if (event.target === drawer) toggleDrawer(false);
});
document.addEventListener('keydown', event => {
  if (drawer.hidden) return;
  if (event.key === 'Escape') {
    event.preventDefault();
    toggleDrawer(false);
  } else if (event.key === 'Tab') {
    const buttons = drawer.querySelectorAll('button:not([hidden]):not(:disabled)');
    const first = buttons[0];
    const last = buttons[buttons.length - 1];
    if (event.shiftKey && event.target === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && event.target === last) {
      event.preventDefault();
      first.focus();
    }
  }
});

function submitText(enter = false) {
  const characters = Array.from(kbInput.value);
  if (characters.length > 1024) {
    showToast('Send up to 1,024 characters at a time.');
    return;
  }
  const commands = [];
  for (let offset = 0; offset < characters.length; offset += 128) {
    commands.push({ action: 'type', text: characters.slice(offset, offset + 128).join('') });
  }
  if (enter) commands.push({ action: 'key', name: 'enter' });
  if (commands.length && enqueue(commands, enter ? 'Enter sent' : '')) {
    kbInput.value = '';
    if (enter) kbInput.blur();
  }
}

kbInput.addEventListener('input', event => {
  if (!event.isComposing) submitText();
});
kbInput.addEventListener('compositionend', () => submitText());
kbInput.addEventListener('keydown', event => {
  if (event.isComposing) return;
  if (event.key === 'Backspace' && !kbInput.value) {
    event.preventDefault();
    sendKey('backspace');
  } else if (event.key === 'Enter') {
    event.preventDefault();
    submitText(true);
  }
});
document.getElementById('enterBtn').addEventListener('click', () => submitText(true));

document.addEventListener('visibilitychange', () => {
  if (document.hidden) suspendControls();
  else resumeControls();
});
window.addEventListener('pagehide', suspendControls);
window.addEventListener('pageshow', resumeControls);
window.addEventListener('online', () => checkConnection());
requestAnimationFrame(flushLoop);
checkConnection();
setInterval(checkConnection, 2000);

// Touchscreen, mouse, and pen input.
let touchGesture;
let lastTap;
let stripY;
let pointer;
let gestureDrag = false;

function addMovement(dx, dy, accelerate = true) {
  const distance = Math.hypot(dx, dy);
  const acceleration = accelerate ? Math.min(2.4, 1 + distance * 0.045) : 1;
  accumDx += dx * sensitivity * acceleration;
  accumDy += dy * sensitivity * acceleration;
}

function addScroll(dx, dy, touch = true) {
  if (touch && !gestures.scroll) return;
  const scale = sensitivity / 2;
  const direction = touch && !gestures.natural_scroll ? -1 : 1;
  if (!touch || gestures.horizontal_scroll) accumScrollX += dx * scale * direction;
  accumScrollY += dy * scale * direction;
}

function startGestureDrag() {
  if (!gestureDrag && !dragLocked && !dragPending &&
      enqueue([{ action: 'drag', active: true }])) {
    gestureDrag = true;
  }
}

function releaseGestureDrag() {
  if (gestureDrag) enqueue([{ action: 'drag', active: false }]);
  gestureDrag = false;
}

function resetGesture() {
  touchGesture = lastTap = stripY = pointer = undefined;
  gestureDrag = false;
  trackpad.classList.remove('active-touch');
}

function touchSample(touches) {
  const count = touches.length;
  let x = 0;
  let y = 0;
  for (const touch of touches) {
    x += touch.clientX;
    y += touch.clientY;
  }
  const span = count === 2
    ? Math.max(1, Math.hypot(touches[0].clientX - touches[1].clientX, touches[0].clientY - touches[1].clientY))
    : 1;
  return { x: x / count, y: y / count, count, span };
}

scrollStrip.addEventListener('touchstart', event => {
  event.stopPropagation();
  stripY = event.targetTouches[0]?.clientY;
}, { passive: false });

scrollStrip.addEventListener('touchmove', event => {
  event.preventDefault();
  event.stopPropagation();
  const y = event.targetTouches[0]?.clientY;
  if (ready && y !== undefined && stripY !== undefined) addScroll(0, (y - stripY) * 2.5);
  stripY = y;
}, { passive: false });

for (const event of ['touchend', 'touchcancel']) {
  scrollStrip.addEventListener(event, touch => {
    touch.stopPropagation();
    stripY = touch.targetTouches[0]?.clientY;
  });
}

trackpad.addEventListener('touchstart', event => {
  event.preventDefault();
  if (!ready || !event.targetTouches.length) return;
  const sample = touchSample(event.targetTouches);
  if (!touchGesture) {
    touchGesture = { last: sample, max: 0, started: Date.now(), moved: 0 };
  }
  touchGesture.last = sample;
  touchGesture.max = Math.max(touchGesture.max, sample.count);
  touchGesture.origin = sample;
  touchGesture.mode = undefined;
  touchGesture.zoom = 0;
  if (sample.count > 3) releaseGestureDrag();
  trackpad.classList.add('active-touch');
}, { passive: false });

trackpad.addEventListener('touchmove', event => {
  event.preventDefault();
  if (!ready || !touchGesture || !event.targetTouches.length) return;
  const sample = touchSample(event.targetTouches);
  const previous = touchGesture.last;
  touchGesture.last = sample;
  if (sample.count !== previous.count || sample.count !== touchGesture.max) return;

  const dx = sample.x - previous.x;
  const dy = sample.y - previous.y;
  const distance = Math.hypot(dx, dy);
  touchGesture.moved += Math.max(distance, Math.abs(sample.span - previous.span));
  if (sample.count === 1) {
    addMovement(dx, dy);
  } else if (sample.count === 2) {
    const zoom = Math.log(sample.span / previous.span);
    touchGesture.zoom += zoom;
    if (!touchGesture.mode) {
      const origin = touchGesture.origin;
      const pan = Math.hypot(sample.x - origin.x, sample.y - origin.y);
      const spread = Math.abs(sample.span - origin.span);
      if (gestures.pinch_zoom && spread >= 8 && spread > pan * 1.25) {
        touchGesture.mode = 'pinch';
      } else if (pan >= 5) {
        touchGesture.mode = 'scroll';
        addScroll((sample.x - origin.x) * 2.2, (sample.y - origin.y) * 2.2);
        return;
      }
    }
    if (touchGesture.mode === 'pinch' && Math.abs(touchGesture.zoom) >= 0.16) {
      sendKey(touchGesture.zoom > 0 ? 'zoom_in' : 'zoom_out');
      touchGesture.zoom = 0;
    } else if (touchGesture.mode === 'scroll') {
      addScroll(dx * 2.2, dy * 2.2);
    }
  } else if (sample.count === 3) {
    if (gestures.three_finger_drag) {
      startGestureDrag();
      if (gestureDrag || dragLocked) addMovement(dx, dy);
    } else if (!touchGesture.mode && !dragLocked && !dragPending) {
      const x = sample.x - touchGesture.origin.x;
      const y = sample.y - touchGesture.origin.y;
      const horizontal = Math.abs(x) >= 42 && Math.abs(x) > Math.abs(y) * 1.35;
      const vertical = Math.abs(y) >= 42 && Math.abs(y) > Math.abs(x) * 1.35;
      const swipe = horizontal ? (x < 0 ? 'swipe_left' : 'swipe_right')
        : vertical ? (y < 0 ? 'swipe_up' : 'swipe_down') : null;
      if (swipe && gestures.swipes.includes(swipe)) {
        touchGesture.mode = 'swipe';
        sendKey(swipe);
      }
    }
  }
}, { passive: false });

trackpad.addEventListener('touchend', event => {
  event.preventDefault();
  if (event.targetTouches.length < 3) releaseGestureDrag();
  if (event.targetTouches.length) return;
  trackpad.classList.remove('active-touch');
  const touch = touchGesture;
  touchGesture = undefined;
  if (!ready || !touch || touch.mode || touch.moved >= 12 || Date.now() - touch.started >= 280) return;

  if (touch.max === 1 && gestures.tap) {
    const now = Date.now();
    const { x, y } = touch.last;
    const double = lastTap && now - lastTap.time < 300 && Math.hypot(x - lastTap.x, y - lastTap.y) < 24;
    const command = { action: 'click', button: 'left', count: 1 };
    if (double) command.continuation = true;
    if (enqueue([command])) {
      vibrate(15);
      lastTap = double ? undefined : { x, y, time: now };
    }
  } else if (touch.max === 2 && gestures.secondary_tap) {
    sendClick('right', 1, 'Right Click');
  }
}, { passive: false });

trackpad.addEventListener('touchcancel', () => {
  releaseGestureDrag();
  resetGesture();
});

trackpad.addEventListener('pointerdown', event => {
  if (!ready || event.pointerType === 'touch' || event.target === scrollStrip ||
      (event.button !== 0 && event.button !== 2)) return;
  event.preventDefault();
  pointer = { id: event.pointerId, x: event.clientX, y: event.clientY, button: event.button, moved: false };
  trackpad.setPointerCapture(event.pointerId);
});

trackpad.addEventListener('pointermove', event => {
  if (!ready || event.pointerType === 'touch' || event.target === scrollStrip) return;
  if (!pointer || pointer.id !== event.pointerId) {
    pointer = { id: event.pointerId, x: event.clientX, y: event.clientY };
    return;
  }
  const dx = event.clientX - pointer.x;
  const dy = event.clientY - pointer.y;
  pointer.x = event.clientX;
  pointer.y = event.clientY;
  if (dx === 0 && dy === 0) return;
  if (pointer.button === 0) {
    pointer.moved = true;
    startGestureDrag();
  }
  addMovement(dx, dy, false);
});

trackpad.addEventListener('pointerup', event => {
  if (event.pointerType === 'touch' || pointer?.id !== event.pointerId) return;
  if (!pointer.moved && !dragLocked && pointer.button !== undefined) {
    sendClick(pointer.button === 2 ? 'right' : 'left');
  }
  releaseGestureDrag();
  pointer = undefined;
});

for (const event of ['pointercancel', 'lostpointercapture', 'pointerleave']) {
  trackpad.addEventListener(event, pointerEvent => {
    if (pointerEvent.pointerType === 'touch' || pointer?.id !== pointerEvent.pointerId) return;
    releaseGestureDrag();
    pointer = undefined;
  });
}

trackpad.addEventListener('contextmenu', event => event.preventDefault());
trackpad.addEventListener('wheel', event => {
  if (!ready || event.ctrlKey) return;
  event.preventDefault();
  const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? trackpad.clientHeight : 1;
  addScroll(-event.deltaX * unit, -event.deltaY * unit, false);
}, { passive: false });
