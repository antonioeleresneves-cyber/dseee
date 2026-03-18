"""
SYS//CONSULTA - Backend
Flask + Telethon com StringSession

Deploy:
  1. pip install -r requirements.txt
  2. python backend.py
  3. Abra http://localhost:5000
"""

import asyncio
import os
import re
import threading
import logging

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, FloodWaitError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("jarvis")

# ── Credenciais ───────────────────────────────────────────────
API_ID   = 34303434
API_HASH = "5d521f53f9721a6376586a014b51173d"
PHONE    = "+5541974010817"
GRUPO    = -1002421438612
CHAVE    = "Skibidi toilet gamer Sigma redz Pill 1234"
PORT     = int(os.environ.get("PORT", 5000))

# ── SESSION STRING (gerada pelo gerar_sessao.py) ──────────────
SESSION_STRING = "1AZWarzwBu4dCV8v3it5AT-cmvzem1XLR9uhXhNxX3tHUM5GBwoL_r1DRZ8c01YWq2hqu4to-bn6FO51UmGNoDh4tr-b6XCjPnJamW6jfIrlvtgmsNZWSDKuWphfyQVCelPHqNXvEhLH8mDSgZEjF95dRHIi2c3-2uEqPsWcL_437MsgUIfq1GElkir_7r_YuiAKmyHG1XZatZI0EPMIJ9Ao61qR7K_eK1pgW8xEs5CGN_Liedq0qGaS6GRPk-T4JrPMMz6qlm9VLZgqc7fS2n_egXlG8dYlrbnKdZ_oBoQpmYkbws0pJTTElk7gr7dx8k36rKCKGRM7-eNOD3N9O5FoPa0770fA="

# ── Flask ─────────────────────────────────────────────────────
app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

# ── Estado global ─────────────────────────────────────────────
_loop           = None
_client         = None
_pending        = {}
_telegram_ready = threading.Event()

_auth = {
    "step":      None,
    "code_hash": None,
    "code_fut":  None,
    "twofa_fut": None,
    "error":     None,
}

# ══════════════════════════════════════════════════════════════
# TEXTO
# ══════════════════════════════════════════════════════════════

def limpar(texto):
    for r in ["@QueryBuscasBot", "https://t.me/querybuscas", "ID \u23af", "ID \u2014"]:
        texto = texto.replace(r, "")
    texto = re.sub(r"\((\d+)\)", r"\1", texto)
    texto = "\n".join(l.strip() for l in texto.splitlines() if l.strip())
    return texto.strip()

def parse(texto):
    linhas = [l for l in texto.splitlines() if l.strip()]
    if len(linhas) < 2:
        return {"RESULTADO": texto}
    campos = {}
    i = 0
    while i < len(linhas) - 1:
        rot = linhas[i].strip().upper()
        val = linhas[i+1].strip()
        if len(rot) <= 40 and not rot[0].isdigit():
            campos[rot] = val
            i += 2
        else:
            campos["INFO %d" % (i+1)] = linhas[i]
            i += 1
    if i < len(linhas):
        campos["INFO %d" % (i+1)] = linhas[i]
    return campos if campos else {"RESULTADO": texto}

# ══════════════════════════════════════════════════════════════
# ROTAS
# ══════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/status")
def api_status():
    return jsonify({
        "ok":         True,
        "telegram":   _telegram_ready.is_set(),
        "needs_auth": _auth["step"] in ("code", "2fa"),
        "auth_step":  _auth["step"],
        "auth_error": _auth["error"],
    })

@app.route("/api/auth", methods=["POST"])
def api_auth():
    body  = request.get_json(force=True, silent=True) or {}
    step  = body.get("step", "")
    value = body.get("value", "").strip()

    if not value:
        return jsonify({"error": "Campo vazio."}), 400

    _auth["error"] = None

    if step == "code":
        if _auth["code_fut"] and not _auth["code_fut"].done():
            _loop.call_soon_threadsafe(_auth["code_fut"].set_result, value)
            return jsonify({"ok": True})
        return jsonify({"error": "Nenhuma autenticacao aguardando codigo."}), 400

    if step == "2fa":
        if _auth["twofa_fut"] and not _auth["twofa_fut"].done():
            _loop.call_soon_threadsafe(_auth["twofa_fut"].set_result, value)
            return jsonify({"ok": True})
        return jsonify({"error": "Nenhuma autenticacao aguardando senha."}), 400

    return jsonify({"error": "Step invalido."}), 400

@app.route("/api/query", methods=["POST"])
def api_query():
    if not _telegram_ready.is_set():
        if _auth["step"] in ("code", "2fa"):
            return jsonify({"error": "Conclua o login primeiro."}), 503
        return jsonify({"error": "Telegram nao conectado. Aguarde."}), 503

    body    = request.get_json(force=True, silent=True) or {}
    comando = body.get("command", "").strip()
    sess    = body.get("session_id", "default")

    if not comando:
        return jsonify({"error": "Nenhum comando enviado."}), 400
    if not comando.startswith("/"):
        return jsonify({"error": "Comandos devem comecar com /"}), 400

    log.info("Comando: %s [%s]", comando, sess)

    try:
        dados = asyncio.run_coroutine_threadsafe(
            _enviar(_comando=comando, _sess=sess, _timeout=45),
            _loop
        ).result(timeout=50)
        return jsonify({"message": "Consulta concluida.", "data": dados})

    except TimeoutError:
        _pending.pop(sess, None)
        return jsonify({"error": "Bot nao respondeu em 45s."}), 504

    except Exception as e:
        log.error("Erro: %s", e)
        return jsonify({"error": str(e)}), 500

