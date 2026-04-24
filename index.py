from flask import Flask, request
import json, os, re, uuid, urllib.request, urllib.parse
from datetime import datetime

app = Flask(__name__)

# ── Configurazione ────────────────────────────────────────────────────────────
GROQ_KEY      = os.environ.get("GROQ_KEY")          # chiave Groq condivisa
ADMIN_TOKEN   = os.environ.get("ADMIN_BOT_TOKEN")   # bot admin di Lorenzo
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")     # tuo chat ID Telegram
SUPABASE_URL  = os.environ.get("SUPABASE_URL")      # https://xxx.supabase.co
SUPABASE_KEY  = os.environ.get("SUPABASE_KEY")      # service role key
APP_URL       = os.environ.get("APP_URL")            # https://tua-app.vercel.app

# ── Stato in memoria (per istanza Vercel) ─────────────────────────────────────
_conversazioni = {}  # token → {chat_id → {storia, ultimo}}
_attesa_date   = {}  # token → {chat_id → {nome, lingua}}
_upload_media  = {}  # token → {chat_id → {file_id, tipo, step, keywords}}
_attesa_corr   = {}  # token → {chat_id → guest_chat_id}
_admin_state   = {}  # chat_id → {step, data}
_client_cache  = {}  # token → {data, ts}
_info_cache    = {}  # client_id → {testo, ts}
CACHE_TTL = 300
MAX_CONV  = 10
SCADENZA  = 2  # ore


# ══════════════════════════════════════════════════════════════════════════════
# SUPABASE
# ══════════════════════════════════════════════════════════════════════════════

def sb(method, table, data=None, params=None, prefer=None):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": prefer or "return=representation"
    }
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    r = urllib.request.urlopen(req, timeout=10)
    raw = r.read()
    return json.loads(raw) if raw else []

def sb_upsert(table, data):
    return sb("POST", table, data=data,
              prefer="resolution=merge-duplicates,return=representation")

def get_client(token):
    """Recupera client dal DB con cache 5 min."""
    ora = datetime.now().timestamp()
    cached = _client_cache.get(token)
    if cached and (ora - cached["ts"]) < CACHE_TTL:
        return cached["data"]
    try:
        rows = sb("GET", "clients", params={"telegram_token": f"eq.{token}", "active": "eq.true", "select": "*"})
        data = rows[0] if rows else None
        _client_cache[token] = {"data": data, "ts": ora}
        return data
    except Exception:
        return None

def get_info(client_id):
    """Restituisce il contenuto apartment_content con cache 5 min."""
    ora = datetime.now().timestamp()
    cached = _info_cache.get(client_id)
    if cached and (ora - cached["ts"]) < CACHE_TTL:
        return cached["testo"]
    try:
        rows = sb("GET", "apartment_content", params={"client_id": f"eq.{client_id}", "select": "content"})
        testo = rows[0]["content"] if rows else ""
        _info_cache[client_id] = {"testo": testo, "ts": ora}
        return testo
    except Exception:
        return ""

def invalida_info_cache(client_id):
    _info_cache.pop(client_id, None)

def salva_info(client_id, content):
    try:
        sb_upsert("apartment_content", {
            "client_id": client_id,
            "content": content,
            "updated_at": datetime.now().strftime("%d/%m/%Y %H:%M")
        })
        invalida_info_cache(client_id)
        return True
    except Exception:
        return False

def aggiungi_qa(client_id, testo):
    """Appende una riga Q&A al contenuto."""
    attuale = get_info(client_id)
    data_oggi = datetime.now().strftime("%d/%m/%Y")
    nuova = f"\n# Aggiunto il {data_oggi}\n{testo}\n"
    return salva_info(client_id, attuale + nuova)

def get_media(client_id):
    try:
        return sb("GET", "media_items", params={"client_id": f"eq.{client_id}", "select": "*"})
    except Exception:
        return []

def salva_media(client_id, keywords, tipo, file_id, caption):
    try:
        sb("POST", "media_items", data={
            "id": str(uuid.uuid4()),
            "client_id": client_id,
            "keywords": keywords,
            "tipo": tipo,
            "file_id": file_id,
            "caption": caption
        })
        return True
    except Exception:
        return False

def trova_media(client_id, domanda):
    t = domanda.lower()
    for m in get_media(client_id):
        kw = [k.strip().lower() for k in m["keywords"].split(",")]
        if any(k in t for k in kw):
            return m
    return None

def get_booking(client_id, guest_chat_id):
    try:
        rows = sb("GET", "bookings", params={
            "client_id": f"eq.{client_id}",
            "guest_chat_id": f"eq.{str(guest_chat_id)}",
            "select": "*"
        })
        return rows[0] if rows else None
    except Exception:
        return None

def salva_booking(client_id, guest_chat_id, nome, checkin, checkout, lingua):
    try:
        sb_upsert("bookings", {
            "id": str(uuid.uuid4()),
            "client_id": client_id,
            "guest_chat_id": str(guest_chat_id),
            "nome": nome,
            "checkin": checkin,
            "checkout": checkout,
            "lingua": lingua
        })
        return True
    except Exception:
        return False

def aggiorna_daily_stats(client_id, domanda, lingua, chat_id):
    try:
        oggi = datetime.now().strftime("%d/%m/%Y")
        rows = sb("GET", "daily_stats", params={
            "client_id": f"eq.{client_id}",
            "stat_date": f"eq.{oggi}",
            "select": "*"
        })
        if rows:
            s = rows[0]
            lingue    = json.loads(s["lingue"])
            argomenti = json.loads(s["argomenti"])
            ospiti    = json.loads(s["ospiti"])
        else:
            s = {"client_id": client_id, "stat_date": oggi}
            lingue, argomenti, ospiti = {}, {}, []
        lingue[lingua] = lingue.get(lingua, 0) + 1
        topic = rileva_topic(domanda)
        argomenti[topic] = argomenti.get(topic, 0) + 1
        if str(chat_id) not in ospiti:
            ospiti.append(str(chat_id))
        sb_upsert("daily_stats", {
            **s,
            "totale": s.get("totale", 0) + 1,
            "lingue": json.dumps(lingue),
            "argomenti": json.dumps(argomenti),
            "ospiti": json.dumps(ospiti)
        })
    except Exception:
        pass

