/* ============================================================
   PhotoMagic Web 前端逻辑
   ============================================================ */

const API = '';
let state = {
  files: [],           // {id, name, path, thumb, checked, group, status}
  groups: {},          // {groupName: [fileId, ...]}
  taskId: null,
  taskState: 'idle',   // idle / running / finished
  reviewTaskId: null,
  profiles: [],
  rtVersions: [],
  settings: {},
};

// ---------- 工具 ----------
const $ = sel => document.querySelector(sel);
const $$ = sel => document.querySelectorAll(sel);

async function fetchJSON(url, options = {}) {
  const res = await fetch(url, options);
  if (!res.ok) {
    const err = await res.text();
    throw new Error(err || `HTTP ${res.status}`);
  }
  return res.json();
}

function showToast(msg, type = 'info') {
  // 简单实现，实际可替换为更漂亮的 Toast
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3000);
}

// ---------- 页面切换 ----------
$$('.nav-item').forEach(item => {
  item.addEventListener('click', (e) => {
    e.preventDefault();
    const page = item.dataset.page;
    
    // 侧栏高亮
    $$('.nav-item').forEach(n => n.classList.remove('active'));
    item.classList.add('active');
    
    // 页面切换
    $$('.page').forEach(p => p.classList.remove('active'));
    $(`#page-${page}`).classList.add('active');
    
    // 面包屑
    $('#crumb-current').textContent = item.querySelector('span').textContent;
    
    // 右栏 Inspector 切换
    $$('.inspector-page').forEach(p => p.classList.remove('active'));
    const insp = $(`.inspector-page[data-page="${page}"]`);
    if (insp) insp.classList.add('active');
  });
});

// ---------- 初始化 ----------
async function init() {
  try {
    state.settings = await fetchJSON('/api/settings');
    applySettingsToUI();
    
    const sys = await fetchJSON('/api/system');
    renderSystemInfo(sys);
    updateStatusPills(sys);
    
    await loadProfiles();
    await loadRtVersions();
    await loadReviewHistory();
    
    // 加载默认 Prompt
    const prompts = await fetchJSON('/api/prompts/default');
    $('#user-prompt').value = prompts.user_prompt;
    
    bindEvents();
  } catch (e) {
    showToast('初始化失败: ' + e.message, 'error');
  }
}

// ---------- 拖拽上传 ----------
const dropZone = $('#drop-zone');
dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.style.borderColor = 'var(--accent)'; });
dropZone.addEventListener('dragleave', e => { e.preventDefault(); dropZone.style.borderColor = ''; });
dropZone.addEventListener('drop', async e => {
  e.preventDefault();
  dropZone.style.borderColor = '';
  if (e.dataTransfer.files.length) {
    await uploadFiles(e.dataTransfer.files);
  }
});

$('#btn-add-files').addEventListener('click', () => {
  const input = document.createElement('input');
  input.type = 'file';
  input.multiple = true;
  input.accept = '.cr2,.nef,.arw,.dng,.orf,.raf,.rw2,.pef,.jpg,.jpeg,.png';
  input.onchange = async () => {
    if (input.files.length) await uploadFiles(input.files);
  };
  input.click();
});

async function uploadFiles(files) {
  const formData = new FormData();
  for (let i = 0; i < files.length; i++) {
    formData.append('files', files[i]);
  }
  
  try {
    dropZone.querySelector('.dropzone-title').textContent = '上传中...';
    const res = await fetchJSON('/api/uploads', { method: 'POST', body: formData });
    
    res.files.forEach(f => {
      state.files.push({
        id: f.id, name: f.name, path: f.path, thumb: f.thumb,
        checked: true, group: null, status: 'pending'
      });
    });
    
    renderFileList();
    dropZone.querySelector('.dropzone-title').textContent = '拖拽 RAW 文件到这里';
  } catch (e) {
    showToast('上传失败: ' + e.message, 'error');
  }
}

