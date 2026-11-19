// Xorora Prospector, team member portal.
// Each person signs in with their own name, work email and password and sees
// only their own campaigns, plus a read-only list of teammates' campaigns.

const SESSION_KEY = 'xorora.member.session';
const INVITE_KEY = 'xorora.member.invite';
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c =>
  ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const store = {
  get(k) { try { return localStorage.getItem(k) || ''; } catch { return ''; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch {} },
  del(k) { try { localStorage.removeItem(k); } catch {} },
};

(function takeInvite() {
  const hash = location.hash || '';
  if (!hash) return;
  const q = new URLSearchParams(hash.slice(1));
  const code = q.get('admin') || q.get('invite');
  history.replaceState(null, '', location.pathname);
  if (code && /^[A-Za-z0-9_-]{16,80}$/.test(code)) {
    store.set(INVITE_KEY, code);
    store.set(INVITE_KEY + '.admin', q.get('admin') ? '1' : '');
  }
})();

async function call(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  const s = store.get(SESSION_KEY);
  if (s) headers['X-Member-Session'] = s;
  if (opts.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
  let res;
  try { res = await fetch(path, Object.assign({}, opts, {headers})); }
  catch { return {success: false, error: 'Can’t reach the server. Check your connection.'}; }
  let data = {};
  try { data = await res.json(); } catch {}
  if (res.status === 401 && data.error === 'signin') { store.del(SESSION_KEY); showAuth('signin'); return null; }
  if (!res.ok && data.success === undefined) data.success = false;
  return data;
}

// ── Sign in / create account ──

function showAuth(mode) {
  const canJoin = !!store.get(INVITE_KEY);
  if (mode === 'signup' && !canJoin) mode = 'signin';
  $('m-user').innerHTML = '';
  $('m-app').innerHTML =
    '<div class="m-auth"><div class="m-auth-card">' +
      (store.get(INVITE_KEY + '.admin') ? '<span class="owner-chip" style="align-self:flex-start">Admin invite</span>' : '') +
      '<h1>' + (mode === 'signup' ? 'Create your account' : 'Welcome back') + '</h1>' +
      '<p class="muted">' + (mode === 'signup'
        ? 'Your campaigns go out under your name. Only you can see and change them.'
        : 'Sign in to run your campaigns.') + '</p>' +
      (mode === 'signup' ? field('a-name', 'Your name', 'text', 'e.g. Sadia Saif', 'name') : '') +
      field('a-email', 'Work email', 'email', 'you@xorora.com', 'email') +
      field('a-pass', 'Password', 'password', mode === 'signup' ? 'At least 8 characters' : '',
        mode === 'signup' ? 'new-password' : 'current-password') +
      '<button class="btn btn-primary m-wide" id="a-go">' + (mode === 'signup' ? 'Create account' : 'Sign in') + '</button>' +
      '<div class="m-err" id="a-err"></div>' +
      (canJoin ? '<p class="m-switch">' + (mode === 'signup'
        ? 'Already have an account? <a href="#" data-mode="signin">Sign in</a>'
        : 'New here? <a href="#" data-mode="signup">Create your account</a>') + '</p>'
        : '<p class="m-switch muted">New to the team? Open the team link you were sent to create your account.</p>') +
    '</div></div>';
  const go = async () => {
    const btn = $('a-go'); btn.disabled = true; $('a-err').textContent = '';
    const body = {email: $('a-email').value, password: $('a-pass').value};
    if (mode === 'signup') body.name = $('a-name').value;
    if (store.get(INVITE_KEY)) body.invite = store.get(INVITE_KEY);
    const r = await call('/api/member/' + (mode === 'signup' ? 'signup' : 'login'),
      {method: 'POST', body: JSON.stringify(body)});
    btn.disabled = false;
    if (!r || !r.success) { $('a-err').textContent = (r && (r.error || r.message)) || 'That didn’t work. Try again.'; return; }
    store.set(SESSION_KEY, r.session);
    if (store.get(INVITE_KEY + '.admin')) { store.del(INVITE_KEY + '.admin'); store.del(INVITE_KEY); }
    boot();
  };
  $('a-go').onclick = go;
  $('m-app').querySelectorAll('input').forEach(i => i.onkeydown = e => { if (e.key === 'Enter') go(); });
  $('m-app').querySelectorAll('[data-mode]').forEach(a => a.onclick = e => { e.preventDefault(); showAuth(a.dataset.mode); });
  const first = $('m-app').querySelector('input'); if (first) first.focus();
}

function field(id, label, type, placeholder, auto) {
  return '<label class="m-field"><span>' + label + '</span><input id="' + id + '" type="' + type +
    '" class="team-input" placeholder="' + esc(placeholder) + '" autocomplete="' + auto + '"></label>';
}

// ── Chip inputs with suggestions ──

function chipInput(id, values, suggestions, placeholder) {
  const state = {values: [...values], suggestions};
  chips[id] = state;
  return '<div class="chips" id="' + id + '"><div class="chip-row"></div>' +
    '<input class="chip-entry" placeholder="' + esc(placeholder) + '" autocomplete="off">' +
    '<ul class="chip-menu" hidden></ul></div>';
}
const chips = {};

function wireChips(id) {
  const box = $(id), st = chips[id];
  const row = box.querySelector('.chip-row'), input = box.querySelector('.chip-entry'),
        menu = box.querySelector('.chip-menu');
  let active = -1, shown = [];
  const render = () => {
    row.innerHTML = st.values.map((v, i) =>
      '<span class="chip">' + esc(v) + '<button type="button" aria-label="Remove ' + esc(v) + '" data-i="' + i + '">×</button></span>').join('');
    row.querySelectorAll('button').forEach(b => b.onclick = () => { st.values.splice(+b.dataset.i, 1); render(); });
    input.placeholder = st.values.length ? 'Add more…' : input.dataset.ph;
  };
  input.dataset.ph = input.placeholder;
  const add = v => {
    v = (v || '').trim().replace(/\s+/g, ' ');
    if (v && !st.values.some(x => x.toLowerCase() === v.toLowerCase()) && st.values.length < 20) st.values.push(v);
    input.value = ''; hide(); render(); input.focus();
  };
  const hide = () => { menu.hidden = true; active = -1; };
  const show = () => {
    const q = input.value.trim().toLowerCase();
    shown = st.suggestions.filter(s => !st.values.some(v => v.toLowerCase() === s.toLowerCase()) &&
      (!q || s.toLowerCase().includes(q))).slice(0, 8);
    if (!shown.length) { hide(); return; }
    menu.innerHTML = shown.map((s, i) => '<li data-i="' + i + '" class="' + (i === active ? 'on' : '') + '">' +
      highlight(s, q) + '</li>').join('');
    menu.querySelectorAll('li').forEach(li => li.onmousedown = e => { e.preventDefault(); add(shown[+li.dataset.i]); });
    menu.hidden = false;
  };
  input.oninput = () => { active = -1; show(); };
  input.onfocus = show;
  input.onblur = () => setTimeout(() => { if (input.value.trim()) add(input.value); hide(); }, 120);
  input.onkeydown = e => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (menu.hidden) show();
      active = Math.max(-1, Math.min(shown.length - 1, active + (e.key === 'ArrowDown' ? 1 : -1)));
      show();
    } else if (e.key === 'Enter' || e.key === ',' || e.key === ';' || e.key === 'Tab') {
      if (e.key === 'Tab' && !input.value.trim() && active < 0) return;
      e.preventDefault();
      const pick = active >= 0 ? shown[active] : (!menu.hidden && shown.length && input.value.trim() ? shown[0] : input.value);
      add(pick);
    } else if (e.key === 'Backspace' && !input.value && st.values.length) {
      st.values.pop(); render();
    } else if (e.key === 'Escape') hide();
  };
  box.onclick = e => { if (e.target === box || e.target === row) input.focus(); };
  st.set = vals => { st.values = [...vals]; render(); };
  render();
}

