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
            TOKEN = data.token;
            USER = data.user;
            // On external hosts (Webflow), render dashboard in place
            var isRenderHost = window.location.hostname === 'lou-platform.onrender.com' || window.location.hostname === 'garou.ch' || window.location.hostname === 'www.garou.ch' || window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';
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
          err.textContent = 'Erreur réseau — réessayez';
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
    // Hook all CTA buttons — open chat if logged in, otherwise auth modal
    var isLoggedIn = isJWT(TOKEN) && USER;
    ['nav-login-btn', 'hero-cta-1', 'cta-bottom'].forEach(function (id) {
      var el = $(id);
      if (el) {
        el.addEventListener('click', function (e) {
          e.preventDefault();
          if (isLoggedIn && (id === 'hero-cta-1' || id === 'cta-bottom')) {
            var chatToggle = document.querySelector('.chat-toggle');
            if (chatToggle) chatToggle.click();
          } else {
            showAuthModal();
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
          window.location.href = '/dashboard';
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
        '<a href="#how">Comment ça marche</a>' +
        '<a href="#" class="btn btn-primary" id="nav-login-btn">Connexion</a>' +
      '</div>';
    document.body.appendChild(nav);

    // HERO
    var hero = ce('section', 'hero');
    hero.innerHTML =
      '<div class="hero-text">' +
        '<h1>Votre <em>chasseur immobilier</em> intelligent en Suisse</h1>' +
        '<p>Lou Garou scrute en continu les meilleures annonces de Suisse romande, les analyse avec l\'IA et vous présente uniquement les biens qui correspondent a vos critères.</p>' +
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
        '<div><strong>500+</strong><span>Annonces analysées</span></div>' +
        '<div><strong>6</strong><span>Critères de scoring</span></div>' +
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
        featureCard('<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/>', 'Scoring intelligent', 'Chaque annonce est notée de A a D selon vos critères : zone, budget, type, surface, equipements, fraîcheur.') +
        featureCard('<path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/>', 'Chatbot IA', 'Discutez avec Lou pour definir vos critères de recherche de manière naturelle. Il comprend vos besoins et affine votre profil.') +
        featureCard('<path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0118 0z"/><circle cx="12" cy="10" r="3"/>', 'Recherche par zone', 'Definissez une ou plusieurs zones géographiques avec un rayon en km. Lou calcule la distance GPS pour chaque bien.') +
        featureCard('<path d="M18 8A6 6 0 006 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 01-3.46 0"/>', 'Alertes en temps reel', 'Soyez averti des qu\'un nouveau bien correspondant a vos critères apparait sur le marche.') +
        featureCard('<rect x="3" y="12" width="4" height="9" rx="1"/><rect x="10" y="7" width="4" height="14" rx="1"/><rect x="17" y="2" width="4" height="19" rx="1"/>', 'Dashboard complet', 'Visualisez tous vos resultats, filtrez par score, triez par prix ou date, et gardez vos favoris.') +
      '</div>';
    document.body.appendChild(features);

    // HOW IT WORKS
    var how = ce('section', 'how');
    how.id = 'how';
    how.innerHTML =
      '<div class="how-inner">' +
        '<h2>Comment ça marche ?</h2>' +
        '<div class="steps">' +
          '<div class="step"><div class="step-num">1</div><h3>Parlez a Lou</h3><p>Dites-lui ce que vous cherchez : region, budget, type de bien, nombre de pièces...</p></div>' +
          '<div class="step"><div class="step-num">2</div><h3>Lou chasse pour vous</h3><p>Notre moteur scrute 8+ portails immobiliers suisses en continu et collecte les nouvelles annonces.</p></div>' +
          '<div class="step"><div class="step-num">3</div><h3>Scoring & analyse</h3><p>Chaque bien est note selon 6 critères ponderes. Seuls les meilleurs vous sont présentés.</p></div>' +
          '<div class="step"><div class="step-num">4</div><h3>Contactez & visitez</h3><p>Retrouvez les coordonnées du propriétaire, l\'annonce originale et tous les details en un clic.</p></div>' +
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
      '@media(max-width:600px){.nav-links a:not(.btn){display:none}.stats-bar-inner{flex-wrap:wrap;margin:0 auto;display:grid;grid-template-columns:repeat(2,1fr);gap:16px}.steps{grid-template-columns:1fr}.hero-visual{display:none}.hero-text{max-width:100%}.hero-text h1{font-size:28px}.hero-ctas{flex-direction:column}.hero-ctas .btn{width:100%;text-align:center}.footer-inner{flex-direction:column;gap:12px}}'
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
        '<button class="dash-logout-btn" id="logout-btn">Déconnexion</button>' +
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
          '<button id="btn-scrape" class="dash-btn-scrape" title="Recalculer les scores selon vos crit\u00e8res">Actualiser mes r\u00e9sultats</button>' +
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

    // Scrape button
    var scrapeBtn = $('btn-scrape');
    if (scrapeBtn) {
      scrapeBtn.onclick = function () {
        var btn = this;
        if (btn.disabled) return;
        btn.disabled = true;
        btn.innerHTML = '<span class="scrape-spinner"></span> Mise \u00e0 jour...';
        btn.classList.add('scraping');

        fetch(API + '/api/scrape', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + TOKEN
          },
          body: JSON.stringify({})
        })
          .then(function (r) {
            if (!r.ok) {
              return r.text().then(function (t) {
                try { return JSON.parse(t); } catch (e) { return { ok: false, error: t }; }
              });
            }
            return r.json();
          })
          .then(function (data) {
            btn.disabled = false;
            btn.classList.remove('scraping');
            if (data.ok) {
              var saved = data.total_saved || 0;
              var scraped = data.total_scraped || 0;
              var scored = data.scored || scraped;
              var msg = '&#10003; ' + scored + ' annonces analys\u00e9es';
              btn.innerHTML = msg;
              btn.classList.add('scrape-success');
              // Refresh dashboard data
              loadStats();
              loadProperties(1, $('sort-select').value, parseInt($('grade-filter').value));
              setTimeout(function () {
                btn.innerHTML = 'Actualiser mes r\u00e9sultats';
                btn.classList.remove('scrape-success');
              }, 5000);
            } else {
              var errMsg = data.error || 'Erreur inconnue';
              btn.innerHTML = 'Erreur: ' + errMsg.substring(0, 40);
              btn.classList.add('scrape-error');
              setTimeout(function () {
                btn.innerHTML = 'Actualiser mes r\u00e9sultats';
                btn.classList.remove('scrape-error');
              }, 4000);
            }
          })
          .catch(function (err) {
            btn.disabled = false;
            btn.classList.remove('scraping');
            btn.innerHTML = 'Erreur réseau — Réessayer';
            btn.classList.add('scrape-error');
            setTimeout(function () {
              btn.innerHTML = 'Actualiser mes r\u00e9sultats';
              btn.classList.remove('scrape-error');
            }, 4000);
          });
      };
    }

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
          $('profile-bar').innerHTML = '<div class="dash-profile-empty">Aucun profil de recherche. <a href="#" id="setup-profile">Parlez a Lou</a> pour configurer vos critères.</div>';
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
        if (p.rooms_min) tags.push(p.rooms_min + '+ pièces');
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
            '<button class="ptag edit-criteria-btn" id="edit-criteria-btn" title="Modifier mes crit\u00e8res">\u270F\uFE0F Modifier</button>' +
          '</div>';
        var editBtn = $('edit-criteria-btn');
        if (editBtn) {
          editBtn.onclick = function() {
            var chatToggle = document.querySelector('.chat-toggle');
            if (chatToggle) chatToggle.click();
          };
        }
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
        list.innerHTML = '<div class="dash-empty">Erreur de chargement. Le serveur est peut-etre en veille — réessayez dans 30 secondes.</div>';
      });
  }

  function renderPropertyCard(p) {
    var gradeColors = { A: '#059669', B: '#0369a1', C: '#d97706', D: '#dc2626' };
    var gc = gradeColors[p.grade] || '#94a3b8';

    var img = (p.images && p.images.length > 0)
      ? '<img src="' + escapeHtml(p.images[0]) + '" alt="' + escapeHtml(p.title || '') + '" class="prop-img" loading="lazy" onerror="this.parentElement.innerHTML=\'<div class=prop-img-placeholder><svg width=48 height=48 viewBox=&quot;0 0 24 24&quot; fill=none stroke=#94a3b8 stroke-width=1.5><rect x=3 y=3 width=18 height=18 rx=2/><circle cx=8.5 cy=8.5 r=1.5/><path d=&quot;M21 15l-5-5L5 21&quot;/></svg></div>\'">'
      : '<div class="prop-img-placeholder"><svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" stroke-width="1.5"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/></svg></div>';

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
        headers: Object.assign(
          { 'Content-Type': 'application/json' },
          TOKEN ? { 'Authorization': 'Bearer ' + TOKEN } : {}
        ),
        body: JSON.stringify({ message: msg, user_id: (USER && USER.id) ? USER.id : 'anon' })
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          loading.remove();
          var reply = data.message || data.reply || 'Désolé, je n\'ai pas compris.';
          // Simple markdown: **bold** -> <strong>, \n -> <br>, • -> bullet
          var formatted = escapeHtml(reply)
            .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
            .replace(/\n/g, '<br>');
          body.innerHTML += '<div class="chat-msg bot">' + formatted + '</div>';

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
                var txt = this.textContent.trim().toLowerCase();
                // Action suggestions — navigate instead of re-sending to chat
                if (txt === 'voir les annonces' || txt === 'voir mes annonces' || txt === 'voir les favoris') {
                  panel.classList.remove('open');
                  var isOnDashboard = window.location.pathname.indexOf('dashboard') !== -1;
                  if (isOnDashboard) {
                    window.scrollTo({ top: 0, behavior: 'smooth' });
                    loadProperties(1, $('sort-select').value, parseInt($('grade-filter').value));
                  } else {
                    window.location.href = '/dashboard';
                  }
                  return;
                }
                if (txt === "créer mon espace" || txt === "créer l'espace" || txt === 'sauvegarder la recherche') {
                  panel.classList.remove('open');
                  if (isJWT(TOKEN) && USER) {
                    window.location.href = '/dashboard';
                  } else {
                    showAuthModal();
                  }
                  return;
                }
                if (txt === 'modifier mes critères' || txt === 'modifier les critères' || txt === 'modifier quelque chose' || txt === 'modifier') {
                  var modBtn = $('edit-criteria-btn');
                  if (modBtn) { panel.classList.remove('open'); modBtn.click(); return; }
                }
                $('chat-in').value = this.textContent;
                sendMsg();
              };
            });
          }
          body.scrollTop = body.scrollHeight;
          // Refresh profile bar if criteria were updated
          if (data.criteria && Object.keys(data.criteria).length > 0 && typeof loadProfileBar === 'function') {
            loadProfileBar();
          }
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
      '.dash-btn-scrape{padding:8px 16px;border:1px solid #0369a1;border-radius:8px;font-size:13px;background:#fff;color:#0369a1;cursor:pointer;font-weight:600;transition:all .2s;display:flex;align-items:center;gap:6px;white-space:nowrap}',
      '.dash-btn-scrape:hover{background:#0369a1;color:#fff}',
      '.dash-btn-scrape:disabled{opacity:.7;cursor:wait}',
      '.dash-btn-scrape.scraping{background:#0369a1;color:#fff}',
      '.dash-btn-scrape.scrape-success{background:#059669;color:#fff;border-color:#059669}',
      '.dash-btn-scrape.scrape-error{background:#dc2626;color:#fff;border-color:#dc2626}',
      '.scrape-spinner{display:inline-block;width:14px;height:14px;border:2px solid rgba(255,255,255,.3);border-top-color:#fff;border-radius:50%;animation:spin 0.8s linear infinite}',
      '@keyframes spin{to{transform:rotate(360deg)}}',

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
      '.prop-img-placeholder{width:100%;height:100%;background:linear-gradient(135deg,#e2e8f0,#f1f5f9);display:flex;align-items:center;justify-content:center}',
      '.prop-score{position:absolute;top:12px;left:12px;display:flex;align-items:center;gap:4px;padding:6px 12px;border-radius:8px;color:#fff;font-weight:700}',
      '.prop-score-num{font-size:18px}',
      '.prop-score-grade{font-size:12px;opacity:.9}',
      '.fav-btn{position:absolute;top:12px;right:12px;background:rgba(255,255,255,.9);border:none;width:36px;height:36px;border-radius:50%;font-size:18px;cursor:pointer;color:#94a3b8;display:flex;align-items:center;justify-content:center;transition:all .2s}',
      '.fav-btn:hover,.fav-btn.active{color:#dc2626;background:#fff}',
      '.edit-criteria-btn{background:#0369a1;color:#fff;cursor:pointer;border:none;padding:6px 14px;border-radius:50px;font-size:13px;transition:background .2s}',
      '.edit-criteria-btn:hover{background:#0284c7}',

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
        '.dash-stats{grid-template-columns:repeat(2,1fr);gap:10px}',
        '.dash-stat{padding:14px}',
        '.dash-stat-num{font-size:24px}',
        '.dash-wrap{padding:16px}',
        '.prop-grid{grid-template-columns:1fr}',
        '.prop-card-top{height:140px}',
        '.dash-header{flex-direction:column;align-items:flex-start}',
        '.dash-header h1{font-size:22px}',
        '.dash-nav{padding:12px 16px}',
        '.dash-user-email{display:none}',
        '.chat-panel{width:calc(100vw - 32px);right:16px;bottom:80px;height:60vh}',
      '}',
      '@media(max-width:480px){',
        '.dash-actions{flex-direction:column;width:100%}',
        '.dash-select{width:100%}',
        '.chat-toggle{width:48px;height:48px;bottom:16px;right:16px}',
        '.chat-panel{height:70vh;bottom:72px}',
      '}'
    ].join('');
  }

  // ============================================================
  // ROUTER — Determine which page to show
  // ============================================================
  var path = window.location.pathname;
  var isRender = window.location.hostname === 'lou-platform.onrender.com' || window.location.hostname === 'garou.ch' || window.location.hostname === 'www.garou.ch' || window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';

  if (path === '/dashboard') {
    showDashboard();
  } else if (!isRender) {
    // External host (Webflow etc.)
    // If user is logged in, show dashboard; otherwise show landing page
    if (isJWT(TOKEN) && USER) {
      if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', showDashboard);
      } else {
        showDashboard();
      }
   } else {
      // Webflow — build landing page then hook CTAs
      function bootWebflow() {
        showLanding();
        initLanding();
        if (window.__chatCardHTML) {
          var hv = document.querySelector('.hero-visual');
          if (hv) hv.outerHTML = window.__chatCardHTML;
        }
      }
      if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', bootWebflow);
      } else {
        bootWebflow();
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
