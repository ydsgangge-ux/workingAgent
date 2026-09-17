/* ── AI Web App ─────────────────── */

// 先走 polling 握手再升级到 websocket，WS 失败时自动降级到 polling
const socket = io({
  transports: ["polling", "websocket"]
});
let currentChatId = null;
let chatHistory = {};
let isTyping = false;
let sidebarOpen = window.innerWidth > 768; // 手机端侧边栏默认收起
let pendingImage = null;  // { image_path, image_url } 待发送的图片

socket.on('connect', () => {
  console.log('Connected');
  const sid = sessionStorage.getItem('sessionId');
  if (sid) socket.emit('restore_session', { session_id: sid });
});

socket.on('auth_result', d => {
  if (!d || !d.ok) { showLoginErr((d && d.error) || '认证失败'); return; }
  sessionStorage.setItem('sessionId', socket.id);
  if (d.name) document.getElementById('userName').textContent = d.name;
  document.getElementById('loginPage').style.display = 'none';
  document.getElementById('appPage').style.display = 'flex';
  loadChatList();
});

socket.on('chat:list', list => { renderChatList(list || []); });

socket.on('chat:loaded', d => {
  currentChatId = d.chat_id;
  document.getElementById('welcomeScreen')?.remove();
  document.getElementById('chatMessages').innerHTML = '';
  document.getElementById('chatTitle').textContent = d.title || 'AI';
  if (d.messages) d.messages.forEach(m => appendMessage(m));
  scrollToBottom();
  setActiveChat(d.chat_id);
});

socket.on('chat:reply', d => {
  hideTyping();
  if (d.message) appendMessage(d.message);
  scrollToBottom();
  if (!currentChatId && d.chat_id) currentChatId = d.chat_id;
  if (d.message) updateChatTitleFromMsg(d.message.content);
});

socket.on('chat:created', d => {
  if (!currentChatId && d.chat_id) currentChatId = d.chat_id;
  loadChatList();
});

socket.on('chat:typing', () => { showTyping(); });

socket.on('error', e => {
  hideTyping();
  const msg = (e && e.message) || e || '未知错误';
  appendMessage({ role: 'assistant', content: '**错误：** ' + msg });
  scrollToBottom();
});

socket.on('timed:reminder', d => {
  showReminderToast(d.message || '⏰ 定时提醒');
  loadTimedTasks();
});

socket.on('timed_task_list', list => { renderTimedList(list || []); });

function doLogin() {
  const p = document.getElementById('loginPass').value.trim();
  if (!p) return;
  const btn = document.getElementById('loginBtn');
  btn.disabled = true;
  btn.textContent = '登录中…';
  // 先通过 HTTP 登录设置 session cookie，再通过 WebSocket 登录
  fetch('/api/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ passphrase: p }),
    credentials: 'same-origin',
  })
  .then(r => r.json())
  .then(httpR => {
    if (!httpR.ok) {
      btn.disabled = false;
      btn.textContent = '登录';
      showLoginErr(httpR.error || '密码错误');
      return;
    }
    // HTTP 登录成功，再通过 WebSocket 建立连接
    socket.emit('login', { passphrase: p }, r => {
      btn.disabled = false;
      btn.textContent = '登录';
      if (!r || !r.ok) showLoginErr((r && r.error) || '登录失败，请检查网络连接');
    });
  })
  .catch(() => {
    btn.disabled = false;
    btn.textContent = '登录';
    showLoginErr('登录失败，请检查网络连接');
  });
  setTimeout(() => {
    if (btn.disabled) {
      btn.disabled = false;
      btn.textContent = '登录';
      showLoginErr('登录超时，请检查服务是否正常运行');
    }
  }, 10000);
}

function showLoginErr(msg) {
  const el = document.getElementById('loginErr');
  el.textContent = msg;
  setTimeout(() => el.textContent = '', 4000);
}

function doLogout() {
  socket.emit('logout');
  sessionStorage.clear();
  location.reload();
}

