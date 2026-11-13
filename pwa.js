const installApp = document.getElementById('installApp');
const installHelp = document.getElementById('installHelp');
const standalone = window.matchMedia('(display-mode: standalone)');
let installPrompt;
let installed = false;

function updateInstallButton() {
  installApp.hidden = installed || standalone.matches || navigator.standalone === true;
}

window.addEventListener('beforeinstallprompt', event => {
  event.preventDefault();
  installPrompt = event;
});
window.addEventListener('appinstalled', () => {
  installed = true;
  installPrompt = undefined;
  updateInstallButton();
});
standalone.addEventListener?.('change', updateInstallButton);
updateInstallButton();

installApp.addEventListener('click', async () => {
  if (installPrompt) {
    const prompt = installPrompt;
    installPrompt = undefined;
    try {
      const choice = await prompt.prompt();
      if (choice.outcome === 'accepted') {
        installed = true;
        updateInstallButton();
      }
      return;
    } catch {
      // Fall back to the browser menu instructions.
    }
  }
  if (typeof installHelp.showModal === 'function') installHelp.showModal();
});

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/service-worker.js').catch(() => {
    // The remote remains usable in browsers that do not allow a service worker.
  });
}