function highlight(s, q) {
  if (!q) return esc(s);
  const i = s.toLowerCase().indexOf(q);
  return i < 0 ? esc(s) : esc(s.slice(0, i)) + '<b>' + esc(s.slice(i, i + q.length)) + '</b>' + esc(s.slice(i + q.length));
}

// ── The portal ──

let me = null, opts = null, refreshTimer = null;

async function boot() {
  if (!store.get(SESSION_KEY)) { showAuth(store.get(INVITE_KEY) ? 'signup' : 'signin'); return; }
  const r = await call('/api/member/me');
  if (!r || !r.success) return;
  me = r.member;
  // Admins work in the full dashboard: take them straight there.
  // Everyone works in the full dashboard. #team-members opens the people list for admins.
  if (location.hash !== '#team-members') { openAdmin(); return; }
  history.replaceState(null, '', location.pathname);
  $('m-user').innerHTML = '<span class="m-avatar">' + esc(me.name.charAt(0).toUpperCase()) + '</span>' +
    '<span class="m-who"><b>' + esc(me.name) + (me.role === 'admin' ? ' <em class="m-role">Admin</em>' : '') +
      '</b><small>' + esc(me.email) + '</small></span>' +
    (me.role === 'admin' ? '<button class="btn btn-secondary btn-sm" id="m-admin">Admin dashboard</button>' : '') +
    '<button class="btn btn-secondary btn-sm" id="m-out">Sign out</button>';
  $('m-out').onclick = async () => {
    await call('/api/member/logout', {method: 'POST'});
    store.del(SESSION_KEY); store.del('harvey-dashboard-token'); showAuth('signin');
  };
  if ($('m-admin')) $('m-admin').onclick = openAdmin;
  opts = await call('/api/member/options');
  if (!opts) return;
  renderPortal();
  if (me.role === 'admin') loadPeople();
  loadCampaigns();
  clearInterval(refreshTimer);
  refreshTimer = setInterval(loadCampaigns, 15000);
}