def get_daily_stats(client_id):
    try:
        oggi = datetime.now().strftime("%d/%m/%Y")
        rows = sb("GET", "daily_stats", params={
            "client_id": f"eq.{client_id}",
            "stat_date": f"eq.{oggi}",
            "select": "*"
        })
        return rows[0] if rows else None
    except Exception:
        return None

def get_all_clients():
    try:
        return sb("GET", "clients", params={"active": "eq.true", "select": "*"})
    except Exception:
        return []


# ══════════════════════════════════════════════════════════════════════════════
# STATO CONVERSAZIONI (in memoria, per token)
# ══════════════════════════════════════════════════════════════════════════════

def get_storia(token, chat_id):
    ora = datetime.now().timestamp()
    conv = _conversazioni.get(token, {}).get(str(chat_id))
    if conv and (ora - conv["ultimo"]) > SCADENZA * 3600:
        _conversazioni[token].pop(str(chat_id), None)
        return []
    return conv["storia"] if conv else []

def aggiorna_storia(token, chat_id, domanda, risposta):
    ora = datetime.now().timestamp()
    if token not in _conversazioni:
        _conversazioni[token] = {}
    storia = _conversazioni[token].get(str(chat_id), {}).get("storia", [])
    storia += [{"role": "user", "content": domanda}, {"role": "assistant", "content": risposta}]
    if len(storia) > MAX_CONV * 2:
        storia = storia[-(MAX_CONV * 2):]
    _conversazioni[token][str(chat_id)] = {"storia": storia, "ultimo": ora}


# ══════════════════════════════════════════════════════════════════════════════
# PARSING DATE & KEYWORDS
# ══════════════════════════════════════════════════════════════════════════════

MESI = {
    "january":1,"jan":1,"gennaio":1,"janvier":1,"enero":1,"januar":1,
    "february":2,"feb":2,"febbraio":2,"février":2,"fevrier":2,"febrero":2,"februar":2,
    "march":3,"mar":3,"marzo":3,"mars":3,"märz":3,"marz":3,
    "april":4,"apr":4,"aprile":4,"avril":4,"abril":4,
    "may":5,"maggio":5,"mai":5,"mayo":5,
    "june":6,"jun":6,"giugno":6,"juin":6,"junio":6,"juni":6,
    "july":7,"jul":7,"luglio":7,"juillet":7,"julio":7,"juli":7,
    "august":8,"aug":8,"agosto":8,"août":8,"aout":8,
    "september":9,"sep":9,"sept":9,"settembre":9,"septembre":9,"septiembre":9,
    "october":10,"oct":10,"ottobre":10,"octobre":10,"octubre":10,"oktober":10,
    "november":11,"nov":11,"novembre":11,"noviembre":11,
    "december":12,"dec":12,"dicembre":12,"décembre":12,"decembre":12,"diciembre":12,"dezember":12,
}

def estrai_date(testo):
    t = testo.lower()
    anno = datetime.now().year
    trovate = []
    for m in re.finditer(r'(\d{1,2})[/\-\.](\d{1,2})(?:[/\-\.](\d{2,4}))?', t):
        g, me = int(m.group(1)), int(m.group(2))
        a = int(m.group(3)) if m.group(3) else anno
        if a < 100: a += 2000
        if 1 <= g <= 31 and 1 <= me <= 12:
            trovate.append(f"{g:02d}/{me:02d}/{a}")
    nomi = "|".join(MESI.keys())
    for m in re.finditer(rf'(\d{{1,2}})\s+({nomi})(?:\s+(\d{{2,4}}))?', t):
        g = int(m.group(1))
        me = MESI[m.group(2)]
        a = int(m.group(3)) if m.group(3) else anno
        if a < 100: a += 2000
        c = f"{g:02d}/{me:02d}/{a}"
        if 1 <= g <= 31 and c not in trovate:
            trovate.append(c)
    return (trovate[0], trovate[1]) if len(trovate) >= 2 else (None, None)


# ══════════════════════════════════════════════════════════════════════════════
# AI (Groq)
# ══════════════════════════════════════════════════════════════════════════════

TOPIC_KW = {
    "wifi":         ["wifi","password","internet","réseau","mot de passe"],
    "check-in":     ["check-in","checkin","arrivo","arrivée","arrival","chiavi","key","keybox","codice"],
    "check-out":    ["check-out","checkout","partenza","départ","departure"],
    "parcheggio":   ["parcheggio","garage","box","parking","voiture","auto","car"],
    "spiaggia":     ["spiaggia","mare","beach","plage","playa","strand"],
    "supermercato": ["supermercato","spesa","supermarché","supermarket"],
    "ristorante":   ["ristorante","mangiare","cena","restaurant","dinner"],
    "lavatrice":    ["lavatrice","washing","machine à laver","lavadora","waschmaschine"],
    "emergenza":    ["emergenza","problema","aiuto","urgente","emergency","urgence"],
    "trasporti":    ["bus","treno","taxi","train","transport"],
}

def rileva_topic(domanda):
    t = domanda.lower()
    for topic, kws in TOPIC_KW.items():
        if any(k in t for k in kws):
            return topic
    return "altro"

def rileva_lingua(testo):
    t = " " + testo.lower() + " "
    p = {"french": 0, "english": 0, "spanish": 0, "german": 0}
    fr = ["bonjour","bonsoir","merci","comment","quelle","où","avez","pouvez","heure","clé","plage","voiture"]
    en = ["hello","hi ","thanks","thank you","where","what","how","is there","please","wifi","parking","beach"]
    es = ["hola","buenos","gracias","dónde","cómo","cuál","hay","puede","llegada","salida","playa"]
    de = ["hallo","guten","danke","bitte","gibt es","wie ","wo ist","können","strand","parkplatz"]
    for w in fr:
        if w in t: p["french"] += 1
    for w in en:
        if w in t: p["english"] += 1
    for w in es:
        if w in t: p["spanish"] += 1
    for w in de:
        if w in t: p["german"] += 1
    best = max(p, key=p.get)
    return best if p[best] > 0 else "italian"

