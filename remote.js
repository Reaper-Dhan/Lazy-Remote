const trackpad = document.getElementById('trackpad');
const scrollStrip = document.getElementById('scrollStrip');
const speedSlider = document.getElementById('speedSlider');
const speedValue = document.getElementById('speedValue');
const gestureHint = document.getElementById('gestureHint');
const kbInput = document.getElementById('kbInput');
const sendTextBtn = document.getElementById('sendTextBtn');
const typingNotice = document.getElementById('typingNotice');
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
const scanPairing = document.getElementById('scanPairing');
const remoteControls = document.getElementById('remoteControls');
const remoteWorkspace = document.getElementById('remoteWorkspace');
const remoteDock = document.getElementById('remoteDock');
const header = document.querySelector('.header');
const trackpadView = document.getElementById('trackpadView');
const mediaView = document.getElementById('mediaView');
const viewButtons = document.querySelectorAll('[data-view]');
const connectionPanel = document.getElementById('connectionPanel');
const connectionTitle = document.getElementById('connectionTitle');
const connectionHelp = document.getElementById('connectionHelp');
const retryConnection = document.getElementById('retryConnection');
const controlSettings = document.getElementById('controlSettings');
const disconnectBtn = document.getElementById('disconnectBtn');
const drawer = document.getElementById('drawerBackdrop');
const drawerTrigger = document.querySelector('[data-drawer="open"]');
const swipeModeBtn = document.getElementById('swipeModeBtn');
const trackpadModeTitle = document.getElementById('trackpadModeTitle');

let sensitivity = Number(speedSlider.value);
let macSwipeMode = false;
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
let pairingOrigins = new Set([location.origin]);
if (qrValue !== null) history.replaceState(null, '', location.pathname + location.search);

let paired = false;
let tab = null;
let protectBackground = true;
let remembered = false;
let awaitingApproval = false;
let manualPairing = false;
let ready = false;
let updatingSession = false;
let checking = false;
let statusFailures = 0;
let dragRevision = 0;
let suspended = false;
let releaseDragOnResume = false;
let revision = 0;
let dragLocked = false;
let dragPending = false;
let busy = false;
let motionInFlight = 0;
let motionSequence = 0;
let motionX = 0;
let motionY = 0;
let lastMotionFrame = -Infinity;
let accumDx = 0;
let accumDy = 0;
let accumScrollX = 0;
let accumScrollY = 0;
let toastTimer;
let sendingText = null;
let textNeedsReview = false;
const pending = [];
const foregroundRequests = new Set();
const gestures = {
  scroll: true, horizontal_scroll: true,
  natural_scroll: true, pinch_zoom: false, three_finger_drag: false, swipes: [], swipe_fingers: {}
};

const swipeLabels = {
  swipe_up: '↑ Mission Control',
  swipe_down: '↓ App Windows',
  swipe_left: '← Next Space',
  swipe_right: '→ Previous Space',
};

function updateGestureSettings(settings) {
  Object.assign(gestures, settings);
  if (!gestures.swipes.length) macSwipeMode = false;
  const hints = ['Tap to click', 'Two fingers to right-click'];
  if (gestures.scroll) hints.push('Two-finger scroll');
  if (gestures.pinch_zoom) hints.push('Pinch to zoom');
  if (gestures.three_finger_drag) hints.push('Three-finger drag');
  const swipeCounts = new Set(gestures.swipes.flatMap(swipe => gestures.swipe_fingers[swipe] || [3]));
  if (swipeCounts.has(3)) hints.push('Three-finger Mac swipes');
  else if (swipeCounts.has(4)) hints.push('Four-finger Mac swipes');
  for (const button of drawer.querySelectorAll('[data-swipe]')) {
    button.hidden = !gestures.swipes.includes(button.dataset.key);
  }
  const hint = (macSwipeMode ? gestures.swipes.map(swipe => swipeLabels[swipe]) : hints).join(' · ');
  if (gestureHint.textContent !== hint) gestureHint.textContent = hint;
  const title = macSwipeMode ? 'Swipe with one finger' : 'Move to point';
  if (trackpadModeTitle.textContent !== title) trackpadModeTitle.textContent = title;
  trackpad.classList.toggle('swipe-active', macSwipeMode);
  scrollStrip.hidden = !gestures.scroll;
  swipeModeBtn.hidden = gestures.swipes.length === 0;
  swipeModeBtn.setAttribute('aria-pressed', String(macSwipeMode));
}