function renderPortal() {
  const services = opts.services || [];
  const acc = opts.accounts || {};
  let mailbox;
  if (!acc.connected) mailbox = '<div class="team-hint">' + esc(acc.message || 'Instantly isn’t connected yet. Ask your admin.') + '</div>';
  else {
    const byDomain = {};
    (acc.accounts || []).forEach(a => (byDomain[a.domain] = byDomain[a.domain] || []).push(a));
    mailbox = '<div id="f-account" class="mbox-list">' +
      Object.entries(byDomain).map(([d, list]) => '<div class="mbox-group"><div class="mbox-domain">' + esc(d) + '</div>' +
        list.map(a => '<label class="mbox"><input type="checkbox" name="f-acc" value="' + esc(a.email) + '"> <span>' + esc(a.email) + '</span></label>').join('') +
        '</div>').join('') + '</div>' +
      '<small class="team-hint">Tick several mailboxes and Instantly rotates between them, so the campaign sends more.</small>';
  }
  const sg = opts.suggest || {};
  $('m-app').innerHTML =
    '<div class="m-head"><h1>Hi ' + esc(me.name.split(' ')[0]) + '</h1>' +
      '<p class="muted">Start a campaign and Xorora Prospector finds the companies, finds the decision maker’s own email, ' +
      'writes to them as you and sends from your mailbox.</p></div>' +
    '<div class="m-grid">' +
    '<section class="m-card m-form">' +
      '<div class="m-form-head"><h2 id="f-title">New campaign</h2><button type="button" class="btn btn-ghost btn-sm" id="f-cancel" hidden>Cancel editing</button></div>' +
      '<div class="m-step"><span>1</span>What you’re offering</div>' +
      '<label class="m-field"><span>Service</span><select id="f-service" class="team-input">' +
        '<option value="">Choose a service…</option>' +
        services.map(s => '<option value="' + esc(s.name) + '">' + esc(s.name) + '</option>').join('') + '</select>' +
        '<small class="team-hint" id="f-pitch"></small></label>' +
      '<div class="m-reco" id="f-reco" hidden></div>' +
      '<div class="m-step"><span>2</span>Who you’re targeting</div>' +
      '<label class="m-field"><span>Industries</span>' + chipInput('f-ind', [], sg.industries || [], 'Type an industry, e.g. dental…') +
        '<small class="team-hint">Pick from the list for the best results.</small></label>' +
      '<label class="m-field"><span>Cities or regions</span>' + chipInput('f-reg', [], sg.regions || [], 'Type a city, e.g. Miami…') +
        '<small class="team-hint">A state or country searches its main cities.</small></label>' +
      '<div class="m-two">' +
        '<label class="m-field"><span>Company size</span><select id="f-head" class="team-input">' +
          '<option value="">Any size</option>' + (opts.headcounts || []).map(h => '<option value="' + esc(h) + '">' + esc(h) + ' employees</option>').join('') +
        '</select></label>' +
        '<label class="m-field"><span>Contacts to reach</span><input id="f-leads" type="number" min="1" max="2000" value="100" class="team-input"></label>' +
      '</div>' +
      '<label class="m-field"><span>Who to email (persona)</span>' + chipInput('f-titles', [], sg.titles || [], 'e.g. CEO, Founder…') +
        '<small class="team-hint">Their own mailbox first. When it can’t be found, the business inbox (info@, contact@).</small></label>' +
      '<div class="m-step"><span>3</span>How it sends</div>' +
      '<div class="m-two">' +
        '<label class="m-field"><span>Send from (Instantly)</span>' + mailbox + '</label>' +
        '<label class="m-field"><span>Emails per day, per mailbox</span><input id="f-daily" type="number" min="1" max="30" value="30" class="team-input">' +
          '<small class="team-hint">30 a mailbox keeps your domain healthy. 3 mailboxes at 30 send up to 90 a day.</small></label>' +
      '</div>' +
      '<label class="m-field"><span>Campaign name <em>(optional)</em></span><input id="f-name" class="team-input" placeholder="Filled in from service, industry and city"></label>' +
      '<div class="m-actions"><button class="btn btn-primary" id="f-go">Start campaign</button><span class="m-msg" id="f-msg"></span></div>' +
    '</section>' +
    '<section class="m-side">' +
      '<div class="m-card"><div class="m-card-head"><h2>My campaigns</h2><span class="m-agent" id="m-agent"></span></div><div id="m-mine" class="m-list"><p class="muted">Loading…</p></div></div>' +
      '<div class="m-card"><div class="m-card-head"><h2>Team campaigns</h2><small class="muted">Read only</small></div><div id="m-team" class="m-list"></div></div>' +
      (me.role === 'admin' ? '<div class="m-card"><div class="m-card-head"><h2>Team members</h2><small class="muted">Admins only</small></div><div id="m-people" class="m-list"></div></div>' : '') +
    '</section></div>';
  ['f-ind', 'f-reg', 'f-titles'].forEach(wireChips);
  $('f-service').onchange = () => applyService($('f-service').value);
  $('f-go').onclick = startCampaign;
  $('f-cancel').onclick = resetForm;
}

