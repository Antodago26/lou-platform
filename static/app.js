/**
 * Lou Garou — Frontend App
 * Landing page auth + Full Dashboard with real properties
 */
(function () {
  'use strict';

  var API = 'https://lou-platform.onrender.com';
  var TOKEN = localStorage.getItem('lou_token');
  var USER = JSON.parse(localStorage.getItem('lou_user') || 'null');

  function $(id) { return document.getElementById(id); }
  function ce(tag, cls, html) {
    var el = document.createElement(tag);
    if (cls) el.className = cls;
    if (html) el.innerHTML = html;
    return el;
  }
  function isJWT(t) { return t && t.split('.').length === 3; }
  function escapeHtml(text) {
    var div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  // ============================================================
  // AUTH MODAL
  // ============================================================
  function showAuthModal() {
    var existing = document.querySelector('.lou-overlay');
    if (existing) existing.remove();

    var overlay = ce('div', 'lou-overlay');
    overlay.innerHTML = [
      '<div class="lou-auth-box">',
      '<button class="close-btn" id="lou-auth-close">&times;</button>',
      '<h2><svg style="width:28px;height:28px;vertical-align:middle;margin-right:8px" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><rect width="48" height="48" rx="12" fill="#0369a1"/><g transform="translate(8,6)"><path d="M4 34 L10 5 L16 16 L22 5 L28 34 L22 28 L16 32 L10 28 Z" fill="rgba(255,255,255,0.95)"/><circle cx="12.5" cy="21" r="2" fill="#0369a1"/><circle cx="19.5" cy="21" r="2" fill="#0369a1"/></g></svg>Lou Garou</h2>',
      '<div class="sub">Votre chasseur immobilier IA en Suisse</div>',
      '<input id="lou-auth-email" type="email" placeholder="Email">',
      '<input id="lou-auth-pass" type="password" placeholder="Mot de passe">',
      '<input id="lou-auth-name" type="text" placeholder="Votre nom" style="display:none">',
      '<button class="auth-submit" id="lou-auth-btn">Se connecter</button>',
      '<div class="lou-auth-switch"><a id="lou-auth-toggle">Creer un compte</a></div>',
      '<div class="lou-auth-err" id="lou-auth-err"></div>',
      '</div>'
    ].join('');
    document.body.appendChild(overlay);

    overlay.addEventListener('click', function (e) {
      if (e.target === overlay) overlay.remove();
    });
    $('lou-auth-close').onclick = function () { overlay.remove(); };

    var mode = 'login';
    $('lou-auth-toggle').onclick = function () {
      mode = mode === 'login' ? 'signup' : 'login';
      $('lou-auth-name').style.display = mode === 'signup' ? 'block' : 'none';
      $('lou-auth-btn').textContent = mode === 'signup' ? "S'inscrire" : 'Se connecter';
      this.textContent = mode === 'signup' ? 'Deja un compte ? Se connecter' : 'Creer un compte';
      $('lou-auth-err').style.display = 'none';
    };

    $('lou-auth-btn').onclick = function () {
      var email = $('lou-auth-email').value.trim();
      var pass = $('lou-auth-pass').value;
      var name = $('lou-auth-name').value.trim();
      var err = $('lou-auth-err');
      var btn = this;

      if (!email || !pass) {
        err.textContent = 'Email et mot de passe requis';
        err.style.display = 'block';
        return;
      }
      btn.textContent = 'Chargement...';
      btn.disabled = true;

      var body = mode === 'signup'
        ? { email: email, password: pass, name: name }
        : { email: email, password: pass };

      fetch(API + '/api/' + mode, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.ok) {
            localStorage.setItem('lou_token', data.token);
            localStorage.setItem('lou_user', JSON.stringify(data.user));
            window.location.href = '/dashboard';
          } else {
            err.textContent = data.error || 'Erreur de connexion';
            err.style.display = 'block';
            btn.textContent = mode === 'signup' ? "S'inscrire" : 'Se connecter';
            btn.disabled = false;
          }
        })
        .catch(function () {
          err.textContent = 'Erreur reseau — reessayez';
          err.style.display = 'block';
          btn.textContent = mode === 'signup' ? "S'inscrire" : 'Se connecter';
          btn.disabled = false;
        });
    };

    overlay.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') $('lou-auth-btn').click();
    });

    setTimeout(function () { $('lou-auth-email').focus(); }, 100);
  }

  // ============================================================
  // LANDING PAGE — Hook CTAs
  // ============================================================
  function initLanding() {
    // Hook all CTA buttons to open auth modal
    ['nav-login-btn', 'hero-cta-1', 'cta-bottom'].forEach(function (id) {
      var el = $(id);
      if (el) {
        el.addEventListener('click', function (e) {
          e.preventDefault();
          showAuthModal();
        });
      }
    });

    // If user is already logged in, change nav button to "Mon Dashboard"
    if (isJWT(TOKEN) && USER) {
      var navBtn = $('nav-login-btn');
      if (navBtn) {
        navBtn.textContent = 'Mon Dashboard';
        navBtn.onclick = function (e) {
          e.preventDefault();
          window.location.href = '/dashboard';
        };
      }
    }

    // Chat bubble on landing page
    injectChatCSS();
    initChat();
  }

  // ============================================================
  // CHAT CSS — inject standalone (for landing page)
  // ============================================================
  function injectChatCSS() {
    var s = ce('style', '', [
      '.chat-toggle{position:fixed;bottom:24px;right:24px;width:56px;height:56px;border-radius:50%;background:#0369a1;color:#fff;border:none;font-size:24px;cursor:pointer;box-shadow:0 4px 20px rgba(3,105,161,.4);z-index:1000;display:flex;align-items:center;justify-content:center;transition:transform .2s}',
      '.chat-toggle:hover{transform:scale(1.1)}',
      '.chat-panel{position:fixed;bottom:90px;right:24px;width:380px;max-width:calc(100vw - 48px);height:500px;background:#fff;border-radius:16px;box-shadow:0 8px 40px rgba(0,0,0,.15);z-index:1000;display:none;flex-direction:column;overflow:hidden}',
      '.chat-panel.open{display:flex}',
      '.chat-head{background:#0f172a;color:#fff;padding:14px 18px;font-weight:600;display:flex;justify-content:space-between;align-items:center;font-size:15px}',
      '.chat-head button{background:none;border:none;color:#fff;font-size:22px;cursor:pointer;opacity:.7}',
      '.chat-head button:hover{opacity:1}',
      '.chat-body{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px}',
      '.chat-msg{max-width:85%;padding:10px 14px;border-radius:12px;font-size:14px;line-height:1.6}',
      '.chat-msg.bot{background:#f1f5f9;color:#0f172a;align-self:flex-start;border-bottom-left-radius:4px}',
      '.chat-msg.user{background:#0369a1;color:#fff;align-self:flex-end;border-bottom-right-radius:4px}',
      '.chat-input{display:flex;border-top:1px solid #e2e8f0;padding:12px}',
      '.chat-input input{flex:1;border:1px solid #e2e8f0;border-radius:8px;padding:10px 14px;font-size:14px;outline:none;font-family:Inter,sans-serif}',
      '.chat-input input:focus{border-color:#0369a1}',
      '.chat-input button{margin-left:8px;background:#0369a1;color:#fff;border:none;border-radius:8px;padding:10px 16px;cursor:pointer;font-size:16px;transition:background .2s}',
      '.chat-input button:hover{background:#0284c7}',
      '.chat-suggestions{display:flex;flex-wrap:wrap;gap:6px;margin-top:4px}',
      '.chat-sug{padding:6px 12px;background:#e0f2fe;border:none;border-radius:20px;font-size:12px;color:#0369a1;cursor:pointer;transition:background .2s}',
      '.chat-sug:hover{background:#bae6fd}',
      '@media(max-width:768px){.chat-panel{width:calc(100vw - 32px);right:16px;bottom:80px;height:60vh}}'
    ].join(''));
    document.head.appendChild(s);
  }

  // ============================================================
  // DASHBOARD
  // ============================================================
  function showDashboard() {
    if (!isJWT(TOKEN) || !USER) {
      window.location.href = '/';
      return;
    }

    document.title = 'Dashboard — Lou Garou';

    // Inject CSS
    var style = ce('style', '', getDashCSS());
    document.head.appendChild(style);

    document.body.innerHTML = '';

    // NAV
    var nav = ce('div', 'dash-nav');
    nav.innerHTML =
      '<a href="/" class="dash-nav-brand"><svg class="dash-logo-wolf" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><rect width="48" height="48" rx="12" fill="#0369a1"/><g transform="translate(8,6)"><path d="M4 34 L10 5 L16 16 L22 5 L28 34 L22 28 L16 32 L10 28 Z" fill="rgba(255,255,255,0.95)"/><circle cx="12.5" cy="21" r="2" fill="#0369a1"/><circle cx="19.5" cy="21" r="2" fill="#0369a1"/></g></svg>Lou Garou</a>' +
      '<div class="dash-nav-right">' +
        '<span class="dash-user-email">' + escapeHtml(USER.email || '') + '</span>' +
        '<button class="dash-logout-btn" id="logout-btn">Deconnexion</button>' +
      '</div>';
    document.body.appendChild(nav);

    // MAIN WRAP
    var wrap = ce('div', 'dash-wrap');
    wrap.id = 'dash-wrap';
    wrap.innerHTML =
      '<div class="dash-header">' +
        '<h1>Mon Dashboard</h1>' +
        '<div class="dash-actions">' +
          '<select id="sort-select" class="dash-select">' +
            '<option value="score">Meilleur score</option>' +
            '<option value="price_asc">Prix croissant</option>' +
            '<option value="price_desc">Prix decroissant</option>' +
            '<option value="newest">Plus recents</option>' +
            '<option value="surface">Plus grande surface</option>' +
          '</select>' +
          '<select id="grade-filter" class="dash-select">' +
            '<option value="0">Tous les biens</option>' +
            '<option value="85">Classe A (85+)</option>' +
            '<option value="70">Classe B+ (70+)</option>' +
            '<option value="55">Classe C+ (55+)</option>' +
          '</select>' +
        '</div>' +
      '</div>' +
      // Stats row
      '<div class="dash-stats" id="dash-stats">' +
        '<div class="dash-stat"><div class="dash-stat-num" id="stat-total">-</div><div class="dash-stat-lbl">Biens analyses</div></div>' +
        '<div class="dash-stat"><div class="dash-stat-num" id="stat-new">-</div><div class="dash-stat-lbl">Nouveaux (24h)</div></div>' +
        '<div class="dash-stat"><div class="dash-stat-num" id="stat-favs">-</div><div class="dash-stat-lbl">Favoris</div></div>' +
        '<div class="dash-stat"><div class="dash-stat-num" id="stat-grade-a">-</div><div class="dash-stat-lbl">Classe A</div></div>' +
      '</div>' +
      // Profile summary
      '<div id="profile-bar" class="dash-profile-bar"></div>' +
      // Properties list
      '<div id="properties-list" class="dash-properties"><div class="dash-loading">Chargement des biens...</div></div>' +
      // Pagination
      '<div id="pagination" class="dash-pagination"></div>';

    document.body.appendChild(wrap);

    // Logout
    $('logout-btn').onclick = function () {
      localStorage.removeItem('lou_token');
      localStorage.removeItem('lou_user');
      window.location.href = '/';
    };

    // Load data
    loadStats();
    loadProfileBar();
    loadProperties(1, 'score', 0);

    // Sort/filter change
    $('sort-select').onchange = function () {
      loadProperties(1, this.value, parseInt($('grade-filter').value));
    };
    $('grade-filter').onchange = function () {
      loadProperties(1, $('sort-select').value, parseInt(this.value));
    };

    // Init chat widget
    initChat();
  }

  // ============================================================
  // LOAD STATS
  // ============================================================
  function loadStats() {
    fetch(API + '/api/stats/' + USER.id)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        $('stat-total').textContent = data.total || 0;
        $('stat-new').textContent = data.new_count || 0;
        $('stat-favs').textContent = data.favorites || 0;
        // Grade A count loaded separately
      })
      .catch(function () {
        $('stat-total').textContent = '?';
      });

    // Count grade A properties
    fetch(API + '/api/properties/' + USER.id + '?min_score=85&per_page=1')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        $('stat-grade-a').textContent = data.total || 0;
      })
      .catch(function () {});
  }

  // ============================================================
  // LOAD PROFILE BAR
  // ============================================================
  function loadProfileBar() {
    fetch(API + '/api/profile', {
      headers: { 'Authorization': 'Bearer ' + TOKEN }
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.profile) {
          $('profile-bar').innerHTML = '<div class="dash-profile-empty">Aucun profil de recherche. <a href="#" id="setup-profile">Parlez a Lou</a> pour configurer vos criteres.</div>';
          var link = $('setup-profile');
          if (link) link.onclick = function (e) {
            e.preventDefault();
            document.querySelector('.chat-toggle').click();
          };
          return;
        }
        var p = data.profile;
        var tags = [];
        if (p.transaction) tags.push(p.transaction === 'location' ? 'Location' : 'Achat');
        if (p.property_types && p.property_types.length) tags.push(p.property_types.join(', '));
        if (p.budget_max) tags.push('Max ' + formatPrice(p.budget_max) + ' CHF');
        if (p.rooms_min) tags.push(p.rooms_min + '+ pieces');
        if (p.surface_min) tags.push(p.surface_min + '+ m2');

        var zones = (p.zones || []).filter(function (z) { return z && z.city; });
        zones.forEach(function (z) {
          tags.push(z.city + (z.radius_km ? ' (' + z.radius_km + ' km)' : ''));
        });

        var priorities = p.priorities || [];

        $('profile-bar').innerHTML =
          '<div class="dash-profile-tags">' +
            tags.map(function (t) { return '<span class="ptag">' + escapeHtml(t) + '</span>'; }).join('') +
            priorities.map(function (t) { return '<span class="ptag blue">' + escapeHtml(t) + '</span>'; }).join('') +
          '</div>';
      })
      .catch(function () {});
  }

  // ============================================================
  // LOAD PROPERTIES
  // ============================================================
  var currentPage = 1;
  var currentSort = 'score';
  var currentMinScore = 0;

  function loadProperties(page, sort, minScore) {
    currentPage = page;
    currentSort = sort || currentSort;
    currentMinScore = minScore !== undefined ? minScore : currentMinScore;

    var list = $('properties-list');
    list.innerHTML = '<div class="dash-loading">Chargement...</div>';

    var url = API + '/api/properties/' + USER.id +
      '?page=' + page +
      '&per_page=12' +
      '&sort=' + currentSort +
      '&min_score=' + currentMinScore;

    fetch(url)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.properties || data.properties.length === 0) {
          list.innerHTML = '<div class="dash-empty">Aucun bien trouve. Lancez un scraping ou ajustez vos filtres.</div>';
          $('pagination').innerHTML = '';
          return;
        }

        var html = '<div class="prop-grid">';
        data.properties.forEach(function (p) {
          html += renderPropertyCard(p);
        });
        html += '</div>';
        list.innerHTML = html;

        // Pagination
        renderPagination(data.total, data.page, data.per_page);

        // Hook favorite buttons
        list.querySelectorAll('.fav-btn').forEach(function (btn) {
          btn.onclick = function () {
            toggleFavorite(parseInt(this.dataset.id), this);
          };
        });
      })
      .catch(function (err) {
        list.innerHTML = '<div class="dash-empty">Erreur de chargement. Le serveur est peut-etre en veille — reessayez dans 30 secondes.</div>';
      });
  }

  function renderPropertyCard(p) {
    var gradeColors = { A: '#059669', B: '#0369a1', C: '#d97706', D: '#dc2626' };
    var gc = gradeColors[p.grade] || '#94a3b8';

    var img = (p.images && p.images.length > 0)
      ? '<img src="' + escapeHtml(p.images[0]) + '" alt="" class="prop-img" onerror="this.style.display=\'none\'">'
      : '';

    var priceText = p.price ? formatPrice(p.price) + ' CHF' : 'Prix sur demande';
    if (p.unit && p.price) priceText += '<small>/' + escapeHtml(p.unit.split('/')[1] || 'mois') + '</small>';

    var details = [];
    if (p.rooms) details.push(p.rooms + ' pcs');
    if (p.surface) details.push(p.surface + ' m2');
    if (p.floor !== null && p.floor !== undefined) details.push(p.floor + 'e etage');
    if (p.distance_km !== null && p.distance_km !== undefined) details.push(p.distance_km + ' km');

    var sourceLabel = (p.source || '').replace('www.', '').split('.')[0] || 'Source';

    return '<div class="prop-card">' +
      '<div class="prop-card-top">' +
        (img || '<div class="prop-img-placeholder"></div>') +
        '<div class="prop-score" style="background:' + gc + '">' +
          '<span class="prop-score-num">' + p.score + '</span>' +
          '<span class="prop-score-grade">' + p.grade + '</span>' +
        '</div>' +
        '<button class="fav-btn' + (p.is_favorite ? ' active' : '') + '" data-id="' + p.id + '" title="Favori">' +
          (p.is_favorite ? '&#9829;' : '&#9825;') +
        '</button>' +
      '</div>' +
      '<div class="prop-card-body">' +
        '<div class="prop-price">' + priceText + '</div>' +
        '<div class="prop-title">' + escapeHtml(p.title || 'Bien immobilier') + '</div>' +
        '<div class="prop-address">' + escapeHtml(p.address || '') + '</div>' +
        '<div class="prop-details">' + details.join(' &middot; ') + '</div>' +
        '<div class="prop-scores-mini">' +
          scoreBar('Zone', p.score_detail.zone) +
          scoreBar('Budget', p.score_detail.budget) +
          scoreBar('Type', p.score_detail.type) +
          scoreBar('Surface', p.score_detail.surface) +
          scoreBar('Equip.', p.score_detail.equipment) +
        '</div>' +
        '<div class="prop-footer">' +
          '<span class="prop-source">' + escapeHtml(sourceLabel) + '</span>' +
          (p.source_url ? '<a href="' + escapeHtml(p.source_url) + '" target="_blank" rel="noopener" class="prop-link">Voir l\'annonce &rarr;</a>' : '') +
        '</div>' +
      '</div>' +
    '</div>';
  }

  function scoreBar(label, val) {
    var color = val >= 80 ? '#059669' : val >= 60 ? '#0369a1' : val >= 40 ? '#d97706' : '#dc2626';
    return '<div class="score-mini-row">' +
      '<span class="score-mini-lbl">' + label + '</span>' +
      '<div class="score-mini-bar"><div class="score-mini-fill" style="width:' + val + '%;background:' + color + '"></div></div>' +
      '<span class="score-mini-val">' + val + '</span>' +
    '</div>';
  }

  function renderPagination(total, page, perPage) {
    var pages = Math.ceil(total / perPage);
    if (pages <= 1) { $('pagination').innerHTML = ''; return; }

    var html = '<span class="pag-info">' + total + ' biens</span>';
    if (page > 1) html += '<button class="pag-btn" data-page="' + (page - 1) + '">&laquo; Precedent</button>';
    for (var i = 1; i <= pages && i <= 10; i++) {
      html += '<button class="pag-btn' + (i === page ? ' active' : '') + '" data-page="' + i + '">' + i + '</button>';
    }
    if (page < pages) html += '<button class="pag-btn" data-page="' + (page + 1) + '">Suivant &raquo;</button>';

    $('pagination').innerHTML = html;
    $('pagination').querySelectorAll('.pag-btn').forEach(function (btn) {
      btn.onclick = function () {
        loadProperties(parseInt(this.dataset.page), currentSort, currentMinScore);
        window.scrollTo({ top: 0, behavior: 'smooth' });
      };
    });
  }

  // ============================================================
  // FAVORITES
  // ============================================================
  function toggleFavorite(propertyId, btn) {
    fetch(API + '/api/favorite/' + propertyId, {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + TOKEN }
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.action === 'added') {
          btn.classList.add('active');
          btn.innerHTML = '&#9829;';
        } else {
          btn.classList.remove('active');
          btn.innerHTML = '&#9825;';
        }
        // Refresh fav count
        loadStats();
      })
      .catch(function () {});
  }

  // ============================================================
  // CHAT WIDGET
  // ============================================================
  function initChat() {
    var toggle = ce('button', 'chat-toggle', '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M5 18 L8.5 7 L10 12 L12 3 L14 12 L15.5 7 L19 18 Z" fill="#fff" opacity="0.95"/><circle cx="10" cy="13.5" r="1" fill="#fff" opacity="0.5"/><circle cx="14" cy="13.5" r="1" fill="#fff" opacity="0.5"/></svg>');
    document.body.appendChild(toggle);

    var panel = ce('div', 'chat-panel');
    panel.innerHTML =
      '<div class="chat-head"><span>Lou — Assistant IA</span><button id="chat-close">&times;</button></div>' +
      '<div class="chat-body" id="chat-body">' +
        '<div class="chat-msg bot">Bonjour ! Je suis Lou, votre assistant immobilier. Comment puis-je vous aider ?</div>' +
      '</div>' +
      '<div class="chat-input"><input id="chat-in" type="text" placeholder="Votre question..."><button id="chat-send"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg></button></div>';
    document.body.appendChild(panel);

    toggle.onclick = function () {
      panel.classList.toggle('open');
      if (panel.classList.contains('open')) $('chat-in').focus();
    };

    $('chat-close').onclick = function () { panel.classList.remove('open'); };

    function sendMsg() {
      var input = $('chat-in');
      var msg = input.value.trim();
      if (!msg) return;
      input.value = '';

      var body = $('chat-body');
      body.innerHTML += '<div class="chat-msg user">' + escapeHtml(msg) + '</div>';
      body.scrollTop = body.scrollHeight;

      var loading = ce('div', 'chat-msg bot', '...');
      body.appendChild(loading);
      body.scrollTop = body.scrollHeight;

      fetch(API + '/api/chat', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': 'Bearer ' + TOKEN
        },
        body: JSON.stringify({ message: msg, user_id: USER.id })
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          loading.remove();
          var reply = data.message || data.reply || 'Desole, je n\'ai pas compris.';
          body.innerHTML += '<div class="chat-msg bot">' + escapeHtml(reply) + '</div>';

          // Show suggestions
          if (data.suggestions && data.suggestions.length) {
            var sugHtml = '<div class="chat-suggestions">';
            data.suggestions.forEach(function (s) {
              sugHtml += '<button class="chat-sug">' + escapeHtml(s) + '</button>';
            });
            sugHtml += '</div>';
            body.innerHTML += sugHtml;
            body.querySelectorAll('.chat-sug').forEach(function (btn) {
              btn.onclick = function () {
                $('chat-in').value = this.textContent;
                sendMsg();
              };
            });
          }
          body.scrollTop = body.scrollHeight;
        })
        .catch(function () {
          loading.remove();
          body.innerHTML += '<div class="chat-msg bot">Erreur de connexion. Reessayez.</div>';
          body.scrollTop = body.scrollHeight;
        });
    }

    $('chat-send').onclick = sendMsg;
    $('chat-in').onkeydown = function (e) { if (e.key === 'Enter') sendMsg(); };
  }

  // ============================================================
  // HELPERS
  // ============================================================
  function formatPrice(n) {
    if (!n) return '0';
    return n.toString().replace(/\B(?=(\d{3})+(?!\d))/g, "'");
  }

  // ============================================================
  // DASHBOARD CSS
  // ============================================================
  function getDashCSS() {
    return [
      'body{margin:0;background:#f8fafc;color:#0f172a;font-family:Inter,system-ui,sans-serif;-webkit-font-smoothing:antialiased}',

      // Nav
      '.dash-nav{background:#0f172a;padding:14px 32px;display:flex;justify-content:space-between;align-items:center;position:sticky;top:0;z-index:100}',
      '.dash-nav-brand{color:#fff;font-size:20px;font-weight:800;font-family:"Playfair Display",Georgia,serif;text-decoration:none;display:flex;align-items:center;gap:10px}',
      '.dash-logo-wolf{width:30px;height:30px;flex-shrink:0}',
      '.dash-nav-right{display:flex;align-items:center;gap:16px}',
      '.dash-user-email{color:#94a3b8;font-size:13px}',
      '.dash-logout-btn{background:none;border:1px solid #475569;color:#94a3b8;padding:6px 14px;border-radius:8px;cursor:pointer;font-size:13px;transition:all .2s}',
      '.dash-logout-btn:hover{border-color:#e2e8f0;color:#e2e8f0}',

      // Wrap
      '.dash-wrap{max-width:1200px;margin:0 auto;padding:28px 24px}',

      // Header
      '.dash-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;flex-wrap:wrap;gap:16px}',
      '.dash-header h1{font-family:"Playfair Display",Georgia,serif;font-size:28px;margin:0}',
      '.dash-actions{display:flex;gap:10px}',
      '.dash-select{padding:8px 14px;border:1px solid #e2e8f0;border-radius:8px;font-size:13px;background:#fff;cursor:pointer;color:#334155;outline:none}',
      '.dash-select:focus{border-color:#0369a1}',

      // Stats
      '.dash-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:24px}',
      '.dash-stat{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:20px;text-align:center;transition:box-shadow .2s}',
      '.dash-stat:hover{box-shadow:0 4px 16px rgba(0,0,0,.06)}',
      '.dash-stat-num{font-size:32px;font-weight:700;font-family:"Playfair Display",Georgia,serif;color:#0f172a}',
      '.dash-stat-lbl{font-size:13px;color:#64748b;margin-top:4px}',

      // Profile bar
      '.dash-profile-bar{margin-bottom:24px}',
      '.dash-profile-tags{display:flex;flex-wrap:wrap;gap:8px}',
      '.ptag{padding:6px 14px;background:#f1f5f9;border-radius:50px;font-size:13px;color:#64748b}',
      '.ptag.blue{background:rgba(3,105,161,.1);color:#0369a1}',
      '.dash-profile-empty{background:#fff;border:1px dashed #cbd5e1;border-radius:12px;padding:20px;text-align:center;color:#64748b;font-size:14px}',
      '.dash-profile-empty a{color:#0369a1;cursor:pointer}',

      // Properties grid
      '.prop-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:20px}',
      '.prop-card{background:#fff;border:1px solid #e2e8f0;border-radius:14px;overflow:hidden;transition:all .2s}',
      '.prop-card:hover{box-shadow:0 8px 24px rgba(0,0,0,.08);border-color:#cbd5e1;transform:translateY(-2px)}',
      '.prop-card-top{position:relative;height:180px;background:#e2e8f0;overflow:hidden}',
      '.prop-img{width:100%;height:100%;object-fit:cover}',
      '.prop-img-placeholder{width:100%;height:100%;background:linear-gradient(135deg,#e2e8f0,#cbd5e1);display:flex;align-items:center;justify-content:center}',
      '.prop-score{position:absolute;top:12px;left:12px;display:flex;align-items:center;gap:4px;padding:6px 12px;border-radius:8px;color:#fff;font-weight:700}',
      '.prop-score-num{font-size:18px}',
      '.prop-score-grade{font-size:12px;opacity:.9}',
      '.fav-btn{position:absolute;top:12px;right:12px;background:rgba(255,255,255,.9);border:none;width:36px;height:36px;border-radius:50%;font-size:18px;cursor:pointer;color:#94a3b8;display:flex;align-items:center;justify-content:center;transition:all .2s}',
      '.fav-btn:hover,.fav-btn.active{color:#dc2626;background:#fff}',

      // Card body
      '.prop-card-body{padding:16px}',
      '.prop-price{font-size:20px;font-weight:700;color:#0f172a;margin-bottom:4px}',
      '.prop-price small{font-size:13px;color:#64748b;font-weight:400;margin-left:2px}',
      '.prop-title{font-size:14px;color:#334155;margin-bottom:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
      '.prop-address{font-size:13px;color:#64748b;margin-bottom:8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
      '.prop-details{font-size:13px;color:#64748b;margin-bottom:12px}',

      // Score mini bars
      '.prop-scores-mini{margin-bottom:12px}',
      '.score-mini-row{display:flex;align-items:center;gap:8px;margin-bottom:3px}',
      '.score-mini-lbl{font-size:11px;color:#94a3b8;width:42px;text-align:right;flex-shrink:0}',
      '.score-mini-bar{flex:1;height:5px;background:#f1f5f9;border-radius:3px;overflow:hidden}',
      '.score-mini-fill{height:100%;border-radius:3px;transition:width .5s}',
      '.score-mini-val{font-size:11px;color:#64748b;width:22px;text-align:right}',

      // Card footer
      '.prop-footer{display:flex;justify-content:space-between;align-items:center;padding-top:12px;border-top:1px solid #f1f5f9}',
      '.prop-source{font-size:12px;color:#94a3b8;text-transform:capitalize}',
      '.prop-link{font-size:13px;color:#0369a1;text-decoration:none;font-weight:500}',
      '.prop-link:hover{text-decoration:underline}',

      // Loading / Empty
      '.dash-loading{text-align:center;padding:48px;color:#64748b;font-size:15px}',
      '.dash-empty{text-align:center;padding:48px;color:#64748b;font-size:15px;background:#fff;border:1px dashed #cbd5e1;border-radius:12px}',

      // Pagination
      '.dash-pagination{display:flex;align-items:center;justify-content:center;gap:8px;margin-top:28px;padding-bottom:32px;flex-wrap:wrap}',
      '.pag-info{font-size:13px;color:#64748b;margin-right:12px}',
      '.pag-btn{padding:8px 14px;border:1px solid #e2e8f0;background:#fff;border-radius:8px;font-size:13px;cursor:pointer;color:#334155;transition:all .2s}',
      '.pag-btn:hover{border-color:#0369a1;color:#0369a1}',
      '.pag-btn.active{background:#0369a1;color:#fff;border-color:#0369a1}',

      // Chat
      '.chat-toggle{position:fixed;bottom:24px;right:24px;width:56px;height:56px;border-radius:50%;background:#0369a1;color:#fff;border:none;font-size:24px;cursor:pointer;box-shadow:0 4px 20px rgba(3,105,161,.4);z-index:1000;display:flex;align-items:center;justify-content:center;transition:transform .2s}',
      '.chat-toggle:hover{transform:scale(1.1)}',
      '.chat-panel{position:fixed;bottom:90px;right:24px;width:380px;max-width:calc(100vw - 48px);height:500px;background:#fff;border-radius:16px;box-shadow:0 8px 40px rgba(0,0,0,.15);z-index:1000;display:none;flex-direction:column;overflow:hidden}',
      '.chat-panel.open{display:flex}',
      '.chat-head{background:#0f172a;color:#fff;padding:14px 18px;font-weight:600;display:flex;justify-content:space-between;align-items:center;font-size:15px}',
      '.chat-head button{background:none;border:none;color:#fff;font-size:22px;cursor:pointer;opacity:.7}',
      '.chat-head button:hover{opacity:1}',
      '.chat-body{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px}',
      '.chat-msg{max-width:85%;padding:10px 14px;border-radius:12px;font-size:14px;line-height:1.6}',
      '.chat-msg.bot{background:#f1f5f9;color:#0f172a;align-self:flex-start;border-bottom-left-radius:4px}',
      '.chat-msg.user{background:#0369a1;color:#fff;align-self:flex-end;border-bottom-right-radius:4px}',
      '.chat-input{display:flex;border-top:1px solid #e2e8f0;padding:12px}',
      '.chat-input input{flex:1;border:1px solid #e2e8f0;border-radius:8px;padding:10px 14px;font-size:14px;outline:none;font-family:Inter,sans-serif}',
      '.chat-input input:focus{border-color:#0369a1}',
      '.chat-input button{margin-left:8px;background:#0369a1;color:#fff;border:none;border-radius:8px;padding:10px 16px;cursor:pointer;font-size:16px;transition:background .2s}',
      '.chat-input button:hover{background:#0284c7}',
      '.chat-suggestions{display:flex;flex-wrap:wrap;gap:6px;margin-top:4px}',
      '.chat-sug{padding:6px 12px;background:#e0f2fe;border:none;border-radius:20px;font-size:12px;color:#0369a1;cursor:pointer;transition:background .2s}',
      '.chat-sug:hover{background:#bae6fd}',

      // Auth overlay (for landing page)
      '.lou-overlay{position:fixed;inset:0;background:rgba(15,23,42,.7);display:flex;align-items:center;justify-content:center;z-index:9999;backdrop-filter:blur(4px)}',
      '.lou-auth-box{background:#fff;border-radius:16px;padding:36px;width:400px;max-width:90vw;box-shadow:0 20px 60px rgba(0,0,0,.3);position:relative;color:#0f172a}',
      '.lou-auth-box .close-btn{position:absolute;top:12px;right:16px;background:none;border:none;font-size:22px;cursor:pointer;color:#94a3b8}',
      '.lou-auth-box h2{font-size:24px;margin:0 0 4px;font-family:"Playfair Display",Georgia,serif}',
      '.lou-auth-box .sub{font-size:14px;color:#64748b;margin-bottom:20px}',
      '.lou-auth-box input{width:100%;padding:12px 14px;border:1px solid #e2e8f0;border-radius:10px;margin-bottom:12px;font-size:14px;box-sizing:border-box;outline:none}',
      '.lou-auth-box input:focus{border-color:#0369a1;box-shadow:0 0 0 3px rgba(3,105,161,.1)}',
      '.auth-submit{width:100%;padding:13px;border:none;border-radius:10px;background:#0369a1;color:#fff;font-size:15px;font-weight:600;cursor:pointer}',
      '.auth-submit:hover{background:#0284c7}',
      '.lou-auth-switch{text-align:center;margin-top:14px;font-size:13px;color:#64748b}',
      '.lou-auth-switch a{color:#0369a1;cursor:pointer;text-decoration:underline}',
      '.lou-auth-err{color:#dc2626;font-size:13px;margin-top:8px;display:none;text-align:center}',

      // Responsive
      '@media(max-width:768px){',
        '.dash-stats{grid-template-columns:repeat(2,1fr)}',
        '.dash-wrap{padding:16px}',
        '.prop-grid{grid-template-columns:1fr}',
        '.dash-header{flex-direction:column;align-items:flex-start}',
        '.dash-nav{padding:12px 16px}',
        '.chat-panel{width:calc(100vw - 32px);right:16px;bottom:80px;height:60vh}',
      '}'
    ].join('');
  }

  // ============================================================
  // ROUTER — Determine which page to show
  // ============================================================
  var path = window.location.pathname;

  if (path === '/dashboard') {
    showDashboard();
  } else {
    // Landing page — hook CTAs
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', initLanding);
    } else {
      initLanding();
    }
  }

})();