SYSTEM_PROMPT = {
    "italian": "Sei un assistente virtuale per un appartamento in affitto. Rispondi SOLO con le informazioni qui sotto. Se non sai, di' che lo chiederai a {owner} e risponderai presto. Non dare mai il numero di telefono del proprietario se non esplicitamente richiesto. Chiamalo sempre '{owner}'. Sii cordiale e conciso.\n\nINFO:\n{info}",
    "english": "You are a virtual assistant for a vacation rental. Answer ONLY using the info below. If you don't know, say you'll ask {owner} and reply soon. Never share the owner's phone unless explicitly asked. Always call them '{owner}'. Be friendly and concise.\n\nINFO:\n{info}",
    "french":  "Tu es un assistant virtuel pour un appartement de location. Réponds UNIQUEMENT avec les infos ci-dessous. Si tu ne sais pas, dis que tu demanderas à {owner}. Ne partage jamais le numéro de téléphone sauf si demandé. Appelle toujours le propriétaire '{owner}'. Sois cordial et concis.\n\nINFOS:\n{info}",
    "spanish": "Eres un asistente virtual para un apartamento de alquiler. Responde SOLO con la info abajo. Si no sabes, di que preguntarás a {owner}. Nunca des el teléfono del propietario salvo si se pide. Llama siempre al propietario '{owner}'. Sé cordial y conciso.\n\nINFO:\n{info}",
    "german":  "Du bist ein virtueller Assistent für eine Ferienwohnung. Antworte NUR mit den untenstehenden Infos. Wenn du es nicht weißt, sag dass du {owner} fragen wirst. Gib niemals die Telefonnummer außer bei expliziter Anfrage. Nenne den Eigentümer immer '{owner}'. Sei freundlich und prägnant.\n\nINFOS:\n{info}",
}

FRASI_NON_SO = [
    "contatterò","contatterà","non ho questa informazione","non dispongo",
    "i'll contact","contact the owner","don't have that information",
    "je vais contacter","je n'ai pas cette information",
    "contactaré","no tengo esa información",
    "ich werde","werde den eigentümer",
]

def groq(messages, timeout=25):
    url = "https://api.groq.com/openai/v1/chat/completions"
    payload = {"model": "llama-3.1-8b-instant", "messages": messages}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GROQ_KEY}",
        "User-Agent": "groq-python/0.9.0"
    })
    r = urllib.request.urlopen(req, timeout=timeout)
    return json.loads(r.read())["choices"][0]["message"]["content"]

def chiedi_ai(domanda, info, owner_name, chat_id, token):
    lingua = rileva_lingua(domanda)
    system = SYSTEM_PROMPT.get(lingua, SYSTEM_PROMPT["english"]).format(
        owner=owner_name, info=info[:6000]
    )
    storia = get_storia(token, chat_id)
    return groq([{"role": "system", "content": system}, *storia, {"role": "user", "content": domanda}])

def bot_non_sa(r):
    return any(f in r.lower() for f in FRASI_NON_SO)

def traduci_keywords(kw_it):
    try:
        prompt = (
            f"Traduci queste parole chiave italiane in inglese, francese, spagnolo e tedesco.\n"
            f"Parole: {kw_it}\n"
            f"Rispondi SOLO con una riga CSV con tutte le varianti (originali + traduzioni), "
            f"separate da virgola, senza duplicati, tutto minuscolo."
        )
        result = groq([{"role": "user", "content": prompt}], timeout=10)
        tutte = [k.strip().lower() for k in result.split(",") if k.strip()]
        for o in [k.strip().lower() for k in kw_it.split(",") if k.strip()]:
            if o not in tutte:
                tutte.insert(0, o)
        return ", ".join(tutte)
    except Exception:
        return kw_it

def genera_benvenuto(benvenuto_it, lingua):
    if lingua == "italian":
        return benvenuto_it
    lingue_nomi = {"french":"French","english":"English","spanish":"Spanish","german":"German"}
    target = lingue_nomi.get(lingua, "English")
    return groq([
        {"role": "system", "content": f"Translate to {target}. Return ONLY the translation, keep emojis and structure."},
        {"role": "user", "content": benvenuto_it}
    ])


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM (per token)
# ══════════════════════════════════════════════════════════════════════════════

def tg(token, method, payload):
    url = f"https://api.telegram.org/bot{token}/{method}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    r = urllib.request.urlopen(req, timeout=10)
    return json.loads(r.read())

def send(token, chat_id, testo, parse_mode=None, remove_kb=False):
    p = {"chat_id": chat_id, "text": testo}
    if parse_mode:
        p["parse_mode"] = parse_mode
    if remove_kb:
        p["reply_markup"] = {"remove_keyboard": True}
    tg(token, "sendMessage", p)

def send_buttons(token, chat_id, testo, bottoni):
    tg(token, "sendMessage", {
        "chat_id": chat_id, "text": testo,
        "reply_markup": {"inline_keyboard": bottoni}
    })

def edit_msg(token, chat_id, msg_id, testo):
    try:
        tg(token, "editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": testo})
    except Exception:
        pass

def send_photo(token, chat_id, file_id, caption=""):
    tg(token, "sendPhoto", {"chat_id": chat_id, "photo": file_id, "caption": caption})

def send_video(token, chat_id, file_id, caption=""):
    tg(token, "sendVideo", {"chat_id": chat_id, "video": file_id, "caption": caption})

def set_webhook(bot_token, path):
    url = f"{APP_URL}{path}"
    tg(bot_token, "setWebhook", {"url": url, "allowed_updates": ["message","callback_query"]})


# ══════════════════════════════════════════════════════════════════════════════
# MESSAGGI MULTILINGUA
# ══════════════════════════════════════════════════════════════════════════════

