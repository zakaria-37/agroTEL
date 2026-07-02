#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════╗
║   AgroTel — Serveur Backend Complet              ║
║   Base de données SQLite + Login + Capteurs      ║
╚══════════════════════════════════════════════════╝

Installation (une seule fois dans le terminal VS Code) :
    pip install groq   (optionnel, seulement pour le chatbot)

Lancement :
    python serveur.py

Accès :
    http://localhost:5050
"""

import json, sqlite3, hashlib, hmac, os, secrets, smtplib, threading, time, base64
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
import urllib.request, urllib.error
try:
    import paho.mqtt.client as mqtt_client
    MQTT_DISPONIBLE = True
except ImportError:
    MQTT_DISPONIBLE = False
    print("  ⚠️  paho-mqtt non installé — lancez : pip install paho-mqtt")

# ════════════════════════════════════════════
#  CONFIGURATION — chargée depuis l'environnement
#  (fichier .env local, ou vraies variables d'env en production)
#  ⚠️  NE JAMAIS remettre de secrets en dur ici.
# ════════════════════════════════════════════

def _charger_dotenv(chemin=".env"):
    """Charge un fichier .env minimal (KEY=VALUE) sans dépendance externe."""
    chemin_absolu = os.path.abspath(chemin)
    if not os.path.exists(chemin):
        print(f"  ⚠️  Fichier .env INTROUVABLE à cet emplacement : {chemin_absolu}")
        print(f"      → Vérifiez que le fichier .env est bien dans le même dossier que serveur.py,")
        print(f"      → et que le serveur est lancé depuis ce dossier (cd vers le dossier du projet).")
        return
    print(f"  📄 Fichier .env trouvé : {chemin_absolu}")
    nb_cles_chargees = 0
    with open(chemin, "r", encoding="utf-8-sig") as f:
        for ligne in f:
            ligne = ligne.strip()
            if not ligne or ligne.startswith("#") or "=" not in ligne:
                continue
            cle, _, val = ligne.partition("=")
            cle, val = cle.strip(), val.strip().strip('"').strip("'")
            if cle:
                os.environ.setdefault(cle, val)
                nb_cles_chargees += 1
    print(f"  ✅ {nb_cles_chargees} variable(s) chargée(s) depuis .env")

_charger_dotenv()

# Clé API pour le chatbot — API Groq officielle : https://console.groq.com
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_API_URL = os.environ.get("GROQ_API_URL", "https://api.groq.com/openai/v1/chat/completions")
GROQ_MODEL_CHAT   = os.environ.get("GROQ_MODEL_CHAT", "llama-3.3-70b-versatile")
GROQ_MODEL_VISION = os.environ.get("GROQ_MODEL_VISION", "meta-llama/llama-4-scout-17b-16e-instruct")

# Email Gmail expéditeur — utilisez un "mot de passe d'application" Gmail,
# jamais votre vrai mot de passe (myaccount.google.com → Sécurité → Mots de passe des applications)
EMAIL_EXPEDITEUR    = os.environ.get("EMAIL_EXPEDITEUR", "")
EMAIL_MOT_DE_PASSE  = os.environ.get("EMAIL_MOT_DE_PASSE", "")
EMAIL_DESTINATAIRES = [e.strip() for e in os.environ.get("EMAIL_DESTINATAIRES", "").split(",") if e.strip()]

# Clé secrète que l'ESP32-CAM doit envoyer pour poster une alerte PIR
# (évite que n'importe qui sur internet puisse spammer /api/alerte-pir)
DEVICE_API_KEY = os.environ.get("DEVICE_API_KEY", "")

# Origine(s) autorisée(s) pour les requêtes CORS, séparées par une virgule.
# En production, mettez l'URL exacte de votre site, ex: "https://monsite.com"
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]

# Mot de passe admin initial. Si absent, un mot de passe aléatoire est généré
# et affiché UNE SEULE FOIS au premier démarrage.
ADMIN_PASSWORD_ENV = os.environ.get("ADMIN_PASSWORD", "")

if not GROQ_API_KEY:
    print("  ⚠️  GROQ_API_KEY absente de l'environnement — chatbot désactivé (voir .env.example)")
else:
    print(f"  🔑 GROQ_API_KEY détectée ({GROQ_API_KEY[:6]}...{GROQ_API_KEY[-4:]}, {len(GROQ_API_KEY)} caractères)")
if not EMAIL_EXPEDITEUR or not EMAIL_MOT_DE_PASSE:
    print("  ⚠️  Email non configuré (EMAIL_EXPEDITEUR / EMAIL_MOT_DE_PASSE absents) — alertes email désactivées")
if not DEVICE_API_KEY:
    print("  ⚠️  DEVICE_API_KEY absente — l'endpoint /api/alerte-pir reste OUVERT à quiconque. Définissez-la avant la mise en ligne.")
if ALLOWED_ORIGINS == ["*"]:
    print("  ⚠️  ALLOWED_ORIGINS='*' — à restreindre à votre domaine avant la mise en production")

# Seuils d'alerte (modifiez selon vos plantes)
SEUIL_TEMP_MAX = 35.0   # °C  alerte si température trop haute
SEUIL_TEMP_MIN = 10.0   # °C  alerte si température trop basse
SEUIL_HUM_MAX  = 90.0   # %   alerte si humidité trop élevée
SEUIL_HUM_MIN  = 30.0   # %   alerte si humidité trop basse
SEUIL_SOL_SEC  = 200    # /1024  alerte sol trop sec

# Seuils d'automatisation actionneurs (modifiables via la plateforme)
seuils_auto = {
    "fan_temp_on":   27.0,   # °C  ventilo ON si temp >= cette valeur
    "fan_temp_off":  24.0,   # °C  ventilo OFF si temp <= cette valeur
    "valve_sol_on":  700,    # /1024  vanne ON si sol > cette valeur
    "valve_sol_off": 400,    # /1024  vanne OFF si sol <= cette valeur
    "pompe_sol_on":  300,    # /1024  pompe ON si sol < cette valeur
    "alert_temp_max":35.0,   # °C  alerte email temp haute
    "alert_temp_min":10.0,   # °C  alerte email temp basse
    "alert_hum_max": 90.0,   # %   alerte email humidité haute
    "alert_hum_min": 30.0,   # %   alerte email humidité basse
    "alert_sol_sec": 200,    # /1024 alerte email sol sec
}

# ════════════════════════════════════════════
#  MQTT — HiveMQ broker
# ════════════════════════════════════════════
MQTT_BROKER = "broker.hivemq.com"
MQTT_PORT   = 1883
MQTT_CLIENT_ID = "AgroTel_Serveur"

# ════════════════════════════════════════════

PORT      = int(os.environ.get("PORT", 5050))
DB_FILE   = os.environ.get("DB_FILE", "smartfarm.db")

# ── Réinitialisation forcée de la base (variable d'env temporaire) ──
if os.environ.get("RESET_DB", "").lower() == "true":
    if os.path.exists(DB_FILE):
        os.remove(DB_FILE)
        print(f"  🗑️  RESET_DB=true → {DB_FILE} supprimé, la base va être recréée proprement.")
    else:
        print(f"  ℹ️  RESET_DB=true → aucun {DB_FILE} existant, rien à supprimer.")
HTML_FILE = "index.html"

# Sessions actives en mémoire  { token -> {user_id, username, role, cree_le} }
sessions = {}
SESSION_DUREE_MAX = 24 * 3600  # 24h — au-delà, le token expire et il faut se reconnecter

# Anti brute-force sur /api/login  { ip -> [timestamps des échecs] }
_login_echecs = {}
LOGIN_MAX_ESSAIS = 5
LOGIN_FENETRE    = 15 * 60  # 15 minutes

def _login_bloque(ip):
    maintenant = time.time()
    essais = [t for t in _login_echecs.get(ip, []) if maintenant - t < LOGIN_FENETRE]
    _login_echecs[ip] = essais
    if len(essais) >= LOGIN_MAX_ESSAIS:
        attente = int(LOGIN_FENETRE - (maintenant - essais[0]))
        return True, max(attente, 1)
    return False, 0

def _login_echec(ip):
    _login_echecs.setdefault(ip, []).append(time.time())

def _login_reussi(ip):
    _login_echecs.pop(ip, None)

# Dernier état connu des capteurs et actionneurs (mis à jour par MQTT)
etat_live = {
    "temperature": None,
    "humidite":    None,
    "sol":         None,
    "ventilo":     0,
    "vanne":       0,
    "pompe":       0,
    "derniere_maj": None
}


# ══════════════════════════════════════════
#   BASE DE DONNÉES SQLite
# ══════════════════════════════════════════
def init_db():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()

    # Table utilisateurs
    cur.execute("""
        CREATE TABLE IF NOT EXISTS utilisateurs (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            username  TEXT UNIQUE NOT NULL,
            password  TEXT NOT NULL,
            role      TEXT NOT NULL DEFAULT 'operateur',
            created   TEXT DEFAULT (datetime('now'))
        )
    """)

    # Table capteurs — historique 1 mois
    cur.execute("""
        CREATE TABLE IF NOT EXISTS capteurs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT DEFAULT (datetime('now')),
            temperature REAL,
            humidite    REAL,
            sol         INTEGER,
            ventilo     INTEGER DEFAULT 0,
            vanne       INTEGER DEFAULT 0,
            pompe       INTEGER DEFAULT 0
        )
    """)

    # Table alertes sécurité PIR — photos intrus
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alertes_securite (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT (datetime('now')),
            photo     BLOB,
            cam_ip    TEXT
        )
    """)

    # Table seuils automatisation — modifiables via la plateforme
    cur.execute("""
        CREATE TABLE IF NOT EXISTS seuils (
            cle   TEXT PRIMARY KEY,
            valeur REAL NOT NULL,
            modifie_par TEXT DEFAULT 'system',
            modifie_le  TEXT DEFAULT (datetime('now'))
        )
    """)

    # Insérer les seuils par défaut si la table est vide
    cur.execute("SELECT COUNT(*) FROM seuils")
    if cur.fetchone()[0] == 0:
        defaults = [
            ("fan_temp_on",    27.0,  "system"),
            ("fan_temp_off",   24.0,  "system"),
            ("valve_sol_on",   700.0, "system"),
            ("valve_sol_off",  400.0, "system"),
            ("pompe_sol_on",   300.0, "system"),
            ("alert_temp_max", 35.0,  "system"),
            ("alert_temp_min", 10.0,  "system"),
            ("alert_hum_max",  90.0,  "system"),
            ("alert_hum_min",  30.0,  "system"),
            ("alert_sol_sec",  200.0, "system"),
        ]
        cur.executemany(
            "INSERT INTO seuils (cle, valeur, modifie_par) VALUES (?,?,?)", defaults
        )
        print("  ✅ Seuils par défaut insérés en base")

    # Créer admin par défaut si base vide
    cur.execute("SELECT COUNT(*) FROM utilisateurs")
    if cur.fetchone()[0] == 0:
        mot_de_passe_initial = ADMIN_PASSWORD_ENV or secrets.token_urlsafe(9)
        cur.execute(
            "INSERT INTO utilisateurs (username, password, role) VALUES (?,?,?)",
            ("admin", hash_password(mot_de_passe_initial), "admin")
        )
        if ADMIN_PASSWORD_ENV:
            print("  ✅ Admin créé  →  user: admin  |  mot de passe : celui défini dans ADMIN_PASSWORD")
        else:
            print("  ═" * 26)
            print(f"  ✅ Admin créé  →  user: admin  |  mot de passe : {mot_de_passe_initial}")
            print("     ⚠️  Notez ce mot de passe maintenant, il ne sera plus jamais affiché.")
            print("     Changez-le dès votre première connexion (menu profil).")
            print("  ═" * 26)

    con.commit()
    con.close()
    print(f"  ✅ Base de données '{DB_FILE}' prête")