# ══════════════════════════════════════════════════════════════
# ASYNC
# ══════════════════════════════════════════════════════════════

async def _enviar(_comando, _sess, _timeout):
    future = _loop.create_future()
    _pending[_sess] = future
    try:
        await _client.send_message(GRUPO, _comando)
        log.info("Enviado: %s", _comando)
        return await asyncio.wait_for(asyncio.shield(future), timeout=_timeout)
    except asyncio.TimeoutError:
        raise TimeoutError()
    finally:
        _pending.pop(_sess, None)

def _setup_handlers():
    @_client.on(events.NewMessage(chats=GRUPO))
    async def handler(event):
        if not event.message.document:
            return

        log.info("Arquivo recebido — processando...")
        caminho = None

        try:
            caminho = await event.message.download_media(file="downloads/")

            with open(caminho, "r", encoding="utf-8", errors="ignore") as f:
                texto = f.read()

            if CHAVE and CHAVE in texto:
                texto = texto.split(CHAVE, 1)[1]

            texto = limpar(texto)
            dados = parse(texto)
            log.info("Campos: %s", list(dados.keys()))

            for sid, future in list(_pending.items()):
                if not future.done():
                    future.set_result(dados)
                    log.info("Resultado -> sessao %s", sid)
                    break
            else:
                log.warning("Arquivo sem consulta pendente.")

        except Exception as e:
            log.error("Erro ao processar: %s", e)
            for sid, future in list(_pending.items()):
                if not future.done():
                    future.set_exception(Exception(str(e)))
                    break
        finally:
            if caminho and os.path.exists(caminho):
                try: os.remove(caminho)
                except: pass

# ══════════════════════════════════════════════════════════════
# AUTH (apenas se SESSION_STRING estiver vazia)
# ══════════════════════════════════════════════════════════════

async def _autenticar():
    try:
        try:
            result = await _client.send_code_request(PHONE)
        except FloodWaitError as e:
            _auth["error"] = "Bloqueado pelo Telegram. Aguarde %d segundos." % e.seconds
            _auth["step"]  = None
            log.error("FloodWait: %ds", e.seconds)
            return False

        _auth["code_hash"] = result.phone_code_hash
        log.info("Codigo SMS enviado para %s", PHONE)

        _auth["code_fut"] = _loop.create_future()
        _auth["step"]     = "code"
        _auth["error"]    = None

        codigo = await asyncio.wait_for(_auth["code_fut"], timeout=300)
        _auth["step"] = None

        try:
            await _client.sign_in(phone=PHONE, code=codigo, phone_code_hash=_auth["code_hash"])

        except SessionPasswordNeededError:
            log.info("2FA necessario")
            _auth["twofa_fut"] = _loop.create_future()
            _auth["step"]      = "2fa"
            _auth["error"]     = None
            senha = await asyncio.wait_for(_auth["twofa_fut"], timeout=300)
            _auth["step"] = None
            await _client.sign_in(password=senha)

        except PhoneCodeInvalidError:
            _auth["error"] = "Codigo incorreto. Tente novamente."
            _auth["step"]  = "code"
            _auth["code_fut"] = _loop.create_future()
            codigo = await asyncio.wait_for(_auth["code_fut"], timeout=300)
            _auth["step"] = None
            await _client.sign_in(phone=PHONE, code=codigo, phone_code_hash=_auth["code_hash"])

        s = _client.session.save()
        log.info("SESSION_STRING: %s", s)
        _auth["step"]  = "done"
        _auth["error"] = None
        log.info("Login concluido!")
        return True

    except asyncio.TimeoutError:
        _auth["error"] = "Tempo esgotado. Reinicie o servidor."
        _auth["step"]  = None
        return False
    except Exception as e:
        _auth["error"] = str(e)
        _auth["step"]  = None
        log.error("Erro auth: %s", e)
        return False

# ══════════════════════════════════════════════════════════════
# THREAD TELEGRAM
# ══════════════════════════════════════════════════════════════

def _run_telegram():
    global _loop, _client

    os.makedirs("downloads", exist_ok=True)

    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)

    if SESSION_STRING and SESSION_STRING.strip():
        session = StringSession(SESSION_STRING)
        log.info("Usando SESSION_STRING — login automatico")
    else:
        session = StringSession()
        log.info("Sem SESSION_STRING — iniciando auth pelo chat")

    _client = TelegramClient(session, API_ID, API_HASH, loop=_loop)

    async def _start():
        await _client.connect()

        if await _client.is_user_authorized():
            log.info("Sessao valida — conectado!")
        else:
            ok = await _autenticar()
            if not ok:
                log.error("Auth falhou")
                return

        me = await _client.get_me()
        log.info("Conectado como: %s (%s)", me.first_name, me.phone)
        _setup_handlers()
        _telegram_ready.set()
        await _client.run_until_disconnected()

    try:
        _loop.run_until_complete(_start())
    except Exception as e:
        log.error("Erro critico: %s", e)

# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("\n╔══════════════════════════════════════╗")
    print("║   SYS//CONSULTA  —  iniciando...     ║")
    print("╚══════════════════════════════════════╝\n")
    threading.Thread(target=_run_telegram, daemon=True).start()
    log.info("Servidor em http://0.0.0.0:%d", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
