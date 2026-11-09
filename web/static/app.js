/* ============================================================
   PhotoMagic Web Frontend Logic
   ============================================================ */

// 1. 用户身份与本地设置 (利用 localStorage 隔离并保存 API Key)
if (!localStorage.getItem('pm_user_id')) {
  localStorage.setItem('pm_user_id', 'usr_' + Math.random().toString(36).substring(2, 12));
}
const USER_ID = localStorage.getItem('pm_user_id');

let state = {
  files: [],
  taskId: null,
  taskState: 'idle',
  currentPreviewFile: null,
  logOffset: 0,
};

// 拦截 Fetch 请求，自动带上 X-User-ID 头
const originalFetch = window.fetch;
window.fetch = function(url, options = {}) {
  if (!options.headers) options.headers = {};
  
  if (options.headers instanceof Headers) {
    options.headers.set('X-User-ID', USER_ID);
  } else if (Array.isArray(options.headers)) {
    options.headers.push(['X-User-ID', USER_ID]);
  } else {
    options.headers['X-User-ID'] = USER_ID;
  }
  
  return originalFetch(url, options);
};

// 封装 fetch 错误处理
async function apiFetch(url, options = {}) {
  const res = await fetch(url, options);
  if (!res.ok) {
    let errMsg = `HTTP ${res.status}`;
    try {
      const errData = await res.json();
      if (errData.detail) errMsg = errData.detail;
    } catch(e) {}
    throw new Error(errMsg);
  }
  return res.json();
}