def hash_password(pwd, salt=None):
    """PBKDF2-HMAC-SHA256 salé — remplace l'ancien SHA256 nu (vulnérable aux rainbow tables)."""
    if salt is None:
        salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"), salt.encode("utf-8"), 200_000)
    return f"pbkdf2${salt}${digest.hex()}"


def verifier_mot_de_passe(pwd, stored):
    """Vérifie un mot de passe contre un hash stocké.
    Supporte aussi l'ancien format SHA256 nu pour permettre la migration
    transparente des comptes créés avant ce correctif (voir login)."""
    if stored.startswith("pbkdf2$"):
        _, salt, hexdigest = stored.split("$", 2)
        candidat = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"), salt.encode("utf-8"), 200_000).hex()
        return hmac.compare_digest(candidat, hexdigest)
    # Ancien format hérité — sha256 nu sans sel
    return hmac.compare_digest(hashlib.sha256(pwd.encode()).hexdigest(), stored)


def get_db():
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    return con


def charger_seuils():
    """Charge les seuils depuis la base de données dans la variable globale."""
    global seuils_auto, SEUIL_TEMP_MAX, SEUIL_TEMP_MIN, SEUIL_HUM_MAX, SEUIL_HUM_MIN, SEUIL_SOL_SEC
    try:
        con = get_db()
        rows = con.execute("SELECT cle, valeur FROM seuils").fetchall()
        con.close()
        for row in rows:
            seuils_auto[row["cle"]] = row["valeur"]
        # Synchroniser les variables SEUIL_* globales
        SEUIL_TEMP_MAX = seuils_auto["alert_temp_max"]
        SEUIL_TEMP_MIN = seuils_auto["alert_temp_min"]
        SEUIL_HUM_MAX  = seuils_auto["alert_hum_max"]
        SEUIL_HUM_MIN  = seuils_auto["alert_hum_min"]
        SEUIL_SOL_SEC  = seuils_auto["alert_sol_sec"]
        print(f"  ✅ Seuils chargés depuis la base")
    except Exception as e:
        print(f"  ⚠️  Impossible de charger les seuils: {e}")


