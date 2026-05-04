"""Bon Home — Chatbot (Lou) Blueprint."""
import os
import json
import re
import logging

import jwt
from flask import Blueprint, jsonify, request

from anthropic import Anthropic

from db import get_db, return_db
from helpers import validate_json, ChatRequest
from auth import JWT_SECRET
from rate_limit import client_ip, check_rate_limit

log = logging.getLogger('lou-app')
chat_bp = Blueprint('chat', __name__)

ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
anthropic_client = Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None

# Per-key chat caps. Authenticated users get a wider envelope than anons.
# Buckets are shared with /api/login etc. via rate_limit.py — an attacker
# cannot rotate from spamming login to spamming chat to dodge throttles.
CHAT_AUTH_PER_MIN = 20
CHAT_AUTH_PER_HOUR = 200
CHAT_ANON_PER_MIN = 5
CHAT_ANON_PER_HOUR = 30


def _rate_key():
    """Returns (key, is_anonymous). Server-controlled only — never trusts
    user-supplied session_id (trivially bypassable by incrementing a cookie)."""
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if token:
        try:
            tdata = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
            return f"user:{tdata['user_id']}", False
        except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
            pass
    return f"ip:{client_ip()}", True


def _check_rate_limit(key, is_anonymous):
    """Chat-specific wrapper that picks anon vs auth limits."""
    per_min = CHAT_ANON_PER_MIN if is_anonymous else CHAT_AUTH_PER_MIN
    per_hour = CHAT_ANON_PER_HOUR if is_anonymous else CHAT_AUTH_PER_HOUR
    return check_rate_limit(key, per_min, per_hour)


LOU_SYSTEM_PROMPT = """Tu es Lou, un chasseur immobilier digital suisse. Tu es un loup sympathique et efficace.

TON ROLE: Aider les gens a definir leur recherche immobiliere en Suisse romande via une conversation naturelle.

REGLES STRICTES:
- Parle en francais, tutoie, sois chaleureux mais pro
- UNE question a la fois
- Si l'utilisateur donne plusieurs infos d'un coup, enregistre TOUT dans criteria
- Sois bref: 1-3 phrases max
- Emojis avec parcimonie
- Ne te presente PAS (le message de bienvenue est deja affiche)
- IMPORTANT: Ne redemande JAMAIS un critere deja collecte. Lis attentivement les [Critères déjà collectés] en debut de message utilisateur. Passe au critere SUIVANT manquant.
- Deduis les infos implicites: "acheter un appartement sur Cortaillod" = transaction:achat + type:appartement + zone:Cortaillod

CRITERES A COLLECTER (dans cet ordre de priorite):
1. Zone: ville(s), canton — SOUVENT donne dans le premier message
2. Type: appartement, maison, villa, studio, loft, attique, duplex — SOUVENT donne dans le premier message
3. Transaction: location ou achat — SOUVENT donne dans le premier message
4. Budget: min et/ou max CHF
5. Pieces: min et/ou max
6. Priorites: balcon, parking, vue, calme, transports, animaux, cave, jardin, ascenseur (optionnel)
7. Surface: m2 min (optionnel)

COMPORTEMENT:
- Apres chaque message, mets a jour TOUS les criteres deja connus dans "criteria"
- Quand tu as zone + type + transaction + budget + pieces, mets profile_ready: true
- Les suggestions doivent etre contextuelles (pas de suggestions de region si la region est deja connue)
- Ne force pas les criteres optionnels

REPONSE: JSON uniquement:
{"message":"texte","suggestions":["btn1","btn2"],"criteria":{"zones":[{"city":"X","canton":"XX","radius_km":3}],"property_types":["appartement"],"transaction":"achat","budget_min":null,"budget_max":900000,"currency":"CHF","rooms_min":3,"rooms_max":null,"surface_min":null,"priorities":[],"move_date":null},"profile_ready":false,"confirmed":false}

Cantons: VD, GE, NE, FR, VS, JU, BE, ZH, BS, TI, LU, SG"""