DOMANDA_DATE = {
    "italian": "📅 Per aiutarti al meglio, puoi dirmi le date del tuo soggiorno?\n(Arrivo e partenza, anche in formato libero es. \"25 aprile - 28 aprile\")",
    "english": "📅 To assist you better, could you share your stay dates?\n(Arrival and departure, e.g. \"April 25 - April 28\")",
    "french":  "📅 Pour mieux vous aider, pourriez-vous m'indiquer les dates de votre séjour?\n(Arrivée et départ, ex. \"25 avril - 28 avril\")",
    "spanish": "📅 Para ayudarte mejor, ¿puedes indicarme las fechas de tu estancia?\n(Llegada y salida, ej. \"25 abril - 28 abril\")",
    "german":  "📅 Um Ihnen besser helfen zu können, nennen Sie mir bitte Ihre Aufenthaltsdaten.\n(An- und Abreise, z.B. \"25. April - 28. April\")",
}
CONFERMA_DATE = {
    "italian": "✅ Perfetto! Soggiorno registrato:\n📆 Arrivo: {checkin}\n🏁 Partenza: {checkout}\n\nSe le date non sono corrette scrivimi!",
    "english": "✅ Perfect! Stay registered:\n📆 Arrival: {checkin}\n🏁 Departure: {checkout}\n\nLet me know if the dates are wrong!",
    "french":  "✅ Parfait! Séjour enregistré:\n📆 Arrivée: {checkin}\n🏁 Départ: {checkout}\n\nDites-moi si les dates sont incorrectes!",
    "spanish": "✅ ¡Perfecto! Estancia registrada:\n📆 Llegada: {checkin}\n🏁 Salida: {checkout}\n\n¡Avísame si las fechas no son correctas!",
    "german":  "✅ Perfekt! Aufenthalt registriert:\n📆 Ankunft: {checkin}\n🏁 Abreise: {checkout}\n\nFalls die Daten falsch sind, lassen Sie es mich wissen!",
}
ERRORE_DATE = {
    "italian": "Non ho capito le date 😊 Puoi scrivermele così?\n\nArrivo: 25/04/2026\nPartenza: 28/04/2026",
    "english": "I didn't catch the dates 😊 Could you write them like this?\n\nArrival: 25/04/2026\nDeparture: 28/04/2026",
    "french":  "Je n'ai pas compris les dates 😊 Pourriez-vous les écrire ainsi?\n\nArrivée: 25/04/2026\nDépart: 28/04/2026",
    "spanish": "No entendí las fechas 😊 ¿Puedes escribirlas así?\n\nLlegada: 25/04/2026\nSalida: 28/04/2026",
    "german":  "Ich habe die Daten nicht verstanden 😊 Könnten Sie sie so schreiben?\n\nAnkunft: 25/04/2026\nAbreise: 28/04/2026",
}

SALUTI = ["ciao","salve","buongiorno","buonasera","hello","hi","hey","good morning",
          "good evening","bonjour","bonsoir","salut","hola","buenos","hallo","guten"]

def e_saluto(t):
    t = t.lower().strip()
    return any(t == s or t.startswith(s + " ") or t.startswith(s + ",") for s in SALUTI)

PAROLE_EMERGENZA = [
    "allagamento","perdita acqua","tubo rotto","guasto luce","senza corrente","blackout",
    "gas","odore gas","riscaldamento","caldaia","ascensore bloccato",
    "flood","water leak","no electricity","power cut","gas leak",
    "inondation","fuite d'eau","panne électrique","coupure de courant",
    "fuga de agua","sin electricidad","wasserrohrbruch","stromausfall","gasgeruch"
]
PAROLE_NEGATIVE = [
    "sporco","sporca","non funziona","rotto","rotta","puzza","disgustoso","pessimo",
    "terribile","inaccettabile","deluso","delusione","lamentela","vergogna","schifo",
    "dirty","broken","disgusting","terrible","awful","horrible","unacceptable",
    "disappointed","complaint","not working","filthy","stinks","unhappy",
    "sale","cassé","dégoûtant","horrible","inacceptable","déçu","plainte","ne fonctionne pas",
    "sucio","roto","asqueroso","terrible","inaceptable","decepcionado","queja",
    "schmutzig","kaputt","ekelhaft","schrecklich","inakzeptabel","enttäuscht","beschwerde",
]


# ══════════════════════════════════════════════════════════════════════════════
# HANDLER PRINCIPALE (logica client)
# ══════════════════════════════════════════════════════════════════════════════

