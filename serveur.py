#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════╗
║   Smart Farm — Serveur Backend Complet           ║
║   Base de données SQLite + Login + Capteurs      ║
╚══════════════════════════════════════════════════╝

Installation (une seule fois dans le terminal VS Code) :
    pip install groq   (optionnel, seulement pour le chatbot)

Lancement :
    python serveur.py

Accès :
    http://localhost:5050
"""

import json, sqlite3, hashlib, os, secrets, smtplib, threading, time, base64
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
#  ⚠️  CONFIGURATION — MODIFIEZ CES VALEURS
# ════════════════════════════════════════════

# Clé Groq — GRATUIT — obtenez la sur https://console.groq.com
# Inscription gratuite, pas de restriction géographique
GROQ_API_KEY = "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"

# Email Gmail expéditeur
# ⚠️ Utilisez un mot de passe d'application Gmail (pas votre vrai mot de passe)
# Tuto: myaccount.google.com → Sécurité → Mots de passe des applications
# APRÈS (tes vraies valeurs)
EMAIL_EXPEDITEUR   = "myyfarm37@gmail.com"
EMAIL_MOT_DE_PASSE = "xmec xnqk unjh ajzw"
EMAIL_DESTINATAIRES = ["myyfarm37@gmail.com"]

# Seuils d'alerte (modifiez selon vos plantes)
SEUIL_TEMP_MAX = 35.0   # °C  alerte si température trop haute
SEUIL_TEMP_MIN = 10.0   # °C  alerte si température trop basse
SEUIL_HUM_MAX  = 90.0   # %   alerte si humidité trop élevée
SEUIL_HUM_MIN  = 30.0   # %   alerte si humidité trop basse
SEUIL_SOL_SEC  = 200    # /1024  alerte sol trop sec

# ════════════════════════════════════════════
#  MQTT — HiveMQ broker
# ════════════════════════════════════════════
MQTT_BROKER = "broker.hivemq.com"
MQTT_PORT   = 1883
MQTT_CLIENT_ID = "SmartFarm_Serveur"

# ════════════════════════════════════════════

PORT      = 5050
DB_FILE   = "smartfarm.db"
HTML_FILE = "index.html"

# Sessions actives en mémoire  { token -> {user_id, username, role} }
sessions = {}

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

    # Créer admin par défaut si base vide
    cur.execute("SELECT COUNT(*) FROM utilisateurs")
    if cur.fetchone()[0] == 0:
        cur.execute(
            "INSERT INTO utilisateurs (username, password, role) VALUES (?,?,?)",
            ("admin", hash_password("admin123"), "admin")
        )
        print("  ✅ Admin créé  →  user: admin  |  pass: admin123")

    con.commit()
    con.close()
    print(f"  ✅ Base de données '{DB_FILE}' prête")


def hash_password(pwd):
    return hashlib.sha256(pwd.encode()).hexdigest()


def get_db():
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    return con


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
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

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

        # Vérifier session courante
        elif path == "/api/me":
            sess = self._auth()
            if not sess: return
            self._json({"username": sess["username"], "role": sess["role"]})

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
            username = body.get("username", "").strip().lower()
            password = body.get("password", "")
            con = get_db()
            user = con.execute(
                "SELECT * FROM utilisateurs WHERE username=? AND password=?",
                (username, hash_password(password))
            ).fetchone()
            con.close()
            if not user:
                return self._json({"error": "Nom d'utilisateur ou mot de passe incorrect"}, 401)
            token = secrets.token_hex(32)
            sessions[token] = {"user_id": user["id"], "username": user["username"], "role": user["role"]}
            print(f"  🔐 Connexion: {username} ({user['role']})")
            self._json({"token": token, "username": user["username"], "role": user["role"]})

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
            # Pas d'auth requise — appelé directement par l'ESP32-CAM
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

            print(f"  🚨 INTRUSION détectée ! cam={cam_ip}")

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
        """Vérifie le token et retourne la session ou envoie 401."""
        sess = sessions.get(self._get_token())
        if not sess:
            self._json({"error": "Non autorisé — veuillez vous connecter"}, 401)
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
        """Appel API Groq — gratuit, rapide, sans restriction géographique"""
        if len(GROQ_API_KEY) < 10:
            return "⚠️ Clé Groq non configurée. Allez sur console.groq.com (gratuit) et collez votre clé dans serveur.py"
        try:
            msgs = [{"role": "system", "content": system_prompt}]
            for m in messages[-12:]:
                role = m.get("role", "user")
                if role == "model": role = "assistant"
                msgs.append({"role": role, "content": m.get("content", "")})
            payload = json.dumps({
                "model": "meta-llama/llama-3.3-70b-instruct:free",
                "max_tokens": 800,
                "messages": msgs
            }).encode("utf-8")
            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/chat/completions",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "HTTP-Referer": "http://localhost:5050",
                    "X-Title": "SmartFarm"
                }
            )
            with urllib.request.urlopen(req, timeout=20) as r:
                resp = json.loads(r.read())
            return resp["choices"][0]["message"]["content"]
        except Exception as e:
            return f"❌ Erreur Groq: {str(e)}"

    def _analyser_plante_vision(self, photo_b64, plante, temp, hum, sol):
        """
        Analyse photo de plante via Groq Vision (llama-3.2-11b-vision-preview).
        Utilise la MÊME clé GROQ_API_KEY que Stakana — aucune configuration supplémentaire.
        """
        if len(GROQ_API_KEY) < 10:
            return "⚠️ Clé Groq non configurée dans serveur.py"

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
                "model": "meta-llama/llama-4-scout-17b-16e-instruct",
                "max_tokens": 1024,
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
                "https://openrouter.ai/api/v1/chat/completions",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "HTTP-Referer": "http://localhost:5050",
                    "X-Title": "SmartFarm"
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
COOLDOWN_ALERTE = 1 * 60   # 30 minutes entre 2 alertes du même type

def peut_envoyer_alerte(type_alerte):
    """Vérifie si on peut envoyer cette alerte (cooldown 30min)."""
    maintenant = time.time()
    dernier = alertes_envoyees.get(type_alerte, 0)
    if maintenant - dernier > COOLDOWN_ALERTE:
        alertes_envoyees[type_alerte] = maintenant
        return True
    return False

def envoyer_email(sujet, corps_html):
    """Envoie un email via Gmail SMTP."""
    if "votre.email@gmail.com" in EMAIL_EXPEDITEUR:
        print("  ⚠️  Email non configuré (modifiez EMAIL_EXPEDITEUR dans serveur.py)")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = sujet
        msg["From"]    = f"Smart Farm 🌿 <{EMAIL_EXPEDITEUR}>"
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
    if "votre.email@gmail.com" in EMAIL_EXPEDITEUR:
        print("  ⚠️  Email sécurité non envoyé — email non configuré")
        return False
    if not peut_envoyer_alerte("pir_intrusion"):
        return False

    maintenant = datetime.now().strftime("%d/%m/%Y à %H:%M:%S")
    try:
        msg = MIMEMultipart("mixed")
        msg["Subject"] = f"🚨 ALERTE INTRUSION — Smart Farm [{maintenant}]"
        msg["From"]    = f"Smart Farm 🌿 <{EMAIL_EXPEDITEUR}>"
        msg["To"]      = ", ".join(EMAIL_DESTINATAIRES)

        corps_html = f"""
        <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
          <div style="background:#c0392b;padding:20px;border-radius:10px 10px 0 0;text-align:center;">
            <h1 style="color:white;margin:0;font-size:24px;">🚨 ALERTE INTRUSION DÉTECTÉE</h1>
            <p style="color:#fadbd8;margin:8px 0 0 0;">Smart Farm — Surveillance Périmétrique</p>
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
              Smart Farm — Alerte automatique — {maintenant}
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

        print(f"  🚨 Email sécurité envoyé (alerte #{alerte_id})")
        return True
    except Exception as e:
        print(f"  ❌ Erreur email sécurité: {e}")
        return False