function newChat() {
  currentChatId = null;
  document.getElementById('chatMessages').innerHTML = `
    <div class="welcome-screen" id="welcomeScreen">
      <h2>AI</h2>
      <p>How can I help you today?</p>
    </div>`;
  document.getElementById('chatTitle').textContent = 'AI';
  clearActiveChat();
  document.getElementById('chatInput').focus();
}

function sendMessage() {
  const input = document.getElementById('chatInput');
  const text = input.value.trim();
  if ((!text && !pendingImage) || isTyping) return;

  // 如果有待发送图片，先上传再发消息
  if (pendingImage) {
    const img = pendingImage;
    pendingImage = null;
    updateAttachBtn(false);

    // 先显示图片在聊天中
    document.getElementById('welcomeScreen')?.remove();

    // 如果有文本就一起发，否则发空消息+图片
    const msgText = text || '';
    input.value = '';
    autoResize(input);

    appendMessage({ role: 'user', content: msgText, image_url: img.image_url });
    scrollToBottom();

    socket.emit('chat:message', {
      chat_id: currentChatId || '',
      message: msgText,
      image_path: img.image_path,
      image_url: img.image_url,
    });
    showTyping();
    return;
  }

  input.value = '';
  autoResize(input);

  document.getElementById('welcomeScreen')?.remove();

  appendMessage({ role: 'user', content: text });
  scrollToBottom();

  socket.emit('chat:message', { chat_id: currentChatId || '', message: text });
  showTyping();
}

function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
}

function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 160) + 'px';
}

/* ── 图片上传 ── */
function initImageUpload() {
  // 创建隐藏的文件输入
  const fileInput = document.createElement('input');
  fileInput.type = 'file';
  fileInput.accept = 'image/*';
  fileInput.style.display = 'none';
  fileInput.id = 'imageFileInput';
  document.body.appendChild(fileInput);

  fileInput.addEventListener('change', function(e) {
    const file = e.target.files[0];
    if (!file) return;

    // 检查文件大小（限制 10MB）
    if (file.size > 10 * 1024 * 1024) {
      alert('图片不能超过 10MB');
      fileInput.value = '';
      return;
    }

    const reader = new FileReader();
    reader.onload = function(ev) {
      const base64 = ev.target.result;
      // 通过 WebSocket 上传（绕过 HTTP cookie 认证问题）
      socket.emit('upload_image', { data: base64, filename: file.name }, res => {
        if (res && res.ok) {
          pendingImage = { image_path: res.image_path, image_url: res.image_url };
          updateAttachBtn(true);
          document.getElementById('chatInput').focus();
        } else {
          alert('图片上传失败：' + ((res && res.error) || '未知错误'));
        }
        // 上传完成后再清空，避免中断读取
        fileInput.value = '';
      });
    };
    reader.readAsDataURL(file);
  });

  // 绑定附件按钮 — 每次点击前确保 input 已清空
  document.querySelector('.btn-attach').addEventListener('click', function(e) {
    fileInput.value = '';
    fileInput.click();
  });

  // 粘贴图片支持
  document.getElementById('chatInput').addEventListener('paste', function(e) {
    const items = e.clipboardData && e.clipboardData.items;
    if (!items) return;
    for (const item of items) {
      if (item.type.startsWith('image/')) {
        e.preventDefault();
        const file = item.getAsFile();
        if (!file) return;
        if (file.size > 10 * 1024 * 1024) {
          alert('图片不能超过 10MB');
          return;
        }
        const reader = new FileReader();
        reader.onload = function(ev) {
          const base64 = ev.target.result;
          socket.emit('upload_image', { data: base64, filename: 'paste_' + Date.now() + '.png' }, res => {
            if (res && res.ok) {
              pendingImage = { image_path: res.image_path, image_url: res.image_url };
              updateAttachBtn(true);
            } else {
              alert('图片粘贴失败：' + ((res && res.error) || '未知错误'));
            }
          });
        };
        reader.readAsDataURL(file);
        break;
      }
    }
  });
}

function updateAttachBtn(hasPending) {
  const btn = document.querySelector('.btn-attach');
  if (hasPending) {
    btn.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#d97706" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>';
    btn.title = '有图片待发送';
  } else {
    btn.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>';
    btn.title = '添加图片';
  }
}

