/* ScanShip — shared page script (both views). Family pieces (toasts, side
   menu, view navigation, table, tucked search) are the same as Amazon Returns.
   BASE and API_KEY come from the template, set just before this file loads. */

function $(id) { return document.getElementById(id); }

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
  ));
}

/* Every endpoint answers in JSON, so anything else is the server failing
   outside the app: an IIS error page, a proxy notice, a dropped request. The
   parser message for those reads as a code fault, which is not what happened
   and not what the operator can act on. */
async function readJson(response) {
  try {
    return await response.json();
  } catch (error) {
    throw new Error('The server returned an unexpected response (HTTP '
      + response.status + ').');
  }
}

const toastStack = $('toast-stack');

/* Past this the column reaches the tables and reads as a wall rather than as a
   list of failures. The oldest goes to make room for the newest, which is the
   one the operator has not read yet. */
const TOAST_MAX     = 4;

/* Family standard: every toast lives 6s, with a thin line along its foot that
   empties as the time runs out. The line's CSS animation IS the timer: hover
   or focus pauses it (CSS), and so does a hidden tab (html.tab-hidden). */
function armToast(node) {
  let bar = node.querySelector(':scope > .toast-timer');
  if (!bar) {
    bar = document.createElement('i');
    bar.className = 'toast-timer';
    bar.setAttribute('aria-hidden', 'true');
    node.appendChild(bar);
  }
  bar.style.animation = 'none';
  void bar.offsetWidth;                       // restart from full
  bar.style.animation = '';
  bar.onanimationend = e => { e.stopPropagation(); removeToast(node); };
}

function removeToast(node) {
  if (!node || node.dataset.leaving) return;
  node.dataset.leaving = '1';
  const bar = node.querySelector(':scope > .toast-timer');
  if (bar) bar.onanimationend = null;
  node.classList.add('hiding');
  node.addEventListener('animationend', function done(e) {
    if (e.target !== node) return;
    node.removeEventListener('animationend', done);
    node.remove();
  });
}

document.addEventListener('visibilitychange', () =>
  document.documentElement.classList.toggle('tab-hidden', document.hidden));

/* When a toast joins or leaves the stack the others slide to their new place
   instead of jumping (FLIP, transform only). */
(function () {
  const measure = () => {
    const m = new Map();
    [...toastStack.children].forEach(t => m.set(t, t.getBoundingClientRect().top));
    return m;
  };
  let last = measure();
  new MutationObserver(() => {
    const now = measure();
    if (!window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      now.forEach((top, t) => {
        const was = last.get(t);
        if (was == null || Math.abs(was - top) < 1) return;
        t.animate([{ transform: `translateY(${was - top}px)` }, { transform: 'none' }],
                  { duration: 240, easing: 'cubic-bezier(.23, 1, .32, 1)', composite: 'add' });
      });
    }
    last = now;
  }).observe(toastStack, { childList: true, attributes: true, attributeFilter: ['class'], subtree: true });
})();

/* Every card, oldest first. Cards on their way out are excluded: they still
   occupy the DOM for the length of their animation. */
function liveToasts() {
  return Array.from(toastStack.children).filter(node => !node.dataset.leaving);
}

function dismissToast() {
  const live = liveToasts();
  removeToast(live[live.length - 1]);
}

function dismissAllToasts() {
  liveToasts().forEach(removeToast);
}

/* kind: 'ok' tints the card green. Every toast lasts 6s (family rule); 'sticky'
   is still accepted from the callers but no longer keeps a card up.

   The same title and message twice is one event reported twice -- a retry of
   the same request, a second view asking the same question -- so it refreshes
   the card that is already up instead of stacking a copy. */
function showToast(title, message, kind) {
  const text = message || '';
  const twin = liveToasts().find(node =>
    node.dataset.title === title && node.dataset.message === text);
  if (twin) {
    armToast(twin);
    return twin;
  }

  const node = document.createElement('div');
  node.className = 'err-fixed-toast' + (kind === 'ok' ? ' ok' : kind === 'error' ? ' error' : '');
  node.dataset.title   = title;
  node.dataset.message = text;
  node.innerHTML =
      '<div class="err-toast-inner">'
    +   '<svg class="err-toast-icon" viewBox="0 0 24 24">'
    +     (kind === 'ok'
          ? '<circle cx="12" cy="12" r="9"/><polyline points="8 12.5 11 15.5 16.5 9.5"/>'
          : kind !== 'error'
          ? '<circle cx="12" cy="12" r="9"/><line x1="12" y1="11" x2="12" y2="16"/><line x1="12" y1="8" x2="12.01" y2="8"/>'
          : '<path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/>'
            + '<line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>')
    +   '</svg>'
    +   '<div class="err-toast-body">'
    +     '<span class="err-toast-title"></span>'
    +     '<span class="err-toast-msg"></span>'
    +   '</div>'
    + '</div>'
    + '<button class="err-toast-close" type="button" aria-label="Dismiss">&#x2715;</button>';
  // textContent, not markup: a toast carries an Acumatica message, and those
  // arrive as whatever the instance wrote.
  node.querySelector('.err-toast-title').textContent = title;
  node.querySelector('.err-toast-msg').textContent   = text;
  node.querySelector('.err-toast-close')
      .addEventListener('click', () => removeToast(node));

  toastStack.appendChild(node);
  node.classList.add('visible');

  const live = liveToasts();
  if (live.length > TOAST_MAX) {
    live.slice(0, live.length - TOAST_MAX).forEach(removeToast);
  }
  armToast(node);
  return node;
}


