(function () {
  const tools = document.querySelector('[data-scroll-tools]');
  const jump = tools ? tools.querySelector('[data-jump-bottom]') : null;
  const notice = tools ? tools.querySelector('[data-new-reply]') : null;
  if (!tools || !jump || !notice) return;

  const taskId = tools.dataset.taskId;
  const followKey = 'codex-follow-latest-' + taskId;
  const scrollKey = 'codex-scroll-pos-' + taskId;
  let initialized = false;

  function gap() {
    return Math.max(0, document.documentElement.scrollHeight - window.scrollY - window.innerHeight);
  }
  function nearBottom() {
    return gap() < 180;
  }
  function update() {
    if (!initialized) return;
    jump.classList.toggle('hidden', nearBottom());
  }
  function goBottom(smooth) {
    if (smooth === undefined) smooth = true;
    window.scrollTo({
      top: document.documentElement.scrollHeight,
      behavior: smooth ? 'smooth' : 'auto',
    });
    notice.classList.add('hidden');
    window.setTimeout(update, smooth ? 350 : 0);
  }

  jump.addEventListener('click', function () { goBottom(true); });
  notice.addEventListener('click', function () {
    sessionStorage.setItem(followKey, '1');
    window.location.reload();
  });
  window.addEventListener('scroll', update, { passive: true });
  setInterval(function () {
    if (!nearBottom()) sessionStorage.setItem(scrollKey, window.scrollY);
  }, 3000);

  const forced = sessionStorage.getItem(followKey) === '1';
  if (forced) sessionStorage.removeItem(followKey);
  const approvalCard = document.getElementById('windows-approval');

  requestAnimationFrame(function () {
    requestAnimationFrame(function () {
      if (approvalCard) {
        approvalCard.scrollIntoView({ block: 'center' });
        notice.classList.add('hidden');
      } else if (!forced) {
        const saved = sessionStorage.getItem(scrollKey);
        if (saved) {
          window.scrollTo({ top: Number(saved), behavior: 'auto' });
          sessionStorage.removeItem(scrollKey);
        } else {
          goBottom(false);
        }
      } else {
        goBottom(false);
      }
      initialized = true;
      update();
    });
  });

  window.codexConversationScroll = {
    terminal: function (hasReply) {
      if (hasReply === undefined) hasReply = true;
      if (nearBottom()) {
        sessionStorage.setItem(followKey, '1');
        window.setTimeout(function () { window.location.reload(); }, 250);
      } else {
        notice.textContent = hasReply ? '有新回复' : '任务已结束';
        notice.classList.remove('hidden');
        jump.classList.remove('hidden');
      }
    },
  };
})();