let editing = null, mineById = {};

function resetForm() {
  editing = null;
  $('f-title').textContent = 'New campaign';
  $('f-go').textContent = 'Start campaign';
  $('f-cancel').hidden = true;
  $('f-service').value = ''; applyService('');
  ['f-ind', 'f-reg', 'f-titles'].forEach(id => chips[id].set([]));
  $('f-head').value = ''; $('f-leads').value = 100; $('f-daily').value = 30; $('f-name').value = '';
  document.querySelectorAll('input[name="f-acc"]').forEach(x => { x.checked = false; });
  $('f-msg').textContent = '';
  document.querySelector('.m-form').classList.remove('editing');
}

function startEdit(c) {
  editing = c.id;
  $('f-title').textContent = 'Edit campaign';
  $('f-go').textContent = 'Save changes';
  $('f-cancel').hidden = false;
  $('f-service').value = c.service || ''; applyService(c.service || '');
  chips['f-ind'].set(c.industries || []); chips['f-reg'].set(c.regions || []); chips['f-titles'].set(c.titles || []);
  $('f-head').value = c.headcount || ''; $('f-leads').value = c.max_leads || 100;
  $('f-daily').value = c.daily_limit || 30; $('f-name').value = c.name || '';
  document.querySelectorAll('input[name="f-acc"]').forEach(x => { x.checked = (c.accounts || []).includes(x.value); });
  $('f-msg').textContent = 'Editing “' + c.name + '”. It keeps running while you change it.';
  const form = document.querySelector('.m-form');
  form.classList.add('editing');
  form.scrollIntoView({behavior: 'smooth', block: 'start'});
}