function updateAvailableKeys(unavailable) {
  // Swipe buttons follow the Mac's gesture settings in updateGestureSettings.
  for (const button of document.querySelectorAll('[data-key]:not([data-swipe])')) {
    button.hidden = unavailable.includes(button.dataset.key);
  }
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
  remoteWorkspace.inert = remoteDock.inert = header.inert = open;
  if (open) {
    drawer.querySelector('[data-drawer="close"]').focus();
  } else if (restoreFocus) {
    drawerTrigger.focus();
  }
}

function showView(view) {
  const media = view === 'media';
  if (media && !trackpadView.hidden) {
    if (gestureDrag) releaseGestureDrag();
    else if (dragLocked || dragPending) enqueue([{ action: 'drag', active: false }]);
    resetGesture();
  }
  trackpadView.hidden = media;
  mediaView.hidden = !media;
  remoteWorkspace.scrollTop = 0;
  for (const button of viewButtons) {
    button.setAttribute('aria-pressed', String(button.dataset.view === view));
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
  remoteControls.hidden = !ready;
  remoteControls.inert = !ready;
  controlSettings.hidden = !ready;
  drawerTrigger.hidden = !ready;
  pairPanel.hidden = state !== 'pair' && state !== 'pending';
  connectionPanel.hidden = ready || !pairPanel.hidden;
  if (!connectionPanel.hidden) {
    connectionTitle.textContent = state === 'offline' ? 'Computer unavailable'
      : state === 'permission' ? 'Input access needed' : 'Connecting to your computer';
    connectionHelp.textContent = state === 'offline' ? 'Check that both devices are on the same network and the server is running.'
      : state === 'permission' ? message : 'Keep Lazy Remote open on your computer.';
    retryConnection.hidden = state === 'checking';
  }
  pairForm.hidden = state === 'pending';
  pendingPanel.hidden = state !== 'pending';
  if (!ready) {
    preserveText();
    pending.length = 0;
    accumDx = accumDy = accumScrollX = accumScrollY = 0;
    dragPending = false;
    resetGesture();
    updateDrag(false);
    macSwipeMode = false;
    toggleDrawer(false, false);
  }
}

function clearTab() {
  tab = null;
  motionSequence = motionX = motionY = 0;
}

function showPairing(message = 'Access locked') {
  paired = false;
  const returning = remembered && !qrTicket && !manualPairing;
  const needsCode = !returning && !qrTicket;
  pairTitle.textContent = returning ? 'Reconnect' : 'Pair this browser';
  pairHelp.textContent = returning
    ? 'Request access, then approve it on your computer.'
    : qrTicket ? 'Pairing QR ready. Request access, then approve it on your computer.'
      : 'Scan the QR code on your computer or enter the pairing code.';
  pairCodeFields.hidden = !needsCode;
  pairCode.required = needsCode;
  rememberFields.hidden = returning;
  enterCodeInstead.hidden = needsCode;
  renderState('pair', qrTicket ? 'QR code ready' : message);
}

function acceptScannedPairingLink(text) {
  if (paired || awaitingApproval || updatingSession) throw new Error('Finish the current request first.');
  let link;
  try {
    link = new URL(text);
  } catch {
    throw new Error('This is not a Lazy Remote pairing QR.');
  }
  const ticket = /^#pair=([A-Za-z0-9_-]{43})$/.exec(link.hash);
  if (link.protocol !== 'https:' || link.username || link.password || link.pathname !== '/' || link.search || !ticket) {
    throw new Error('This is not a Lazy Remote pairing QR.');
  }
  if (!pairingOrigins.has(link.origin)) {
    throw new Error('This QR is for another address. Open the address shown on your computer, then scan again.');
  }
  qrTicket = ticket[1];
  manualPairing = false;
  pairError.textContent = '';
  showPairing();
}

function showPending(match) {
  paired = false;
  if (matchNumber.textContent !== match) matchNumber.textContent = match;
  renderState('pending', 'Waiting for approval');
}

function showOffline(message = 'Computer unreachable. Reconnecting…') {
  renderState('offline', message);
}

function sessionHeaders(body, sessionTab) {
  const headers = body === undefined ? {} : { 'Content-Type': 'application/json' };
  if (sessionTab) headers['X-Lazy-Remote-Tab'] = sessionTab;
  return headers;
}

function lockHiddenTab(sessionTab) {
  if (!sessionTab) return;
  // Keep this small so the browser can finish sending it after the page is hidden.
  fetch('/api/disconnect', {
    method: 'POST', body: '{}', headers: sessionHeaders({}, sessionTab),
    credentials: 'same-origin', cache: 'no-store', keepalive: true
  }).catch(() => {});
}

async function api(path, body, timeout = 7500) {
  const abort = new AbortController();
  const foregroundOnly = path === '/api/status' || path === '/api/cmd';
  if (foregroundOnly) foregroundRequests.add(abort);
  const timer = setTimeout(() => abort.abort(), timeout);
  try {
    const response = await fetch(path, {
      method: body === undefined ? 'GET' : 'POST',
      headers: sessionHeaders(body, tab),
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
  if (updatingSession || checking || suspended || document.hidden) return;
  checking = true;
  const started = revision;
  const dragAtStart = dragRevision;
  try {
    let status = await api('/api/status');
    if (started !== revision || document.hidden) return;
    if (status.status === 401 && status.approved) {
      const claim = await api('/api/claim', {});
      if (claim.status === 401 || claim.status === 409) {
        if (started !== revision || document.hidden) return;
        remembered = status.remembered;
        awaitingApproval = false;
        showPairing();
        pairError.textContent = claim.error || 'Approval ended. Request access again.';
        return;
      }
      if (claim.status !== 200) throw new Error('Approval request failed');
      if (started !== revision || document.hidden) {
        lockHiddenTab(claim.tab);
        return;
      }
      tab = claim.tab;
      protectBackground = claim.protect_background !== false;
      status = await api('/api/status');
    }
    if (started !== revision || document.hidden) return;
    statusFailures = 0;
    if (status.status === 401) {
      clearTab();
      releaseDragOnResume = false;
      remembered = status.remembered;
      pairingOrigins = new Set([location.origin, ...(status.pairing_origins || [])]);
      if (status.match) {
        awaitingApproval = true;
        showPending(status.match);
      } else {
        if (awaitingApproval) pairError.textContent = 'Approval ended. Request access again.';
        awaitingApproval = false;
        showPairing();
        if (status.new_pairing_required && !qrTicket) {
          pairError.textContent = 'This page was reopened. On the computer, click Lock phone to get a fresh pairing QR or code.';
        }
      }
    } else if (status.status === 200) {
      awaitingApproval = false;
      manualPairing = false;
      paired = true;
      protectBackground = status.protect_background !== false;
      updateGestureSettings(status.gestures);
      updateAvailableKeys(status.unavailable || []);
      renderState(status.ready ? 'ready' : 'permission', status.ready
        ? 'Connected securely'
        : 'Allow mouse and keyboard control in Lazy Remote on the computer.');
      if (!busy && !motionInFlight && dragAtStart === dragRevision) {
        updateDrag(status.dragging);
        if (releaseDragOnResume && status.ready && status.dragging) {
          enqueue([{ action: 'drag', active: false }]);
        }
      }
      if (releaseDragOnResume && status.ready && !status.dragging) releaseDragOnResume = false;
    } else {
      showOffline(status.error || 'Computer input is unavailable. Reconnecting…');
    }
  } catch {
    if (started === revision && (!ready || ++statusFailures >= 2)) showOffline();
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
  scanPairing.disabled = true;
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
    scanPairing.disabled = false;
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
      clearTab();
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
  if (protectBackground) {
    lockHiddenTab(tab);
    clearTab();
    paired = false;
  } else {
    releaseDragOnResume ||= paired;
  }
  statusFailures = 0;
  for (const request of foregroundRequests) request.abort();
  renderState('checking', 'Checking connection…');
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
    accumScrollX -= dx;
    accumScrollY -= dy;
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
  drainCommands();
  return true;
}

function drainCommands() {
  if (!ready || busy || motionInFlight || document.hidden || !pending.length) return;
  const next = pending.shift();
  sendCommand(next.command, next.message);
}

async function sendCommand(command, message) {
  busy = true;
  dragRevision++;
  if (command.action === 'type') sendingText = command.text;
  const started = revision;
  try {
    const response = await api('/api/cmd', command);
    if (started !== revision) return;
    if (response.status === 200) {
      if (command.action === 'type') sendingText = null;
      if (command.action === 'drag' && !command.active && !response.dragging) releaseDragOnResume = false;
      updateDrag(response.dragging);
      showToast(message);
    } else if (response.status === 401) {
      revision++;
      clearTab();
      paired = false;
      renderState('checking', 'Access locked');
    } else if (response.status === 503) {
      showOffline(response.error);
    } else {
      if (command.action === 'type') {
        preserveText(false);
        pending.length = 0;
      }
      showToast(response.error || 'Command failed.');
    }
  } catch {
    if (started === revision) showOffline('Command could not be confirmed. Reconnecting…');
  } finally {
    dragRevision++;
    if (command.action === 'type') sendingText = null;
    if (command.action === 'drag') dragPending = false;
    busy = false;
    if (!paired || started !== revision) checkConnection();
    else drainCommands();
  }
}

async function sendMotion(dx, dy) {
  motionX += dx;
  motionY += dy;
  const started = revision;
  motionInFlight++;
  try {
    const response = await api('/api/cmd', {
      action: 'motion', seq: ++motionSequence, dx: motionX, dy: motionY
    }, 750);
    if (started !== revision) return;
    if (response.status === 401) {
      revision++;
      clearTab();
      paired = false;
      renderState('checking', 'Access locked');
    } else if (response.status === 503) {
      showOffline(response.error);
    }
  } catch {
    // The next sample includes this movement. Keep transient Wi-Fi delays off the status bar.
  } finally {
    motionInFlight--;
    if (!paired || started !== revision) checkConnection();
    else drainCommands();
  }
}

function flushLoop(timestamp) {
  if (ready && !busy && !document.hidden) {
    if (pending.length) {
      drainCommands();
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
  if (!enqueue([{ action: 'key', name }], message)) return false;
  vibrate(15);
  return true;
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
for (const button of viewButtons) button.addEventListener('click', () => showView(button.dataset.view));
retryConnection.addEventListener('click', checkConnection);
swipeModeBtn.addEventListener('click', () => {
  if (!ready || !gestures.swipes.length) return;
  if (gestureDrag) releaseGestureDrag();
  else if (dragLocked || dragPending) enqueue([{ action: 'drag', active: false }]);
  resetGesture();
  macSwipeMode = !macSwipeMode;
  updateGestureSettings();
});
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

function preserveText(uncertain = true) {
  const queued = pending.filter(item => item.command.action === 'type').map(item => item.command.text).join('');
  if (!sendingText && !queued) return;
  const interrupted = sendingText;
  kbInput.value = (interrupted || '') + queued + kbInput.value;
  sendingText = null;
  textNeedsReview = true;
  sendTextBtn.hidden = false;
  typingNotice.hidden = false;
  typingNotice.textContent = interrupted && uncertain
    ? 'Check the computer before resending. Some of this text may already be there.'
    : 'Unsent text is saved here. Tap Send text when ready.';
}

function submitText(enter = false, confirmed = false) {
  if (textNeedsReview && !confirmed) return;
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
  if (commands.length && !enqueue(commands, enter ? 'Enter sent' : '')) return;
  if (commands.length || confirmed) {
    kbInput.value = '';
    textNeedsReview = false;
    sendTextBtn.hidden = typingNotice.hidden = true;
    if (enter) kbInput.blur();
  }
}

kbInput.addEventListener('input', event => {
  if (!event.isComposing) submitText();
});
kbInput.addEventListener('compositionend', () => submitText());
kbInput.addEventListener('keydown', event => {
  if (event.isComposing || textNeedsReview) return;
  if (event.key === 'Backspace' && !kbInput.value) {
    event.preventDefault();
    sendKey('backspace');
  } else if (event.key === 'Enter') {
    event.preventDefault();
    submitText(true);
  }
});
sendTextBtn.addEventListener('click', () => submitText(false, true));
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
const trackpadContacts = new Map();
const TAP_SLOP = 12;
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
  trackpadContacts.clear();
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

function activeTrackpadTouches(event) {
  return Array.from(event.touches).filter(touch => trackpadContacts.has(touch.identifier));
}

function recordTapMovement(touches) {
  for (const touch of touches) {
    const start = trackpadContacts.get(touch.identifier);
    if (start && Math.hypot(touch.clientX - start.x, touch.clientY - start.y) >= TAP_SLOP) {
      touchGesture.tapMoved = true;
    }
  }
}

function recognizeMacSwipe(gesture, count, x, y) {
  if (gesture.mode || dragLocked || dragPending) return;
  const horizontal = Math.abs(x) >= 32 && Math.abs(x) > Math.abs(y) * 1.2;
  const vertical = Math.abs(y) >= 32 && Math.abs(y) > Math.abs(x) * 1.2;
  const swipe = horizontal ? (x < 0 ? 'swipe_left' : 'swipe_right')
    : vertical ? (y < 0 ? 'swipe_up' : 'swipe_down') : null;
  if (!swipe || !gestures.swipes.includes(swipe)) return;
  const fingers = gestures.swipe_fingers[swipe] || [3];
  if (((count === 1 && macSwipeMode) || fingers.includes(count)) &&
      sendKey(swipe, swipeLabels[swipe].slice(2))) {
    gesture.mode = 'swipe';
  }
}

trackpad.addEventListener('touchstart', event => {
  event.preventDefault();
  if (!ready) return;
  const touchesAdded = Array.from(event.changedTouches).filter(touch =>
    trackpad.contains(touch.target) && !scrollStrip.contains(touch.target));
  if (!touchesAdded.length) return;
  if (document.activeElement === kbInput) kbInput.blur();
  for (const touch of touchesAdded) {
    trackpadContacts.set(touch.identifier, { x: touch.clientX, y: touch.clientY });
  }
  const touches = activeTrackpadTouches(event);
  if (!touches.length) return;
  const sample = touchSample(touches);
  if (!touchGesture) {
    touchGesture = { max: 0, started: Date.now(), tapMoved: false };
  }
  recordTapMovement(touches);
  if (touchGesture.mode) {
    // Once committed, a replaced finger must not restart the same swipe or drag.
    touchGesture.finished = true;
    releaseGestureDrag();
  }
  touchGesture.last = sample;
  touchGesture.max = Math.max(touchGesture.max, sample.count);
  touchGesture.origin = sample;
  trackpad.classList.add('active-touch');
}, { passive: false });

function updateTouchGesture(touches) {
  const gesture = touchGesture;
  if (!ready || !gesture || !touches.length) return;
  recordTapMovement(touches);
  if (gesture.finished || touches.length !== gesture.max) return;

  const sample = touchSample(touches);
  const previous = gesture.last;
  gesture.last = sample;
  const dx = sample.x - previous.x;
  const dy = sample.y - previous.y;
  const panX = sample.x - gesture.origin.x;
  const panY = sample.y - gesture.origin.y;
  const pan = Math.hypot(panX, panY);
  if (sample.count === 1) {
    if (macSwipeMode) {
      recognizeMacSwipe(gesture, sample.count, panX, panY);
    } else if (gesture.mode === 'move') {
      addMovement(dx, dy);
    } else if (pan >= TAP_SLOP) {
      gesture.mode = 'move';
      addMovement(panX, panY);
    }
  } else if (sample.count === 2) {
    const spread = sample.span - gesture.origin.span;
    if (!gesture.mode) {
      if (gestures.pinch_zoom && Math.abs(spread) >= Math.max(12, gesture.origin.span * 0.08) &&
          Math.abs(spread) > pan * 1.25) {
        gesture.mode = 'pinch';
        gesture.zoomAnchor = sample.span;
        sendKey(spread > 0 ? 'zoom_in' : 'zoom_out');
        return;
      }
      const scrollPan = gestures.horizontal_scroll ? pan : Math.abs(panY);
      if (gestures.scroll && scrollPan >= TAP_SLOP) {
        gesture.mode = 'scroll';
        addScroll(panX * 2.2, panY * 2.2);
        return;
      }
    }
    if (gesture.mode === 'pinch') {
      const zoom = Math.log(sample.span / gesture.zoomAnchor);
      if (Math.abs(zoom) >= 0.16 && sendKey(zoom > 0 ? 'zoom_in' : 'zoom_out')) {
        gesture.zoomAnchor = sample.span;
      }
    } else if (gesture.mode === 'scroll') {
      addScroll(dx * 2.2, dy * 2.2);
    }
  } else if (sample.count === 3 && gestures.three_finger_drag) {
    if (!gesture.mode && pan >= TAP_SLOP) {
      startGestureDrag();
      if (gestureDrag || dragLocked) {
        gesture.mode = 'drag';
        addMovement(panX, panY);
      }
    } else if (gesture.mode === 'drag') {
      addMovement(dx, dy);
    }
  } else if (sample.count === 3 || sample.count === 4) {
    recognizeMacSwipe(gesture, sample.count, panX, panY);
  }
}

trackpad.addEventListener('touchmove', event => {
  event.preventDefault();
  updateTouchGesture(activeTrackpadTouches(event));
}, { passive: false });

trackpad.addEventListener('touchend', event => {
  event.preventDefault();
  if (!touchGesture) return;
  const remaining = activeTrackpadTouches(event);
  const ended = Array.from(event.changedTouches).filter(touch => trackpadContacts.has(touch.identifier));
  // A fast gesture can cross its threshold on the lift, with no final touchmove.
  updateTouchGesture([...remaining, ...ended]);
  for (const touch of ended) trackpadContacts.delete(touch.identifier);
  if (remaining.length < 3) releaseGestureDrag();
  if (remaining.length) return;
  trackpad.classList.remove('active-touch');
  const touch = touchGesture;
  touchGesture = undefined;
  trackpadContacts.clear();
  if (!ready || touch.mode || touch.tapMoved || Date.now() - touch.started > 420) {
    lastTap = undefined;
    return;
  }

  if (touch.max === 1 && !macSwipeMode) {
    if (dragLocked || dragPending) {
      enqueue([{ action: 'drag', active: false }]);
      lastTap = undefined;
      return;
    }
    const now = Date.now();
    const { x, y } = touch.last;
    const double = lastTap && now - lastTap.time < 300 && Math.hypot(x - lastTap.x, y - lastTap.y) < 24;
    const command = { action: 'click', button: 'left', count: 1 };
    if (double) command.continuation = true;
    if (enqueue([command])) {
      vibrate(15);
      lastTap = double ? undefined : { x, y, time: now };
    }
  } else if (touch.max === 2) {
    lastTap = undefined;
    sendClick('right', 1, 'Right click');
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
  if (macSwipeMode) {
    if (pointer.button === 0) recognizeMacSwipe(pointer, 1, dx, dy);
    return;
  }
  if (dx === 0 && dy === 0) return;
  if (pointer.button === 0 && !pointer.moved && Math.hypot(dx, dy) < 4) return;
  pointer.x = event.clientX;
  pointer.y = event.clientY;
  if (pointer.button === 0) {
    pointer.moved = true;
    startGestureDrag();
  }
  addMovement(dx, dy, false);
});

trackpad.addEventListener('pointerup', event => {
  if (event.pointerType === 'touch' || pointer?.id !== event.pointerId) return;
  if (macSwipeMode && pointer.button === 0) {
    recognizeMacSwipe(pointer, 1, event.clientX - pointer.x, event.clientY - pointer.y);
  } else if (!pointer.moved && !dragLocked && pointer.button !== undefined) {
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