def verifier_alertes(temp, hum, sol, ventilo, vanne, pompe):
    """Vérifie les seuils et envoie les alertes nécessaires."""
    maintenant = datetime.now().strftime("%d/%m/%Y à %H:%M")
    alertes = []

    if temp > SEUIL_TEMP_MAX:
        if peut_envoyer_alerte("temp_haute"):
            alertes.append(("🌡️ ALERTE — Température trop élevée", "temp_haute",
                f"La température dans la serre est de <b>{temp}°C</b> (seuil max: {SEUIL_TEMP_MAX}°C).",
                "#e74c3c"))

    if temp < SEUIL_TEMP_MIN:
        if peut_envoyer_alerte("temp_basse"):
            alertes.append(("❄️ ALERTE — Température trop basse", "temp_basse",
                f"La température dans la serre est de <b>{temp}°C</b> (seuil min: {SEUIL_TEMP_MIN}°C).",
                "#3498db"))

    if hum > SEUIL_HUM_MAX:
        if peut_envoyer_alerte("hum_haute"):
            alertes.append(("💧 ALERTE — Humidité trop élevée", "hum_haute",
                f"L\'humidité dans la serre est de <b>{hum}%</b> (seuil max: {SEUIL_HUM_MAX}%). Risque de maladies fongiques.",
                "#9b59b6"))

    if hum < SEUIL_HUM_MIN:
        if peut_envoyer_alerte("hum_basse"):
            alertes.append(("🏜️ ALERTE — Humidité trop basse", "hum_basse",
                f"L\'humidité dans la serre est de <b>{hum}%</b> (seuil min: {SEUIL_HUM_MIN}%). Les plantes manquent d\'eau.",
                "#e67e22"))

    if sol < SEUIL_SOL_SEC:
        if peut_envoyer_alerte("sol_sec"):
            alertes.append(("🌱 ALERTE — Sol trop sec", "sol_sec",
                f"L\'humidité du sol est de <b>{sol}/1024</b> (seuil sec: {SEUIL_SOL_SEC}). Arrosage nécessaire.",
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
              Smart Farm — {maintenant} — Alerte automatique
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
        <h1 style="color:white;margin:0;">🌿 Smart Farm — Système démarré</h1>
      </div>
      <div style="background:#f9f9f9;padding:24px;border-radius:0 0 10px 10px;border:1px solid #eee;">
        <p style="font-size:15px;color:#333;">
          Le serveur Smart Farm a démarré avec succès.<br>
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
          Smart Farm — {maintenant}
        </p>
      </div>
    </div>
    """
    envoyer_email("✅ Smart Farm — Serveur démarré", corps)

# ══════════════════════════════════════════
#   POINT D'ENTRÉE
# ══════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 52)
    print("  🌿 Smart Farm — Démarrage du serveur")
    print("=" * 52)
    init_db()
    if len(GROQ_API_KEY) < 10:
        print("  ⚠️  Chatbot désactivé — ajoutez votre clé Groq dans serveur.py")
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
    if "votre.email@gmail.com" not in EMAIL_EXPEDITEUR:
        print("  📧 Envoi email de démarrage...")
        threading.Thread(target=tester_email, daemon=True).start()
    else:
        print("  ⚠️  Emails non configurés (modifiez EMAIL_EXPEDITEUR)")

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  🛑 Serveur arrêté proprement.")