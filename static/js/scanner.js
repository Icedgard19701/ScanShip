/* ScanShip — Scanner view. Moved out of templates/index.html (2026-10-01);
   uses the shared helpers of app.js. A handheld scanner types into whatever
   has focus and ends with Enter, so the flow is driven by two fields: the
   order number, then each item barcode. */

const searchInput = $('search-input');
const itemInput   = $('item-input');
const orderBox    = $('order');
const itemsList   = $('items-list');
const btnComplete = $('btn-complete');
const confirmBox  = $('confirm');
const completed   = $('completed');

const state = { order: '', type: '', busy: false };
let completedTimer = null;

/* ── SEARCH ─────────────────────────────────────────────── */
function doSearch() {
  const q = searchInput.value.trim();
  if (q) searchOrder(q, false);
}
searchInput.addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); doSearch(); }
});
$('search-go').addEventListener('click', doSearch);

/* The order section stands in with placeholder rows while the search runs,
   so the screen changes once, on the click, not again when the data lands. */
function showSkeleton(q) {
  $('empty-state').hidden = true;
  if (!orderBox.classList.contains('active')) {
    orderBox.classList.add('active');
    if (!motionIsReduced()) {
      orderBox.classList.add('entering');
      orderBox.addEventListener('animationend', () => orderBox.classList.remove('entering'), { once: true });
    }
  }
  $('order-number').textContent = q;
  $('items-count').textContent = '';
  const widths = [130, 100, 150, 110, 90, 140];
  itemsList.classList.remove('entering');
  itemsList.innerHTML = widths.map(w =>
    '<div class="item-row skel"><div class="item-info">'
    + '<div class="skel-bar" style="width:' + w + 'px;margin-bottom:6px"></div>'
    + '<div class="skel-bar" style="width:' + Math.round(w * .6) + 'px"></div>'
    + '</div><button class="item-check" type="button" disabled tabindex="-1" aria-hidden="true"><i></i></button></div>'
  ).join('');
  setComplete(0);
  itemInput.value = '';
}

/* refresh: called after a scan, with the order already on screen. A failure
   then keeps what is there instead of throwing the operator back to empty. */
async function searchOrder(q, refresh) {
  if (!refresh) showSkeleton(q);
  try {
    const res  = await apiFetch('/api/search?q=' + encodeURIComponent(q));
    const data = await readJson(res);
    if (!res.ok) throw new Error(data.error || 'The order could not be loaded.');
    if (!data.items || data.total === 0) {
      showToast('Order Not Found', data.message || ('No order matches ' + q + '.'), 'error');
      if (!refresh) resetToEmpty();
      return;
    }
    state.order = q;
    state.type  = data.items[0].OrderType || '';
    if (data.done > 0 && data.done === data.total) {
      searchInput.value = '';
      showCompleted();
      return;
    }
    renderOrder(data, refresh);
  } catch (err) {
    showToast('Connection Error', err.message || 'The server could not be reached. Try again.', 'error');
    if (!refresh) resetToEmpty();
  }
}

function itemRow(item) {
  const qty  = parseInt(item.ShippedQty, 10) || 1;
  const unit = qty > 1 ? ' · unit ' + item.unit : '';
  const sku  = item.InventoryID || '';
  return '<div class="item-row' + (item.scanned ? ' scanned' : '') + '" data-scan-id="' + escapeHtml(item.ScanID)
    + '" data-sku="' + escapeHtml(sku) + '">'
    + '<div class="item-info"><div class="item-sku">' + escapeHtml(sku) + '</div>'
    + '<div class="item-sub">' + escapeHtml(item.OrderNbr || '') + escapeHtml(unit) + '</div></div>'
    + '<button class="item-check" type="button" aria-pressed="' + !!item.scanned + '" aria-label="'
    + escapeHtml((item.scanned ? 'Unmark ' : 'Mark ') + sku + (item.scanned ? '' : ' as scanned')) + '">'
    + '<i><svg viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg></i></button>'
    + '</div>';
}

/* A refresh with the same units only flips the ones that changed, so the
   check fills and the tick draws on the row that was just scanned. */
function renderOrder(data, refresh) {
  $('order-number').textContent = data.items[0].OrderNbr || state.order;
  $('items-count').textContent = data.done + ' of ' + data.total + ' scanned';

  const rows = itemsList.querySelectorAll('.item-row:not(.skel)');
  const same = refresh && rows.length === data.items.length
    && data.items.every((item, i) => String(item.ScanID) === rows[i].dataset.scanId);
  if (same) {
    data.items.forEach((item, i) => {
      const row = rows[i], btn = row.querySelector('.item-check');
      row.classList.toggle('scanned', !!item.scanned);
      btn.setAttribute('aria-pressed', String(!!item.scanned));
      btn.setAttribute('aria-label', (item.scanned ? 'Unmark ' : 'Mark ') + row.dataset.sku + (item.scanned ? '' : ' as scanned'));
    });
  } else {
    itemsList.classList.toggle('entering', !refresh);
    itemsList.innerHTML = data.items.map(itemRow).join('');
  }
  setComplete(data.total - data.done);
  itemInput.value = '';
  itemInput.focus({ preventScroll: true });
}