def handle_client(body, token, client):
    client_id  = client["id"]
    owner_id   = client["owner_chat_id"]
    owner_name = client.get("owner_name", "il proprietario")

    # ── Callback query ──────────────────────────────────────────────────────
    cb = body.get("callback_query")
    if cb:
        cb_id     = cb["id"]
        cb_data   = cb.get("data", "")
        cb_cid    = cb["message"]["chat"]["id"]
        cb_mid    = cb["message"]["message_id"]
        cb_testo  = cb["message"].get("text", "")
        tg(token, "answerCallbackQuery", {"callback_query_id": cb_id})

        if cb_data == "SALVA_MEDIA":
            m_fid  = re.search(r'FILE_ID: (.+)', cb_testo)
            m_tipo = re.search(r'TIPO: (.+)', cb_testo)
            m_kw   = re.search(r'PAROLE_CHIAVE: (.+)', cb_testo)
            m_desc = re.search(r'DESCRIZIONE: (.+)', cb_testo)
            if m_fid and m_tipo and m_kw and m_desc:
                ok = salva_media(client_id, m_kw.group(1).strip(), m_tipo.group(1).strip(),
                                 m_fid.group(1).strip(), m_desc.group(1).strip())
                edit_msg(token, cb_cid, cb_mid,
                    f"✅ Media salvato!\nParole chiave: {m_kw.group(1).strip()}"
                    if ok else "❌ Errore nel salvataggio.")

        elif cb_data == "SALVA":
            match_dq = re.search(r'D: (.+?)\nR: (.+)', cb_testo, re.DOTALL)
            match_r  = re.search(r'R: (.+)', cb_testo, re.DOTALL)
            if match_dq:
                testo_da_salvare = f"{match_dq.group(1).strip()}: {match_dq.group(2).strip()}"
                msg_ok = f"🧠 Salvato!\n\n{testo_da_salvare}"
            elif match_r:
                testo_da_salvare = match_r.group(1).strip()
                msg_ok = f"✅ Info aggiunta:\n\n{testo_da_salvare}"
            else:
                testo_da_salvare = None
            if testo_da_salvare:
                ok = aggiungi_qa(client_id, testo_da_salvare)
                edit_msg(token, cb_cid, cb_mid, msg_ok if ok else "❌ Errore nel salvataggio.")

        elif cb_data.startswith("MODIFICA_DATE:"):
            guest_id = cb_data.split(":")[1]
            if token not in _attesa_corr: _attesa_corr[token] = {}
            _attesa_corr[token][str(cb_cid)] = guest_id
            m_nome = re.search(r'Ospite: (.+?) \[', cb_testo)
            edit_msg(token, cb_cid, cb_mid,
                f"✏️ Inviami le date corrette per {m_nome.group(1) if m_nome else 'l\'ospite'} nel formato:\n"
                f"25/04/2026 - 28/04/2026"
            )

        elif cb_data == "DATE_OK":
            edit_msg(token, cb_cid, cb_mid, cb_testo.split("\n\n")[0] + "\n\n✅ Date confermate!")

        elif cb_data == "RICOMINCIA_MEDIA":
            m_fid  = re.search(r'FILE_ID: (.+)', cb_testo)
            m_tipo = re.search(r'TIPO: (.+)', cb_testo)
            if m_fid and m_tipo:
                if token not in _upload_media: _upload_media[token] = {}
                _upload_media[token][str(cb_cid)] = {
                    "file_id": m_fid.group(1).strip(),
                    "tipo": m_tipo.group(1).strip(), "step": "keywords"
                }
                edit_msg(token, cb_cid, cb_mid,
                    "🔄 Ricominciamo!\n\n1️⃣ Scrivi le parole chiave in italiano:"
                )

        elif cb_data == "NO":
            if token in _upload_media: _upload_media[token].pop(str(cb_cid), None)
            edit_msg(token, cb_cid, cb_mid, "✅ Ok, non salvato.")

        return

    # ── Messaggio ───────────────────────────────────────────────────────────
    msg      = body.get("message", {})
    chat_id  = msg.get("chat", {}).get("id")
    testo    = msg.get("text", "")
    nome     = msg.get("from", {}).get("first_name", "Ospite")
    username = msg.get("from", {}).get("username", "")
    is_owner = str(chat_id) == str(owner_id)

    if not chat_id:
        return

    # ── Proprietario invia foto/video ──
    if is_owner and not testo:
        foto  = msg.get("photo")
        video = msg.get("video")
        doc   = msg.get("document")
        if foto:   file_id, tipo = foto[-1]["file_id"], "photo"
        elif video: file_id, tipo = video["file_id"], "video"
        elif doc:   file_id, tipo = doc["file_id"], "photo"
        else: return
        if token not in _upload_media: _upload_media[token] = {}
        _upload_media[token][str(chat_id)] = {"file_id": file_id, "tipo": tipo, "step": "keywords"}
        send(token, chat_id,
            f"📸 {'Foto' if tipo == 'photo' else 'Video'} ricevuto!\n\n"
            f"1️⃣ Scrivi le *parole chiave* in italiano che attiveranno questo media.\n"
            f"Separale con virgola.\n\nEsempio: `box, garage, parcheggio`"
        )
        return

    if not testo:
        return

    # ── /start o saluto ospite ──
    if testo == "/start" or (not is_owner and e_saluto(testo)):
        lingua = rileva_lingua(testo) if testo != "/start" else "italian"
        info   = get_info(client_id)
        # Cerca benvenuto nel contenuto
        match_benv = re.search(r'\[BENVENUTO\](.*?)\[/BENVENUTO\]', info, re.DOTALL)
        benvenuto_it = match_benv.group(1).strip() if match_benv else (
            f"Benvenuto! 😊 Sono l'assistente virtuale. Sono qui per aiutarti durante il tuo soggiorno.\n\n"
            f"Per qualsiasi domanda sono a disposizione!"
        )
        try:
            testo_benv = genera_benvenuto(benvenuto_it, lingua)
        except Exception:
            testo_benv = benvenuto_it
        send(token, chat_id, testo_benv, remove_kb=True)
        booking = get_booking(client_id, chat_id)
        if not booking:
            send(token, chat_id, DOMANDA_DATE.get(lingua, DOMANDA_DATE["english"]))
            if token not in _attesa_date: _attesa_date[token] = {}
            _attesa_date[token][str(chat_id)] = {"nome": nome, "lingua": lingua}
        return

    # ── Proprietario risponde a notifica ──
    if is_owner and msg.get("reply_to_message"):
        orig = msg["reply_to_message"].get("text", "")
        m_id = re.search(r'\[ID:(\d+)\]', orig)
        if m_id:
            send(token, int(m_id.group(1)), f"💬 {testo}")
            send(token, chat_id, "✅ Risposta inviata!")
            m_dom = re.search(r'❓ "?(.+?)"?(?:\n|$)', orig)
            if m_dom:
                send_buttons(token, chat_id,
                    f"💾 Vuoi salvare questa risposta?\n\nD: {m_dom.group(1).strip()}\nR: {testo}",
                    [[{"text":"✅ Sì, salva","callback_data":"SALVA"},{"text":"❌ No","callback_data":"NO"}]]
                )
            return

    # ── Flusso guidato upload media ──
    if is_owner and token in _upload_media and str(chat_id) in _upload_media[token] and not testo.startswith("/"):
        stato = _upload_media[token][str(chat_id)]
        if stato["step"] == "keywords":
            send(token, chat_id, "⏳ Traduco le parole chiave in tutte le lingue...")
            kw_complete = traduci_keywords(testo.strip())
            stato["keywords"] = kw_complete
            stato["step"] = "description"
            send(token, chat_id,
                f"✅ Parole chiave:\n`{kw_complete}`\n\n"
                f"2️⃣ Scrivi la *descrizione* che vedrà l'ospite:"
            )
            return
        elif stato["step"] == "description":
            descrizione = testo.strip()
            file_id = stato["file_id"]
            tipo    = stato["tipo"]
            keywords = stato["keywords"]
            del _upload_media[token][str(chat_id)]
            send_buttons(token, chat_id,
                f"💾 Riepilogo — vuoi salvare?\n\n"
                f"🔑 Parole chiave: {keywords}\n"
                f"📝 Descrizione: {descrizione}\n"
                f"📎 Tipo: {'Foto 📸' if tipo == 'photo' else 'Video 🎬'}\n\n"
                f"FILE_ID: {file_id}\nTIPO: {tipo}\nPAROLE_CHIAVE: {keywords}\nDESCRIZIONE: {descrizione}",
                [[
                    {"text":"✅ Sì, salva","callback_data":"SALVA_MEDIA"},
                    {"text":"✏️ Ricomincia","callback_data":"RICOMINCIA_MEDIA"},
                    {"text":"❌ Annulla","callback_data":"NO"}
                ]]
            )
            return

    # ── Correzione date ──
    if is_owner and token in _attesa_corr and str(chat_id) in _attesa_corr[token] and not testo.startswith("/"):
        guest_id = _attesa_corr[token].pop(str(chat_id))
        ci, co = estrai_date(testo)
        if ci and co:
            booking = get_booking(client_id, guest_id)
            nome_g  = booking["nome"] if booking else "Ospite"
            lingua_g = booking["lingua"] if booking else "italian"
            salva_booking(client_id, guest_id, nome_g, ci, co, lingua_g)
            send(token, chat_id, f"✅ Date aggiornate per {nome_g}!\n📆 {ci} → {co}")
        else:
            send(token, chat_id, "❌ Non ho capito le date. Formato: 25/04/2026 - 28/04/2026")
        return

    # ── Proprietario scrive info ──
    if is_owner and not msg.get("reply_to_message") and not testo.startswith("/"):
        send_buttons(token, chat_id,
            f"💾 Vuoi aggiungere questa info?\n\nR: {testo}",
            [[{"text":"✅ Sì, aggiungi","callback_data":"SALVA"},{"text":"❌ No","callback_data":"NO"}]]
        )
        return

    # ── /stats ──
    if testo == "/stats" and is_owner:
        s = get_daily_stats(client_id)
        if not s or s.get("totale", 0) == 0:
            send(token, chat_id, "📊 Nessun messaggio ricevuto oggi.")
            return
        lingue = json.loads(s["lingue"])
        argomenti = json.loads(s["argomenti"])
        ospiti = json.loads(s["ospiti"])
        bandiere = {"italian":"🇮🇹","french":"🇫🇷","english":"🇬🇧","spanish":"🇪🇸","german":"🇩🇪"}
        rl = " · ".join(f"{bandiere.get(l,'🌍')} {n}" for l,n in sorted(lingue.items(), key=lambda x:-x[1]))
        ra = "\n".join(f"  • {a.capitalize()}: {n}" for a,n in sorted(argomenti.items(), key=lambda x:-x[1])[:5])
        oggi = datetime.now().strftime("%d/%m/%Y")
        send(token, chat_id,
            f"📊 *Riepilogo {oggi}*\n\n"
            f"💬 Messaggi: *{s['totale']}*\n👥 Ospiti attivi: *{len(ospiti)}*\n\n"
            f"🌍 Lingue: {rl}\n\n🔥 Argomenti:\n{ra}",
            parse_mode="Markdown"
        )
        return

    if testo.startswith("/"):
        return

    # ── Ospite in attesa date ──
    if not is_owner and token in _attesa_date and str(chat_id) in _attesa_date[token]:
        ci, co = estrai_date(testo)
        if ci and co:
            info_att = _attesa_date[token].pop(str(chat_id))
            lingua   = info_att.get("lingua", "italian")
            send(token, chat_id, CONFERMA_DATE.get(lingua, CONFERMA_DATE["english"]).format(checkin=ci, checkout=co))
            try:
                salva_booking(client_id, chat_id, nome, ci, co, lingua)
            except Exception:
                pass
            nome_d = f"@{username}" if username else nome
            send_buttons(token, int(owner_id),
                f"📅 Nuova prenotazione!\n\nOspite: {nome_d} [ID:{chat_id}]\n📆 Check-in: {ci}\n🏁 Check-out: {co}",
                [[
                    {"text":"✏️ Modifica date","callback_data":f"MODIFICA_DATE:{chat_id}"},
                    {"text":"✅ Ok","callback_data":"DATE_OK"}
                ]]
            )
            return
        else:
            lingua = _attesa_date[token][str(chat_id)].get("lingua","italian")
            try:
                info  = get_info(client_id)
                reply = chiedi_ai(testo, info, owner_name, chat_id, token)
                aggiorna_storia(token, chat_id, testo, reply)
                send(token, chat_id, reply)
            except Exception:
                pass
            send(token, chat_id, ERRORE_DATE.get(lingua, ERRORE_DATE["english"]))
            return

    # ── Risposta AI ──
    try:
        info  = get_info(client_id)
        reply = chiedi_ai(testo, info, owner_name, chat_id, token)
        aggiorna_storia(token, chat_id, testo, reply)
        try:
            aggiorna_daily_stats(client_id, testo, rileva_lingua(testo), chat_id)
        except Exception:
            pass
    except Exception:
        reply = f"Mi dispiace, in questo momento non riesco a rispondere. Lo chiedo a {owner_name} e ti rispondo presto!"

    send(token, chat_id, reply)

    e_emerg = any(p in testo.lower() for p in PAROLE_EMERGENZA)
    e_negat = any(p in testo.lower() for p in PAROLE_NEGATIVE) and not e_emerg

    # ── Media automatici ──
    if not is_owner:
        m = trova_media(client_id, testo)
        if m:
            try:
                if m["tipo"] == "video": send_video(token, chat_id, m["file_id"], m["caption"])
                else:                    send_photo(token, chat_id, m["file_id"], m["caption"])
            except Exception:
                pass

    # ── Notifica proprietario ──
    if not is_owner:
        nome_d = f"@{username}" if username else nome
        try:
            if e_emerg:
                send(token, int(owner_id),
                    f"🚨🚨 EMERGENZA TECNICA 🚨🚨\n\nOspite: {nome_d} [ID:{chat_id}]\n\n❓ {testo}\n\n🤖 {reply}\n\n⚡ Rispondi subito premendo Rispondi.")
            elif e_negat:
                send(token, int(owner_id),
                    f"😤 OSPITE INSODDISFATTO\n\nOspite: {nome_d} [ID:{chat_id}]\n\n❓ {testo}\n\n🤖 {reply}\n\n👆 Premi Rispondi per contattarlo.")
            else:
                send(token, int(owner_id),
                    f"📩 {nome_d} [ID:{chat_id}]\n\n❓ {testo}\n\n🤖 {reply}")
        except Exception:
            pass

    # ── Alert "non sa rispondere" ──
    if not is_owner and bot_non_sa(reply) and not e_emerg:
        nome_d = f"@{username}" if username else nome
        send(token, int(owner_id),
            f"⚠️ RISPOSTA RICHIESTA\n\nL'ospite {nome_d} ha fatto una domanda a cui non so rispondere:\n\n"
            f"❓ \"{testo}\"\n\nPremi Rispondi e scrivi la tua risposta.\n[ID:{chat_id}]")


