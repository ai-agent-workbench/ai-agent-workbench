(function () {
  const live = document.getElementById('live-status');
  const taskId = live ? live.dataset.taskId : null;
  const details = document.getElementById('execution-details');
  if (!live || live.dataset.active !== '1') return;

 let intervalId = null;
  let knownAgentMessageCount = -1;
 const activeStates = new Set([
    'queued', 'running', 'stopping', 'waiting_approval', 'windows_executing',
  ]);

  function text(seconds) {
    seconds = Number(seconds) || 0;
    return seconds < 60 ? seconds + '秒'
      : Math.floor(seconds / 60) + '分' + (seconds % 60) + '秒';
  }

  function stopPolling() {
    live.dataset.active = '0';
    if (intervalId !== null) {
      clearInterval(intervalId);
      intervalId = null;
    }
    if (details) details.removeEventListener('toggle', poll);
  }

  async function poll() {
    if (live.dataset.ws === '1') return;
    try {
      const qs = details && details.open ? '1' : '0';
      const resp = await fetch(
        '/task/' + taskId + '/status?details=' + qs,
        { cache: 'no-store', credentials: 'same-origin' },
      );
      if (!resp.ok) return;
     const data = await resp.json();

      const msgCount = Number(data.agent_message_count) || 0;
      if (knownAgentMessageCount < 0) {
        knownAgentMessageCount = msgCount;
      } else if (msgCount > knownAgentMessageCount) {
        stopPolling();
        window.setTimeout(function () { window.location.reload(); }, 250);
        return;
      }

     document.getElementById('live-status-label').textContent = data.label;
      document.getElementById('live-status-label').className = 'status ' + data.status;
      const runtimeEl = document.getElementById('live-runtime');
      if (data.status === 'running') {
        runtimeEl.textContent = '运行 ' + Math.floor(data.elapsed_seconds/60) + ':' + String(data.elapsed_seconds%60).padStart(2,'0');
      } else if (data.status === 'stopping') {
        runtimeEl.textContent = '正在等待任务退出';
      } else if (data.status === 'waiting_approval') {
        runtimeEl.textContent = '等待手机授权';
      } else if (data.status === 'windows_executing') {
        runtimeEl.textContent = '后台执行中';
      } else {
        runtimeEl.textContent = '耗时 ' + Math.floor(data.elapsed_seconds/60) + ':' + String(data.elapsed_seconds%60).padStart(2,'0');
      }
     document.getElementById('live-output-state').textContent =
        data.status === 'running' ? ' · 最后 ' + Math.floor(data.no_output_seconds/60) + ':' + String(data.no_output_seconds%60).padStart(2,'0') : '';
     document.getElementById('live-warning').textContent =
        data.output_warning && activeStates.has(data.status)
          ? '暂时没有新的执行输出，可能正在等待上游模型；可以继续等待或停止任务。' : '';

      if (details && details.open && data.summary !== undefined) {
        document.getElementById('live-summary').textContent = data.summary || '暂无摘要。';
        document.getElementById('live-output').textContent = data.output_tail || '';
      }

      if (!activeStates.has(data.status)) {
        stopPolling();
        window.setTimeout(function () { window.location.reload(); }, 250);
      }
    } catch (err) {
      // network error, retry next interval
    }
  }

  // 2026-07-28 用户拍板：关闭 5 秒轮询自动刷新（试用）。
  // 状态行不再逐秒跳动，新消息与终态不再自动载入；
  // 用户用 composer 的 ↻ 按钮或发消息手动刷新。恢复 = 还原下面两行。
  // poll();
  // intervalId = setInterval(poll, 5000);
  if (details) details.addEventListener('toggle', poll);
})();
