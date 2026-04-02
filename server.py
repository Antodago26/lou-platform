"""
Lou Garou — API Server
Pure Python HTTP server (zero external dependencies)
Endpoints:
  POST /api/signup          — Create account + save search profile
  POST /api/login           — Authenticate and get session token
  GET  /api/me              — Get current user info
  GET  /api/profiles        — Get user's search profiles
  GET  /api/properties/:id  — Get properties for a profile
  GET  /api/stats/:id       — Get stats for a profile
  POST /api/favorite/:id    — Toggle favorite on a property
  POST /api/dismiss/:id     — Dismiss a property
  POST /api/search/:id      — Trigger a search for a profile
  GET  /api/health          — Health check
"""
import json
import os
import sys
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Add parent dir to path
sys.path.insert(0, os.path.dirname(__file__))

from database import init_db, create_user, authenticate, create_session, \
    get_user_from_token, create_search_profile, get_user_profiles, \
    get_properties, get_property_stats, toggle_favorite, dismiss_property, add_property
from scoring import compute_lou_score
from scrapers import run_all_scrapers

# Initialize database on import
init_db()

# CORS origins (add your Webflow domain)
ALLOWED_ORIGINS = [
    "https://garou2.webflow.io",
    "https://garou.ch",
    "https://www.garou.ch",
    "http://localhost:8000",
    "http://localhost:3000",
]


