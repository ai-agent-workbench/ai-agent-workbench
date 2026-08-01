(function () {
  const composer = document.querySelector("textarea[name='prompt']");
  const messages = [...document.querySelectorAll('[data-message]')];
  const closeMenus = function (except) {
    messages.forEach(function (item) {
      if (item !== except) item.querySelector('.message-menu').classList.add('hidden');
    });
  };
  const copyText = async function (text) {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return;
    }
    const area = document.createElement('textarea');
    area.value = text;
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    document.execCommand('copy');
    area.remove();
  };
  messages.forEach(function (message) {
    const body = message.querySelector('.message-body');
    const menu = message.querySelector('.message-menu');
    const toggle = message.querySelector('[data-toggle]');
    const menuButton = message.querySelector('[data-menu]');
    const setCollapsed = function (collapsed) {
      body.classList.toggle('collapsed', collapsed);
      if (toggle) toggle.textContent = collapsed ? '展开' : '折叠';
      menu.classList.add('hidden');
    };
    menuButton.addEventListener('click', function (event) {
      event.stopPropagation();
      const opening = menu.classList.contains('hidden');
      closeMenus(message);
      menu.classList.toggle('hidden', !opening);
    });
    message.querySelector('[data-copy]').addEventListener('click', function () {
      copyText(body.textContent).catch(function () {});
      menu.classList.add('hidden');
    });
    message.querySelector('[data-quote]').addEventListener('click', function () {
      if (!composer) return;
      composer.value += (composer.value ? '\n\n' : '') + '> ' + body.textContent.replace(/\n/g, '\n> ') + '\n';
      composer.focus();
      menu.classList.add('hidden');
    });
    message.querySelector('[data-important]').addEventListener('click', function (event) {
      message.classList.toggle('important');
      event.currentTarget.textContent = message.classList.contains('important') ? '取消重要' : '标记重要';
      menu.classList.add('hidden');
    });
    if (toggle) toggle.addEventListener('click', function () {
      setCollapsed(!body.classList.contains('collapsed'));
    });
    const expandBtn = message.querySelector('[data-expand]');
    if (expandBtn) expandBtn.addEventListener('click', function () { setCollapsed(false); });
  });
  document.addEventListener('click', function (event) {
    if (!event.target.closest('[data-message]')) closeMenus();
  });
})();
