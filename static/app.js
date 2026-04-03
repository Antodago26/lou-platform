/**
 * Lou Garou — Frontend App (Single File)
 * Auth (login/signup) + Dashboard + Chat
 * Served from /static/app.js on Render
 */

(function () {
  'use strict';

  var API = 'https://lou-platform.onrender.com';
  var TOKEN = localStorage.getItem('lou_token');

  // ============================================================
  // UTILITY
  // ============================================================
  function $(id) { return document.getElementById(id); }
  function ce(tag, cls, html) {
    var el = document.createElement(tag);
    if (cls) el.className = cls;
    if (html) el.innerHTML = html;
    return el;
  }
  function isJWT(t) { return t && t.split('.').length === 3; }

  // ============================================================
  // AUTH — Login / Signup Modal
  // ============================================================
  function showAuth() {
    document.body.innerHTML = '';
    var css = ce('style', '', [
      'body{margin:0;background:#0f172a;font-family:system-ui,sans-serif;color:#0f172a}',
      '.auth-wrap{position:fixed;inset:0;display:flex;align-items:center;justify-content:center}',
      '.auth-box{background:#fff;border-radius:16px;padding:36px;width:380px;max-width:90vw;box-shadow:0 20px 60px rgba(0,0,0,.3)}',
      '.auth-box h2{font-size:24px;margin:0 0 4px;font-family:Georgia,serif}',
      '.auth-box .sub{font-size:14px;color:#64748b;margin-bottom:20px}',
      '.auth-box input{width:100%;padding:10px 14px;border:1px solid #e2e8f0;border-radius:10px;margin-bottom:10px;font-size:14px;box-sizing:border-box;outline:none}',
      '.auth-box input:focus{border-color:#0369a1}',
      '.auth-box button{width:100%;padding:12px;border:none;border-radius:10px;background:#0369a1;color:#fff;font-size:15px;font-weight:600;cursor:pointer;transition:background .2s}',
      '.auth-box button:hover{background:#0284c7}',
      '.auth-switch{text-align:center;margin-top:14px;font-size:13px;color:#64748b}',
      '.auth-switch a{color:#0369a1;cursor:pointer;text-decoration:underline}',
      '.auth-err{color:#dc2626;font-size:13px;margin-top:8px;display:none;text-align:center}'
    ].join(''));
    document.head.appendChild(css);

    var wrap = ce('div', 'auth-wrap');
    wrap.innerHTML = [
      '<div class="auth-box">',
      '<h2>Lou Garou</h2>',
      '<div class="sub">Votre chasseur immobilier IA en Suisse</div>',
      '<input id="auth-email" type="email" placeholder="Email">',
      '<input id="auth-pass" type="password" placeholder="Mot de passe">',
      '<input id="auth-name" type="text" placeholder="Votre nom" style="display:none">',
      '<button id="auth-btn">Se connecter</button>',
      '<div class="auth-switch"><a id="auth-toggle">Créer un compte</a></div>',
      '<div class="auth-err" id="auth-err"></div>',
      '</div>'
    ].join('');
    document.body.appendChild(wrap);

    var mode = 'login';
    $('auth-toggle').onclick = function () {
      mode = mode === 'login' ? 'signup' : 'login';
      $('auth-name').style.display = mode === 'signup' ? 'block' : 'none';
      $('auth-btn').textContent = mode === 'signup' ? "S'inscrire" : 'Se connecter';
      this.textContent = mode === 'signup' ? 'Déjà un compte ? Se connecter' : 'Créer un compte';
      $('auth-err').style.display = 'none';
    };

    $('auth-btn').onclick = function () {
      var email = $('auth-email').value.trim();
      var pass = $('auth-pass').value;
      var name = $('auth-name').value.trim();
      var err = $('auth-err');
      var btn = this;

      if (!email || !pass) {
        err.textContent = 'Email et mot de passe requis';
        err.style.display = 'block';
        return;
      }
      btn.textContent = 'Chargement...';
      var body = mode === 'signup' ? { email: email, password: pass, name: name } : { email: email, password: pass };
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
            // Clear old fake data
            localStorage.removeItem('lou_profile');
            localStorage.removeItem('lou_bk');
            location.reload();
          } else {
            err.textContent = data.error || 'Erreur de connexion';
            err.style.display = 'block';
            btn.textContent = mode === 'signup' ? "S'inscrire" : 'Se connecter';
          }
        })
        .catch(function () {
          err.textContent = 'Erreur réseau — réessayez';
          err.style.display = 'block';
          btn.textContent = mode === 'signup' ? "S'inscrire" : 'Se connecter';
        });
    };

    // Allow Enter key to submit
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && $('auth-btn')) $('auth-btn').click();
    });
  }

  // ============================================================
  // CSS — Dashboard Styles
  // ============================================================
  function injectCSS() {
    var s = ce('style', '', [
      'body{margin:0;background:#fafbfc;color:#0f172a;font-family:system-ui,sans-serif}',
      '.nav{background:#0f172a;padding:14px 32px;display:flex;justify-content:space-between;align-items:center}',
      '.nav-brand{color:#fff;font-size:20px;font-weight:800;font-family:Georgia,serif}',
      '.nav-right{color:#94a3b8;font-size:14px;display:flex;align-items:center;gap:12px}',
      '.nav-right button{background:none;border:1px solid #475569;color:#94a3b8;padding:5px 12px;border-radius:8px;cursor:pointer;font-size:12px}',
      '.wrap{max-width:1100px;margin:0 auto;padding:32px}',
      '.dash-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;flex-wrap:wrap;gap:16px}',
      '.dash-header h1{font-family:Georgia,serif;font-size:28px;margin:0}',
      '.profile-card{background:#fff;border:1px solid #e2e8f0;border-radius:16px;padding:24px;margin-bottom:24px;display:flex;justify-content:space-between;align-items:start;flex-wrap:wrap;gap:16px}',
      '.tags{display:flex;flex-wrap:wrap;gap:8px}',
      '.tags span{padding:6px 14px;background:#f1f5f9;border-radius:50px;font-size:13px;color:#64748b}',
      '.tags .blue{background:rgba(3,105,161,.1);color:#0369a1}',
      '.score-num{font-size:36px;font-weight:800;color:#0369a1;font-family:Georgia,serif}',
      '.grade{display:inline-block;padding:3px 12px;border-radius:50px;font-size:11px;font-weight:700;color:#fff;margin-top:4px}',
      '.stats-row{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:24px}',
      '.stat-card{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:16px;text-align:center}',
      '.stat-card .num{font-size:28px;font-weight:700;font-family:Georgia,serif}',
      '.stat-card .lbl{font-size:12px;color:#64748b;margin-top:2px}',
      '.lk{display:inline-block;margin-top:12px;padding:8px 20px;background:#0369a1;color:#fff;border-radius:10px;text-decoration:none;font-size:14px;font-weight:600}',
      '.lk:hover{background:#0284c7}',
      // Chat styles
      '.chat-toggle{position:fixed;bottom:24px;right:24px;width:56px;height:56px;border-radius:50%;background:#0369a1;color:#fff;border:none;font-size:24px;cursor:pointer;box-shadow:0 4px 16px rgba(0,0,0,.2);z-index:1000;display:flex;align-items:center;justify-content:center}',
      '.chat-panel{position:fixed;bottom:90px;right:24px;width:380px;max-width:calc(100vw - 48px);height:480px;background:#fff;border-radius:16px;box-shadow:0 8px 32px rgba(0,0,0,.15);z-index:1000;display:none;flex-direction:column;overflow:hidden}',
      '.chat-panel.open{display:flex}',
      '.chat-head{background:#0f172a;color:#fff;padding:14px 18px;font-weight:600;display:flex;justify-content:space-between;align-items:center}',
      '.chat-head button{background:none;border:none;color:#fff;font-size:20px;cursor:pointer}',
      '.chat-body{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px}',
      '.chat-msg{max-width:85%;padding:10px 14px;border-radius:12px;font-size:14px;line-height:1.5}',
      '.chat-msg.bot{background:#f1f5f9;color:#0f172a;align-self:flex-start;border-bottom-left-radius:4px}',
      '.chat-msg.user{background:#0369a1;color:#fff;align-self:flex-end;border-bottom-right-radius:4px}',
      '.chat-input{display:flex;border-top:1px solid #e2e8f0;padding:10px}',
      '.chat-input input{flex:1;border:1px solid #e2e8f0;border-radius:8px;padding:8px 12px;font-size:14px;outline:none}',
      '.chat-input button{margin-left:8px;background:#0369a1;color:#fff;border:none;border-radius:8px;padding:8px 14px;cursor:pointer;font-size:14px}',
      '@media(max-width:768px){.stats-row{grid-template-columns:repeat(2,1fr)}.wrap{padding:16px}.profile-card{flex-direction:column}.chat-panel{width:calc(100vw - 32px);right:16px;bottom:80px;height:60vh}}'
    ].join(''));
    document.head.appendChild(s);
  }

  // ============================================================
  // DASHBOARD — Render
  // ============================================================
  function showDashboard() {
    injectCSS();
    document.body.innerHTML = '';

    var user = JSON.parse(localStorage.getItem('lou_user') || '{}');
    var profile = JSON.parse(localStorage.getItem('lou_profile') || '{}');
    var criteria = profile.criteria || {};
    var score = profile.score || { t: 0, g: '-' };

    var gradeColors = {
      'A': '#059669', 'B': '#0369a1', 'C': '#d97706',
      'D': '#dc2626', 'E': '#7c3aed', '-': '#94a3b8'
    };
    var gc = gradeColors[score.g] || '#94a3b8';

    // Nav
    var nav = ce('div', 'nav');
    nav.innerHTML = '<div class="nav-brand">Lou Garou</div>' +
      '<div class="nav-right"><span>' + (user.email || '') + '</span>' +
      '<button id="logout-btn">Déconnexion</button></div>';
    document.body.appendChild(nav);

    // Wrap
    var wrap = ce('div', 'wrap');

    // Header
    wrap.innerHTML = '<div class="dash-header"><h1>Mon Dashboard</h1></div>';

    // Profile card with tags
    var tags = [
      criteria.canton, criteria.city, criteria.type,
      criteria.transaction, criteria.budget, criteria.rooms
    ].filter(Boolean).map(function (t) { return '<span>' + t + '</span>'; }).join('');

    var priorities = (criteria.priorities || []).map(function (p) {
      return '<span class="blue">' + p + '</span>';
    }).join('');

    wrap.innerHTML += '<div class="profile-card">' +
      '<div><div class="tags">' + tags + '</div>' +
      '<div class="tags" style="margin-top:8px">' + priorities + '</div>' +
      '<a class="lk" href="' + API + '/static/profil.html?token=' + TOKEN + '">Mes critères</a></div>' +
      '<div style="text-align:center"><div class="score-num">' + score.t + '<span style="font-size:16px;color:#94a3b8">/100</span></div>' +
      '<div class="grade" style="background:' + gc + '">Classe ' + score.g + '</div></div></div>';

    // Stats row
    var cats = score.cats || [
      { n: 'Biens', s: 0 }, { n: 'Nouveaux', s: 0 },
      { n: 'Favoris', s: 0 }, { n: 'Score moyen', s: 0 }
    ];
    var statsHtml = '<div class="stats-row">';
    cats.forEach(function (c) {
      statsHtml += '<div class="stat-card"><div class="num">' + c.s + '</div>' +
        '<div class="lbl">' + c.n + '</div></div>';
    });
    statsHtml += '</div>';
    wrap.innerHTML += statsHtml;

    document.body.appendChild(wrap);

    // Logout
    setTimeout(function () {
      var lb = $('logout-btn');
      if (lb) lb.onclick = function () {
        localStorage.removeItem('lou_token');
        localStorage.removeItem('lou_user');
        localStorage.removeItem('lou_profile');
        localStorage.removeItem('lou_bk');
        location.reload();
      };
    }, 100);

    // Init chat
    initChat();

    // Load real profile from API
    loadProfile();
  }

  // ============================================================
  // PROFILE — Load from API
  // ============================================================
  function loadProfile() {
    fetch(API + '/api/profile', {
      headers: { 'Authorization': 'Bearer ' + TOKEN }
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.profile) {
          var p = data.profile;
          var profile = {
            email: data.email || '',
            criteria: {
              canton: (p.zones && p.zones[0]) ? p.zones[0].canton : '',
              city: (p.zones && p.zones[0]) ? p.zones[0].city : '',
              type: (p.property_types || [])[0] || '',
              transaction: p.transaction || '',
              budget: formatBudget(p.budget_min, p.budget_max),
              rooms: p.rooms_min ? p.rooms_min + ' pièces' : '',
              priorities: p.priorities || []
            },
            score: { t: 0, g: '-', cats: [] }
          };
          localStorage.setItem('lou_profile', JSON.stringify(profile));
          localStorage.setItem('lou_user', JSON.stringify({ id: data.user_id, email: data.email, name: data.name }));
        }
      })
      .catch(function () { /* silently fail */ });
  }

  function formatBudget(min, max) {
    if (!min && !max) return '';
    var fmt = function (n) {
      if (!n) return '';
      if (n >= 1000000) return (n / 1000000) + 'M';
      if (n >= 1000) return (n / 1000) + 'K';
      return n;
    };
    if (min && max) return fmt(min) + '-' + fmt(max);
    if (min) return 'dès ' + fmt(min);
    return 'max ' + fmt(max);
  }

  // ============================================================
  // CHAT — Chatbot Lou
  // ============================================================
  function initChat() {
    // Toggle button
    var toggle = ce('button', 'chat-toggle', '🐺');
    document.body.appendChild(toggle);

    // Chat panel
    var panel = ce('div', 'chat-panel');
    panel.innerHTML = [
      '<div class="chat-head"><span>Lou — Assistant IA</span><button id="chat-close">✕</button></div>',
      '<div class="chat-body" id="chat-body">',
      '<div class="chat-msg bot">Bonjour ! Je suis Lou, votre assistant immobilier. Comment puis-je vous aider ?</div>',
      '</div>',
      '<div class="chat-input"><input id="chat-in" type="text" placeholder="Votre question..."><button id="chat-send">➤</button></div>'
    ].join('');
    document.body.appendChild(panel);

    toggle.onclick = function () {
      panel.classList.toggle('open');
      if (panel.classList.contains('open')) $('chat-in').focus();
    };

    setTimeout(function () {
      $('chat-close').onclick = function () { panel.classList.remove('open'); };

      function sendMsg() {
        var input = $('chat-in');
        var msg = input.value.trim();
        if (!msg) return;
        input.value = '';

        var body = $('chat-body');
        body.innerHTML += '<div class="chat-msg user">' + escapeHtml(msg) + '</div>';
        body.scrollTop = body.scrollHeight;

        // Loading indicator
        var loading = ce('div', 'chat-msg bot', '...');
        body.appendChild(loading);
        body.scrollTop = body.scrollHeight;

        fetch(API + '/api/chat', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + TOKEN
          },
          body: JSON.stringify({ message: msg })
        })
          .then(function (r) { return r.json(); })
          .then(function (data) {
            loading.remove();
            body.innerHTML += '<div class="chat-msg bot">' + escapeHtml(data.reply || data.response || 'Désolé, je n\'ai pas compris.') + '</div>';
            body.scrollTop = body.scrollHeight;
          })
          .catch(function () {
            loading.remove();
            body.innerHTML += '<div class="chat-msg bot">Erreur de connexion. Réessayez.</div>';
            body.scrollTop = body.scrollHeight;
          });
      }

      $('chat-send').onclick = sendMsg;
      $('chat-in').onkeydown = function (e) { if (e.key === 'Enter') sendMsg(); };
    }, 200);
  }

  function escapeHtml(text) {
    var div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  // ============================================================
  // INIT — Main Entry Point
  // ============================================================
  if (!isJWT(TOKEN)) {
    showAuth();
  } else {
    window._LA = API;
    showDashboard();
  }

})();