function applyService(name) {
  const svc = (opts.services || []).find(s => s.name === name);
  $('f-pitch').textContent = svc ? svc.pitch || '' : '';
  const reco = $('f-reco');
  if (!svc || !svc.icp) { reco.hidden = true; return; }
  const icp = svc.icp;
  const group = (label, field, items) => !items.length ? '' :
    '<div class="m-sug"><span>' + label + '</span><div>' + items.map(v =>
      '<button type="button" class="m-sug-chip" data-f="' + field + '" data-v="' + esc(v) + '">+ ' + esc(v) + '</button>').join('') +
    '</div></div>';
  reco.hidden = false;
  reco.innerHTML = '<b>Suggested for ' + esc(svc.name.split(' (')[0]) + '</b>' +
    '<small>Click what fits your campaign. Nothing is added until you choose it.</small>' +
    group('Decision makers', 'f-titles', icp.titles || []) +
    group('Industries', 'f-ind', icp.industries || []) +
    group('Cities', 'f-reg', icp.regions || []) +
    (icp.headcount ? group('Company size', 'f-head', [icp.headcount + ' employees']) : '');
  reco.querySelectorAll('.m-sug-chip').forEach(b => b.onclick = () => {
    const f = b.dataset.f, v = b.dataset.v;
    if (f === 'f-head') $('f-head').value = v.replace(' employees', '');
    else { const st = chips[f]; if (!st.values.some(x => x.toLowerCase() === v.toLowerCase())) { st.values.push(v); st.set(st.values); } }
    b.classList.add('on'); b.textContent = '✓ ' + v;
  });
}

async function startCampaign() {
  const msg = $('f-msg'), btn = $('f-go');
  const v = id => ($(id) || {}).value || '';
  const body = {
    service: v('f-service'), accounts: [...document.querySelectorAll('input[name="f-acc"]:checked')].map(x => x.value),
    industries: chips['f-ind'].values, regions: chips['f-reg'].values, titles: chips['f-titles'].values,
    headcount: v('f-head'), max_leads: parseInt(v('f-leads') || '100', 10),
    daily_limit: Math.max(1, Math.min(30, parseInt(v('f-daily') || '30', 10) || 30)), name: v('f-name'),
  };
  if (!body.service) { msg.textContent = 'Pick the service to offer.'; return; }
  if (!body.industries.length) { msg.textContent = 'Add at least one industry.'; return; }
  if (!body.regions.length) { msg.textContent = 'Add at least one city or region.'; return; }
  if (!body.titles.length) { msg.textContent = 'Add who to email, e.g. CEO or Founder.'; return; }
  if (!body.accounts.length) { msg.textContent = 'Tick at least one mailbox to send from.'; return; }
  btn.disabled = true; msg.textContent = editing ? 'Saving…' : 'Starting…';
  const r = editing
    ? await call('/api/member/campaign/' + encodeURIComponent(editing) + '/edit', {method: 'POST', body: JSON.stringify(body)})
    : await call('/api/member/campaign', {method: 'POST', body: JSON.stringify(body)});
  btn.disabled = false;
  if (!r || r.success === false) { msg.textContent = (r && (r.error || r.message)) || 'Couldn’t save it. Try again.'; return; }
  if (editing) {
    resetForm();
    $('f-msg').textContent = r.retargeted ? 'Saved. It’s searching for your new targets now.' : 'Saved. Changes apply right away.';
  } else {
    msg.textContent = 'Started. It’s finding companies now.';
    chips['f-reg'].set([]); $('f-name').value = '';
  }
  loadCampaigns();
}

function pill(c) {
  if (c.status === 'stopped') return '<span class="live-pill paused"><i></i>Paused</span>';
  if (c.status === 'complete') return '<span class="live-pill done"><i></i>Done</span>';
  if (c.discovery === 'failed') return '<span class="live-pill warn"><i></i>Needs attention</span>';
  return '<span class="live-pill live"><i></i>Live</span>';
}
function stage(c) {
  if (c.status !== 'active') return '';
  if (c.discovery === 'queued') return 'Waiting to search';
  if (c.discovery === 'running') return 'Finding companies';
  if (!c.contacts) return 'Finding decision makers';
  if ((c.written || 0) === 0) return 'Writing emails';
  return 'Sending';
}
function personName(n) {
  n = String(n || '').trim();
  if (!n.includes('@')) return n;
  return n.split('@')[0].split(/[._-]+/).filter(Boolean).map(w => w[0].toUpperCase() + w.slice(1)).join(' ');
}