# ══════════════════════════════════════════════════════════════════════════════
# HANDLER BOT ADMIN (Lorenzo)
# ══════════════════════════════════════════════════════════════════════════════

def handle_admin(body):
    msg     = body.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    testo   = msg.get("text", "")

    if not chat_id or str(chat_id) != str(ADMIN_CHAT_ID):
        return  # sicurezza: solo Lorenzo

    stato = _admin_state.get(str(chat_id), {})
    step  = stato.get("step")

    # ── /nuovo ──
    if testo == "/nuovo":
        _admin_state[str(chat_id)] = {"step": "await_name", "data": {}}
        send(ADMIN_TOKEN, chat_id,
            "➕ *Nuovo cliente*\n\n"
            "1️⃣ Come si chiama il cliente o la struttura?\n"
            "Es: `Mario Rossi - Villa Antibes`",
            parse_mode="Markdown"
        )
        return

    if step == "await_name":
        stato["data"]["name"] = testo.strip()
        stato["step"] = "await_token"
        send(ADMIN_TOKEN, chat_id,
            f"✅ Nome: *{testo.strip()}*\n\n"
            f"2️⃣ Incolla il *token del bot Telegram* del cliente.\n"
            f"(Il cliente deve creare un bot con @BotFather e darti il token)",
            parse_mode="Markdown"
        )
        return

    if step == "await_token":
        stato["data"]["telegram_token"] = testo.strip()
        stato["step"] = "await_owner_id"
        send(ADMIN_TOKEN, chat_id,
            f"✅ Token ricevuto.\n\n"
            f"3️⃣ Qual è il *chat ID* del cliente?\n"
            f"(Il cliente deve scrivere a @userinfobot e mandarti il numero ID)",
            parse_mode="Markdown"
        )
        return

    if step == "await_owner_id":
        stato["data"]["owner_chat_id"] = testo.strip()
        stato["step"] = "await_owner_name"
        send(ADMIN_TOKEN, chat_id,
            f"✅ Chat ID: *{testo.strip()}*\n\n"
            f"4️⃣ Come si chiama il proprietario? (nome che il bot userà nelle risposte)\n"
            f"Es: `Mario`",
            parse_mode="Markdown"
        )
        return

    if step == "await_owner_name":
        stato["data"]["owner_name"] = testo.strip()
        stato["step"] = "await_confirm"
        d = stato["data"]
        send(ADMIN_TOKEN, chat_id,
            f"📋 *Riepilogo nuovo cliente:*\n\n"
            f"🏠 Nome: {d['name']}\n"
            f"👤 Proprietario: {d['owner_name']}\n"
            f"🤖 Token: `{d['telegram_token'][:20]}...`\n"
            f"💬 Chat ID: {d['owner_chat_id']}\n\n"
            f"Scrivi *CONFERMA* per salvare o *ANNULLA* per cancellare.",
            parse_mode="Markdown"
        )
        return

    if step == "await_confirm":
        if testo.strip().upper() == "CONFERMA":
            d = stato["data"]
            try:
                client_id = str(uuid.uuid4())
                sb("POST", "clients", data={
                    "id": client_id,
                    "name": d["name"],
                    "telegram_token": d["telegram_token"],
                    "owner_chat_id": d["owner_chat_id"],
                    "owner_name": d["owner_name"],
                    "active": True,
                    "created_at": datetime.now().strftime("%d/%m/%Y %H:%M")
                })
                sb_upsert("apartment_content", {
                    "client_id": client_id,
                    "content": f"# Appartamento {d['name']}\n# Aggiorna queste informazioni!\n\nNome struttura: {d['name']}\n",
                    "updated_at": datetime.now().strftime("%d/%m/%Y %H:%M")
                })
                # Registra webhook
                set_webhook(d["telegram_token"], f"/webhook/{d['telegram_token']}")
                _admin_state.pop(str(chat_id), None)
                send(ADMIN_TOKEN, chat_id,
                    f"✅ *Cliente attivato!*\n\n"
                    f"🏠 {d['name']} è ora online.\n\n"
                    f"📌 Prossimi passi per il cliente:\n"
                    f"1. Scrivere a @BotFather → /mybots → seleziona il bot → Edit Bot → Edit Description\n"
                    f"2. Mandare al bot le info dell'appartamento (wifi, check-in, ecc.)\n"
                    f"3. Condividere il link del bot agli ospiti",
                    parse_mode="Markdown"
                )
            except Exception as e:
                send(ADMIN_TOKEN, chat_id, f"❌ Errore durante il salvataggio: {e}")
        elif testo.strip().upper() == "ANNULLA":
            _admin_state.pop(str(chat_id), None)
            send(ADMIN_TOKEN, chat_id, "❌ Operazione annullata.")
        return

    # ── /clienti ──
    if testo == "/clienti":
        clienti = get_all_clients()
        if not clienti:
            send(ADMIN_TOKEN, chat_id, "Nessun cliente attivo.")
            return
        righe = "\n".join(
            f"{'✅' if c['active'] else '⏸'} *{c['name']}* — {c.get('owner_name','?')}\n"
            f"   📅 {c.get('created_at','')}"
            for c in clienti
        )
        send(ADMIN_TOKEN, chat_id, f"👥 *Clienti attivi: {len(clienti)}*\n\n{righe}", parse_mode="Markdown")
        return

    # ── /stats ──
    if testo == "/stats":
        clienti = get_all_clients()
        oggi = datetime.now().strftime("%d/%m/%Y")
        totale_msg = 0
        righe = []
        for c in clienti:
            s = get_daily_stats(c["id"])
            n = s["totale"] if s else 0
            totale_msg += n
            righe.append(f"  🏠 {c['name']}: {n} msg")
        send(ADMIN_TOKEN, chat_id,
            f"📊 *Stats di oggi — {oggi}*\n\n"
            f"💬 Messaggi totali: *{totale_msg}*\n\n" +
            "\n".join(righe),
            parse_mode="Markdown"
        )
        return

    # ── /pausa ──
    if testo.startswith("/pausa "):
        nome_cerca = testo[7:].strip().lower()
        clienti = get_all_clients()
        trovato = next((c for c in clienti if nome_cerca in c["name"].lower()), None)
        if trovato:
            sb("PATCH", "clients", data={"active": False},
               params={"id": f"eq.{trovato['id']}"})
            _client_cache.pop(trovato["telegram_token"], None)
            send(ADMIN_TOKEN, chat_id, f"⏸ *{trovato['name']}* messo in pausa.", parse_mode="Markdown")
        else:
            send(ADMIN_TOKEN, chat_id, "❌ Cliente non trovato. Usa /clienti per vedere i nomi.")
        return

    # ── /riattiva ──
    if testo.startswith("/riattiva "):
        nome_cerca = testo[10:].strip().lower()
        try:
            rows = sb("GET", "clients", params={"active": "eq.false", "select": "*"})
            trovato = next((c for c in rows if nome_cerca in c["name"].lower()), None)
            if trovato:
                sb("PATCH", "clients", data={"active": True},
                   params={"id": f"eq.{trovato['id']}"})
                set_webhook(trovato["telegram_token"], f"/webhook/{trovato['telegram_token']}")
                _client_cache.pop(trovato["telegram_token"], None)
                send(ADMIN_TOKEN, chat_id, f"✅ *{trovato['name']}* riattivato.", parse_mode="Markdown")
            else:
                send(ADMIN_TOKEN, chat_id, "❌ Cliente non trovato tra quelli in pausa.")
        except Exception as e:
            send(ADMIN_TOKEN, chat_id, f"❌ Errore: {e}")
        return

    # ── /setinfo ──
    if testo.startswith("/setinfo"):
        nome_cerca = testo[8:].strip().lower()
        if not nome_cerca:
            send(ADMIN_TOKEN, chat_id, "Uso: /setinfo <nome cliente>\nEs: /setinfo mario rossi")
            return
        clienti = get_all_clients()
        trovato = next((c for c in clienti if nome_cerca in c["name"].lower()), None)
        if not trovato:
            send(ADMIN_TOKEN, chat_id, "❌ Cliente non trovato. Usa /clienti per vedere i nomi.")
            return
        attuale = get_info(trovato["id"])
        _admin_state[str(chat_id)] = {"step": "await_info", "data": {"client": trovato}}
        anteprima = attuale[:400] + "..." if len(attuale) > 400 else attuale
        send(ADMIN_TOKEN, chat_id,
            f"📝 *{trovato['name']}*\n\n"
            f"Contenuto attuale:\n```\n{anteprima}\n```\n\n"
            f"Inviami il nuovo contenuto completo per sovrascriverlo,\n"
            f"oppure scrivi *AGGIUNGI:* seguito dal testo per aggiungere in fondo.",
            parse_mode="Markdown"
        )
        return

    if step == "await_info":
        client = stato["data"]["client"]
        if testo.startswith("AGGIUNGI:"):
            aggiunta = testo[9:].strip()
            aggiungi_qa(client["id"], aggiunta)
            _admin_state.pop(str(chat_id), None)
            send(ADMIN_TOKEN, chat_id, f"✅ Info aggiunta a *{client['name']}*!", parse_mode="Markdown")
        else:
            salva_info(client["id"], testo)
            _admin_state.pop(str(chat_id), None)
            send(ADMIN_TOKEN, chat_id, f"✅ Contenuto aggiornato per *{client['name']}*!", parse_mode="Markdown")
        return

    # ── Aiuto ──
    send(ADMIN_TOKEN, chat_id,
        "🤖 *Comandi disponibili:*\n\n"
        "/nuovo — Aggiungi un nuovo cliente\n"
        "/clienti — Lista tutti i clienti\n"
        "/stats — Statistiche di oggi\n"
        "/setinfo <nome> — Modifica info appartamento cliente\n"
        "/pausa <nome> — Metti in pausa un bot\n"
        "/riattiva <nome> — Riattiva un bot",
        parse_mode="Markdown"
    )


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES FLASK
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/webhook/<token>", methods=["POST"])
def webhook_client(token):
    try:
        body   = request.get_json(force=True)
        client = get_client(token)
        if client:
            handle_client(body, token, client)
    except Exception:
        pass
    return "ok"

