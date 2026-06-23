/* ══════════════════════════════════════════════════════════════
   SMART FARM — Chat Storage Module v3.0
   Sauvegarde persistante des conversations par utilisateur
   ══════════════════════════════════════════════════════════════ */

var ChatStorage = (function () {

  var KEY_PREFIX  = 'agrotel_chat_';
  var META_PREFIX = 'agrotel_chatmeta_';
  var MAX_MESSAGES = 200;
  var MAX_HISTORY  = 50;

  function _key(u)     { return KEY_PREFIX  + (u || 'guest').toLowerCase(); }
  function _metaKey(u) { return META_PREFIX + (u || 'guest').toLowerCase(); }

  /* ── Charger ── */
  function load(username) {
    try {
      var raw = localStorage.getItem(_key(username));
      if (raw) {
        var data = JSON.parse(raw);
        return { plant: data.plant || null, messages: data.messages || [], chatHistory: data.chatHistory || [] };
      }
    } catch (e) { console.warn('[ChatStorage] Erreur chargement:', e); }
    return { plant: null, messages: [], chatHistory: [] };
  }

  /* ── Sauvegarder ── */
  function save(username, state) {
    if (!username) return;
    try {
      var msgs = (state.messages || []).slice();
      if (msgs.length > MAX_MESSAGES) msgs = msgs.slice(-MAX_MESSAGES);
      var hist = (state.chatHistory || []).slice();
      if (hist.length > MAX_HISTORY * 2) hist = hist.slice(-(MAX_HISTORY * 2));
      localStorage.setItem(_key(username), JSON.stringify({
        plant: state.plant || null, messages: msgs, chatHistory: hist,
        lastSaved: new Date().toISOString()
      }));
      localStorage.setItem(_metaKey(username), JSON.stringify({
        username: username, lastActive: new Date().toISOString(), msgCount: msgs.length
      }));
    } catch (e) { console.warn('[ChatStorage] Erreur sauvegarde:', e); }
  }

  /* ── Effacer ── */
  function clear(username) {
    if (!username) return;
    localStorage.removeItem(_key(username));
    localStorage.removeItem(_metaKey(username));
  }

  return { load: load, save: save, clear: clear };

})();


/* ══════════════════════════════════════════════════════════════
   initChatPersistence — appelé après login
   Restaure l'historique + arme la sauvegarde automatique
   ══════════════════════════════════════════════════════════════ */
function initChatPersistence() {

  var _username = (typeof currentUser !== 'undefined' && currentUser)
                    ? currentUser.username : null;
  if (!_username) return;

  /* ══ 1. Restaurer ══ */
  var saved = ChatStorage.load(_username);

  if (saved.messages.length > 0) {
    chatHistory = saved.chatHistory || [];
    chatPlant   = saved.plant || null;

    var container = document.getElementById('chat-msgs');
    if (container) {
      container.innerHTML = '';
      saved.messages.forEach(function (m) {
        var div = document.createElement('div');
        div.className = 'msg ' + (m.role === 'bot' ? 'bot' : 'user');
        div.innerHTML = '<div class="msg-bubble">' + (m.html || '') + '</div>'
                      + '<span class="msg-time">' + (m.time || '') + '</span>';
        container.appendChild(div);
      });
      var resume = document.createElement('div');
      resume.style.cssText = 'text-align:center;font-family:var(--mono);font-size:10px;color:var(--green);padding:8px 0 4px;opacity:.7;';
      resume.textContent = '\u21a9 Conversation reprise \u2014 '
        + new Date().toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' });
      container.appendChild(resume);
      container.scrollTop = container.scrollHeight;
    }

    var notif = document.getElementById('chat-notif');
    if (notif) { notif.style.display = ''; notif.textContent = '\u21a9'; }

    console.log('%c\ud83d\udcbe [ChatStorage] ' + saved.messages.length + ' messages restaures pour : ' + _username, 'color:#1DC878;font-size:11px;font-weight:bold;');

  } else {
    if (typeof initChat === 'function') initChat();
  }

  /* ══ 2. Bloquer initChat si historique existe ══ */
  var _origInitChat = window.initChat;
  window.initChat = function () {
    if (typeof chatHistory !== 'undefined' && chatHistory.length > 0) return;
    if (typeof _origInitChat === 'function') _origInitChat();
  };

  /* ══ 3. Sauvegarde periodique toutes les 20s ══ */
  window._chatAutoSave = setInterval(function () {
    if (typeof currentUser !== 'undefined' && currentUser && currentUser.username === _username) {
      if (typeof _saveChatNow === 'function') _saveChatNow();
    } else {
      clearInterval(window._chatAutoSave);
    }
  }, 20000);

  /* ══ 4. Sauvegarde de secours a la fermeture ══ */
  window.addEventListener('beforeunload', function () {
    if (typeof _saveChatNow === 'function') _saveChatNow();
  });
}


/* ══════════════════════════════════════════════════════════════
   clearChatHistory — bouton 🗑 dans le chat
   ══════════════════════════════════════════════════════════════ */
function clearChatHistory() {
  var _username = (typeof currentUser !== 'undefined' && currentUser) ? currentUser.username : null;
  if (!_username) return;
  if (!confirm('Effacer toute la conversation pour ' + _username + ' ?')) return;

  if (window._chatAutoSave) { clearInterval(window._chatAutoSave); window._chatAutoSave = null; }
  ChatStorage.clear(_username);
  chatHistory = [];
  chatPlant   = null;

  var container = document.getElementById('chat-msgs');
  if (container) container.innerHTML = '';

  var notif = document.getElementById('chat-notif');
  if (notif) { notif.style.display = 'none'; notif.textContent = ''; }

  if (typeof appendBotMsg === 'function' && typeof buildPlantChips === 'function') {
    appendBotMsg('👋 Bonjour ! Je suis <strong>Stakana</strong>.<br>Quelle plante cultivez-vous ?', buildPlantChips());
  }

  if (typeof showToast === 'function') showToast('Conversation effacée', 'info', 2500);
}