/* Every endpoint sits behind the API key, rendered into the page. */
function apiFetch(path, options) {
  const opts = Object.assign({}, options);
  opts.headers = Object.assign({}, opts.headers, { 'X-API-Key': API_KEY });
  return fetch(BASE + path, opts);
}

/* Keyboard: Enter or Space on a sortable header does what a click does, and
   the focus follows the header across the re-render. */
document.addEventListener('keydown', event => {
  if (event.key !== 'Enter' && event.key !== ' ') return;
  const el = event.target instanceof Element ? event.target : null;
  if (!el || !el.matches('th.sortable')) return;
  event.preventDefault();
  el.click();
  refocusHeader(el);
});

/* Sorting re-renders the table, so the focused header is replaced and focus
   would fall back to the page. Wait for the new header and hand focus to it. */
function refocusHeader(old) {
  const box = old.closest('main, section') || document;
  const key = old.dataset.sort;
  const until = performance.now() + 4000;
  (function look() {
    const next = box.querySelector('th.sortable[data-sort="' + key + '"]');
    if (next && next !== old) { next.focus({ preventScroll: true }); return; }
    const lost = document.activeElement === old || document.activeElement === document.body;
    if (performance.now() < until && lost) requestAnimationFrame(look);
  })();
}

/* The first page is asked for with a probe limit, before the table exists to
   be measured. When the measurement says fewer rows fit, page 1 already holds
   the right rows: drop the extra ones instead of fetching and rebuilding the
   table, which cut its entrance animation off halfway. Likewise when page 1
   already shows every row there is. True when the table can stay as it is. */
function trimRows(mainId, limit, page, total) {
  if (page !== 1) return false;
  const rows = document.querySelectorAll('#' + mainId + ' tbody tr');
  if (rows.length > limit) {
    for (let i = limit; i < rows.length; i++) rows[i].remove();
    return true;
  }
  return rows.length >= total;
}

/* ── TUCKED SEARCH (as LIR) ────────────────────────────────
   An icon that opens on hover or focus; a search in use stays open. The x
   empties it and tucks it back to its icon, even under the pointer (.shut
   holds it closed until the pointer leaves); from the keyboard the focus
   stays in the field. `syncSearch` is called by the views whenever they set
   the value themselves (clear filters, Escape). */
function syncSearch(input) {
  const box = input.closest('.search');
  if (!box) return;
  box.classList.toggle('has-q', !!input.value);
  box.querySelector('.search-x').hidden = !input.value;
}

function tuckSearch(input, onClear) {
  const box = input.closest('.search');
  const clear = box.querySelector('.search-x');
  input.addEventListener('input', () => syncSearch(input));
  clear.addEventListener('click', e => {
    e.preventDefault();
    input.value = '';
    syncSearch(input);
    onClear();
    if (e.detail === 0) input.focus(); else { input.blur(); box.classList.add('shut'); }
  });
  box.addEventListener('pointerleave', () => box.classList.remove('shut'));
}

/* A table is described once and rendered twice — as data and as a skeleton —
   so the two cannot drift apart and the rows cannot shift when the data lands.
   A column is { label, width, num, bar, sort }: `width` is its colgroup
   percentage, `bar` the width of its skeleton stub, `num` right-aligns the
   cells, and `sort` is the key the server orders by — a column without one is
   not sortable and says so by carrying no arrow.

   `state` is { col, dir } as the server echoed it back, so the arrow marks the
   order the rows are actually in. */
