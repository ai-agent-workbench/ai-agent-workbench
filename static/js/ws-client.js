/**
 * WebSocket client for Codex Workbench real-time channel.
 *
 * Strategy: WebSocket first, fall back to HTTP polling on failure.
 *
 * Events dispatched from the server (/ws):
 *   task.status   — {task_id, status, elapsed_seconds, ...}
 *   task.output   — {task_id, output_tail, summary}
 *   approval.request — {task_id, request: {id, action, summary, ...}}
 *
 * When the WS is active, HTTP polling is suppressed.
 * On WS disconnect, the existing chat-live.js poll loop resumes.
 */
(function () {
  'use strict';

  // 入口路径可能带 40+ hex 随机前缀（公网 nginx 按前缀转发并剥前缀），
  // 无前缀的直连路由页面回落到根路径 /ws。
  var prefixMatch = location.pathname.match(/^\/([0-9a-f]{40,})(\/|$)/);
  var WS_URL = (location.protocol === 'https:' ? 'wss://' : 'ws://')
             + location.host + (prefixMatch ? '/' + prefixMatch[1] : '') + '/ws';

  var ws = null;
  var reconnectTimer = null;
  var reconnectDelay = 2000;   // start at 2 s, capped at 30 s
  var maxReconnectDelay = 30000;

  function connect() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
      return;
    }

    try {
      ws = new WebSocket(WS_URL);
    } catch (e) {
      scheduleReconnect();
      return;
    }

    ws.onopen = function () {
      reconnectDelay = 2000;
      // Inform chat-live.js that WS is active so it can skip polling
      if (window.codexWSActive) window.codexWSActive(true);
      // Send a keep-alive ping every 45 s
      var pingInterval = setInterval(function () {
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send('ping');
        } else {
          clearInterval(pingInterval);
        }
      }, 45000);
    };

    ws.onmessage = function (event) {
      try {
        var msg = JSON.parse(event.data);
      } catch (e) {
        return;
      }

      switch (msg.type) {
        case 'task.status':
          handleTaskStatus(msg.data);
          break;
        case 'task.output':
          handleTaskOutput(msg.data);
          break;
        case 'approval.request':
          handleApproval(msg.data);
          break;
        case 'pong':
          // keep-alive response — nothing to do
          break;
      }
    };

    ws.onclose = function () {
      ws = null;
      if (window.codexWSActive) window.codexWSActive(false);
      scheduleReconnect();
    };

    ws.onerror = function () {
      // onclose will fire next — no need to double-handle
    };
  }

  function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(function () {
      reconnectTimer = null;
      connect();
    }, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, maxReconnectDelay);
  }

  // ── event handlers ─────────────────────────────────────────────────────

  function handleTaskStatus(data) {
    var live = document.getElementById('live-status');
    if (!live || live.dataset.taskId !== data.task_id) return;

    var label = document.getElementById('live-status-label');
    var runtime = document.getElementById('live-runtime');
    var outputState = document.getElementById('live-output-state');
    var warning = document.getElementById('live-warning');

    if (label) label.textContent = data.label || '';
    if (label) label.className = 'status ' + (data.status || '');

    if (runtime) {
      var secs = Number(data.elapsed_seconds) || 0;
      var text = secs < 60 ? secs + '秒' : Math.floor(secs / 60) + '分' + (secs % 60) + '秒';
      if (data.status === 'running') {
        runtime.textContent = '已运行 ' + text;
      } else {
        runtime.textContent = '耗时 ' + text;
      }
    }

    if (outputState) {
      outputState.textContent = data.status === 'running'
        ? '最后输出距今 ' + (Number(data.no_output_seconds) > 0
            ? (Number(data.no_output_seconds) < 60
                ? data.no_output_seconds + '秒'
                : Math.floor(data.no_output_seconds / 60) + '分' + (data.no_output_seconds % 60) + '秒')
            : '0秒')
        : '';
    }

    if (warning) {
      warning.textContent = data.output_warning
        ? '暂时没有新的执行输出，可能正在等待上游模型；可以继续等待或停止任务。'
        : '';
    }

    // If the task transitioned to a terminal state, reload the page
    if (data.status && ['succeeded', 'failed', 'cancelled', 'timed_out', 'finished'].indexOf(data.status) !== -1) {
      // 优先走 scroll.js 的收敛：贴底则跟随 reload；阅读中弹「任务已结束」条，
      // 不拽用户滚动位置（无 scroll.js 时退回直接 reload）。
      if (window.codexConversationScroll && window.codexConversationScroll.terminal) {
        window.codexConversationScroll.terminal(true);
      } else {
        window.setTimeout(function () { window.location.reload(); }, 500);
      }
    }
  }

  function handleTaskOutput(data) {
    var live = document.getElementById('live-status');
    if (!live || live.dataset.taskId !== data.task_id) return;

    var summaryEl = document.getElementById('live-summary');
    var outputEl = document.getElementById('live-output');
    if (summaryEl && data.summary !== undefined) {
      summaryEl.textContent = data.summary || '暂无摘要。';
    }
    if (outputEl && data.output_tail !== undefined) {
      outputEl.textContent = data.output_tail || '';
    }
    // 段落级准实时：更新/创建阶段性回复气泡（复用服务端 staged 结构）
    if (typeof data.staged === 'string' && data.staged) {
      updateStagedBubble(data.task_id, data.staged);
    }
  }

  function nearBottom() {
    var gap = document.documentElement.scrollHeight - window.scrollY - window.innerHeight;
    return Math.max(0, gap) < 180;
  }

  function updateStagedBubble(taskId, text) {
    var list = document.querySelector('section.conversation-messages');
    if (!list) return;
    var messageId = taskId + '-staged';
    var article = list.querySelector(
      "article.bubble.agent.staged[data-message-id='" + messageId + "']"
    );
    var stick = nearBottom(); // 变更前记录：用户在底部才跟随，阅读中不拽走
    if (!article) {
      var wrap = document.createElement('div');
      wrap.className = 'message-wrap agent';
      article = document.createElement('article');
      article.className = 'bubble agent staged';
      article.setAttribute('data-message', '');
      article.setAttribute('data-message-id', messageId);
      article.appendChild(document.createElement('div')).className = 'message-body';
      var meta = document.createElement('div');
      meta.className = 'message-meta';
      meta.textContent = '阶段性回复 · 任务处理中';
      wrap.appendChild(article);
      wrap.appendChild(meta);
      list.appendChild(wrap);
    }
    var body = article.querySelector('.message-body');
    if (body) body.textContent = text; // textContent 赋值，天然防 XSS
    if (stick) {
      window.scrollTo({ top: document.documentElement.scrollHeight, behavior: 'auto' });
    }
  }

  function handleApproval(data) {
    // Reload to show the approval card
    var live = document.getElementById('live-status');
    if (!live || live.dataset.taskId !== data.task_id) return;
    if (!document.querySelector('.windows-approval')) {
      window.location.reload();
    }
  }

  // ── expose globals for chat-live.js integration ───────────────────────

  /** chat-live.js calls this so we know when the WS takes over polling. */
  window.codexWSActive = function (active) {
    var live = document.getElementById('live-status');
    if (!live) return;
    if (active) {
      live.dataset.ws = '1';
    } else {
      delete live.dataset.ws;
    }
  };

  /** One-shot connect on page load.  chat-live.js falls back when WS is absent. */
  connect();
})();