// ---------- 文件列表渲染 ----------
function renderFileList() {
  const ul = $('#file-list');
  ul.innerHTML = '';
  
  $('#files-sub').textContent = state.files.length ? `共 ${state.files.length} 个文件` : '尚未添加文件';
  
  state.files.forEach(f => {
    const li = document.createElement('li');
    li.className = `file-item ${f.checked ? 'selected' : ''}`;
    
    // 组标
    let groupTag = f.group ? `<span class="file-status" style="color:var(--accent)">${f.group}</span>` : '';
    
    // 状态标
    let statusText = '待处理';
    if (f.status === 'running') statusText = '处理中';
    if (f.status === 'done') statusText = '完成';
    if (f.status === 'failed') statusText = '失败';
    
    li.innerHTML = `
      <div class="file-check">${f.checked ? '✓' : ''}</div>
      <img class="file-thumb" src="${f.thumb || ''}" alt="">
      <div class="file-name">${f.name} ${groupTag}</div>
      <div class="file-status">${statusText}</div>
    `;
    
    li.addEventListener('click', (e) => {
      if (e.target.closest('.file-check')) {
        f.checked = !f.checked;
        renderFileList();
        updateGroupHint();
      } else {
        selectPreviewFile(f);
      }
    });
    
    ul.appendChild(li);
  });
  
  updateGroupHint();
}

// ---------- 分组逻辑 ----------
$('#btn-group').addEventListener('click', () => {
  const checked = state.files.filter(f => f.checked);
  if (!checked.length) return showToast('请先勾选文件');
  
  const name = prompt('组名：', `组 ${Object.keys(state.groups).length + 1}`);
  if (!name) return;
  
  checked.forEach(f => {
    f.group = name;
    if (!state.groups[name]) state.groups[name] = [];
    if (!state.groups[name].includes(f.id)) state.groups[name].push(f.id);
  });
  
  renderFileList();
});

$('#btn-ungroup').addEventListener('click', () => {
  const checked = state.files.filter(f => f.checked);
  checked.forEach(f => {
    if (f.group) {
      state.groups[f.group] = state.groups[f.group].filter(id => id !== f.id);
      if (!state.groups[f.group].length) delete state.groups[f.group];
      f.group = null;
    }
  });
  renderFileList();
});

function updateGroupHint() {
  const checked = state.files.filter(f => f.checked);
  const grouped = {};
  checked.forEach(f => {
    if (f.group) {
      if (!grouped[f.group]) grouped[f.group] = 0;
      grouped[f.group]++;
    }
  });
  
  const parts = Object.entries(grouped).map(([n, c]) => `${n}(${c}张)`);
  $('#group-hint').textContent = parts.length ? `组图批次：${parts.join(', ')}` : '按单张处理';
}

// ---------- 预览 ----------
let previewFile = null;
function selectPreviewFile(f) {
  previewFile = f;
  $('#preview-placeholder').style.display = 'none';
  $('#preview-tag').textContent = f.name;
  
  const canvas = $('#preview-canvas');
  const ctx = canvas.getContext('2d');
  const img = new Image();
  img.onload = () => {
    canvas.width = img.width;
    canvas.height = img.height;
    ctx.drawImage(img, 0, 0);
  };
  img.src = f.thumb;
}