// ================= HTML / URL 安全处理（防 XSS） =================
// 所有拼接进 innerHTML 的动态数据都必须经过这里转义，避免用户文件名或
// LLM 返回内容被当作 HTML/JS 执行。
function escapeHtml(value) {
  const map = {
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;',
    "'": '&#39;', '`': '&#96;', '=': '&#61;', '/': '&#47;',
  };
  return String(value == null ? '' : value).replace(/[&<>"'`=\/]/g, ch => map[ch]);
}

// 只接受后端生成的图片 data URL，避免注入 javascript: / 属性逃逸
function safeImageDataUrl(value) {
  const text = String(value == null ? '' : value);
  return /^data:image\/(?:jpeg|jpg|png|webp|gif);base64,[A-Za-z0-9+/]+={0,2}$/.test(text) ? text : '';
}

// 把静态目录文件名转成可安全放进 URL / CSS 的片段
function encodeStaticName(value) {
  const name = String(value == null ? '' : value);
  if (!name || /[\/\\\x00-\x1f"'`<>]/.test(name)) return '';
  return encodeURIComponent(name);
}

// 全局提示
function toast(msg, type = 'info') {
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  // 简单的 toast 样式（内联）
  Object.assign(el.style, {
    position: 'fixed', bottom: '32px', left: '50%', transform: 'translateX(-50%)',
    background: type === 'error' ? '#ef4444' : type === 'success' ? '#10b981' : '#3b82f6',
    color: '#fff', padding: '12px 24px', borderRadius: '8px', zIndex: '9999',
    boxShadow: '0 4px 12px rgba(0,0,0,0.3)', fontSize: '14px', fontWeight: '500',
    opacity: '0', transition: 'opacity 0.3s'
  });
  document.body.appendChild(el);
  requestAnimationFrame(() => el.style.opacity = '1');
  setTimeout(() => {
    el.style.opacity = '0';
    setTimeout(() => el.remove(), 300);
  }, 3000);
}

// ================= 事件绑定 =================
function bindEvents() {
  // 首页“开始处理照片”按钮（原先用内联 onclick，不符合 CSP 要求）
  const heroStart = document.getElementById('btn-hero-start');
  if (heroStart) {
    heroStart.addEventListener('click', () => {
      const workspaceNav = document.querySelector('.nav-link[data-target="workspace"]');
      if (workspaceNav) workspaceNav.click();
    });
  }
}

// ================= 初始化与路由 =================
document.addEventListener('DOMContentLoaded', async () => {
  initRouter();
  await checkSystemStatus();
  await loadBackground();
  await loadSettings();
  await loadGallery();
  bindEvents();
});

function initRouter() {
  document.querySelectorAll('[data-target]').forEach(el => {
    el.addEventListener('click', (e) => {
      e.preventDefault();
      const target = el.dataset.target;
      
      // 更新导航栏状态
      document.querySelectorAll('.nav-link').forEach(l => l.classList.remove('active'));
      const navTarget = document.querySelector(`.nav-link[data-target="${target}"]`);
      if (navTarget) navTarget.classList.add('active');

      // 切换视图
      document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
      const view = document.getElementById(`view-${target}`);
      if (view) view.classList.add('active');

      // 首页/工作台互切时，处理状态显示
      if (target === 'workspace' && state.files.length > 0) {
        document.getElementById('dropzone').style.display = 'none';
        document.getElementById('workspace-container').style.display = 'block';
      } else if (target === 'workspace') {
        document.getElementById('dropzone').style.display = 'flex';
        document.getElementById('workspace-container').style.display = 'none';
      }
    });
  });
}

// ================= 系统检测 =================
async function checkSystemStatus() {
  const statusEl = document.getElementById('server-status');
  const detailEl = document.getElementById('rt-status-detail');
  
  try {
    const sys = await apiFetch('/api/system');
    const rtVersion = escapeHtml(sys.rt_version);
    if (sys.rt_available) {
      statusEl.innerHTML = `<span class="dot ok"></span><span class="text">服务器就绪 (RT ${rtVersion})</span>`;
      detailEl.innerHTML = `<span class="dot ok"></span> RawTherapee ${rtVersion} 已就绪`;
    } else {
      statusEl.innerHTML = `<span class="dot error"></span><span class="text">缺少 RT 引擎</span>`;
      detailEl.innerHTML = `<span class="dot error"></span> 警告: 服务器未安装 rawtherapee-cli，无法处理图片。`;
    }
  } catch(e) {
    statusEl.innerHTML = `<span class="dot error"></span><span class="text">连接失败</span>`;
    detailEl.innerHTML = `<span class="dot error"></span> 无法连接到服务器。`;
  }
}

// ================= 背景与案例加载 =================
async function loadBackground() {
  try {
    const res = await apiFetch('/api/backgrounds');
    if (res.images && res.images.length > 0) {
      const img = encodeStaticName(res.images[Math.floor(Math.random() * res.images.length)]);
      if (img) document.getElementById('bg-blur').style.backgroundImage = `url('/static/backgrounds/${img}')`;
    }
  } catch(e) { console.warn("背景加载失败", e); }
}

async function loadGallery() {
  try {
    const res = await apiFetch('/api/gallery');
    const homeGal = document.getElementById('home-gallery');
    const fullGal = document.getElementById('full-gallery');
    
    if (!res.images || res.images.length === 0) {
      homeGal.innerHTML = '<div class="empty-placeholder">维护者可以在 web/static/gallery/ 放入图片</div>';
      fullGal.innerHTML = '<div class="empty-placeholder" style="grid-column:1/-1;text-align:center;">暂无案例图</div>';
      return;
    }

    // 首页只取前 6 张
    const homeImgs = res.images.slice(0, 6)
      .map(encodeStaticName)
      .filter(Boolean);
    homeGal.innerHTML = homeImgs.map(img => `<img src="/static/gallery/${img}" loading="lazy" alt="">`).join('');
    
    // 画廊页取全部
    fullGal.innerHTML = res.images.map(rawName => {
      const img = encodeStaticName(rawName);
      if (!img) return '';
      return `
      <div class="photo-item">
        <img src="/static/gallery/${img}" class="photo-thumb" loading="lazy" alt="">
        <div class="photo-info"><div class="photo-name">${escapeHtml(rawName)}</div></div>
      </div>
    `;
    }).join('');
  } catch(e) { console.warn("画廊加载失败", e); }
}

// ================= 设置读写 =================
async function loadSettings() {
  // 从 localStorage 读取 API 配置
  document.getElementById('set-api-base').value = localStorage.getItem('pm_api_base') || 'https://api.openai.com/v1';
  document.getElementById('set-api-model').value = localStorage.getItem('pm_api_model') || 'gpt-4o';
  document.getElementById('set-api-key').value = localStorage.getItem('pm_api_key') || '';

  // 从 SQLite 读取用户专属设置
  try {
    const res = await apiFetch('/api/settings');
    document.getElementById('set-user-prompt').value = res.user_prompt;
    document.getElementById('set-strength').value = res.strength;
    document.getElementById('val-strength').textContent = res.strength + '%';
    document.getElementById('set-max-tokens').value = res.max_tokens;
    document.getElementById('set-parallel').value = res.parallel;
  } catch(e) { toast("加载设置失败", "error"); }
}

document.getElementById('set-strength').addEventListener('input', (e) => {
  document.getElementById('val-strength').textContent = e.target.value + '%';
});

document.getElementById('btn-save-settings').addEventListener('click', async () => {
  // 保存本地
  localStorage.setItem('pm_api_base', document.getElementById('set-api-base').value.trim());
  localStorage.setItem('pm_api_model', document.getElementById('set-api-model').value.trim());
  localStorage.setItem('pm_api_key', document.getElementById('set-api-key').value.trim());

  // 保存后端
  try {
    await apiFetch('/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        user_prompt: document.getElementById('set-user-prompt').value,
        strength: parseInt(document.getElementById('set-strength').value),
        max_tokens: parseInt(document.getElementById('set-max-tokens').value),
        parallel: parseInt(document.getElementById('set-parallel').value),
      })
    });
    toast("设置已保存", "success");
    toggleDrawer(false);
  } catch(e) { toast(e.message, "error"); }
});

// ================= 抽屉控制 =================
function toggleDrawer(show) {
  const overlay = document.getElementById('drawer-overlay');
  const drawer = document.getElementById('settings-drawer');
  if (show) {
    overlay.classList.add('active');
    drawer.classList.add('active');
  } else {
    overlay.classList.remove('active');
    drawer.classList.remove('active');
  }
}
document.getElementById('btn-open-settings').addEventListener('click', () => toggleDrawer(true));
document.getElementById('btn-close-settings').addEventListener('click', () => toggleDrawer(false));
document.getElementById('drawer-overlay').addEventListener('click', () => toggleDrawer(false));

// ================= 文件上传 =================
const dropzone = document.getElementById('dropzone');
['dragenter', 'dragover'].forEach(ev => dropzone.addEventListener(ev, e => { e.preventDefault(); dropzone.classList.add('dragover'); }));
['dragleave', 'drop'].forEach(ev => dropzone.addEventListener(ev, e => { e.preventDefault(); dropzone.classList.remove('dragover'); }));
dropzone.addEventListener('drop', e => { if(e.dataTransfer.files.length) handleUpload(e.dataTransfer.files); });

document.getElementById('btn-select-files').addEventListener('click', () => document.getElementById('file-input').click());
document.getElementById('file-input').addEventListener('change', e => { if(e.target.files.length) handleUpload(e.target.files); });

async function handleUpload(files) {
  const fd = new FormData();
  for(let f of files) fd.append('files', f);

  const btn = document.getElementById('btn-select-files');
  btn.textContent = "正在上传解析...";
  btn.disabled = true;

  try {
    const res = await apiFetch('/api/uploads', { method: 'POST', body: fd });
    res.files.forEach(f => state.files.push({ ...f, checked: true, status: 'pending' }));
    
    // 切换视图
    dropzone.style.display = 'none';
    document.getElementById('workspace-container').style.display = 'block';
    renderGrid();
  } catch(e) {
    toast("上传失败: " + e.message, "error");
  } finally {
    btn.textContent = "浏览文件";
    btn.disabled = false;
  }
}

// ================= 文件网格与选择 =================
function renderGrid() {
  const grid = document.getElementById('photo-grid');
  document.getElementById('file-count').textContent = state.files.length;
  
  grid.innerHTML = state.files.map(f => {
    let badgeClass = f.status === 'done' ? 'done' : f.status === 'failed' ? 'failed' : f.status === 'running' ? 'running' : '';
    let badgeText = {pending:'待处理', running:'处理中', done:'已完成', failed:'失败'}[f.status] || '待处理';
    const name = escapeHtml(f.name);
    const id = escapeHtml(f.id);
    const thumb = safeImageDataUrl(f.thumb);
    const thumbHtml = thumb
      ? `<img src="${thumb}" class="photo-thumb" alt="">`
      : `<div class="photo-thumb"></div>`;
    
    return `
      <div class="photo-item ${f.checked?'selected':''}" data-id="${id}">
        <div class="select-indicator"></div>
        ${thumbHtml}
        <div class="photo-info">
          <div class="photo-name" title="${name}">${name}</div>
          <div class="photo-meta">
            <span class="status-badge ${badgeClass}">${badgeText}</span>
            <span class="text-muted text-sm">${(f.size/1024/1024).toFixed(1)} MB</span>
          </div>
        </div>
      </div>
    `;
  }).join('');

  // 绑定点击事件
  document.querySelectorAll('.photo-item').forEach(card => {
    card.addEventListener('click', (e) => {
      const id = card.dataset.id;
      const file = state.files.find(x => x.id === id);
      if(!file) return;

      // 如果是点击左上角选择框，或者按住了 Ctrl/Shift，则切换选中状态
      if (e.target.closest('.select-indicator') || e.ctrlKey || e.metaKey) {
        file.checked = !file.checked;
        renderGrid();
      } else {
        // 否则打开大图预览
        openPreview(file);
      }
    });
  });
}

// ================= 任务控制 =================
document.getElementById('btn-check-all').addEventListener('click', () => { state.files.forEach(f=>f.checked=true); renderGrid(); });
document.getElementById('btn-uncheck-all').addEventListener('click', () => { state.files.forEach(f=>f.checked=false); renderGrid(); });
document.getElementById('btn-clear').addEventListener('click', () => {
  if(state.taskState === 'running') return toast("正在处理中，无法清空", "error");
  state.files = []; renderGrid(); 
  document.getElementById('workspace-container').style.display = 'none';
  dropzone.style.display = 'flex';
});

document.getElementById('btn-start').addEventListener('click', async () => {
  const targets = state.files.filter(f => f.checked).map(f => f.id);
  if(!targets.length) return toast("请先勾选要处理的照片", "error");
  
  const apiKey = localStorage.getItem('pm_api_key');
  if(!apiKey) return toast("请先在右上角设置中配置 API Key", "error");

  const payload = {
    target_ids: targets,
    user_prompt: document.getElementById('set-user-prompt').value,
    parallel: parseInt(document.getElementById('set-parallel').value),
    max_tokens: parseInt(document.getElementById('set-max-tokens').value),
    strength: parseInt(document.getElementById('set-strength').value),
    api_base: localStorage.getItem('pm_api_base') || 'https://api.openai.com/v1',
    api_key: apiKey,
    model: localStorage.getItem('pm_api_model') || 'gpt-4o',
  };

  try {
    const res = await apiFetch('/api/tasks', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
    state.taskId = res.task_id;
    state.taskState = 'running';
    state.logOffset = 0;
    
    document.getElementById('btn-start').disabled = true;
    document.getElementById('btn-stop').disabled = false;
    document.getElementById('global-progress').style.display = 'flex';
    
    // 初始化状态
    targets.forEach(id => {
      const f = state.files.find(x=>x.id===id);
      if(f) f.status = 'pending';
    });
    renderGrid();

    pollTask();
  } catch(e) { toast(e.message, "error"); }
});

document.getElementById('btn-stop').addEventListener('click', async () => {
  if(!state.taskId) return;
  await apiFetch(`/api/tasks/${state.taskId}/stop`, { method: 'POST' });
  toast("正在停止...");
});

async function pollTask() {
  if(!state.taskId) return;
  try {
    const res = await apiFetch(`/api/tasks/${state.taskId}?since=${state.logOffset}`);
    state.logOffset = res.log_total;
    
    // 进度条
    const pct = res.total ? Math.round(res.completed / res.total * 100) : 0;
    document.getElementById('progress-fill').style.width = pct + '%';
    document.getElementById('progress-text').textContent = `${res.completed} / ${res.total}`;
    
    // 文件状态
    let needsRender = false;
    for(const [fid, st] of Object.entries(res.file_status)) {
      const f = state.files.find(x => x.id === fid);
      if(f && f.status !== st) { f.status = st; needsRender = true; }
    }
    if(needsRender) renderGrid();

    if(res.state === 'finished' || res.state === 'cancelled') {
      state.taskState = 'finished';
      document.getElementById('btn-start').disabled = false;
      document.getElementById('btn-stop').disabled = true;
      toast(res.state === 'finished' ? "全部处理完成" : "处理已停止", "success");
    } else {
      setTimeout(pollTask, 1000);
    }
  } catch(e) { console.error(e); state.taskState = 'finished'; }
}

// ================= 沉浸式预览 =================
function openPreview(file) {
  state.currentPreviewFile = file;
  const modal = document.getElementById('preview-modal');
  document.getElementById('modal-title').textContent = file.name;
  document.getElementById('modal-img').src = safeImageDataUrl(file.thumb);
  
  const btnDownload = document.getElementById('btn-download');
  const overlay = document.getElementById('modal-processing');
  
  if (file.status === 'done' && state.taskId) {
    btnDownload.disabled = false;
    overlay.style.display = 'none';
    // 此处可以做前后对比，但需要后端提供处理后的原图读取接口，这里暂且只展示缩略图+下载
  } else {
    btnDownload.disabled = true;
    overlay.style.display = file.status === 'running' ? 'flex' : 'none';
  }

  modal.classList.add('active');
}

document.getElementById('btn-close-modal').addEventListener('click', () => document.getElementById('preview-modal').classList.remove('active'));
document.getElementById('btn-download').addEventListener('click', () => {
  if(state.taskId && state.currentPreviewFile) {
    const tid = encodeURIComponent(state.taskId);
    const fid = encodeURIComponent(state.currentPreviewFile.id);
    window.open(`/api/tasks/${tid}/output/${fid}`, '_blank', 'noopener');
  }
});

// 监听任务完成时，如果 modal 开着，更新 modal 的下载按钮状态
const originalPoll = pollTask;
pollTask = async function() {
  await originalPoll();
  if (state.currentPreviewFile && state.currentPreviewFile.status === 'done') {
    document.getElementById('btn-download').disabled = false;
    document.getElementById('modal-processing').style.display = 'none';
  }
};
// ================= 照片评价模块 =================
let reviewState = {
  currentFileId: null,
  currentTaskId: null,
  pollInterval: null,
  history: []
};

// 切换评价页时按需加载历史
document.querySelector('[data-target="review"]').addEventListener('click', () => {
  loadReviewHistory();
});

function initReviewModule() {
  const dropzone = document.getElementById('review-dropzone');
  const fileInput = document.createElement('input');
  fileInput.type = 'file';
  fileInput.accept = '.jpg,.jpeg,.png,.webp,.cr2,.nef,.arw';
  
  document.getElementById('btn-review-pick').addEventListener('click', () => fileInput.click());
  document.getElementById('btn-review-change').addEventListener('click', () => fileInput.click());
  
  fileInput.addEventListener('change', async (e) => {
    if (!e.target.files.length) return;
    const file = e.target.files[0];
    
    // 上传文件
    const fd = new FormData();
    fd.append('files', file);
    try {
      document.getElementById('review-dropzone').style.display = 'none';
      document.getElementById('review-preview-box').style.display = 'block';
      document.getElementById('review-img').src = ''; // clear old
      
      const res = await apiFetch('/api/uploads', { method: 'POST', body: fd });
      const uploaded = res.files[0];
      
      reviewState.currentFileId = uploaded.id;
      document.getElementById('review-img').src = safeImageDataUrl(uploaded.thumb);
      document.getElementById('review-form').style.display = 'block';
      document.getElementById('review-result-card').style.display = 'none';
    } catch (err) {
      toast("上传失败: " + err.message, "error");
      document.getElementById('review-dropzone').style.display = 'flex';
    }
  });

  // 提交评价
  document.getElementById('btn-review-submit').addEventListener('click', async () => {
    if (!reviewState.currentFileId) return;
    const apiKey = localStorage.getItem('pm_api_key');
    if (!apiKey) return toast("请先在设置中配置 API Key", "error");

    const payload = {
      file_id: reviewState.currentFileId,
      description: document.getElementById('review-desc').value.trim(),
      api_base: localStorage.getItem('pm_api_base') || 'https://api.openai.com/v1',
      api_key: apiKey,
      model: localStorage.getItem('pm_api_model') || 'gpt-4o'
    };

    try {
      document.getElementById('review-form').style.display = 'none';
      document.getElementById('review-loading').style.display = 'flex';
      document.getElementById('review-result-card').style.display = 'none';

      const res = await apiFetch('/api/reviews', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      
      reviewState.currentTaskId = res.task_id;
      pollReviewResult();
    } catch (err) {
      toast(err.message, "error");
      document.getElementById('review-form').style.display = 'block';
      document.getElementById('review-loading').style.display = 'none';
    }
  });

  // 清空历史
  document.getElementById('btn-clear-history').addEventListener('click', async () => {
    if (!confirm("确定清空所有历史评价记录吗？")) return;
    try {
      await apiFetch('/api/reviews/history', { method: 'DELETE' });
      loadReviewHistory();
    } catch(e) { toast(e.message, "error"); }
  });
}

async function pollReviewResult() {
  if (!reviewState.currentTaskId) return;
  try {
    const res = await apiFetch(`/api/reviews/${reviewState.currentTaskId}`);
    if (res.state === 'done') {
      document.getElementById('review-loading').style.display = 'none';
      document.getElementById('review-result-card').style.display = 'block';
      document.getElementById('review-form').style.display = 'block';
      renderReviewResult(res.result);
      loadReviewHistory(); // 刷新列表
    } else if (res.state === 'error') {
      toast("评价失败: " + res.error, "error");
      document.getElementById('review-loading').style.display = 'none';
      document.getElementById('review-form').style.display = 'block';
    } else {
      setTimeout(pollReviewResult, 1500);
    }
  } catch (err) {
    setTimeout(pollReviewResult, 3000);
  }
}

function renderReviewResult(result) {
  result = result || {};
  document.getElementById('res-summary').textContent = result.summary || '';
  
  const ul = document.getElementById('res-suggestions');
  const suggestions = Array.isArray(result.suggestions) ? result.suggestions : [];
  // LLM 返回的内容不可信，必须转义后再拼接，避免被当作 HTML 执行
  ul.innerHTML = suggestions.map(s => `<li>${escapeHtml(s)}</li>`).join('');
  
  // 绘制雷达图
  drawRadarChart(result.scores || {});
}

function drawRadarChart(scores) {
  const canvas = document.getElementById('radar-chart');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const w = canvas.width, h = canvas.height;
  const cx = w / 2, cy = h / 2;
  const r = Math.min(w, h) / 2 - 50; // 半径留白
  
  ctx.clearRect(0, 0, w, h);
  
  const labels = Object.keys(scores);
  const n = labels.length;
  if (n === 0) return;
  const angleStep = (Math.PI * 2) / n;
  
  // 画网格
  ctx.strokeStyle = 'rgba(255,255,255,0.1)';
  ctx.lineWidth = 1;
  for (let level = 1; level <= 5; level++) {
    ctx.beginPath();
    for (let i = 0; i < n; i++) {
      const angle = i * angleStep - Math.PI / 2;
      const x = cx + Math.cos(angle) * r * (level / 5);
      const y = cy + Math.sin(angle) * r * (level / 5);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.closePath();
    ctx.stroke();
  }
  
  // 画轴线
  for (let i = 0; i < n; i++) {
    const angle = i * angleStep - Math.PI / 2;
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(cx + Math.cos(angle) * r, cy + Math.sin(angle) * r);
    ctx.stroke();
  }
  
  // 画数据区域
  ctx.beginPath();
  for (let i = 0; i < n; i++) {
    const val = Math.max(0, Math.min(100, scores[labels[i]] || 0));
    const angle = i * angleStep - Math.PI / 2;
    const dist = r * (val / 100);
    const x = cx + Math.cos(angle) * dist;
    const y = cy + Math.sin(angle) * dist;
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.closePath();
  
  // 填充与描边
  ctx.fillStyle = 'rgba(59, 130, 246, 0.3)'; // --primary with opacity
  ctx.fill();
  ctx.strokeStyle = '#3b82f6';
  ctx.lineWidth = 2;
  ctx.stroke();
  
  // 画数据点和文本
  ctx.fillStyle = '#fff';
  ctx.font = '12px Inter';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  
  for (let i = 0; i < n; i++) {
    const val = Math.max(0, Math.min(100, scores[labels[i]] || 0));
    const angle = i * angleStep - Math.PI / 2;
    const dist = r * (val / 100);
    const x = cx + Math.cos(angle) * dist;
    const y = cy + Math.sin(angle) * dist;
    
    // 点
    ctx.beginPath();
    ctx.arc(x, y, 3, 0, Math.PI * 2);
    ctx.fillStyle = '#3b82f6';
    ctx.fill();
    
    // 标签 (往外推一点)
    const tx = cx + Math.cos(angle) * (r + 24);
    const ty = cy + Math.sin(angle) * (r + 16);
    ctx.fillStyle = '#94a3b8';
    ctx.fillText(labels[i], tx, ty);
    ctx.fillStyle = '#fff';
    ctx.font = 'bold 13px Inter';
    ctx.fillText(`${val}`, tx, ty + 16);
    ctx.font = '12px Inter';
  }
}

async function loadReviewHistory() {
  const list = document.getElementById('review-history-list');
  try {
    const res = await apiFetch('/api/reviews/history');
    reviewState.history = res.items || [];
    
    if (reviewState.history.length === 0) {
      list.innerHTML = '<div class="empty-placeholder">暂无历史记录</div>';
      return;
    }
    
    list.innerHTML = reviewState.history.map((item, idx) => {
      item = item || {};
      const scores = item.scores || {};
      const scoreVals = Object.values(scores).filter(v => typeof v === 'number' && isFinite(v));
      const avg = scoreVals.length ? (scoreVals.reduce((a,b)=>a+b,0)/scoreVals.length).toFixed(1) : '-';
      // 提取文件名
      const rawPath = typeof item.image_path === 'string' ? item.image_path : '';
      const filename = rawPath.split(/[/\\]/).pop() || '未知图片';
      
      return `
        <div class="history-item" data-idx="${idx}">
          <div class="history-info">
            <div class="history-title">${escapeHtml(filename)}</div>
            <div class="history-date">${escapeHtml(item.timestamp)}</div>
          </div>
          <div class="history-score">${escapeHtml(avg)}</div>
        </div>
      `;
    }).join('');
    
    // 绑定点击查看历史
    document.querySelectorAll('.history-item').forEach(el => {
      el.addEventListener('click', () => {
        const idx = parseInt(el.dataset.idx);
        const item = reviewState.history[idx];
        
        // 渲染结果区
        document.getElementById('review-result-card').style.display = 'block';
        document.getElementById('review-loading').style.display = 'none';
        document.getElementById('review-preview-box').style.display = 'none'; // 隐藏图片预览区，因为历史上传可能没了
        document.getElementById('review-form').style.display = 'none';
        
        // 调整卡片位置：如果左边空，就把右边占满
        document.querySelector('.review-layout').style.gridTemplateColumns = '1fr';
        
        renderReviewResult(item);
      });
    });
    
  } catch (e) { console.error(e); }
}

// 在 DOMContentLoaded 中初始化
document.addEventListener('DOMContentLoaded', () => {
  initReviewModule();
});