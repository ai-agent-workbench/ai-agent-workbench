(function () {
  const composer = document.querySelector('[data-draft-key]');
  if (!composer) return;
  const key = composer.dataset.draftKey;
  const saved = localStorage.getItem(key);
  if (saved) composer.value = saved;
  composer.addEventListener('input', function () {
    if (composer.value) {
      localStorage.setItem(key, composer.value);
    } else {
      localStorage.removeItem(key);
    }
  });
  const form = document.getElementById('thread-send');
  if (form) {
    form.addEventListener('submit', function () { localStorage.removeItem(key); });
  }
})();