class LouAPIHandler(BaseHTTPRequestHandler):
    """HTTP request handler for Lou Garou API"""

    def _cors_headers(self):
        origin = self.headers.get("Origin", "")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
        else:
            self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGINS[0])
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Credentials", "true")

    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _send_error(self, message, status=400):
        self._send_json({"error": message}, status)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        body = self.rfile.read(length)
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {}

    def _get_user(self):
        """Extract and validate user from Authorization header"""
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
            return get_user_from_token(token)
        return None

    def _serve_static(self, filepath, content_type="text/html"):
        """Serve a static file"""
        static_dir = os.path.join(os.path.dirname(__file__), "static")
        full_path = os.path.join(static_dir, filepath)
        if os.path.exists(full_path):
            self.send_response(200)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self._cors_headers()
            self.end_headers()
            with open(full_path, "rb") as f:
                self.wfile.write(f.read())
        else:
            self._send_error("Not found", 404)

    def _route(self, method):
        """Route request to appropriate handler"""
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)

        # Static routes
        if path == "/" or path == "/dashboard":
            return self._serve_static("dashboard.html")

        # Health check
        if path == "/api/health":
            return self._send_json({"status": "ok", "service": "lou-garou"})

        # Auth endpoints
        if method == "POST" and path == "/api/signup":
            return self._handle_signup()
        if method == "POST" and path == "/api/login":
            return self._handle_login()

        # Protected endpoints
        user = self._get_user()
        if not user:
            return self._send_error("Non authentifié", 401)

        if method == "GET" and path == "/api/me":
            return self._handle_me(user)
        if method == "GET" and path == "/api/profiles":
            return self._handle_profiles(user)

        # Profile-specific endpoints
        match = re.match(r"/api/properties/(\d+)", path)
        if match and method == "GET":
            return self._handle_properties(user, int(match.group(1)), query)

        match = re.match(r"/api/stats/(\d+)", path)
        if match and method == "GET":
            return self._handle_stats(user, int(match.group(1)))

        match = re.match(r"/api/favorite/(\d+)", path)
        if match and method == "POST":
            return self._handle_favorite(user, int(match.group(1)))

        match = re.match(r"/api/dismiss/(\d+)", path)
        if match and method == "POST":
            return self._handle_dismiss(user, int(match.group(1)))

        match = re.match(r"/api/search/(\d+)", path)
        if match and method == "POST":
            return self._handle_search(user, int(match.group(1)))

        return self._send_error("Route non trouvée", 404)

    # ─── Auth ───

    def _handle_signup(self):
        data = self._read_body()
        email = data.get("email", "").strip()
        password = data.get("password", "")
        name = data.get("name", "")

        if not email or not password:
            return self._send_error("Email et mot de passe requis")
        if len(password) < 6:
            return self._send_error("Mot de passe: 6 caractères minimum")
        if "@" not in email:
            return self._send_error("Email invalide")

        result = create_user(email, password, name)
        if not result["ok"]:
            return self._send_error(result["error"])

        # Auto-login after signup
        user = authenticate(email, password)
        token = create_session(user["id"])

        # If search criteria provided, save profile
        profile_id = None
        criteria = data.get("criteria")
        if criteria:
            profile_id = create_search_profile(user["id"], criteria)

        return self._send_json({
            "ok": True,
            "token": token,
            "user": {"id": user["id"], "email": email, "name": name},
            "profile_id": profile_id,
        })

    def _handle_login(self):
        data = self._read_body()
        email = data.get("email", "").strip()
        password = data.get("password", "")

        user = authenticate(email, password)
        if not user:
            return self._send_error("Email ou mot de passe incorrect", 401)

        token = create_session(user["id"])
        return self._send_json({
            "ok": True,
            "token": token,
            "user": {
                "id": user["id"],
                "email": user["email"],
                "name": user["name"],
            }
        })

    # ─── Protected endpoints ───

    def _handle_me(self, user):
        return self._send_json({
            "id": user["id"],
            "email": user["email"],
            "name": user["name"],
            "created_at": user["created_at"],
        })

    def _handle_profiles(self, user):
        profiles = get_user_profiles(user["id"])
        for p in profiles:
            if p.get("priorities"):
                try:
                    p["priorities"] = json.loads(p["priorities"])
                except:
                    pass
            # Get quick stats
            stats = get_property_stats(p["id"])
            p["stats"] = stats
        return self._send_json({"profiles": profiles})

    def _handle_properties(self, user, profile_id, query):
        # Verify profile belongs to user
        profiles = get_user_profiles(user["id"])
        if not any(p["id"] == profile_id for p in profiles):
            return self._send_error("Profil non trouvé", 404)

        sort = query.get("sort", ["lou_score DESC"])[0]
        limit = int(query.get("limit", [50])[0])
        offset = int(query.get("offset", [0])[0])

        props = get_properties(profile_id, limit=limit, offset=offset, sort=sort)
        for p in props:
            if p.get("images"):
                try:
                    p["images"] = json.loads(p["images"])
                except:
                    pass
            if p.get("score_details"):
                try:
                    p["score_details"] = json.loads(p["score_details"])
                except:
                    pass
        return self._send_json({"properties": props})

    def _handle_stats(self, user, profile_id):
        profiles = get_user_profiles(user["id"])
        if not any(p["id"] == profile_id for p in profiles):
            return self._send_error("Profil non trouvé", 404)
        stats = get_property_stats(profile_id)
        return self._send_json(stats)

    def _handle_favorite(self, user, property_id):
        toggle_favorite(property_id, user["id"])
        return self._send_json({"ok": True})

    def _handle_dismiss(self, user, property_id):
        dismiss_property(property_id, user["id"])
        return self._send_json({"ok": True})

    def _handle_search(self, user, profile_id):
        """Trigger a scraping run for a profile"""
        profiles = get_user_profiles(user["id"])
        profile = next((p for p in profiles if p["id"] == profile_id), None)
        if not profile:
            return self._send_error("Profil non trouvé", 404)

        # Run all scrapers
        raw_results = run_all_scrapers(profile)

        # Score each property
        scored = []
        for prop in raw_results:
            try:
                score = compute_lou_score(prop, profile)
                prop["lou_score"] = score["total"]
                prop["lou_grade"] = score["grade"]
                prop["score_details"] = score["categories"]
                add_property(profile_id, prop)
                scored.append(prop)
            except Exception as e:
                print(f"Error scoring property: {e}")

        return self._send_json({
            "ok": True,
            "found": len(scored),
            "sources": list(set(p["source"] for p in scored)),
        })

    # ─── HTTP methods ───

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def log_message(self, format, *args):
        print(f"[Lou API] {args[0]}")


def run(port=8080):
    server = HTTPServer(("0.0.0.0", port), LouAPIHandler)
    print(f"🐺 Lou Garou API running on http://localhost:{port}")
    print(f"   Endpoints: /api/signup, /api/login, /api/profiles, /api/properties/:id")
    print(f"   Health: /api/health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.server_close()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", sys.argv[1] if len(sys.argv) > 1 else 8080))
    run(port)
