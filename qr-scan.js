(() => {
  const opener = document.getElementById('scanPairing');
  const dialog = document.getElementById('scanDialog');
  const video = document.getElementById('scanVideo');
  const status = document.getElementById('scanStatus');
  const retry = document.getElementById('startCamera');
  const photo = document.getElementById('qrPhoto');
  const canvas = document.createElement('canvas');
  const context = canvas.getContext('2d', { willReadFrequently: true });
  let decoder;
  let stream;
  let frame;
  let generation = 0;

  function showStatus(message) {
    if (status.textContent !== message) status.textContent = message;
  }

  function loadDecoder() {
    if (decoder) return decoder;
    decoder = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = '/vendor/jsQR.js';
      script.onload = () => typeof window.jsQR === 'function' ? resolve(window.jsQR) : reject();
      script.onerror = reject;
      document.head.append(script);
    }).catch(() => {
      decoder = undefined;
      throw new Error('Could not load the QR scanner. Try again or enter the pairing code.');
    });
    return decoder;
  }

  function stopCamera() {
    generation++;
    cancelAnimationFrame(frame);
    frame = undefined;
    stream?.getTracks().forEach(track => track.stop());
    stream = undefined;
    video.srcObject = null;
    video.hidden = true;
    retry.hidden = !navigator.mediaDevices?.getUserMedia;
  }

  function decodeImage(decode, image, width, height, maxSize) {
    const scale = Math.min(1, maxSize / Math.max(width, height));
    const w = Math.max(1, Math.round(width * scale));
    const h = Math.max(1, Math.round(height * scale));
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
    context.drawImage(image, 0, 0, w, h);
    return decode(context.getImageData(0, 0, w, h).data, w, h, { inversionAttempts: 'dontInvert' });
  }

  function useResult(result) {
    if (!result) return false;
    try {
      acceptScannedPairingLink(result.data);
    } catch (error) {
      showStatus(error.message);
      return false;
    }
    stopCamera();
    dialog.close();
    document.getElementById('pairSubmit').focus();
    return true;
  }

  async function startCamera() {
    stopCamera();
    if (!navigator.mediaDevices?.getUserMedia) {
      showStatus('Live camera is unavailable. Take a QR photo or enter the pairing code.');
      return;
    }
    const request = generation;
    retry.hidden = true;
    showStatus('Allow camera access to scan the QR shown on your computer.');
    try {
      const camera = await navigator.mediaDevices.getUserMedia({
        audio: false, video: { facingMode: { ideal: 'environment' }, width: { ideal: 640 } }
      });
      if (request !== generation || !dialog.open) {
        camera.getTracks().forEach(track => track.stop());
        return;
      }
      if (document.hidden) {
        camera.getTracks().forEach(track => track.stop());
        stopCamera();
        showStatus('Camera paused. Start it again to scan.');
        return;
      }
      stream = camera;
      video.srcObject = camera;
      video.hidden = false;
      await video.play();
      const decode = await loadDecoder();
      if (request !== generation) return;
      showStatus('Point the camera at the pairing QR on your computer.');
      let lastScan = -Infinity;
      function scan(time) {
        if (request !== generation) return;
        if (time - lastScan >= 135 && video.readyState >= 2 && video.videoWidth) {
          lastScan = time;
          try {
            if (useResult(decodeImage(decode, video, video.videoWidth, video.videoHeight, 640))) return;
          } catch {
            stopCamera();
            showStatus('Could not read the camera. Try again or take a QR photo.');
            return;
          }
        }
        frame = requestAnimationFrame(scan);
      }
      frame = requestAnimationFrame(scan);
    } catch {
      if (request !== generation) return;
      stopCamera();
      showStatus('Could not open the camera. Try again, take a QR photo, or enter the pairing code.');
    }
  }

  opener.addEventListener('click', () => {
    if (!dialog.showModal || !context) return;
    dialog.showModal();
    startCamera();
  });
  if (!dialog.showModal || !context) opener.hidden = true;
  retry.addEventListener('click', startCamera);
  dialog.addEventListener('close', stopCamera);
  window.addEventListener('pagehide', stopCamera);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden && stream) {
      stopCamera();
      showStatus('Camera paused. Start it again to scan.');
    }
  });

  document.getElementById('takeQrPhoto').addEventListener('click', () => {
    stopCamera();
    photo.click();
  });
  photo.addEventListener('change', async () => {
    const file = photo.files[0];
    photo.value = '';
    if (!file) return;
    const request = generation;
    const url = URL.createObjectURL(file);
    try {
      showStatus('Reading QR photo…');
      const image = new Image();
      image.src = url;
      await image.decode();
      const decode = await loadDecoder();
      if (request !== generation || !dialog.open) return;
      const result = decodeImage(decode, image, image.naturalWidth, image.naturalHeight, 1600);
      if (!result) showStatus('No QR found. Try taking a closer photo or enter the pairing code.');
      else useResult(result);
    } catch {
      if (request === generation && dialog.open) showStatus('Could not read the photo. Try again or enter the pairing code.');
    } finally {
      URL.revokeObjectURL(url);
    }
  });
})();