// ---------- 任务控制 ----------
$('#btn-start').addEventListener('click', async () => {
  const targetIds = state.files.filter(f => f.checked).map(f => f.id);
  if (!targetIds.length) return showToast('请勾选要处理的文件');
  
  const payload = {
    target_ids: targetIds,
    groups: state.groups,
    unify: $('#inspector-unify').checked,
    user_prompt: $('#user-prompt').value,
    output_dir: $('#inspector-output-dir').value,
    output_format: ['jpg', 'tiff', 'png'][$('#inspector-format').value],
    bit_depth: $('#inspector-bit-depth').value,
    jpeg_quality: parseInt($('#inspector-quality').value),
    parallel: parseInt($('#inspector-parallel').value),
    strength: parseInt($('#inspector-strength').value) / 100,
    max_tokens: parseInt($('#inspector-max-tokens').value),
    profile_id: state.profiles.find(p => p.active)?.id || '',
  };
  
  try {
    const res = await fetchJSON('/api/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    
    state.taskId = res.task_id;
    state.taskState = 'running';
    updateTaskButtons();
    pollTask();
  } catch (e) {
    showToast('启动失败: ' + e.message, 'error');
  }
});

$('#btn-stop').addEventListener('click', async () => {
  if (!state.taskId) return;
  await fetchJSON(`/api/tasks/${state.taskId}/stop`, { method: 'POST' });
  showToast('正在停止...');
});

function updateTaskButtons() {
  const running = state.taskState === 'running';
  $('#btn-start').disabled = running;
  $('#btn-stop').disabled = !running;
  $('#btn-retry').disabled = running;
}

let logOffset = 0;
async function pollTask() {
  if (!state.taskId) return;
  
  try {
    const res = await fetchJSON(`/api/tasks/${state.taskId}?since=${logOffset}`);
    
    // 更新日志
    if (res.new_lines.length) {
      const logEl = $('#log');
      res.new_lines.forEach(line => {
        logEl.textContent += line + '\n';
      });
      logEl.scrollTop = logEl.scrollHeight;
      logOffset = res.log_total;
    }
    
    // 更新进度
    const pct = res.total ? Math.round(res.completed / res.total * 100) : 0;
    $('#progress-bar').style.width = pct + '%';
    $('#progress-text').textContent = `${res.completed} / ${res.total}`;
    
    // 更新文件状态
    for (const [fid, status] of Object.entries(res.file_status)) {
      const file = state.files.find(f => f.id === fid);
      if (file) file.status = status;
    }
    renderFileList();
    
    if (res.state === 'finished' || res.state === 'cancelled') {
      state.taskState = 'finished';
      updateTaskButtons();
      showToast(res.state === 'finished' ? '处理完成' : '已停止', 'success');
    } else {
      setTimeout(pollTask, 1000);
    }
  } catch (e) {
    console.error(e);
    state.taskState = 'finished';
    updateTaskButtons();
  }
}

// ---------- 设置与配置 ----------
async function loadProfiles() {
  const res = await fetchJSON('/api/profiles');
  state.profiles = res.profiles;
  const select = $('#profile-select');
  select.innerHTML = '';
  res.profiles.forEach(p => {
    const opt = document.createElement('option');
    opt.value = p.id;
    opt.textContent = `${p.name} ${p.active ? '(当前)' : ''}`;
    select.appendChild(opt);
  });
  if (res.active) select.value = res.active;
}

async function loadRtVersions() {
  const res = await fetchJSON('/api/rt-versions');
  state.rtVersions = res.versions;
  const select = $('#rt-select');
  select.innerHTML = '';
  res.versions.forEach(v => {
    const opt = document.createElement('option');
    opt.value = v.id;
    opt.textContent = `${v.name} ${v.active ? '(当前)' : ''}`;
    select.appendChild(opt);
  });
  if (res.active) select.value = res.active;
  $('#rt-status').textContent = `实际调用：${res.resolved || '未找到'}`;
}

function applySettingsToUI() {
  const s = state.settings;
  $('#inspector-unify').checked = s.unify_style;
  $('#inspector-thinking').checked = s.thinking_enabled;
  $('#inspector-parallel').value = s.parallel;
  $('#inspector-strength').value = s.strength;
  $('#inspector-strength-value').textContent = `${s.strength}%`;
  $('#inspector-quality').value = s.jpeg_quality;
  $('#inspector-output-dir').value = s.output_dir;
  $('#inspector-max-tokens').value = s.max_tokens;
}

$('#inspector-strength').addEventListener('input', e => {
  $('#inspector-strength-value').textContent = `${e.target.value}%`;
});

// 实时保存设置
async function saveSettings() {
  const payload = {
    unify_style: $('#inspector-unify').checked,
    thinking_enabled: $('#inspector-thinking').checked,
    parallel: parseInt($('#inspector-parallel').value),
    strength: parseInt($('#inspector-strength').value),
    jpeg_quality: parseInt($('#inspector-quality').value),
    output_dir: $('#inspector-output-dir').value,
    max_tokens: parseInt($('#inspector-max-tokens').value),
  };
  await fetchJSON('/api/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
}
['#inspector-unify', '#inspector-thinking', '#inspector-parallel', '#inspector-strength', '#inspector-quality', '#inspector-output-dir', '#inspector-max-tokens'].forEach(sel => {
  $(sel).addEventListener('change', saveSettings);
});

// ---------- 渲染系统信息 ----------
function renderSystemInfo(sys) {
  const el = $('#system-info');
  el.innerHTML = `
    <dt>版本</dt><dd>${sys.version}</dd>
    <dt>配置目录</dt><dd>${sys.config_dir}</dd>
    <dt>日志目录</dt><dd>${sys.log_dir}</dd>
    <dt>本次日志</dt><dd>${sys.log_file || '未落盘'}</dd>
    <dt>钥匙串</dt><dd>${sys.keyring_available ? '可用' : '不可用'}</dd>
    <dt>RawTherapee</dt><dd>${sys.rt_cli || '未找到'}</dd>
  `;
}

function updateStatusPills(sys) {
  const apiPill = $('#pill-api');
  const rtPill = $('#pill-rt');
  
  // 简单判断
  apiPill.className = `status-pill ${sys.keyring_available ? 'on' : 'off'}`;
  rtPill.className = `status-pill ${sys.rt_cli ? 'on' : 'off'}`;
}

// ---------- 初始化 ----------
document.addEventListener('DOMContentLoaded', init);