def _load_chat_history(user_id):
    """Load conversation history from DB."""
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT role, content FROM conversations
            WHERE (user_id = %s OR session_id = %s)
            ORDER BY created_at ASC
            LIMIT 20
        """, (user_id if user_id.isdigit() else None, user_id))
        rows = cur.fetchall()
        return [{"role": r['role'], "content": r['content']} for r in rows]
    except Exception:
        return []
    finally:
        cur.close()
        return_db(conn)


def _save_chat_message(user_id, role, content, criteria_json=None):
    """Save a chat message to DB."""
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO conversations (user_id, session_id, role, content, criteria_json)
            VALUES (%s, %s, %s, %s, %s)
        """, (
            int(user_id) if user_id.isdigit() else None,
            user_id,
            role,
            content,
            json.dumps(criteria_json) if criteria_json else None
        ))
        conn.commit()
    except Exception as e:
        conn.rollback()
        log.error(f"Failed to save chat message: {e}")
    finally:
        cur.close()
        return_db(conn)


def _post_process_zones(result, user_id, is_anonymous):
    """
    v6.3.2 étape 5 — post-processing déterministe des zones extraites par le LLM.

    Pour chaque zone de result.criteria.zones :
      - tente resolve_zone_coords (cache DB → dict → geo.admin.ch)
      - si non résolue, calcule suggestions via geo_suggestions
      - log dans unresolved_locations (audit hebdo)

    Si des zones restent non résolues, injecte :
      - result['unresolved_zones'] : liste [{query, is_npa, suggestions, log_id}]
      - result['message'] : message déterministe (override LLM)
      - result['suggestions'] : boutons = top suggestions de la première zone
      - result['profile_ready'] = False (on ne peut pas finaliser sans GPS)

    Le LLM ne gère PAS cette logique parce qu'il ne connaît pas le dict
    CITY_COORDS et hallucinerait des communes inexistantes.
    """
    criteria = result.get('criteria') or {}
    zones = criteria.get('zones') or []
    if not zones or not isinstance(zones, list):
        return result

    from scoring_engine import resolve_zone_coords
    from geo_suggestions import suggest_similar_cities, is_npa, log_unresolved

    conn = get_db()
    unresolved = []
    try:
        for z in zones:
            if not isinstance(z, dict):
                continue
            city = (z.get('city') or '').strip()
            if not city:
                continue
            if z.get('latitude') and z.get('longitude'):
                continue  # LLM a déjà fourni GPS (rare mais possible)
            try:
                resolve_zone_coords(z, conn=conn)
            except Exception as e:
                log.warning(f"resolve_zone_coords failed for {city!r}: {e}")
            if z.get('latitude') and z.get('longitude'):
                continue
            # Zone non résolue — suggestions + log audit
            sugg = suggest_similar_cities(city, limit=3)
            uid_int = int(user_id) if user_id and user_id.isdigit() else None
            anon_sid = user_id if is_anonymous else None
            row_id = log_unresolved(
                conn, city, sugg,
                user_id=uid_int, anon_session_id=anon_sid,
            )
            unresolved.append({
                'query': city,
                'is_npa': is_npa(city),
                'suggestions': sugg,
                'log_id': row_id,
            })
    finally:
        return_db(conn)

    if not unresolved:
        return result

    result['unresolved_zones'] = unresolved
    result['profile_ready'] = False

    # Message déterministe (override LLM) — contrat UX de l'étape 5.
    parts = []
    for u in unresolved:
        q = u['query']
        if u['is_npa']:
            parts.append(
                f"Je ne trouve pas le NPA {q} dans la base suisse — "
                f"peux-tu me donner le nom de la commune ?"
            )
        elif u['suggestions']:
            names = ', '.join(f"« {s['city']} »" for s in u['suggestions'][:3])
            parts.append(
                f"Je ne connais pas « {q} » — tu voulais dire {names} ?"
            )
        else:
            parts.append(
                f"Je ne connais pas « {q} » — peux-tu me donner le NPA "
                f"(code postal 4 chiffres) ?"
            )
    result['message'] = ' '.join(parts)

    # Boutons suggestion = top 3 de la première zone non résolue (si dispo).
    first = unresolved[0]
    if first['suggestions']:
        result['suggestions'] = [s['city'] for s in first['suggestions']]
    else:
        result['suggestions'] = []

    return result


def _parse_llm_json(raw):
    """Robustly parse JSON from Claude's response, handling markdown wrapping."""
    text = raw.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r'```(?:json)?\s*(.*?)```', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    brace_start = text.find('{')
    brace_end = text.rfind('}')
    if brace_start != -1 and brace_end != -1 and brace_end > brace_start:
        try:
            return json.loads(text[brace_start:brace_end + 1])
        except json.JSONDecodeError:
            pass

    return None