function appendMessage(m) {
  const area = document.getElementById('chatMessages');
  const div = document.createElement('div');
  div.className = 'message-row ' + (m.role === 'user' ? 'user' : 'assistant');

  let html = '<div class="message-text">';

  // 显示图片
  if (m.image_url) {
    html += `<img src="${m.image_url}" class="message-image" alt="uploaded image" onclick="openImageViewer(this.src)">`;
  }

  html += formatContent(m.content || '');
  html += '</div>';

  if (m.timestamp) {
    html += `<div class="msg-time">${formatTime(m.timestamp)}</div>`;
  }

  div.innerHTML = html;
  area.appendChild(div);

  if (m.role === 'user') saveChatToHistory(m.content);
}

function openImageViewer(src) {
  document.getElementById('imageViewerImg').src = src;
  document.getElementById('imageViewer').style.display = 'flex';
}

function formatContent(text) {
  if (!text) return '';
  let s = text
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  s = s.replace(/```(\w*)\n?([\s\S]*?)```/g, '<pre><code>$2</code></pre>');
  // [img:/api/images/xxx.png] → <img> 标签（AI生成图片 / 工具返回图片）
  s = s.replace(/\[img:(.+?)\]/g, (match, url) => {
    return `<img src="${url}" class="message-image" alt="generated image" onclick="openImageViewer('${url}')">`;
  });
  // [download:filename:显示名] → 下载链接（Office文件等）
  s = s.replace(/\[download:(.*?):(.*?)\]/g, (match, path, name) => {
    return `<a href="/api/download/${encodeURIComponent(path)}" class="download-link" download>📎 下载 ${name}</a>`;
  });
  s = s.replace(/\n/g, '<br>');
  return s;
}

function formatTime(ts) {
  try { return new Date(ts).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }); }
  catch { return ''; }
}

function scrollToBottom() {
  const el = document.getElementById('chatMessages');
  requestAnimationFrame(() => { el.scrollTop = el.scrollHeight; });
}

function showTyping() {
  if (isTyping) return;
  isTyping = true;
  const area = document.getElementById('chatMessages');
  const row = document.createElement('div');
  row.className = 'typing-row';
  row.id = 'typingIndicator';
  row.innerHTML = `<div class="typing-dots"><span></span><span></span><span></span></div>`;
  area.appendChild(row);
  scrollToBottom();
}

function hideTyping() {
  isTyping = false;
  const el = document.getElementById('typingIndicator');
  if (el) el.remove();
}

function toggleSidebar() {
  const sb = document.getElementById('sidebar');
  sidebarOpen = !sidebarOpen;
  sb.classList.toggle('collapsed', !sidebarOpen);

  // 手机端：打开侧边栏时添加遮罩，收起时显示浮动按钮
  if (window.innerWidth <= 768) {
    let backdrop = document.getElementById('sidebarBackdrop');
    if (!sidebarOpen) {
      if (backdrop) backdrop.remove();
      showFloatMenuBtn();
    } else {
      if (!backdrop) {
        backdrop = document.createElement('div');
        backdrop.id = 'sidebarBackdrop';
        backdrop.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.3);z-index:99;';
        backdrop.addEventListener('click', () => toggleSidebar());
        document.getElementById('appPage').appendChild(backdrop);
      }
      hideFloatMenuBtn();
    }
  }
}

/* ── 手机端浮动菜单按钮 ── */
function showFloatMenuBtn() {
  let btn = document.getElementById('floatMenuBtn');
  if (!btn) {
    btn = document.createElement('button');
    btn.id = 'floatMenuBtn';
    btn.innerHTML = '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>';
    btn.style.cssText = 'position:fixed;bottom:16px;left:12px;width:44px;height:44px;border-radius:50%;background:#1a1a1a;color:#fff;border:none;z-index:1000;cursor:pointer;display:flex;align-items:center;justify-content:center;box-shadow:0 2px 12px rgba(0,0,0,.2);';
    btn.addEventListener('click', () => toggleSidebar());
    document.body.appendChild(btn);
  }
  btn.style.display = 'flex';
}

