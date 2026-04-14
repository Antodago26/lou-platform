"""Bon Home — Chatbot (Lou) Blueprint."""
import os
import json
import re
import time
import logging
from collections import defaultdict

import jwt
from flask import Blueprint, jsonify, request

from anthropic import Anthropic

from db import get_db, return_db
from helpers import validate_json, ChatRequest
from auth import JWT_SECRET

log = logging.getLogger('lou-app')
chat_bp = Blueprint('chat', __name__)

ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
anthropic_client = Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None

# Simple rate limiter for chat endpoint
_chat_rate = defaultdict(list)
CHAT_RATE_LIMIT = 20
CHAT_RATE_WINDOW = 60
ANON_RATE_LIMIT = 5


def _check_rate_limit(user_id, is_anonymous=False):
    """Simple in-memory rate limiter. Returns True if allowed."""
    now = time.time()
    timestamps = _chat_rate[user_id]
    _chat_rate[user_id] = [t for t in timestamps if now - t < CHAT_RATE_WINDOW]
    limit = ANON_RATE_LIMIT if is_anonymous else CHAT_RATE_LIMIT
    if len(_chat_rate[user_id]) >= limit:
        return False
    _chat_rate[user_id].append(now)
    return True


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

    if not _check_rate_limit(user_id, is_anonymous=is_anonymous):
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
        chat_model = os.environ.get('CLAUDE_CHAT_MODEL', 'claude-opus-4-5-20250929')
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

        _save_chat_message(user_id, "user", message)
        _save_chat_message(user_id, "assistant", raw, criteria_json=result.get("criteria"))

        return jsonify(result)

    except Exception as e:
        log.error(f"Chat error for user {user_id}: {e}")
        return jsonify({
            "message": "Problème technique, réessaie dans quelques secondes.",
            "suggestions": ["Réessayer"], "criteria": {},
            "profile_ready": False, "confirmed": False
        }), 500


@chat_bp.route('/api/chat/reset', methods=['POST'])
def chat_reset():
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