# ══════════════════════════════════════════
#   CLIENT MQTT — reçoit les données des ESP
# ══════════════════════════════════════════

# Verrou pour éviter les écritures SQLite simultanées depuis MQTT + HTTP
_db_lock = threading.Lock()

def mqtt_on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("  ✅ MQTT connecté au broker HiveMQ")
        topics = [
            ("agrotel/temp",        0),
            ("agrotel/hum",         0),
            ("agrotel/soil",        0),
            ("agrotel/state/fan",   0),
            ("agrotel/state/valve", 0),
            ("agrotel/state/pompe", 0),
        ]
        client.subscribe(topics)
        print(f"  📡 Abonné à {len(topics)} topics MQTT")
    else:
        print(f"  ❌ Echec connexion MQTT rc={rc}")

def mqtt_on_disconnect(client, userdata, rc):
    if rc != 0:
        print(f"  ⚠️  MQTT déconnecté (rc={rc}) — reconnexion auto...")

def mqtt_on_message(client, userdata, msg):
    global etat_live
    topic = msg.topic
    try:
        valeur = msg.payload.decode("utf-8").strip()
    except Exception:
        return

    # ── Mise à jour de l'état live ──
    if topic == "agrotel/temp":
        etat_live["temperature"]  = float(valeur)
    elif topic == "agrotel/hum":
        etat_live["humidite"]     = float(valeur)
    elif topic == "agrotel/soil":
        etat_live["sol"]          = int(valeur)
    elif topic == "agrotel/state/fan":
        etat_live["ventilo"]      = int(valeur)
    elif topic == "agrotel/state/valve":
        etat_live["vanne"]        = int(valeur)
    elif topic == "agrotel/state/pompe":
        etat_live["pompe"]        = int(valeur)

    etat_live["derniere_maj"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── Enregistrer en base dès qu'on a les 3 capteurs ──
    t = etat_live["temperature"]
    h = etat_live["humidite"]
    s = etat_live["sol"]

    if t is not None and h is not None and s is not None:
        try:
            with _db_lock:
                con = sqlite3.connect(DB_FILE)
                con.execute(
                    "INSERT INTO capteurs (temperature,humidite,sol,ventilo,vanne,pompe) VALUES (?,?,?,?,?,?)",
                    (t, h, s,
                     etat_live["ventilo"],
                     etat_live["vanne"],
                     etat_live["pompe"])
                )
                con.execute("DELETE FROM capteurs WHERE timestamp < datetime('now', '-31 days')")
                con.commit()
                con.close()
            print(f"  💾 MQTT→DB  Temp:{t}°C  Hum:{h}%  Sol:{s}  "
                  f"Fan:{etat_live['ventilo']}  Vanne:{etat_live['vanne']}  Pompe:{etat_live['pompe']}")
            # Vérifier les seuils d'alerte
            threading.Thread(
                target=verifier_alertes,
                args=(t, h, s,
                      etat_live["ventilo"],
                      etat_live["vanne"],
                      etat_live["pompe"]),
                daemon=True
            ).start()
        except Exception as e:
            print(f"  ❌ Erreur écriture DB depuis MQTT: {e}")

def publier_commande(topic, valeur):
    """Publie une commande vers l'ESP32 via MQTT."""
    if mqtt_instance and mqtt_instance.is_connected():
        mqtt_instance.publish(topic, str(valeur), qos=1)
        print(f"  📤 MQTT publish → [{topic}] : {valeur}")
        return True
    print(f"  ⚠️  MQTT non connecté — commande [{topic}] ignorée")
    return False

mqtt_instance = None

def demarrer_mqtt():
    global mqtt_instance
    if not MQTT_DISPONIBLE:
        return
    client = mqtt_client.Client(client_id=MQTT_CLIENT_ID, clean_session=True)
    client.on_connect    = mqtt_on_connect
    client.on_disconnect = mqtt_on_disconnect
    client.on_message    = mqtt_on_message
    mqtt_instance = client
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
        client.loop_forever()   # bloque — lancé dans un thread daemon
    except Exception as e:
        print(f"  ❌ MQTT impossible de se connecter: {e}")


# ══════════════════════════════════════════
#   SERVEUR HTTP
# ══════════════════════════════════════════
class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"  [{datetime.now().strftime('%H:%M:%S')}] {fmt % args}")

    def cors(self):
        origin = self.headers.get("Origin", "")
        if ALLOWED_ORIGINS == ["*"]:
            self.send_header("Access-Control-Allow-Origin", "*")
        elif origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Device-Key")

    def do_OPTIONS(self):
        self.send_response(200)
        self.cors()
        self.end_headers()

    # ────────────────── GET ──────────────────
    def do_GET(self):
        path = self.path.split("?")[0]

        if path in ("/", "/index.html"):
            self._serve_file(HTML_FILE, "text/html; charset=utf-8")

        elif path == "/style.css":
            self._serve_file("style.css", "text/css; charset=utf-8")

        elif path == "/chat-storage.js":
            self._serve_file("chat-storage.js", "application/javascript; charset=utf-8")

        # État live (données MQTT temps réel)
        elif path == "/api/live":
            if not self._auth(): return
            self._json(etat_live)

        # Historique capteurs (30 derniers jours)
        elif path == "/api/capteurs":
            if not self._auth(): return
            con = get_db()
            rows = con.execute("""
                SELECT timestamp, temperature, humidite, sol, ventilo, vanne, pompe
                FROM capteurs
                WHERE timestamp >= datetime('now', '-30 days')
                ORDER BY timestamp DESC LIMIT 2000
            """).fetchall()
            con.close()
            self._json({"capteurs": [dict(r) for r in rows]})

        # Statistiques du mois
        elif path == "/api/stats":
            if not self._auth(): return
            con = get_db()
            s = con.execute("""
                SELECT COUNT(*) total,
                       ROUND(AVG(temperature),1) avg_temp,
                       ROUND(MIN(temperature),1) min_temp,
                       ROUND(MAX(temperature),1) max_temp,
                       ROUND(AVG(humidite),1)    avg_hum,
                       ROUND(MIN(humidite),1)    min_hum,
                       ROUND(MAX(humidite),1)    max_hum,
                       ROUND(AVG(sol),0)         avg_sol
                FROM capteurs
                WHERE timestamp >= datetime('now', '-30 days')
            """).fetchone()
            con.close()
            self._json(dict(s))

        # Liste utilisateurs (admin uniquement)
        elif path == "/api/utilisateurs":
            sess = self._auth()
            if not sess: return
            if sess["role"] != "admin":
                return self._json({"error": "Accès refusé"}, 403)
            con = get_db()
            rows = con.execute(
                "SELECT id, username, role, created FROM utilisateurs ORDER BY id"
            ).fetchall()
            con.close()
            self._json({"utilisateurs": [dict(r) for r in rows]})

        # Seuils automatisation — lecture
        elif path == "/api/seuils":
            if not self._auth(): return
            con = get_db()
            rows = con.execute(
                "SELECT cle, valeur, modifie_par, modifie_le FROM seuils ORDER BY cle"
            ).fetchall()
            con.close()
            self._json({"seuils": {r["cle"]: {"valeur": r["valeur"], "modifie_par": r["modifie_par"], "modifie_le": r["modifie_le"]} for r in rows}})

        # Vérifier session courante
        elif path == "/api/me":
            sess = self._auth()
            if not sess: return
            self._json({"username": sess["username"], "role": sess["role"]})

        # Alertes sécurité PIR — compteur léger pour polling temps réel
        elif path == "/api/alertes-count":
            if not self._auth(): return
            con = get_db()
            row = con.execute(
                "SELECT COUNT(*) as total, MAX(id) as last_id FROM alertes_securite"
            ).fetchone()
            con.close()
            self._json({"total": row["total"], "last_id": row["last_id"] or 0})

        # Alertes sécurité PIR — nouvelles depuis un ID donné (polling incrémental)
        elif path.startswith("/api/alertes-since/"):
            if not self._auth(): return
            try:
                since_id = int(path.split("/")[-1])
            except ValueError:
                return self._json({"error": "ID invalide"}, 400)
            con = get_db()
            rows = con.execute("""
                SELECT id, timestamp, cam_ip,
                       CASE WHEN photo IS NOT NULL THEN 1 ELSE 0 END as has_photo
                FROM alertes_securite
                WHERE id > ?
                ORDER BY timestamp DESC LIMIT 20
            """, (since_id,)).fetchall()
            con.close()
            self._json({"alertes": [dict(r) for r in rows], "count": len(rows)})

        # Alertes sécurité PIR (dernières 50)
        elif path == "/api/alertes-securite":
            if not self._auth(): return
            con = get_db()
            rows = con.execute("""
                SELECT id, timestamp, cam_ip,
                       CASE WHEN photo IS NOT NULL THEN 1 ELSE 0 END as has_photo
                FROM alertes_securite
                ORDER BY timestamp DESC LIMIT 50
            """).fetchall()
            con.close()
            self._json({"alertes": [dict(r) for r in rows]})

        # Photo d'une alerte sécurité
        elif path.startswith("/api/alerte-photo/"):
            if not self._auth(): return
            try:
                alerte_id = int(path.split("/")[-1])
            except ValueError:
                return self._json({"error": "ID invalide"}, 400)
            con = get_db()
            row = con.execute(
                "SELECT photo FROM alertes_securite WHERE id=?", (alerte_id,)
            ).fetchone()
            con.close()
            if not row or not row["photo"]:
                return self._json({"error": "Photo introuvable"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(row["photo"])))
            self.cors()
            self.end_headers()
            self.wfile.write(row["photo"])

        else:
            self.send_response(404)
            self.end_headers()

    # ────────────────── POST ─────────────────
    def do_POST(self):
        body = self._read_body()
        path = self.path.split("?")[0]

        # ── LOGIN ──
        if path == "/api/login":
            ip = self.client_address[0]
            bloque, attente = _login_bloque(ip)
            if bloque:
                return self._json({"error": f"Trop de tentatives — réessayez dans {attente}s"}, 429)

            username = body.get("username", "").strip().lower()
            password = body.get("password", "")
            con = get_db()
            user = con.execute(
                "SELECT * FROM utilisateurs WHERE username=?", (username,)
            ).fetchone()

            if not user or not verifier_mot_de_passe(password, user["password"]):
                _login_echec(ip)
                con.close()
                return self._json({"error": "Nom d'utilisateur ou mot de passe incorrect"}, 401)

            # Migration transparente de l'ancien hash SHA256 nu vers PBKDF2 salé
            if not user["password"].startswith("pbkdf2$"):
                con.execute("UPDATE utilisateurs SET password=? WHERE id=?",
                            (hash_password(password), user["id"]))
                con.commit()

            con.close()
            _login_reussi(ip)
            token = secrets.token_hex(32)
            sessions[token] = {
                "user_id": user["id"], "username": user["username"], "role": user["role"],
                "cree_le": time.time()
            }
            print(f"  🔐 Connexion: {username} ({user['role']})")
            self._json({"token": token, "username": user["username"], "role": user["role"]})

        # ── CHANGER SON MOT DE PASSE ──
        elif path == "/api/change-password":
            sess = self._auth()
            if not sess: return
            ancien = body.get("ancien_mdp", "")
            nouveau = body.get("nouveau_mdp", "")
            if len(nouveau) < 6:
                return self._json({"error": "Nouveau mot de passe trop court (minimum 6 caractères)"}, 400)
            con = get_db()
            user = con.execute("SELECT * FROM utilisateurs WHERE id=?", (sess["user_id"],)).fetchone()
            if not user or not verifier_mot_de_passe(ancien, user["password"]):
                con.close()
                return self._json({"error": "Ancien mot de passe incorrect"}, 401)
            con.execute("UPDATE utilisateurs SET password=? WHERE id=?",
                        (hash_password(nouveau), user["id"]))
            con.commit()
            con.close()
            print(f"  🔑 Mot de passe changé pour {user['username']}")
            self._json({"success": True})

        # ── SIGNUP (admin crée des comptes) ──
        elif path == "/api/signup":
            sess = self._auth()
            # Autoriser si c'est le premier utilisateur OU si admin
            con = get_db()
            total = con.execute("SELECT COUNT(*) FROM utilisateurs").fetchone()[0]
            if total > 0 and (not sess or sess["role"] != "admin"):
                con.close()
                return self._json({"error": "Seul un admin peut créer des comptes"}, 403)

            username = body.get("username", "").strip().lower()
            password = body.get("password", "")
            role     = body.get("role", "operateur")

            if len(username) < 3:
                con.close()
                return self._json({"error": "Nom trop court (minimum 3 caractères)"}, 400)
            if len(password) < 6:
                con.close()
                return self._json({"error": "Mot de passe trop court (minimum 6 caractères)"}, 400)
            if role not in ("admin", "operateur"):
                role = "operateur"

            try:
                con.execute(
                    "INSERT INTO utilisateurs (username, password, role) VALUES (?,?,?)",
                    (username, hash_password(password), role)
                )
                con.commit()
                print(f"  👤 Nouveau: {username} ({role})")
                self._json({"success": True, "message": f"Compte '{username}' créé avec succès"})
            except sqlite3.IntegrityError:
                self._json({"error": "Ce nom d'utilisateur existe déjà"}, 400)
            finally:
                con.close()

        # ── MISE À JOUR SEUILS AUTOMATISATION ──
        elif path == "/api/seuils":
            sess = self._auth()
            if not sess: return
            # Seul l'admin peut modifier les seuils
            if sess["role"] != "admin":
                return self._json({"error": "Accès refusé — admin uniquement"}, 403)
            cles_valides = {
                "fan_temp_on", "fan_temp_off",
                "valve_sol_on", "valve_sol_off",
                "pompe_sol_on",
                "alert_temp_max", "alert_temp_min",
                "alert_hum_max", "alert_hum_min",
                "alert_sol_sec"
            }
            erreurs = []
            mis_a_jour = {}
            con = get_db()
            for cle, valeur in body.items():
                if cle not in cles_valides:
                    erreurs.append(f"Clé inconnue: {cle}")
                    continue
                try:
                    v = float(valeur)
                    con.execute(
                        """INSERT INTO seuils (cle, valeur, modifie_par, modifie_le)
                           VALUES (?,?,?,datetime('now'))
                           ON CONFLICT(cle) DO UPDATE SET
                             valeur=excluded.valeur,
                             modifie_par=excluded.modifie_par,
                             modifie_le=excluded.modifie_le""",
                        (cle, v, sess["username"])
                    )
                    seuils_auto[cle] = v
                    mis_a_jour[cle] = v
                except (ValueError, TypeError):
                    erreurs.append(f"Valeur invalide pour {cle}: {valeur}")
            con.commit()
            con.close()
            # Resynchroniser les variables globales d'alerte
            charger_seuils()
            print(f"  ⚙️  Seuils mis à jour par {sess['username']}: {mis_a_jour}")
            self._json({"success": True, "mis_a_jour": mis_a_jour, "erreurs": erreurs, "seuils_actuels": seuils_auto})

        # ── LOGOUT ──
        elif path == "/api/logout":
            token = self._get_token()
            sessions.pop(token, None)
            self._json({"success": True})

        # ── ME (vérifier session et retourner l'utilisateur courant) ──
        elif path == "/api/me":
            sess = self._auth()
            if not sess: return
            self._json({"username": sess["username"], "role": sess["role"]})

        # ── ENVOYER COMMANDE MQTT vers ESP32 ──
        elif path == "/api/cmd":
            if not self._auth(): return
            action = body.get("action")   # ex: "fan", "valve", "pompe", "mode/fan" ...
            valeur = body.get("valeur")   # "1" ou "0"
            if not action or valeur is None:
                return self._json({"error": "Champs manquants: action, valeur"}, 400)
            topic = f"agrotel/cmd/{action}"
            ok = publier_commande(topic, valeur)
            self._json({"success": ok, "topic": topic, "valeur": valeur})

        # ── ENREGISTRER DONNÉES CAPTEURS ──
        elif path == "/api/capteurs":
            if not self._auth(): return
            t = body.get("temperature")
            h = body.get("humidite")
            s = body.get("sol")
            if t is None or h is None or s is None:
                return self._json({"error": "Champs manquants: temperature, humidite, sol"}, 400)
            con = get_db()
            con.execute(
                "INSERT INTO capteurs (temperature,humidite,sol,ventilo,vanne,pompe) VALUES (?,?,?,?,?,?)",
                (float(t), float(h), int(s),
                 int(body.get("ventilo", 0)),
                 int(body.get("vanne",   0)),
                 int(body.get("pompe",   0)))
            )
            # Supprimer données > 31 jours automatiquement
            con.execute("DELETE FROM capteurs WHERE timestamp < datetime('now', '-31 days')")
            con.commit()
            con.close()
            # Vérifier les seuils et envoyer alertes si nécessaire
            threading.Thread(target=verifier_alertes, args=(
                float(t), float(h), int(s),
                int(body.get("ventilo",0)), int(body.get("vanne",0)), int(body.get("pompe",0))
            ), daemon=True).start()
            self._json({"success": True})

        # ── ALERTE PIR ESP32-CAM ──
        elif path == "/api/alerte-pir":
            # Appelé directement par l'ESP32-CAM — protégé par une clé d'appareil
            # partagée (et non par une session utilisateur classique).
            if DEVICE_API_KEY:
                cle_recue = self.headers.get("X-Device-Key", "") or body.get("device_key", "")
                if not hmac.compare_digest(cle_recue, DEVICE_API_KEY):
                    return self._json({"error": "Clé d'appareil invalide"}, 401)
            cam_ip   = body.get("cam_ip", "inconnu")
            photo_b64 = body.get("photo", "")     # JPEG encodé en base64
            photo_bytes = None
            if photo_b64:
                try:
                    photo_bytes = base64.b64decode(photo_b64)
                except Exception:
                    photo_bytes = None

            # Sauvegarder en base
            con = get_db()
            con.execute(
                "INSERT INTO alertes_securite (photo, cam_ip) VALUES (?,?)",
                (photo_bytes, cam_ip)
            )
            alerte_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
            # Garder seulement les 200 dernières alertes
            con.execute("""
                DELETE FROM alertes_securite
                WHERE id NOT IN (
                    SELECT id FROM alertes_securite ORDER BY id DESC LIMIT 200
                )
            """)
            con.commit()
            con.close()

            has_photo = photo_bytes is not None
            print(f"  🚨 INTRUSION détectée ! cam={cam_ip} | alerte_id={alerte_id} | photo={'OUI (' + str(len(photo_bytes)) + ' bytes)' if has_photo else 'NON'}")

            # Envoyer email alerte sécurité dans un thread
            threading.Thread(
                target=envoyer_email_securite,
                args=(cam_ip, photo_bytes, alerte_id),
                daemon=True
            ).start()

            self._json({"success": True, "alerte_id": alerte_id})

        # ── SUPPRIMER UTILISATEUR (admin) ──
        elif path == "/api/supprimer-user":
            sess = self._auth()
            if not sess: return
            if sess["role"] != "admin":
                return self._json({"error": "Accès refusé"}, 403)
            uid = body.get("user_id")
            if not uid:
                return self._json({"error": "user_id manquant"}, 400)
            con = get_db()
            con.execute("DELETE FROM utilisateurs WHERE id=?", (uid,))
            con.commit()
            con.close()
            self._json({"success": True})

        # ── CHATBOT GEMINI ──
        elif path == "/api/chat":
            if not self._auth(): return
            reply = self._call_groq(
                body.get("system", "Tu es AgriBot, expert agricole."),
                body.get("messages", [])
            )
            self._json({"content": [{"type": "text", "text": reply}]})

        # ── ANALYSE MALADIE PLANTE PAR PHOTO (Groq Vision) ──
        elif path == "/api/analyser-plante":
            if not self._auth(): return
            photo_b64 = body.get("photo", "")
            plante    = body.get("plante", "plante inconnue")
            temp      = body.get("temperature", "?")
            hum       = body.get("humidite", "?")
            sol       = body.get("sol", "?")
            if not photo_b64:
                return self._json({"error": "Aucune photo fournie"}, 400)
            print(f"  🔬 Analyse plante: {plante} — Groq Vision")
            resultat = self._analyser_plante_vision(photo_b64, plante, temp, hum, sol)
            self._json({"analyse": resultat})

        else:
            self.send_response(404)
            self.end_headers()

    # ────────────────── DELETE ───────────────
    def do_DELETE(self):
        path = self.path.split("?")[0]
        sess = self._auth()
        if not sess: return
        if sess["role"] != "admin":
            return self._json({"error": "Accès refusé — admin uniquement"}, 403)

        # Supprimer UNE alerte sécurité
        # DELETE /api/alertes-securite/42
        if path.startswith("/api/alertes-securite/"):
            try:
                alerte_id = int(path.split("/")[-1])
            except ValueError:
                return self._json({"error": "ID invalide"}, 400)
            con = get_db()
            con.execute("DELETE FROM alertes_securite WHERE id=?", (alerte_id,))
            affected = con.execute("SELECT changes()").fetchone()[0]
            con.commit()
            con.close()
            if affected == 0:
                return self._json({"error": "Alerte introuvable"}, 404)
            print(f"  🗑️  Alerte sécurité #{alerte_id} supprimée par {sess['username']}")
            self._json({"success": True, "deleted_id": alerte_id})

        # Supprimer TOUTES les alertes sécurité
        # DELETE /api/alertes-securite
        elif path == "/api/alertes-securite":
            con = get_db()
            con.execute("DELETE FROM alertes_securite")
            con.commit()
            con.close()
            print(f"  🗑️  Toutes les alertes sécurité supprimées par {sess['username']}")
            self._json({"success": True, "message": "Toutes les alertes supprimées"})

        else:
            self.send_response(404)
            self.end_headers()
    def _serve_file(self, filename, ctype):
        try:
            with open(filename, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.cors()
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(f"{filename} introuvable.".encode())

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            return {}

    def _get_token(self):
        return self.headers.get("Authorization", "").replace("Bearer ", "").strip()

    def _auth(self):
        """Vérifie le token, son expiration, et retourne la session ou envoie 401."""
        token = self._get_token()
        sess = sessions.get(token)
        if not sess:
            self._json({"error": "Non autorisé — veuillez vous connecter"}, 401)
            return None
        if time.time() - sess.get("cree_le", 0) > SESSION_DUREE_MAX:
            sessions.pop(token, None)
            self._json({"error": "Session expirée — veuillez vous reconnecter"}, 401)
            return None
        return sess

    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.cors()
        self.end_headers()
        self.wfile.write(body)

    def _call_groq(self, system_prompt, messages):
        """Appel API Groq officielle — https://console.groq.com"""
        if len(GROQ_API_KEY) < 10:
            return "⚠️ Clé Groq non configurée. Allez sur console.groq.com (gratuit) et ajoutez GROQ_API_KEY dans votre fichier .env"
        try:
            msgs = [{"role": "system", "content": system_prompt}]
            for m in messages[-12:]:
                role = m.get("role", "user")
                if role == "model": role = "assistant"
                msgs.append({"role": role, "content": m.get("content", "")})
            payload = json.dumps({
                "model": GROQ_MODEL_CHAT,
                "max_completion_tokens": 800,
                "temperature": 0.4,
                "messages": msgs
            }).encode("utf-8")
            req = urllib.request.Request(
                GROQ_API_URL,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "User-Agent": "Mozilla/5.0 (compatible; AgroTel-Server/1.0)",
                    "Accept": "application/json",
                }
            )
            with urllib.request.urlopen(req, timeout=20) as r:
                resp = json.loads(r.read())
            return resp["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()
            print(f"  ❌ Groq HTTP {e.code}: {err_body}")
            return f"❌ Erreur Groq ({e.code}): {err_body}"
        except Exception as e:
            return f"❌ Erreur Groq: {str(e)}"

    def _analyser_plante_vision(self, photo_b64, plante, temp, hum, sol):
        """
        Analyse photo de plante via Groq Vision (llama-3.2-11b-vision-preview).
        Utilise la MÊME clé GROQ_API_KEY que Stakana — aucune configuration supplémentaire.
        """
        if len(GROQ_API_KEY) < 10:
            return "⚠️ Clé Groq non configurée (voir .env)"

        prompt = f"""Tu es un expert agronome spécialisé dans les maladies des plantes.

Analyse cette photo d'une plante de type : {plante}

Conditions actuelles de la serre :
- Température : {temp}°C
- Humidité air : {hum}%
- Humidité sol : {sol}/1024

Réponds UNIQUEMENT en français avec ce format exact :

🌿 ÉTAT GÉNÉRAL : [Sain / Stressé / Malade / Critique]

🔍 DIAGNOSTIC :
[Décris ce que tu observes sur la plante : couleur des feuilles, taches, déformations, etc.]

🦠 MALADIE DÉTECTÉE : [Nom de la maladie ou "Aucune maladie visible"]

⚠️ RISQUES POTENTIELS :
[Maladies possibles compte tenu des conditions actuelles de la serre]

💊 TRAITEMENT RECOMMANDÉ :
[Actions concrètes à prendre immédiatement]

🌡️ AJUSTEMENTS CONSEILLÉS :
[Modifications température / humidité / arrosage à effectuer]

📅 URGENCE : [Immédiate / Dans 24h / Dans 1 semaine / Aucune]"""

        try:
            payload = json.dumps({
                "model": GROQ_MODEL_VISION,
                "max_completion_tokens": 1024,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{photo_b64}" if photo_b64.startswith("iVBORw0KGgo") else f"data:image/jpeg;base64,{photo_b64}"
                                }
                            },
                            {
                                "type": "text",
                                "text": prompt
                            }
                        ]
                    }
                ]
            }).encode("utf-8")

            req = urllib.request.Request(
                GROQ_API_URL,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "User-Agent": "Mozilla/5.0 (compatible; AgroTel-Server/1.0)",
                    "Accept": "application/json",
                }
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                resp = json.loads(r.read())
            result = resp["choices"][0]["message"]["content"]
            print(f"  ✅ Analyse Groq Vision terminée pour {plante}")
            return result

        except urllib.error.HTTPError as e:
            err_body = e.read().decode()
            print(f"  ❌ Groq Vision HTTP {e.code}: {err_body}")
            return f"❌ Erreur Groq Vision ({e.code}): {err_body}"
        except Exception as e:
            print(f"  ❌ Erreur Vision: {e}")
            return f"❌ Erreur analyse: {str(e)}"



