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

  // Accumulated chatbot criteria (persists across messages)
  var chatCriteria = {};

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
  // Fetch captcha config from backend
  fetch(API + '/api/config').then(function(r){ return r.json(); }).then(function(d){
    if (d.hcaptcha_sitekey) { HCAPTCHA_SITEKEY = d.hcaptcha_sitekey; loadHCaptcha(); }
  }).catch(function(){});

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
      '<h2><svg style="width:28px;height:28px;vertical-align:middle;margin-right:8px" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><rect width="48" height="48" rx="12" fill="#0369a1"/><circle cx="21" cy="20" r="11" fill="none" stroke="#fff" stroke-width="2.8"/><line x1="29" y1="28" x2="39" y2="38" stroke="#fff" stroke-width="2.8" stroke-linecap="round"/><path d="M21 13 L14 19 L15.5 19 L15.5 26 L26.5 26 L26.5 19 L28 19 Z" fill="#fff" opacity="0.95"/><rect x="19.5" y="22" width="3" height="4" rx="0.5" fill="#0369a1"/></svg>Bon Home</h2>',
      '<div class="sub">Votre chasseur immobilier IA en Suisse</div>',
      '<input id="lou-auth-email" type="email" placeholder="Email">',
      '<input id="lou-auth-pass" type="password" placeholder="Mot de passe">',
      '<input id="lou-auth-name" type="text" placeholder="Votre nom" style="display:none">',
      '<div id="lou-hcaptcha" style="display:none;margin-bottom:12px"></div>',
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
    var _hcaptchaWidgetId = null;
    $('lou-auth-toggle').onclick = function () {
      mode = mode === 'login' ? 'signup' : 'login';
      $('lou-auth-name').style.display = mode === 'signup' ? 'block' : 'none';
      $('lou-auth-btn').textContent = mode === 'signup' ? "S'inscrire" : 'Se connecter';
      this.textContent = mode === 'signup' ? 'Deja un compte ? Se connecter' : 'Creer un compte';
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

    // Hook CTA buttons to open chat directly
    ['hero-cta-1', 'cta-bottom', 'nav-cta-btn'].forEach(function (id) {
      var el = $(id);
      if (el) {
        el.addEventListener('click', function (e) {
          e.preventDefault();
          var chatToggle = document.querySelector('.chat-toggle');
          if (chatToggle) {
            var panel = document.querySelector('.chat-panel');
            if (panel && !panel.classList.contains('open')) chatToggle.click();
          }
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
        '<svg class="logo-wolf" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><rect width="48" height="48" rx="12" fill="#0369a1"/><circle cx="21" cy="20" r="11" fill="none" stroke="#fff" stroke-width="2.8"/><line x1="29" y1="28" x2="39" y2="38" stroke="#fff" stroke-width="2.8" stroke-linecap="round"/><path d="M21 13 L14 19 L15.5 19 L15.5 26 L26.5 26 L26.5 19 L28 19 Z" fill="#fff" opacity="0.95"/><rect x="19.5" y="22" width="3" height="4" rx="0.5" fill="#0369a1"/></svg>' +
        '<span class="logo-text">Bon Home</span>' +
      '</a>' +
      '<div class="nav-links">' +
        '<a href="#features">Fonctions</a>' +
        '<a href="#how">Comment ca marche</a>' +
        '<a href="#" class="btn btn-primary" id="nav-login-btn">Connexion</a>' +
      '</div>';
    document.body.appendChild(nav);

    // HERO
    var hero = ce('section', 'hero');
    hero.innerHTML =
      '<div class="hero-text">' +
        '<h1>Le bon <em>home</em>,<br>au bon moment.</h1>' +
        '<p>Bon Home scrute 10+ portails immobiliers suisses en continu. Lou, notre IA, deniche les biens qui vous correspondent et vous les presente — scores, analyses, tout est pret.</p>' +
        '<div class="hero-ctas">' +
          '<a href="#" class="btn btn-primary" id="hero-cta-1">Parler a Lou</a>' +
          '<a href="#how" class="btn btn-outline">En savoir plus</a>' +
        '</div>' +
      '</div>' +
      '<div class="hero-visual">' +
        '<svg class="hero-wolf-icon" width="180" height="180" viewBox="0 0 80 80" xmlns="http://www.w3.org/2000/svg"><circle cx="35" cy="33" r="18" fill="none" stroke="rgba(255,255,255,0.95)" stroke-width="4.5"/><line x1="48" y1="46" x2="65" y2="63" stroke="rgba(255,255,255,0.95)" stroke-width="4.5" stroke-linecap="round"/><path d="M35 21 L24 30 L27 30 L27 41 L43 41 L43 30 L46 30 Z" fill="rgba(255,255,255,0.95)"/><rect x="33" y="35" width="5" height="6" rx="1" fill="rgba(3,105,161,0.6)"/></svg>' +
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
        '<div><strong>500+</strong><span>Annonces analysees</span></div>' +
        '<div><strong>6</strong><span>Criteres de scoring</span></div>' +
        '<div><strong>24/7</strong><span>Veille automatique</span></div>' +
      '</div>';
    document.body.appendChild(statsBar);

    // FEATURES
    var features = ce('section', 'features');
    features.id = 'features';
    features.innerHTML =
      '<div class="features-header"><h2>Pourquoi Bon Home ?</h2><p>Un assistant immobilier complet, de la recherche a la prise de contact.</p></div>' +
      '<div class="features-grid">' +
        featureCard('<circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/>', 'Scraping multi-portails', 'Bon Home scrute automatiquement Homegate, ImmoScout24, Flatfox, Immobilier.ch, Comparis et bien d\'autres pour ne rien manquer.') +
        featureCard('<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>', 'Scoring intelligent', 'Chaque annonce est notee de A a D selon vos criteres : zone, budget, type, surface, equipements, fraicheur.') +
        featureCard('<path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/>', 'Chatbot IA', 'Discutez avec Lou pour definir vos criteres de recherche de maniere naturelle. Il comprend vos besoins et affine votre profil.') +
        featureCard('<path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0118 0z"/><circle cx="12" cy="10" r="3"/>', 'Recherche par zone', 'Definissez une ou plusieurs zones geographiques avec un rayon en km. Lou calcule la distance GPS pour chaque bien.') +
        featureCard('<path d="M18 8A6 6 0 006 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 01-3.46 0"/>', 'Alertes en temps reel', 'Soyez averti des qu\'un nouveau bien correspondant a vos criteres apparait sur le marche.') +
        featureCard('<rect x="3" y="12" width="4" height="9" rx="1"/><rect x="10" y="7" width="4" height="14" rx="1"/><rect x="17" y="2" width="4" height="19" rx="1"/>', 'Dashboard complet', 'Visualisez tous vos resultats, filtrez par score, triez par prix ou date, et gardez vos favoris.') +
      '</div>';
    document.body.appendChild(features);

    // HOW IT WORKS
    var how = ce('section', 'how');
    how.id = 'how';
    how.innerHTML =
      '<div class="how-inner">' +
        '<h2>Comment ca marche ?</h2>' +
        '<div class="steps">' +
          '<div class="step"><div class="step-num">1</div><h3>Parlez a Lou</h3><p>Dites-lui ce que vous cherchez : region, budget, type de bien, nombre de pieces...</p></div>' +
          '<div class="step"><div class="step-num">2</div><h3>Lou chasse pour vous</h3><p>Notre moteur scrute 10+ portails immobiliers suisses en continu et collecte les nouvelles annonces.</p></div>' +
          '<div class="step"><div class="step-num">3</div><h3>Scoring & analyse</h3><p>Chaque bien est note selon 6 criteres ponderes. Seuls les meilleurs vous sont presentes.</p></div>' +
          '<div class="step"><div class="step-num">4</div><h3>Contactez & visitez</h3><p>Retrouvez les coordonnees du proprietaire, l\'annonce originale et tous les details en un clic.</p></div>' +
        '</div>' +
      '</div>';
    document.body.appendChild(how);

    // CTA
    var ctaSection = ce('section', 'cta');
    ctaSection.innerHTML =
      '<h2>Pret a trouver le bon home ?</h2>' +
      '<p>Rejoignez Bon Home et laissez Lou faire le travail de recherche pour vous.</p>' +
      '<a href="#" class="btn btn-primary" id="cta-bottom">Parler a Lou</a>';
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
      '<div class="feature-icon"><svg viewBox="0 0 24 24" fill="none" stroke="#0369a1" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' + svgInner + '</svg></div>' +
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
      ':root{--dark:#0f172a;--blue:#0369a1;--blue-light:#0ea5e9;--gray-50:#f8fafc;--gray-100:#f1f5f9;--gray-300:#cbd5e1;--gray-500:#64748b;--gray-700:#334155;--white:#fff;--green:#059669;--radius:12px}',
      'body{font-family:"Inter",system-ui,sans-serif;color:var(--dark);background:var(--white);-webkit-font-smoothing:antialiased}',
      'h1,h2,h3{font-family:"Playfair Display",Georgia,serif}',

      '.nav{display:flex;justify-content:space-between;align-items:center;padding:18px 5%;max-width:1280px;margin:0 auto}',
      '.nav-logo{display:flex;align-items:center;gap:10px;text-decoration:none;color:var(--dark)}',
      '.nav-logo .logo-wolf{width:36px;height:36px;flex-shrink:0}',
      '.nav-logo .logo-text{font-family:"Playfair Display",Georgia,serif;font-size:22px;font-weight:800}',
      '.nav-links{display:flex;align-items:center;gap:32px}',
      '.nav-links a{text-decoration:none;color:var(--gray-700);font-size:15px;font-weight:500;transition:color .2s}',
      '.nav-links a:hover{color:var(--blue)}',
      '.btn{display:inline-block;padding:10px 24px;border-radius:10px;font-size:15px;font-weight:600;cursor:pointer;text-decoration:none;border:none;transition:all .2s}',
      '.btn-primary{background:var(--blue);color:var(--white)}',
      '.btn-primary:hover{background:var(--blue-light);transform:translateY(-1px);box-shadow:0 4px 16px rgba(3,105,161,.3)}',
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
      '.feature-card:hover{border-color:var(--blue);box-shadow:0 8px 32px rgba(3,105,161,.08);transform:translateY(-4px)}',
      '.feature-icon{width:48px;height:48px;background:rgba(3,105,161,.1);border-radius:12px;display:flex;align-items:center;justify-content:center;margin-bottom:16px}',
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
      '.lou-auth-box{background:#fff;border-radius:16px;padding:36px;width:400px;max-width:90vw;box-shadow:0 20px 60px rgba(0,0,0,.3);position:relative;color:#0f172a}',
      '.lou-auth-box .close-btn{position:absolute;top:12px;right:16px;background:none;border:none;font-size:22px;cursor:pointer;color:#94a3b8}',
      '.lou-auth-box .close-btn:hover{color:#0f172a}',
      '.lou-auth-box h2{font-size:24px;margin:0 0 4px;font-family:"Playfair Display",Georgia,serif}',
      '.lou-auth-box .sub{font-size:14px;color:#64748b;margin-bottom:20px}',
      '.lou-auth-box input{width:100%;padding:12px 14px;border:1px solid #e2e8f0;border-radius:10px;margin-bottom:12px;font-size:14px;box-sizing:border-box;outline:none;font-family:"Inter",sans-serif}',
      '.lou-auth-box input:focus{border-color:#0369a1;box-shadow:0 0 0 3px rgba(3,105,161,.1)}',
      '.auth-submit{width:100%;padding:13px;border:none;border-radius:10px;background:#0369a1;color:#fff;font-size:15px;font-weight:600;cursor:pointer;transition:background .2s;font-family:"Inter",sans-serif}',
      '.auth-submit:hover{background:#0284c7}',
      '.lou-auth-switch{text-align:center;margin-top:14px;font-size:13px;color:#64748b}',
      '.lou-auth-switch a{color:#0369a1;cursor:pointer;text-decoration:underline}',
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
      '@media(max-width:768px){.chat-panel{width:calc(100vw - 24px);right:12px;bottom:88px;height:60vh;max-height:480px;border-radius:16px;box-shadow:0 8px 40px rgba(0,0,0,.25)}.chat-input{padding:10px;padding-bottom:max(10px,env(safe-area-inset-bottom))}.chat-input input{font-size:16px}}'
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
        '<div class="admin-body" id="admin-body"><p style="color:#64748b">Chargement...</p></div>' +
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
        html += '<table class="admin-table"><thead><tr><th>Nom</th><th>Email</th><th>Inscription</th><th>Derniere connexion</th><th>Plan</th><th>Profils</th><th>Favoris</th><th>Statut</th></tr></thead><tbody>';
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
      window.location.reload();
      return;
    }

    document.title = 'Dashboard — Bon Home';

    // Inject CSS
    var style = ce('style', '', getDashCSS());
    document.head.appendChild(style);

    document.body.innerHTML = '';

    // NAV
    var nav = ce('div', 'dash-nav');
    nav.innerHTML =
      '<a href="/" class="dash-nav-brand"><svg class="dash-logo-wolf" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><rect width="48" height="48" rx="12" fill="#0369a1"/><circle cx="21" cy="20" r="11" fill="none" stroke="#fff" stroke-width="2.8"/><line x1="29" y1="28" x2="39" y2="38" stroke="#fff" stroke-width="2.8" stroke-linecap="round"/><path d="M21 13 L14 19 L15.5 19 L15.5 26 L26.5 26 L26.5 19 L28 19 Z" fill="#fff" opacity="0.95"/><rect x="19.5" y="22" width="3" height="4" rx="0.5" fill="#0369a1"/></svg>Bon Home</a>' +
      '<div class="dash-nav-right">' +
        '<button class="dash-admin-btn" id="admin-btn" style="display:none">Admin</button>' +
        '<span class="dash-user-email">' + escapeHtml(USER.email || '') + '</span>' +
        '<button class="dash-logout-btn" id="logout-btn">Deconnexion</button>' +
      '</div>';
    document.body.appendChild(nav);

    // Check if user is admin and show button
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
          '<button id="refresh-btn" class="dash-refresh-btn" title="Actualiser les resultats">&#8635; Actualiser</button>' +
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
        '<div class="dash-stat clickable" id="stat-fav-card" style="cursor:pointer"><div class="dash-stat-num" id="stat-favs">-</div><div class="dash-stat-lbl">Favoris</div></div>' +
        '<div class="dash-stat"><div class="dash-stat-num" id="stat-grade-a">-</div><div class="dash-stat-lbl">Classe A</div></div>' +
      '</div>' +
      // View tabs
      '<div class="dash-tabs" id="dash-tabs">' +
        '<button class="dash-tab active" data-view="properties">Tous les biens</button>' +
        '<button class="dash-tab" data-view="favorites">&#9829; Mes favoris</button>' +
      '</div>' +
      // Profile summary
      '<div id="profile-bar" class="dash-profile-bar"></div>' +
      // Favorites toolbar (hidden by default)
      '<div id="fav-toolbar" class="fav-toolbar" style="display:none">' +
        '<div class="fav-toolbar-left">' +
          '<select id="fav-sort" class="dash-select">' +
            '<option value="date">Plus recents</option>' +
            '<option value="score">Meilleur score</option>' +
            '<option value="price_asc">Prix croissant</option>' +
            '<option value="price_desc">Prix decroissant</option>' +
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
      // Compare panel (hidden by default)
      '<div id="compare-panel" class="compare-panel" style="display:none"></div>' +
      // Pagination
      '<div id="pagination" class="dash-pagination"></div>';

    document.body.appendChild(wrap);

    // Logout
    $('logout-btn').onclick = function () {
      localStorage.removeItem('lou_token');
      localStorage.removeItem('lou_user');
      window.location.reload();
    };

    // Admin panel
    $('admin-btn').onclick = function () { showAdminPanel(); };

    // Load data
    loadStats();
    loadProfileBar();
    loadProperties(1, 'score', 0);

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

    // Sort/filter change
    $('sort-select').onchange = function () {
      loadProperties(1, this.value, parseInt($('grade-filter').value));
    };
    $('grade-filter').onchange = function () {
      loadProperties(1, $('sort-select').value, parseInt(this.value));
    };

    // View tabs
    var currentView = 'properties';
    document.querySelectorAll('.dash-tab').forEach(function (tab) {
      tab.onclick = function () {
        switchView(this.dataset.view);
      };
    });

    // Click on Favoris stat card
    $('stat-fav-card').onclick = function () {
      switchView('favorites');
    };

    function switchView(view) {
      currentView = view;
      document.querySelectorAll('.dash-tab').forEach(function (t) {
        t.classList.toggle('active', t.dataset.view === view);
      });
      var isFav = view === 'favorites';
      $('properties-list').style.display = isFav ? 'none' : '';
      $('favorites-list').style.display = isFav ? '' : 'none';
      $('pagination').style.display = isFav ? 'none' : '';
      $('fav-toolbar').style.display = isFav ? '' : 'none';
      $('sort-select').style.display = isFav ? 'none' : '';
      $('grade-filter').style.display = isFav ? 'none' : '';
      $('compare-panel').style.display = 'none';
      if (isFav) {
        loadFavorites();
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
  function loadStats() {
    apiFetch(API + '/api/stats')
      .then(function (r) { return r.json(); })
      .then(function (data) {
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
      .catch(function () {});
  }

  // ============================================================
  // LOAD PROFILE BAR
  // ============================================================
  var _currentProfile = null; // cached profile for edit form

  function loadProfileBar() {
    apiFetch(API + '/api/profile')
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
        _currentProfile = data.profile;
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
          '<div class="dash-profile-row">' +
            '<div class="dash-profile-tags">' +
              tags.map(function (t) { return '<span class="ptag">' + escapeHtml(t) + '</span>'; }).join('') +
              priorities.map(function (t) { return '<span class="ptag blue">' + escapeHtml(t) + '</span>'; }).join('') +
            '</div>' +
            '<button class="dash-edit-btn" id="edit-profile-btn">Modifier</button>' +
          '</div>' +
          '<div id="profile-edit-form" style="display:none"></div>';

        $('edit-profile-btn').onclick = function () { toggleProfileForm(); };
      })
      .catch(function () {});
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
      return '<div class="pf-zone"><span>📍 ' + escapeHtml(z.city) + '</span><span style="color:#0ea5e9;font-size:12px;font-weight:600">' + z.radius_km + ' km</span><button onclick="_pfRmZone(' + i + ')" style="background:none;border:none;color:#ef4444;cursor:pointer;font-size:14px">✕</button></div>';
    }).join('');
  }

  // Expose globally for onclick
  window._pfRmZone = function (i) { _pfZones.splice(i, 1); _pfRenderZones(); };
  window._pfAddZone = function () {
    var city = $('pf-new-city').value.trim();
    var km = parseFloat($('pf-new-km').value) || 3;
    if (!city) return;
    _pfZones.push({ city: city, canton: '', radius_km: km });
    $('pf-new-city').value = '';
    _pfRenderZones();
  };
  window._pfToggleChip = function (el) { el.classList.toggle('on'); };
  window._pfUpdateBudget = function (id) {
    var val = parseInt($(id).value);
    $(id + '-label').textContent = _pfFormatCHF(val);
  };
  window._pfUpdateRange = function (el) {
    el.nextElementSibling.textContent = el.value + (el.dataset.unit || '');
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
    var imgs = el.querySelectorAll('.prop-img');
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
  };

  window.showScoreDetail = function(el) {
    var existing = document.querySelector('.score-tooltip');
    if (existing) existing.remove();
    var scores;
    try { scores = JSON.parse(el.getAttribute('data-scores')); } catch(e) { return; }
    var tip = document.createElement('div');
    tip.className = 'score-tooltip';
    tip.innerHTML = '<div class="st-row"><span>Zone</span><strong>' + (scores.zone||0) + '/100</strong></div>' +
        '<div class="st-row"><span>Budget</span><strong>' + (scores.budget||0) + '/100</strong></div>' +
        '<div class="st-row"><span>Type</span><strong>' + (scores.type||0) + '/100</strong></div>' +
        '<div class="st-row"><span>Surface</span><strong>' + (scores.surface||0) + '/100</strong></div>' +
        '<div class="st-row"><span>Equip.</span><strong>' + (scores.equipment||0) + '/100</strong></div>' +
        '<div class="st-row"><span>Fraicheur</span><strong>' + (scores.freshness||0) + '/100</strong></div>' +
        '<button onclick="event.stopPropagation();this.parentNode.remove()" style="margin-top:8px;background:none;border:1px solid #cbd5e1;border-radius:6px;padding:4px 12px;cursor:pointer;font-size:12px">Fermer</button>';
    el.style.position = 'relative';
    el.appendChild(tip);
    event.stopPropagation();
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

    var gradeColors = { A: '#059669', B: '#0369a1', C: '#d97706', D: '#dc2626' };
    var gc = gradeColors[p.grade] || '#94a3b8';

    // Images gallery
    var galleryHtml = '';
    if (p.images && p.images.length > 0) {
      var gid = 'detail-gallery';
      galleryHtml = '<div class="detail-gallery" id="' + gid + '">';
      for (var i = 0; i < p.images.length; i++) {
        galleryHtml += '<img src="' + escapeHtml(p.images[i]) + '" class="detail-img' + (i === 0 ? ' active' : '') + '" data-idx="' + i + '" onerror="this.style.display=\'none\'">';
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
    if (p.rooms) rows.push(['Pieces', p.rooms + ' pcs']);
    if (p.surface) rows.push(['Surface', p.surface + ' m²']);
    if (p.floor !== null && p.floor !== undefined) rows.push(['Etage', p.floor + 'e']);
    if (p.distance_km !== null && p.distance_km !== undefined) rows.push(['Distance', p.distance_km + ' km']);
    if (p.days_online !== null && p.days_online !== undefined) rows.push(['En ligne depuis', p.days_online <= 1 ? 'Aujourd\'hui' : p.days_online + ' jours']);
    if (p.published_at) rows.push(['Publie le', new Date(p.published_at).toLocaleDateString('fr-CH')]);

    var tableHtml = rows.map(function(r) {
      return '<div class="detail-row"><span>' + r[0] + '</span><strong>' + r[1] + '</strong></div>';
    }).join('');

    // Score detail
    var sd = p.score_detail || {};
    var scoreHtml = '<div class="detail-score-wrap">' +
      '<div class="detail-score-badge" style="background:' + gc + '"><span class="dsb-num">' + (p.score||0) + '</span><span class="dsb-grade">' + (p.grade||'') + '</span></div>' +
      '<div class="detail-score-bars">' +
        _detailBar('Zone', sd.zone) + _detailBar('Budget', sd.budget) + _detailBar('Type', sd.type) +
        _detailBar('Surface', sd.surface) + _detailBar('Equip.', sd.equipment) + _detailBar('Fraicheur', sd.freshness) +
      '</div></div>';

    // Sources
    var sources = p.all_sources || [{ source: p.source || '', url: p.source_url || '' }];
    var sourcesHtml = sources.map(function(s) {
      var name = (s.source || '').replace('www.', '').split('.')[0] || 'Source';
      if (s.url) {
        return '<a href="' + escapeHtml(s.url) + '" target="_blank" rel="noopener" class="detail-source-link" onclick="event.stopPropagation()">' + escapeHtml(name) + ' ↗</a>';
      }
      return '<span class="detail-source-text">' + escapeHtml(name) + '</span>';
    }).join('');

    // Contact
    var contactHtml = '';
    if (p.contact_name || p.contact_phone || p.contact_email) {
      contactHtml = '<div class="detail-section"><h3>Contact</h3><div class="detail-contact">';
      if (p.contact_name) contactHtml += '<div>👤 ' + escapeHtml(p.contact_name) + '</div>';
      if (p.contact_phone) contactHtml += '<div><a href="tel:' + escapeHtml(p.contact_phone) + '">📞 ' + escapeHtml(p.contact_phone) + '</a></div>';
      if (p.contact_email) contactHtml += '<div><a href="mailto:' + escapeHtml(p.contact_email) + '">✉️ ' + escapeHtml(p.contact_email) + '</a></div>';
      contactHtml += '</div></div>';
    }

    // Features
    var featHtml = '';
    if (p.features && p.features.length > 0) {
      featHtml = '<div class="detail-section"><h3>Equipements</h3><div class="detail-features">' +
        p.features.map(function(f) { return '<span class="detail-feat">✓ ' + escapeHtml(f) + '</span>'; }).join('') +
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
          '<h2 class="detail-title">' + escapeHtml(p.title || 'Bien immobilier') + '</h2>' +
          '<div class="detail-address">📍 ' + escapeHtml(p.address || '') + '</div>' +
          '<div class="detail-section"><h3>Caracteristiques</h3><div class="detail-table">' + tableHtml + '</div></div>' +
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

  function _detailBar(label, val) {
    val = val || 0;
    var color = val >= 80 ? '#059669' : val >= 60 ? '#0369a1' : val >= 40 ? '#d97706' : '#dc2626';
    return '<div class="dsb-row"><span>' + label + '</span><div class="dsb-track"><div class="dsb-fill" style="width:' + val + '%;background:' + color + '"></div></div><strong>' + val + '</strong></div>';
  }

  function toggleProfileForm() {
    var formWrap = $('profile-edit-form');
    if (!formWrap) return;
    if (formWrap.style.display !== 'none') {
      formWrap.style.display = 'none';
      return;
    }
    var p = _currentProfile || {};
    _pfZones = (p.zones || []).filter(function (z) { return z && z.city; }).map(function (z) { return { city: z.city, canton: z.canton || '', radius_km: z.radius_km || 3 }; });

    var types = ['appartement','maison','villa','immeuble','terrain','parking','commerce'];
    var pTypes = p.property_types || [];
    var typeChips = types.map(function (t) {
      return '<span class="pf-chip' + (pTypes.indexOf(t) > -1 ? ' on' : '') + '" data-v="' + t + '" onclick="_pfToggleChip(this)">' + t.charAt(0).toUpperCase() + t.slice(1) + '</span>';
    }).join('');

    var prios = ['vue','balcon','calme','parking','transports','ecoles','commerces','animaux','cave','jardin','ascenseur','renove','minergie','meuble','buanderie'];
    var prioLabels = {'vue':'Vue degagee','balcon':'Balcon/terrasse','calme':'Calme','parking':'Parking','transports':'Proche transports','ecoles':'Proche ecoles','commerces':'Proche commerces','animaux':'Animaux acceptes','cave':'Cave','jardin':'Jardin','ascenseur':'Ascenseur','renove':'Renove','minergie':'Minergie','meuble':'Meuble','buanderie':'Buanderie'};
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
        // Zones
        '<div class="pf-section"><div class="pf-section-title">📍 Zones geographiques</div>' +
          '<div id="pf-zone-list" class="pf-zone-list"></div>' +
          '<div class="pf-zone-add">' +
            '<input id="pf-new-city" type="text" placeholder="Ajouter une ville..." style="flex:1">' +
            '<select id="pf-new-km"><option value="1">1 km</option><option value="2">2 km</option><option value="3" selected>3 km</option><option value="5">5 km</option><option value="10">10 km</option><option value="15">15 km</option><option value="20">20 km</option></select>' +
            '<button class="pf-add-btn" onclick="_pfAddZone()">+</button>' +
          '</div>' +
        '</div>' +
        // Type de bien
        '<div class="pf-section"><div class="pf-section-title">🏠 Type de bien</div>' +
          '<div class="pf-chips" id="pf-types">' + typeChips + '</div>' +
        '</div>' +
        // Transaction & Budget
        '<div class="pf-section"><div class="pf-section-title">💰 Transaction & Budget</div>' +
          '<div class="pf-grid">' +
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
        // Caracteristiques
        '<div class="pf-section"><div class="pf-section-title">📐 Caracteristiques</div>' +
          '<div class="pf-grid">' +
            '<div class="pf-field"><label>Pieces min</label><div class="pf-range"><input type="range" id="pf-rooms-min" min="1" max="10" step="0.5" value="' + rMinVal + '" data-unit=" pcs" oninput="_pfUpdateRange(this)"><span>' + rMinVal + ' pcs</span></div></div>' +
            '<div class="pf-field"><label>Pieces max</label><div class="pf-range"><input type="range" id="pf-rooms-max" min="1" max="10" step="0.5" value="' + rMaxVal + '" data-unit=" pcs" oninput="_pfUpdateRange(this)"><span>' + rMaxVal + ' pcs</span></div></div>' +
            '<div class="pf-field"><label>Surface min (m²)</label><div class="pf-range"><input type="range" id="pf-surface-min" min="20" max="300" step="5" value="' + sMinVal + '" data-unit=" m²" oninput="_pfUpdateRange(this)"><span>' + sMinVal + ' m²</span></div></div>' +
            '<div class="pf-field"><label>Surface max (m²)</label><div class="pf-range"><input type="range" id="pf-surface-max" min="20" max="500" step="5" value="' + sMaxVal + '" data-unit=" m²" oninput="_pfUpdateRange(this)"><span>' + sMaxVal + ' m²</span></div></div>' +
          '</div>' +
        '</div>' +
        // Priorites
        '<div class="pf-section"><div class="pf-section-title">⭐ Priorites & Equipements</div>' +
          '<div class="pf-chips" id="pf-priorities">' + prioChips + '</div>' +
        '</div>' +
        // Actions
        '<div class="pf-actions">' +
          '<button id="pf-save" class="pf-save-btn">Sauvegarder & relancer Lou 🔍</button>' +
          '<button id="pf-cancel" class="pf-cancel-btn">Annuler</button>' +
        '</div>' +
      '</div>';

    formWrap.style.display = 'block';
    _pfRenderZones();

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
    btn.textContent = 'Sauvegarde...';
    btn.disabled = true;

    apiFetch(API + '/api/profile', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    })
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (data.ok) {
        $('profile-edit-form').style.display = 'none';
        loadProfileBar();
        // Re-score and reload properties
        apiFetch(API + '/api/score', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
          .then(function () { loadProperties(1, 'score', 0); })
          .catch(function () { loadProperties(1, 'score', 0); });
      } else {
        btn.textContent = 'Erreur — reessayez';
        btn.disabled = false;
      }
    })
    .catch(function () {
      btn.textContent = 'Erreur reseau';
      btn.disabled = false;
    });
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

    var url = API + '/api/properties' +
      '?page=' + page +
      '&per_page=12' +
      '&sort=' + currentSort +
      '&min_score=' + currentMinScore;

    apiFetch(url)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.properties || data.properties.length === 0) {
          list.innerHTML = '<div class="dash-empty">' +
            '<h3 style="margin-bottom:8px;font-family:Playfair Display,serif">Pas encore de resultats</h3>' +
            '<p>Lou est en train de chasser pour vous ! Les premiers biens apparaitront apres le prochain cycle de recherche (toutes les 2 heures).</p>' +
            '<p style="margin-top:12px">En attendant, <a href="#" class="open-chat-link" style="color:#0369a1;cursor:pointer">parlez a Lou</a> pour affiner vos criteres.</p>' +
          '</div>';
          var chatLink = list.querySelector('.open-chat-link');
          if (chatLink) chatLink.onclick = function(e) { e.preventDefault(); document.querySelector('.chat-toggle').click(); };
          $('pagination').innerHTML = '';
          return;
        }

        // Store property data for detail view
        window._propData = window._propData || {};
        data.properties.forEach(function (p) { window._propData[p.id] = p; });

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

    var imgHtml = '';
    if (p.images && p.images.length > 1) {
      // Carousel with arrows
      var cid = 'carousel-' + p.id;
      imgHtml = '<div class="prop-carousel" id="' + cid + '">';
      for (var ii = 0; ii < p.images.length; ii++) {
        imgHtml += '<img src="' + escapeHtml(p.images[ii]) + '" alt="" class="prop-img' + (ii === 0 ? ' active' : '') + '" data-idx="' + ii + '" onerror="this.style.display=\'none\';_checkAllImgsFailed(this.parentNode)">';
      }
      imgHtml += '<button class="carousel-btn prev" onclick="carouselNav(\'' + cid + '\',-1)">&#8249;</button>';
      imgHtml += '<button class="carousel-btn next" onclick="carouselNav(\'' + cid + '\',1)">&#8250;</button>';
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
    var daysColor = daysOnline <= 3 ? '#059669' : daysOnline <= 14 ? '#0369a1' : daysOnline <= 30 ? '#d97706' : '#94a3b8';

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
      scoreDetailAttr = ' data-scores=\'' + JSON.stringify({zone: p.score_detail.zone||0, budget: p.score_detail.budget||0, type: p.score_detail.type||0, surface: p.score_detail.surface||0, equipment: p.score_detail.equipment||0, freshness: p.score_detail.freshness||0}) + '\' onclick="showScoreDetail(this)" title="Cliquez pour le detail"';
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
        '<div class="prop-title">' + escapeHtml(p.title || 'Bien immobilier') + '</div>' +
        '<div class="prop-address">' + escapeHtml(p.address || '') + '</div>' +
        '<div class="prop-details">' + details.join(' &middot; ') + '</div>' +
        '<div class="prop-scores-mini">' +
          (p.score_detail ? (
            scoreBar('Zone', p.score_detail.zone || 0) +
            scoreBar('Budget', p.score_detail.budget || 0) +
            scoreBar('Type', p.score_detail.type || 0) +
            scoreBar('Surface', p.score_detail.surface || 0) +
            scoreBar('Equip.', p.score_detail.equipment || 0)
          ) : '') +
        '</div>' +
        '<div class="prop-footer">' +
          (function() {
            var sources = p.all_sources || [{ source: p.source || '', url: p.source_url || '' }];
            var html = '<div class="prop-sources">';
            sources.forEach(function(s) {
              var name = (s.source || '').replace('www.', '').split('.')[0] || 'Source';
              if (s.url) {
                html += '<a href="' + escapeHtml(s.url) + '" target="_blank" rel="noopener" class="prop-source-link">' + escapeHtml(name) + '</a> ';
              } else {
                html += '<span class="prop-source">' + escapeHtml(name) + '</span> ';
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
    apiFetch(API + '/api/favorite/' + propertyId, { method: 'POST' })
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
          list.innerHTML = '<div class="dash-empty">' +
            '<div style="font-size:48px;margin-bottom:16px">&#9825;</div>' +
            '<div style="font-size:16px;font-weight:600;margin-bottom:8px">Aucun favori</div>' +
            '<div>Cliquez sur le coeur d\'un bien pour l\'ajouter a vos favoris</div>' +
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
            }
          };
        });
      })
      .catch(function () {
        list.innerHTML = '<div class="dash-empty">Erreur de chargement des favoris</div>';
      });
  }

  function renderFavoriteCard(p) {
    var gradeColors = { A: '#059669', B: '#0369a1', C: '#d97706', D: '#dc2626' };
    var gc = gradeColors[p.grade] || '#94a3b8';

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
    var daysColor = daysOnline <= 3 ? '#059669' : daysOnline <= 14 ? '#0369a1' : daysOnline <= 30 ? '#d97706' : '#94a3b8';

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
        '<input type="checkbox" class="fav-compare-check" data-id="' + p.id + '" title="Selectionner pour comparer" style="display:' + (compareMode ? '' : 'none') + '">' +
      '</div>' +
      '<div class="prop-card-body">' +
        '<div class="prop-price">' + priceText + '</div>' +
        '<div class="prop-title">' + escapeHtml(p.title || 'Bien immobilier') + '</div>' +
        '<div class="prop-address">' + escapeHtml(p.address || '') + '</div>' +
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
    var ids = Object.keys(compareSet);
    if (ids.length < 2) {
      panel.style.display = 'none';
      return;
    }

    var props = ids.map(function (id) { return compareSet[id]; });
    var gradeColors = { A: '#059669', B: '#0369a1', C: '#d97706', D: '#dc2626' };

    // Build comparison table
    var html = '<div class="compare-header">' +
      '<h3>&#9878; Comparaison (' + props.length + ' biens)</h3>' +
      '<button class="compare-close-btn" onclick="document.getElementById(\'compare-panel\').style.display=\'none\'">&times;</button>' +
    '</div>';

    html += '<div class="compare-scroll"><table class="compare-table"><thead><tr><th>Critere</th>';
    props.forEach(function (p) {
      var gc = gradeColors[p.grade] || '#94a3b8';
      html += '<th>' +
        '<div class="compare-th-score" style="background:' + gc + '">' + p.score + ' ' + p.grade + '</div>' +
        '<div class="compare-th-title">' + escapeHtml((p.title || '').substring(0, 40)) + '</div>' +
      '</th>';
    });
    html += '</tr></thead><tbody>';

    // Rows
    var rows = [
      { label: 'Prix', key: 'price', fmt: function (v) { return v ? formatPrice(v) + ' CHF' : '-'; } },
      { label: 'Pieces', key: 'rooms', fmt: function (v) { return v || '-'; } },
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
          var color = v >= 80 ? '#059669' : v >= 60 ? '#0369a1' : v >= 40 ? '#d97706' : '#dc2626';
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
  function initChat() {
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

    function sendMsg() {
      var input = $('chat-in');
      var msg = input.value.trim();
      if (!msg) return;
      input.value = '';

      var body = $('chat-body');

      // Remove old suggestion buttons before adding new message
      var oldSugs = body.querySelectorAll('.chat-suggestions');
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
          }

          // Handle profile_ready — prompt signup if not logged in
          if (data.profile_ready && !isJWT(TOKEN)) {
            body.insertAdjacentHTML('beforeend', '<div class="chat-msg bot" style="background:#e0f2fe">' +
              'Super, j\'ai tous tes criteres ! ' +
              '<a href="#" class="chat-signup-link" style="color:#0369a1;font-weight:600">Cree ton espace</a> pour que je lance la recherche.' +
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

          // Show suggestions
          if (data.suggestions && data.suggestions.length) {
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
        })
        .catch(function () {
          loading.remove();
          body.insertAdjacentHTML('beforeend', '<div class="chat-msg bot">Erreur de connexion. Reessayez.</div>');
          body.scrollTop = body.scrollHeight;
        });
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
          return { city: z.city || '', canton: z.canton || '', radius_km: z.radius_km || 3 };
        })
      };
      apiFetch(API + '/api/profile', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      }).then(function (r) { return r.json(); })
      .then(function () {
        loadProfileBar();
        // Trigger scoring + scraping so results appear immediately
        apiFetch(API + '/api/score', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
          .then(function () {
            if (typeof loadProperties === 'function') loadProperties(1, 'score', 0);
            if (typeof loadStats === 'function') loadStats();
          })
          .catch(function () {});
      }).catch(function () {});
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
      '.dash-admin-btn{background:#0369a1;border:none;color:#fff;padding:6px 14px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600;transition:all .2s}',
      '.dash-admin-btn:hover{background:#0284c7}',
      '.dash-logout-btn{background:none;border:1px solid #475569;color:#94a3b8;padding:6px 14px;border-radius:8px;cursor:pointer;font-size:13px;transition:all .2s}',
      '.dash-logout-btn:hover{border-color:#e2e8f0;color:#e2e8f0}',

      // Admin panel
      '.admin-panel{background:#fff;border-radius:16px;padding:28px;width:90vw;max-width:900px;max-height:80vh;overflow-y:auto;box-shadow:0 20px 60px rgba(0,0,0,.3);position:relative;color:#0f172a}',
      '.admin-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}',
      '.admin-header h2{margin:0;font-family:"Playfair Display",Georgia,serif;font-size:22px}',
      '.admin-header .close-btn{position:static;background:none;border:none;font-size:22px;cursor:pointer;color:#94a3b8}',
      '.admin-stats{font-size:15px;color:#64748b;margin-bottom:16px}',
      '.admin-table{width:100%;border-collapse:collapse;font-size:13px}',
      '.admin-table th{text-align:left;padding:10px 8px;border-bottom:2px solid #e2e8f0;color:#64748b;font-weight:600;font-size:12px;text-transform:uppercase}',
      '.admin-table td{padding:10px 8px;border-bottom:1px solid #f1f5f9}',
      '.admin-table tr:hover td{background:#f8fafc}',
      '.admin-plan{background:#e0f2fe;color:#0369a1;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600}',

      // Wrap
      '.dash-wrap{max-width:1200px;margin:0 auto;padding:28px 24px}',

      // Header
      '.dash-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;flex-wrap:wrap;gap:16px}',
      '.dash-header h1{font-family:"Playfair Display",Georgia,serif;font-size:28px;margin:0}',
      '.dash-actions{display:flex;gap:10px}',
      '.dash-select{padding:8px 14px;border:1px solid #e2e8f0;border-radius:8px;font-size:13px;background:#fff;cursor:pointer;color:#334155;outline:none}',
      '.dash-select:focus{border-color:#0369a1}',
      '.dash-refresh-btn{padding:8px 16px;background:#059669;color:#fff;border:none;border-radius:8px;font-size:13px;cursor:pointer;font-weight:600;transition:background .2s}',
      '.dash-refresh-btn:hover{background:#047857}',
      '.dash-refresh-btn:disabled{opacity:.6;cursor:wait}',

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
      '.dash-profile-row{display:flex;align-items:center;justify-content:space-between;gap:12px}',
      '.dash-edit-btn{padding:8px 18px;background:#0369a1;color:#fff;border:none;border-radius:8px;font-size:13px;cursor:pointer;white-space:nowrap;transition:background .2s}',
      '.dash-edit-btn:hover{background:#024e7a}',
      '.profile-form{background:#fff;border:1px solid #e2e8f0;border-radius:16px;padding:24px;margin-top:12px}',
      '.pf-section{margin-bottom:20px}',
      '.pf-section-title{font-size:15px;font-weight:700;margin-bottom:12px;color:#0f172a}',
      '.pf-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}',
      '.pf-field{display:flex;flex-direction:column;gap:6px}',
      '.pf-field label{font-size:12px;color:#64748b;font-weight:600}',
      '.pf-field input,.pf-field select{padding:9px 12px;border:1px solid #e2e8f0;border-radius:8px;font-size:14px;font-family:inherit}',
      '.pf-field input:focus,.pf-field select:focus{border-color:#0369a1;outline:none}',
      '.pf-range{display:flex;align-items:center;gap:10px}',
      '.pf-range input[type=range]{flex:1;accent-color:#0369a1;cursor:pointer}',
      '.pf-range span{font-size:13px;font-weight:600;color:#0369a1;min-width:90px;text-align:right;white-space:nowrap}',
      '.pf-chips{display:flex;flex-wrap:wrap;gap:8px}',
      '.pf-chip{padding:7px 14px;border-radius:20px;font-size:13px;cursor:pointer;border:1px solid #e2e8f0;background:#f8fafc;color:#64748b;transition:all .15s;user-select:none}',
      '.pf-chip:hover{border-color:#0369a1;color:#0f172a}',
      '.pf-chip.on{background:#0369a1;border-color:#0369a1;color:#fff}',
      '.pf-zone-list{display:flex;flex-direction:column;gap:6px;margin-bottom:10px}',
      '.pf-zone{display:flex;align-items:center;gap:10px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:8px 14px;font-size:14px}',
      '.pf-zone span:first-child{flex:1;font-weight:500}',
      '.pf-zone-add{display:flex;gap:8px;align-items:center}',
      '.pf-zone-add input{flex:1;padding:9px 12px;border:1px solid #e2e8f0;border-radius:8px;font-size:14px;font-family:inherit}',
      '.pf-zone-add input:focus{border-color:#0369a1;outline:none}',
      '.pf-zone-add select{padding:9px 8px;border:1px solid #e2e8f0;border-radius:8px;font-size:13px}',
      '.pf-add-btn{padding:8px 14px;background:#0369a1;color:#fff;border:none;border-radius:8px;font-size:16px;cursor:pointer;font-weight:700}',
      '.pf-actions{display:flex;gap:10px;justify-content:flex-end;margin-top:8px;padding-top:16px;border-top:1px solid #e2e8f0}',
      '.pf-save-btn{padding:11px 24px;background:#0369a1;color:#fff;border:none;border-radius:10px;font-size:14px;cursor:pointer;font-weight:600;transition:all .2s}',
      '.pf-save-btn:hover{background:#024e7a;transform:translateY(-1px);box-shadow:0 4px 12px rgba(3,105,161,.3)}',
      '.pf-cancel-btn{padding:11px 24px;background:#f1f5f9;color:#64748b;border:none;border-radius:10px;font-size:14px;cursor:pointer}',
      '@media(max-width:768px){.pf-grid{grid-template-columns:1fr}.dash-profile-row{flex-direction:column;align-items:stretch}.pf-zone-add{flex-wrap:wrap}}',

      // Properties grid
      '.prop-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:20px}',
      '.prop-card{background:#fff;border:1px solid #e2e8f0;border-radius:14px;overflow:hidden;transition:all .2s}',
      '.prop-card:hover{box-shadow:0 8px 24px rgba(0,0,0,.08);border-color:#cbd5e1;transform:translateY(-2px)}',
      '.prop-card-top{position:relative;height:180px;background:#e2e8f0;overflow:hidden}',
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
      '.prop-img-placeholder{width:100%;height:100%;background:linear-gradient(135deg,#e2e8f0,#cbd5e1);display:flex;align-items:center;justify-content:center;color:#94a3b8;font-size:13px}',
      '.prop-img-placeholder::after{content:"Pas d\'image";opacity:.6}',
      '.prop-score{position:absolute;top:12px;left:12px;display:flex;align-items:center;gap:4px;padding:6px 12px;border-radius:8px;color:#fff;font-weight:700;cursor:pointer}',
      '.prop-score-num{font-size:18px}',
      '.prop-score-grade{font-size:12px;opacity:.9}',
      '.fav-btn{position:absolute;top:12px;right:12px;background:rgba(255,255,255,.9);border:none;width:36px;height:36px;border-radius:50%;font-size:18px;cursor:pointer;color:#94a3b8;display:flex;align-items:center;justify-content:center;transition:all .2s}',
      '.fav-btn:hover,.fav-btn.active{color:#dc2626;background:#fff}',
      '.prop-days{position:absolute;top:12px;right:52px;padding:3px 8px;border-radius:8px;font-size:11px;font-weight:700;color:#fff;z-index:2}',
      '.price-drop-badge{background:#059669;color:#fff;font-size:12px;font-weight:700;padding:2px 8px;border-radius:6px;margin-right:6px}',
      '.old-price{color:#94a3b8;font-size:14px;font-weight:400;margin-left:8px}',
      '.score-tooltip{position:absolute;top:100%;left:0;background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:12px;box-shadow:0 8px 24px rgba(0,0,0,.15);z-index:50;min-width:160px;font-size:13px}',
      '.st-row{display:flex;justify-content:space-between;padding:3px 0;color:#334155}',
      '.st-row strong{color:#0369a1}',

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
      '.prop-sources{display:flex;flex-wrap:wrap;gap:6px;align-items:center}',
      '.prop-source-link{font-size:12px;color:#0369a1;text-decoration:none;font-weight:500;padding:2px 8px;border:1px solid #cbd5e1;border-radius:12px;text-transform:capitalize}',
      '.prop-source-link:hover{background:#f0f9ff;border-color:#0369a1}',
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

      // Property detail overlay
      '.detail-overlay{position:fixed;inset:0;background:rgba(15,23,42,.6);z-index:2000;display:flex;justify-content:center;overflow-y:auto;padding:24px;backdrop-filter:blur(4px)}',
      '.detail-panel{background:#fff;border-radius:16px;width:100%;max-width:720px;overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,.2);margin:auto;position:relative;animation:detailIn .25s ease}',
      '@keyframes detailIn{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:translateY(0)}}',
      '.detail-close{position:absolute;top:16px;right:16px;width:36px;height:36px;border-radius:50%;background:rgba(0,0,0,.5);color:#fff;border:none;font-size:18px;cursor:pointer;z-index:10;display:flex;align-items:center;justify-content:center;transition:background .2s}',
      '.detail-close:hover{background:rgba(0,0,0,.7)}',
      '.detail-gallery{position:relative;width:100%;height:360px;background:#e2e8f0;overflow:hidden}',
      '.detail-img{width:100%;height:100%;object-fit:cover;display:none}',
      '.detail-img.active{display:block}',
      '.detail-gallery .carousel-btn{width:40px;height:40px;font-size:28px}',
      '.detail-counter{position:absolute;bottom:12px;right:16px;background:rgba(0,0,0,.55);color:#fff;padding:4px 12px;border-radius:20px;font-size:12px;font-weight:600}',
      '.detail-gallery-empty{width:100%;height:200px;background:#f1f5f9;display:flex;align-items:center;justify-content:center;color:#94a3b8;font-size:15px}',
      '.detail-body{padding:28px}',
      '.detail-price{font-size:28px;font-weight:800;color:#0f172a;margin-bottom:6px;font-family:"Playfair Display",Georgia,serif}',
      '.detail-title{font-size:18px;font-weight:600;color:#334155;margin-bottom:4px}',
      '.detail-address{font-size:14px;color:#64748b;margin-bottom:24px}',
      '.detail-section{margin-bottom:24px}',
      '.detail-section h3{font-size:15px;font-weight:700;color:#0f172a;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid #f1f5f9}',
      '.detail-table{display:grid;grid-template-columns:1fr 1fr;gap:0}',
      '.detail-row{display:flex;justify-content:space-between;padding:10px 0;border-bottom:1px solid #f8fafc;font-size:14px}',
      '.detail-row span{color:#64748b}',
      '.detail-row strong{color:#0f172a}',
      '.detail-score-wrap{display:flex;gap:20px;align-items:flex-start;margin-bottom:24px;padding:20px;background:#f8fafc;border-radius:12px}',
      '.detail-score-badge{width:64px;height:64px;border-radius:14px;display:flex;flex-direction:column;align-items:center;justify-content:center;flex-shrink:0}',
      '.dsb-num{font-size:22px;font-weight:800;color:#fff;line-height:1}',
      '.dsb-grade{font-size:13px;font-weight:700;color:rgba(255,255,255,.8)}',
      '.detail-score-bars{flex:1;display:flex;flex-direction:column;gap:6px}',
      '.dsb-row{display:flex;align-items:center;gap:10px;font-size:13px}',
      '.dsb-row span{min-width:70px;color:#64748b}',
      '.dsb-track{flex:1;height:6px;background:#e2e8f0;border-radius:3px;overflow:hidden}',
      '.dsb-fill{height:100%;border-radius:3px;transition:width .3s}',
      '.dsb-row strong{min-width:24px;text-align:right;font-size:13px;color:#334155}',
      '.detail-contact{display:flex;flex-direction:column;gap:8px;font-size:14px}',
      '.detail-contact a{color:#0369a1;text-decoration:none}',
      '.detail-contact a:hover{text-decoration:underline}',
      '.detail-features{display:flex;flex-wrap:wrap;gap:8px}',
      '.detail-feat{padding:6px 14px;background:#f0f9ff;border:1px solid #bae6fd;border-radius:20px;font-size:13px;color:#0369a1}',
      '.detail-sources{display:flex;flex-wrap:wrap;gap:10px}',
      '.detail-source-link{padding:10px 20px;background:#0369a1;color:#fff;border-radius:10px;font-size:14px;font-weight:600;text-decoration:none;text-transform:capitalize;transition:background .2s}',
      '.detail-source-link:hover{background:#0284c7}',
      '.detail-source-text{padding:10px 20px;background:#f1f5f9;border-radius:10px;font-size:14px;color:#64748b;text-transform:capitalize}',
      '@media(max-width:768px){.detail-overlay{padding:0}.detail-panel{border-radius:0;max-width:100%;min-height:100vh}.detail-gallery{height:260px}.detail-body{padding:20px}.detail-price{font-size:22px}.detail-table{grid-template-columns:1fr}.detail-score-wrap{flex-direction:column}}',

      // View tabs
      '.dash-tabs{display:flex;gap:0;margin-bottom:20px;border-bottom:2px solid #e2e8f0}',
      '.dash-tab{padding:10px 20px;border:none;background:none;font-size:14px;font-weight:600;cursor:pointer;color:#64748b;border-bottom:2px solid transparent;margin-bottom:-2px;transition:all .2s}',
      '.dash-tab:hover{color:#0369a1}',
      '.dash-tab.active{color:#0369a1;border-bottom-color:#0369a1}',

      // Favorites toolbar
      '.fav-toolbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;gap:12px;flex-wrap:wrap}',
      '.fav-toolbar-left{display:flex;gap:10px;align-items:center}',
      '.fav-toolbar-right{display:flex;gap:10px;align-items:center}',
      '.fav-action-btn{padding:8px 16px;border:1px solid #e2e8f0;background:#fff;border-radius:8px;font-size:13px;cursor:pointer;color:#334155;font-weight:500;transition:all .2s}',
      '.fav-action-btn:hover{border-color:#0369a1;color:#0369a1}',
      '.fav-action-btn.active{background:#0369a1;color:#fff;border-color:#0369a1}',

      // Favorite card extras
      '.fav-note-preview{font-size:12px;color:#64748b;background:#f8fafc;padding:6px 10px;border-radius:6px;margin-bottom:8px;border-left:3px solid #0369a1;font-style:italic}',
      '.fav-card-footer{display:flex;justify-content:space-between;align-items:center;padding-top:10px;border-top:1px solid #f1f5f9}',
      '.fav-note-btn{background:none;border:1px solid #e2e8f0;padding:5px 12px;border-radius:6px;font-size:12px;color:#64748b;cursor:pointer;transition:all .2s}',
      '.fav-note-btn:hover{border-color:#0369a1;color:#0369a1}',
      '.fav-date{font-size:11px;color:#94a3b8}',
      '.fav-compare-check{position:absolute;top:12px;left:52px;width:20px;height:20px;accent-color:#0369a1;cursor:pointer;z-index:3}',
      '.dash-stat.clickable:hover{border-color:#0369a1;box-shadow:0 4px 16px rgba(3,105,161,.1)}',

      // Note modal
      '.note-modal-overlay{position:fixed;inset:0;background:rgba(15,23,42,.6);z-index:3000;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px)}',
      '.note-modal{background:#fff;border-radius:16px;padding:24px;width:440px;max-width:90vw;box-shadow:0 20px 60px rgba(0,0,0,.3);color:#0f172a}',
      '.note-modal-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}',
      '.note-modal-head h3{margin:0;font-size:18px;font-weight:700}',
      '.note-modal-close{background:none;border:none;font-size:24px;cursor:pointer;color:#94a3b8}',
      '.note-textarea{width:100%;height:120px;border:1px solid #e2e8f0;border-radius:10px;padding:12px;font-size:14px;font-family:Inter,sans-serif;resize:vertical;outline:none;box-sizing:border-box}',
      '.note-textarea:focus{border-color:#0369a1;box-shadow:0 0 0 3px rgba(3,105,161,.1)}',
      '.note-modal-footer{display:flex;justify-content:space-between;align-items:center;margin-top:12px}',
      '.note-char-count{font-size:12px;color:#94a3b8}',
      '.note-modal-actions{display:flex;gap:8px}',
      '.note-cancel-btn{padding:8px 16px;background:#f1f5f9;border:none;border-radius:8px;font-size:13px;cursor:pointer;color:#64748b}',
      '.note-save-btn{padding:8px 16px;background:#0369a1;color:#fff;border:none;border-radius:8px;font-size:13px;cursor:pointer;font-weight:600;transition:background .2s}',
      '.note-save-btn:hover{background:#0284c7}',

      // Compare panel
      '.compare-panel{background:#fff;border:1px solid #e2e8f0;border-radius:16px;padding:24px;margin-bottom:24px;box-shadow:0 4px 16px rgba(0,0,0,.06)}',
      '.compare-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}',
      '.compare-header h3{margin:0;font-size:18px;font-weight:700}',
      '.compare-close-btn{background:none;border:none;font-size:22px;cursor:pointer;color:#94a3b8}',
      '.compare-scroll{overflow-x:auto}',
      '.compare-table{width:100%;border-collapse:collapse;font-size:13px}',
      '.compare-table th{padding:12px 10px;text-align:center;border-bottom:2px solid #e2e8f0;min-width:140px}',
      '.compare-th-score{display:inline-block;padding:4px 12px;border-radius:8px;color:#fff;font-weight:700;font-size:14px;margin-bottom:4px}',
      '.compare-th-title{font-size:12px;color:#64748b;font-weight:400;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:160px}',
      '.compare-table td{padding:10px;text-align:center;border-bottom:1px solid #f1f5f9}',
      '.compare-label{text-align:left!important;color:#64748b;font-weight:600}',
      '.compare-best{background:#f0fdf4;color:#059669;font-weight:700}',
      '.compare-note{font-size:12px;color:#64748b;text-align:left!important;max-width:180px}',

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
        '.dash-nav{padding:12px 16px;flex-wrap:wrap;gap:8px}',
        '.dash-nav-brand{font-size:16px}',
        '.dash-logo-wolf{width:24px;height:24px}',
        '.dash-nav-right{gap:8px;flex-wrap:wrap}',
        '.dash-user-email{display:none}',
        '.dash-logout-btn{padding:5px 10px;font-size:12px}',
        '.dash-admin-btn{padding:5px 10px;font-size:12px}',
        '.prop-card-top{height:160px}',
        '.chat-panel{width:calc(100vw - 24px);right:12px;bottom:88px;height:60vh;max-height:480px;border-radius:16px;box-shadow:0 8px 40px rgba(0,0,0,.25)}',
        '.chat-input{padding:10px;padding-bottom:max(10px,env(safe-area-inset-bottom))}',
        '.chat-input input{font-size:16px}',
        '.fav-toolbar{flex-direction:column;align-items:stretch}',
        '.fav-toolbar-left,.fav-toolbar-right{justify-content:center}',
        '.compare-table th{min-width:120px}',
        '.note-modal{width:95vw}',
        '.dash-tabs{overflow-x:auto}',
      '}'
    ].join('');
  }

  // ============================================================
  // ROUTER — Determine which page to show
  // ============================================================
  var path = window.location.pathname;
  var isRender = window.location.hostname === 'lou-platform.onrender.com' || window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';

  if (path === '/dashboard') {
    showDashboard();
  } else if (!isRender) {
    // External host (Webflow etc.)
    // If user is logged in, show dashboard; otherwise hook into existing page
    if (isJWT(TOKEN) && USER) {
      if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', showDashboard);
      } else {
        showDashboard();
      }
    } else {
      if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initLanding);
      } else {
        initLanding();
      }
    }
  } else {
    // Render host — HTML already exists, just hook CTAs
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', initLanding);
    } else {
      initLanding();
    }
  }

})();