@app.route("/admin", methods=["POST"])
def webhook_admin():
    try:
        body = request.get_json(force=True)
        handle_admin(body)
    except Exception:
        pass
    return "ok"

@app.route("/daily-report", methods=["GET", "POST"])
def daily_report():
    """Vercel Cron: invia riepilogo giornaliero a ogni cliente."""
    try:
        clienti = get_all_clients()
        oggi    = datetime.now().strftime("%d/%m/%Y")
        for c in clienti:
            try:
                s = get_daily_stats(c["id"])
                if not s or s.get("totale", 0) == 0:
                    testo = f"📊 *Riepilogo {oggi}*\n\nNessun messaggio ricevuto oggi. 😴"
                else:
                    lingue    = json.loads(s["lingue"])
                    argomenti = json.loads(s["argomenti"])
                    ospiti    = json.loads(s["ospiti"])
                    bandiere  = {"italian":"🇮🇹","french":"🇫🇷","english":"🇬🇧","spanish":"🇪🇸","german":"🇩🇪"}
                    rl = " · ".join(f"{bandiere.get(l,'🌍')} {n}" for l,n in sorted(lingue.items(), key=lambda x:-x[1]))
                    ra = "\n".join(f"  • {a.capitalize()}: {n}" for a,n in sorted(argomenti.items(), key=lambda x:-x[1])[:5])
                    testo = (
                        f"📊 *Riepilogo {oggi} — {c['name']}*\n\n"
                        f"💬 Messaggi: *{s['totale']}*\n"
                        f"👥 Ospiti attivi: *{len(ospiti)}*\n\n"
                        f"🌍 Lingue: {rl}\n\n🔥 Argomenti:\n{ra}"
                    )
                send(c["telegram_token"], int(c["owner_chat_id"]), testo, parse_mode="Markdown")
            except Exception:
                pass
    except Exception:
        pass
    return "ok"

@app.route("/")
def health():
    return "BotSaaS attivo ✓"
