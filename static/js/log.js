/* ScanShip — Scan Log view. Moved out of templates/log.html (2026-10-01);
   uses the shared helpers of app.js. The page size is measured to fit the
   window; the log refreshes itself every 10s while the tab is visible. */

const COLS = [
  { label: 'Status',        width: '13%', sort: 'ScanStatus',   bar: '60%' },
  { label: 'Scan Date',     width: '16%', sort: 'ScanDate',     bar: '75%' },
  { label: 'Order Nbr.',    width: '14%', sort: 'OrderNbr',     bar: '70%' },
  { label: 'Shipment Nbr.', width: '15%', sort: 'ShipmentNbr',  bar: '70%' },
  { label: 'Ship. Date',    width: '12%', sort: 'ShipmentDate', bar: '65%' },
  { label: 'Item / SKU',    width: '17%', sort: 'InventoryID',  bar: '75%' },
  { label: 'Acu. Status',   width: '13%', sort: 'Status',       bar: '60%' },
];

const PROBE_LIMIT  = 30;   // first page, before there is a table to measure
const MIN_LIMIT    = 1;
const REFRESH_MS   = 10000;
const SEARCH_DEBOUNCE_MS = 350;

const s = {
  page: 1, pages: 1, total: 0,
  limit: Number(sessionStorage.getItem('ss.log.limit')) || 0,
  sort: 'ScanDate', dir: 'desc', query: '',
  loading: false, hasTable: false, fits: 0,
};
const searchInput = $('log-search');
let searchTimer = null, refreshTimer = null;

/* ── RENDER ─────────────────────────────────────────────── */
function statusBadge(value) {
  const label = value || '—';
  return '<span class="badge ' + (value === 'Scanned' ? 'badge-scanned' : 'badge-pending')
    + '"><span class="badge-dot"></span>' + escapeHtml(label) + '</span>';
}

/* Scan times are Miami time whatever the machine says. */
function fmtMiami(value) {
  if (!value) return '—';
  if (/^\d{4}-\d{2}-\d{2}$/.test(String(value))) return fmtDate(value);
  const d = new Date(value);
  if (isNaN(d)) return String(value);
  const p = Object.fromEntries(new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).formatToParts(d).map(x => [x.type, x.value]));
  return p.month + '/' + p.day + '/' + p.year + ' ' + p.hour + ':' + p.minute;
}

function td(value, cls) {
  const v = value == null || value === '' ? null : String(value);
  return v === null ? '<td class="dim">—</td>'
    : '<td' + (cls ? ' class="' + cls + '"' : '') + '>' + escapeHtml(v) + '</td>';
}

function logRow(row) {
  return '<tr>'
    + '<td>' + statusBadge(row.ScanStatus) + '</td>'
    + '<td class="dim mono">' + escapeHtml(fmtMiami(row.ScanDate)) + '</td>'
    + td(row.OrderNbr, 'mono')
    + td(row.ShipmentNbr, 'mono')
    + '<td class="dim mono">' + escapeHtml(fmtMiami(row.ShipmentDate)) + '</td>'
    + td(row.InventoryID)
    + td(row.Status)
    + '</tr>';
}

function renderRows(rows, silent) {
  if (!rows.length) {
    s.hasTable = false;
    $('log-main').innerHTML = '<div class="empty">'
      + '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/>'
      + '<line x1="3" y1="9" x2="21" y2="9"/><line x1="3" y1="15" x2="21" y2="15"/>'
      + '<line x1="9" y1="9" x2="9" y2="21"/></svg>'
      + '<div class="empty-text">' + (s.query ? 'No unit matches “' + escapeHtml(s.query) + '”.' : 'No units yet.') + '</div></div>';
    return;
  }
  s.hasTable = true;
  $('log-main').innerHTML = tableOpen(COLS, silent ? 'silent' : '', { col: s.sort, dir: s.dir })
    + rows.map(logRow).join('') + TABLE_CLOSE;
}

function renderPager() {
  $('pager-info').textContent = s.total ? 'Page ' + s.page.toLocaleString() + ' of ' + s.pages.toLocaleString() : '';
  $('pager-total').textContent = s.total ? s.total.toLocaleString() + (s.total === 1 ? ' unit' : ' units') : '';
  $('page-prev').disabled = s.page <= 1;
  $('page-next').disabled = s.page >= s.pages;
}