@chat_bp.route('/api/chat', methods=['POST'])
def api_chat():
    _req, _err = validate_json(ChatRequest)
    if _err:
        return _err
    data = request.json or {}
    message = (getattr(_req, 'message', None) or data.get('message', '') or '').strip()

    if not message:
        return jsonify({"error": "Message vide"}), 400

    if len(message) > 2000:
        return jsonify({"error": "Message trop long (2000 caractères max)"}), 400

    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    is_anonymous = False
    if token:
        try:
            token_data = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
            user_id = str(token_data['user_id'])
        except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
            session_id = data.get('session_id', 'anon')
            user_id = f"anon-{session_id}"
            is_anonymous = True
    else:
        session_id = data.get('session_id', 'anon')
        user_id = f"anon-{session_id}"
        is_anonymous = True

    if not anthropic_client:
        return jsonify({
            "message": "Chatbot IA non configuré. Ajoutez ANTHROPIC_API_KEY.",
            "suggestions": [], "criteria": {}, "profile_ready": False, "confirmed": False
        })

    # Rate-limit AVANT tout appel LLM. Clé = IP (anon) ou JWT user_id.
    rl_key, rl_is_anon = _rate_key()
    if not _check_rate_limit(rl_key, rl_is_anon):
        log.warning(f"Chat rate-limited key={rl_key} anon={rl_is_anon}")
        return jsonify({
            "message": "Trop de messages. Attends quelques secondes avant de réessayer.",
            "suggestions": [], "criteria": {}, "profile_ready": False, "confirmed": False
        }), 429

    # TODO: activer quand PRICING_ENABLED = True
    # from plans import check_limit
    # if not is_anonymous:
    #     user_plan = _get_user_plan(int(user_id)) if user_id.isdigit() else 'free'
    #     # Count today's messages from DB and compare to plan limit
    #     # if not check_limit(user_plan, 'chat_messages_per_day', today_count):
    #     #     return jsonify({"limited": True, "upgrade_url": "/pricing"}), 429

    history = _load_chat_history(user_id)

    collected = {}
    for h in history:
        if h['role'] == 'assistant':
            try:
                parsed = _parse_llm_json(h['content'])
                if parsed and parsed.get('criteria'):
                    c = parsed['criteria']
                    for k, v in c.items():
                        if v is not None and v != [] and v != '':
                            collected[k] = v
            except Exception:
                pass

    messages = []
    if not history:
        messages.append({"role": "assistant", "content": "Salut ! Je suis Lou, ton chasseur immobilier digital. Dis-moi ce que tu cherches et je me mets en chasse !"})
    for h in history:
        if h['role'] == 'assistant':
            try:
                parsed = _parse_llm_json(h['content'])
                if parsed and parsed.get('message'):
                    messages.append({"role": "assistant", "content": parsed['message']})
                else:
                    messages.append(h)
            except Exception:
                messages.append(h)
        else:
            messages.append(h)

    enriched_message = message
    if collected:
        reminder_parts = []
        if collected.get('zones'):
            cities = [z.get('city', '') for z in collected['zones'] if z.get('city')]
            if cities:
                reminder_parts.append(f"zones: {', '.join(cities)}")
        if collected.get('transaction'):
            reminder_parts.append(f"transaction: {collected['transaction']}")
        if collected.get('property_types'):
            reminder_parts.append(f"type: {', '.join(collected['property_types'])}")
        if collected.get('budget_max'):
            reminder_parts.append(f"budget max: {collected['budget_max']} CHF")
        if collected.get('budget_min'):
            reminder_parts.append(f"budget min: {collected['budget_min']} CHF")
        if collected.get('rooms_min'):
            reminder_parts.append(f"pieces min: {collected['rooms_min']}")
        if collected.get('rooms_max'):
            reminder_parts.append(f"pieces max: {collected['rooms_max']}")
        if collected.get('surface_min'):
            reminder_parts.append(f"surface min: {collected['surface_min']} m2")
        if collected.get('priorities'):
            reminder_parts.append(f"priorites: {', '.join(collected['priorities'])}")
        if reminder_parts:
            missing = []
            if not collected.get('zones'):
                missing.append('zone')
            if not collected.get('transaction'):
                missing.append('transaction')
            if not collected.get('property_types'):
                missing.append('type de bien')
            if not collected.get('budget_max') and not collected.get('budget_min'):
                missing.append('budget')
            if not collected.get('rooms_min'):
                missing.append('nombre de pieces')
            missing_str = f" | Critères manquants: {', '.join(missing)}" if missing else " | Tous les critères essentiels sont collectés!"
            enriched_message = f"[Critères déjà collectés: {', '.join(reminder_parts)}{missing_str}]\n{message}"

    messages.append({"role": "user", "content": enriched_message})

    try:
        chat_model = os.environ.get('CLAUDE_CHAT_MODEL', 'claude-sonnet-4-6')
        response = anthropic_client.messages.create(
            model=chat_model,
            max_tokens=800,
            system=LOU_SYSTEM_PROMPT,
            messages=messages
        )
        raw = response.content[0].text.strip()

        result = _parse_llm_json(raw)

        if result is None:
            result = {
                "message": raw,
                "suggestions": [], "criteria": {}, "profile_ready": False, "confirmed": False
            }

        result.setdefault("message", "...")
        result.setdefault("suggestions", [])
        result.setdefault("criteria", {})
        result.setdefault("profile_ready", False)
        result.setdefault("confirmed", False)

        # v6.3.2 étape 5 — résolution déterministe des zones + UX échec.
        # Tolérant : si ça throw, on log et on laisse passer la réponse LLM.
        try:
            result = _post_process_zones(result, user_id, is_anonymous)
        except Exception as e:
            log.warning(f"_post_process_zones failed for user={user_id}: {e}", exc_info=True)

        _save_chat_message(user_id, "user", message)
        _save_chat_message(user_id, "assistant", raw, criteria_json=result.get("criteria"))

        return jsonify(result)

    except Exception:
        log.exception(f"Chat error for user {user_id} (model={chat_model})")
        return jsonify({
            "message": "Problème technique, réessaie dans quelques secondes.",
            "suggestions": ["Réessayer"], "criteria": {},
            "profile_ready": False, "confirmed": False
        }), 500


