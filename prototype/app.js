/* ══════════════════════════════════════════════════════════════
   GLKVM 屏幕守卫 · UI 原型（简化版）
   交互全部为静态演示，不连接真实设备
   ══════════════════════════════════════════════════════════════ */
(() => {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const state = {
    mode: 'local',      // local | remote
    paused: false,
    dirty: false,
  };

  /* ── 标签页 ───────────────────────────────────────────────── */
  function setTab(name) {
    $$('.tab').forEach(t => t.classList.toggle('is-active', t.dataset.tab === name));
    $$('.panel').forEach(p => p.classList.toggle('is-active', p.dataset.panel === name));
    $('#content').scrollTop = 0;
  }

  /* ── 表单快照与未保存状态 ─────────────────────────────────── */
  function formEls() {
    return $$('#content input, #content select, #content textarea')
      .filter(el => el.id !== 'pauseCheck');
  }

  let cleanValues = [];

  function captureClean() {
    cleanValues = formEls().map(el =>
      (el.type === 'checkbox' || el.type === 'radio') ? el.checked : el.value
    );
  }

  function restoreClean() {
    formEls().forEach((el, i) => {
      if (el.type === 'checkbox' || el.type === 'radio') el.checked = cleanValues[i];
      else el.value = cleanValues[i];
    });
    syncScreenSelect();
  }

  function setDirty(dirty) {
    state.dirty = dirty;
    const text = $('#dirtyText');
    text.textContent = dirty ? '有未保存的更改' : '无未保存更改';
    text.classList.toggle('is-dirty', dirty);
    $('#saveBtn').disabled = !dirty;
    $('#cancelBtn').disabled = !dirty;
  }

  /* ── 断开动作里的屏幕编号 ─────────────────────────────────── */
  function syncScreenSelect() {
    const picked = ($('input[name="onDisconnect"]:checked') || {}).value;
    $('#screenSel').disabled = picked !== 'screen';
  }

  /* ── 显示状态 ─────────────────────────────────────────────── */
  function renderMode() {
    const remote = state.mode === 'remote';
    const paused = state.paused;

    const pill = $('#statusPill');
    pill.classList.toggle('is-remote', remote && !paused);
    pill.classList.toggle('is-paused', paused);
    $('#statusText').textContent = paused ? '已暂停' : remote ? '远程观看中' : '本地显示';

    $('#clientCount').textContent = remote ? '1' : '0';

    const capture = $('#captureBtn');
    capture.disabled = remote;
    $('#captureHint').textContent = remote
      ? '远程观看中：当前是远程布局，断开后再采集。'
      : '读取当前桌面并覆盖这份快照';

    $('#pauseCheck').checked = paused;
    $('#footDot').className = `dot ${paused ? 'warn' : 'ok'}`;
    $('#footSvc').textContent = paused ? '守护已暂停' : '服务运行中';
    $('#footConn').textContent = remote
      ? 'GLKVM 正在被观看 · 1 个客户端'
      : 'GLKVM 已连接 · 16 ms';
  }

  /* ── 测试连接 ─────────────────────────────────────────────── */
  function testConnection() {
    const btn = $('#testBtn');
    if (btn.disabled) return;
    const result = $('#testResult');
    btn.disabled = true;
    btn.textContent = '测试中…';
    result.innerHTML = '<span class="dot warn"></span>正在连接…';

    setTimeout(() => {
      btn.disabled = false;
      btn.textContent = '测试连接';
      const ms = 14 + Math.floor(Math.random() * 6);
      result.innerHTML = `<span class="dot ok"></span>已连接 · kvmd 4.82 · ${ms} ms`;
      $('#footConn').textContent = `GLKVM 已连接 · ${ms} ms`;
    }, 900);
  }

  /* ── 采集当前显示器设置 ───────────────────────────────────── */
  let capturing = false;

  function captureSnapshot() {
    const btn = $('#captureBtn');
    if (capturing || state.mode === 'remote') return;
    capturing = true;

    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = '采集中…';

    setTimeout(() => {
      capturing = false;
      btn.disabled = false;
      btn.textContent = label;
      $('#snapMeta').textContent = '27U8A Pro-S · DP-1 · 采集于刚刚';
      $('#captureHint').textContent = '已应用当前显示器设置';
    }, 1000);
  }

  /* ── 事件绑定 ─────────────────────────────────────────────── */
  function bind() {
    $('#tabs').addEventListener('click', e => {
      const tab = e.target.closest('.tab');
      if (tab) setTab(tab.dataset.tab);
    });

    // 任何表单改动都标记为未保存
    $('#content').addEventListener('input', e => {
      if (e.target.id === 'pauseCheck') return;
      if (e.target.matches('input, select, textarea')) setDirty(true);
    });
    $('#content').addEventListener('change', e => {
      if (e.target.id === 'pauseCheck') return;
      if (e.target.matches('input, select, textarea')) setDirty(true);
    });

    $$('input[name="onDisconnect"]').forEach(r => {
      r.addEventListener('change', syncScreenSelect);
    });

    $('#testBtn').addEventListener('click', testConnection);
    $('#captureBtn').addEventListener('click', captureSnapshot);

    $('#showPass').addEventListener('click', () => {
      const input = $('#pass');
      const show = input.type === 'password';
      input.type = show ? 'text' : 'password';
      $('#showPass').textContent = show ? '隐藏' : '显示';
    });

    $('#pauseCheck').addEventListener('change', e => {
      state.paused = e.target.checked;
      renderMode();
    });

    $('#demoState').addEventListener('change', e => {
      state.mode = e.target.value;
      renderMode();
    });

    $('#saveBtn').addEventListener('click', () => {
      captureClean();
      setDirty(false);
      const text = $('#dirtyText');
      text.textContent = '已保存';
      setTimeout(() => {
        if (!state.dirty) text.textContent = '无未保存更改';
      }, 2200);
    });

    $('#cancelBtn').addEventListener('click', () => {
      restoreClean();
      setDirty(false);
      const text = $('#dirtyText');
      text.textContent = '已取消更改';
      setTimeout(() => {
        if (!state.dirty) text.textContent = '无未保存更改';
      }, 2200);
    });

    // 刷新显示器：仅做视觉反馈
    $('#rescanBtn').addEventListener('click', () => {
      const btn = $('#rescanBtn');
      btn.disabled = true;
      btn.textContent = '刷新中…';
      setTimeout(() => {
        btn.disabled = false;
        btn.textContent = '刷新';
      }, 700);
    });

    // 键盘：Alt + 1..4
    document.addEventListener('keydown', e => {
      if (!e.altKey || e.ctrlKey || e.metaKey) return;
      const map = { '1': 'connect', '2': 'actions', '3': 'displays', '4': 'service' };
      if (map[e.key]) {
        e.preventDefault();
        setTab(map[e.key]);
      }
    });
  }

  /* ── 初始化 ───────────────────────────────────────────────── */
  function init() {
    bind();
    syncScreenSelect();
    captureClean();
    setDirty(false);

    // 便于单独预览某个标签页：index.html?tab=actions&state=remote
    const params = new URLSearchParams(location.search);
    const tab = params.get('tab');
    if (tab && $(`.panel[data-panel="${tab}"]`)) setTab(tab);
    if (params.get('state') === 'remote') {
      state.mode = 'remote';
      $('#demoState').value = 'remote';
    }

    renderMode();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
