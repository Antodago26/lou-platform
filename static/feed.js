/**
 * bonhome. Feed mobile : un bien par écran, swipe pour décider.
 * Autonome : ne dépend pas de app.js. Partage seulement le token (lou_token).
 */
(function () {
  'use strict';

  var API = window.location.origin;
  var TOKEN = localStorage.getItem('lou_token');
  if (!TOKEN) { window.location.replace('/?login=1&next=feed'); return; }

  var root = document.getElementById('feed-app');
  var items = [];          // biens chargés, dans l'ordre
  var pointer = 0;         // index du bien affiché
  var dayTotal = 0;        // biens du jour (pour la barre)
  var doneToday = 0;
  var undoable = false;
  var loading = false;
  var exhausted = false;
  var includeNearby = false;
  var huntTries = 0;       // attente du premier scoring (nouveau compte)

  // ---------- SVG (inline, une seule fois) ----------
  var I = {
    x: '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
    heart: '<svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20.5s-7.5-4.6-7.5-10A4.2 4.2 0 0 1 12 8a4.2 4.2 0 0 1 7.5 2.5c0 5.4-7.5 10-7.5 10z"/></svg>',
    heartS: '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><path d="M12 20.5s-7.5-4.6-7.5-10A4.2 4.2 0 0 1 12 8a4.2 4.2 0 0 1 7.5 2.5c0 5.4-7.5 10-7.5 10z"/></svg>',
    heartXL: '<svg width="44" height="44" viewBox="0 0 24 24" fill="#fff" stroke="#fff" stroke-width="1.5" stroke-linejoin="round"><path d="M12 20.5s-7.5-4.6-7.5-10A4.2 4.2 0 0 1 12 8a4.2 4.2 0 0 1 7.5 2.5c0 5.4-7.5 10-7.5 10z"/></svg>',
    up: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M6 11l6-6 6 6"/></svg>',
    undo: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 14L4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 0 12h-3"/></svg>',
    link: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 4h6v6M20 4l-9 9"/><path d="M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/></svg>',
    pin: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 21s-6-5.3-6-11a6 6 0 0 1 12 0c0 5.7-6 11-6 11z"/><circle cx="12" cy="10" r="2.2"/></svg>',
    phone: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2z"/></svg>',
    chevron: '<svg viewBox="0 0 30 14" fill="none" stroke="#8FA780" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12 L15 2 L27 12"/></svg>'
  };

  // ---------- Utils ----------
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function chf(n) { n = Math.round(Number(n) || 0); return n.toString().replace(/\B(?=(\d{3})+(?!\d))/g, "'"); }
  function rooms(r) { r = Number(r) || 0; return r ? (r % 1 === 0 ? r.toFixed(0) : r.toFixed(1)) + ' pièces' : ''; }
  function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({ 'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json' }, opts.headers || {});
    return fetch(API + path, opts).then(function (r) {
      if (r.status === 401) { localStorage.removeItem('lou_token'); window.location.replace('/?login=1&next=feed'); throw new Error('401'); }
      return r.json().then(function (j) { if (!r.ok) throw new Error(j.error || r.status); return j; });
    });
  }
  function toast(msg) {
    var t = root.querySelector('.fd-toast');
    t.textContent = msg; t.classList.add('show');
    clearTimeout(t._h); t._h = setTimeout(function () { t.classList.remove('show'); }, 1600);
  }

  // ---------- Squelette ----------
  root.innerHTML =
    '<div class="fd-top">' +
      '<a class="fd-wordmark" href="/dashboard">bonhome<span class="dot">.</span>' + I.chevron + '</a>' +
      '<div class="fd-chips">' +
        '<div class="fd-chip" id="fd-new"><span class="pip"></span><span>…</span></div>' +
        '<a class="fd-chip is-link" href="/dashboard#favorites" id="fd-favs"><span class="ico-heart">' + I.heartS + '</span><span>0</span></a>' +
      '</div>' +
    '</div>' +
    '<div class="fd-progress" id="fd-progress"></div>' +
    '<div class="fd-progress-label" id="fd-progress-label"></div>' +
    '<div class="fd-scroll" id="fd-scroll"><div class="fd-loading">Lou prépare ton feed…</div></div>' +
    '<div class="fd-toast"></div>' +
    '<div class="fd-match" id="fd-match"><div class="heart">' + I.heartXL + '</div><h2>Match</h2><p>Gardé dans tes favoris</p></div>' +
    '<div class="fd-sheet-backdrop" id="fd-backdrop"></div>' +
    '<div class="fd-sheet" id="fd-sheet"></div>';

  var scroller = document.getElementById('fd-scroll');

  // ---------- Rendu d'une carte ----------
  function card(p, idx) {
    var img = (p.images && p.images[0]) ? '<img src="' + esc(p.images[0]) + '" alt="" loading="' + (idx < 2 ? 'eager' : 'lazy') + '">' : '<div class="fd-nophoto">PAS DE PHOTO</div>';
    var facts = [rooms(p.rooms), p.surface ? p.surface + ' m²' : '', p.floor != null && p.floor !== '' ? (p.floor === 0 ? 'rez' : p.floor + 'e étage') : ''].filter(Boolean).join(' · ');
    var where = [p.address, p.city].filter(Boolean).join(', ');
    var when = p.days_online === 0 ? 'aujourd\'hui' : p.days_online === 1 ? 'hier' : (p.days_online ? 'il y a ' + p.days_online + ' j' : '');
    var unit = (p.unit || 'CHF/mois').split('/')[1] || 'mois';
    var src = (p.source || '').replace('.ch', '') || 'annonce';
    return '<article class="fd-card" data-idx="' + idx + '" data-id="' + p.id + '">' +
      '<div class="fd-photo" data-open="1">' + img + '</div>' +
      '<div class="fd-stamp" data-open="1"><b>' + esc(p.score) + '</b><small>' + esc(p.grade || '') + '</small></div>' +
      '<div class="fd-info" data-open="1">' +
        '<div class="fd-price"><b>' + chf(p.price) + ' CHF</b><span>/ ' + esc(unit) + '</span></div>' +
        '<div class="fd-facts">' + esc(facts) + '</div>' +
        '<div class="fd-where">' + I.pin + '<span>' + esc(where) + (when ? ' · ' + when : '') + '</span></div>' +
      '</div>' +
      '<div class="fd-lou" data-open="1"><div class="avatar">L</div><div><div class="who">Lou</div><div class="say">' + esc(p.lou_note || '') + '</div></div></div>' +
      '<div class="fd-actions">' +
        '<button class="fd-btn undo" data-act="undo" aria-label="Revenir" ' + (undoable ? '' : 'disabled') + '>' + I.undo + '</button>' +
        '<div class="fd-mid">' +
          '<button class="fd-btn pass" data-act="pass" aria-label="Pas pour moi">' + I.x + '</button>' +
          '<button class="fd-btn like" data-act="like" aria-label="Garder">' + I.heart + '</button>' +
        '</div>' +
        '<a class="fd-source" href="' + esc(p.source_url || '#') + '" target="_blank" rel="noopener">' + I.link + '<span>' + esc(src) + '</span></a>' +
      '</div>' +
      '<div class="fd-hint">' + I.up + '<span>Suivant</span></div>' +
    '</article>';
  }

  function endCard() {
    if (!includeNearby) {
      return '<section class="fd-end" data-end="1"><h2>Tu as tout vu pour aujourd\'hui.</h2><p>Lou veille. Dès qu\'un bien qui te correspond sort, tu le vois ici en premier.</p>' +
        '<button class="fd-cta ghost" data-act="nearby">Voir un peu plus loin</button>' +
        '<a class="fd-cta" href="/dashboard#favorites">' + I.heartS + ' Mes favoris</a></section>';
    }
    return '<section class="fd-end" data-end="1"><h2>C\'est tout, même à côté.</h2><p>Lou continue de chercher. Reviens demain, ou élargis tes critères.</p><a class="fd-cta" href="/profil">Modifier mes critères</a></section>';
  }

  function renderAll() {
    var html = items.slice(pointer).map(function (p, i) { return card(p, pointer + i); }).join('');
    scroller.innerHTML = (html || '') + (exhausted || items.length - pointer < 3 ? endCard() : '');
    scroller.scrollTop = 0;
    renderProgress();
  }

  function renderProgress() {
    var pr = document.getElementById('fd-progress');
    var total = Math.max(dayTotal, 1);
    var done = Math.min(doneToday, total);
    var n = Math.min(total, 12);
    var segs = '';
    for (var i = 0; i < n; i++) segs += '<i class="' + (i < Math.round(done / total * n) ? 'done' : '') + '"></i>';
    pr.innerHTML = segs;
    document.getElementById('fd-progress-label').textContent = done + ' sur ' + total + ' aujourd\'hui';
  }

  // ---------- Données ----------
  function load(reset) {
    if (loading) return Promise.resolve();
    loading = true;
    return api('/api/feed?limit=12' + (includeNearby ? '&include_nearby=1' : '')).then(function (d) {
      loading = false;
      if (reset) { items = []; pointer = 0; }
      var known = {};
      items.forEach(function (p) { known[p.id] = 1; });
      (d.items || []).forEach(function (p) { if (!known[p.id]) items.push(p); });
      exhausted = (d.items || []).length === 0 || d.remaining <= items.length;
      if (!dayTotal) dayTotal = (d.remaining || 0) + (d.seen_today || 0);
      doneToday = d.seen_today || 0;
      document.querySelector('#fd-new span:last-child').textContent = (d.new_today || 0) + (d.new_today === 1 ? ' nouveau' : ' nouveaux');
      if (!d.has_profile) { scroller.innerHTML = '<section class="fd-end"><h2>Dis-moi ce que tu cherches.</h2><p>Lou a besoin de tes critères avant de te montrer des biens.</p><a class="fd-cta" href="/">Parler à Lou</a></section>'; return; }
      if (items.length === 0 && !d.seen_today && !includeNearby && huntTries < 12) {
        // Nouveau compte : le scoring tourne encore. On patiente sans dire « tu as tout vu ».
        huntTries += 1;
        scroller.innerHTML = '<section class="fd-end"><h2>Lou est en chasse.</h2><p>Elle passe tes critères sur toutes les annonces. Quelques secondes.</p></section>';
        setTimeout(function () { load(true); }, 5000);
        return;
      }
      renderAll();
    }).catch(function (e) { loading = false; if (e.message !== '401') { scroller.innerHTML = '<section class="fd-end"><h2>Oups.</h2><p>Le feed ne répond pas. Réessaie dans un instant.</p><button class="fd-cta" onclick="location.reload()">Recharger</button></section>'; } });
  }
  function refreshFavCount() {
    api('/api/favorites').then(function (d) { document.querySelector('#fd-favs span:last-child').textContent = d.total || 0; }).catch(function () {});
  }

  // ---------- Gestes ----------
  function currentCard() { return scroller.querySelector('.fd-card[data-idx="' + pointer + '"]'); }

  function decide(action) {
    var el = currentCard();
    if (!el) return;
    var p = items[pointer];
    el.classList.add(action === 'like' ? 'is-leaving-right' : 'is-leaving-left');
    api('/api/swipe', { method: 'POST', body: JSON.stringify({ property_id: p.id, action: action }) })
      .then(function () {
        undoable = true; doneToday += 1;
        if (action === 'like') { refreshFavCount(); if (p.score >= 90) showMatch(); }
      })
      .catch(function () { toast('Geste non enregistré'); });
    setTimeout(function () {
      pointer += 1;
      renderAll();
      if (items.length - pointer <= 3 && !exhausted) load(false);
    }, 240);
  }

  function skipOnScroll() {
    // Le défilement vertical passe au suivant : on enregistre un skip.
    var cards = scroller.querySelectorAll('.fd-card');
    var h = scroller.clientHeight;
    var idx = Math.round(scroller.scrollTop / h);
    var visible = cards[idx];
    if (!visible) return;
    var newPointer = parseInt(visible.getAttribute('data-idx'), 10);
    if (newPointer > pointer) {
      for (var i = pointer; i < newPointer; i++) {
        (function (p) { api('/api/swipe', { method: 'POST', body: JSON.stringify({ property_id: p.id, action: 'skip' }) }).catch(function () {}); })(items[i]);
        doneToday += 1;
      }
      pointer = newPointer; undoable = true;
      // Retire les cartes passées pour garder le DOM léger, sans sauter visuellement.
      var removed = 0;
      for (var j = 0; j < cards.length; j++) { if (parseInt(cards[j].getAttribute('data-idx'), 10) < pointer) { cards[j].remove(); removed += 1; } }
      if (removed) scroller.scrollTop -= removed * h;
      var undoBtn = currentCard() && currentCard().querySelector('.undo'); if (undoBtn) undoBtn.disabled = false;
      renderProgress();
      if (items.length - pointer <= 3 && !exhausted) load(false);
    }
  }
  var scrollT; scroller.addEventListener('scroll', function () { clearTimeout(scrollT); scrollT = setTimeout(skipOnScroll, 120); });

  function undo() {
    api('/api/swipe/undo', { method: 'POST' }).then(function (d) {
      undoable = false; doneToday = Math.max(0, doneToday - 1);
      // Le bien revient en tête si on le connaît encore.
      var idx = -1; items.forEach(function (p, i) { if (p.id === d.property_id) idx = i; });
      if (idx >= 0 && idx < pointer) { pointer = idx; renderAll(); }
      else { load(true); }
      if (d.action === 'like') refreshFavCount();
      toast('Bien remis dans le feed');
    }).catch(function () { toast('Rien à annuler'); });
  }

  function showMatch() {
    var m = document.getElementById('fd-match');
    m.classList.add('show');
    setTimeout(function () { m.classList.remove('show'); }, 1100);
  }

  // Swipe horizontal (doigt) sur la carte courante
  var sx = 0, sy = 0, dragging = false, dx = 0;
  scroller.addEventListener('touchstart', function (e) { var t = e.touches[0]; sx = t.clientX; sy = t.clientY; dragging = true; dx = 0; }, { passive: true });
  scroller.addEventListener('touchmove', function (e) {
    if (!dragging) return;
    var t = e.touches[0]; dx = t.clientX - sx; var dy = t.clientY - sy;
    if (Math.abs(dx) > Math.abs(dy) && Math.abs(dx) > 12) {
      var el = currentCard(); if (el) { el.style.transition = 'none'; el.style.transform = 'translateX(' + dx + 'px) rotate(' + (dx / 25) + 'deg)'; }
    }
  }, { passive: true });
  scroller.addEventListener('touchend', function () {
    if (!dragging) return; dragging = false;
    var el = currentCard(); if (!el) return;
    el.style.transition = ''; el.style.transform = '';
    if (dx > 90) decide('like'); else if (dx < -90) decide('pass');
    dx = 0;
  });

  // ---------- Clics ----------
  root.addEventListener('click', function (e) {
    var act = e.target.closest('[data-act]');
    if (act) {
      var a = act.getAttribute('data-act');
      if (a === 'like' || a === 'pass') decide(a);
      else if (a === 'undo') undo();
      else if (a === 'nearby') { includeNearby = true; exhausted = false; load(true); }
      else if (a === 'close') closeSheet();
      return;
    }
    if (e.target.closest('[data-open]')) { openSheet(items[pointer]); return; }
    if (e.target.id === 'fd-backdrop') closeSheet();
  });

  // ---------- Fiche (panneau) ----------
  function bar(label, v) {
    v = Math.max(0, Math.min(100, Number(v) || 0));
    return '<div class="fd-bar"><label>' + label + '</label><div class="track"><div class="fill' + (v < 60 ? ' low' : '') + '" style="width:' + v + '%"></div></div><output>' + v + '</output></div>';
  }
  function openSheet(p) {
    if (!p) return;
    var d = p.score_detail || {};
    var contact = p.contact_phone ? '<a class="fd-cta" href="tel:' + esc(p.contact_phone) + '">' + I.phone + ' Appeler ' + esc(p.contact_name || 'l\'agence') + '</a>'
      : p.contact_email ? '<a class="fd-cta" href="mailto:' + esc(p.contact_email) + '">' + I.phone + ' Écrire à ' + esc(p.contact_name || 'l\'agence') + '</a>'
      : '<a class="fd-cta" href="' + esc(p.source_url || '#') + '" target="_blank" rel="noopener">' + I.link + ' Contacter via l\'annonce</a>';
    var sheet = document.getElementById('fd-sheet');
    sheet.innerHTML = '<div class="grab"></div>' +
      '<h3>' + esc(p.title) + '</h3>' +
      '<div class="addr">' + I.pin + '<span>' + esc([p.address, p.city].filter(Boolean).join(', ')) + '</span></div>' +
      '<div class="fd-grid">' +
        '<div><b>' + esc(p.rooms || '–') + '</b><small>pièces</small></div>' +
        '<div><b>' + esc(p.surface || '–') + '</b><small>m²</small></div>' +
        '<div><b>' + (p.floor != null && p.floor !== '' ? esc(p.floor) : '–') + '</b><small>étage</small></div>' +
        '<div><b>' + (p.distance_km != null ? esc(p.distance_km.toFixed(1)) : '–') + '</b><small>km</small></div>' +
      '</div>' +
      '<div class="fd-why"><b>Pourquoi ' + esc(p.score) + '</b><span>selon tes critères</span></div>' +
      '<div class="fd-bars">' + bar('Zone', d.zone) + bar('Budget', d.budget) + bar('Surface', d.surface) + bar('Équipements', d.equipment) + bar('Fraîcheur', d.freshness) + '</div>' +
      (p.description ? '<div class="fd-desc">' + esc(p.description) + '</div>' : '') +
      contact +
      '<a class="fd-link" href="' + esc(p.source_url || '#') + '" target="_blank" rel="noopener">' + I.link + ' Voir l\'annonce sur ' + esc((p.source || '').replace('.ch', '') || 'le portail') + '</a>';
    sheet.classList.add('open'); document.getElementById('fd-backdrop').classList.add('open');
  }
  function closeSheet() { document.getElementById('fd-sheet').classList.remove('open'); document.getElementById('fd-backdrop').classList.remove('open'); }

  // ---------- Go ----------
  load(true); refreshFavCount();
})();