function hideFloatMenuBtn() {
  const btn = document.getElementById('floatMenuBtn');
  if (btn) btn.style.display = 'none';
}

let toolListCache = [];

socket.on('tool_list', list => {
  toolListCache = list || [];
  renderToolbox(list);
});

function toggleToolbox() {
  const dd = document.getElementById('toolboxDropdown');
  const btn = document.getElementById('toolboxBtn');
  const isOpen = dd.style.display !== 'none';
  dd.style.display = isOpen ? 'none' : 'block';
  btn.classList.toggle('active', !isOpen);
  if (!isOpen && toolListCache.length === 0) {
    socket.emit('get_tool_list');
  }
  if (!isOpen) {
    const si = document.getElementById('toolSearchInput');
    if (si) { si.value = ''; si.focus(); }
    renderToolbox(toolListCache);
  }
}

function renderToolbox(list) {
  const container = document.getElementById('toolboxList');
  if (!container) return;
  if (!list || list.length === 0) {
    container.innerHTML = '<div style="padding:10px;text-align:center;color:#999;font-size:.82rem">暂无可用工具</div>';
    return;
  }
  container.innerHTML = '';
  list.forEach(t => {
    const riskColors = { high: '#f53f3f', medium: '#ff7d00', low: '#999' };
    const riskColor = riskColors[t.risk] || '#999';
    const item = document.createElement('div');
    item.className = 'toolbox-item';
    item.dataset.name = t.name;
    item.onclick = () => useTool(t.name, t.params);
    item.innerHTML = `
      <div class="toolbox-item-info">
        <span class="toolbox-item-name">${escapeHtml(t.name)}</span>
        <span class="toolbox-item-desc">${escapeHtml(t.description || '')}</span>
      </div>
      <span class="toolbox-item-risk" style="color:${riskColor}">${t.risk}</span>`;
    container.appendChild(item);
  });
}

function filterTools(keyword) {
  const kw = (keyword || '').toLowerCase();
  if (!kw) { renderToolbox(toolListCache); return; }
  const filtered = toolListCache.filter(t =>
    t.name.toLowerCase().includes(kw) || (t.description || '').toLowerCase().includes(kw)
  );
  renderToolbox(filtered);
}

function useTool(name, params) {
  const input = document.getElementById('chatInput');
  const paramHint = (params && params.length > 0) ? params.join(', ') : '';
  input.value = `使用 ${name} 工具` + (paramHint ? `（参数：${paramHint}）` : '') + '：';
  input.focus();
  autoResize(input);
  toggleToolbox();
}

document.addEventListener('click', e => {
  const wrapper = document.querySelector('.toolbox-wrapper');
  if (wrapper && !wrapper.contains(e.target)) {
    const dd = document.getElementById('toolboxDropdown');
    const btn = document.getElementById('toolboxBtn');
    if (dd) dd.style.display = 'none';
    if (btn) btn.classList.remove('active');
  }
});

/* ── Chat List ── */

function renderChatList(list) {
  const starredEl = document.getElementById('chatListStarred');
  const recentEl = document.getElementById('chatListRecent');
  starredEl.innerHTML = '';
  recentEl.innerHTML = '';

  const sorted = [...list].sort((a, b) =>
    new Date(b.updated_at || b.created_at || 0) - new Date(a.updated_at || a.created_at || 0));

  sorted.forEach(c => {
    const item = document.createElement('div');
    item.className = 'chat-item' + (c.chat_id === currentChatId || c.id === currentChatId ? ' active' : '');
    item.dataset.id = c.chat_id || c.id;

    const title = c.title || '新对话';
    const msgs = c.messages || [];
    const msgCount = msgs.length;

    item.innerHTML = `
      <span class="chat-item-title" onclick="loadChat('${c.chat_id || c.id}')">${escapeHtml(title)}</span>
      ${msgCount > 1 ? `<span class="chat-item-count">${msgCount}</span>` : ''}
      <button class="chat-item-delete" onclick="deleteChat('${c.chat_id || c.id}',event)" title="删除">&times;</button>`;

    (c.starred ? starredEl : recentEl).appendChild(item);
  });
}