async function loadCampaigns() {
  const r = await call('/api/member/campaigns');
  if (!r || !r.success) return;
  const agent = $('m-agent');
  if (agent) agent.innerHTML = r.agent_running
    ? '<span class="live-pill live"><i></i>Agent running</span>'
    : '<span class="live-pill paused"><i></i>Agent stopped</span>';
  mineById = {}; r.mine.forEach(c => { mineById[c.id] = c; });
  $('m-mine').innerHTML = r.mine.length ? r.mine.map(c => {
    const pct = Math.min(100, Math.round(100 * (c.written || 0) / Math.max(1, c.max_leads || 1)));
    return '<article class="m-camp">' +
      '<div class="m-camp-top"><div><strong>' + esc(c.name) + '</strong>' +
        '<small>' + esc(c.service) + '</small></div>' + pill(c) + '</div>' +
      '<div class="m-meta">' + esc((c.industries || []).join(', ')) + ' · ' + esc((c.regions || []).join(', ')) +
        (c.headcount ? ' · ' + esc(c.headcount) + ' employees' : '') + '</div>' +
      '<div class="m-meta">Emailing ' + esc((c.titles || []).join(', ') || 'decision makers') +
        ' · from ' + esc((c.accounts || []).join(', ')) + ' · ' + (c.daily_limit || 30) * Math.max(1, (c.accounts || []).length) + '/day' + (c.timezone ? ' · sends 9am to 5pm ' + c.timezone : '') + '</div>' +
      '<div class="m-funnel">' +
        '<div><b>' + (c.companies || 0) + '</b><span>companies</span></div>' +
        '<div><b>' + (c.contacts || 0) + '</b><span>decision makers</span></div>' +
        '<div><b>' + (c.ready || 0) + '</b><span>ready</span></div>' +
        '<div><b>' + (c.written || 0) + '<em>/' + (c.max_leads || 0) + '</em></b><span>emailed</span></div>' +
      '</div>' +
      '<div class="m-bar"><i style="width:' + pct + '%"></i></div>' +
      (stage(c) ? '<div class="m-stage">' + esc(stage(c)) + '…</div>' : '') +
      (c.instantly_stats && c.instantly_stats.sent ? '<div class="m-note">In Instantly: ' + c.instantly_stats.sent + ' sent · ' + Math.round(100 * c.instantly_stats.opened / c.instantly_stats.sent) + '% opened · ' + c.instantly_stats.replied + ' replies · ' + (Math.round(1000 * c.instantly_stats.bounced / c.instantly_stats.sent) / 10) + '% bounced</div>' : '') +
      (c.stop_reason ? '<div class="m-note" style="color:var(--danger,#d93025);font-weight:600">' + esc(c.stop_reason) + '</div>' : '') +
      (c.discovery_note && c.discovery === 'failed' ? '<div class="m-note">' + esc(c.discovery_note) + '</div>' : '') +
      '<div class="m-camp-actions">' +
        (c.status === 'active'
          ? '<button class="btn btn-secondary btn-sm" data-a="stop" data-id="' + c.id + '">Pause</button>'
          : '<button class="btn btn-secondary btn-sm" data-a="resume" data-id="' + c.id + '">Resume</button>') +
        '<button class="btn btn-secondary btn-sm" data-a="find-more" data-id="' + c.id + '"' +
          (c.discovery === 'running' || c.discovery === 'queued' ? ' disabled' : '') + '>Find more</button>' +
        '<button class="btn btn-secondary btn-sm" data-a="limit" data-id="' + c.id + '" data-limit="' + (c.daily_limit || 30) + '">Emails/day</button>' +
        '<button class="btn btn-secondary btn-sm" data-a="edit" data-id="' + c.id + '">Edit</button>' +
        '<button class="btn btn-secondary btn-sm" data-a="contacts" data-id="' + c.id + '">Contacts</button>' +
        '<button class="btn btn-ghost btn-sm" data-a="delete" data-id="' + c.id + '">Delete</button>' +
      '</div><div class="m-contacts" id="ct-' + c.id + '" hidden></div></article>';
  }).join('') : '<p class="m-empty">No campaigns yet. Start your first one on the left.</p>';
  $('m-mine').querySelectorAll('[data-a]').forEach(b => b.onclick = () => campaignAction(b));
  $('m-team').innerHTML = r.team.length ? r.team.map(c =>
    '<div class="m-row"><div class="m-row-main"><span class="owner-chip">' + esc(personName(c.owner)) + '</span>' +
      '<strong>' + esc(c.name) + '</strong><small>' + esc(c.service) + '</small>' +
      '<small>' + esc((c.industries || []).join(', ')) + ' · ' + esc((c.regions || []).join(', ')) +
        (c.headcount ? ' · ' + esc(c.headcount) + ' employees' : '') + '</small>' +
      '</div>' +
      pill(c) + '</div>').join('')
    : '<p class="m-empty">No one else has a campaign yet.</p>';
}