function tableOpen(cols, classes, state) {
  const active = state || {};
  const group = cols.map(c => '<col style="width:' + c.width + '">').join('');
  const head  = cols.map(c => {
    if (!c.sort) return '<th>' + escapeHtml(c.label) + '</th>';
    const on  = active.col === c.sort;
    const dir = on ? (active.dir === 'asc' ? 'asc' : 'desc') : '';
    return '<th class="sortable' + (on ? ' sort-' + dir : '') + '" tabindex="0"'
      + ' data-sort="' + c.sort + '"'
      + (on ? ' aria-sort="' + (dir === 'asc' ? 'ascending' : 'descending') + '"' : '')
      + '>' + escapeHtml(c.label)
      + '<span class="sort-arrow">' + (on && dir === 'asc' ? '▲' : '▼')
      + '</span></th>';
  }).join('');
  return '<div class="table-wrap' + (classes ? ' ' + classes : '') + '"><table>'
    + '<colgroup>' + group + '</colgroup>'
    + '<thead><tr>' + head + '</tr></thead><tbody>';
}

const TABLE_CLOSE = '</tbody></table></div>';

/* `rows` is the page size, so the skeleton occupies exactly the space the
   answer will. Marked silent: the shimmer is motion enough, and the entrance
   belongs to the real table. */
function skelTable(cols, rows, classes, state) {
  const row = '<tr class="skel-row">' + cols.map(c =>
      '<td' + (c.left ? ' class="left"' : '') + '><div class="skel-bar" style="width:'
      + (c.bar || '80%') + '"></div></td>').join('') + '</tr>';
  return tableOpen(cols, 'silent' + (classes ? ' ' + classes : ''), state)
    + row.repeat(Math.max(1, rows)) + TABLE_CLOSE;
}

/* One date format for the whole interface: MM/DD/YYYY, fixed width, so a column
   of dates lines up and the eye can compare it without reading. toLocaleString
   is not used anywhere for a date — it varies by machine and locale, which is
   exactly what a shared log cannot afford. */
const pad2 = value => String(value).padStart(2, '0');

function fmtDate(value) {
  if (!value) return '';
  // A date with no time is not a moment. Parsing "2026-08-24" as UTC and then
  // printing it in local time moves it a day, so its parts are read as text.
  const plain = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value));
  if (plain) return plain[2] + '/' + plain[3] + '/' + plain[1];
  const date = value instanceof Date ? value : new Date(value);
  if (isNaN(date)) return String(value);
  return pad2(date.getMonth() + 1) + '/' + pad2(date.getDate()) + '/' + date.getFullYear();
}

/* The same date with the minute, for the one place that needs it: a title, or a
   card reporting when the job last ran. */
function fmtStamp(value) {
  if (!value) return '';
  if (/^\d{4}-\d{2}-\d{2}$/.test(String(value))) return fmtDate(value);
  const date = value instanceof Date ? value : new Date(value);
  if (isNaN(date)) return String(value);
  return fmtDate(date) + ' ' + pad2(date.getHours()) + ':' + pad2(date.getMinutes());
}

/* ── MOTION OVERRIDE (development) ──────────────────────────
   Windows Server ships with client-area animation switched off, which makes
   every browser on the machine report `prefers-reduced-motion: reduce`, which
   correctly removes every animation in this app -- including the ones somebody
   is trying to look at. `?motion=on` forces them back on for the tab,
   `?motion=off` releases it.

   The flag lives in sessionStorage rather than the URL because the header
   links navigate to a plain path: the setting has to survive the page load it
   is meant to animate. Per-tab and disposable, and it can only ever turn
   animation on -- nobody's accessibility setting is weakened by a page they
   did not ask for this on. */
(function motionFlag() {
  const KEY = 'ss-force-motion';
  const asked = new URLSearchParams(location.search).get('motion');
  try {
    if (asked === 'on')  sessionStorage.setItem(KEY, '1');
    if (asked === 'off') sessionStorage.removeItem(KEY);
    if (sessionStorage.getItem(KEY)) document.documentElement.dataset.motion = 'force';
  } catch (error) { /* private mode denies storage; then the flag is simply off */ }
})();

/* True when this browser wants nothing to move -- and the override does not
   say otherwise. Read at click time, not cached: the OS setting can change
   while the page is open. */
function motionIsReduced() {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches
    && document.documentElement.dataset.motion !== 'force';
}

// ── Side menu (header button) ─────────────────────────────────
// The app's views (Scanner / Scan Log). Opens as a circle out of
// the button (CSS); Esc, a click outside or picking an entry closes it, and
// focus goes back to the button. Closed, it is inert (no Tab stops inside).
const menuBtn   = document.getElementById('menu-toggle');
const sideMenu  = document.getElementById('side-menu');
const menuScrim = document.getElementById('menu-scrim');

// The circle is centred on the button, measured (not assumed): header
// padding and button margins change with the screen tier.
function placeMenuCircle() {
  const r = sideMenu.getBoundingClientRect(), b = menuBtn.getBoundingClientRect();
  const cx = b.left + b.width / 2 - r.left, cy = b.top + b.height / 2 - r.top;
  sideMenu.style.setProperty('--cx', cx + 'px');
  sideMenu.style.setProperty('--cy', cy + 'px');
  // Radius that reaches the panel's far corner (bottom left).
  sideMenu.style.setProperty('--menu-r', Math.ceil(Math.hypot(cx, r.height - cy)) + 2 + 'px');
}
placeMenuCircle();
window.addEventListener('resize', placeMenuCircle);

