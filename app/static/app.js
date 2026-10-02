/* 元器件物料管理 —— 前端逻辑(原生 JS,无构建、无依赖) */
'use strict';

/* ------------------------------------------------------------------ 工具 */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ESC_MAP[c]);
const num = (n) => (n === null || n === undefined ? '0' : String(n));

const KIND_LABEL = { IN: '入库', OUT: '出库', ADJUST: '盘点', TRANSFER: '移库' };
const STATE_LABEL = { ok: '充足', low: '偏低', out: '缺货' };

async function api(path, { method = 'GET', body, form } = {}) {
  const init = { method, headers: {} };
  if (form) {
    init.body = form;                       // FormData:让浏览器自己带 boundary
  } else if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json; charset=utf-8';
    init.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch {
    // 浏览器只在网络层失败时抛这个,几乎总是「服务端没在跑」
    throw new Error('连不上服务端。请确认「启动.bat」那个黑色命令行窗口还开着,'
                  + '然后按 F5 刷新本页再试。');
  }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { error: text }; }
  if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}`);
  return data;
}

function toast(msg, type = '') {
  const el = document.createElement('div');
  el.className = 'toast ' + type;
  el.textContent = msg;
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), type === 'err' ? 6000 : 2800);
}

const onEsc = (e) => { if (e.key === 'Escape') closeModal(); };

function openModal(html, { wide = false } = {}) {
  const root = $('#modal-root');
  root.innerHTML = `<div class="mask"><div class="modal${wide ? ' wide' : ''}">${html}</div></div>`;
  const mask = $('.mask', root);
  mask.addEventListener('mousedown', (e) => { if (e.target === mask) closeModal(); });
  document.addEventListener('keydown', onEsc);
  return root;
}

function closeModal() {
  $('#modal-root').innerHTML = '';
  document.removeEventListener('keydown', onEsc);
}

function badge(state) {
  return `<span class="badge ${state}">${STATE_LABEL[state] || state}</span>`;
}

/* ------------------------------------------------------------------ 状态 */

const state = {
  tab: 'components',
  filters: { q: '', category: '', state: '', sort: 'category' },
  meta: { categories: [], filters: { categories: [], packages: [], manufacturers: [] }, locations: [] },
  stockPick: null,          // 出入库表单选中的元件
  lastImport: null,
};

async function loadMeta() {
  try { state.meta = await api('/api/meta'); } catch (e) { /* 首启空库也能用 */ }
}

/* ------------------------------------------------------------------ 统计 */

async function loadStats() {
  let s;
  try { s = await api('/api/summary'); } catch { return; }
  $('#stats').innerHTML = `
    <div class="stat"><b>${num(s.components)}</b><span>元件种类</span></div>
    <div class="stat"><b>${num(s.total_qty)}</b><span>库存总数</span></div>
    <div class="stat warn"><b>${num(s.low)}</b><span>库存偏低</span></div>
    <div class="stat bad"><b>${num(s.out)}</b><span>缺货</span></div>
    <div class="stat"><b>${num(s.projects)}</b><span>项目</span></div>`;
}

/* ------------------------------------------------------------------ 库存台账 */

function categoryOptions(selected) {
  const list = state.meta.filters.categories.length
    ? state.meta.filters.categories : state.meta.categories;
  return `<option value="">全部品类</option>` +
    list.map((c) => `<option value="${esc(c)}"${c === selected ? ' selected' : ''}>${esc(c)}</option>`).join('');
}

async function renderComponents() {
  const f = state.filters;
  $('#view').innerHTML = `
    <div class="card">
      <div class="toolbar">
        <input type="search" id="f-q" class="grow" placeholder="搜索:名称 / 立创编号 / 厂家料号 / 厂家 / 封装 / 值" value="${esc(f.q)}">
        <select id="f-cat">${categoryOptions(f.category)}</select>
        <select id="f-state">
          <option value="">全部状态</option>
          <option value="ok"${f.state === 'ok' ? ' selected' : ''}>充足</option>
          <option value="low"${f.state === 'low' ? ' selected' : ''}>偏低</option>
          <option value="out"${f.state === 'out' ? ' selected' : ''}>缺货</option>
        </select>
        <select id="f-sort">
          <option value="category"${f.sort === 'category' ? ' selected' : ''}>按品类排序</option>
          <option value="qty"${f.sort === 'qty' ? ' selected' : ''}>库存从少到多</option>
          <option value="qty_desc"${f.sort === 'qty_desc' ? ' selected' : ''}>库存从多到少</option>
          <option value="value"${f.sort === 'value' ? ' selected' : ''}>按值排序</option>
          <option value="lcsc"${f.sort === 'lcsc' ? ' selected' : ''}>按立创编号</option>
          <option value="updated"${f.sort === 'updated' ? ' selected' : ''}>最近修改</option>
        </select>
        <button class="primary" id="btn-new">＋ 新建元件</button>
        <button class="ghost" id="btn-import">📥 导入 BOM</button>
      </div>
      <div id="comp-table"><div class="empty">加载中…</div></div>
    </div>`;

  const reload = () => { state.filters = { q: $('#f-q').value, category: $('#f-cat').value, state: $('#f-state').value, sort: $('#f-sort').value }; loadComponents(); };
  $('#f-cat').onchange = reload;
  $('#f-state').onchange = reload;
  $('#f-sort').onchange = reload;
  let timer;
  $('#f-q').oninput = () => { clearTimeout(timer); timer = setTimeout(reload, 250); };
  $('#btn-new').onclick = () => componentForm(null);
  $('#btn-import').onclick = bomImportDialog;

  loadComponents();
}

async function loadComponents() {
  const f = state.filters;
  const qs = new URLSearchParams();
  if (f.q) qs.set('q', f.q);
  if (f.category) qs.set('category', f.category);
  if (f.state) qs.set('state', f.state);
  qs.set('sort', f.sort);
  const box = $('#comp-table');
  if (!box) return;
  let data;
  try { data = await api('/api/components?' + qs); }
  catch (e) { box.innerHTML = `<div class="empty">加载失败:${esc(e.message)}</div>`; return; }

  if (!data.items.length) {
    box.innerHTML = `<div class="empty">没有匹配的元件。<br>点右上角「新建元件」手工添加,或「导入 BOM」从 Altium 的 BOM 表批量建库。</div>`;
    return;
  }
  box.innerHTML = `
    <div class="muted" style="margin-bottom:6px">共 <b>${data.total}</b> 条</div>
    <table>
      <thead><tr>
        <th>名称</th><th>立创编号</th><th>封装</th><th>值</th><th>品类</th><th>厂家</th>
        <th class="num">库存</th><th>状态</th><th class="nowrap">操作</th>
      </tr></thead>
      <tbody>${data.items.map(rowHtml).join('')}</tbody>
    </table>`;

  $$('tbody tr', box).forEach((tr) => {
    const id = Number(tr.dataset.id);
    tr.classList.add('clickable');
    tr.onclick = (e) => { if (!e.target.closest('button')) componentDetail(id); };
    const inc = $('button[data-act="in"]', tr);
    const out = $('button[data-act="out"]', tr);
    if (inc) inc.onclick = (e) => { e.stopPropagation(); moveForm(data.items.find((x) => x.id === id), 'IN'); };
    if (out) out.onclick = (e) => { e.stopPropagation(); moveForm(data.items.find((x) => x.id === id), 'OUT'); };
  });
}

function rowHtml(c) {
  return `<tr data-id="${c.id}">
    <td><b>${esc(c.name)}</b>${c.note ? `<div class="muted" style="font-size:12px">${esc(c.note)}</div>` : ''}</td>
    <td class="mono">${esc(c.lcsc_pn || '—')}</td>
    <td>${esc(c.package || '—')}</td>
    <td>${esc(c.value || '—')}</td>
    <td><span class="tag">${esc(c.category || '其他')}</span></td>
    <td class="muted">${esc(c.manufacturer || '—')}</td>
    <td class="num"><b>${num(c.on_hand)}</b>${c.unit && c.unit !== '个' ? ' ' + esc(c.unit) : ''}</td>
    <td>${badge(c.stock_state)}${c.min_stock ? `<div class="muted" style="font-size:11.5px">安全 ${c.min_stock}</div>` : ''}</td>
    <td class="nowrap">
      <button class="sm ghost" data-act="in">＋入库</button>
      <button class="sm ghost" data-act="out">－出库</button>
    </td>
  </tr>`;
}

/* ------------------------------------------------------------------ 元件详情 */

async function componentDetail(id) {
  let c;
  try { c = await api('/api/components/' + id); }
  catch (e) { return toast(e.message, 'err'); }

  const locs = c.stock_by_location.length
    ? c.stock_by_location.map((l) => `<span class="tag" style="margin-right:6px">${esc(l.code)} · <b>${num(l.qty)}</b></span>`).join('')
    : '<span class="muted">暂无库存</span>';

  const movs = c.movements.length ? `
    <table><thead><tr><th>时间</th><th>动作</th><th class="num">数量</th><th>仓位</th><th>项目 / 单据</th></tr></thead>
    <tbody>${c.movements.map((m) => `<tr>
      <td class="nowrap muted">${esc(m.created_at)}</td>
      <td>${KIND_LABEL[m.kind] || m.kind}</td>
      <td class="num">${m.kind === 'OUT' ? '−' : m.kind === 'IN' ? '+' : ''}${num(m.qty)}</td>
      <td class="mono">${esc(m.location_code || '—')}${m.to_location_code ? ' → ' + esc(m.to_location_code) : ''}</td>
      <td>${esc(m.project_name || m.ref || '—')}${m.note ? `<div class="muted" style="font-size:12px">${esc(m.note)}</div>` : ''}</td>
    </tr>`).join('')}</tbody></table>` : '<div class="muted">还没有出入库记录</div>';

  openModal(`
    <div class="modal-head">
      <h2>${esc(c.name)}</h2>${badge(c.stock_state)}
      <button class="ghost sm x" onclick="closeModal()">关闭</button>
    </div>
    <div class="modal-body">
      <div class="summary-line">
        <span>现有库存 <b>${num(c.on_hand)}</b></span>
        <span>安全库存 <b>${num(c.min_stock)}</b></span>
        <span>品类 <b>${esc(c.category || '其他')}</b></span>
        ${c.lcsc_pn ? `<span>立创 <b class="mono">${esc(c.lcsc_pn)}</b></span>` : ''}
      </div>
      <div class="grid">
        <div><label>厂家料号</label><div class="mono">${esc(c.mpn || '—')}</div></div>
        <div><label>厂家</label><div>${esc(c.manufacturer || '—')}</div></div>
        <div><label>封装</label><div>${esc(c.package || '—')}</div></div>
        <div><label>值</label><div>${esc(c.value || '—')}</div></div>
      </div>
      <h3>仓位分布</h3>${locs}
      <h3>最近流水</h3>${movs}
    </div>
    <div class="modal-foot">
      <button class="ghost" onclick="closeModal()">关闭</button>
      <button class="ghost" id="d-edit">编辑</button>
      <button class="ghost" id="d-out">－出库</button>
      <button class="primary" id="d-in">＋入库</button>
    </div>`, { wide: true });

  $('#d-in').onclick = () => { closeModal(); moveForm(c, 'IN'); };
  $('#d-out').onclick = () => { closeModal(); moveForm(c, 'OUT'); };
  $('#d-edit').onclick = () => { closeModal(); componentForm(c); };
}

/* ------------------------------------------------------------------ 元件表单 */

function componentForm(c) {
  const isNew = !c;
  c = c || { category: '其他', unit: '个', min_stock: 0, params: {} };
  const cats = state.meta.categories.length ? state.meta.categories : ['其他'];
  openModal(`
    <div class="modal-head"><h2>${isNew ? '新建元件' : '编辑元件'}</h2>
      <button class="ghost sm x" onclick="closeModal()">关闭</button></div>
    <div class="modal-body">
      <div class="grid">
        <div class="full"><label>名称 *</label><input id="c-name" value="${esc(c.name || '')}" placeholder="例如 10kΩ 0603"></div>
        <div><label>立创编号(LCSC C-号)</label><input id="c-lcsc" class="mono" value="${esc(c.lcsc_pn || '')}" placeholder="C2907002"></div>
        <div><label>厂家料号 MPN</label><input id="c-mpn" value="${esc(c.mpn || '')}"></div>
        <div><label>厂家</label><input id="c-mfr" value="${esc(c.manufacturer || '')}"></div>
        <div><label>品类</label><select id="c-cat">${cats.map((x) => `<option${x === (c.category || '其他') ? ' selected' : ''}>${esc(x)}</option>`).join('')}</select></div>
        <div><label>值</label><input id="c-value" value="${esc(c.value || '')}" placeholder="10kΩ / 100nF"></div>
        <div><label>封装</label><input id="c-pkg" value="${esc(c.package || '')}" placeholder="0603 / SOT-23-5"></div>
        <div><label>单位</label><input id="c-unit" value="${esc(c.unit || '个')}"></div>
        <div><label>安全库存(低于则预警)</label><input id="c-min" type="number" min="0" value="${num(c.min_stock)}"></div>
        <div class="full"><label>参数(JSON,可选,例如耐压/精度/功率)</label>
          <input id="c-params" class="mono" value="${esc(JSON.stringify(c.params || {}))}" placeholder='{"耐压":"50V","精度":"±1%"}'></div>
        <div class="full"><label>备注</label><input id="c-note" value="${esc(c.note || '')}"></div>
      </div>
    </div>
    <div class="modal-foot">
      ${isNew ? '' : '<button class="danger" id="c-del">删除</button>'}
      <button class="ghost" onclick="closeModal()">取消</button>
      <button class="primary" id="c-save">保存</button>
    </div>`);

  $('#c-save').onclick = async () => {
    let params = {};
    const raw = $('#c-params').value.trim();
    if (raw) { try { params = JSON.parse(raw); } catch { return toast('参数不是合法 JSON', 'err'); } }
    const payload = {
      name: $('#c-name').value.trim(), lcsc_pn: $('#c-lcsc').value.trim() || null,
      mpn: $('#c-mpn').value.trim() || null, manufacturer: $('#c-mfr').value.trim() || null,
      category: $('#c-cat').value, value: $('#c-value').value.trim() || null,
      package: $('#c-pkg').value.trim() || null, unit: $('#c-unit').value.trim() || '个',
      min_stock: Number($('#c-min').value || 0), params, note: $('#c-note').value.trim() || null,
    };
    if (!payload.name) return toast('名称不能为空', 'err');
    try {
      if (isNew) await api('/api/components', { method: 'POST', body: payload });
      else await api('/api/components/' + c.id, { method: 'PUT', body: payload });
      closeModal(); toast('已保存', 'ok'); refresh();
    } catch (e) { toast(e.message, 'err'); }
  };

  if (!isNew) {
    $('#c-del').onclick = async () => {
      if (!confirm(`确定删除「${c.name}」?相关库存与流水会一并删除。`)) return;
      try {
        await api(`/api/components/${c.id}?force=1`, { method: 'DELETE' });
        closeModal(); toast('已删除', 'ok'); refresh();
      } catch (e) { toast(e.message, 'err'); }
    };
  }
}

/* ------------------------------------------------------------------ 出入库 */

function locationOptions(selected) {
  return state.meta.locations.map((l) =>
    `<option value="${esc(l.code)}"${l.code === selected ? ' selected' : ''}>${esc(l.code)}</option>`).join('');
}

async function moveForm(c, kind) {
  await loadMeta();
  kind = kind || 'IN';
  openModal(`
    <div class="modal-head"><h2>出入库登记</h2>
      <button class="ghost sm x" onclick="closeModal()">关闭</button></div>
    <div class="modal-body">
      <div class="grid">
        <div class="full"><label>元件 *</label>
          <input id="m-pick" class="grow" placeholder="输入名称或立创编号搜索" value="${c ? esc(c.name) : ''}">
          <div id="m-results"></div>
          <input type="hidden" id="m-cid" value="${c ? c.id : ''}">
        </div>
        <div><label>动作 *</label><select id="m-kind">
          ${Object.entries(KIND_LABEL).map(([k, v]) => `<option value="${k}"${k === kind ? ' selected' : ''}>${v}</option>`).join('')}
        </select></div>
        <div><label>数量 *</label><input id="m-qty" type="number" min="0" value="1"></div>
        <div><label>仓位</label>
          <input id="m-loc" list="loc-list" value="${state.meta.locations[0] ? esc(state.meta.locations[0].code) : '未分类'}">
          <datalist id="loc-list">${locationOptions()}</datalist>
        </div>
        <div id="m-toloc-wrap" style="display:none"><label>目标仓位(移库)</label>
          <input id="m-toloc" list="loc-list2"><datalist id="loc-list2">${locationOptions()}</datalist></div>
        <div><label>单据号 / 备注</label><input id="m-ref" placeholder="可留空"></div>
      </div>
      <div class="muted" style="margin-top:10px;font-size:12.5px">
        盘点:数量填「实际清点到的数」,系统按差额自动调整并留痕。
      </div>
    </div>
    <div class="modal-foot">
      <button class="ghost" onclick="closeModal()">取消</button>
      <button class="primary" id="m-save">提交</button>
    </div>`);

  const syncKind = () => { $('#m-toloc-wrap').style.display = $('#m-kind').value === 'TRANSFER' ? '' : 'none'; };
  $('#m-kind').onchange = syncKind; syncKind();

  const pick = $('#m-pick');
  const results = $('#m-results');
  let timer;
  const search = async () => {
    const q = pick.value.trim();
    if (q.length < 1) { results.innerHTML = ''; return; }
    try {
      const d = await api('/api/components?q=' + encodeURIComponent(q) + '&limit=8');
      results.innerHTML = d.items.map((x) =>
        `<div class="tag" style="cursor:pointer;margin:4px 6px 0 0;display:inline-block" data-id="${x.id}" data-name="${esc(x.name)}">
           ${esc(x.name)} <span class="mono">${esc(x.lcsc_pn || '')}</span> · 存${num(x.on_hand)}</div>`).join('')
        || '<div class="muted" style="margin-top:6px">没找到</div>';
      $$('[data-id]', results).forEach((el) => {
        el.onclick = () => { $('#m-cid').value = el.dataset.id; pick.value = el.dataset.name; results.innerHTML = ''; };
      });
    } catch (e) { results.innerHTML = `<div class="muted">${esc(e.message)}</div>`; }
  };
  pick.oninput = () => { clearTimeout(timer); timer = setTimeout(search, 220); if (c && pick.value !== c.name) $('#m-cid').value = ''; };
  if (c) search();

  $('#m-save').onclick = async () => {
    const cid = Number($('#m-cid').value);
    if (!cid) return toast('请先从搜索结果里选择元件', 'err');
    const payload = {
      kind: $('#m-kind').value, component_id: cid, qty: Number($('#m-qty').value || 0),
      location: $('#m-loc').value.trim() || '未分类', ref: $('#m-ref').value.trim() || null,
    };
    if (payload.kind === 'TRANSFER') payload.to_location = $('#m-toloc').value.trim();
    try {
      const r = await api('/api/stock/move', { method: 'POST', body: payload });
      closeModal(); toast(`已登记,该仓位现有 ${r.qty_at_location},总库存 ${r.on_hand}`, 'ok'); refresh();
    } catch (e) { toast(e.message, 'err'); }
  };
}

/* ------------------------------------------------------------------ 出入库页 */

async function renderStock() {
  await loadMeta();
  let low = { items: [] };
  try { low = await api('/api/lowstock'); } catch { /* ignore */ }

  $('#view').innerHTML = `
    <div class="card">
      <h2>快捷出入库</h2>
      <div class="toolbar">
        <button class="primary" id="s-in">＋ 入库</button>
        <button class="ghost" id="s-out">－ 出库</button>
        <button class="ghost" id="s-adj">📋 盘点</button>
        <button class="ghost" id="s-tr">↔ 移库</button>
      </div>
      <div class="muted">也可以直接在「库存台账」里点某一行的 ＋入库 / －出库。</div>
    </div>

    <div class="card">
      <h2>仓位管理</h2>
      <div class="toolbar">
        <input id="loc-new" placeholder="新仓位编码,例如 A-01-02">
        <button class="ghost" id="loc-add">添加仓位</button>
      </div>
      <div id="loc-list">${state.meta.locations.map((l) =>
        `<span class="tag" style="margin:0 6px 6px 0;display:inline-block">${esc(l.code)}
          <a href="#" data-del="${l.id}" style="color:#c02a2a;text-decoration:none;margin-left:4px">×</a></span>`).join('')
        || '<span class="muted">还没有仓位</span>'}</div>
    </div>

    <div class="card">
      <h2>低库存 / 缺货预警 <span class="tag">${low.items.length}</span></h2>
      <div id="low-table">${low.items.length ? `
        <table><thead><tr><th>名称</th><th>立创编号</th><th>封装</th>
          <th class="num">库存</th><th class="num">安全库存</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>${low.items.map((c) => `<tr>
          <td><b>${esc(c.name)}</b></td><td class="mono">${esc(c.lcsc_pn || '—')}</td>
          <td>${esc(c.package || '—')}</td>
          <td class="num"><b>${num(c.on_hand)}</b></td><td class="num muted">${num(c.min_stock)}</td>
          <td>${badge(c.stock_state)}</td>
          <td><button class="sm ghost" data-in="${c.id}">＋入库</button></td>
        </tr>`).join('')}</tbody></table>` : '<div class="empty">没有低库存元件 👍</div>'}
      </div>
    </div>`;

  $('#s-in').onclick = () => moveForm(null, 'IN');
  $('#s-out').onclick = () => moveForm(null, 'OUT');
  $('#s-adj').onclick = () => moveForm(null, 'ADJUST');
  $('#s-tr').onclick = () => moveForm(null, 'TRANSFER');

  $$('[data-in]').forEach((b) => {
    b.onclick = async () => {
      const c = await api('/api/components/' + b.dataset.in);
      moveForm(c, 'IN');
    };
  });

  $('#loc-add').onclick = async () => {
    const code = $('#loc-new').value.trim();
    if (!code) return;
    try { await api('/api/locations', { method: 'POST', body: { code } }); toast('已添加', 'ok'); await loadMeta(); renderStock(); }
    catch (e) { toast(e.message, 'err'); }
  };
  $$('[data-del]').forEach((a) => {
    a.onclick = async (e) => {
      e.preventDefault();
      try { await api('/api/locations/' + a.dataset.del, { method: 'DELETE' }); await loadMeta(); renderStock(); }
      catch (err) { toast(err.message, 'err'); }
    };
  });
}

/* ------------------------------------------------------------------ 项目 BOM */

async function renderProjects() {
  let d;
  try { d = await api('/api/projects'); } catch (e) { return toast(e.message, 'err'); }

  $('#view').innerHTML = `
    <div class="card">
      <div class="toolbar">
        <h2 style="margin:0" class="grow">项目 BOM</h2>
        <button class="ghost" id="p-new">＋ 空项目</button>
        <button class="primary" id="p-import">📥 导入 BOM 建项目</button>
      </div>
      ${d.items.length ? `
      <table><thead><tr><th>项目</th><th>板名/编号</th><th class="num">BOM 行数</th>
        <th class="num">总需求</th><th>创建时间</th><th>操作</th></tr></thead>
      <tbody>${d.items.map((p) => `<tr>
        <td><b>${esc(p.name)}</b>${p.repo ? `<div class="muted" style="font-size:12px">${esc(p.repo)}</div>` : ''}</td>
        <td class="mono">${esc(p.code || '—')}</td>
        <td class="num">${num(p.bom_lines)}</td><td class="num">${num(p.required_qty)}</td>
        <td class="muted nowrap">${esc(p.created_at)}</td>
        <td><button class="sm ghost" data-open="${p.id}">查看 BOM</button>
            <button class="sm danger" data-pdel="${p.id}">删除</button></td>
      </tr>`).join('')}</tbody></table>`
      : '<div class="empty">还没有项目。点「导入 BOM 建项目」上传 Altium 导出的 BOM 表,自动建元件库并算出缺料。</div>'}
    </div>`;

  $$('[data-open]').forEach((b) => { b.onclick = () => projectDetail(Number(b.dataset.open)); });
  $$('[data-pdel]').forEach((b) => {
    b.onclick = async () => {
      if (!confirm('删除该项目?(元件与库存不受影响)')) return;
      try { await api('/api/projects/' + b.dataset.pdel, { method: 'DELETE' }); toast('已删除', 'ok'); refresh(); }
      catch (e) { toast(e.message, 'err'); }
    };
  });
  $('#p-new').onclick = () => {
    openModal(`<div class="modal-head"><h2>新建空项目</h2><button class="ghost sm x" onclick="closeModal()">关闭</button></div>
      <div class="modal-body"><div class="grid">
        <div class="full"><label>项目名称 *</label><input id="np-name" placeholder="例如 FMUv6X 载板"></div>
        <div><label>板名/编号</label><input id="np-code" placeholder="Board1_PCB1"></div>
        <div><label>仓库地址(可选)</label><input id="np-repo"></div>
      </div></div>
      <div class="modal-foot"><button class="ghost" onclick="closeModal()">取消</button>
      <button class="primary" id="np-save">创建</button></div>`);
    $('#np-save').onclick = async () => {
      const name = $('#np-name').value.trim();
      if (!name) return toast('名称不能为空', 'err');
      try {
        await api('/api/projects', { method: 'POST', body: { name, code: $('#np-code').value.trim() || null, repo: $('#np-repo').value.trim() || null } });
        closeModal(); toast('已创建', 'ok'); refresh();
      } catch (e) { toast(e.message, 'err'); }
    };
  };
  $('#p-import').onclick = bomImportDialog;
}

async function projectDetail(pid) {
  let d;
  try { d = await api(`/api/projects/${pid}/bom`); } catch (e) { return toast(e.message, 'err'); }
  const p = d.project;

  const rows = d.lines.map((l) => {
    const ds = (l.designators || '').split(',').filter(Boolean);
    const short = ds.length > 6 ? ds.slice(0, 6).join(',') + ` +${ds.length - 6}` : ds.join(',');
    return `<tr>
      <td><b>${esc(l.name)}</b><div class="muted mono" style="font-size:12px">${esc(l.lcsc_pn || l.mpn || '')}</div></td>
      <td>${esc(l.package || '—')}</td>
      <td class="num">${num(l.required_qty)}</td>
      <td class="muted" style="font-size:12px">${esc(short)}</td>
      <td class="num">${num(l.on_hand)}</td>
      <td class="num" style="color:${l.gap ? 'var(--out)' : 'var(--ok)'};font-weight:600">${l.gap ? '缺 ' + l.gap : '齐'}</td>
    </tr>`;
  }).join('');

  openModal(`
    <div class="modal-head"><h2>${esc(p.name)}</h2>
      <span class="tag">${esc(p.code || '未设编号')}</span>
      <button class="ghost sm x" onclick="closeModal()">关闭</button></div>
    <div class="modal-body">
      ${d.ready
        ? '<div class="okbox">✅ 库存齐套:这份 BOM 需要的料都够。</div>'
        : `<div class="warnbox">⚠️ 缺 <b>${d.shortage_lines}</b> 种料,合计缺口 <b>${d.shortage_qty}</b> 个。红色行是缺料项。</div>`}
      <div class="summary-line">
        <span>BOM 行数 <b>${d.line_count}</b></span>
        <span>缺料种类 <b>${d.shortage_lines}</b></span>
        <span>缺口总数 <b>${d.shortage_qty}</b></span>
      </div>
      <table><thead><tr><th>元件</th><th>封装</th><th class="num">需求</th>
        <th>位号</th><th class="num">现有</th><th class="num">缺口</th></tr></thead>
      <tbody>${rows}</tbody></table>
    </div>
    <div class="modal-foot">
      <button class="danger" id="pd-del">删除项目</button>
      <button class="ghost" id="pd-export">导出缺料 CSV</button>
      <button class="ghost" onclick="closeModal()">关闭</button>
      <button class="primary" id="pd-pick">按 BOM 领料出库</button>
    </div>`, { wide: true });

  $('#pd-del').onclick = async () => {
    if (!confirm('删除该项目?')) return;
    await api('/api/projects/' + pid, { method: 'DELETE' });
    closeModal(); toast('已删除', 'ok'); refresh();
  };
  $('#pd-export').onclick = () => exportCsv(p.name, d.lines);
  $('#pd-pick').onclick = async () => {
    if (!confirm('按这份 BOM 从库存批量出库?库存不足的料会被跳过并列出来。')) return;
    try {
      const r = await api(`/api/projects/${pid}/pick`, { method: 'POST', body: {} });
      const okN = r.picked.length, badN = r.failed.length;
      closeModal();
      toast(`领料完成:成功 ${okN} 项${badN ? `,${badN} 项库存不足` : ''}`, badN ? 'err' : 'ok');
      if (badN) {
        openModal(`<div class="modal-head"><h2>以下料号库存不足,未能领料</h2>
          <button class="ghost sm x" onclick="closeModal()">关闭</button></div>
          <div class="modal-body"><ul>${r.failed.map((f) => `<li>元件 #${f.component_id}:需要 ${f.qty} —— ${esc(f.reason)}</li>`).join('')}</ul></div>`);
      }
      refresh();
    } catch (e) { toast(e.message, 'err'); }
  };
}

function exportCsv(projectName, lines) {
  const head = ['立创编号', '厂家料号', '名称', '品类', '封装', '需求', '现有', '缺口', '位号'];
  const esc2 = (v) => `"${String(v ?? '').replace(/"/g, '""')}"`;
  const body = lines.map((l) => [l.lcsc_pn, l.mpn, l.name, l.category, l.package,
    l.required_qty, l.on_hand, l.gap, l.designators].map(esc2).join(','));
  const csv = '\uFEFF' + [head.map(esc2).join(','), ...body].join('\r\n');
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
  a.download = `${projectName}_缺料清单.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

/* ------------------------------------------------------------------ 导入 BOM */

function bomImportDialog() {
  openModal(`
    <div class="modal-head"><h2>导入 BOM</h2>
      <button class="ghost sm x" onclick="closeModal()">关闭</button></div>
    <div class="modal-body">
      <div class="drop" id="b-drop">
        把 Altium 导出的 <b>.xlsx</b> 拖到这里,或点击选择文件
        <input type="file" id="b-file" accept=".xlsx,.xlsm" style="display:none">
      </div>
      <div class="grid" style="margin-top:14px">
        <div><label>项目名称(留空则用文件名)</label><input id="b-name" placeholder="例如 FMUv6X 载板"></div>
        <div><label>板名 / 编号</label><input id="b-code" placeholder="Board1_PCB1"></div>
        <div class="full"><label>仓库地址(可选)</label><input id="b-repo" placeholder="https://github.com/..."></div>
      </div>
      <div id="b-result" style="margin-top:14px"></div>
    </div>
    <div class="modal-foot">
      <button class="ghost" onclick="closeModal()">取消</button>
      <button class="primary" id="b-go" disabled>导入</button>
    </div>`, { wide: true });

  let file = null;
  const drop = $('#b-drop'), input = $('#b-file'), go = $('#b-go'), result = $('#b-result');
  const setFile = (f) => {
    file = f;
    drop.innerHTML = `已选择:<b>${esc(f.name)}</b> <span class="muted">(${(f.size / 1024).toFixed(1)} KB)</span>`;
    go.disabled = false;
    if (!$('#b-name').value) $('#b-name').value = f.name.replace(/\.(xlsx|xlsm)$/i, '');
  };
  drop.onclick = () => input.click();
  input.onchange = () => { if (input.files[0]) setFile(input.files[0]); };
  ['dragenter', 'dragover'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', (e) => { const f = e.dataTransfer.files[0]; if (f) setFile(f); });

  go.onclick = async () => {
    if (!file) return;
    const fd = new FormData();
    fd.append('file', file);
    fd.append('project_name', $('#b-name').value.trim());
    fd.append('project_code', $('#b-code').value.trim());
    fd.append('repo', $('#b-repo').value.trim());
    go.disabled = true; go.textContent = '导入中…';
    try {
      const r = await api('/api/bom/import', { method: 'POST', form: fd });
      const s = r.shortage;
      result.innerHTML = `
        <div class="okbox">导入成功:项目「${esc(r.project_name)}」共 <b>${r.bom_lines}</b> 行,
          新建元件 <b>${r.components_created}</b> 个,复用已有 <b>${r.components_reused}</b> 个,
          总需求 <b>${r.total_qty}</b> 个。</div>
        ${r.warnings && r.warnings.length ? `<div class="warnbox">${r.warnings.map(esc).join('<br>')}</div>` : ''}
        <div class="${s.ready ? 'okbox' : 'warnbox'}">
          ${s.ready ? '✅ 这份 BOM 库存齐套' : `⚠️ 缺 <b>${s.shortage_lines}</b> 种料,合计缺口 <b>${s.shortage_qty}</b> 个`}
        </div>`;
      go.textContent = '完成';
      await loadStats();
    } catch (e) {
      result.innerHTML = `<div class="warnbox">导入失败:${esc(e.message)}</div>`;
      go.disabled = false; go.textContent = '重试';
    }
  };
}

/* ------------------------------------------------------------------ 流水 */

async function renderMovements() {
  let d;
  try { d = await api('/api/movements?limit=300'); } catch (e) { return toast(e.message, 'err'); }
  $('#view').innerHTML = `
    <div class="card">
      <h2>出入库流水 <span class="tag">最近 ${d.items.length} 条</span></h2>
      ${d.items.length ? `
      <table><thead><tr><th>时间</th><th>动作</th><th>元件</th><th class="num">数量</th>
        <th>仓位</th><th>项目 / 单据</th><th>操作人</th></tr></thead>
      <tbody>${d.items.map((m) => `<tr>
        <td class="muted nowrap">${esc(m.created_at)}</td>
        <td>${KIND_LABEL[m.kind] || m.kind}</td>
        <td><b>${esc(m.component_name)}</b><div class="mono muted" style="font-size:12px">${esc(m.lcsc_pn || m.component_mpn || '')}</div></td>
        <td class="num">${m.kind === 'OUT' ? '−' : m.kind === 'IN' ? '+' : ''}${num(m.qty)}</td>
        <td class="mono">${esc(m.location_code || '—')}${m.to_location_code ? ' → ' + esc(m.to_location_code) : ''}</td>
        <td>${esc(m.project_name || m.ref || '—')}${m.note ? `<div class="muted" style="font-size:12px">${esc(m.note)}</div>` : ''}</td>
        <td class="muted">${esc(m.operator || '')}</td>
      </tr>`).join('')}</tbody></table>`
      : '<div class="empty">还没有任何出入库记录</div>'}
    </div>`;
}

/* ------------------------------------------------------------------ 主循环 */

const RENDER = {
  components: renderComponents,
  stock: renderStock,
  projects: renderProjects,
  movements: renderMovements,
};

async function refresh() {
  await Promise.all([loadStats(), (RENDER[state.tab] || renderComponents)()]);
}

$$('#tabs button').forEach((b) => {
  b.onclick = () => {
    state.tab = b.dataset.tab;
    $$('#tabs button').forEach((x) => x.classList.toggle('active', x === b));
    refresh();
  };
});

window.closeModal = closeModal;   // 供内联 onclick 调用

/* 心跳:告诉本地服务「界面还开着」。
   窗口一关请求就停了,便携版的服务会空闲超时自动退出 —— 靠这个收尾,
   不依赖任何浏览器进程判断,Edge 换成默认浏览器也一样成立。 */
setInterval(() => { fetch('/api/ping', { cache: 'no-store' }).catch(() => {}); }, 5000);

(async function init() {
  await loadMeta();
  await refresh();
})();