async function campaignAction(b) {
  const id = b.dataset.id, a = b.dataset.a;
  if (a === 'contacts') return toggleContacts(id);
  if (a === 'edit') return mineById[id] && startEdit(mineById[id]);
  let body;
  if (a === 'delete' && !confirm('Delete this campaign? Emails already sent stay sent.')) return;
  if (a === 'limit') {
    const raw = prompt('Emails per day for each mailbox (1 to 30). 30 keeps your domain healthy.', b.dataset.limit);
    if (raw === null) return;
    const n = parseInt(raw, 10);
    if (!(n >= 1 && n <= 50)) { alert('Pick a number from 1 to 50.'); return; }
    body = JSON.stringify({daily_limit: n});
  }
  b.disabled = true;
  await call('/api/member/campaign/' + encodeURIComponent(id) + '/' + a, {method: 'POST', body});
  loadCampaigns();
}

async function toggleContacts(id) {
  const box = $('ct-' + id);
  if (!box.hidden) { box.hidden = true; return; }
  box.hidden = false; box.innerHTML = '<p class="muted">Loading…</p>';
  const r = await call('/api/member/campaign/' + encodeURIComponent(id) + '/contacts');
  const rows = (r && r.contacts) || [];
  box.innerHTML = rows.length ? '<table><thead><tr><th>Name</th><th>Title</th><th>Company</th><th>Email</th><th>Status</th></tr></thead><tbody>' +
    rows.map(p => '<tr><td>' + esc([p.first_name, p.last_name].filter(Boolean).join(' ')) + '</td><td>' + esc(p.title) +
      '</td><td>' + esc(p.company) + '</td><td>' + esc(p.email) + '</td><td>' + esc(p.status) + '</td></tr>').join('') +
    '</tbody></table>' : '<p class="muted">No decision makers found yet. They appear here as they’re found.</p>';
}

boot();


// ── Admins ──

async function openAdmin() {
  const r = await call('/api/member/admin-token');
  if (!r || !r.success) { alert((r && r.error) || 'Couldn’t open the admin dashboard.'); return; }
  store.set('harvey-dashboard-token', r.token);
  store.set('xorora.teamName', me.name);
  location.href = '/';
}

async function loadPeople() {
  const box = $('m-people');
  if (!box) return;
  const r = await call('/api/member/people');
  if (!r || !r.success) return;
  box.innerHTML = r.people.map(p =>
    '<div class="m-row"><div class="m-row-main"><strong>' + esc(p.name) +
      (p.role === 'admin' ? ' <em class="m-role">Admin</em>' : '') + '</strong><small>' + esc(p.email) + '</small></div>' +
    (p.id === me.id ? '<small class="muted">You</small>' :
      '<div class="m-camp-actions" style="margin:0">' +
        (p.role === 'admin'
          ? '<button class="btn btn-secondary btn-sm" data-p="member" data-id="' + p.id + '">Make member</button>'
          : '<button class="btn btn-secondary btn-sm" data-p="admin" data-id="' + p.id + '">Make admin</button>') +
        '<button class="btn btn-ghost btn-sm" data-p="remove" data-id="' + p.id + '">Remove</button></div>') +
    '</div>').join('');
  box.querySelectorAll('[data-p]').forEach(b => b.onclick = async () => {
    if (b.dataset.p === 'remove' && !confirm('Remove this person? They’ll be signed out. Their campaigns stay.')) return;
    await call('/api/member/people/' + encodeURIComponent(b.dataset.id) + '/' + b.dataset.p, {method: 'POST'});
    loadPeople();
  });
}