// quick: picking an entry closes it at once and fast (the new screen is
// already coming in; the full close kept a third of the window covered
// for ~560ms, measured).
function setMenu(open, quick) {
  if (open === sideMenu.classList.contains('open')) return;
  if (open) placeMenuCircle();
  if (quick) {
    document.body.classList.add('menu-quick');
    setTimeout(() => document.body.classList.remove('menu-quick'), 260);
  }
  sideMenu.classList.toggle('open', open);
  menuScrim.classList.toggle('open', open);
  document.body.classList.toggle('menu-open', open);
  sideMenu.inert = !open;
  menuBtn.setAttribute('aria-expanded', String(open));
  menuBtn.setAttribute('aria-label', open ? 'Close menu' : 'Open menu');
  if (open) (sideMenu.querySelector('[aria-current]') || sideMenu.querySelector('.menu-item')).focus({ preventScroll: true });
  else if (sideMenu.contains(document.activeElement)) menuBtn.focus();
}

menuBtn.addEventListener('click', () => setMenu(!sideMenu.classList.contains('open')));
menuScrim.addEventListener('click', () => setMenu(false));
// Tabbing out of the open menu closes it (it is not a trap, but it never
// stays open behind the focus).
sideMenu.addEventListener('focusout', e => {
  const to = e.relatedTarget;
  if (to && !sideMenu.contains(to) && to !== menuBtn) setMenu(false);
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && sideMenu.classList.contains('open')) { e.preventDefault(); setMenu(false); }
});

// Pointer glide (mouse only): the highlight slides to the entry under the
// pointer; it appears in place on the first entry, fades when leaving.
const menuBody  = sideMenu.querySelector('.menu-body');
const menuGlide = sideMenu.querySelector('.menu-glide');
if (window.matchMedia('(hover: hover) and (pointer: fine)').matches) {
  menuBody.addEventListener('pointerover', e => {
    const item = e.target.closest('.menu-item');
    if (!item) return;
    const r = item.getBoundingClientRect(), b = menuBody.getBoundingClientRect();
    const fresh = !menuGlide.classList.contains('on');
    if (fresh) menuGlide.style.transition = 'opacity 160ms ease';   // fades in place, no slide from the last spot
    menuGlide.style.height = r.height + 'px';
    menuGlide.style.transform = `translateY(${r.top - b.top}px)`;
    if (fresh) { void menuGlide.offsetWidth; menuGlide.style.transition = ''; }
    menuGlide.classList.add('on');
  });
  menuBody.addEventListener('pointerleave', () => menuGlide.classList.remove('on'));
}


/* ── VIEW NAVIGATION ───────────────────────────────────────
   Scanner and Scan Log are separate documents. Picking one in the menu (or
   the logo, which goes to Scanner) navigates at once, with no exit animation;
   the arriving document comes in from the left (family rule), told by a
   one-shot flag in sessionStorage. The view already open means "take me to the
   top of this": the view gets first refusal through a cancelable 'view:home'
   (handled in place it keeps the search, the page and the sort); nothing
   listening and the link reloads the view, as written. */
(function viewNav() {
  const KEY  = 'ss-nav-in';
  const body = $('app-body');
  try {
    const arriving = sessionStorage.getItem(KEY);
    sessionStorage.removeItem(KEY);
    if (arriving && body && !motionIsReduced()) {
      body.classList.add('entering-fwd');
      body.addEventListener('animationend', () => body.classList.remove('entering-fwd'), { once: true });
    }
  } catch (error) { /* private mode denies storage; then nothing animates */ }

  function go(event, link) {
    // A new tab, a new window and a download are the browser's to perform.
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if (sideMenu.classList.contains('open')) setMenu(false, true);
    const same = link.pathname.replace(/\/+$/, '') === location.pathname.replace(/\/+$/, '');
    if (same) {
      const claim = new CustomEvent('view:home', { bubbles: true, cancelable: true });
      link.dispatchEvent(claim);
      if (claim.defaultPrevented) { event.preventDefault(); return; }
    }
    try { sessionStorage.setItem(KEY, '1'); } catch (error) {}
  }
  sideMenu.querySelectorAll('a.menu-item').forEach(link => link.addEventListener('click', e => go(e, link)));
  const home = $('header-home');
  if (home) home.addEventListener('click', e => go(e, home));

  // Back/Forward can restore this document from the cache mid-animation.
  window.addEventListener('pageshow', e => { if (e.persisted && body) body.classList.remove('entering-fwd'); });
})();