/* ── LOAD ───────────────────────────────────────────────── */
async function load(options) {
  const silent = !!(options && options.silent);
  if (s.loading) return;
  s.loading = true;
  if (!silent && !s.hasTable) {
    $('log-main').innerHTML = skelTable(COLS, Math.max(MIN_LIMIT, s.limit || 12), '', { col: s.sort, dir: s.dir });
  }
  const limit = Math.max(MIN_LIMIT, s.limit || PROBE_LIMIT);
  const params = new URLSearchParams({ page: s.page, limit, sort_col: s.sort, sort_dir: s.dir });
  if (s.query) params.append('q', s.query);
  try {
    const res  = await apiFetch('/api/log?' + params.toString());
    const data = await readJson(res);
    if (!res.ok) throw new Error(data.error || 'The scan log could not be read (HTTP ' + res.status + ').');
    s.pages = data.pages || 1;
    s.total = data.total || 0;
    s.page  = data.page || 1;
    renderRows(data.logs || [], silent);
    renderPager();
    measureOnce();
  } catch (err) {
    if (!silent) {
      if (!s.hasTable) {
        $('log-main').innerHTML = '<div class="empty"><div class="empty-text">'
          + escapeHtml(err.message) + '</div></div>';
      }
      showToast('Scan Log Unavailable', err.message, 'error');
    }
  } finally {
    s.loading = false;
  }
}

/* How many rows fit, from where the rendered rows actually end (sub-pixel
   row heights add up, so an average would let the last row run past the
   bottom). Rows that fit are counted; the spare height below them adds more
   only when the page was full. */
function fitLimit() {
  const wrap = document.querySelector('#log-main .table-wrap');
  if (!wrap) return 0;
  const rows = wrap.querySelectorAll('tbody tr');
  if (!rows.length) return 0;
  const floor = wrap.getBoundingClientRect().top + wrap.clientTop + wrap.clientHeight;
  let fits = 0, last = null;
  rows.forEach(row => {
    const box = row.getBoundingClientRect();
    if (box.bottom <= floor + 0.5) { fits += 1; last = box; }
  });
  if (!fits) return MIN_LIMIT;
  const full = rows.length >= (s.limit || rows.length);
  if (!full && fits === rows.length) return s.limit || 0;
  return fits + (full ? Math.max(0, Math.floor((floor - last.bottom) / (last.height || 36))) : 0);
}

/* The first page comes with a probe limit; once measured, extra rows are
   dropped in place (never a rebuild mid-entrance, family rule). */
const FIT_PASSES = 3;
function measureOnce() {
  if (s.fits >= FIT_PASSES || !s.hasTable) return;
  requestAnimationFrame(() => {
    const limit = fitLimit();
    s.fits += 1;
    if (!limit || limit === s.limit) return;
    s.limit = limit;
    try { sessionStorage.setItem('ss.log.limit', limit); } catch (_) {}
    if (trimRows('log-main', limit, s.page, s.total)) {
      s.pages = Math.max(1, Math.ceil(s.total / limit));
      renderPager();
      measureOnce();
    } else {
      load({ silent: true });
    }
  });
}

let resizeTimer = null;
window.addEventListener('resize', () => {
  if (document.activeElement === searchInput) return;
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    const limit = fitLimit();
    if (limit && limit !== s.limit) {
      s.limit = limit; s.fits = 0;
      try { sessionStorage.setItem('ss.log.limit', limit); } catch (_) {}
      s.page = Math.min(s.page, Math.max(1, Math.ceil(s.total / limit)));
      load({ silent: true });
    }
  }, 300);
});

/* ── SEARCH, SORT, PAGES ────────────────────────────────── */
function runSearch() {
  clearTimeout(searchTimer);
  syncSearch(searchInput);
  s.query = searchInput.value.trim();
  s.page = 1; s.hasTable = false;
  load();
}
searchInput.addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(runSearch, SEARCH_DEBOUNCE_MS);
});
searchInput.addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); runSearch(); }
  if (e.key === 'Escape' && searchInput.value) { e.preventDefault(); searchInput.value = ''; runSearch(); }
});
tuckSearch(searchInput, runSearch);

$('log-main').addEventListener('click', e => {
  const th = e.target.closest('th.sortable');
  if (!th) return;
  const col = th.dataset.sort;
  s.dir  = s.sort === col ? (s.dir === 'asc' ? 'desc' : 'asc') : 'asc';
  s.sort = col;
  s.page = 1;
  load();
});

$('page-prev').addEventListener('click', () => { if (s.page > 1) { s.page -= 1; load(); } });
$('page-next').addEventListener('click', () => { if (s.page < s.pages) { s.page += 1; load(); } });

/* The menu's Scan Log entry while already here: back to page 1, no reload. */
document.addEventListener('view:home', e => {
  e.preventDefault();
  s.page = 1;
  load({ silent: true });
});

/* ── AUTO-REFRESH (paused while the tab is hidden) ──────── */
function startRefresh() {
  if (!refreshTimer) refreshTimer = setInterval(() => {
    if (document.activeElement !== searchInput) load({ silent: true });
  }, REFRESH_MS);
}
function stopRefresh() { clearInterval(refreshTimer); refreshTimer = null; }
document.addEventListener('visibilitychange', () => {
  if (document.hidden) stopRefresh(); else { load({ silent: true }); startRefresh(); }
});

load();
startRefresh();