@chat_bp.route('/api/chat/unresolved-choice', methods=['POST'])
def api_chat_unresolved_choice():
    """
    v6.3.2 étape 5 — enregistre le choix final de l'user après suggestions.

    Payload : { "log_id": int, "chosen": str | null }
      - chosen = nom de la commune finalement sélectionnée
      - chosen = null si l'user a abandonné (ferme le chat / change sujet)

    Pas d'auth stricte : le log_id est une clé opaque retournée par /api/chat
    dans unresolved_zones[i].log_id, donc une corruption demande de guesser
    un id précis. Impact d'un vandal : pollution du champ 'chosen' dans
    unresolved_locations (table d'audit interne, pas de data user).
    """
    # Rate-limit IP/user avant DB write (évite pollution massive de
    # unresolved_locations par un attaquant qui scripte le POST).
    rl_key, rl_is_anon = _rate_key()
    if not _check_rate_limit(rl_key, rl_is_anon):
        log.warning(f"Unresolved-choice rate-limited key={rl_key}")
        return jsonify({'error': 'rate_limited'}), 429

    data = request.json or {}
    row_id = data.get('log_id') or data.get('row_id')
    chosen = data.get('chosen')
    if not row_id:
        return jsonify({'error': 'log_id required'}), 400
    try:
        row_id = int(row_id)
    except (TypeError, ValueError):
        return jsonify({'error': 'log_id must be integer'}), 400
    conn = get_db()
    try:
        from geo_suggestions import update_unresolved_choice
        update_unresolved_choice(conn, row_id, chosen)
    finally:
        return_db(conn)
    return jsonify({'ok': True})


@chat_bp.route('/api/chat/reset', methods=['POST'])
def chat_reset():
    # Rate-limit (évite reset-spam qui re-force des /api/chat derrière).
    rl_key, rl_is_anon = _rate_key()
    if not _check_rate_limit(rl_key, rl_is_anon):
        log.warning(f"Chat-reset rate-limited key={rl_key}")
        return jsonify({'error': 'rate_limited'}), 429

    data = request.json or {}
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    if token:
        try:
            token_data = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
            uid = str(token_data['user_id'])
        except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
            session_id = data.get('session_id', 'anon')
            uid = f"anon-{session_id}"
    else:
        session_id = data.get('session_id', 'anon')
        uid = f"anon-{session_id}"
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("""
            DELETE FROM conversations
            WHERE user_id = %s OR session_id = %s
        """, (int(uid) if uid.isdigit() else None, uid))
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        cur.close()
        return_db(conn)
    return jsonify({"ok": True})