function escapeHtml(s) {
  const d = document.createElement('div'); d.textContent = s; return d.innerHTML;
}

function loadChat(id) {
  socket.emit('load_chat', { chat_id: id });
}

function deleteChat(id, e) {
  e.stopPropagation();
  if (!confirm('确定删除此对话？')) return;
  socket.emit('delete_chat', { chat_id: id });
  if (id === currentChatId) newChat();
  setTimeout(() => loadChatList(), 200);
}

function loadChatList() { socket.emit('get_chat_list'); }

function setActiveChat(id) {
  document.querySelectorAll('.chat-item').forEach(el => el.classList.toggle('active', el.dataset.id === id));
}
function clearActiveChat() { document.querySelectorAll('.chat-item').forEach(el => el.classList.remove('active')); }

function saveChatToHistory(text) {
  if (!currentChatId) return;
  if (!chatHistory[currentChatId]) chatHistory[currentChatId] = [];
  chatHistory[currentChatId].push({ role: 'user', content: text, ts: Date.now() });
}

function updateChatTitleFromMsg(content) {
  if (!content) return;
  const title = content.replace(/\*\*/g, '').replace(/<[^>]+>/g, '').substring(0, 40);
  if (title.length > 3) {
    document.getElementById('chatTitle').textContent = title;
    socket.emit('rename_chat', { chat_id: currentChatId, title: title });
  }
}

/* ── Timed Tasks ── */

function showTimedTasks() {
  document.getElementById('timedPanel').style.display = 'flex';
  loadTimedTasks();
}

function hideTimedTasks() {
  document.getElementById('timedPanel').style.display = 'none';
}

function closeTimedTasks(e) {
  if (e.target === e.currentTarget) hideTimedTasks();
}

function loadTimedTasks() { socket.emit('list_timed_tasks'); }

function renderTimedList(list) {
  const body = document.getElementById('timedBody');
  if (!list || list.length === 0) {
    body.innerHTML = '<p class="timed-hint">暂无定时任务。通过对话设置提醒，如"30分钟后提醒我喝水"</p>';
    return;
  }
  body.innerHTML = '';
  list.forEach(t => {
    const item = document.createElement('div');
    item.className = 'timed-item';
    const statusMap = { pending: '⏳ 等待中', done: '✅ 已完成', cancelled: '❌ 已取消', expired: '⚠️ 已过期' };
    const statusLabel = statusMap[t.status] || t.status;
    item.innerHTML = `
      <div class="timed-item-content">
        <strong>${escapeHtml(t.message || '提醒')}</strong>
        <span style="color:#999;font-size:.78rem;margin-left:6px">[${statusLabel}]</span>
        ${t.repeat_interval ? '<span class="timed-item-repeat">重复</span>' : ''}
      </div>
      <div class="timed-item-time">
        触发时间：${new Date((t.trigger_time || 0) * 1000).toLocaleString('zh-CN')}
        ${t.repeat_interval ? '<br>重复间隔：' + t.repeat_interval : ''}
      </div>`;
    body.appendChild(item);
  });
}

/* ── Image Viewer ── */

function closeImageViewer() {
  document.getElementById('imageViewer').style.display = 'none';
}

/* ── Reminder Toast ── */

function showReminderToast(msg) {
  const existing = document.querySelector('.reminder-toast');
  if (existing) existing.remove();

  const toast = document.createElement('div');
  toast.className = 'reminder-toast';
  toast.innerHTML = `<div class="reminder-toast-title">⏰ 提醒时间到</div><div class="reminder-toast-msg">${escapeHtml(msg)}</div>`;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 8000);
}

/* ── Init ── */
document.addEventListener('DOMContentLoaded', () => {
  // 手机端侧边栏默认收起
  if (window.innerWidth <= 768) {
    document.getElementById('sidebar').classList.add('collapsed');
    showFloatMenuBtn();
  }
  document.getElementById('chatInput').focus();
  initImageUpload();
});