# ══════════════════════════════════════════
#   SYSTÈME D'ALERTES EMAIL
# ══════════════════════════════════════════

# Garde en mémoire les alertes envoyées (évite spam)
# format: { "type_alerte": timestamp_dernier_envoi }
alertes_envoyees = {}
COOLDOWN_CAPTEURS = 5 * 60    # 5 min entre 2 alertes capteurs (temp/hum/sol)
COOLDOWN_PIR      = 2 * 60    # 2 min entre 2 emails PIR intrusion

def peut_envoyer_alerte(type_alerte):
    """Vérifie si on peut envoyer cette alerte selon son cooldown propre."""
    maintenant = time.time()
    dernier = alertes_envoyees.get(type_alerte, 0)
    # Cooldown selon le type
    if type_alerte == "pir_intrusion":
        cooldown = COOLDOWN_PIR
    else:
        cooldown = COOLDOWN_CAPTEURS
    if maintenant - dernier > cooldown:
        alertes_envoyees[type_alerte] = maintenant
        return True
    return False

def envoyer_email(sujet, corps_html):
    """Envoie un email via Gmail SMTP."""
    if not EMAIL_EXPEDITEUR or not EMAIL_MOT_DE_PASSE:
        print("  ⚠️  Email non configuré (EMAIL_EXPEDITEUR / EMAIL_MOT_DE_PASSE absents de .env)")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = sujet
        msg["From"]    = f"AgroTel <{EMAIL_EXPEDITEUR}>"
        msg["To"]      = ", ".join(EMAIL_DESTINATAIRES)
        msg.attach(MIMEText(corps_html, "html", "utf-8"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as smtp:
            smtp.login(EMAIL_EXPEDITEUR, EMAIL_MOT_DE_PASSE)
            smtp.sendmail(EMAIL_EXPEDITEUR, EMAIL_DESTINATAIRES, msg.as_string())

        print(f"  📧 Email envoyé: {sujet}")
        return True
    except Exception as e:
        print(f"  ❌ Erreur email: {e}")
        return False


def envoyer_email_securite(cam_ip, photo_bytes, alerte_id):
    """Envoie un email d'alerte sécurité avec la photo de l'intrus."""
    if not EMAIL_EXPEDITEUR or not EMAIL_MOT_DE_PASSE:
        print("  ⚠️  Email sécurité non envoyé — email non configuré")
        return False
    if not peut_envoyer_alerte("pir_intrusion"):
        print(f"  ⏳ Email PIR ignoré — cooldown actif (alerte #{alerte_id})")
        return False

    maintenant = datetime.now().strftime("%d/%m/%Y à %H:%M:%S")
    try:
        msg = MIMEMultipart("mixed")
        msg["Subject"] = f"🚨 ALERTE INTRUSION — AgroTel [{maintenant}]"
        msg["From"]    = f"AgroTel <{EMAIL_EXPEDITEUR}>"
        msg["To"]      = ", ".join(EMAIL_DESTINATAIRES)

        corps_html = f"""
        <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
          <div style="background:#c0392b;padding:20px;border-radius:10px 10px 0 0;text-align:center;">
            <h1 style="color:white;margin:0;font-size:24px;">🚨 ALERTE INTRUSION DÉTECTÉE</h1>
            <p style="color:#fadbd8;margin:8px 0 0 0;">AgroTel — Surveillance Périmétrique</p>
          </div>
          <div style="background:#f9f9f9;padding:24px;border-radius:0 0 10px 10px;border:1px solid #eee;">
            <p style="font-size:15px;color:#333;">
              Le capteur PIR de votre serre a détecté un <b>mouvement suspect</b>.
              Une photo a été capturée automatiquement par l'ESP32-CAM.
            </p>
            <table style="width:100%;border-collapse:collapse;margin:16px 0;">
              <tr style="background:#fff;border-bottom:1px solid #eee;">
                <td style="padding:10px 12px;color:#666;">📅 Date & Heure</td>
                <td style="padding:10px 12px;font-weight:bold;">{maintenant}</td>
              </tr>
              <tr style="background:#f9f9f9;border-bottom:1px solid #eee;">
                <td style="padding:10px 12px;color:#666;">📷 Caméra IP</td>
                <td style="padding:10px 12px;font-weight:bold;">{cam_ip}</td>
              </tr>
              <tr style="background:#fff;">
                <td style="padding:10px 12px;color:#666;">🆔 ID Alerte</td>
                <td style="padding:10px 12px;font-weight:bold;">#{alerte_id}</td>
              </tr>
            </table>
            {"<p style='font-weight:bold;color:#c0392b;'>📸 Photo capturée ci-jointe</p>" if photo_bytes else "<p style='color:#999;'>⚠️ Aucune photo disponible (capture échouée)</p>"}
            <hr style="border:1px solid #eee;margin:20px 0;">
            <p style="color:#aaa;font-size:12px;text-align:center;">
              AgroTel — Alerte automatique — {maintenant}
            </p>
          </div>
        </div>
        """

        msg.attach(MIMEText(corps_html, "html", "utf-8"))

        # Joindre la photo si disponible
        if photo_bytes:
            img = MIMEImage(photo_bytes, _subtype="jpeg")
            img.add_header(
                "Content-Disposition", "attachment",
                filename=f"intrusion_{alerte_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
            )
            msg.attach(img)

        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as smtp:
            smtp.login(EMAIL_EXPEDITEUR, EMAIL_MOT_DE_PASSE)
            smtp.sendmail(EMAIL_EXPEDITEUR, EMAIL_DESTINATAIRES, msg.as_string())

        print(f"  ✅ Email sécurité envoyé avec succès → {', '.join(EMAIL_DESTINATAIRES)} (alerte #{alerte_id})")
        return True
    except smtplib.SMTPAuthenticationError:
        print(f"  ❌ SMTP Auth échouée — vérifiez EMAIL_EXPEDITEUR et le mot de passe d'application Gmail")
        return False
    except smtplib.SMTPException as e:
        print(f"  ❌ Erreur SMTP: {e}")
        return False
    except Exception as e:
        print(f"  ❌ Erreur email sécurité: {e}")
        return False

def verifier_alertes(temp, hum, sol, ventilo, vanne, pompe):
    """Vérifie les seuils et envoie les alertes nécessaires."""
    maintenant = datetime.now().strftime("%d/%m/%Y à %H:%M")
    alertes = []

    if temp > seuils_auto["alert_temp_max"]:
        if peut_envoyer_alerte("temp_haute"):
            alertes.append(("🌡️ ALERTE — Température trop élevée", "temp_haute",
                f"La température dans la serre est de <b>{temp}°C</b> (seuil max: {seuils_auto['alert_temp_max']}°C).",
                "#e74c3c"))

    if temp < seuils_auto["alert_temp_min"]:
        if peut_envoyer_alerte("temp_basse"):
            alertes.append(("❄️ ALERTE — Température trop basse", "temp_basse",
                f"La température dans la serre est de <b>{temp}°C</b> (seuil min: {seuils_auto['alert_temp_min']}°C).",
                "#3498db"))

    if hum > seuils_auto["alert_hum_max"]:
        if peut_envoyer_alerte("hum_haute"):
            alertes.append(("💧 ALERTE — Humidité trop élevée", "hum_haute",
                f"L\'humidité dans la serre est de <b>{hum}%</b> (seuil max: {seuils_auto['alert_hum_max']}%). Risque de maladies fongiques.",
                "#9b59b6"))

    if hum < seuils_auto["alert_hum_min"]:
        if peut_envoyer_alerte("hum_basse"):
            alertes.append(("🏜️ ALERTE — Humidité trop basse", "hum_basse",
                f"L\'humidité dans la serre est de <b>{hum}%</b> (seuil min: {seuils_auto['alert_hum_min']}%). Les plantes manquent d\'eau.",
                "#e67e22"))

    if sol < seuils_auto["alert_sol_sec"]:
        if peut_envoyer_alerte("sol_sec"):
            alertes.append(("🌱 ALERTE — Sol trop sec", "sol_sec",
                f"L\'humidité du sol est de <b>{sol}/1024</b> (seuil sec: {seuils_auto['alert_sol_sec']}). Arrosage nécessaire.",
                "#d35400"))

    for sujet, _, message, couleur in alertes:
        corps = f"""
        <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
          <div style="background:{couleur};padding:20px;border-radius:10px 10px 0 0;text-align:center;">
            <h1 style="color:white;margin:0;font-size:22px;">{sujet}</h1>
          </div>
          <div style="background:#f9f9f9;padding:24px;border-radius:0 0 10px 10px;border:1px solid #eee;">
            <p style="font-size:15px;color:#333;">{message}</p>
            <hr style="border:1px solid #eee;margin:16px 0;">
            <h3 style="color:#2c3e50;margin-bottom:10px;">📊 État actuel de la serre :</h3>
            <table style="width:100%;border-collapse:collapse;">
              <tr style="background:#fff;border-bottom:1px solid #eee;">
                <td style="padding:8px 12px;color:#666;">🌡️ Température</td>
                <td style="padding:8px 12px;font-weight:bold;">{temp}°C</td>
              </tr>
              <tr style="background:#f9f9f9;border-bottom:1px solid #eee;">
                <td style="padding:8px 12px;color:#666;">💧 Humidité air</td>
                <td style="padding:8px 12px;font-weight:bold;">{hum}%</td>
              </tr>
              <tr style="background:#fff;border-bottom:1px solid #eee;">
                <td style="padding:8px 12px;color:#666;">🌱 Humidité sol</td>
                <td style="padding:8px 12px;font-weight:bold;">{sol}/1024</td>
              </tr>
              <tr style="background:#f9f9f9;border-bottom:1px solid #eee;">
                <td style="padding:8px 12px;color:#666;">🌀 Ventilateur</td>
                <td style="padding:8px 12px;font-weight:bold;">{"ON ✅" if ventilo else "OFF ⭕"}</td>
              </tr>
              <tr style="background:#fff;border-bottom:1px solid #eee;">
                <td style="padding:8px 12px;color:#666;">🚿 Pompe</td>
                <td style="padding:8px 12px;font-weight:bold;">{"ON ✅" if pompe else "OFF ⭕"}</td>
              </tr>
            </table>
            <p style="color:#aaa;font-size:12px;text-align:center;margin-top:20px;">
              AgroTel — {maintenant} — Alerte automatique
            </p>
          </div>
        </div>
        """
        threading.Thread(target=envoyer_email, args=(sujet, corps), daemon=True).start()

def tester_email():
    """Envoie un email de test au démarrage."""
    maintenant = datetime.now().strftime("%d/%m/%Y à %H:%M")
    corps = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
      <div style="background:#1D9E75;padding:20px;border-radius:10px 10px 0 0;text-align:center;">
        <h1 style="color:white;margin:0;">🌿 AgroTel — Système démarré</h1>
      </div>
      <div style="background:#f9f9f9;padding:24px;border-radius:0 0 10px 10px;border:1px solid #eee;">
        <p style="font-size:15px;color:#333;">
          Le serveur AgroTel a démarré avec succès.<br>
          Les alertes email sont maintenant <b>actives</b>.
        </p>
        <table style="width:100%;border-collapse:collapse;margin-top:16px;">
          <tr><td style="padding:8px;color:#666;">🌡️ Seuil temp. max</td><td style="font-weight:bold;">{SEUIL_TEMP_MAX}°C</td></tr>
          <tr><td style="padding:8px;color:#666;">❄️ Seuil temp. min</td><td style="font-weight:bold;">{SEUIL_TEMP_MIN}°C</td></tr>
          <tr><td style="padding:8px;color:#666;">💧 Seuil humidité max</td><td style="font-weight:bold;">{SEUIL_HUM_MAX}%</td></tr>
          <tr><td style="padding:8px;color:#666;">🏜️ Seuil humidité min</td><td style="font-weight:bold;">{SEUIL_HUM_MIN}%</td></tr>
          <tr><td style="padding:8px;color:#666;">🌱 Seuil sol sec</td><td style="font-weight:bold;">{SEUIL_SOL_SEC}/1024</td></tr>
        </table>
        <p style="color:#aaa;font-size:12px;text-align:center;margin-top:20px;">
          AgroTel — {maintenant}
        </p>
      </div>
    </div>
    """
    envoyer_email("✅ AgroTel — Serveur démarré", corps)

# ══════════════════════════════════════════
#   POINT D'ENTRÉE
# ══════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 52)
    print("  🌿 AgroTel — Démarrage du serveur")
    print("=" * 52)
    init_db()
    charger_seuils()
    if len(GROQ_API_KEY) < 10:
        print("  ⚠️  Chatbot désactivé — ajoutez GROQ_API_KEY dans votre fichier .env")
        print("      → https://console.groq.com (inscription gratuite)")
    else:
        print("  ✅ Chatbot Groq AI activé (llama3-8b-8192)")

    # ── Démarrage client MQTT ──
    if MQTT_DISPONIBLE:
        print(f"  📡 Connexion MQTT → {MQTT_BROKER}:{MQTT_PORT} ...")
        threading.Thread(target=demarrer_mqtt, daemon=True).start()
    else:
        print("  ⚠️  MQTT désactivé — installez paho-mqtt : pip install paho-mqtt")

    print(f"  🌐 Dashboard : http://localhost:{PORT}")
    print("  🛑 Arrêt     : Ctrl+C")
    print("=" * 52)
    # Envoyer email de test au démarrage (si email configuré)
    if EMAIL_EXPEDITEUR and EMAIL_MOT_DE_PASSE:
        print("  📧 Envoi email de démarrage...")
        threading.Thread(target=tester_email, daemon=True).start()
    else:
        print("  ⚠️  Emails non configurés (voir .env.example)")

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  🛑 Serveur arrêté proprement.")