/* ── SCAN / UNSCAN ──────────────────────────────────────── */
itemInput.addEventListener('keydown', e => {
  if (e.key !== 'Enter') return;
  e.preventDefault();
  const sku = itemInput.value.trim();
  if (sku) scanItem(sku);
});
$('item-go').addEventListener('click', () => {
  const sku = itemInput.value.trim();
  if (sku) scanItem(sku); else itemInput.focus();
});

async function post(path, body) {
  const res = await apiFetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  let data = {};
  try { data = await res.json(); } catch (_) {}
  return { res, data };
}

async function scanItem(sku) {
  try {
    const { res, data } = await post('/api/scan', {
      OrderNbr: state.order, OrderType: state.type, InventoryID: sku, ScanUser: 'User',
    });
    if (res.ok) {
      showToast('Item Scanned', data.sku || sku, 'ok');
      if (data.completed) { showCompleted(); return; }
      await searchOrder(state.order, true);
    } else {
      showToast('Scan Failed', data.error || ('Error ' + res.status), 'error');
    }
  } catch (err) {
    showToast('Connection Error', 'The server could not be reached. Try again.', 'error');
  }
  itemInput.value = '';
  itemInput.focus({ preventScroll: true });
}

itemsList.addEventListener('click', e => {
  const btn = e.target.closest('.item-check');
  const row = btn && btn.closest('.item-row');
  if (!row || row.classList.contains('skel')) return;
  toggleItem(row);
});

async function toggleItem(row) {
  if (state.busy) return;
  state.busy = true;
  const sku = row.dataset.sku;
  try {
    if (row.classList.contains('scanned')) {
      try {
        const { res, data } = await post('/api/unscan', {
          ScanID: parseInt(row.dataset.scanId, 10), OrderNbr: state.order, OrderType: state.type,
        });
        if (res.ok) {
          showToast('Item Unmarked', sku);
          await searchOrder(state.order, true);
        } else {
          showToast('Unmark Failed', data.error || ('Error ' + res.status), 'error');
        }
      } catch (err) {
        showToast('Connection Error', 'The server could not be reached. Try again.', 'error');
      }
    } else {
      await scanItem(sku);
    }
  } finally {
    state.busy = false;
  }
}

/* ── COMPLETE ORDER (mark every remaining unit) ─────────── */
function setComplete(remaining) {
  btnComplete.dataset.remaining = remaining;
  btnComplete.hidden = !(remaining > 0);
}
btnComplete.addEventListener('click', () => {
  const remaining = parseInt(btnComplete.dataset.remaining, 10) || 0;
  if (remaining <= 0) return;
  $('confirm-text').textContent =
    'Mark the ' + remaining + ' remaining item' + (remaining === 1 ? '' : 's') + ' as scanned?';
  confirmBox.classList.add('show');
  $('confirm-ok').focus();
});
function closeConfirm() {
  if (!confirmBox.classList.contains('show')) return;
  confirmBox.classList.remove('show');
  itemInput.focus({ preventScroll: true });
}
$('confirm-cancel').addEventListener('click', closeConfirm);
confirmBox.addEventListener('click', e => { if (e.target === confirmBox) closeConfirm(); });
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && confirmBox.classList.contains('show')) { e.preventDefault(); closeConfirm(); }
});

$('confirm-ok').addEventListener('click', async () => {
  const btn = $('confirm-ok');
  if (btn.disabled) return;
  btn.disabled = true;
  try {
    const { res, data } = await post('/api/complete-order', { OrderNbr: state.order, OrderType: state.type });
    closeConfirm();
    if (res.ok) {
      showToast('Order Updated', data.filled + ' item' + (data.filled === 1 ? '' : 's') + ' marked as scanned', 'ok');
      if (data.completed) { showCompleted(); return; }
      await searchOrder(state.order, true);
    } else {
      showToast('Complete Failed', data.error || ('Error ' + res.status), 'error');
    }
  } catch (err) {
    closeConfirm();
    showToast('Connection Error', 'The server could not be reached. Try again.', 'error');
  } finally {
    btn.disabled = false;
  }
});

/* ── ORDER COMPLETED ────────────────────────────────────── */
function showCompleted() {
  $('completed-order').textContent = state.order;
  completed.classList.add('show');
  if (document.activeElement) document.activeElement.blur();   // hides the phone keyboard
  completedTimer = setTimeout(dismissCompleted, 3000);
}
function dismissCompleted() {
  if (completedTimer) { clearTimeout(completedTimer); completedTimer = null; }
  if (!completed.classList.contains('show')) return;
  completed.classList.remove('show');
  resetToEmpty();
}
completed.addEventListener('click', dismissCompleted);
document.addEventListener('keydown', e => {
  if (completed.classList.contains('show') && (e.key === 'Enter' || e.key === 'Escape' || e.key === ' ')) {
    e.preventDefault(); dismissCompleted();
  }
});

/* ── BACK TO EMPTY ──────────────────────────────────────── */
function resetToEmpty() {
  state.order = ''; state.type = '';
  confirmBox.classList.remove('show');
  setComplete(0);
  orderBox.classList.remove('active', 'entering');
  itemsList.innerHTML = '';
  $('empty-state').hidden = false;
  searchInput.value = '';
  searchInput.focus({ preventScroll: true });
}
$('order-close').addEventListener('click', resetToEmpty);

/* The menu's Scanner entry (or the logo) while already here: back to an
   empty search, ready for the next order, without a reload. */
document.addEventListener('view:home', e => { e.preventDefault(); resetToEmpty(); });

searchInput.focus({ preventScroll: true });
