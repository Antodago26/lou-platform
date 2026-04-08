/**
 * Lou Garou — Frontend App
 * Landing page auth + Full Dashboard with real properties
 */
(function () {
  'use strict';

  // Dynamic API URL — works on any host (garou.ch, onrender, localhost)
  var API = window.location.origin;
  var TOKEN = localStorage.getItem('lou_token');
  var USER = JSON.parse(localStorage.getItem('lou_user') || 'null');

  // Accumulated chatbot criteria (persists across messages)
  var chatCriteria = {};

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
        ? { email: email, password: pass, name: name, criteria: chatCriteria }
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
    document.title = 'Lou Garou — Chasseur Immobilier IA en Suisse';

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
        '<svg class="logo-wolf" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><rect width="48" height="48" rx="12" fill="#0369a1"/><g transform="translate(8,6)"><path d="M4 34 L10 5 L16 16 L22 5 L28 34 L22 28 L16 32 L10 28 Z" fill="rgba(255,255,255,0.95)"/><circle cx="12.5" cy="21" r="2" fill="#0369a1"/><circle cx="19.5" cy="21" r="2" fill="#0369a1"/></g></svg>' +
        '<span class="logo-text">Lou Garou</span>' +
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
        '<h1>Votre <em>chasseur immobilier</em> intelligent en Suisse</h1>' +
        '<p>Lou Garou scrute en continu les meilleures annonces de Suisse romande, les analyse avec l\'IA et vous presente uniquement les biens qui correspondent a vos criteres.</p>' +
        '<div class="hero-ctas">' +
          '<a href="#" class="btn btn-primary" id="hero-cta-1">Commencer ma recherche</a>' +
          '<a href="#how" class="btn btn-outline">En savoir plus</a>' +
        '</div>' +
      '</div>' +
      '<div class="hero-visual">' +
        '<svg class="hero-wolf-icon" width="180" height="180" viewBox="0 0 80 80" xmlns="http://www.w3.org/2000/svg"><g transform="translate(14,10)"><path d="M6 56 L16 8 L26 26 L36 8 L46 56 L36 46 L26 52 L16 46 Z" fill="rgba(255,255,255,0.95)"/><circle cx="20" cy="34" r="3" fill="rgba(255,255,255,0.3)"/><circle cx="32" cy="34" r="3" fill="rgba(255,255,255,0.3)"/></g></svg>' +
        '<div class="hero-badge">' +
          '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="opacity:.8;flex-shrink:0"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>' +
          '<div>8+ portails suisses<small>Homegate, ImmoScout24, Flatfox...</small></div>' +
        '</div>' +
      '</div>';
    document.body.appendChild(hero);

    // STATS BAR
    var statsBar = ce('div', 'stats-bar');
    statsBar.innerHTML =
      '<div class="stats-bar-inner">' +
        '<div><strong>8+</strong><span>Portails suisses</span></div>' +
        '<div><strong>500+</strong><span>Annonces analysees</span></div>' +
        '<div><strong>6</strong><span>Criteres de scoring</span></div>' +
        '<div><strong>24/7</strong><span>Veille automatique</span></div>' +
      '</div>';
    document.body.appendChild(statsBar);

    // FEATURES
    var features = ce('section', 'features');
    features.id = 'features';
    features.innerHTML =
      '<div class="features-header"><h2>Pourquoi Lou Garou ?</h2><p>Un assistant immobilier complet, de la recherche a la prise de contact.</p></div>' +
      '<div class="features-grid">' +
        featureCard('<circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/>', 'Scraping multi-portails', 'Lou scrute automatiquement Homegate, ImmoScout24, Flatfox, Immobilier.ch, Properstar et bien d\'autres pour ne rien manquer.') +
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
          '<div class="step"><div class="step-num">2</div><h3>Lou chasse pour vous</h3><p>Notre moteur scrute 8+ portails immobiliers suisses en continu et collecte les nouvelles annonces.</p></div>' +
          '<div class="step"><div class="step-num">3</div><h3>Scoring & analyse</h3><p>Chaque bien est note selon 6 criteres ponderes. Seuls les meilleurs vous sont presentes.</p></div>' +
          '<div class="step"><div class="step-num">4</div><h3>Contactez & visitez</h3><p>Retrouvez les coordonnees du proprietaire, l\'annonce originale et tous les details en un clic.</p></div>' +
        '</div>' +
      '</div>';
    document.body.appendChild(how);

    // CTA
    var ctaSection = ce('section', 'cta');
    ctaSection.innerHTML =
      '<h2>Pret a trouver votre bien ?</h2>' +
      '<p>Rejoignez Lou Garou et laissez l\'IA faire le travail de recherche pour vous.</p>' +
      '<a href="#" class="btn btn-primary" id="cta-bottom">Commencer gratuitement</a>';
    document.body.appendChild(ctaSection);

    // FOOTER
    var footer = ce('footer', 'footer');
    footer.innerHTML =
      '<div class="footer-inner">' +
        '<p>&copy; 2026 Lou Garou. Chasseur immobilier IA en Suisse.</p>' +
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
      '@media(max-width:600px){.nav-links a:not(.btn){display:none}.stats-bar-inner{flex-wrap:wrap}.steps{grid-template-columns:1fr}}'
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
      '@media(max-width:768px){.chat-panel{width:calc(100vw - 32px);right:16px;bottom:80px;height:60vh}}'
    ].join(''));
    document.head.appendChild(s);
  }

  // ============================================================
  // DASHBOARD
  // ============================================================
  function showDashboard() {
    if (!isJWT(TOKEN) || !USER) {
      window.location.reload();
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
      window.location.reload();
    };

    // Load data
    loadStats();
    loadProfileBar();
    loadProperties(1, 'score', 0);

    // Refresh button — launch background scrape+score, then poll for results
    $('refresh-btn').onclick = function () {
      var btn = this;
      btn.disabled = true;
      btn.textContent = '⟳ Recherche en cours...';
      apiFetch(API + '/api/scrape', { method: 'POST' })
        .then(function () {
          // Scraping runs in background (~60s). Wait then reload.
          btn.textContent = '⟳ Scraping 8 portails...';
          setTimeout(function () {
            btn.textContent = '⟳ Mise a jour...';
            loadStats();
            loadProfileBar();
            loadProperties(1, 'score', 0);
            btn.textContent = '↻ Actualiser';
            btn.disabled = false;
          }, 90000); // 90 seconds for background scrape+score to finish
        })
        .catch(function () {
          btn.textContent = '↻ Actualiser';
          btn.disabled = false;
          loadProperties(1, 'score', 0);
        });
    };

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

  function toggleProfileForm() {
    var formWrap = $('profile-edit-form');
    if (!formWrap) return;
    if (formWrap.style.display !== 'none') {
      formWrap.style.display = 'none';
      return;
    }
    var p = _currentProfile || {};
    var zones = (p.zones || []).filter(function (z) { return z && z.city; });
    var zoneVal = zones.map(function (z) { return z.city; }).join(', ');

    formWrap.innerHTML =
      '<div class="profile-form">' +
        '<div class="pf-row">' +
          '<label>Transaction</label>' +
          '<select id="pf-transaction">' +
            '<option value="location"' + (p.transaction === 'location' ? ' selected' : '') + '>Location</option>' +
            '<option value="achat"' + (p.transaction === 'achat' ? ' selected' : '') + '>Achat</option>' +
          '</select>' +
        '</div>' +
        '<div class="pf-row">' +
          '<label>Type de bien</label>' +
          '<input id="pf-types" placeholder="appartement, maison" value="' + escapeHtml((p.property_types || []).join(', ')) + '">' +
        '</div>' +
        '<div class="pf-row">' +
          '<label>Budget max (CHF)</label>' +
          '<input id="pf-budget-max" type="number" value="' + (p.budget_max || '') + '">' +
        '</div>' +
        '<div class="pf-row">' +
          '<label>Budget min (CHF)</label>' +
          '<input id="pf-budget-min" type="number" value="' + (p.budget_min || '') + '">' +
        '</div>' +
        '<div class="pf-row">' +
          '<label>Pieces min</label>' +
          '<input id="pf-rooms-min" type="number" step="0.5" value="' + (p.rooms_min || '') + '">' +
        '</div>' +
        '<div class="pf-row">' +
          '<label>Surface min (m2)</label>' +
          '<input id="pf-surface-min" type="number" value="' + (p.surface_min || '') + '">' +
        '</div>' +
        '<div class="pf-row">' +
          '<label>Villes (separees par virgule)</label>' +
          '<input id="pf-zones" placeholder="Lausanne, Geneve" value="' + escapeHtml(zoneVal) + '">' +
        '</div>' +
        '<div class="pf-row">' +
          '<label>Priorites (separees par virgule)</label>' +
          '<input id="pf-priorities" placeholder="balcon, calme, parking" value="' + escapeHtml((p.priorities || []).join(', ')) + '">' +
        '</div>' +
        '<div class="pf-actions">' +
          '<button id="pf-save" class="pf-save-btn">Sauvegarder</button>' +
          '<button id="pf-cancel" class="pf-cancel-btn">Annuler</button>' +
        '</div>' +
      '</div>';

    formWrap.style.display = 'block';

    $('pf-cancel').onclick = function () { formWrap.style.display = 'none'; };
    $('pf-save').onclick = function () { saveProfileForm(); };
  }

  function saveProfileForm() {
    var payload = {
      transaction: $('pf-transaction').value,
      property_types: $('pf-types').value.split(',').map(function (s) { return s.trim(); }).filter(Boolean),
      budget_max: parseInt($('pf-budget-max').value) || null,
      budget_min: parseInt($('pf-budget-min').value) || null,
      rooms_min: parseFloat($('pf-rooms-min').value) || null,
      surface_min: parseInt($('pf-surface-min').value) || null,
      priorities: $('pf-priorities').value.split(',').map(function (s) { return s.trim(); }).filter(Boolean),
      zones: $('pf-zones').value.split(',').map(function (s) {
        var city = s.trim();
        if (!city) return null;
        return { city: city, radius_km: 3 };
      }).filter(Boolean)
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
        apiFetch(API + '/api/score', { method: 'POST' })
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
    if (p.unit && p.price) {
      var unitPart = (p.unit.split('/')[1] || '').toLowerCase();
      // Don't show "/total" or "/one-time" for purchases — only show for rentals
      if (unitPart && unitPart !== 'total' && unitPart !== 'one-time') {
        priceText += '<small>/' + escapeHtml(unitPart) + '</small>';
      }
    }

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
          (p.score_detail ? (
            scoreBar('Zone', p.score_detail.zone || 0) +
            scoreBar('Budget', p.score_detail.budget || 0) +
            scoreBar('Type', p.score_detail.type || 0) +
            scoreBar('Surface', p.score_detail.surface || 0) +
            scoreBar('Equip.', p.score_detail.equipment || 0)
          ) : '') +
        '</div>' +
        '<div class="prop-footer">' +
          '<span class="prop-source">' + escapeHtml(sourceLabel) + '</span>' +
          (p.source_url ? '<a href="' + escapeHtml(p.source_url) + '" target="_blank" rel="noopener" class="prop-link">Voir l\'annonce &rarr;</a>' : '') +
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

      var chatUserId = (USER && USER.id) ? String(USER.id) : ANON_SESSION;

      fetch(API + '/api/chat', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({ message: msg, user_id: chatUserId })
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
        apiFetch(API + '/api/score', { method: 'POST' })
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
      '.profile-form{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:20px;margin-top:12px;display:grid;grid-template-columns:1fr 1fr;gap:12px}',
      '.pf-row{display:flex;flex-direction:column;gap:4px}',
      '.pf-row label{font-size:12px;color:#64748b;font-weight:600}',
      '.pf-row input,.pf-row select{padding:8px 12px;border:1px solid #e2e8f0;border-radius:8px;font-size:14px;font-family:inherit}',
      '.pf-row input:focus,.pf-row select:focus{border-color:#0369a1;outline:none}',
      '.pf-actions{grid-column:1/-1;display:flex;gap:10px;justify-content:flex-end;margin-top:4px}',
      '.pf-save-btn{padding:10px 24px;background:#0369a1;color:#fff;border:none;border-radius:8px;font-size:14px;cursor:pointer;font-weight:600}',
      '.pf-save-btn:hover{background:#024e7a}',
      '.pf-cancel-btn{padding:10px 24px;background:#f1f5f9;color:#64748b;border:none;border-radius:8px;font-size:14px;cursor:pointer}',
      '@media(max-width:768px){.profile-form{grid-template-columns:1fr}.dash-profile-row{flex-direction:column;align-items:stretch}}',

      // Properties grid
      '.prop-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:20px}',
      '.prop-card{background:#fff;border:1px solid #e2e8f0;border-radius:14px;overflow:hidden;transition:all .2s}',
      '.prop-card:hover{box-shadow:0 8px 24px rgba(0,0,0,.08);border-color:#cbd5e1;transform:translateY(-2px)}',
      '.prop-card-top{position:relative;height:180px;background:#e2e8f0;overflow:hidden}',
      '.prop-img{width:100%;height:100%;object-fit:cover}',
      '.prop-img-placeholder{width:100%;height:100%;background:linear-gradient(135deg,#e2e8f0,#cbd5e1);display:flex;align-items:center;justify-content:center;color:#94a3b8;font-size:13px}',
      '.prop-img-placeholder::after{content:"Pas d\'image";opacity:.6}',
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
