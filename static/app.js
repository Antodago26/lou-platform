/**
 * Bon Home — Frontend App
 * Landing page auth + Full Dashboard with real properties
 */
(function () {
  'use strict';

  // Dynamic API URL — works on any host (bonhome.ch, onrender, localhost)
  var API = window.location.origin;
  var TOKEN = localStorage.getItem('lou_token');
  var USER = null;
  try { USER = JSON.parse(localStorage.getItem('lou_user') || 'null'); } catch(e) { localStorage.removeItem('lou_user'); }

  // Accumulated chatbot criteria (persists across messages).
  // v6.3.1 Bug #3: also persisted to localStorage so anonymous criteria
  // survive a page reload between chat and signup — without this, a user
  // who closes the tab after chatting loses their criteria and sees an
  // empty profile after signup.
  var chatCriteria = {};
  try {
    var _storedCriteria = localStorage.getItem('lou_anon_criteria');
    if (_storedCriteria) chatCriteria = JSON.parse(_storedCriteria) || {};
  } catch (_) { localStorage.removeItem('lou_anon_criteria'); }

  function _persistChatCriteria() {
    try {
      if (chatCriteria && Object.keys(chatCriteria).length > 0) {
        localStorage.setItem('lou_anon_criteria', JSON.stringify(chatCriteria));
      }
    } catch (_) {}
  }

  // v6.3.2 Bug #1 hardening — lou_first_login a désormais une TTL de 60s
  // (durée max plausible d'un scoring sync + bg rescore). Avant, le flag
  // persistait indéfiniment si aucun timer ne le nettoyait : un user qui
  // fermait/rouvrait l'onglet pendant la fenêtre de scoring se retrouvait
  // bloqué en permanence sur "Lou est en chasse" sans progression.
  // Compat : lit aussi l'ancien format 'true' sans TTL (fallback).
  var _FIRST_LOGIN_TTL_MS = 60000;
  function _setFirstLogin() {
    try {
      localStorage.setItem('lou_first_login', JSON.stringify({
        value: true,
        expires_at: Date.now() + _FIRST_LOGIN_TTL_MS
      }));
    } catch (_) {}
  }
  function _isFirstLoginActive() {
    var raw = localStorage.getItem('lou_first_login');
    if (!raw) return false;
    try {
      var parsed = JSON.parse(raw);
      if (parsed && parsed.value && typeof parsed.expires_at === 'number') {
        if (Date.now() < parsed.expires_at) return true;
        // Expiré : nettoie pour éviter relecture.
        localStorage.removeItem('lou_first_login');
        return false;
      }
    } catch (_) {
      // Fallback compat : ancien format 'true' sans TTL.
      return raw === 'true';
    }
    return false;
  }
  function _clearFirstLogin() {
    try { localStorage.removeItem('lou_first_login'); } catch (_) {}
  }

  // Map cache — must live at IIFE scope because saveProfileForm() invalidates
  // it from outside showDashboard() (where the map code lives). Previously
  // declared as `var _mapAllProps` inside showDashboard, which made the
  // assignment in saveProfileForm throw ReferenceError under 'use strict' and
  // silently rejected the whole save chain.
  var _mapAllProps = null;

  // Favorites compare state (shared between showDashboard and helper functions)
  var compareMode = false;
  var compareSet = {};

  // hCaptcha site key (set via backend config endpoint, or use default)
  var HCAPTCHA_SITEKEY = '';
  // Load hCaptcha script once
  var _hcaptchaLoaded = false;
  function loadHCaptcha() {
    if (_hcaptchaLoaded) return;
    _hcaptchaLoaded = true;
    var s = document.createElement('script');
    s.src = 'https://js.hcaptcha.com/1/api.js?render=explicit';
    s.async = true;
    document.head.appendChild(s);
  }
  // Helper: log a fetch failure with context instead of swallowing silently.
  // Use for catches that shouldn't surface UI but we still want visible in console/Sentry.
  function _logErr(ctx) {
    return function (err) {
      try { console.warn('[lou] ' + ctx + ':', err && err.message ? err.message : err); } catch (_) {}
    };
  }

  // Minimal inline toast — auto-dismisses after 3.5s. Used for transient errors
  // where a full modal would be overkill.
  function showToast(msg, kind) {
    try {
      var el = document.createElement('div');
      el.className = 'lou-toast' + (kind === 'success' ? ' lou-toast-ok' : '');
      el.textContent = msg;
      document.body.appendChild(el);
      setTimeout(function () { el.classList.add('lou-toast-show'); }, 10);
      setTimeout(function () {
        el.classList.remove('lou-toast-show');
        setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 300);
      }, 3500);
    } catch (_) {}
  }

  // Fetch captcha config from backend
  fetch(API + '/api/config').then(function(r){ return r.json(); }).then(function(d){
    if (d.hcaptcha_sitekey) { HCAPTCHA_SITEKEY = d.hcaptcha_sitekey; loadHCaptcha(); }
  }).catch(_logErr('config fetch'));

  // Stable anonymous session ID (persists in localStorage so chat history works)
  var ANON_SESSION = localStorage.getItem('lou_anon_session');
  if (!ANON_SESSION) {
    ANON_SESSION = 'anon-' + Date.now() + '-' + Math.random().toString(36).substr(2, 6);
    localStorage.setItem('lou_anon_session', ANON_SESSION);
  }

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
    return div.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // SVG icon helper — clean inline icons to replace emojis
  var ICO = {
    pin: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display:inline-block;vertical-align:-2px"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg>',
    home: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display:inline-block;vertical-align:-2px"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>',
    money: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display:inline-block;vertical-align:-2px"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>',
    ruler: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display:inline-block;vertical-align:-2px"><path d="M1 3h6l2 2-7 7-2-2V4a1 1 0 0 1 1-1z"/><path d="M14 3h6a1 1 0 0 1 1 1v6l-2 2-7-7 2-2z"/><path d="M3 14l7 7 2-2-7-7-2 2z"/><path d="M14 21l7-7-2-2-7 7 2 2z"/></svg>',
    star: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display:inline-block;vertical-align:-2px"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>',
    search: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display:inline-block;vertical-align:-2px"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>',
    user: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display:inline-block;vertical-align:-2px"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>',
    phone: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display:inline-block;vertical-align:-2px"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.127.96.361 1.903.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0 1 22 16.92z"/></svg>',
    mail: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display:inline-block;vertical-align:-2px"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>',
    wolf: '<svg width="48" height="48" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><rect width="48" height="48" rx="12" fill="#2A6670"/><circle cx="21" cy="20" r="11" fill="none" stroke="#fff" stroke-width="2.8"/><line x1="29" y1="28" x2="39" y2="38" stroke="#fff" stroke-width="2.8" stroke-linecap="round"/><path d="M21 13 L14 19 L15.5 19 L15.5 26 L26.5 26 L26.5 19 L28 19 Z" fill="#fff" opacity="0.95"/><rect x="19.5" y="22" width="3" height="4" rx="0.5" fill="#2A6670"/></svg>',
    check: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="display:inline-block;vertical-align:-2px"><polyline points="20 6 9 17 4 12"/></svg>',
  };

  // Detect English-language titles (scraped from international ads) — we'd rather
  // fall back to city name than show English copy on a French UI.
  var EN_TITLE_RE = /\b(bedroom|bathroom|living\s*room|ground\s*floor|top\s*floor|fully\s*furnished|for\s*rent|for\s*sale|available\s*(from|now)|walking\s*distance|close\s*to|in\s*the\s*heart|stunning|spacious|charming|beautiful|apartment|\bflat\b|\d+\s*(bed|bath)rooms?)\b/i;

  // German real-estate titles from Comparis/ImmoScout DE ads.
  // "Zimmer" is the safest anchor — "3.5 Zimmer" or "Zimmer, 100 m²" are
  // unambiguously German. We also catch standalone descriptors (wohnung,
  // erdgeschoss, etc.) and floor abbreviations (EG/OG/UG/DG when preceded
  // by a comma or digit, to avoid false positives on short strings).
  var DE_TITLE_RE = /\b(zimmer|wohnung|erdgeschoss|obergeschoss|untergeschoss|dachgeschoss|stockwerk|mietwohnung|eigentumswohnung|möbliert|renoviert|(?:\d[.,]?\d?\s*|\,\s*)(?:EG|OG|UG|DG)\b)\b/i;

  function cleanTitle(t, prop) {
    if (!t) return '';
    t = t.trim();

    // Pattern B: ANY title with a middle-dot/bullet separator is structured data
    // ("Appartement · 4.5 pièces · 116 m²", "Appartement · 340 m²")
    // Covers U+00B7 (·), U+2022 (•), U+2027 (‧), and other bullet-like chars.
    // No real estate listing title ever uses these characters.
    if (/[\u00B7\u2022\u2027\u2219\u22C5\u25CF]/.test(t)) return '';

    // Drop English/German titles entirely
    if (EN_TITLE_RE.test(t) || DE_TITLE_RE.test(t)) return '';

    // Pattern E: metadata garbage ("Travel time -", "Travel time", "CH 2016 Cortaillod")
    if (/^travel\s+time/i.test(t)) return '';
    if (/^ch\s+\d{4}/i.test(t)) return '';

    // Remove price prefixes left in DB: "CHF 1,630.–", "CHF 2,200.–Plus", "1'590.–"
    t = t.replace(/^CHF\s*[\d\s'',.\u2019]+[.\u2013\u2014\-]*\w*\s*/i, '').trim();
    t = t.replace(/^[\d\s'',.\u2019]+(?:[\u2013\u2014]|\s*(?:CHF|Fr\.?))[\u2013\u2014\-.]*\s*/i, '').trim();

    // Pattern C: prefix "NPA Ville Canton" e.g. "2013 Colombier NE ..."
    t = t.replace(/^\d{4}\s+[A-ZÀ-Ü][a-zà-ü]+(?:\s+[A-Z]{2})?\s+/, '').trim();
    // Fallback: remove bare leading postal codes "2034 "
    t = t.replace(/^\d{4}\s+/, '').trim();

    // Remove "Travel time" residuals from Homegate (with or without minutes)
    t = t.replace(/\bTravel time\s*(\d+\s*min)?\s*-?\s*/gi, '').trim();
    t = t.replace(/\btemps de trajet\s+\d+\s*min\b/gi, '').trim();

    // Pattern D: trailing "NPA Ville" e.g. "Appartement à vendre 1114 Colombier"
    t = t.replace(/\s+\d{4}\s+[A-ZÀ-Ü][a-zà-ü]+\s*$/, '').trim();

    // Re-check for garbage after NPA/travel-time strip
    if (/^travel\s+time/i.test(t)) return '';

    // If result is empty, just punctuation, or just a single word that's a city name
    if (/^[\s.\u2013\u2014\-]*$/.test(t)) return '';
    if (t.length <= 2 || !/[a-zA-ZÀ-ÿ]{2,}/.test(t)) return '';
    if (prop && prop.city && t.toLowerCase() === prop.city.toLowerCase()) return '';

    // Comma-separated structured data: "4.5 pcs, 109 m²" or "3.5 pièces, 125 m2"
    if (/^\d+[.,]?\d*\s*(pcs|pi[èe]ces?)\b/i.test(t)) return '';

    // Final length check
    t = t.trim();
    if (t.length <= 3) return '';

    return t;
  }

  // Build a descriptive fallback title when cleanTitle returns empty.
  // v6.1 fix: no · separator (cleanTitle REJECTS titles with ·, and if we
  // rebuild with · the fix destroys itself). Also drop surface — it's already
  // shown on the details line below the title ("4.5 pcs · 114 m²").
  function _fallbackTitle(p) {
    var type = 'Appartement';
    var pt = (p.property_type || '').toLowerCase();
    if (/maison|house|villa|chalet/.test(pt)) type = 'Maison';
    else if (/terrain|land/.test(pt)) type = 'Terrain';
    else if (/commercial|bureau|office/.test(pt)) type = 'Local commercial';
    else if (/parking|garage/.test(pt)) type = 'Parking';
    if (p.rooms && p.rooms > 0 && p.rooms < 20) {
      return type + ' ' + p.rooms + ' pièces';
    }
    return type;
  }

  // v6.1 Bug 2 fix: clean a raw city field from DB.
  // Handles "CH 2016 Cortaillod", ". 2016 Cortaillod", "Cortaillod 2016",
  // "Neuchâtel NE", bare NPAs ("2074"), etc.
  function cleanCity(raw) {
    if (!raw) return '';
    var c = String(raw).trim();
    // Remove "CH " / "ch " / leading "." prefix (common scraper garbage)
    c = c.replace(/^(CH|ch)\s+/, '');
    c = c.replace(/^[.,;:]\s*/, '');
    // Remove leading NPA: "2016 Cortaillod" → "Cortaillod"
    c = c.replace(/^\d{4}\s+/, '');
    // Remove trailing NPA: "Cortaillod 2016" → "Cortaillod"
    c = c.replace(/\s+\d{4}\s*$/, '');
    // Remove trailing canton abbreviation: "Neuchâtel NE" → "Neuchâtel"
    c = c.replace(/\s+[A-Z]{2}\s*$/, '');
    c = c.trim();
    // Bare NPA ("2074") — nothing useful, return empty
    if (/^\d{4}$/.test(c)) return '';
    // Two characters or less → probably garbage
    if (c.length <= 2) return '';
    return c;
  }

  // v6.1 Bug 2 fix: clean a raw address field.
  // When the address is a full street address ("Rue des Chavannes 57 2016 Cortaillod"),
  // we only want the city — strip the street portion and return just the locality.
  function cleanAddress(raw, city) {
    if (!raw) return cleanCity(city || '');
    var a = String(raw).trim()
      .replace(/\bTravel time\s+\d+\s*min\b/gi, '')
      .replace(/\btemps de trajet\s+\d+\s*min\b/gi, '')
      .trim();
    // Full street address? Look for "NPA + city" at the end and keep only that.
    var m = a.match(/\d{4}\s+([A-ZÀ-Ÿa-zà-ÿ][\w'\-\s]*?)\s*$/);
    if (m) {
      // Heuristic: if the string has digits near the start (street number), it's a full address
      if (/^\d+,?\s*(rue|chemin|route|avenue|place|impasse|boulevard|quai|allée|all\u00e9e)\b/i.test(a) ||
          /(rue|chemin|route|avenue|place|impasse|boulevard|quai|allée|all\u00e9e)\s+.+\d/i.test(a) ||
          a.length > 30) {
        return m[1].trim();
      }
    }
    // Otherwise treat as a city/locality string and clean it
    var cleaned = cleanCity(a);
    return cleaned || cleanCity(city || '');
  }

  // Wrapper for authenticated API calls — handles 401 (expired token)
  function apiFetch(url, opts) {
    opts = opts || {};
    opts.headers = opts.headers || {};
    if (TOKEN) opts.headers['Authorization'] = 'Bearer ' + TOKEN;
    return fetch(url, opts).then(function (r) {
      if (r.status === 401) {
        // Token expired or invalid — force re-login
        localStorage.removeItem('lou_token');
        localStorage.removeItem('lou_user');
        TOKEN = null;
        USER = null;
        showAuthModal();
        return Promise.reject(new Error('Session expiree'));
      }
      return r;
    });
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
      '<h2><svg style="width:28px;height:28px;vertical-align:middle;margin-right:8px" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><rect width="48" height="48" rx="12" fill="#2A6670"/><circle cx="21" cy="20" r="11" fill="none" stroke="#fff" stroke-width="2.8"/><line x1="29" y1="28" x2="39" y2="38" stroke="#fff" stroke-width="2.8" stroke-linecap="round"/><path d="M21 13 L14 19 L15.5 19 L15.5 26 L26.5 26 L26.5 19 L28 19 Z" fill="#fff" opacity="0.95"/><rect x="19.5" y="22" width="3" height="4" rx="0.5" fill="#2A6670"/></svg>Bon Home</h2>',
      '<div class="sub">Votre chasseur immobilier IA en Suisse</div>',
      '<input id="lou-auth-email" type="email" placeholder="Email">',
      '<input id="lou-auth-pass" type="password" placeholder="Mot de passe">',
      '<input id="lou-auth-name" type="text" placeholder="Votre nom" style="display:none">',
      '<div id="lou-hcaptcha" style="display:none;margin-bottom:12px"></div>',
      '<button class="auth-submit" id="lou-auth-btn">Se connecter</button>',
      '<div class="lou-auth-switch"><a id="lou-auth-toggle">Créer un compte</a></div>',
      '<div class="lou-auth-err" id="lou-auth-err"></div>',
      '</div>'
    ].join('');
    document.body.appendChild(overlay);

    overlay.addEventListener('click', function (e) {
      if (e.target === overlay) overlay.remove();
    });
    $('lou-auth-close').onclick = function () { overlay.remove(); };

    var mode = 'login';
    var _hcaptchaWidgetId = null;
    $('lou-auth-toggle').onclick = function () {
      mode = mode === 'login' ? 'signup' : 'login';
      $('lou-auth-name').style.display = mode === 'signup' ? 'block' : 'none';
      $('lou-auth-btn').textContent = mode === 'signup' ? "S'inscrire" : 'Se connecter';
      this.textContent = mode === 'signup' ? 'Déjà un compte ? Se connecter' : 'Créer un compte';
      $('lou-auth-err').style.display = 'none';
      // Show/hide hCaptcha
      var hcDiv = $('lou-hcaptcha');
      if (mode === 'signup' && HCAPTCHA_SITEKEY) {
        hcDiv.style.display = 'block';
        if (_hcaptchaWidgetId === null && window.hcaptcha) {
          _hcaptchaWidgetId = window.hcaptcha.render('lou-hcaptcha', { sitekey: HCAPTCHA_SITEKEY, size: 'normal' });
        }
      } else {
        hcDiv.style.display = 'none';
      }
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

      // Get hCaptcha token for signup
      var captchaToken = '';
      if (mode === 'signup' && HCAPTCHA_SITEKEY && window.hcaptcha) {
        captchaToken = window.hcaptcha.getResponse(_hcaptchaWidgetId) || '';
        if (!captchaToken) {
          err.textContent = 'Veuillez completer le CAPTCHA';
          err.style.display = 'block';
          btn.textContent = "S'inscrire";
          btn.disabled = false;
          return;
        }
      }

      var body = mode === 'signup'
        ? { email: email, password: pass, name: name, criteria: chatCriteria, captcha_token: captchaToken }
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
            TOKEN = data.token;
            USER = data.user;
            // v6.3.1 Bug #3: signup already transferred `criteria` in the body.
            // Clear the anonymous cache so it doesn't resurface on future
            // anonymous visits on the same device.
            try { localStorage.removeItem('lou_anon_criteria'); } catch (_) {}
            // If signup, trigger initial scraping so results appear quickly
            if (mode === 'signup') {
              _setFirstLogin();
              fetch(API + '/api/scrape', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + data.token },
                body: '{}'
              }).catch(_logErr('signup scrape trigger'));
              // Also trigger scoring for any existing properties in DB
              fetch(API + '/api/score', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + data.token },
                body: '{}'
              }).catch(_logErr('signup score trigger'));
            }
            // Bug #0A fix: if the user logged in AFTER a chat (where chatCriteria
            // was accumulated but not yet sent), push the criteria now.
            // Signup already sends them in the body; login doesn't, so we push via PUT.
            if (mode === 'login' && chatCriteria && Object.keys(chatCriteria).length > 0) {
              var criteriaPayload = {
                property_types: chatCriteria.property_types || (chatCriteria.property_type ? [chatCriteria.property_type] : ['appartement']),
                transaction: chatCriteria.transaction || 'location',
                budget_max: chatCriteria.budget_max,
                budget_min: chatCriteria.budget_min,
                rooms_min: chatCriteria.rooms_min,
                rooms_max: chatCriteria.rooms_max,
                surface_min: chatCriteria.surface_min,
                priorities: chatCriteria.priorities || [],
                zones: (chatCriteria.zones || []).map(function (z) {
                  return { city: z.city || '', canton: z.canton || '', radius_km: z.radius_km || 3,
                           latitude: z.latitude || null, longitude: z.longitude || null, postal_code: z.postal_code || null };
                })
              };
              fetch(API + '/api/profile', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + data.token },
                body: JSON.stringify(criteriaPayload)
              }).catch(_logErr('login post-chat profile push'));
            }
            // On external hosts (Webflow), render dashboard in place
            var isRenderHost = window.location.hostname === 'lou-platform.onrender.com' || window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';
            if (isRenderHost) {
              window.location.href = '/dashboard';
            } else {
              overlay.remove();
              showDashboard();
            }
          } else {
            err.textContent = data.error || 'Erreur de connexion';
            err.style.display = 'block';
            btn.textContent = mode === 'signup' ? "S'inscrire" : 'Se connecter';
            btn.disabled = false;
            if (window.hcaptcha && _hcaptchaWidgetId !== null) window.hcaptcha.reset(_hcaptchaWidgetId);
          }
        })
        .catch(function () {
          err.textContent = 'Erreur reseau — reessayez';
          err.style.display = 'block';
          btn.textContent = mode === 'signup' ? "S'inscrire" : 'Se connecter';
          btn.disabled = false;
          if (window.hcaptcha && _hcaptchaWidgetId !== null) window.hcaptcha.reset(_hcaptchaWidgetId);
        });
    };

    overlay.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') $('lou-auth-btn').click();
    });

    setTimeout(function () { $('lou-auth-email').focus(); }, 100);
  }

  // ============================================================
  // LANDING PAGE — Hook CTAs (when HTML already exists, e.g. Render)
  // ============================================================
  function initLanding() {
    var isRenderHost = window.location.hostname === 'lou-platform.onrender.com' || window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';

    // Hook login button to auth modal
    ['nav-login-btn'].forEach(function (id) {
      var el = $(id);
      if (el) {
        el.addEventListener('click', function (e) {
          e.preventDefault();
          showAuthModal();
        });
      }
    });

    // Hook CTA buttons to open chat directly.
    // v6.3.1 Bug #1: the previous guard `if (panel && !panel.classList.contains('open'))`
    // silently did nothing when initChat hadn't created the panel yet (race on
    // slow mobile networks) — clicks looked dead. Now we call _openChat() which
    // initializes the widget on demand and explicitly opens the panel.
    ['hero-cta-1', 'cta-bottom', 'nav-cta-btn', 'setup-profile'].forEach(function (id) {
      var el = $(id);
      if (el) {
        el.addEventListener('click', function (e) {
          e.preventDefault();
          _openChat();
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
          if (isRenderHost) {
            window.location.href = '/dashboard';
          } else {
            showDashboard();
          }
        };
      }
    }

    // If URL carries ?login=1, open auth modal (used by /pricing, /faq "Connexion" links)
    if (window.location.search.indexOf('login=1') !== -1 && !(isJWT(TOKEN) && USER)) {
      // Clean URL so a refresh doesn't re-trigger
      try { history.replaceState(null, '', window.location.pathname); } catch(e) {}
      setTimeout(showAuthModal, 50);
    }

    // Chat bubble on landing page
    injectChatCSS();
    initChat();
  }

  // ============================================================
  // LANDING PAGE — Full render (for Webflow / external hosts)
  // ============================================================
  function showLanding() {
    document.title = 'Bon Home — Le bon home, au bon moment';

    // Inject fonts
    if (!document.querySelector('link[href*="Playfair+Display"]')) {
      var fontLink = document.createElement('link');
      fontLink.rel = 'stylesheet';
      fontLink.href = 'https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;600;700;800&family=Inter:wght@400;500;600;700&display=swap';
      document.head.appendChild(fontLink);
    }

    // Inject CSS
    var style = ce('style', '', getLandingCSS());
    document.head.appendChild(style);

    // Replace body
    document.body.innerHTML = '';
    document.body.style.margin = '0';

    // NAV
    var nav = ce('nav', 'nav');
    nav.innerHTML =
      '<a href="/" class="nav-logo">' +
        '<svg class="logo-wolf" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><rect width="48" height="48" rx="12" fill="#2A6670"/><circle cx="21" cy="20" r="11" fill="none" stroke="#fff" stroke-width="2.8"/><line x1="29" y1="28" x2="39" y2="38" stroke="#fff" stroke-width="2.8" stroke-linecap="round"/><path d="M21 13 L14 19 L15.5 19 L15.5 26 L26.5 26 L26.5 19 L28 19 Z" fill="#fff" opacity="0.95"/><rect x="19.5" y="22" width="3" height="4" rx="0.5" fill="#2A6670"/></svg>' +
        '<span class="logo-text">Bon Home</span>' +
      '</a>' +
      '<div class="nav-links">' +
        '<a href="#features">Fonctions</a>' +
        '<a href="#how">Comment ça marche</a>' +
        '<a href="#" class="btn btn-primary" id="nav-login-btn">Connexion</a>' +
      '</div>';
    document.body.appendChild(nav);

    // HERO
    var hero = ce('section', 'hero');
    hero.innerHTML =
      '<div class="hero-text">' +
        '<h1>Le bon <em>home</em>,<br>au bon moment.</h1>' +
        '<p>Bon Home scrute 10+ portails immobiliers suisses en continu. Lou, notre IA, déniche les biens qui vous correspondent et vous les présente — scores, analyses, tout est prêt.</p>' +
        '<div class="hero-ctas">' +
          '<a href="#" class="btn btn-primary" id="hero-cta-1">Parler à Lou</a>' +
          '<a href="#how" class="btn btn-outline">En savoir plus</a>' +
        '</div>' +
      '</div>' +
      '<div class="hero-visual">' +
        '<svg class="hero-wolf-icon" width="180" height="180" viewBox="0 0 80 80" xmlns="http://www.w3.org/2000/svg"><circle cx="35" cy="33" r="18" fill="none" stroke="rgba(255,255,255,0.95)" stroke-width="4.5"/><line x1="48" y1="46" x2="65" y2="63" stroke="rgba(255,255,255,0.95)" stroke-width="4.5" stroke-linecap="round"/><path d="M35 21 L24 30 L27 30 L27 41 L43 41 L43 30 L46 30 Z" fill="rgba(255,255,255,0.95)"/><rect x="33" y="35" width="5" height="6" rx="1" fill="rgba(42,102,112,0.6)"/></svg>' +
        '<div class="hero-badge">' +
          '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="opacity:.8;flex-shrink:0"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>' +
          '<div>10+ portails suisses<small>Homegate, ImmoScout24, Comparis...</small></div>' +
        '</div>' +
      '</div>';
    document.body.appendChild(hero);

    // STATS BAR
    var statsBar = ce('div', 'stats-bar');
    statsBar.innerHTML =
      '<div class="stats-bar-inner">' +
        '<div><strong>10+</strong><span>Portails suisses</span></div>' +
        '<div><strong>500+</strong><span>Annonces analysées</span></div>' +
        '<div><strong>6</strong><span>Critères de scoring</span></div>' +
        '<div><strong>24/7</strong><span>Veille automatique</span></div>' +
      '</div>';
    document.body.appendChild(statsBar);

    // FEATURES
    var features = ce('section', 'features');
    features.id = 'features';
    features.innerHTML =
      '<div class="features-header"><h2>Pourquoi Bon Home ?</h2><p>Un assistant immobilier complet, de la recherche à la prise de contact.</p></div>' +
      '<div class="features-grid">' +
        featureCard('<circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/>', 'Scraping multi-portails', 'Bon Home scrute automatiquement Homegate, ImmoScout24, Flatfox, Immobilier.ch, Comparis et bien d\'autres pour ne rien manquer.') +
        featureCard('<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>', 'Scoring intelligent', 'Chaque annonce est notée de A à D selon vos critères : zone, budget, type, surface, équipements, fraîcheur.') +
        featureCard('<path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/>', 'Chatbot IA', 'Discutez avec Lou pour définir vos critères de recherche de manière naturelle. Il comprend vos besoins et affine votre profil.') +
        featureCard('<path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0118 0z"/><circle cx="12" cy="10" r="3"/>', 'Recherche par zone', 'Définissez une ou plusieurs zones géographiques avec un rayon en km. Lou calcule la distance GPS pour chaque bien.') +
        featureCard('<path d="M18 8A6 6 0 006 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 01-3.46 0"/>', 'Alertes en temps réel', 'Soyez averti dès qu\'un nouveau bien correspondant à vos critères apparaît sur le marché.') +
        featureCard('<rect x="3" y="12" width="4" height="9" rx="1"/><rect x="10" y="7" width="4" height="14" rx="1"/><rect x="17" y="2" width="4" height="19" rx="1"/>', 'Dashboard complet', 'Visualisez tous vos résultats, filtrez par score, triez par prix ou date, et gardez vos favoris.') +
      '</div>';
    document.body.appendChild(features);

    // HOW IT WORKS
    var how = ce('section', 'how');
    how.id = 'how';
    how.innerHTML =
      '<div class="how-inner">' +
        '<h2>Comment ça marche ?</h2>' +
        '<div class="steps">' +
          '<div class="step"><div class="step-num">1</div><h3>Parlez à Lou</h3><p>Dites-lui ce que vous cherchez : région, budget, type de bien, nombre de pièces...</p></div>' +
          '<div class="step"><div class="step-num">2</div><h3>Lou chasse pour vous</h3><p>Notre moteur scrute 10+ portails immobiliers suisses en continu et collecte les nouvelles annonces.</p></div>' +
          '<div class="step"><div class="step-num">3</div><h3>Scoring & analyse</h3><p>Chaque bien est noté selon 6 critères pondérés. Seuls les meilleurs vous sont présentés.</p></div>' +
          '<div class="step"><div class="step-num">4</div><h3>Contactez & visitez</h3><p>Retrouvez les coordonnées du propriétaire, l\'annonce originale et tous les détails en un clic.</p></div>' +
        '</div>' +
      '</div>';
    document.body.appendChild(how);

    // CTA
    var ctaSection = ce('section', 'cta');
    ctaSection.innerHTML =
      '<h2>Prêt à trouver le bon home ?</h2>' +
      '<p>Rejoignez Bon Home et laissez Lou faire le travail de recherche pour vous.</p>' +
      '<a href="#" class="btn btn-primary" id="cta-bottom">Parler à Lou</a>';
    document.body.appendChild(ctaSection);

    // FOOTER
    var footer = ce('footer', 'footer');
    footer.innerHTML =
      '<div class="footer-inner">' +
        '<p>&copy; 2026 Bon Home — Le bon home, au bon moment.</p>' +
        '<div class="footer-links"><a href="#">Confidentialite</a><a href="#">Conditions</a><a href="#">Contact</a></div>' +
      '</div>';
    document.body.appendChild(footer);

    // Hook CTAs + chat
    initLanding();
  }

  function featureCard(svgInner, title, desc) {
    return '<div class="feature-card">' +
      '<div class="feature-icon"><svg viewBox="0 0 24 24" fill="none" stroke="#2A6670" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' + svgInner + '</svg></div>' +
      '<h3>' + title + '</h3>' +
      '<p>' + desc + '</p>' +
    '</div>';
  }

  // ============================================================
  // LANDING CSS (for Webflow / external hosts)
  // ============================================================
  function getLandingCSS() {
    return [
      '*,*::before,*::after{margin:0;padding:0;box-sizing:border-box}',
      ':root{--dark:#1E2A44;--blue:#2A6670;--blue-light:#0ea5e9;--gray-50:#F7F4EE;--gray-100:#EEE9DE;--gray-300:#E4DFD4;--gray-500:#7A8398;--gray-700:#4A5468;--white:#fff;--green:#059669;--radius:12px}',
      'body{font-family:"Inter",system-ui,sans-serif;color:var(--dark);background:var(--white);-webkit-font-smoothing:antialiased}',
      'h1,h2,h3{font-family:Fraunces,Georgia,serif}',

      '.nav{display:flex;justify-content:space-between;align-items:center;padding:18px 5%;max-width:1280px;margin:0 auto}',
      '.nav-logo{display:flex;align-items:center;gap:10px;text-decoration:none;color:var(--dark)}',
      '.nav-logo .logo-wolf{width:36px;height:36px;flex-shrink:0}',
      '.nav-logo .logo-text{font-family:Fraunces,Georgia,serif;font-size:22px;font-weight:800}',
      '.nav-links{display:flex;align-items:center;gap:32px}',
      '.nav-links a{text-decoration:none;color:var(--gray-700);font-size:15px;font-weight:500;transition:color .2s}',
      '.nav-links a:hover{color:var(--blue)}',
      '.btn{display:inline-block;padding:10px 24px;border-radius:10px;font-size:15px;font-weight:600;cursor:pointer;text-decoration:none;border:none;transition:all .2s}',
      '.btn-primary{background:var(--blue);color:var(--white)}',
      '.btn-primary:hover{background:var(--blue-light);transform:translateY(-1px);box-shadow:0 4px 16px rgba(42,102,112,.3)}',
      '.btn-outline{background:transparent;color:var(--blue);border:2px solid var(--blue)}',
      '.btn-outline:hover{background:var(--blue);color:var(--white)}',

      '.hero{display:flex;align-items:center;justify-content:space-between;max-width:1280px;margin:0 auto;padding:60px 5% 80px;gap:60px}',
      '.hero-text{max-width:560px}',
      '.hero-text h1{font-size:52px;line-height:1.15;margin-bottom:20px;letter-spacing:-0.5px}',
      '.hero-text h1 em{font-style:normal;color:var(--blue)}',
      '.hero-text p{font-size:18px;line-height:1.7;color:var(--gray-500);margin-bottom:32px}',
      '.hero-ctas{display:flex;gap:16px;align-items:center}',
      '.hero-visual{flex-shrink:0;width:440px;height:380px;background:linear-gradient(135deg,var(--blue) 0%,#0c4a6e 100%);border-radius:24px;display:flex;align-items:center;justify-content:center;position:relative;overflow:hidden}',
      '.hero-visual::before{content:"";position:absolute;inset:0;background:url("data:image/svg+xml,%3Csvg width=\'60\' height=\'60\' xmlns=\'http://www.w3.org/2000/svg\'%3E%3Cdefs%3E%3Cpattern id=\'g\' width=\'60\' height=\'60\' patternUnits=\'userSpaceOnUse\'%3E%3Cpath d=\'M0 30h60M30 0v60\' stroke=\'rgba(255,255,255,.06)\' stroke-width=\'1\'/%3E%3C/pattern%3E%3C/defs%3E%3Crect width=\'60\' height=\'60\' fill=\'url(%23g)\'/%3E%3C/svg%3E")}',
      '.hero-wolf-icon{z-index:1;filter:drop-shadow(0 8px 32px rgba(0,0,0,.3))}',
      '.hero-badge{position:absolute;bottom:24px;left:24px;background:rgba(255,255,255,.15);backdrop-filter:blur(8px);border-radius:12px;padding:12px 18px;color:var(--white);font-size:14px;font-weight:600;z-index:1;display:flex;align-items:center;gap:10px}',
      '.hero-badge small{display:block;font-weight:400;font-size:12px;opacity:.8;margin-top:2px}',

      '.stats-bar{background:var(--gray-50);border-top:1px solid var(--gray-100);border-bottom:1px solid var(--gray-100)}',
      '.stats-bar-inner{max-width:1280px;margin:0 auto;padding:32px 5%;display:flex;justify-content:space-around;gap:24px;text-align:center}',
      '.stats-bar-inner div strong{display:block;font-size:28px;font-family:"Playfair Display",serif;color:var(--blue)}',
      '.stats-bar-inner div span{font-size:14px;color:var(--gray-500)}',

      '.features{max-width:1280px;margin:0 auto;padding:80px 5%}',
      '.features-header{text-align:center;margin-bottom:56px}',
      '.features-header h2{font-size:36px;margin-bottom:12px}',
      '.features-header p{font-size:16px;color:var(--gray-500);max-width:560px;margin:0 auto}',
      '.features-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:32px}',
      '.feature-card{background:var(--white);border:1px solid var(--gray-100);border-radius:16px;padding:32px;transition:all .3s}',
      '.feature-card:hover{border-color:var(--blue);box-shadow:0 8px 32px rgba(42,102,112,.08);transform:translateY(-4px)}',
      '.feature-icon{width:48px;height:48px;background:rgba(42,102,112,.1);border-radius:12px;display:flex;align-items:center;justify-content:center;margin-bottom:16px}',
      '.feature-icon svg{width:24px;height:24px}',
      '.feature-card h3{font-size:20px;margin-bottom:8px;font-family:"Playfair Display",serif}',
      '.feature-card p{font-size:14px;color:var(--gray-500);line-height:1.7}',

      '.how{background:var(--dark);color:var(--white);padding:80px 5%}',
      '.how-inner{max-width:1280px;margin:0 auto}',
      '.how-inner h2{text-align:center;font-size:36px;margin-bottom:56px}',
      '.steps{display:grid;grid-template-columns:repeat(4,1fr);gap:32px}',
      '.step{text-align:center}',
      '.step-num{width:48px;height:48px;border-radius:50%;background:var(--blue);color:var(--white);display:inline-flex;align-items:center;justify-content:center;font-size:20px;font-weight:700;margin-bottom:16px}',
      '.step h3{font-size:18px;margin-bottom:8px;font-family:"Playfair Display",serif}',
      '.step p{font-size:14px;color:var(--gray-300);line-height:1.6}',

      '.cta{text-align:center;max-width:1280px;margin:0 auto;padding:80px 5%}',
      '.cta h2{font-size:36px;margin-bottom:16px}',
      '.cta p{font-size:16px;color:var(--gray-500);margin-bottom:32px;max-width:480px;margin-left:auto;margin-right:auto}',

      '.footer{background:var(--gray-50);border-top:1px solid var(--gray-100);padding:40px 5%;text-align:center}',
      '.footer-inner{max-width:1280px;margin:0 auto;display:flex;justify-content:space-between;align-items:center}',
      '.footer p{font-size:13px;color:var(--gray-500)}',
      '.footer-links{display:flex;gap:24px}',
      '.footer-links a{font-size:13px;color:var(--gray-500);text-decoration:none}',
      '.footer-links a:hover{color:var(--blue)}',

      '.lou-overlay{position:fixed;inset:0;background:rgba(15,23,42,.7);display:flex;align-items:center;justify-content:center;z-index:9999;backdrop-filter:blur(4px)}',
      '.lou-auth-box{background:#fff;border-radius:16px;padding:36px;width:400px;max-width:90vw;box-shadow:0 20px 60px rgba(0,0,0,.3);position:relative;color:#1E2A44}',
      '.lou-auth-box .close-btn{position:absolute;top:12px;right:16px;background:none;border:none;font-size:22px;cursor:pointer;color:#7A8398}',
      '.lou-auth-box .close-btn:hover{color:#1E2A44}',
      '.lou-auth-box h2{font-size:24px;margin:0 0 4px;font-family:Fraunces,Georgia,serif}',
      '.lou-auth-box .sub{font-size:14px;color:#7A8398;margin-bottom:20px}',
      '.lou-auth-box input{width:100%;padding:12px 14px;border:1px solid #E4DFD4;border-radius:10px;margin-bottom:12px;font-size:14px;box-sizing:border-box;outline:none;font-family:"Inter",sans-serif}',
      '.lou-auth-box input:focus{border-color:#2A6670;box-shadow:0 0 0 3px rgba(42,102,112,.1)}',
      '.auth-submit{width:100%;padding:13px;border:none;border-radius:10px;background:#2A6670;color:#fff;font-size:15px;font-weight:600;cursor:pointer;transition:background .2s;font-family:"Inter",sans-serif}',
      '.auth-submit:hover{background:#1A4650}',
      '.lou-auth-switch{text-align:center;margin-top:14px;font-size:13px;color:#7A8398}',
      '.lou-auth-switch a{color:#2A6670;cursor:pointer;text-decoration:underline}',
      '.lou-auth-err{color:#dc2626;font-size:13px;margin-top:8px;display:none;text-align:center}',

      '@media(max-width:900px){.hero{flex-direction:column;text-align:center;padding:40px 5% 60px}.hero-text h1{font-size:36px}.hero-ctas{justify-content:center}.hero-visual{width:100%;max-width:360px;height:280px}.features-grid{grid-template-columns:1fr}.steps{grid-template-columns:repeat(2,1fr)}.nav-links{gap:16px}.footer-inner{flex-direction:column;gap:12px}}',
      '@media(max-width:600px){.nav-links a:not(.btn){display:none}.stats-bar-inner{flex-wrap:wrap}.steps{grid-template-columns:1fr}.hero-text h1{font-size:28px}.hero-text p{font-size:14px}.hero-visual{max-width:100%}.features-header h2{font-size:28px}}'
    ].join('');
  }

  // ============================================================
  // CHAT CSS — inject standalone (for landing page)
  // ============================================================
  function injectChatCSS() {
    var s = ce('style', '', [
      '.chat-toggle{position:fixed;bottom:24px;right:24px;width:56px;height:56px;border-radius:50%;background:#2A6670;color:#fff;border:none;font-size:24px;cursor:pointer;box-shadow:0 4px 20px rgba(42,102,112,.4);z-index:1000;display:flex;align-items:center;justify-content:center;transition:transform .2s}',
      '.chat-toggle:hover{transform:scale(1.1)}',
      '.chat-panel{position:fixed;bottom:90px;right:24px;width:380px;max-width:calc(100vw - 48px);height:500px;background:#fff;border-radius:16px;box-shadow:0 8px 40px rgba(0,0,0,.15);z-index:1000;display:none;flex-direction:column;overflow:hidden}',
      '.chat-panel.open{display:flex}',
      '.chat-head{background:#1A4650;color:#F7F4EE;padding:14px 18px;font-weight:600;display:flex;justify-content:space-between;align-items:center;font-size:15px}',
      '.chat-head button{background:none;border:none;color:#fff;font-size:22px;cursor:pointer;opacity:.7}',
      '.chat-head button:hover{opacity:1}',
      '.chat-body{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px}',
      '.chat-msg{max-width:85%;padding:10px 14px;border-radius:12px;font-size:14px;line-height:1.6}',
      '.chat-msg.bot{background:#EEE9DE;color:#1E2A44;align-self:flex-start;border-bottom-left-radius:4px}',
      '.chat-msg.user{background:#2A6670;color:#fff;align-self:flex-end;border-bottom-right-radius:4px}',
      '.chat-input{display:flex;border-top:1px solid #E4DFD4;padding:12px}',
      '.chat-input input{flex:1;border:1px solid #E4DFD4;border-radius:8px;padding:10px 14px;font-size:14px;outline:none;font-family:Inter,sans-serif}',
      '.chat-input input:focus{border-color:#2A6670}',
      '.chat-input button{margin-left:8px;background:#2A6670;color:#fff;border:none;border-radius:8px;padding:10px 16px;cursor:pointer;font-size:16px;transition:background .2s}',
      '.chat-input button:hover{background:#1A4650}',
      '.chat-suggestions{display:flex;flex-wrap:wrap;gap:6px;margin-top:4px}',
      '.chat-sug{padding:6px 12px;background:#CEDFE1;border:none;border-radius:20px;font-size:12px;color:#2A6670;cursor:pointer;transition:background .2s}',
      '.chat-sug:hover{background:#CEDFE1}',
      '.chat-unresolved{display:flex;flex-direction:column;gap:4px;margin-top:4px;align-self:flex-start;max-width:85%}',
      '.chat-unresolved-label{font-size:12px;color:#7A8398;font-style:italic}',
      '@media(max-width:768px){.chat-panel{width:calc(100vw - 24px);right:12px;bottom:88px;height:50vh;max-height:380px;border-radius:16px;box-shadow:0 8px 40px rgba(0,0,0,.25)}.chat-input{padding:10px;padding-bottom:max(10px,env(safe-area-inset-bottom))}.chat-input input{font-size:16px}}',
      '@media(max-width:480px){.chat-toggle{width:48px;height:48px;bottom:16px;right:16px}.chat-panel{width:calc(100vw - 20px);right:10px;bottom:72px;height:45vh;max-height:340px;border-radius:14px}.chat-head{padding:10px 14px;font-size:13px}.chat-body{padding:10px;gap:8px}.chat-msg{font-size:13px;padding:8px 12px}.chat-input{padding:8px}.chat-input input{padding:8px 10px;font-size:16px}.chat-input button{padding:8px 12px}}'
    ].join(''));
    document.head.appendChild(s);
  }

  // ============================================================
  // DASHBOARD
  // ============================================================
  function showAdminPanel() {
    var overlay = ce('div', 'lou-overlay');
    overlay.innerHTML =
      '<div class="admin-panel">' +
        '<div class="admin-header">' +
          '<h2>Administration</h2>' +
          '<button class="close-btn" id="admin-close">&times;</button>' +
        '</div>' +
        '<div class="admin-body" id="admin-body"><p style="color:#7A8398">Chargement...</p></div>' +
      '</div>';
    document.body.appendChild(overlay);
    overlay.addEventListener('click', function(e){ if (e.target === overlay) overlay.remove(); });
    document.getElementById('admin-close').onclick = function(){ overlay.remove(); };

    fetch(API + '/api/admin/users', { headers: { 'Authorization': 'Bearer ' + TOKEN } })
      .then(function(r){ return r.json(); })
      .then(function(data){
        if (data.error) { document.getElementById('admin-body').innerHTML = '<p style="color:#dc2626">' + escapeHtml(data.error) + '</p>'; return; }
        var users = data.users || [];
        var html = '<div class="admin-stats"><strong>' + users.length + '</strong> utilisateur' + (users.length > 1 ? 's' : '') + ' inscrits</div>';
        html += '<table class="admin-table"><thead><tr><th>Nom</th><th>Email</th><th>Inscription</th><th>Dernière connexion</th><th>Plan</th><th>Profils</th><th>Favoris</th><th>Statut</th></tr></thead><tbody>';
        users.forEach(function(u){
          var created = u.created_at ? new Date(u.created_at).toLocaleDateString('fr-CH') : '-';
          var lastLogin = u.last_login ? new Date(u.last_login).toLocaleDateString('fr-CH') : 'Jamais';
          html += '<tr>' +
            '<td>' + escapeHtml(u.name || '-') + '</td>' +
            '<td>' + escapeHtml(u.email) + '</td>' +
            '<td>' + created + '</td>' +
            '<td>' + lastLogin + '</td>' +
            '<td><span class="admin-plan">' + escapeHtml(u.plan) + '</span></td>' +
            '<td>' + u.profiles_count + '</td>' +
            '<td>' + u.favorites_count + '</td>' +
            '<td>' + (u.is_active ? '<span style="color:#059669">Actif</span>' : '<span style="color:#dc2626">Inactif</span>') + '</td>' +
          '</tr>';
        });
        html += '</tbody></table>';
        document.getElementById('admin-body').innerHTML = html;
      })
      .catch(function(){ document.getElementById('admin-body').innerHTML = '<p style="color:#dc2626">Erreur de chargement</p>'; });
  }

  function showDashboard() {
    if (!isJWT(TOKEN) || !USER) {
      // Not authenticated — redirect to landing with auth modal trigger
      // (don't reload current URL or we'd loop on /dashboard)
      window.location.replace('/?login=1');
      return;
    }

    document.title = 'Dashboard — bonhome.';

    // Inject CSS
    var style = ce('style', '', getDashCSS());
    document.head.appendChild(style);

    document.body.innerHTML = '';

    // NAV
    var nav = ce('div', 'dash-nav');
    nav.innerHTML =
      '<a href="/" class="dash-nav-brand"><span class="wordmark wordmark--sm">bonh<span class="wordmark__chevron-wrap"><svg class="wordmark__chevron" viewBox="0 0 20 12" xmlns="http://www.w3.org/2000/svg"><path d="M2 10 L10 3 L18 10"/></svg>o</span>me<span class="wordmark__point">.</span></span></a>' +
      '<div class="dash-nav-right">' +
        '<button class="dash-admin-btn" id="admin-btn" style="display:none">Admin</button>' +
        '<span class="dash-user-email">' + escapeHtml(USER.email || '') + '</span>' +
        '<button class="dash-logout-btn" id="logout-btn">Déconnexion</button>' +
      '</div>';
    document.body.appendChild(nav);

    // Check if user is admin and show button — 401/403 is expected for non-admins,
    // so we stay silent here (no logging).
    fetch(API + '/api/admin/check', { headers: { 'Authorization': 'Bearer ' + TOKEN } })
      .then(function(r){ return r.json(); })
      .then(function(d){
        if (d.is_admin) $('admin-btn').style.display = 'inline-block';
      }).catch(function(){});

    // MAIN WRAP
    var wrap = ce('div', 'dash-wrap');
    wrap.id = 'dash-wrap';
    wrap.innerHTML =
      '<div class="dash-header">' +
        '<h1>Mon Dashboard</h1>' +
        '<div class="dash-actions">' +
          '<button id="refresh-btn" class="dash-refresh-btn" title="Actualiser les résultats">&#8635; Actualiser</button>' +
          '<select id="sort-select" class="dash-select">' +
            '<option value="score">Meilleur score</option>' +
            '<option value="price_asc">Prix croissant</option>' +
            '<option value="price_desc">Prix décroissant</option>' +
            '<option value="newest">Plus récents</option>' +
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
        '<div class="dash-stat"><div class="dash-stat-num" id="stat-total">-</div><div class="dash-stat-lbl">Biens analysés</div></div>' +
        '<div class="dash-stat clickable" id="stat-new-card" style="cursor:pointer"><div class="dash-stat-num" id="stat-new">-</div><div class="dash-stat-lbl">Nouveaux (24h)</div></div>' +
        '<div class="dash-stat clickable" id="stat-fav-card" style="cursor:pointer"><div class="dash-stat-num" id="stat-favs">-</div><div class="dash-stat-lbl">Favoris</div></div>' +
        '<div class="dash-stat"><div class="dash-stat-num" id="stat-grade-a">-</div><div class="dash-stat-lbl">Classe A</div></div>' +
      '</div>' +
      // Score legend
      '<div class="score-legend" id="score-legend">' +
        '<button class="score-legend-toggle" id="score-legend-btn">&#9432; Comment fonctionne le score ?</button>' +
        '<div class="score-legend-body" id="score-legend-body" style="display:none">' +
          '<p class="score-legend-intro">Chaque bien est noté de 0 à 100 selon vos critères :</p>' +
          '<div class="score-legend-grades">' +
            '<span class="score-legend-badge" style="background:#059669">A <small>85-100</small></span>' +
            '<span class="score-legend-badge" style="background:#2A6670">B <small>70-84</small></span>' +
            '<span class="score-legend-badge" style="background:#d97706">C <small>55-69</small></span>' +
            '<span class="score-legend-badge" style="background:#dc2626">D <small>0-54</small></span>' +
          '</div>' +
          '<div class="score-legend-criteria">' +
            '<div class="score-legend-item"><strong>Zone</strong> — Proximité de vos villes cibles (distance réelle en km)</div>' +
            '<div class="score-legend-item"><strong>Budget</strong> — Adéquation entre le prix du bien et votre budget max</div>' +
            '<div class="score-legend-item"><strong>Type</strong> — Correspondance au type de bien souhaité (appartement, maison…)</div>' +
            '<div class="score-legend-item"><strong>Surface</strong> — Surface et nombre de pièces par rapport à votre minimum</div>' +
            '<div class="score-legend-item"><strong>Équipements</strong> — Parking, balcon, ascenseur, etc. détectés dans l\'annonce</div>' +
            '<div class="score-legend-item"><strong>Fraîcheur</strong> — Date de publication récente de l\'annonce</div>' +
          '</div>' +
          '<p class="score-legend-tip">Cliquez sur le badge de score d\'une annonce pour voir le détail.</p>' +
        '</div>' +
      '</div>' +
      // View tabs
      '<div class="dash-tabs" id="dash-tabs">' +
        '<button class="dash-tab active" data-view="properties">Tous les biens</button>' +
        '<button class="dash-tab" data-view="favorites">&#9829; Mes favoris</button>' +
        '<button class="dash-tab" data-view="map">&#128506; Carte</button>' +
      '</div>' +
      // Profile summary
      '<div id="profile-bar" class="dash-profile-bar"></div>' +
      // Favorites toolbar (hidden by default)
      '<div id="fav-toolbar" class="fav-toolbar" style="display:none">' +
        '<div class="fav-toolbar-left">' +
          '<select id="fav-sort" class="dash-select">' +
            '<option value="date">Plus récents</option>' +
            '<option value="score">Meilleur score</option>' +
            '<option value="price_asc">Prix croissant</option>' +
            '<option value="price_desc">Prix décroissant</option>' +
          '</select>' +
          '<button id="fav-compare-btn" class="fav-action-btn">&#9878; Comparer</button>' +
        '</div>' +
        '<div class="fav-toolbar-right">' +
          '<button id="fav-export-btn" class="fav-action-btn">&#8681; Exporter CSV</button>' +
        '</div>' +
      '</div>' +
      // Properties list
      '<div id="properties-list" class="dash-properties"><div class="dash-loading">Chargement des biens...</div></div>' +
      // Favorites list (hidden by default)
      '<div id="favorites-list" class="dash-properties" style="display:none"></div>' +
      // Map view (hidden by default)
      '<div id="map-view" class="map-view" style="display:none"></div>' +
      // Compare panel (hidden by default)
      '<div id="compare-panel" class="compare-panel" style="display:none"></div>' +
      // Pagination
      '<div id="pagination" class="dash-pagination"></div>';

    document.body.appendChild(wrap);

    // Logout
    // v6.3.2 Bug #4: clear ALL lou_* keys, not just token/user. Otherwise
    // lou_first_login / lou_anon_criteria / lou_anon_session leak across
    // accounts when users switch on the same browser (e.g. a friend test
    // session reusing a demo laptop).
    $('logout-btn').onclick = function () {
      try {
        var toRemove = [];
        for (var i = 0; i < localStorage.length; i++) {
          var k = localStorage.key(i);
          if (k && k.indexOf('lou_') === 0) toRemove.push(k);
        }
        toRemove.forEach(function (k) { localStorage.removeItem(k); });
      } catch (_) {
        // Fallback: explicit list if localStorage iteration fails.
        ['lou_token', 'lou_user', 'lou_first_login',
         'lou_anon_criteria', 'lou_anon_session'].forEach(function (k) {
          try { localStorage.removeItem(k); } catch (_) {}
        });
      }
      window.location.reload();
    };

    // Admin panel
    $('admin-btn').onclick = function () { showAdminPanel(); };

    // Load data. loadProfileBar AND loadStats are awaited so loadProperties
    // sees _hasProfile and _lastStats.last_scored_at already set — otherwise a
    // new signup would flash the wrong empty-state placeholder (Bug #2).
    var statsReady = loadStats();
    var profileReady = loadProfileBar();
    var readinessGate = Promise.all([profileReady, statsReady]).catch(function () {});
    // If first login, trigger scoring first (properties may already exist in DB from other users' scrapes)
    if (_isFirstLoginActive()) {
      readinessGate.then(function () {
        return apiFetch(API + '/api/score', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      })
        .then(function () { loadStats(); loadProperties(1, 'score', 0); })
        .catch(function () { loadProperties(1, 'score', 0); });
    } else {
      readinessGate.then(function () { loadProperties(1, 'score', 0); })
        .catch(function () { loadProperties(1, 'score', 0); });
    }

    // Refresh button — reload data from database without scraping
    $('refresh-btn').onclick = function () {
      var btn = this;
      btn.disabled = true;
      btn.textContent = '⟳ Chargement...';
      loadStats();
      loadProfileBar();
      loadProperties(1, 'score', 0);
      btn.textContent = '↻ Actualiser';
      btn.disabled = false;
    };

    // Sort/filter change — re-sort active view (properties list OR map sidebar)
    $('sort-select').onchange = function () {
      currentNewOnly = false;
      document.querySelectorAll('.dash-stat').forEach(function(s) { s.classList.remove('stat-active'); });
      if (currentView === 'map') {
        _mapCurrentSort = this.value;
        _mapVisibleCount = _mapPageSize; // Reset pagination
        _refreshMapSidebar();
      } else {
        loadProperties(1, this.value, parseInt($('grade-filter').value), false);
      }
    };
    $('grade-filter').onchange = function () {
      currentNewOnly = false;
      document.querySelectorAll('.dash-stat').forEach(function(s) { s.classList.remove('stat-active'); });
      loadProperties(1, $('sort-select').value, parseInt(this.value), false);
    };

    // View tabs
    var currentView = 'properties';
    document.querySelectorAll('.dash-tab').forEach(function (tab) {
      tab.onclick = function () {
        currentNewOnly = false;
        document.querySelectorAll('.dash-stat').forEach(function(s) { s.classList.remove('stat-active'); });
        switchView(this.dataset.view);
        if (this.dataset.view === 'properties') {
          loadProperties(1, 'score', 0, false);
        }
      };
    });

    // Score legend toggle
    $('score-legend-btn').onclick = function () {
      var body = $('score-legend-body');
      var open = body.style.display !== 'none';
      body.style.display = open ? 'none' : 'block';
      this.innerHTML = open ? '&#9432; Comment fonctionne le score ?' : '&#9432; Masquer l\'explication';
    };

    // Click on Nouveaux stat card — show only new listings (last 24h)
    $('stat-new-card').onclick = function () {
      var isActive = this.classList.contains('stat-active');
      // Remove active from all stat cards
      document.querySelectorAll('.dash-stat').forEach(function(s) { s.classList.remove('stat-active'); });
      if (isActive) {
        // Deactivate: show all properties again
        currentNewOnly = false;
        switchView('properties');
        loadProperties(1, 'score', 0, false);
      } else {
        this.classList.add('stat-active');
        currentNewOnly = true;
        switchView('properties');
        loadProperties(1, 'newest', 0, true);
      }
    };

    // Click on Favoris stat card
    $('stat-fav-card').onclick = function () {
      document.querySelectorAll('.dash-stat').forEach(function(s) { s.classList.remove('stat-active'); });
      currentNewOnly = false;
      switchView('favorites');
    };

    function switchView(view) {
      currentView = view;
      document.querySelectorAll('.dash-tab').forEach(function (t) {
        t.classList.toggle('active', t.dataset.view === view);
      });
      var isFav = view === 'favorites';
      var isMap = view === 'map';
      $('properties-list').style.display = (isFav || isMap) ? 'none' : '';
      $('favorites-list').style.display = isFav ? '' : 'none';
      $('map-view').style.display = isMap ? '' : 'none';
      $('pagination').style.display = (isFav || isMap) ? 'none' : '';
      $('fav-toolbar').style.display = isFav ? '' : 'none';
      // Keep sort/filter visible on all views (rewire them for each view via onchange handlers below)
      $('sort-select').style.display = isFav ? 'none' : '';
      $('grade-filter').style.display = (isFav || isMap) ? 'none' : '';
      $('compare-panel').style.display = 'none';
      if (isFav) {
        loadFavorites();
      }
      if (isMap) {
        _mapAllProps = null; // Force reload so the map reflects current filters
        loadMapView();
      }
    }

    // ---- Map View ----
    var _mapInstance = null;
    var _mapMarkers = null;
    var _leafletLoaded = false;

    var SWISS_CITY_COORDS = {
      'lausanne': [46.5197, 6.6323],
      'geneve': [46.2044, 6.1432],
      'geneva': [46.2044, 6.1432],
      'berne': [46.9480, 7.4474],
      'bern': [46.9480, 7.4474],
      'zurich': [47.3769, 8.5417],
      'zuerich': [47.3769, 8.5417],
      'neuchatel': [46.9900, 6.9293],
      'fribourg': [46.8065, 7.1620],
      'bienne': [47.1368, 7.2467],
      'biel': [47.1368, 7.2467],
      'la chaux-de-fonds': [47.0997, 6.8261],
      'sion': [46.2333, 7.3607],
      'montreux': [46.4312, 6.9107],
      'nyon': [46.3833, 6.2348],
      'yverdon': [46.7785, 6.6413],
      'yverdon-les-bains': [46.7785, 6.6413],
      'morges': [46.5110, 6.4985],
      'vevey': [46.4628, 6.8431],
      'delemont': [47.3656, 7.3430],
      'bulle': [46.6197, 7.0569],
      'thun': [46.7580, 7.6280],
      'basel': [47.5596, 7.5886],
      'bale': [47.5596, 7.5886],
      'luzern': [47.0502, 8.3093],
      'lucerne': [47.0502, 8.3093],
      'lugano': [46.0037, 8.9511],
      'winterthur': [47.5001, 8.7240],
      'st. gallen': [47.4245, 9.3767],
      'saint-gall': [47.4245, 9.3767],
      'aarau': [47.3925, 8.0442],
      'sierre': [46.2919, 7.5350],
      'martigny': [46.0986, 7.0722],
      'renens': [46.5400, 6.5882],
      'pully': [46.5100, 6.6615],
      'ecublens': [46.5295, 6.5603],
      'prilly': [46.5369, 6.5972],
      'monthey': [46.2547, 6.9553],
      'aigle': [46.3180, 6.9706],
      'payerne': [46.8216, 6.9393],
      'cortaillod': [46.9445, 6.8451],
      'boudry': [46.9517, 6.8383],
      'colombier': [46.9607, 6.8575],
      'peseux': [46.9780, 6.8920],
      'corcelles-cormondrèche': [47.0010, 6.8840],
      'corcelles': [47.0010, 6.8840],
      'hauterive': [46.9900, 6.9600],
      'saint-blaise': [47.0140, 6.9880],
      'bevaix': [46.9292, 6.8161],
      'gorgier': [46.9125, 6.7853],
      'le landeron': [47.0533, 7.0711],
      'marin-epagnier': [47.0083, 7.0017],
      'val-de-ruz': [47.0300, 6.8900],
      'cernier': [47.0556, 6.8972],
      'fontainemelon': [47.0450, 6.8989],
      'le locle': [47.0594, 6.7489],
      'fleurier': [46.9014, 6.5817],
      'couvet': [46.9217, 6.6317],
      'travers': [46.9400, 6.6833],
      'cressier': [47.0503, 7.0372],
      'moutier': [47.2789, 7.3722],
      'bettlach': [47.2067, 7.4317],
      'grenchen': [47.1917, 7.3953],
      'solothurn': [47.2089, 7.5372],
      'olten': [47.3528, 7.9069],
      'langenthal': [47.2139, 7.7897],
      'brig': [46.3147, 7.9878],
      'visp': [46.2933, 7.8825],
      'fully': [46.1317, 7.1142],
      'saxon': [46.1483, 7.1811],
      'savigny': [46.5342, 6.7312],
      'lutry': [46.5039, 6.6855],
      'cully': [46.4885, 6.7297]
    };

    function geocodeCity(city) {
      if (!city) return null;
      var key = city.toLowerCase().trim().normalize('NFD').replace(/[\u0300-\u036f]/g, '');
      // Try exact match first
      if (SWISS_CITY_COORDS[key]) return SWISS_CITY_COORDS[key];
      // Try partial match
      for (var k in SWISS_CITY_COORDS) {
        if (key.indexOf(k) !== -1 || k.indexOf(key) !== -1) return SWISS_CITY_COORDS[k];
      }
      return null;
    }

    function loadLeaflet(cb) {
      if (_leafletLoaded && window.L) { cb(); return; }
      var link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css';
      document.head.appendChild(link);

      var mcLink = document.createElement('link');
      mcLink.rel = 'stylesheet';
      mcLink.href = 'https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css';
      document.head.appendChild(mcLink);

      var mcDefLink = document.createElement('link');
      mcDefLink.rel = 'stylesheet';
      mcDefLink.href = 'https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css';
      document.head.appendChild(mcDefLink);

      var script = document.createElement('script');
      script.src = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js';
      script.onload = function () {
        var mcScript = document.createElement('script');
        mcScript.src = 'https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js';
        mcScript.onload = function () {
          _leafletLoaded = true;
          cb();
        };
        document.head.appendChild(mcScript);
      };
      document.head.appendChild(script);
    }

    var _mapHighlightedCard = null;

    // _mapAllProps is hoisted to the IIFE top-level scope (see top of file)
    // so saveProfileForm() can invalidate it from outside showDashboard().
    var _mapCurrentSort = 'score'; // Current sort for map sidebar
    var _mapPageSize = 50; // Progressive loading size
    var _mapVisibleCount = 50; // Number of cards currently visible

    function loadMapView() {
      var container = $('map-view');
      if (!container) return;
      container.style.display = '';

      // Load ALL properties for the map (not just 1 page)
      _loadAllMapProperties(function() {
        if (_mapInstance) {
          _refreshMapMarkers();
          _refreshMapSidebar();
          _mapInstance.invalidateSize();
          return;
        }

        container.innerHTML =
          '<div class="map-split">' +
            '<div class="map-sidebar" id="map-sidebar"><div style="padding:20px;color:#7A8398;text-align:center">Chargement...</div></div>' +
            '<div class="map-canvas" id="map-canvas"></div>' +
          '</div>';

        loadLeaflet(function () {
          var mapDiv = $('map-canvas');
          _mapInstance = L.map(mapDiv, { zoomControl: false }).setView([46.8, 8.2], 8);
          L.control.zoom({ position: 'topright' }).addTo(_mapInstance);
          L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png', {
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>',
            maxZoom: 19
          }).addTo(_mapInstance);

          _refreshMapMarkers();
          _refreshMapSidebar();
          setTimeout(function () { _mapInstance.invalidateSize(); }, 200);
        });
      });
    }

    function _loadAllMapProperties(cb) {
      // Always reload: apply the same filters the main list uses so
      // the map doesn't show properties outside the zone / score_zone < 80.
      // Map always uses strict zone filter (no include_nearby) so only
      // properties within the user's configured radius appear on the map.
      var mapUrl = API + '/api/properties?page=1&per_page=500&view=map' +
        '&sort=' + (currentSort || 'score') +
        '&min_score=' + (currentMinScore || 0);
      apiFetch(mapUrl)
      .then(function(r) { return r.json(); })
      .then(function(data) {
        _mapAllProps = {};
        (data.properties || []).forEach(function(p) {
          _mapAllProps[p.id] = p;
          window._propData = window._propData || {};
          window._propData[p.id] = p;
        });
        cb();
      })
      .catch(function() { _mapAllProps = {}; cb(); });
    }

    function _refreshMapSidebar() {
      var sidebar = $('map-sidebar');
      if (!sidebar) return;
      var data = _mapAllProps || {};
      var props = Object.keys(data).map(function(id) { return data[id]; })
        .filter(function(p) {
          // Safety net: only show zone-matching properties on the map
          return !p.score_detail || !p.score_detail.zone || p.score_detail.zone >= 80;
        });

      // Sort according to current sort choice (sync with main sort dropdown if available)
      var sortSel = $('sort-select');
      if (sortSel && sortSel.value) _mapCurrentSort = sortSel.value;
      var sortKey = _mapCurrentSort || 'score';
      props.sort(function(a, b) {
        if (sortKey === 'price_asc') return (a.price || Infinity) - (b.price || Infinity);
        if (sortKey === 'price_desc') return (b.price || 0) - (a.price || 0);
        if (sortKey === 'newest') return (new Date(b.published_at || b.scraped_at || 0)) - (new Date(a.published_at || a.scraped_at || 0));
        if (sortKey === 'surface') return (b.surface || 0) - (a.surface || 0);
        return (b.score || 0) - (a.score || 0); // default: score
      });

      if (props.length === 0) {
        sidebar.innerHTML = '<div style="padding:20px;color:#7A8398;text-align:center">Aucun bien à afficher</div>';
        return;
      }

      // Progressive loading: show first N cards, with a "Voir plus" button
      var visible = Math.min(_mapVisibleCount, props.length);
      var shown = props.slice(0, visible);

      var html = '<div class="map-sidebar-header"><span>' + visible + ' / ' + props.length + ' biens</span></div>';
      html += '<div class="map-sidebar-list" id="map-sidebar-list">';
      shown.forEach(function(p) {
        var gradeColors = { A: '#059669', B: '#2A6670', C: '#d97706', D: '#dc2626' };
        var gc = gradeColors[p.grade] || '#7A8398';
        var priceStr = p.price ? formatPrice(p.price) + ' CHF' : 'Prix sur demande';
        var img = (p.images && p.images.length > 0) ? p.images[0] : '';
        var details = [];
        if (p.rooms && p.rooms > 0) details.push(p.rooms + ' pcs');
        if (p.surface && p.surface > 0) details.push(p.surface + ' m²');

        var cardTitle = cleanTitle(p.title, p);
        // For map card: show city as main title, cleaned title or fallback as description
        var mapMainTitle = cleanCity(p.city) || 'Bien';
        var mapDesc = cardTitle || _fallbackTitle(p);
        // Clean address (removes street, keeps only city)
        var cleanAddr = cleanAddress(p.address, p.city);

        html += '<div class="map-card" data-id="' + p.id + '" onclick="openPropertyDetail(' + p.id + ')">' +
          (img ? '<img class="map-card-img" src="' + escapeHtml(img) + '" onerror="this.style.display=\'none\'">' : '<div class="map-card-img-ph"></div>') +
          '<div class="map-card-info">' +
            '<div class="map-card-price">' + priceStr + '</div>' +
            '<div class="map-card-title">' + escapeHtml(mapMainTitle) + '</div>' +
            '<div class="map-card-details">' + details.join(' · ') + '</div>' +
            (mapDesc ? '<div class="map-card-addr">' + escapeHtml(mapDesc) + '</div>' : (cleanAddr ? '<div class="map-card-addr">' + escapeHtml(cleanAddr) + '</div>' : '')) +
            '<div style="display:flex;gap:3px;flex-wrap:wrap;margin-top:2px">' + (function() { var ss = p.all_sources || [{source: p.source||'', url: p.source_url||''}]; return ss.map(function(s) { var n = (s.source||'').split('.')[0]||'Source'; return '<span class="prop-source-link" data-src="' + escapeHtml(s.source||'') + '" style="font-size:9px;padding:1px 5px">' + escapeHtml(n) + '</span>'; }).join(''); })() + '</div>' +
          '</div>' +
          '<div class="map-card-score" style="background:' + gc + '">' + (p.score || 0) + '</div>' +
        '</div>';
      });
      html += '</div>';

      // "Load more" button when more items available
      if (visible < props.length) {
        var remaining = props.length - visible;
        html += '<button id="map-load-more" class="map-load-more-btn">Voir ' + Math.min(_mapPageSize, remaining) + ' biens de plus (' + remaining + ' restants)</button>';
      }

      sidebar.innerHTML = html;

      // Wire up "load more"
      var loadMoreBtn = $('map-load-more');
      if (loadMoreBtn) {
        loadMoreBtn.onclick = function() {
          _mapVisibleCount += _mapPageSize;
          _refreshMapSidebar();
        };
      }

      // Hover card -> highlight marker
      sidebar.querySelectorAll('.map-card').forEach(function(card) {
        card.addEventListener('mouseenter', function() {
          var id = parseInt(this.dataset.id);
          _highlightMapMarker(id);
        });
      });
    }

    function _highlightMapMarker(propId) {
      // Open popup of the matching marker
      if (!_mapMarkers) return;
      _mapMarkers.eachLayer(function(layer) {
        if (layer._propId === propId) {
          _mapMarkers.zoomToShowLayer(layer, function() {
            layer.openPopup();
          });
        }
      });
    }

    function _refreshMapMarkers() {
      if (!_mapInstance || !window.L) return;

      if (_mapMarkers) {
        _mapInstance.removeLayer(_mapMarkers);
      }
      _mapMarkers = L.markerClusterGroup({ maxClusterRadius: 40 });

      var gradeColors = { A: '#059669', B: '#2A6670', C: '#d97706', D: '#dc2626' };
      var data = _mapAllProps || {};
      var bounds = [];

      Object.keys(data).forEach(function (id) {
        var p = data[id];
        // Safety net: skip out-of-zone properties
        if (p.score_detail && p.score_detail.zone && p.score_detail.zone < 80) return;

        var lat = p.latitude;
        var lng = p.longitude;

        if (!lat || !lng) {
          // Try geocoding by city name, use deterministic offset based on property ID
          // so the same property always appears at the same spot on the map
          var cityCoords = geocodeCity(p.city || '');
          if (!cityCoords) cityCoords = geocodeCity(p.address || '');
          if (cityCoords) {
            // Deterministic pseudo-random offset based on property ID (stable across reloads)
            var seed = parseInt(String(p.id || 0).replace(/\D/g, '') || '0', 10);
            var offsetLat = ((seed * 2654435761 >>> 0) % 1000 - 500) / 500 * 0.0015;
            var offsetLng = ((seed * 2246822519 >>> 0) % 1000 - 500) / 500 * 0.0015;
            lat = cityCoords[0] + offsetLat;
            lng = cityCoords[1] + offsetLng;
          }
        }
        if (!lat || !lng) return;

        var color = gradeColors[p.grade] || '#7A8398';
        var priceStr = p.price ? formatPrice(p.price) : '?';

        // Price label marker instead of grade circle
        var icon = L.divIcon({
          className: 'map-marker-custom',
          html: '<div class="map-price-marker" style="background:' + color + '">' + priceStr + '</div>',
          iconSize: [80, 28],
          iconAnchor: [40, 14],
          popupAnchor: [0, -16]
        });

        var popup = '<div style="min-width:220px;font-family:Inter,sans-serif">' +
          (p.images && p.images[0] ? '<img src="' + escapeHtml(p.images[0]) + '" style="width:100%;height:120px;object-fit:cover;border-radius:8px;margin-bottom:8px" onerror="this.style.display=\'none\'">' : '') +
          '<div style="font-weight:700;font-size:14px;margin-bottom:4px">' + escapeHtml(cleanTitle(p.title, p) || _fallbackTitle(p)) + '</div>' +
          '<div style="font-size:16px;font-weight:800;color:#1E2A44;margin-bottom:4px">' + priceStr + ' CHF</div>' +
          '<div style="font-size:13px;color:#7A8398;margin-bottom:6px">' + escapeHtml(cleanAddress(p.address, p.city)) + '</div>' +
          '<div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">' +
            '<span style="background:' + color + ';color:#fff;padding:2px 8px;border-radius:6px;font-size:12px;font-weight:700">' + (p.grade || '?') + ' ' + (p.score || 0) + '/100</span>' +
            (p.rooms ? '<span style="font-size:12px;color:#7A8398">' + p.rooms + ' pcs</span>' : '') +
            (p.surface ? '<span style="font-size:12px;color:#7A8398">' + p.surface + ' m²</span>' : '') +
          '</div>' +
          '<div style="display:flex;gap:4px;flex-wrap:wrap;margin-bottom:6px">' + (function() { var ss = p.all_sources || [{source: p.source||'', url: p.source_url||''}]; return ss.map(function(s) { var n = (s.source||'').split('.')[0]||'Source'; return '<span class="prop-source-link" data-src="' + escapeHtml(s.source||'') + '" style="font-size:10px;padding:2px 6px">' + escapeHtml(n) + '</span>'; }).join(''); })() + '</div>' +
          '<a href="#" onclick="openPropertyDetail(' + p.id + ');return false;" style="color:#2A6670;font-size:13px;font-weight:600;text-decoration:none">Voir le détail →</a>' +
        '</div>';

        var marker = L.marker([lat, lng], { icon: icon }).bindPopup(popup, { maxWidth: 280 });
        marker._propId = p.id;

        // Click marker -> scroll sidebar card into view
        marker.on('click', function() {
          var card = document.querySelector('.map-card[data-id="' + p.id + '"]');
          if (card) {
            // Remove previous highlight
            if (_mapHighlightedCard) _mapHighlightedCard.classList.remove('map-card-active');
            card.classList.add('map-card-active');
            _mapHighlightedCard = card;
            card.scrollIntoView({ behavior: 'smooth', block: 'center' });
          }
        });

        _mapMarkers.addLayer(marker);
        bounds.push([lat, lng]);
      });

      _mapInstance.addLayer(_mapMarkers);
      if (bounds.length > 0) {
        _mapInstance.fitBounds(bounds, { padding: [40, 40], maxZoom: 13 });
      }
    }

    // Favorites toolbar events
    $('fav-sort').onchange = function () { loadFavorites(); };
    $('fav-export-btn').onclick = function () {
      window.open(API + '/api/favorites/export?token=' + TOKEN, '_blank');
    };

    compareMode = false;
    compareSet = {};
    $('fav-compare-btn').onclick = function () {
      compareMode = !compareMode;
      this.classList.toggle('active', compareMode);
      this.textContent = compareMode ? '\u2715 Annuler' : '\u2696 Comparer';
      compareSet = {};
      $('compare-panel').style.display = 'none';
      // Re-render to show checkboxes
      if (document.querySelectorAll('.fav-card').length > 0) {
        document.querySelectorAll('.fav-compare-check').forEach(function (cb) {
          cb.style.display = compareMode ? '' : 'none';
          cb.checked = false;
        });
      }
    };

    // Init chat widget
    initChat();
  }

  // ============================================================
  // LOAD STATS
  // ============================================================
  // v6.3.1 Bug #2: cached stats (incl. last_scored_at) so loadProperties can
  // distinguish "scoring in progress" vs "scoring done, 0 match".
  var _lastStats = null;

  function loadStats() {
    return apiFetch(API + '/api/stats')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        _lastStats = data;
        $('stat-total').textContent = data.total || 0;
        $('stat-new').textContent = data.new_count || 0;
        $('stat-favs').textContent = data.favorites || 0;
      })
      .catch(function () {
        $('stat-total').textContent = '?';
      });

    // Count grade A properties
    apiFetch(API + '/api/properties?min_score=85&per_page=1')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        $('stat-grade-a').textContent = data.total || 0;
      })
      .catch(_logErr('grade-A count'));
  }

  // ============================================================
  // LOAD PROFILE BAR
  // ============================================================
  var _currentProfile = null; // cached profile for edit form
  // v6.3.1 Bug #2: shared flag so loadProperties() knows whether to show the
  // "Lou est en chasse" first-login placeholder (only valid if a profile
  // actually exists). Previously, users with no profile saw the misleading
  // "1-3 min" message even though nothing was being scored.
  var _hasProfile = false;

  function loadProfileBar() {
    // Returns a promise so callers can sequence loadProperties after the
    // profile state is known (v6.3.1 Bug #2: avoid race where loadProperties
    // runs before _hasProfile is set).
    return apiFetch(API + '/api/profile')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.profile) {
          _hasProfile = false;
          $('profile-bar').innerHTML = '<div class="dash-profile-empty">Aucun profil de recherche. <a href="#" id="setup-profile">Parlez à Lou</a> pour configurer vos critères.</div>';
          var link = $('setup-profile');
          if (link) link.onclick = function (e) {
            e.preventDefault();
            _openChat();
          };
          return;
        }
        _hasProfile = true;
        _currentProfile = data.profile;
        var p = data.profile;
        var tags = [];
        if (p.transaction) tags.push(p.transaction === 'location' ? 'Location' : 'Achat');
        if (p.property_types && p.property_types.length) tags.push(p.property_types.join(', '));
        if (p.budget_max) tags.push('Max ' + formatPrice(p.budget_max) + ' CHF');
        if (p.rooms_min) tags.push(p.rooms_min + '+ pièces');
        if (p.surface_min) tags.push(p.surface_min + '+ m²');

        var zones = (p.zones || []).filter(function (z) { return z && z.city; });

        var priorities = p.priorities || [];

        // Build chips — zones and priorities get a "×" close button
        var tagHtml = tags.map(function (t) { return '<span class="ptag">' + escapeHtml(t) + '</span>'; }).join('');
        var zoneHtml = zones.map(function (z) {
          var label = z.city + (z.radius_km == 0 ? ' (commune exacte)' : z.radius_km ? ' (' + z.radius_km + ' km)' : '');
          return '<span class="ptag ptag-removable" data-kind="zone" data-value="' + escapeHtml(z.city) + '">' +
            escapeHtml(label) + '<button class="ptag-x" title="Retirer">×</button></span>';
        }).join('');
        var prioHtml = priorities.map(function (t) {
          return '<span class="ptag blue ptag-removable" data-kind="priority" data-value="' + escapeHtml(t) + '">' +
            escapeHtml(t) + '<button class="ptag-x" title="Retirer">×</button></span>';
        }).join('');

        $('profile-bar').innerHTML =
          '<div class="dash-profile-row">' +
            '<div class="dash-profile-tags">' + tagHtml + zoneHtml + prioHtml + '</div>' +
            '<button class="dash-edit-btn" id="edit-profile-btn">Modifier</button>' +
          '</div>' +
          '<div id="profile-edit-form" style="display:none"></div>';

        $('edit-profile-btn').onclick = function () { toggleProfileForm(); };

        // Wire up "×" buttons on removable chips
        $('profile-bar').querySelectorAll('.ptag-removable .ptag-x').forEach(function (btn) {
          btn.onclick = function (e) {
            e.stopPropagation();
            var chip = this.parentNode;
            _quickRemoveProfileItem(chip.dataset.kind, chip.dataset.value);
            chip.style.opacity = '0.5';
            chip.style.pointerEvents = 'none';
          };
        });
      })
      .catch(_logErr('profile bar load'));
  }

  // Quick-remove a zone or priority chip without opening the full form.
  function _quickRemoveProfileItem(kind, value) {
    if (!_currentProfile) return;
    var p = _currentProfile;
    if (kind === 'zone') {
      p.zones = (p.zones || []).filter(function (z) { return z.city !== value; });
    } else if (kind === 'priority') {
      p.priorities = (p.priorities || []).filter(function (pr) { return pr !== value; });
    } else { return; }

    apiFetch(API + '/api/profile', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(p)
    })
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (data && data.ok) {
        _mapAllProps = null;
        apiFetch(API + '/api/score', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
          .then(function () { loadProperties(1, 'score', 0); loadStats(); loadProfileBar(); })
          .catch(_logErr('quick-remove re-score'));
      }
    })
    .catch(_logErr('quick-remove profile'));
  }

  // Profile form state
  var _pfZones = [];

  function _pfFormatCHF(v) {
    if (!v || v === 0) return '—';
    if (v >= 1000000) return "CHF " + (v/1000000).toFixed(1).replace('.0','') + " M";
    return "CHF " + v.toString().replace(/\B(?=(\d{3})+(?!\d))/g, "'");
  }

  function _pfRenderZones() {
    var c = $('pf-zone-list');
    if (!c) return;
    c.innerHTML = _pfZones.map(function (z, i) {
      var radiusLabel = (z.radius_km == 0) ? 'Commune exacte' : z.radius_km + ' km';
      return '<div class="pf-zone"><span>' + ICO.pin + ' ' + escapeHtml(z.city) + '</span><span style="color:#0ea5e9;font-size:12px;font-weight:600">' + radiusLabel + '</span><button onclick="_pfRmZone(' + i + ')" style="background:none;border:none;color:#ef4444;cursor:pointer;font-size:14px">✕</button></div>';
    }).join('');
  }

  // ── NPA / City autocomplete using geo.admin.ch ──
  var _pfAutoSelected = null;
  var _pfAutoTimer = null;

  function _pfSetupAutocomplete() {
    var input = $('pf-new-city');
    if (!input) return;
    // Create dropdown
    var dd = document.createElement('div');
    dd.id = 'pf-city-autocomplete';
    dd.style.cssText = 'position:absolute;z-index:1000;background:#fff;border:1px solid #d1d5db;border-radius:8px;box-shadow:0 4px 12px rgba(0,0,0,.12);max-height:220px;overflow-y:auto;display:none;width:100%';
    input.parentNode.style.position = 'relative';
    input.parentNode.appendChild(dd);

    input.setAttribute('autocomplete', 'off');
    input.addEventListener('input', function () {
      _pfAutoSelected = null;
      _pfClearZoneError();
      clearTimeout(_pfAutoTimer);
      var q = input.value.trim();
      if (q.length < 2) { dd.style.display = 'none'; return; }
      _pfAutoTimer = setTimeout(function () { _pfFetchSuggestions(q, dd, input); }, 250);
    });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        // v6.3.2 Bug #2: si le dropdown est visible avec des suggestions et
        // aucune n'a été sélectionnée, auto-sélectionne la première (UX : pas
        // obliger le clic quand l'user a tapé presque tout le nom).
        if (!_pfAutoSelected && dd.style.display !== 'none') {
          var first = dd.querySelector('.pf-auto-item');
          if (first) { first.click(); }
        }
        _pfAddZone();
        dd.style.display = 'none';
      }
      if (e.key === 'Escape') dd.style.display = 'none';
    });
    document.addEventListener('click', function (e) {
      if (!input.contains(e.target) && !dd.contains(e.target)) dd.style.display = 'none';
    });
  }

  function _pfFetchSuggestions(query, dd, input) {
    // geo.admin.ch search API — returns Swiss localities with NPA + coords
    var url = 'https://api3.geo.admin.ch/rest/services/api/SearchServer?sr=4326&lang=fr&limit=8&type=locations&searchText=' + encodeURIComponent(query);
    fetch(url).then(function (r) { return r.json(); }).then(function (data) {
      var results = (data.results || []).filter(function (r) {
        // Only keep "gg25" (communes) and "zipcode" location types
        var attrs = r.attrs || {};
        return attrs.origin === 'gg25' || attrs.origin === 'zipcode' || attrs.origin === 'district';
      }).slice(0, 8);
      if (!results.length) { dd.style.display = 'none'; return; }
      dd.innerHTML = results.map(function (r, i) {
        var a = r.attrs || {};
        var label = (a.label || '').replace(/<[^>]+>/g, '').trim();
        // Extract NPA if in label (e.g., "2074 Marin-Epagnier")
        var npaMatch = label.match(/^(\d{4})\s+(.+)/);
        var npa = npaMatch ? npaMatch[1] : '';
        var cityName = npaMatch ? npaMatch[2] : label;
        // Clean HTML artifacts
        cityName = cityName.replace(/\s*\(.*\)\s*$/, '').trim();
        var canton = (a.detail || '').replace(/<[^>]+>/g, '').trim();
        // Extract canton abbreviation from detail (often "neuch\u00e2tel" → "NE")
        var cantonAbbr = '';
        var cMap = {'vaud':'VD','genève':'GE','neuchâtel':'NE','fribourg':'FR','valais':'VS','berne':'BE','jura':'JU','bâle-ville':'BS','zurich':'ZH','lucerne':'LU','tessin':'TI','st-gall':'SG','argovie':'AG','thurgovie':'TG','soleure':'SO','bâle-campagne':'BL','grisons':'GR','schwyz':'SZ'};
        var detailLow = canton.toLowerCase();
        for (var ck in cMap) { if (detailLow.indexOf(ck) > -1) { cantonAbbr = cMap[ck]; break; } }
        return '<div class="pf-auto-item" data-idx="' + i + '" data-lat="' + (a.lat || '') + '" data-lng="' + (a.lon || '') + '" data-npa="' + npa + '" data-city="' + escapeHtml(cityName) + '" data-canton="' + cantonAbbr + '" style="padding:8px 12px;cursor:pointer;font-size:14px;border-bottom:1px solid #f3f4f6;display:flex;justify-content:space-between;align-items:center">' +
          '<span>' + escapeHtml(npa ? npa + ' ' + cityName : cityName) + '</span>' +
          (cantonAbbr ? '<span style="color:#6b7280;font-size:12px">' + cantonAbbr + '</span>' : '') +
        '</div>';
      }).join('');
      dd.style.display = 'block';
      // Click handler on items
      var items = dd.querySelectorAll('.pf-auto-item');
      items.forEach(function (el) {
        el.addEventListener('mouseenter', function () { el.style.background = '#F2EEE5'; });
        el.addEventListener('mouseleave', function () { el.style.background = '#fff'; });
        el.addEventListener('click', function () {
          var npa = el.getAttribute('data-npa');
          var city = el.getAttribute('data-city');
          var displayName = npa ? npa + ' ' + city : city;
          input.value = displayName;
          _pfAutoSelected = {
            label: displayName,
            city: city,
            npa: npa,
            canton: el.getAttribute('data-canton'),
            lat: parseFloat(el.getAttribute('data-lat')) || null,
            lng: parseFloat(el.getAttribute('data-lng')) || null
          };
          dd.style.display = 'none';
        });
      });
    }).catch(function () { dd.style.display = 'none'; });
  }

  // Expose globally for onclick
  window._pfRmZone = function (i) { _pfZones.splice(i, 1); _pfRenderZones(); };

  // Bug #4 fix: clear compareSet + uncheck all compare checkboxes when the
  // user closes the compare panel. Previously only hid the panel, leaving
  // compareSet populated and UI checkboxes checked — reopening showed stale
  // biens.
  window._closeComparePanel = function () {
    compareSet = {};
    var panel = document.getElementById('compare-panel');
    if (panel) panel.style.display = 'none';
    document.querySelectorAll('.fav-compare-check').forEach(function (cb) { cb.checked = false; });
  };
  function _pfShowZoneError(msg) {
    var el = document.getElementById('pf-zone-error');
    if (el) { el.textContent = msg; el.style.display = 'block'; }
  }
  function _pfClearZoneError() {
    var el = document.getElementById('pf-zone-error');
    if (el) { el.textContent = ''; el.style.display = 'none'; }
  }
  window._pfAddZone = function () {
    var input = $('pf-new-city');
    var city = input ? input.value.trim() : '';
    var kmStr = $('pf-new-km').value;
    var km = parseFloat(kmStr);
    if (isNaN(km)) km = 3;
    if (!city) { _pfClearZoneError(); return; }
    // v6.3.2 Bug #2: on exige qu'une suggestion ait été sélectionnée. Saisie
    // libre (ex: 'corta') → resolve_zone_coords échoue silencieusement,
    // scoring tombe au fallback canton, user voit dashboard vide sans raison.
    if (!_pfAutoSelected) {
      _pfShowZoneError('Sélectionnez une commune dans la liste proposée.');
      if (input) input.focus();
      return;
    }
    var canton = _pfAutoSelected.canton || '';
    var lat = _pfAutoSelected.lat;
    var lng = _pfAutoSelected.lng;
    var postal = _pfAutoSelected.npa || null;
    city = _pfAutoSelected.label;
    _pfZones.push({ city: city, canton: canton, radius_km: km, latitude: lat, longitude: lng, postal_code: postal });
    if (input) input.value = '';
    _pfAutoSelected = null;
    _pfClearZoneError();
    _pfRenderZones();
  };
  window._pfToggleChip = function (el) { el.classList.toggle('on'); };
  window._pfUpdateBudget = function (id) {
    var bmin = $('pf-budget-min'), bmax = $('pf-budget-max');
    if (bmin && bmax) {
      var minV = parseInt(bmin.value), maxV = parseInt(bmax.value);
      if (id === 'pf-budget-min' && minV > maxV) { bmax.value = minV; $('pf-budget-max-label').textContent = _pfFormatCHF(minV); }
      if (id === 'pf-budget-max' && maxV < minV) { bmin.value = maxV; $('pf-budget-min-label').textContent = _pfFormatCHF(maxV); }
    }
    var val = parseInt($(id).value);
    $(id + '-label').textContent = _pfFormatCHF(val);
  };
  window._pfUpdateRange = function (el) {
    el.nextElementSibling.textContent = el.value + (el.dataset.unit || '');
    // Enforce min <= max for paired sliders
    var pairs = [['pf-rooms-min','pf-rooms-max'],['pf-surface-min','pf-surface-max']];
    pairs.forEach(function(pair) {
      if (el.id === pair[0] || el.id === pair[1]) {
        var lo = $(pair[0]), hi = $(pair[1]);
        if (lo && hi) {
          if (el.id === pair[0] && parseFloat(lo.value) > parseFloat(hi.value)) { hi.value = lo.value; hi.nextElementSibling.textContent = hi.value + (hi.dataset.unit || ''); }
          if (el.id === pair[1] && parseFloat(hi.value) < parseFloat(lo.value)) { lo.value = hi.value; lo.nextElementSibling.textContent = lo.value + (lo.dataset.unit || ''); }
        }
      }
    });
  };
  window._pfSetTx = function () {
    var tx = $('pf-transaction').value;
    var bmin = $('pf-budget-min'), bmax = $('pf-budget-max');
    if (tx === 'achat') {
      bmin.min=0; bmin.max=3000000; bmin.step=50000;
      bmax.min=0; bmax.max=3000000; bmax.step=50000;
      if (parseInt(bmax.value)<50000) bmax.value=500000;
    } else {
      bmin.min=0; bmin.max=5000; bmin.step=100;
      bmax.min=0; bmax.max=5000; bmax.step=100;
      if (parseInt(bmax.value)>5000) bmax.value=2500;
      if (parseInt(bmin.value)>5000) bmin.value=0;
    }
    _pfUpdateBudget('pf-budget-min'); _pfUpdateBudget('pf-budget-max');
  };

  // If all images fail to load, show placeholder
  window._checkAllImgsFailed = function (container) {
    if (!container) return;
    var imgs = container.querySelectorAll('.prop-img');
    var allHidden = true;
    for (var i = 0; i < imgs.length; i++) {
      if (imgs[i].style.display !== 'none') { allHidden = false; break; }
    }
    if (allHidden) {
      // Hide carousel buttons/dots, show placeholder
      var btns = container.querySelectorAll('.carousel-btn, .carousel-dots');
      for (var j = 0; j < btns.length; j++) btns[j].style.display = 'none';
      var ph = document.createElement('div');
      ph.className = 'prop-img-placeholder';
      container.appendChild(ph);
    }
  };

  // Image carousel navigation — skip broken images
  window.carouselNav = function (cid, dir) {
    var el = document.getElementById(cid);
    if (!el) return;
    var imgs = el.querySelectorAll('.prop-img, .detail-img');
    var dots = el.querySelectorAll('.carousel-dot');
    var cur = 0;
    imgs.forEach(function (im, i) { if (im.classList.contains('active')) cur = i; });
    imgs[cur].classList.remove('active');
    if (dots[cur]) dots[cur].classList.remove('active');
    // Skip hidden/broken images
    var attempts = imgs.length;
    do {
      cur = (cur + dir + imgs.length) % imgs.length;
      attempts--;
    } while (imgs[cur].style.display === 'none' && attempts > 0);
    imgs[cur].classList.add('active');
    if (dots[cur]) dots[cur].classList.add('active');
    // Update counter if present (detail gallery)
    var counter = el.querySelector('.detail-counter');
    if (counter) counter.textContent = (cur + 1) + ' / ' + imgs.length;
  };

  window.showScoreDetail = function(el, e) {
    if (e) { e.stopPropagation(); e.preventDefault(); }
    // Toggle: if tooltip already visible for this element, just close it
    var existing = document.querySelector('.score-tooltip');
    if (existing) {
      var wasOnSame = existing._sourceEl === el;
      existing.remove();
      if (wasOnSame) return;
    }
    var scores;
    try { scores = JSON.parse(el.getAttribute('data-scores')); } catch(err) { return; }

    function scoreColor(v) { return v >= 80 ? '#059669' : v >= 60 ? '#2A6670' : v >= 40 ? '#d97706' : '#dc2626'; }
    var ST_TIPS = (window.SCORE_TOOLTIPS_INLINE = window.SCORE_TOOLTIPS_INLINE || {
      'Zone': 'Distance entre le bien et vos zones de recherche',
      'Budget': 'Rapport entre le prix et votre budget max',
      'Type': 'Correspondance au type de bien souhaité',
      'Surface': 'Surface et nombre de pièces vs votre minimum',
      'Equip.': 'Équipements demandés détectés dans l\'annonce',
      'Fraicheur': 'Date de publication récente de l\'annonce'
    });
    function scoreRow(label, val) {
      val = val || 0;
      var tip = ST_TIPS[label] || '';
      var helpHtml = tip ? ' <span class="st-help" title="' + tip.replace(/"/g, '&quot;') + '" style="display:inline-block;width:14px;height:14px;line-height:14px;text-align:center;font-size:10px;font-weight:700;background:#E4DFD4;color:#4A5468;border-radius:50%;cursor:help;margin-left:4px">?</span>' : '';
      return '<div class="st-row" title="' + tip.replace(/"/g, '&quot;') + '"><span>' + label + helpHtml + '</span>' +
        '<div style="flex:1;margin:0 8px;height:6px;background:#E4DFD4;border-radius:3px"><div style="width:' + val + '%;height:100%;background:' + scoreColor(val) + ';border-radius:3px"></div></div>' +
        '<strong>' + val + '</strong></div>';
    }

    var tip = document.createElement('div');
    tip.className = 'score-tooltip';
    tip._sourceEl = el;
    tip.innerHTML = '<div style="font-weight:700;font-size:14px;margin-bottom:4px;color:#1E2A44">Détail du score</div>' +
        '<div style="font-size:11px;color:#7A8398;margin-bottom:10px">Survolez un critère pour l\'explication</div>' +
        scoreRow('Zone', scores.zone) +
        scoreRow('Budget', scores.budget) +
        scoreRow('Type', scores.type) +
        scoreRow('Surface', scores.surface) +
        scoreRow('Equip.', scores.equipment) +
        scoreRow('Fraicheur', scores.freshness) +
        '<button onclick="event.stopPropagation();this.parentNode.remove()" style="margin-top:10px;width:100%;background:#EEE9DE;border:1px solid #E4DFD4;border-radius:6px;padding:5px 12px;cursor:pointer;font-size:12px;color:#4A5468">Fermer</button>';

    // Position tooltip on body to avoid overflow:hidden clipping
    var rect = el.getBoundingClientRect();
    tip.style.position = 'fixed';
    tip.style.top = (rect.bottom + 6) + 'px';
    tip.style.left = Math.max(8, rect.left) + 'px';
    tip.style.zIndex = '9999';
    document.body.appendChild(tip);

    // Ensure tooltip doesn't overflow right edge
    setTimeout(function() {
      var tipRect = tip.getBoundingClientRect();
      if (tipRect.right > window.innerWidth - 8) {
        tip.style.left = Math.max(8, window.innerWidth - tipRect.width - 8) + 'px';
      }
    }, 0);

    // Close on outside click
    setTimeout(function() {
      document.addEventListener('click', function closeTip(ev) {
        if (!tip.contains(ev.target) && ev.target !== el && !el.contains(ev.target)) {
          tip.remove();
          document.removeEventListener('click', closeTip);
        }
      });
    }, 10);
  };

  // ============================================================
  // PROPERTY DETAIL VIEW
  // ============================================================
  window.openPropertyDetail = function(id) {
    // Don't open if clicking on score tooltip, fav button, or source link
    if (event && (event.target.closest('.fav-btn') || event.target.closest('.prop-source-link') || event.target.closest('.prop-score') || event.target.closest('.carousel-btn') || event.target.closest('.score-tooltip'))) return;

    var p = (window._propData || {})[id];
    if (!p) return;

    var existing = document.querySelector('.detail-overlay');
    if (existing) existing.remove();

    var gradeColors = { A: '#059669', B: '#2A6670', C: '#d97706', D: '#dc2626' };
    var gc = gradeColors[p.grade] || '#7A8398';

    // Images gallery — upgrade thumbnails to HD for detail view
    function _hdImg(url) {
      if (!url) return url;
      // Homegate/Cloudinary: upgrade width from thumbnail to HD
      // e.g. /t_fill,f_auto,q_auto,w_200/ → /t_fill,f_auto,q_auto,w_1200/
      url = url.replace(/\/t_[^/]*w_\d+[^/]*\//g, function(match) {
        return match.replace(/w_\d+/, 'w_1200');
      });
      // Also handle h_ parameter: /c_fill,f_auto,h_150,... → h_800
      url = url.replace(/h_\d+/g, 'h_800');
      // Generic Cloudinary transforms: /w_XXX,h_YYY/ or /w_XXX/
      url = url.replace(/\/w_\d+(,h_\d+)?\//g, '/w_1200/');
      // Properstar: ?width=300&height=255 → ?width=1200&height=800
      url = url.replace(/width=\d+/g, 'width=1200').replace(/height=\d+/g, 'height=800');
      // Immobilier.ch: /NewThumbnail/ → /Original/ or /Big/
      url = url.replace(/\/NewThumbnail\//g, '/Big/');
      // Flatfox: add ?w=1200 if no size params
      if (url.includes('flatfox') && !url.includes('w=')) {
        url += (url.includes('?') ? '&' : '?') + 'w=1200';
      }
      return url;
    }

    var galleryHtml = '';
    if (p.images && p.images.length > 0) {
      var gid = 'detail-gallery';
      galleryHtml = '<div class="detail-gallery" id="' + gid + '">';
      for (var i = 0; i < p.images.length; i++) {
        galleryHtml += '<img src="' + escapeHtml(_hdImg(p.images[i])) + '" class="detail-img' + (i === 0 ? ' active' : '') + '" data-idx="' + i + '" onerror="this.src=\'' + escapeHtml(p.images[i]) + '\'">';
      }
      if (p.images.length > 1) {
        galleryHtml += '<button class="carousel-btn prev" onclick="event.stopPropagation();carouselNav(\'' + gid + '\',-1)">&#8249;</button>';
        galleryHtml += '<button class="carousel-btn next" onclick="event.stopPropagation();carouselNav(\'' + gid + '\',1)">&#8250;</button>';
        galleryHtml += '<span class="detail-counter">1 / ' + p.images.length + '</span>';
      }
      galleryHtml += '</div>';
    } else {
      galleryHtml = '<div class="detail-gallery-empty">Pas d\'image disponible</div>';
    }

    // Price
    var priceHtml = p.price ? formatPrice(p.price) + ' CHF' : 'Prix sur demande';
    if (p.price_drop) {
      priceHtml = '<span class="price-drop-badge" style="font-size:14px">↓ ' + Math.round(p.price_drop.change_pct) + '%</span> ' + priceHtml + ' <del class="old-price">' + formatPrice(p.price_drop.old_price) + ' CHF</del>';
    }

    // Details table
    var rows = [];
    if (p.rooms) rows.push(['Pièces', p.rooms + ' pcs']);
    if (p.surface) rows.push(['Surface', p.surface + ' m²']);
    if (p.floor !== null && p.floor !== undefined) rows.push(['Étage', p.floor + 'e']);
    if (p.distance_km !== null && p.distance_km !== undefined) rows.push(['Distance', p.distance_km + ' km']);
    if (p.days_online !== null && p.days_online !== undefined) rows.push(['En ligne depuis', p.days_online <= 1 ? 'Aujourd\'hui' : p.days_online + ' jours']);
    if (p.published_at) rows.push(['Publié le', new Date(p.published_at).toLocaleDateString('fr-CH')]);

    var tableHtml = rows.map(function(r) {
      return '<div class="detail-row"><span>' + r[0] + '</span><strong>' + r[1] + '</strong></div>';
    }).join('');

    // Score detail
    var sd = p.score_detail || {};
    var scoreHtml = '<div class="detail-score-wrap">' +
      '<div class="detail-score-badge" style="background:' + gc + '"><span class="dsb-num">' + (p.score||0) + '</span><span class="dsb-grade">' + (p.grade||'') + '</span></div>' +
      '<div class="detail-score-bars">' +
        _detailBar('Zone', sd.zone) + _detailBar('Budget', sd.budget) + _detailBar('Type', sd.type) +
        _detailBar('Surface', sd.surface) + _detailBar('Equip.', sd.equipment) + _detailBar('Fraîcheur', sd.freshness) +
      '</div></div>';

    // Sources
    var sources = p.all_sources || [{ source: p.source || '', url: p.source_url || '' }];
    var sourcesHtml = sources.map(function(s) {
      var name = (s.source || '').replace('www.', '').split('.')[0] || 'Source';
      if (s.url) {
        return '<a href="' + escapeHtml(s.url) + '" target="_blank" rel="noopener" class="detail-source-link prop-source-link" data-src="' + escapeHtml(s.source || '') + '" onclick="event.stopPropagation()">' + escapeHtml(name) + ' ↗</a>';
      }
      return '<span class="detail-source-text prop-source-link" data-src="' + escapeHtml(s.source || '') + '">' + escapeHtml(name) + '</span>';
    }).join('');

    // Contact
    var contactHtml = '';
    if (p.contact_name || p.contact_phone || p.contact_email) {
      contactHtml = '<div class="detail-section"><h3>Contact</h3><div class="detail-contact">';
      if (p.contact_name) contactHtml += '<div>' + ICO.user + ' ' + escapeHtml(p.contact_name) + '</div>';
      if (p.contact_phone) contactHtml += '<div><a href="tel:' + escapeHtml(p.contact_phone) + '">' + ICO.phone + ' ' + escapeHtml(p.contact_phone) + '</a></div>';
      if (p.contact_email) contactHtml += '<div><a href="mailto:' + escapeHtml(p.contact_email) + '">' + ICO.mail + ' ' + escapeHtml(p.contact_email) + '</a></div>';
      contactHtml += '</div></div>';
    }

    // Features
    var featHtml = '';
    if (p.features && p.features.length > 0) {
      featHtml = '<div class="detail-section"><h3>Équipements</h3><div class="detail-features">' +
        p.features.map(function(f) { return '<span class="detail-feat">' + ICO.check + ' ' + escapeHtml(f) + '</span>'; }).join('') +
      '</div></div>';
    }

    // Build overlay
    var overlay = document.createElement('div');
    overlay.className = 'detail-overlay';
    overlay.innerHTML =
      '<div class="detail-panel">' +
        '<button class="detail-close" onclick="event.stopPropagation();this.closest(\'.detail-overlay\').remove()">✕</button>' +
        galleryHtml +
        '<div class="detail-body">' +
          '<div class="detail-price">' + priceHtml + '</div>' +
          '<h2 class="detail-title">' + escapeHtml(cleanTitle(p.title, p) || _fallbackTitle(p)) + '</h2>' +
          '<div class="detail-address">' + ICO.pin + ' ' + escapeHtml(cleanAddress(p.address, p.city)) + '</div>' +
          '<div class="detail-section"><h3>Caractéristiques</h3><div class="detail-table">' + tableHtml + '</div></div>' +
          (p.description ? '<div class="detail-section"><h3>Description</h3><p class="detail-description">' + escapeHtml(p.description) + ((p.description.length >= 490 || /\.\.\.\s*$/.test(p.description)) && sources.length > 0 && sources[0].url ? ' <a href="' + escapeHtml(sources[0].url) + '" target="_blank" rel="noopener" class="read-more-link" onclick="event.stopPropagation()">Lire la suite sur le portail ↗</a>' : '') + '</p></div>' : '') +
          scoreHtml +
          featHtml +
          contactHtml +
          '<div class="detail-section"><h3>Voir sur le portail</h3><div class="detail-sources">' + sourcesHtml + '</div></div>' +
        '</div>' +
      '</div>';

    overlay.addEventListener('click', function(e) {
      if (e.target === overlay) overlay.remove();
    });

    document.body.appendChild(overlay);
    document.body.style.overflow = 'hidden';
    overlay.querySelector('.detail-close').addEventListener('click', function() {
      document.body.style.overflow = '';
    });
    overlay.addEventListener('click', function(e) {
      if (e.target === overlay) { overlay.remove(); document.body.style.overflow = ''; }
    });
  };

  // Explanatory tooltips for each scoring criterion
  var SCORE_TOOLTIPS = {
    'Zone': 'Correspond à la distance entre le bien et vos zones de recherche. Plus le bien est proche d\'une de vos villes, plus le score est élevé.',
    'Budget': 'Correspond au rapport entre le prix du bien et votre budget max. Un bien pile dans votre fourchette obtient 100, un bien hors budget descend à 0.',
    'Type': 'Correspond au type de bien recherché (appartement, maison, etc.). Score maximal si le type correspond exactement.',
    'Surface': 'Correspond à la surface et au nombre de pièces par rapport à votre minimum. Plus c\'est grand que votre seuil, mieux c\'est.',
    'Equip.': 'Correspond aux équipements demandés (parking, balcon, ascenseur, etc.) détectés dans l\'annonce.',
    'Equipements': 'Correspond aux équipements demandés (parking, balcon, ascenseur, etc.) détectés dans l\'annonce.',
    'Fraicheur': 'Correspond à la date de publication de l\'annonce. Une annonce récente obtient un meilleur score.',
    'Fraîcheur': 'Correspond à la date de publication de l\'annonce. Une annonce récente obtient un meilleur score.'
  };

  // Default weights used in scoring_engine.score_property (w_zone, w_budget, …).
  // Shown next to each criterion in the detail modal so users understand why
  // e.g. a perfect Equip. (10%) barely moves the total vs Zone (30%).
  // If weight customization is ever exposed in the UI, replace this with the
  // values returned by /api/profile.
  var SCORE_WEIGHTS = {
    'Zone': 30,
    'Budget': 25,
    'Type': 20,
    'Surface': 10,
    'Equip.': 10,
    'Equipements': 10,
    'Fraicheur': 5,
    'Fraîcheur': 5
  };

  function _detailBar(label, val) {
    val = val || 0;
    var color = val >= 80 ? '#059669' : val >= 60 ? '#2A6670' : val >= 40 ? '#d97706' : '#dc2626';
    var tip = SCORE_TOOLTIPS[label] || '';
    var tipAttr = tip ? ' title="' + escapeHtml(tip) + '"' : '';
    var weight = SCORE_WEIGHTS[label];
    var weightHtml = (typeof weight === 'number') ? ' <span class="dsb-weight">· ' + weight + '%</span>' : '';
    return '<div class="dsb-row"' + tipAttr + '><span>' + label + weightHtml + '</span><div class="dsb-track"><div class="dsb-fill" style="width:' + val + '%;background:' + color + '"></div></div><strong>' + val + '</strong></div>';
  }

  function toggleProfileForm() {
    var formWrap = $('profile-edit-form');
    if (!formWrap) return;
    if (formWrap.style.display !== 'none') {
      formWrap.style.display = 'none';
      return;
    }
    var p = _currentProfile || {};
    _pfZones = (p.zones || []).filter(function (z) { return z && z.city; }).map(function (z) { return { city: z.city, canton: z.canton || '', radius_km: z.radius_km || 3, latitude: z.latitude || null, longitude: z.longitude || null, postal_code: z.postal_code || null }; });

    var types = ['appartement','maison','villa','immeuble','terrain','parking','commerce'];
    var pTypes = p.property_types || [];
    var typeChips = types.map(function (t) {
      return '<span class="pf-chip' + (pTypes.indexOf(t) > -1 ? ' on' : '') + '" data-v="' + t + '" onclick="_pfToggleChip(this)">' + t.charAt(0).toUpperCase() + t.slice(1) + '</span>';
    }).join('');

    var prios = ['vue','balcon','calme','parking','transports','ecoles','commerces','animaux','cave','jardin','ascenseur','renove','minergie','meuble','buanderie'];
    var prioLabels = {'vue':'Vue dégagée','balcon':'Balcon/terrasse','calme':'Calme','parking':'Parking','transports':'Proche transports','ecoles':'Proche écoles','commerces':'Proche commerces','animaux':'Animaux acceptés','cave':'Cave','jardin':'Jardin','ascenseur':'Ascenseur','renove':'Rénové','minergie':'Minergie','meuble':'Meublé','buanderie':'Buanderie'};
    var pPrios = p.priorities || [];
    var prioChips = prios.map(function (pr) {
      return '<span class="pf-chip' + (pPrios.indexOf(pr) > -1 ? ' on' : '') + '" data-v="' + pr + '" onclick="_pfToggleChip(this)">' + escapeHtml(prioLabels[pr] || pr) + '</span>';
    }).join('');

    var isAchat = p.transaction === 'achat';
    var bMinMax = isAchat ? 3000000 : 5000;
    var bStep = isAchat ? 50000 : 100;
    var bMinVal = p.budget_min || 0;
    var bMaxVal = p.budget_max || (isAchat ? 500000 : 2500);
    var rMinVal = p.rooms_min || 3;
    var rMaxVal = p.rooms_max || 6;
    var sMinVal = p.surface_min || 60;
    var sMaxVal = p.surface_max || 200;

    formWrap.innerHTML =
      '<div class="profile-form">' +
        // Row 1: Zones + Type side by side
        '<div class="pf-row">' +
          '<div class="pf-section pf-flex1"><div class="pf-section-title">' + ICO.pin + ' Zones géographiques</div>' +
            '<div id="pf-zone-list" class="pf-zone-list"></div>' +
            '<div class="pf-zone-add">' +
              '<input id="pf-new-city" type="text" placeholder="Ajouter une ville..." style="flex:1">' +
              '<select id="pf-new-km"><option value="0">Commune exacte</option><option value="1">1 km</option><option value="2">2 km</option><option value="3" selected>3 km</option><option value="5">5 km</option><option value="10">10 km</option><option value="15">15 km</option><option value="20">20 km</option></select>' +
              '<button class="pf-add-btn" onclick="_pfAddZone()">+</button>' +
            '</div>' +
            '<div id="pf-zone-error" style="display:none;color:#b91c1c;font-size:13px;margin-top:6px"></div>' +
          '</div>' +
          '<div class="pf-section pf-flex1"><div class="pf-section-title">' + ICO.home + ' Type de bien</div>' +
            '<div class="pf-chips" id="pf-types">' + typeChips + '</div>' +
          '</div>' +
        '</div>' +
        // Row 2: Transaction + Budget
        '<div class="pf-section"><div class="pf-section-title">' + ICO.money + ' Transaction & Budget</div>' +
          '<div class="pf-budget-grid">' +
            '<div class="pf-field"><label>Transaction</label><select id="pf-transaction" onchange="_pfSetTx()">' +
              '<option value="location"' + (p.transaction !== 'achat' ? ' selected' : '') + '>Location</option>' +
              '<option value="achat"' + (p.transaction === 'achat' ? ' selected' : '') + '>Achat</option>' +
            '</select></div>' +
            '<div class="pf-field"><label>Budget min</label>' +
              '<div class="pf-range"><input type="range" id="pf-budget-min" min="0" max="' + bMinMax + '" step="' + bStep + '" value="' + bMinVal + '" oninput="_pfUpdateBudget(\'pf-budget-min\')"><span id="pf-budget-min-label">' + _pfFormatCHF(bMinVal) + '</span></div></div>' +
            '<div class="pf-field"><label>Budget max</label>' +
              '<div class="pf-range"><input type="range" id="pf-budget-max" min="0" max="' + bMinMax + '" step="' + bStep + '" value="' + bMaxVal + '" oninput="_pfUpdateBudget(\'pf-budget-max\')"><span id="pf-budget-max-label">' + _pfFormatCHF(bMaxVal) + '</span></div></div>' +
          '</div>' +
        '</div>' +
        // Row 3: Pièces + Surface
        '<div class="pf-section"><div class="pf-section-title">' + ICO.ruler + ' Caractéristiques</div>' +
          '<div class="pf-grid">' +
            '<div class="pf-field"><label>Pièces min</label><div class="pf-range"><input type="range" id="pf-rooms-min" min="1" max="10" step="0.5" value="' + rMinVal + '" data-unit=" pcs" oninput="_pfUpdateRange(this)"><span>' + rMinVal + ' pcs</span></div></div>' +
            '<div class="pf-field"><label>Pièces max</label><div class="pf-range"><input type="range" id="pf-rooms-max" min="1" max="10" step="0.5" value="' + rMaxVal + '" data-unit=" pcs" oninput="_pfUpdateRange(this)"><span>' + rMaxVal + ' pcs</span></div></div>' +
            '<div class="pf-field"><label>Surface min (m²)</label><div class="pf-range"><input type="range" id="pf-surface-min" min="20" max="300" step="5" value="' + sMinVal + '" data-unit=" m²" oninput="_pfUpdateRange(this)"><span>' + sMinVal + ' m²</span></div></div>' +
            '<div class="pf-field"><label>Surface max (m²)</label><div class="pf-range"><input type="range" id="pf-surface-max" min="20" max="500" step="5" value="' + sMaxVal + '" data-unit=" m²" oninput="_pfUpdateRange(this)"><span>' + sMaxVal + ' m²</span></div></div>' +
          '</div>' +
        '</div>' +
        // Row 4: Priorités
        '<div class="pf-section"><div class="pf-section-title">' + ICO.star + ' Priorités & Équipements</div>' +
          '<div class="pf-chips" id="pf-priorities">' + prioChips + '</div>' +
        '</div>' +
        // Actions
        '<div class="pf-actions">' +
          '<button id="pf-save" class="pf-save-btn">Sauvegarder & relancer Lou ' + ICO.search + '</button>' +
          '<button id="pf-cancel" class="pf-cancel-btn">Annuler</button>' +
        '</div>' +
      '</div>';

    formWrap.style.display = 'block';
    _pfRenderZones();
    _pfSetupAutocomplete();

    $('pf-cancel').onclick = function () { formWrap.style.display = 'none'; };
    $('pf-save').onclick = function () { saveProfileForm(); };
  }

  function saveProfileForm() {
    var bmin = parseInt($('pf-budget-min').value) || null;
    var bmax = parseInt($('pf-budget-max').value) || null;
    if (bmin === 0) bmin = null;
    if (bmax === 0) bmax = null;

    var payload = {
      transaction: $('pf-transaction').value,
      property_types: Array.from(document.querySelectorAll('#pf-types .pf-chip.on')).map(function (c) { return c.dataset.v; }),
      budget_max: bmax,
      budget_min: bmin,
      rooms_min: parseFloat($('pf-rooms-min').value) || null,
      rooms_max: parseFloat($('pf-rooms-max').value) || null,
      surface_min: parseInt($('pf-surface-min').value) || null,
      surface_max: parseInt($('pf-surface-max').value) || null,
      priorities: Array.from(document.querySelectorAll('#pf-priorities .pf-chip.on')).map(function (c) { return c.dataset.v; }),
      zones: _pfZones
    };

    var btn = $('pf-save');
    var formWrap = $('profile-edit-form');
    var actions = btn.parentNode;

    // Clean visual loading state: spinner in the button, grayed-out fields,
    // explanatory hint next to the buttons. Everything is torn down on error
    // or on success (loadProfileBar wipes the wrapper on success).
    btn.innerHTML = '<span class="pf-spinner"></span>Sauvegarde...';
    btn.disabled = true;
    if (formWrap) formWrap.classList.add('pf-loading');
    var hint = document.createElement('div');
    hint.className = 'pf-hint';
    hint.id = 'pf-hint';
    hint.textContent = 'Cela peut prendre quelques secondes…';
    if (actions && !$('pf-hint')) actions.insertBefore(hint, actions.firstChild);

    function clearLoading() {
      btn.disabled = false;
      if (formWrap) formWrap.classList.remove('pf-loading');
      var h = $('pf-hint'); if (h) h.remove();
    }

    apiFetch(API + '/api/profile', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    })
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (!data || !data.ok) {
        btn.innerHTML = 'Erreur — reessayez';
        clearLoading();
        return;
      }
      // Profile saved — now re-run Lou (score + reload list + stats).
      // Show "analyzing" state on the button BEFORE we kick off anything that
      // could re-render the profile bar (loadProfileBar regenerates
      // #profile-edit-form, which would visually close the form mid-flight
      // and detach this btn reference).
      btn.innerHTML = '<span class="pf-spinner"></span>Lou analyse vos nouveaux critères…';
      _mapAllProps = null; // Force map to reload all properties

      // Re-score, then refresh everything in one wave. loadProfileBar() runs
      // LAST so the form disappearance acts as the visible "done" signal.
      // Each refresh is isolated so one throw doesn't kill the rest, AND
      // doesn't bubble up to the outer .catch (which would mask the real error
      // by showing "Erreur reseau" instead of e.g. a /api/score failure).
      return apiFetch(API + '/api/score', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
        .catch(function (e) { console.warn('[saveProfile] /api/score failed', e); })
        .then(function () {
          try { loadProperties(1, 'score', 0); } catch (e) { console.error('[saveProfile] loadProperties threw', e); }
          try { loadStats(); } catch (e) { console.error('[saveProfile] loadStats threw', e); }
          try { loadProfileBar(); } catch (e) { console.error('[saveProfile] loadProfileBar threw', e); }
          try { window.scrollTo({ top: 0, behavior: 'smooth' }); } catch (e) { window.scrollTo(0, 0); }
        });
    })
    .catch(function (e) {
      // Surface the real error on the button so we can diagnose without DevTools.
      console.error('[saveProfile] outer catch fired', e);
      var msg = (e && (e.message || e.name)) || 'inconnu';
      btn.textContent = 'Err: ' + String(msg).slice(0, 60);
      clearLoading();
    });
  }

  // ============================================================
  // LOAD PROPERTIES
  // ============================================================
  var currentPage = 1;
  var currentSort = 'score';
  var currentMinScore = 0;
  var currentNewOnly = false;
  var currentIncludeNearby = false;
  // Track first-login auto-refresh timers so they can be cleared when results
  // arrive (or the user navigates away) — otherwise they re-trigger a full
  // load long after the user has moved on.
  var _firstLoginTimers = [];
  function _clearFirstLoginTimers() {
    while (_firstLoginTimers.length) { clearTimeout(_firstLoginTimers.pop()); }
  }

  function loadProperties(page, sort, minScore, newOnly, includeNearby) {
    currentPage = page;
    currentSort = sort || currentSort;
    currentMinScore = minScore !== undefined ? minScore : currentMinScore;
    currentNewOnly = newOnly !== undefined ? newOnly : currentNewOnly;
    currentIncludeNearby = includeNearby !== undefined ? includeNearby : currentIncludeNearby;

    var list = $('properties-list');
    list.innerHTML = '<div class="dash-loading">Chargement...</div>';

    var url = API + '/api/properties' +
      '?page=' + page +
      '&per_page=12' +
      '&sort=' + currentSort +
      '&min_score=' + currentMinScore +
      (currentNewOnly ? '&new_only=true' : '') +
      (currentIncludeNearby ? '&include_nearby=true' : '');

    apiFetch(url)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.properties || data.properties.length === 0) {
          // v6.3.1 Bug #2: distinguish three empty cases:
          //   A) no profile → "Configurez vos critères"
          //   B) profile + scoring recent (<3 min) or lou_first_login → "Lou est en chasse"
          //   C) profile + scoring done (>3 min ago) → "Aucun match — ajustez vos critères"
          // Without the last_scored_at gate, a user with overly narrow criteria
          // (e.g. 10 rooms, 5M CHF in Val-de-Travers) sees "1-3 min" forever.
          var scoringFresh = false;
          if (_lastStats && _lastStats.last_scored_at) {
            try {
              var ageMs = Date.now() - new Date(_lastStats.last_scored_at).getTime();
              scoringFresh = ageMs < 3 * 60 * 1000; // < 3 min
            } catch (_) {}
          }
          // First-login flag is a frontend hint — still useful right after signup
          // when last_scored_at is null because scoring hasn't run yet.
          var isFirstLogin = _isFirstLoginActive() && _hasProfile;
          var scoringInProgress = _hasProfile && (scoringFresh || (isFirstLogin && _lastStats && !_lastStats.last_scored_at));
          if (!_hasProfile) {
            // Case A — no profile. profile-bar above already shows a prominent
            // "Aucun profil" CTA; reinforce here with a clear call to action.
            list.innerHTML = '<div class="dash-empty">' +
              '<h3 style="margin-bottom:8px;font-family:Fraunces,serif">Configurez vos critères</h3>' +
              '<p>Lou a besoin de vos critères pour chercher. <a href="#" class="open-chat-link" style="color:#2A6670;cursor:pointer">Parlez-lui</a> pour démarrer.</p>' +
            '</div>';
            var chatLinkNp = list.querySelector('.open-chat-link');
            if (chatLinkNp) chatLinkNp.onclick = function(e) { e.preventDefault(); _openChat(); };
            $('pagination').innerHTML = '';
            return;
          }
          if (currentNewOnly) {
            list.innerHTML = '<div class="dash-empty">' +
              '<h3 style="margin-bottom:8px;font-family:Fraunces,serif">Aucun nouveau bien</h3>' +
              '<p>Aucun nouveau bien n\'a été détecté dans les dernières 24 heures.</p>' +
              '<p style="margin-top:12px">Les résultats se mettent à jour automatiquement.</p>' +
            '</div>';
          } else if (isFirstLogin || scoringInProgress) {
            list.innerHTML = '<div class="dash-empty">' +
              '<div style="margin-bottom:16px">' + ICO.wolf + '</div>' +
              '<h3 style="margin-bottom:8px;font-family:Fraunces,serif">Bienvenue ! Lou est en chasse...</h3>' +
              '<p>Votre première recherche est en cours sur 8 portails immobiliers suisses. Les résultats apparaîtront dans <strong>1 à 3 minutes</strong>.</p>' +
              '<div class="first-login-progress" style="margin:20px auto;width:200px;height:4px;background:#E4DFD4;border-radius:4px;overflow:hidden">' +
                '<div style="height:100%;background:#2A6670;border-radius:4px;animation:progressBar 90s linear forwards"></div>' +
              '</div>' +
              '<p style="margin-top:12px;color:#7A8398;font-size:13px">Actualisez la page dans un instant, ou <a href="#" class="open-chat-link" style="color:#2A6670;cursor:pointer">discutez avec Lou</a> en attendant.</p>' +
              '<button class="dash-btn" id="first-login-refresh" style="margin-top:16px;padding:10px 24px;background:#2A6670;color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:14px;font-weight:600">↻ Actualiser les résultats</button>' +
            '</div>';
            // Auto-refresh after 30s and 90s. Clear any prior timers first so a
            // re-render doesn't stack them, and track ids so we can cancel once
            // results arrive.
            _clearFirstLoginTimers();
            _firstLoginTimers.push(setTimeout(function () {
              loadProperties(1, 'score', 0);
              loadStats();
            }, 30000));
            _firstLoginTimers.push(setTimeout(function () {
              loadProperties(1, 'score', 0);
              loadStats();
              _clearFirstLogin();
            }, 90000));
          } else if (_lastStats && _lastStats.last_scored_at) {
            // Case C: scoring done (>3 min ago), 0 match → criteria likely too narrow.
            // Point the user at adjustments rather than implying Lou is still working.
            list.innerHTML = '<div class="dash-empty">' +
              '<h3 style="margin-bottom:8px;font-family:Fraunces,serif">Aucun bien ne correspond</h3>' +
              '<p>Vos critères sont peut-être trop étroits. Essayez d\'élargir le budget, la zone ou le nombre de pièces.</p>' +
              '<p style="margin-top:12px"><a href="#" class="open-chat-link" style="color:#2A6670;cursor:pointer">Parlez à Lou</a> pour ajuster, ou modifiez directement votre profil ci-dessus.</p>' +
            '</div>';
          } else {
            // Case D: profile exists but no last_scored_at yet (scoring not run, or
            // background scrape still warming up). Auto-refresh once after 10s so
            // the user doesn't sit on a stale empty state.
            list.innerHTML = '<div class="dash-empty">' +
              '<h3 style="margin-bottom:8px;font-family:Fraunces,serif">Analyse en cours…</h3>' +
              '<p>Vos premiers résultats apparaîtront dans quelques secondes.</p>' +
              '<p style="margin-top:12px">En attendant, <a href="#" class="open-chat-link" style="color:#2A6670;cursor:pointer">parlez à Lou</a> pour affiner vos critères.</p>' +
            '</div>';
            _clearFirstLoginTimers();
            _firstLoginTimers.push(setTimeout(function () {
              loadStats();
              loadProperties(1, 'score', 0);
            }, 10000));
          }
          var chatLink = list.querySelector('.open-chat-link');
          if (chatLink) chatLink.onclick = function(e) { e.preventDefault(); _openChat(); };
          var refreshBtn = $('first-login-refresh');
          if (refreshBtn) refreshBtn.onclick = function () {
            this.textContent = '⟳ Chargement...';
            this.disabled = true;
            // Trigger scoring first, then reload
            apiFetch(API + '/api/score', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
              .then(function () { loadProperties(1, 'score', 0); loadStats(); })
              .catch(function () { loadProperties(1, 'score', 0); loadStats(); });
          };
          $('pagination').innerHTML = '';
          return;
        }

        // Clear first login flag once we have results + cancel any pending
        // first-login auto-refresh timers so they don't fire later on top of
        // a fully-loaded dashboard.
        _clearFirstLogin();
        _clearFirstLoginTimers();

        // Store property data for detail view
        window._propData = window._propData || {};
        data.properties.forEach(function (p) { window._propData[p.id] = p; });

        var html = '';
        if (currentNewOnly) {
          html += '<div class="new-filter-banner">' +
            '<span>Nouveaux biens des dernières 24h</span>' +
            '<button class="new-filter-clear" id="clear-new-filter">✕ Voir tous les biens</button>' +
          '</div>';
        }
        // Low-result banner: offer to widen search when strict zone is sparse
        var nearby = parseInt(data.nearby_available || 0, 10);
        if (!currentIncludeNearby && !currentNewOnly && data.total < 5 && nearby > 0) {
          html += '<div class="nearby-banner">' +
            '<span><strong>' + data.total + ' bien' + (data.total > 1 ? 's' : '') + '</strong> dans votre zone — ' +
            nearby + ' autre' + (nearby > 1 ? 's' : '') + ' disponible' + (nearby > 1 ? 's' : '') + ' à proximité.</span>' +
            '<button class="nearby-expand" id="nearby-expand-btn">Élargir la recherche</button>' +
          '</div>';
        } else if (currentIncludeNearby) {
          html += '<div class="nearby-banner nearby-banner-active">' +
            '<span>Zone élargie — inclut les biens proches hors zone stricte.</span>' +
            '<button class="nearby-expand" id="nearby-collapse-btn">Retour à la zone stricte</button>' +
          '</div>';
        }
        html += '<div class="prop-grid">';
        data.properties.forEach(function (p) {
          html += renderPropertyCard(p);
        });
        html += '</div>';
        list.innerHTML = html;

        // Hook clear new filter button
        var clearBtn = $('clear-new-filter');
        if (clearBtn) {
          clearBtn.onclick = function () {
            currentNewOnly = false;
            document.querySelectorAll('.dash-stat').forEach(function(s) { s.classList.remove('stat-active'); });
            loadProperties(1, 'score', 0, false);
          };
        }
        var expandBtn = $('nearby-expand-btn');
        if (expandBtn) {
          expandBtn.onclick = function () { loadProperties(1, currentSort, currentMinScore, currentNewOnly, true); };
        }
        var collapseBtn = $('nearby-collapse-btn');
        if (collapseBtn) {
          collapseBtn.onclick = function () { loadProperties(1, currentSort, currentMinScore, currentNewOnly, false); };
        }

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
    var gradeColors = { A: '#059669', B: '#2A6670', C: '#d97706', D: '#dc2626' };
    var gc = gradeColors[p.grade] || '#7A8398';

    var imgHtml = '';
    if (p.images && p.images.length > 1) {
      // Carousel with arrows
      var cid = 'carousel-' + p.id;
      imgHtml = '<div class="prop-carousel" id="' + cid + '">';
      for (var ii = 0; ii < p.images.length; ii++) {
        imgHtml += '<img src="' + escapeHtml(p.images[ii]) + '" alt="" class="prop-img' + (ii === 0 ? ' active' : '') + '" data-idx="' + ii + '" onerror="this.style.display=\'none\';_checkAllImgsFailed(this.parentNode)">';
      }
      imgHtml += '<button class="carousel-btn prev" onclick="event.stopPropagation();carouselNav(\'' + cid + '\',-1)">&#8249;</button>';
      imgHtml += '<button class="carousel-btn next" onclick="event.stopPropagation();carouselNav(\'' + cid + '\',1)">&#8250;</button>';
      imgHtml += '<span class="carousel-dots">';
      for (var di = 0; di < p.images.length; di++) {
        imgHtml += '<span class="carousel-dot' + (di === 0 ? ' active' : '') + '"></span>';
      }
      imgHtml += '</span>';
      imgHtml += '</div>';
    } else if (p.images && p.images.length === 1) {
      imgHtml = '<img src="' + escapeHtml(p.images[0]) + '" alt="" class="prop-img active" onerror="this.style.display=\'none\';_checkAllImgsFailed(this.parentNode)">';
    }

    var daysOnline = p.days_online || 0;
    var daysText = daysOnline <= 1 ? 'Nouveau' : daysOnline + 'j';
    var daysColor = daysOnline <= 3 ? '#059669' : daysOnline <= 14 ? '#2A6670' : daysOnline <= 30 ? '#d97706' : '#7A8398';

    var priceText = p.price ? formatPrice(p.price) + ' CHF' : 'Prix sur demande';
    if (p.unit && p.price) {
      var unitPart = (p.unit.split('/')[1] || '').toLowerCase();
      // Don't show "/total" or "/one-time" for purchases — only show for rentals
      if (unitPart && unitPart !== 'total' && unitPart !== 'one-time') {
        priceText += '<small>/' + escapeHtml(unitPart) + '</small>';
      }
    }
    if (p.price_drop) {
      priceText = '<span class="price-drop-badge">↓ ' + Math.round(p.price_drop.change_pct) + '%</span> ' + priceText + '<del class="old-price">' + formatPrice(p.price_drop.old_price) + ' CHF</del>';
    }

    var details = [];
    if (p.rooms && p.rooms > 0 && p.rooms < 20) details.push(p.rooms + ' pcs');
    if (p.surface && p.surface > 0) details.push(p.surface + ' m\u00B2');
    if (p.floor !== null && p.floor !== undefined) details.push(p.floor + 'e etage');
    if (p.distance_km !== null && p.distance_km !== undefined) details.push(p.distance_km + ' km');

    var sourceLabel = (p.source || '').replace('www.', '').split('.')[0] || 'Source';

    var scoreDetailAttr = '';
    if (p.score_detail) {
      scoreDetailAttr = ' data-scores=\'' + JSON.stringify({zone: p.score_detail.zone||0, budget: p.score_detail.budget||0, type: p.score_detail.type||0, surface: p.score_detail.surface||0, equipment: p.score_detail.equipment||0, freshness: p.score_detail.freshness||0}) + '\' onclick="showScoreDetail(this, event)" title="Cliquez pour voir le détail du score"';
    }

    return '<div class="prop-card" onclick="openPropertyDetail(' + p.id + ')" style="cursor:pointer">' +
      '<div class="prop-card-top">' +
        (imgHtml || '<div class="prop-img-placeholder"></div>') +
        '<div class="prop-score" style="background:' + gc + '"' + scoreDetailAttr + '>' +
          '<span class="prop-score-num">' + p.score + '</span>' +
          '<span class="prop-score-grade">' + p.grade + '</span>' +
        '</div>' +
        '<div class="prop-days" style="background:' + daysColor + '">' + daysText + '</div>' +
        '<button class="fav-btn' + (p.is_favorite ? ' active' : '') + '" data-id="' + p.id + '" title="Favori">' +
          (p.is_favorite ? '&#9829;' : '&#9825;') +
        '</button>' +
      '</div>' +
      '<div class="prop-card-body">' +
        '<div class="prop-price">' + priceText + '</div>' +
        '<div class="prop-title">' + escapeHtml(cleanTitle(p.title, p) || _fallbackTitle(p)) + '</div>' +
        '<div class="prop-address">' + escapeHtml(cleanAddress(p.address, p.city)) + '</div>' +
        '<div class="prop-details">' + details.join(' &middot; ') + '</div>' +
        '<div class="prop-footer">' +
          (function() {
            var sources = p.all_sources || [{ source: p.source || '', url: p.source_url || '' }];
            var html = '<div class="prop-sources">';
            sources.forEach(function(s) {
              var name = (s.source || '').replace('www.', '').split('.')[0] || 'Source';
              if (s.url) {
                html += '<a href="' + escapeHtml(s.url) + '" target="_blank" rel="noopener" class="prop-source-link" data-src="' + escapeHtml(s.source || '') + '">' + escapeHtml(name) + '</a> ';
              } else {
                html += '<span class="prop-source-link" data-src="' + escapeHtml(s.source || '') + '">' + escapeHtml(name) + '</span> ';
              }
            });
            html += '</div>';
            return html;
          })() +
        '</div>' +
      '</div>' +
    '</div>';
  }

  function scoreBar(label, val) {
    val = val || 0;
    var color = val >= 80 ? '#059669' : val >= 60 ? '#2A6670' : val >= 40 ? '#d97706' : '#dc2626';
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
    // Remember previous state so we can revert if the request fails.
    var wasActive = btn.classList.contains('active');
    var wasHtml = btn.innerHTML;
    apiFetch(API + '/api/favorite/' + propertyId, { method: 'POST' })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
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
      .catch(function (err) {
        _logErr('toggle favorite')(err);
        // Revert optimistic state if any + surface a hint to the user.
        if (wasActive) { btn.classList.add('active'); } else { btn.classList.remove('active'); }
        btn.innerHTML = wasHtml;
        if (typeof showToast === 'function') {
          showToast('Impossible d\'enregistrer le favori — réessayez.');
        }
      });
  }

  // ============================================================
  // FAVORITES VIEW
  // ============================================================
  function loadFavorites() {
    var list = $('favorites-list');
    list.innerHTML = '<div class="dash-loading">Chargement des favoris...</div>';
    var sort = $('fav-sort') ? $('fav-sort').value : 'date';
    apiFetch(API + '/api/favorites?sort=' + sort)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.favorites || data.favorites.length === 0) {
          list.innerHTML = '<div class="dash-empty fav-empty">' +
            '<div class="fav-empty-icon">&#10084;&#65039;</div>' +
            '<div class="fav-empty-title">Aucun favori pour l\'instant</div>' +
            '<div class="fav-empty-text">Cliquez sur le cœur d\'un bien pour le retrouver ici.</div>' +
            '<button class="fav-empty-cta" onclick="document.querySelector(\'.dash-tab[data-view=properties]\').click()">Parcourir les biens</button>' +
          '</div>';
          return;
        }

        // Store for detail view
        window._propData = window._propData || {};
        data.favorites.forEach(function (p) { window._propData[p.id] = p; });

        var html = '<div class="prop-grid">';
        data.favorites.forEach(function (p) {
          html += renderFavoriteCard(p);
        });
        html += '</div>';
        list.innerHTML = html;

        // Hook favorite buttons
        list.querySelectorAll('.fav-btn').forEach(function (btn) {
          btn.onclick = function (e) {
            e.stopPropagation();
            var id = parseInt(this.dataset.id);
            toggleFavorite(id, this);
            // Clean up compare state for removed item
            delete compareSet[id];
            if (Object.keys(compareSet).length >= 2) {
              renderCompare();
            } else {
              $('compare-panel').style.display = 'none';
            }
            // After removing, reload favorites view after short delay
            var self = this;
            setTimeout(function () { loadFavorites(); loadStats(); }, 400);
          };
        });

        // Hook note buttons
        list.querySelectorAll('.fav-note-btn').forEach(function (btn) {
          btn.onclick = function (e) {
            e.stopPropagation();
            var id = parseInt(this.dataset.id);
            var currentNote = this.dataset.note || '';
            showNoteModal(id, currentNote);
          };
        });

        // Hook compare checkboxes
        list.querySelectorAll('.fav-compare-check').forEach(function (cb) {
          cb.onchange = function (e) {
            e.stopPropagation();
            var id = parseInt(this.dataset.id);
            if (this.checked) {
              compareSet[id] = window._propData[id];
            } else {
              delete compareSet[id];
            }
            var count = Object.keys(compareSet).length;
            if (count >= 2) {
              renderCompare();
            } else {
              $('compare-panel').style.display = 'none';
              // Bug #4 fix: when panel closes because count dropped < 2,
              // uncheck the remaining stray checkbox so UI stays in sync
              // with compareSet. Otherwise the last 1 bien stays visually
              // checked while the user can no longer see the panel.
              if (count === 1) {
                document.querySelectorAll('.fav-compare-check').forEach(function (otherCb) {
                  var otherId = parseInt(otherCb.dataset.id);
                  if (!compareSet[otherId]) otherCb.checked = false;
                });
              }
            }
          };
        });
      })
      .catch(function () {
        list.innerHTML = '<div class="dash-empty">Erreur de chargement des favoris</div>';
      });
  }

  function renderFavoriteCard(p) {
    var gradeColors = { A: '#059669', B: '#2A6670', C: '#d97706', D: '#dc2626' };
    var gc = gradeColors[p.grade] || '#7A8398';

    var imgHtml = '';
    if (p.images && p.images.length > 1) {
      var cid = 'fav-carousel-' + p.id;
      imgHtml = '<div class="prop-carousel" id="' + cid + '">';
      for (var ii = 0; ii < p.images.length; ii++) {
        imgHtml += '<img src="' + escapeHtml(p.images[ii]) + '" alt="" class="prop-img' + (ii === 0 ? ' active' : '') + '" data-idx="' + ii + '" onerror="this.style.display=\'none\';_checkAllImgsFailed(this.parentNode)">';
      }
      imgHtml += '<button class="carousel-btn prev" onclick="event.stopPropagation();carouselNav(\'' + cid + '\',-1)">&#8249;</button>';
      imgHtml += '<button class="carousel-btn next" onclick="event.stopPropagation();carouselNav(\'' + cid + '\',1)">&#8250;</button>';
      imgHtml += '</div>';
    } else if (p.images && p.images.length === 1) {
      imgHtml = '<img src="' + escapeHtml(p.images[0]) + '" alt="" class="prop-img active" onerror="this.style.display=\'none\';_checkAllImgsFailed(this.parentNode)">';
    }

    var daysOnline = p.days_online || 0;
    var daysText = daysOnline <= 1 ? 'Nouveau' : daysOnline + 'j';
    var daysColor = daysOnline <= 3 ? '#059669' : daysOnline <= 14 ? '#2A6670' : daysOnline <= 30 ? '#d97706' : '#7A8398';

    var priceText = p.price ? formatPrice(p.price) + ' CHF' : 'Prix sur demande';
    if (p.unit && p.price) {
      var unitPart = (p.unit.split('/')[1] || '').toLowerCase();
      if (unitPart && unitPart !== 'total' && unitPart !== 'one-time') {
        priceText += '<small>/' + escapeHtml(unitPart) + '</small>';
      }
    }
    if (p.price_drop) {
      priceText = '<span class="price-drop-badge">\u2193 ' + Math.round(p.price_drop.change_pct) + '%</span> ' + priceText + '<del class="old-price">' + formatPrice(p.price_drop.old_price) + ' CHF</del>';
    }

    var details = [];
    if (p.rooms && p.rooms > 0 && p.rooms < 20) details.push(p.rooms + ' pcs');
    if (p.surface && p.surface > 0) details.push(p.surface + ' m\u00B2');
    if (p.floor !== null && p.floor !== undefined) details.push(p.floor + 'e etage');

    var noteSnippet = p.fav_note ? '<div class="fav-note-preview">' + escapeHtml(p.fav_note.substring(0, 60)) + (p.fav_note.length > 60 ? '...' : '') + '</div>' : '';
    var favDate = p.fav_date ? new Date(p.fav_date).toLocaleDateString('fr-CH') : '';

    return '<div class="prop-card fav-card" onclick="openPropertyDetail(' + p.id + ')" style="cursor:pointer">' +
      '<div class="prop-card-top">' +
        (imgHtml || '<div class="prop-img-placeholder"></div>') +
        '<div class="prop-score" style="background:' + gc + '">' +
          '<span class="prop-score-num">' + p.score + '</span>' +
          '<span class="prop-score-grade">' + p.grade + '</span>' +
        '</div>' +
        '<div class="prop-days" style="background:' + daysColor + '">' + daysText + '</div>' +
        '<button class="fav-btn active" data-id="' + p.id + '" title="Retirer des favoris">&#9829;</button>' +
        '<input type="checkbox" class="fav-compare-check" data-id="' + p.id + '" title="Selectionner pour comparer" onclick="event.stopPropagation()" style="display:' + (compareMode ? '' : 'none') + '">' +
      '</div>' +
      '<div class="prop-card-body">' +
        '<div class="prop-price">' + priceText + '</div>' +
        '<div class="prop-title">' + escapeHtml(cleanTitle(p.title, p) || _fallbackTitle(p)) + '</div>' +
        '<div class="prop-address">' + escapeHtml(cleanAddress(p.address, p.city)) + '</div>' +
        '<div class="prop-details">' + details.join(' &middot; ') + '</div>' +
        noteSnippet +
        '<div class="fav-card-footer">' +
          '<button class="fav-note-btn" data-id="' + p.id + '" data-note="' + escapeHtml(p.fav_note || '') + '" onclick="event.stopPropagation()" title="Ajouter une note">' +
            (p.fav_note ? '&#9998; Note' : '&#43; Note') +
          '</button>' +
          '<span class="fav-date">' + favDate + '</span>' +
        '</div>' +
      '</div>' +
    '</div>';
  }

  // ============================================================
  // NOTE MODAL
  // ============================================================
  function showNoteModal(propertyId, currentNote) {
    // Remove existing modal
    var old = document.querySelector('.note-modal-overlay');
    if (old) old.remove();

    var overlay = document.createElement('div');
    overlay.className = 'note-modal-overlay';
    overlay.innerHTML =
      '<div class="note-modal">' +
        '<div class="note-modal-head">' +
          '<h3>Note sur ce bien</h3>' +
          '<button class="note-modal-close">&times;</button>' +
        '</div>' +
        '<textarea id="note-textarea" class="note-textarea" placeholder="Vos remarques, questions, points d\'attention..." maxlength="500">' + escapeHtml(currentNote) + '</textarea>' +
        '<div class="note-modal-footer">' +
          '<span class="note-char-count"><span id="note-chars">' + currentNote.length + '</span>/500</span>' +
          '<div class="note-modal-actions">' +
            '<button class="note-cancel-btn">Annuler</button>' +
            '<button class="note-save-btn">Sauvegarder</button>' +
          '</div>' +
        '</div>' +
      '</div>';
    document.body.appendChild(overlay);

    var textarea = overlay.querySelector('#note-textarea');
    textarea.focus();
    textarea.oninput = function () {
      overlay.querySelector('#note-chars').textContent = this.value.length;
    };

    overlay.querySelector('.note-modal-close').onclick = function () { overlay.remove(); };
    overlay.querySelector('.note-cancel-btn').onclick = function () { overlay.remove(); };
    overlay.onclick = function (e) { if (e.target === overlay) overlay.remove(); };

    overlay.querySelector('.note-save-btn').onclick = function () {
      var note = textarea.value.trim();
      var btn = this;
      btn.textContent = '...';
      btn.disabled = true;
      apiFetch(API + '/api/favorite/' + propertyId + '/note', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ note: note })
      })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.ok) {
          overlay.remove();
          loadFavorites();
        } else {
          btn.textContent = 'Erreur';
          btn.disabled = false;
        }
      })
      .catch(function () {
        btn.textContent = 'Erreur';
        btn.disabled = false;
      });
    };
  }

  // ============================================================
  // COMPARE MODE
  // ============================================================
  function renderCompare() {
    var panel = $('compare-panel');
    // Filter out stale/undefined entries (e.g. after a favorite was removed)
    Object.keys(compareSet).forEach(function (id) {
      if (!compareSet[id]) delete compareSet[id];
    });
    var ids = Object.keys(compareSet);
    if (ids.length < 2) {
      panel.style.display = 'none';
      return;
    }

    var props = ids.map(function (id) { return compareSet[id]; });
    var gradeColors = { A: '#059669', B: '#2A6670', C: '#d97706', D: '#dc2626' };

    // Build comparison table
    // Bug #4 fix: close button now also clears compareSet and unchecks all
    // checkboxes so the next open starts from a clean state.
    var html = '<div class="compare-header">' +
      '<h3>&#9878; Comparaison (' + props.length + ' biens)</h3>' +
      '<button class="compare-close-btn" onclick="_closeComparePanel()">&times;</button>' +
    '</div>';

    html += '<div class="compare-scroll"><table class="compare-table"><thead><tr><th>Critere</th>';
    props.forEach(function (p) {
      var gc = gradeColors[p.grade] || '#7A8398';
      html += '<th>' +
        '<div class="compare-th-score" style="background:' + gc + '">' + p.score + ' ' + p.grade + '</div>' +
        '<div class="compare-th-title">' + escapeHtml((cleanTitle(p.title, p) || _fallbackTitle(p)).substring(0, 40)) + '</div>' +
      '</th>';
    });
    html += '</tr></thead><tbody>';

    // Rows
    var rows = [
      { label: 'Prix', key: 'price', fmt: function (v) { return v ? formatPrice(v) + ' CHF' : '-'; } },
      { label: 'Pièces', key: 'rooms', fmt: function (v) { return v || '-'; } },
      { label: 'Surface', key: 'surface', fmt: function (v) { return v ? v + ' m\u00B2' : '-'; } },
      { label: 'Ville', key: 'city', fmt: function (v) { return v || '-'; } },
      { label: 'Etage', key: 'floor', fmt: function (v) { return v !== null && v !== undefined ? v + 'e' : '-'; } },
      { label: 'Score', key: 'score', fmt: function (v) { return v || 0; } },
      { label: 'En ligne', key: 'days_online', fmt: function (v) { return v ? v + ' jours' : '-'; } },
    ];

    rows.forEach(function (row) {
      html += '<tr><td class="compare-label">' + row.label + '</td>';
      var values = props.map(function (p) { return p[row.key]; });
      // Highlight best
      var best = null;
      if (row.key === 'price') best = Math.min.apply(null, values.filter(function (v) { return v; }));
      if (row.key === 'surface' || row.key === 'score') best = Math.max.apply(null, values.filter(function (v) { return v; }));

      props.forEach(function (p) {
        var val = p[row.key];
        var isBest = best !== null && val === best;
        html += '<td' + (isBest ? ' class="compare-best"' : '') + '>' + row.fmt(val) + '</td>';
      });
      html += '</tr>';
    });

    // Score detail rows
    if (props[0].score_detail) {
      var scoreKeys = [
        { label: 'Zone', key: 'zone' },
        { label: 'Budget', key: 'budget' },
        { label: 'Type', key: 'type' },
        { label: 'Surface', key: 'surface' },
        { label: 'Equip.', key: 'equipment' },
      ];
      scoreKeys.forEach(function (sk) {
        html += '<tr><td class="compare-label">' + sk.label + '</td>';
        props.forEach(function (p) {
          var v = p.score_detail ? (p.score_detail[sk.key] || 0) : 0;
          var color = v >= 80 ? '#059669' : v >= 60 ? '#2A6670' : v >= 40 ? '#d97706' : '#dc2626';
          html += '<td><span style="color:' + color + ';font-weight:600">' + v + '</span></td>';
        });
        html += '</tr>';
      });
    }

    // Notes row
    html += '<tr><td class="compare-label">Notes</td>';
    props.forEach(function (p) {
      html += '<td class="compare-note">' + escapeHtml(p.fav_note || '-') + '</td>';
    });
    html += '</tr>';

    html += '</tbody></table></div>';

    panel.innerHTML = html;
    panel.style.display = '';
  }

  // ============================================================
  // CHAT WIDGET
  // ============================================================
  // v6.3.1 Bug #1 helper: open chat widget reliably from any CTA.
  // Initializes the widget on demand if it hasn't been created yet, then
  // explicitly adds `.open` and focuses the input. Previous implementation
  // relied on the CSS toggle class already existing — which failed silently
  // if initChat ran late or was skipped because of a JS error earlier.
  function _openChat() {
    var panel = document.querySelector('.chat-panel');
    if (!panel) {
      try { initChat(); } catch (e) { console.warn('[lou] initChat failed', e); }
      panel = document.querySelector('.chat-panel');
    }
    if (!panel) return;
    panel.classList.add('open');
    setTimeout(function () {
      var input = document.getElementById('chat-in');
      if (input) {
        try { input.focus({ preventScroll: true }); } catch (_) { input.focus(); }
      }
    }, 120);
  }

  function initChat() {
    // Idempotent: if the widget already exists, short-circuit.
    if (document.querySelector('.chat-panel')) return;
    var toggle = ce('button', 'chat-toggle', '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M5 18 L8.5 7 L10 12 L12 3 L14 12 L15.5 7 L19 18 Z" fill="#fff" opacity="0.95"/><circle cx="10" cy="13.5" r="1" fill="#fff" opacity="0.5"/><circle cx="14" cy="13.5" r="1" fill="#fff" opacity="0.5"/></svg>');
    document.body.appendChild(toggle);

    var panel = ce('div', 'chat-panel');
    panel.innerHTML =
      '<div class="chat-head"><span>Lou — Assistant IA</span><button id="chat-close">&times;</button></div>' +
      '<div class="chat-body" id="chat-body">' +
        '<div class="chat-msg bot">Salut ! Je suis Lou, ton chasseur immobilier digital. Dis-moi ce que tu cherches et je me mets en chasse !</div>' +
      '</div>' +
      '<div class="chat-input"><input id="chat-in" type="text" placeholder="Votre message..."><button id="chat-send"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg></button></div>';
    document.body.appendChild(panel);

    toggle.onclick = function () {
      panel.classList.toggle('open');
      if (panel.classList.contains('open')) $('chat-in').focus();
    };

    $('chat-close').onclick = function () { panel.classList.remove('open'); };

    var _chatSending = false;
    function sendMsg() {
      // Bug #0C fix: prevent double-submit (rapid clicks / Enter spam)
      if (_chatSending) return;
      var input = $('chat-in');
      var msg = input.value.trim();
      if (!msg) return;
      _chatSending = true;
      input.value = '';
      var sendBtn = $('chat-send');
      if (sendBtn) sendBtn.disabled = true;

      var body = $('chat-body');

      // Remove old suggestion buttons before adding new message
      var oldSugs = body.querySelectorAll('.chat-suggestions, .chat-unresolved');
      oldSugs.forEach(function (el) { el.remove(); });

      body.insertAdjacentHTML('beforeend', '<div class="chat-msg user">' + escapeHtml(msg) + '</div>');
      body.scrollTop = body.scrollHeight;

      var loading = ce('div', 'chat-msg bot', '...');
      body.appendChild(loading);
      body.scrollTop = body.scrollHeight;

      var chatHeaders = { 'Content-Type': 'application/json' };
      if (TOKEN) chatHeaders['Authorization'] = 'Bearer ' + TOKEN;

      fetch(API + '/api/chat', {
        method: 'POST',
        headers: chatHeaders,
        body: JSON.stringify({ message: msg, session_id: ANON_SESSION })
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          loading.remove();
          var reply = data.message || data.reply || 'Desole, je n\'ai pas compris.';
          body.insertAdjacentHTML('beforeend', '<div class="chat-msg bot">' + escapeHtml(reply) + '</div>');

          // Accumulate criteria from chatbot responses
          if (data.criteria && typeof data.criteria === 'object') {
            Object.keys(data.criteria).forEach(function (k) {
              if (data.criteria[k] !== null && data.criteria[k] !== undefined) {
                chatCriteria[k] = data.criteria[k];
              }
            });
            _persistChatCriteria();
          }

          // Handle profile_ready — prompt signup if not logged in
          if (data.profile_ready && !isJWT(TOKEN)) {
            body.insertAdjacentHTML('beforeend', '<div class="chat-msg bot" style="background:#CEDFE1">' +
              'Super, j\'ai tous tes critères ! ' +
              '<a href="#" class="chat-signup-link" style="color:#2A6670;font-weight:600">Crée ton espace</a> pour que je lance la recherche.' +
            '</div>');
            var signupLink = body.querySelector('.chat-signup-link');
            if (signupLink) signupLink.onclick = function (e) {
              e.preventDefault();
              showAuthModal();
            };
          }

          // Handle profile_ready + logged in — update profile
          if (data.profile_ready && isJWT(TOKEN) && Object.keys(chatCriteria).length > 0) {
            _updateProfileFromChat(chatCriteria);
          }

          // v6.3.2 étape 5 — unresolved zones : un groupe distinct par zone,
          // avec label "Pour « query » :" + boutons cliquables. Chaque click
          // inject la commune corrigée comme turn user (transparent, pas
          // silent patch) via sendMsg(), et POST /api/chat/unresolved-choice
          // pour audit. Rendu AVANT data.suggestions pour éviter doublons
          // (backend duplique data.suggestions = top-3 de la 1ère unresolved).
          var hasUnresolved = data.unresolved_zones && data.unresolved_zones.length;
          if (hasUnresolved) {
            data.unresolved_zones.forEach(function (uz) {
              var groupHtml = '<div class="chat-unresolved" data-log-id="' +
                (uz.log_id != null ? uz.log_id : '') + '" data-query="' +
                escapeHtml(uz.query || '') + '">';
              if (uz.suggestions && uz.suggestions.length) {
                groupHtml += '<div class="chat-unresolved-label">Pour « ' +
                  escapeHtml(uz.query || '') + ' » :</div>';
                groupHtml += '<div class="chat-suggestions">';
                uz.suggestions.forEach(function (s) {
                  groupHtml += '<button class="chat-sug chat-sug-city" data-city="' +
                    escapeHtml(s.city) + '">' + escapeHtml(s.city) + '</button>';
                });
                groupHtml += '</div>';
              }
              // Note : si suggestions=[], le message bot ci-dessus explique
              // déjà à l'user quoi faire (donner NPA ou nom commune).
              // On rend quand même le div (vide) avec data-log-id pour
              // tracer l'abandon éventuel si on l'implémente un jour.
              groupHtml += '</div>';
              body.insertAdjacentHTML('beforeend', groupHtml);
            });
            body.querySelectorAll('.chat-unresolved').forEach(function (grp) {
              var logId = grp.getAttribute('data-log-id');
              grp.querySelectorAll('.chat-sug-city').forEach(function (btn) {
                btn.onclick = function () {
                  var city = btn.getAttribute('data-city');
                  _postUnresolvedChoice(logId, city);
                  $('chat-in').value = city;
                  sendMsg();
                };
              });
            });
          }

          // Show generic suggestions — skip si on a déjà rendu des groupes
          // unresolved (backend renvoie les mêmes noms dans data.suggestions
          // pour fallback mais on veut pas les dupliquer).
          if (!hasUnresolved && data.suggestions && data.suggestions.length) {
            var sugHtml = '<div class="chat-suggestions">';
            data.suggestions.forEach(function (s) {
              sugHtml += '<button class="chat-sug">' + escapeHtml(s) + '</button>';
            });
            sugHtml += '</div>';
            body.insertAdjacentHTML('beforeend', sugHtml);
            body.querySelectorAll('.chat-sug').forEach(function (btn) {
              btn.onclick = function () {
                $('chat-in').value = this.textContent;
                sendMsg();
              };
            });
          }
          body.scrollTop = body.scrollHeight;
          _chatSending = false;
          if (sendBtn) sendBtn.disabled = false;
        })
        .catch(function () {
          loading.remove();
          body.insertAdjacentHTML('beforeend', '<div class="chat-msg bot">Erreur de connexion. Reessayez.</div>');
          body.scrollTop = body.scrollHeight;
          _chatSending = false;
          if (sendBtn) sendBtn.disabled = false;
        });
    }

    // v6.3.2 étape 5 — log silent du choix user face aux suggestions de
    // commune. Fire-and-forget ; un échec réseau ne doit pas bloquer l'UX.
    // Le payload inclut anon_session_id pour les chats pré-signup (même si
    // le backend a déjà cette info — on la repasse pour cohérence/future).
    function _postUnresolvedChoice(logId, chosen) {
      if (!logId) return;
      var headers = { 'Content-Type': 'application/json' };
      if (TOKEN) headers['Authorization'] = 'Bearer ' + TOKEN;
      var payload = { log_id: logId, chosen: chosen };
      if (!TOKEN && ANON_SESSION) payload.anon_session_id = ANON_SESSION;
      try {
        fetch(API + '/api/chat/unresolved-choice', {
          method: 'POST',
          headers: headers,
          body: JSON.stringify(payload)
        }).catch(function () {});
      } catch (_) {}
    }

    // Auto-update the user's profile when chatbot has collected all criteria
    function _updateProfileFromChat(criteria) {
      var payload = {
        property_types: criteria.property_types || [criteria.property_type || 'appartement'],
        transaction: criteria.transaction || 'location',
        budget_max: criteria.budget_max,
        budget_min: criteria.budget_min,
        rooms_min: criteria.rooms_min,
        rooms_max: criteria.rooms_max,
        surface_min: criteria.surface_min,
        priorities: criteria.priorities || [],
        zones: (criteria.zones || []).map(function (z) {
          // v6.3.2 Bug #2: include lat/lng if chat extracted them (chat LLM
          // currently doesn't, but future geo.admin.ch resolution may add them).
          var zone = { city: z.city || '', canton: z.canton || '', radius_km: z.radius_km || 3 };
          if (z.latitude) zone.latitude = z.latitude;
          if (z.longitude) zone.longitude = z.longitude;
          if (z.postal_code) zone.postal_code = z.postal_code;
          return zone;
        })
      };
      // v6.3.2 Bug #1: set lou_first_login BEFORE PUT so that if the user is
      // currently on the dashboard, the next loadProperties() pass renders
      // Case B ("Lou est en chasse 1-3 min") with its progress bar + auto-refresh,
      // instead of falling through to Case D ("Analyse en cours…") during the
      // 5-15s window where the bg rescore thread hasn't committed yet.
      // TTL 60s pour éviter qu'un user fermant l'onglet reste bloqué.
      _setFirstLogin();
      apiFetch(API + '/api/profile', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      }).then(function (r) {
        return r.json().then(function (body) { return { status: r.status, body: body }; });
      })
      .then(function (res) {
        // v6.3.2 Bug #2: backend now returns 400 if any zone failed to resolve.
        if (res.status === 400) {
          var body = document.getElementById('chat-body');
          if (body) {
            var msg = (res.body && res.body.error) ? res.body.error :
              "Je n'ai pas pu enregistrer tes critères — une commune n'a pas été reconnue.";
            body.insertAdjacentHTML('beforeend',
              '<div class="chat-msg bot" style="background:#fef3c7;color:#92400e">' +
              escapeHtml(msg) + ' Peux-tu préciser la commune ?</div>');
            body.scrollTop = body.scrollHeight;
          }
          // Keep criteria locally so the user can retry in one more turn.
          return;
        }
        loadProfileBar();
        // Trigger scoring + scraping so results appear immediately
        apiFetch(API + '/api/score', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
          .then(function () {
            if (typeof loadProperties === 'function') loadProperties(1, 'score', 0);
            if (typeof loadStats === 'function') loadStats();
          })
          .catch(_logErr('chat post-profile score'));
      }).catch(_logErr('chat profile update'));
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
      'body{margin:0;background:#F7F4EE;color:#1E2A44;font-family:Inter,system-ui,sans-serif;-webkit-font-smoothing:antialiased}',

      // Nav
      '.dash-nav{background:#1A4650;padding:14px 32px;display:flex;justify-content:space-between;align-items:center;position:sticky;top:0;z-index:100}',
      '.dash-nav-brand{text-decoration:none;display:flex;align-items:center}',
      '.dash-nav .wordmark{color:#F7F4EE;font-size:22px}',
      '.dash-nav .wordmark__point{color:#CEDFE1}',
      '.dash-nav .wordmark__chevron{stroke:#CEDFE1}',
      '.dash-nav-right{display:flex;align-items:center;gap:16px}',
      '.dash-user-email{color:#7A8398;font-size:13px}',
      '.dash-admin-btn{background:#2A6670;border:none;color:#fff;padding:6px 14px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600;transition:all .2s}',
      '.dash-admin-btn:hover{background:#1A4650}',
      '.dash-logout-btn{background:none;border:1px solid #4A5468;color:#7A8398;padding:6px 14px;border-radius:8px;cursor:pointer;font-size:13px;transition:all .2s}',
      '.dash-logout-btn:hover{border-color:#E4DFD4;color:#E4DFD4}',

      // Admin panel
      '.admin-panel{background:#fff;border-radius:16px;padding:28px;width:90vw;max-width:900px;max-height:80vh;overflow-y:auto;box-shadow:0 20px 60px rgba(0,0,0,.3);position:relative;color:#1E2A44}',
      '.admin-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}',
      '.admin-header h2{margin:0;font-family:Fraunces,Georgia,serif;font-size:22px}',
      '.admin-header .close-btn{position:static;background:none;border:none;font-size:22px;cursor:pointer;color:#7A8398}',
      '.admin-stats{font-size:15px;color:#7A8398;margin-bottom:16px}',
      '.admin-table{width:100%;border-collapse:collapse;font-size:13px}',
      '.admin-table th{text-align:left;padding:10px 8px;border-bottom:2px solid #E4DFD4;color:#7A8398;font-weight:600;font-size:12px;text-transform:uppercase}',
      '.admin-table td{padding:10px 8px;border-bottom:1px solid #EEE9DE}',
      '.admin-table tr:hover td{background:#F7F4EE}',
      '.admin-plan{background:#CEDFE1;color:#2A6670;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600}',

      // Wrap
      '.dash-wrap{max-width:1200px;margin:0 auto;padding:28px 24px}',

      // Header
      '.dash-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;flex-wrap:wrap;gap:16px}',
      '.dash-header h1{font-family:Fraunces,Georgia,serif;font-size:28px;margin:0}',
      '.dash-actions{display:flex;gap:10px}',
      '.dash-select{padding:8px 14px;border:1px solid #E4DFD4;border-radius:8px;font-size:13px;background:#fff;cursor:pointer;color:#4A5468;outline:none}',
      '.dash-select:focus{border-color:#2A6670}',
      '.dash-refresh-btn{padding:8px 16px;background:#059669;color:#fff;border:none;border-radius:8px;font-size:13px;cursor:pointer;font-weight:600;transition:background .2s}',
      '.dash-refresh-btn:hover{background:#047857}',
      '.dash-refresh-btn:disabled{opacity:.6;cursor:wait}',

      // Stats
      '.dash-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:24px}',
      '.dash-stat{background:#fff;border:1px solid #E4DFD4;border-radius:12px;padding:20px;text-align:center;transition:box-shadow .2s}',
      '.dash-stat:hover{box-shadow:0 4px 16px rgba(0,0,0,.06)}',
      '.dash-stat-num{font-size:32px;font-weight:700;font-family:Fraunces,Georgia,serif;color:#1E2A44}',
      '.dash-stat-lbl{font-size:13px;color:#7A8398;margin-top:4px}',
      '.dash-stat.clickable:hover{border-color:#2A6670;box-shadow:0 4px 16px rgba(42,102,112,.12)}',
      '.dash-stat.stat-active{border-color:#2A6670;background:#F2EEE5;box-shadow:0 4px 16px rgba(42,102,112,.15)}',
      '.dash-stat.stat-active .dash-stat-num{color:#2A6670}',
      '.dash-stat.stat-active .dash-stat-lbl{color:#2A6670;font-weight:600}',
      '.new-filter-banner{display:flex;align-items:center;justify-content:space-between;background:#F2EEE5;border:1px solid #CEDFE1;border-radius:10px;padding:12px 18px;margin-bottom:16px;font-size:14px;color:#2A6670;font-weight:600}',
      '.nearby-banner{display:flex;align-items:center;justify-content:space-between;gap:12px;background:#fffbeb;border:1px solid #fde68a;border-radius:10px;padding:12px 18px;margin-bottom:16px;font-size:14px;color:#78350f;flex-wrap:wrap}',
      '.nearby-banner-active{background:#F2EEE5;border-color:#CEDFE1;color:#2A6670}',
      '.nearby-expand{padding:7px 14px;background:#2A6670;color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;transition:background .15s}',
      '.nearby-expand:hover{background:#1A4650}',
      '.lou-toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(20px);background:#1E2A44;color:#fff;padding:12px 20px;border-radius:10px;font-size:14px;font-weight:500;box-shadow:0 10px 25px rgba(0,0,0,.25);opacity:0;transition:opacity .25s ease,transform .25s ease;z-index:9999;max-width:90vw}',
      '.lou-toast-show{opacity:1;transform:translateX(-50%) translateY(0)}',
      '.lou-toast-ok{background:#059669}',
      '.new-filter-clear{background:none;border:1px solid #2A6670;color:#2A6670;border-radius:6px;padding:4px 12px;cursor:pointer;font-size:13px;font-weight:500;transition:all .2s}',
      '.new-filter-clear:hover{background:#2A6670;color:#fff}',

      // Profile bar
      '.dash-profile-bar{margin-bottom:24px}',
      '.dash-profile-tags{display:flex;flex-wrap:wrap;gap:8px}',
      '.ptag{padding:6px 14px;background:#EEE9DE;border-radius:50px;font-size:13px;color:#7A8398}',
      '.ptag.blue{background:rgba(42,102,112,.1);color:#2A6670}',
      '.ptag-removable{padding-right:6px;display:inline-flex;align-items:center;gap:4px}',
      '.ptag-x{background:none;border:none;color:inherit;cursor:pointer;font-size:15px;line-height:1;padding:0 2px;opacity:.5;transition:opacity .15s}',
      '.ptag-x:hover{opacity:1}',
      '.read-more-link{color:#2A6670;font-weight:600;text-decoration:none;white-space:nowrap}',
      '.read-more-link:hover{text-decoration:underline}',
      '.dash-profile-empty{background:#fff;border:1px dashed #E4DFD4;border-radius:12px;padding:20px;text-align:center;color:#7A8398;font-size:14px}',
      '.dash-profile-empty a{color:#2A6670;cursor:pointer}',
      '.dash-profile-row{display:flex;align-items:center;justify-content:space-between;gap:12px}',
      '.dash-edit-btn{padding:8px 18px;background:#2A6670;color:#fff;border:none;border-radius:8px;font-size:13px;cursor:pointer;white-space:nowrap;transition:background .2s}',
      '.dash-edit-btn:hover{background:#1A4650}',
      '.profile-form{background:#fff;border:1px solid #E4DFD4;border-radius:16px;padding:24px 24px 0;margin-top:12px;max-height:75vh;overflow-y:auto}',
      '.pf-row{display:flex;gap:20px;margin-bottom:0}',
      '.pf-flex1{flex:1;min-width:0}',
      '.pf-section{margin-bottom:18px}',
      '.pf-section-title{font-size:14px;font-weight:700;margin-bottom:10px;color:#1E2A44}',
      '.pf-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}',
      '.pf-budget-grid{display:grid;grid-template-columns:150px 1fr 1fr;gap:14px;align-items:end}',
      '.pf-field{display:flex;flex-direction:column;gap:6px}',
      '.pf-field label{font-size:12px;color:#7A8398;font-weight:600}',
      '.pf-field input,.pf-field select{padding:9px 12px;border:1px solid #E4DFD4;border-radius:8px;font-size:14px;font-family:inherit}',
      '.pf-field input:focus,.pf-field select:focus{border-color:#2A6670;outline:none}',
      '.pf-range{display:flex;align-items:center;gap:10px}',
      '.pf-range input[type=range]{flex:1;accent-color:#2A6670;cursor:pointer}',
      '.pf-range span{font-size:13px;font-weight:600;color:#2A6670;min-width:90px;text-align:right;white-space:nowrap}',
      '.pf-chips{display:flex;flex-wrap:wrap;gap:8px}',
      '.pf-chip{padding:7px 14px;border-radius:20px;font-size:13px;cursor:pointer;border:1px solid #E4DFD4;background:#F7F4EE;color:#7A8398;transition:all .15s;user-select:none}',
      '.pf-chip:hover{border-color:#2A6670;color:#1E2A44}',
      '.pf-chip.on{background:#2A6670;border-color:#2A6670;color:#fff}',
      '.pf-zone-list{display:flex;flex-direction:column;gap:6px;margin-bottom:10px}',
      '.pf-zone{display:flex;align-items:center;gap:10px;background:#F7F4EE;border:1px solid #E4DFD4;border-radius:8px;padding:8px 14px;font-size:14px}',
      '.pf-zone span:first-child{flex:1;font-weight:500}',
      '.pf-zone-add{display:flex;gap:8px;align-items:center}',
      '.pf-zone-add input{flex:1;padding:9px 12px;border:1px solid #E4DFD4;border-radius:8px;font-size:14px;font-family:inherit}',
      '.pf-zone-add input:focus{border-color:#2A6670;outline:none}',
      '.pf-zone-add select{padding:9px 8px;border:1px solid #E4DFD4;border-radius:8px;font-size:13px}',
      '.pf-add-btn{padding:8px 14px;background:#2A6670;color:#fff;border:none;border-radius:8px;font-size:16px;cursor:pointer;font-weight:700}',
      '.pf-actions{display:flex;gap:10px;justify-content:flex-end;margin-top:8px;padding:16px 24px;border-top:1px solid #E4DFD4;position:sticky;bottom:0;background:#fff;border-radius:0 0 16px 16px;z-index:2}',
      '.pf-save-btn{padding:11px 24px;background:#2A6670;color:#fff;border:none;border-radius:10px;font-size:14px;cursor:pointer;font-weight:600;transition:all .2s;display:inline-flex;align-items:center;gap:8px}',
      '.pf-save-btn:hover:not(:disabled){background:#1A4650;transform:translateY(-1px);box-shadow:0 4px 12px rgba(42,102,112,.3)}',
      '.pf-save-btn:disabled{cursor:wait;opacity:.85}',
      '.pf-cancel-btn{padding:11px 24px;background:#EEE9DE;color:#7A8398;border:none;border-radius:10px;font-size:14px;cursor:pointer}',
      // Spinner shown in the save button during PUT/POST/refresh
      '.pf-spinner{display:inline-block;width:14px;height:14px;border:2px solid rgba(255,255,255,.35);border-top-color:#fff;border-radius:50%;animation:pf-spin .7s linear infinite;flex-shrink:0}',
      '@keyframes pf-spin{to{transform:rotate(360deg)}}',
      // Loading state: gray out the whole form + disable interactions
      '.pf-loading .pf-chip,.pf-loading input,.pf-loading select,.pf-loading button:not(#pf-save){pointer-events:none;opacity:.55}',
      '.pf-loading{position:relative}',
      '.pf-hint{font-size:12px;color:#7A8398;margin-right:auto;align-self:center;font-style:italic}',
      '@media(max-width:768px){.pf-grid{grid-template-columns:1fr}.pf-budget-grid{grid-template-columns:1fr}.pf-row{flex-direction:column}.dash-profile-row{flex-direction:column;align-items:stretch}.pf-zone-add{flex-wrap:wrap}}',

      // Properties grid
      '.prop-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:20px}',
      '.prop-card{background:#fff;border:1px solid #E4DFD4;border-radius:14px;overflow:hidden;transition:all .2s}',
      '.prop-card:hover{box-shadow:0 8px 24px rgba(0,0,0,.08);border-color:#E4DFD4;transform:translateY(-2px)}',
      '.prop-card-top{position:relative;height:280px;background:#E4DFD4;overflow:hidden}',
      '.prop-carousel{position:relative;width:100%;height:100%}',
      '.prop-img{width:100%;height:100%;object-fit:cover;display:none}',
      '.prop-img.active{display:block}',
      '.carousel-btn{position:absolute;top:50%;transform:translateY(-50%);background:rgba(0,0,0,.45);color:#fff;border:none;font-size:22px;width:30px;height:30px;border-radius:50%;cursor:pointer;display:flex;align-items:center;justify-content:center;z-index:2;line-height:1}',
      '.carousel-btn.prev{left:6px}',
      '.carousel-btn.next{right:6px}',
      '.carousel-btn:hover{background:rgba(0,0,0,.7)}',
      '.carousel-dots{position:absolute;bottom:6px;left:50%;transform:translateX(-50%);display:flex;gap:4px;z-index:2}',
      '.carousel-dot{width:6px;height:6px;border-radius:50%;background:rgba(255,255,255,.5)}',
      '.carousel-dot.active{background:#fff}',
      '.prop-img-placeholder{width:100%;height:100%;background:linear-gradient(135deg,#EEE9DE,#E4DFD4);display:flex;align-items:center;justify-content:center;color:#7A8398;font-size:13px}',
      '.prop-img-placeholder::after{content:"Pas d\'image";opacity:.6}',
      '.prop-score{position:absolute;top:12px;left:12px;display:flex;align-items:center;gap:4px;padding:6px 12px;border-radius:8px;color:#fff;font-weight:700;cursor:pointer}',
      '.prop-score-num{font-size:18px}',
      '.prop-score-grade{font-size:12px;opacity:.9}',
      '.fav-btn{position:absolute;top:12px;right:12px;background:rgba(255,255,255,.9);border:none;width:36px;height:36px;border-radius:50%;font-size:18px;cursor:pointer;color:#7A8398;display:flex;align-items:center;justify-content:center;transition:all .2s}',
      '.fav-btn:hover,.fav-btn.active{color:#dc2626;background:#fff}',
      '.prop-days{position:absolute;top:12px;right:52px;padding:3px 8px;border-radius:8px;font-size:11px;font-weight:700;color:#fff;z-index:2}',
      '.price-drop-badge{background:#059669;color:#fff;font-size:12px;font-weight:700;padding:2px 8px;border-radius:6px;margin-right:6px}',
      '.old-price{color:#7A8398;font-size:14px;font-weight:400;margin-left:8px}',
      '.score-tooltip{background:#fff;border:1px solid #E4DFD4;border-radius:12px;padding:14px 16px;box-shadow:0 8px 32px rgba(0,0,0,.18);min-width:200px;max-width:260px;font-size:13px}',
      '.st-row{display:flex;align-items:center;padding:4px 0;color:#4A5468}',
      '.st-row span{min-width:65px;font-size:12px}',
      '.st-row strong{color:#2A6670;font-size:13px;min-width:24px;text-align:right}',

      // Card body
      '.prop-card-body{padding:16px}',
      '.prop-price{font-size:20px;font-weight:700;color:#1E2A44;margin-bottom:4px}',
      '.prop-price small{font-size:13px;color:#7A8398;font-weight:400;margin-left:2px}',
      '.prop-title{font-size:14px;color:#4A5468;margin-bottom:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
      '.prop-address{font-size:13px;color:#7A8398;margin-bottom:8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
      '.prop-details{font-size:13px;color:#7A8398;margin-bottom:12px}',

      // Score mini bars
      '.prop-scores-mini{margin-bottom:12px}',
      '.score-mini-row{display:flex;align-items:center;gap:8px;margin-bottom:3px}',
      '.score-mini-lbl{font-size:11px;color:#7A8398;width:42px;text-align:right;flex-shrink:0}',
      '.score-mini-bar{flex:1;height:5px;background:#EEE9DE;border-radius:3px;overflow:hidden}',
      '.score-mini-fill{height:100%;border-radius:3px;transition:width .5s}',
      '.score-mini-val{font-size:11px;color:#7A8398;width:22px;text-align:right}',

      // Card footer
      '.prop-footer{display:flex;justify-content:space-between;align-items:center;padding-top:12px;border-top:1px solid #EEE9DE}',
      '.prop-source{font-size:12px;color:#7A8398;text-transform:capitalize}',
      '.prop-sources{display:flex;flex-wrap:wrap;gap:6px;align-items:center}',
      '.prop-source-link{font-size:11px;color:#fff;text-decoration:none;font-weight:600;padding:3px 8px;border-radius:12px;text-transform:capitalize;white-space:nowrap}',
      '.prop-source-link:hover{opacity:0.85;filter:brightness(1.1)}',
      '.prop-source-link[data-src="Homegate"]{background:#e74c3c}',
      '.prop-source-link[data-src="ImmoScout24"]{background:#1a73e8}',
      '.prop-source-link[data-src="Immobilier.ch"]{background:#2ecc71}',
      '.prop-source-link[data-src="Comparis"]{background:#f39c12}',
      '.prop-source-link[data-src="Flatfox"]{background:#9b59b6}',
      '.prop-source-link[data-src="Anibis"]{background:#e67e22}',
      '.prop-source-link[data-src="Acheter-Louer"]{background:#1abc9c}',
      '.prop-source-link[data-src="Properstar"]{background:#34495e}',
      '.prop-link{font-size:13px;color:#2A6670;text-decoration:none;font-weight:500}',
      '.prop-link:hover{text-decoration:underline}',

      // Loading / Empty
      '.dash-loading{text-align:center;padding:48px;color:#7A8398;font-size:15px}',
      '.dash-empty{text-align:center;padding:48px;color:#7A8398;font-size:15px;background:#fff;border:1px dashed #E4DFD4;border-radius:12px}',
      '.fav-empty{padding:64px 32px;background:linear-gradient(135deg,#F7F4EE 0%,#fff 100%);border:1px solid #E4DFD4}',
      '.fav-empty-icon{font-size:56px;margin-bottom:20px;display:block;filter:grayscale(.3)}',
      '.fav-empty-title{font-family:Fraunces,Georgia,serif;font-size:22px;font-weight:700;color:#1E2A44;margin-bottom:8px}',
      '.fav-empty-text{font-size:15px;color:#7A8398;margin-bottom:24px;max-width:380px;margin-left:auto;margin-right:auto;line-height:1.5}',
      '.fav-empty-cta{background:#2A6670;color:#fff;border:none;padding:12px 28px;border-radius:10px;font-size:14px;font-weight:600;cursor:pointer;transition:all .2s}',
      '.fav-empty-cta:hover{background:#1A4650;transform:translateY(-1px);box-shadow:0 4px 16px rgba(42,102,112,.3)}',

      // Pagination
      '.dash-pagination{display:flex;align-items:center;justify-content:center;gap:8px;margin-top:28px;padding-bottom:32px;flex-wrap:wrap}',
      '.pag-info{font-size:13px;color:#7A8398;margin-right:12px}',
      '.pag-btn{padding:8px 14px;border:1px solid #E4DFD4;background:#fff;border-radius:8px;font-size:13px;cursor:pointer;color:#4A5468;transition:all .2s}',
      '.pag-btn:hover{border-color:#2A6670;color:#2A6670}',
      '.pag-btn.active{background:#2A6670;color:#fff;border-color:#2A6670}',

      // Chat
      '.chat-toggle{position:fixed;bottom:24px;right:24px;width:56px;height:56px;border-radius:50%;background:#2A6670;color:#fff;border:none;font-size:24px;cursor:pointer;box-shadow:0 4px 20px rgba(42,102,112,.4);z-index:1000;display:flex;align-items:center;justify-content:center;transition:transform .2s}',
      '.chat-toggle:hover{transform:scale(1.1)}',
      '.chat-panel{position:fixed;bottom:90px;right:24px;width:380px;max-width:calc(100vw - 48px);height:500px;background:#fff;border-radius:16px;box-shadow:0 8px 40px rgba(0,0,0,.15);z-index:1000;display:none;flex-direction:column;overflow:hidden}',
      '.chat-panel.open{display:flex}',
      '.chat-head{background:#1A4650;color:#F7F4EE;padding:14px 18px;font-weight:600;display:flex;justify-content:space-between;align-items:center;font-size:15px}',
      '.chat-head button{background:none;border:none;color:#fff;font-size:22px;cursor:pointer;opacity:.7}',
      '.chat-head button:hover{opacity:1}',
      '.chat-body{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px}',
      '.chat-msg{max-width:85%;padding:10px 14px;border-radius:12px;font-size:14px;line-height:1.6}',
      '.chat-msg.bot{background:#EEE9DE;color:#1E2A44;align-self:flex-start;border-bottom-left-radius:4px}',
      '.chat-msg.user{background:#2A6670;color:#fff;align-self:flex-end;border-bottom-right-radius:4px}',
      '.chat-input{display:flex;border-top:1px solid #E4DFD4;padding:12px}',
      '.chat-input input{flex:1;border:1px solid #E4DFD4;border-radius:8px;padding:10px 14px;font-size:14px;outline:none;font-family:Inter,sans-serif}',
      '.chat-input input:focus{border-color:#2A6670}',
      '.chat-input button{margin-left:8px;background:#2A6670;color:#fff;border:none;border-radius:8px;padding:10px 16px;cursor:pointer;font-size:16px;transition:background .2s}',
      '.chat-input button:hover{background:#1A4650}',
      '.chat-suggestions{display:flex;flex-wrap:wrap;gap:6px;margin-top:4px}',
      '.chat-sug{padding:6px 12px;background:#CEDFE1;border:none;border-radius:20px;font-size:12px;color:#2A6670;cursor:pointer;transition:background .2s}',
      '.chat-sug:hover{background:#CEDFE1}',
      '.chat-unresolved{display:flex;flex-direction:column;gap:4px;margin-top:4px;align-self:flex-start;max-width:85%}',
      '.chat-unresolved-label{font-size:12px;color:#7A8398;font-style:italic}',

      // Property detail overlay
      '.detail-overlay{position:fixed;inset:0;background:rgba(15,23,42,.6);z-index:2000;display:flex;justify-content:center;overflow-y:auto;padding:24px;backdrop-filter:blur(4px)}',
      '.detail-panel{background:#fff;border-radius:16px;width:100%;max-width:720px;overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,.2);margin:auto;position:relative;animation:detailIn .25s ease}',
      '@keyframes detailIn{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:translateY(0)}}',
      '@keyframes progressBar{from{width:0}to{width:100%}}',
      '.detail-close{position:absolute;top:16px;right:16px;width:36px;height:36px;border-radius:50%;background:rgba(0,0,0,.5);color:#fff;border:none;font-size:18px;cursor:pointer;z-index:10;display:flex;align-items:center;justify-content:center;transition:background .2s}',
      '.detail-close:hover{background:rgba(0,0,0,.7)}',
      '.detail-gallery{position:relative;width:100%;height:360px;background:#E4DFD4;overflow:hidden}',
      '.detail-img{width:100%;height:100%;object-fit:cover;display:none}',
      '.detail-img.active{display:block}',
      '.detail-gallery .carousel-btn{width:40px;height:40px;font-size:28px}',
      '.detail-counter{position:absolute;bottom:12px;right:16px;background:rgba(0,0,0,.55);color:#fff;padding:4px 12px;border-radius:20px;font-size:12px;font-weight:600}',
      '.detail-gallery-empty{width:100%;height:200px;background:#EEE9DE;display:flex;align-items:center;justify-content:center;color:#7A8398;font-size:15px}',
      '.detail-body{padding:28px}',
      '.detail-price{font-size:28px;font-weight:800;color:#1E2A44;margin-bottom:6px;font-family:Fraunces,Georgia,serif}',
      '.detail-title{font-size:18px;font-weight:600;color:#4A5468;margin-bottom:4px}',
      '.detail-address{font-size:14px;color:#7A8398;margin-bottom:24px}',
      '.detail-section{margin-bottom:24px}',
      '.detail-section h3{font-size:15px;font-weight:700;color:#1E2A44;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid #EEE9DE}',
      '.detail-table{display:grid;grid-template-columns:1fr 1fr;column-gap:24px;row-gap:4px}',
      '.detail-row{display:flex;justify-content:space-between;align-items:center;padding:10px 12px;background:#F7F4EE;border-radius:8px;font-size:14px;gap:12px}',
      '.detail-row span{color:#7A8398}',
      '.detail-row strong{color:#1E2A44;text-align:right}',
      '.detail-score-wrap{display:flex;gap:20px;align-items:flex-start;margin-bottom:24px;padding:20px;background:#F7F4EE;border-radius:12px}',
      '.detail-score-badge{width:64px;height:64px;border-radius:14px;display:flex;flex-direction:column;align-items:center;justify-content:center;flex-shrink:0}',
      '.dsb-num{font-size:22px;font-weight:800;color:#fff;line-height:1}',
      '.dsb-grade{font-size:13px;font-weight:700;color:rgba(255,255,255,.8)}',
      '.detail-score-bars{flex:1;display:flex;flex-direction:column;gap:6px}',
      '.dsb-row{display:flex;align-items:center;gap:10px;font-size:13px;cursor:help}',
      '.dsb-row span{min-width:90px;color:#7A8398;display:flex;align-items:center;gap:4px}',
      '.dsb-weight{font-size:11px;font-weight:600;color:#7A8398;letter-spacing:.2px}',
      '.dsb-track{flex:1;height:6px;background:#E4DFD4;border-radius:3px;overflow:hidden}',
      '.dsb-fill{height:100%;border-radius:3px;transition:width .3s}',
      '.dsb-row strong{min-width:24px;text-align:right;font-size:13px;color:#4A5468}',
      '.detail-contact{display:flex;flex-direction:column;gap:8px;font-size:14px}',
      '.detail-contact a{color:#2A6670;text-decoration:none}',
      '.detail-contact a:hover{text-decoration:underline}',
      '.detail-features{display:flex;flex-wrap:wrap;gap:8px}',
      '.detail-description{font-size:14px;color:#4A5468;line-height:1.6;margin:0;white-space:pre-line}',
      '.detail-feat{padding:6px 14px;background:#F2EEE5;border:1px solid #CEDFE1;border-radius:20px;font-size:13px;color:#2A6670}',
      '.detail-sources{display:flex;flex-wrap:wrap;gap:10px}',
      '.detail-source-link{padding:10px 20px;background:#2A6670;color:#fff;border-radius:10px;font-size:14px;font-weight:600;text-decoration:none;text-transform:capitalize;transition:background .2s}',
      '.detail-source-link:hover{background:#1A4650}',
      '.detail-source-text{padding:10px 20px;background:#EEE9DE;border-radius:10px;font-size:14px;color:#7A8398;text-transform:capitalize}',
      '@media(max-width:768px){.detail-overlay{padding:0}.detail-panel{border-radius:0;max-width:100%;min-height:100vh}.detail-gallery{height:260px}.detail-body{padding:20px}.detail-price{font-size:22px}.detail-table{grid-template-columns:1fr}.detail-score-wrap{flex-direction:column}}',

      // Score legend
      '.score-legend{margin-bottom:16px}',
      '.score-legend-toggle{background:none;border:none;color:#2A6670;font-size:13px;font-weight:600;cursor:pointer;padding:6px 0;display:flex;align-items:center;gap:4px}',
      '.score-legend-toggle:hover{color:#1A4650;text-decoration:underline}',
      '.score-legend-body{background:#F7F4EE;border:1px solid #E4DFD4;border-radius:12px;padding:16px 20px;margin-top:8px}',
      '.score-legend-intro{font-size:13px;color:#4A5468;margin:0 0 12px}',
      '.score-legend-grades{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap}',
      '.score-legend-badge{color:#fff;font-weight:700;font-size:14px;padding:4px 14px;border-radius:8px;display:inline-flex;align-items:center;gap:6px}',
      '.score-legend-badge small{font-weight:400;opacity:.85;font-size:11px}',
      '.score-legend-criteria{display:grid;grid-template-columns:1fr 1fr;gap:6px 20px}',
      '.score-legend-item{font-size:12px;color:#4A5468;line-height:1.5}',
      '.score-legend-item strong{color:#1E2A44}',
      '.score-legend-tip{font-size:12px;color:#7A8398;margin:12px 0 0;font-style:italic}',

      // View tabs
      '.dash-tabs{display:flex;gap:0;margin-bottom:20px;border-bottom:2px solid #E4DFD4}',
      '.dash-tab{padding:10px 20px;border:none;background:none;font-size:14px;font-weight:600;cursor:pointer;color:#7A8398;border-bottom:2px solid transparent;margin-bottom:-2px;transition:all .2s}',
      '.dash-tab:hover{color:#2A6670}',
      '.dash-tab.active{color:#2A6670;border-bottom-color:#2A6670}',

      // Favorites toolbar
      '.fav-toolbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;gap:12px;flex-wrap:wrap}',
      '.fav-toolbar-left{display:flex;gap:10px;align-items:center}',
      '.fav-toolbar-right{display:flex;gap:10px;align-items:center}',
      '.fav-action-btn{padding:8px 16px;border:1px solid #E4DFD4;background:#fff;border-radius:8px;font-size:13px;cursor:pointer;color:#4A5468;font-weight:500;transition:all .2s}',
      '.fav-action-btn:hover{border-color:#2A6670;color:#2A6670}',
      '.fav-action-btn.active{background:#2A6670;color:#fff;border-color:#2A6670}',

      // Favorite card extras
      '.fav-note-preview{font-size:12px;color:#7A8398;background:#F7F4EE;padding:6px 10px;border-radius:6px;margin-bottom:8px;border-left:3px solid #2A6670;font-style:italic}',
      '.fav-card-footer{display:flex;justify-content:space-between;align-items:center;padding-top:10px;border-top:1px solid #EEE9DE}',
      '.fav-note-btn{background:none;border:1px solid #E4DFD4;padding:5px 12px;border-radius:6px;font-size:12px;color:#7A8398;cursor:pointer;transition:all .2s}',
      '.fav-note-btn:hover{border-color:#2A6670;color:#2A6670}',
      '.fav-date{font-size:11px;color:#7A8398}',
      '.fav-compare-check{position:absolute;top:12px;left:52px;width:20px;height:20px;accent-color:#2A6670;cursor:pointer;z-index:3}',
      '.dash-stat.clickable:hover{border-color:#2A6670;box-shadow:0 4px 16px rgba(42,102,112,.1)}',

      // Note modal
      '.note-modal-overlay{position:fixed;inset:0;background:rgba(15,23,42,.6);z-index:3000;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px)}',
      '.note-modal{background:#fff;border-radius:16px;padding:24px;width:440px;max-width:90vw;box-shadow:0 20px 60px rgba(0,0,0,.3);color:#1E2A44}',
      '.note-modal-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}',
      '.note-modal-head h3{margin:0;font-size:18px;font-weight:700}',
      '.note-modal-close{background:none;border:none;font-size:24px;cursor:pointer;color:#7A8398}',
      '.note-textarea{width:100%;height:120px;border:1px solid #E4DFD4;border-radius:10px;padding:12px;font-size:14px;font-family:Inter,sans-serif;resize:vertical;outline:none;box-sizing:border-box}',
      '.note-textarea:focus{border-color:#2A6670;box-shadow:0 0 0 3px rgba(42,102,112,.1)}',
      '.note-modal-footer{display:flex;justify-content:space-between;align-items:center;margin-top:12px}',
      '.note-char-count{font-size:12px;color:#7A8398}',
      '.note-modal-actions{display:flex;gap:8px}',
      '.note-cancel-btn{padding:8px 16px;background:#EEE9DE;border:none;border-radius:8px;font-size:13px;cursor:pointer;color:#7A8398}',
      '.note-save-btn{padding:8px 16px;background:#2A6670;color:#fff;border:none;border-radius:8px;font-size:13px;cursor:pointer;font-weight:600;transition:background .2s}',
      '.note-save-btn:hover{background:#1A4650}',

      // Compare panel
      '.compare-panel{background:#fff;border:1px solid #E4DFD4;border-radius:16px;padding:24px;margin-bottom:24px;box-shadow:0 4px 16px rgba(0,0,0,.06)}',
      '.compare-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}',
      '.compare-header h3{margin:0;font-size:18px;font-weight:700}',
      '.compare-close-btn{background:none;border:none;font-size:22px;cursor:pointer;color:#7A8398}',
      '.compare-scroll{overflow-x:auto}',
      '.compare-table{width:100%;border-collapse:collapse;font-size:13px}',
      '.compare-table th{padding:12px 10px;text-align:center;border-bottom:2px solid #E4DFD4;min-width:140px}',
      '.compare-th-score{display:inline-block;padding:4px 12px;border-radius:8px;color:#fff;font-weight:700;font-size:14px;margin-bottom:4px}',
      '.compare-th-title{font-size:12px;color:#7A8398;font-weight:400;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:160px}',
      '.compare-table td{padding:10px;text-align:center;border-bottom:1px solid #EEE9DE}',
      '.compare-label{text-align:left!important;color:#7A8398;font-weight:600}',
      '.compare-best{background:#f0fdf4;color:#059669;font-weight:700}',
      '.compare-note{font-size:12px;color:#7A8398;text-align:left!important;max-width:180px}',

      // Auth overlay (for landing page)
      '.lou-overlay{position:fixed;inset:0;background:rgba(15,23,42,.7);display:flex;align-items:center;justify-content:center;z-index:9999;backdrop-filter:blur(4px)}',
      '.lou-auth-box{background:#fff;border-radius:16px;padding:36px;width:400px;max-width:90vw;box-shadow:0 20px 60px rgba(0,0,0,.3);position:relative;color:#1E2A44}',
      '.lou-auth-box .close-btn{position:absolute;top:12px;right:16px;background:none;border:none;font-size:22px;cursor:pointer;color:#7A8398}',
      '.lou-auth-box h2{font-size:24px;margin:0 0 4px;font-family:Fraunces,Georgia,serif}',
      '.lou-auth-box .sub{font-size:14px;color:#7A8398;margin-bottom:20px}',
      '.lou-auth-box input{width:100%;padding:12px 14px;border:1px solid #E4DFD4;border-radius:10px;margin-bottom:12px;font-size:14px;box-sizing:border-box;outline:none}',
      '.lou-auth-box input:focus{border-color:#2A6670;box-shadow:0 0 0 3px rgba(42,102,112,.1)}',
      '.auth-submit{width:100%;padding:13px;border:none;border-radius:10px;background:#2A6670;color:#fff;font-size:15px;font-weight:600;cursor:pointer}',
      '.auth-submit:hover{background:#1A4650}',
      '.lou-auth-switch{text-align:center;margin-top:14px;font-size:13px;color:#7A8398}',
      '.lou-auth-switch a{color:#2A6670;cursor:pointer;text-decoration:underline}',
      '.lou-auth-err{color:#dc2626;font-size:13px;margin-top:8px;display:none;text-align:center}',

      // Map view
      '.map-view{height:calc(100vh - 280px);min-height:500px;border-radius:12px;overflow:hidden;border:1px solid #E4DFD4}',
      '.map-split{display:flex;height:100%;width:100%}',
      '.map-sidebar{width:380px;min-width:320px;height:100%;overflow-y:auto;background:#F7F4EE;border-right:1px solid #E4DFD4}',
      '.map-sidebar-header{padding:12px 16px;border-bottom:1px solid #E4DFD4;font-size:13px;font-weight:600;color:#7A8398;background:#fff;position:sticky;top:0;z-index:1}',
      '.map-load-more-btn{display:block;width:calc(100% - 24px);margin:12px;padding:12px;background:#fff;border:1px solid #2A6670;color:#2A6670;border-radius:10px;font-size:14px;font-weight:600;cursor:pointer;transition:all .2s}',
      '.map-load-more-btn:hover{background:#2A6670;color:#fff;transform:translateY(-1px);box-shadow:0 4px 12px rgba(42,102,112,.2)}',
      '.map-sidebar-list{padding:8px}',
      '.map-card{display:flex;gap:10px;padding:10px;margin-bottom:6px;background:#fff;border-radius:10px;border:1px solid #E4DFD4;cursor:pointer;transition:all .15s;position:relative}',
      '.map-card:hover{border-color:#2A6670;box-shadow:0 2px 8px rgba(42,102,112,.1)}',
      '.map-card-active{border-color:#2A6670;box-shadow:0 0 0 2px rgba(42,102,112,.2)}',
      '.map-card-img{width:80px;height:70px;object-fit:cover;border-radius:8px;flex-shrink:0}',
      '.map-card-img-ph{width:80px;height:70px;background:#E4DFD4;border-radius:8px;flex-shrink:0}',
      '.map-card-info{flex:1;min-width:0;display:flex;flex-direction:column;gap:2px}',
      '.map-card-price{font-size:15px;font-weight:700;color:#1E2A44}',
      '.map-card-title{font-size:12px;color:#4A5468;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
      '.map-card-details{font-size:11px;color:#7A8398}',
      '.map-card-addr{font-size:11px;color:#7A8398;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
      '.map-card-score{position:absolute;top:8px;right:8px;color:#fff;font-size:11px;font-weight:700;padding:2px 6px;border-radius:6px}',
      '.map-canvas{flex:1;min-width:0;height:100%}',
      '.map-price-marker{color:#fff;font-size:12px;font-weight:700;padding:4px 10px;border-radius:20px;white-space:nowrap;box-shadow:0 2px 6px rgba(0,0,0,.25);border:2px solid #fff}',
      '.map-marker-custom{background:transparent;border:none}',

      // Responsive tablet
      '@media(max-width:768px){',
        '.dash-stats{grid-template-columns:repeat(2,1fr)}',
        '.dash-wrap{padding:16px}',
        '.prop-grid{grid-template-columns:1fr}',
        '.dash-header{flex-direction:column;align-items:flex-start}',
        '.dash-nav{padding:12px 16px;flex-wrap:wrap;gap:8px}',
        '.dash-nav .wordmark{font-size:18px}',
        '.dash-nav-right{gap:8px;flex-wrap:wrap}',
        '.dash-user-email{display:none}',
        '.dash-logout-btn{padding:5px 10px;font-size:12px}',
        '.dash-admin-btn{padding:5px 10px;font-size:12px}',
        '.prop-card-top{height:240px}',
        '.chat-panel{width:calc(100vw - 24px);right:12px;bottom:88px;height:50vh;max-height:380px;border-radius:16px;box-shadow:0 8px 40px rgba(0,0,0,.25)}',
        '.chat-input{padding:10px;padding-bottom:max(10px,env(safe-area-inset-bottom))}',
        '.chat-input input{font-size:16px}',
        '.fav-toolbar{flex-direction:column;align-items:stretch}',
        '.fav-toolbar-left,.fav-toolbar-right{justify-content:center}',
        '.compare-table th{min-width:120px}',
        '.note-modal{width:95vw}',
        '.dash-tabs{overflow-x:auto}',
        '.map-view{height:calc(100vh - 200px)}',
        '.map-sidebar{width:280px;min-width:220px}',
      '}',

      // Responsive iPhone (375px)
      '@media(max-width:480px){',
        // Nav compact
        '.dash-nav{padding:10px 12px;gap:6px}',
        '.dash-nav .wordmark{font-size:16px}',
        '.dash-nav-right{gap:6px}',
        '.dash-logout-btn,.dash-admin-btn{padding:4px 8px;font-size:11px}',
        // Header
        '.dash-header h1{font-size:20px}',
        '.dash-actions{flex-wrap:wrap;gap:6px;width:100%}',
        '.dash-select{font-size:12px;padding:6px 8px;flex:1;min-width:0}',
        '.dash-refresh-btn{font-size:12px;padding:6px 10px}',
        // Stats compact 2x2
        '.dash-stats{grid-template-columns:1fr 1fr;gap:8px;margin-bottom:16px}',
        '.dash-stat{padding:12px 8px}',
        '.dash-stat-num{font-size:22px}',
        '.dash-stat-lbl{font-size:11px}',
        // Score legend mobile
        '.score-legend-criteria{grid-template-columns:1fr}',
        '.score-legend-body{padding:12px 14px}',
        '.score-legend-badge{font-size:12px;padding:3px 10px}',
        // Tabs
        '.dash-tabs{gap:0;margin-bottom:12px}',
        '.dash-tab{padding:8px 12px;font-size:12px}',
        // Profile bar
        '.dash-profile-bar{margin-bottom:12px}',
        '.ptag{padding:4px 10px;font-size:11px}',
        // Wrap
        '.dash-wrap{padding:12px 10px}',
        // Cards
        '.prop-grid{gap:12px}',
        '.prop-card-top{height:220px}',
        '.prop-card-body{padding:12px}',
        '.prop-price{font-size:16px}',
        '.prop-title{font-size:13px}',
        '.prop-address{font-size:12px;margin-bottom:6px}',
        '.prop-details{font-size:12px;margin-bottom:8px}',
        '.prop-score{padding:4px 8px}',
        '.prop-score-num{font-size:14px}',
        '.prop-score-grade{font-size:10px}',
        '.fav-btn{width:30px;height:30px;font-size:15px}',
        '.prop-days{font-size:10px;padding:2px 6px;right:46px}',
        '.prop-source-link{font-size:11px;padding:2px 6px}',
        // Score mini bars
        '.score-mini-row{gap:4px;margin-bottom:2px}',
        '.score-mini-lbl{font-size:10px;width:36px}',
        '.score-mini-val{font-size:10px;width:18px}',
        '.score-mini-bar{height:4px}',
        // Chat — compact popup above button
        '.chat-toggle{width:48px;height:48px;bottom:16px;right:16px}',
        '.chat-panel{width:calc(100vw - 20px);right:10px;bottom:72px;height:45vh;max-height:340px;border-radius:14px}',
        '.chat-head{padding:10px 14px;font-size:13px}',
        '.chat-body{padding:10px;gap:8px}',
        '.chat-msg{font-size:13px;padding:8px 12px}',
        '.chat-input{padding:8px}',
        '.chat-input input{padding:8px 10px;font-size:16px}',
        '.chat-input button{padding:8px 12px}',
        // Pagination
        '.dash-pagination{gap:4px;margin-top:16px;padding-bottom:16px}',
        '.pag-btn{padding:6px 10px;font-size:12px}',
        '.pag-info{font-size:12px;margin-right:6px}',
        // Favorites
        '.fav-note-preview{font-size:11px;padding:4px 8px}',
        '.fav-note-btn{font-size:11px;padding:4px 8px}',
        '.fav-date{font-size:10px}',
        '.fav-toolbar-left,.fav-toolbar-right{flex-wrap:wrap;gap:6px}',
        '.fav-action-btn{font-size:12px;padding:6px 10px}',
        // Map
        '.map-view{height:calc(100vh - 180px);min-height:300px}',
        '.map-split{flex-direction:column}',
        '.map-sidebar{width:100%;height:180px;min-width:unset;border-right:none;border-bottom:1px solid #E4DFD4}',
        '.map-sidebar-list{display:flex;overflow-x:auto;gap:8px;padding:8px;flex-wrap:nowrap}',
        '.map-card{min-width:240px;flex-shrink:0;margin-bottom:0}',
        '.map-canvas{flex:1;min-height:250px}',
        // Detail overlay
        '.detail-gallery{height:200px}',
        '.detail-body{padding:16px}',
        '.detail-price{font-size:20px}',
        '.detail-title{font-size:15px}',
      '}'
    ].join('');
  }

  // ============================================================
  // ROUTER — Determine which page to show
  // ============================================================
  var path = window.location.pathname;

  if (path === '/dashboard') {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', showDashboard);
    } else {
      showDashboard();
    }
  } else {
    // Landing or any other public page — always hook CTAs, never auto-redirect to dashboard.
    // When logged in, initLanding swaps the "Connexion" button for "Mon Dashboard".
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', initLanding);
    } else {
      initLanding();
    }
  }

  if ('serviceWorker' in navigator) {
    // SW registration failures on unsupported browsers / dev are expected — stay silent.
    navigator.serviceWorker.register('/static/sw.js').catch(function(){});
  }

})();
