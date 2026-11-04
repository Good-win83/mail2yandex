"""TOTP-protected administration panel and dashboard."""
import json
import os
import re
import sqlite3
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from totp_auth import LoginManager, read_secret
from pathlib import Path
from urllib.parse import urlsplit

from mail2yandex import Router, audit, validate_destination, validate_destinations, MessengerDeliveryError
from mailbox_status import StatusStore
from delivery_history import DeliveryHistory
from accounts import Accounts, test_connection, ImapAccountCheckError

LOGIN_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mail2Yandex — вход</title><style>
body{background:#f4f7fb;color:#182237;font:16px system-ui,sans-serif;display:grid;place-items:center;min-height:95vh}
main{background:white;border-radius:16px;padding:32px;width:min(390px,85vw);box-shadow:0 8px 32px #24365220}
h1{margin:0 0 8px}p{color:#52627b}input,button{box-sizing:border-box;display:block;width:100%;padding:12px;margin:14px 0;border-radius:8px;border:1px solid #b7c2d5;font:inherit}
button{background:#1c63b9;color:#fff;border:0;cursor:pointer}
#error{color:#9a2333;min-height:24px}
</style></head><body><main><h1>Mail2Yandex</h1><p>Вход в панель управления</p>
<form id="login"><input id="username" autocomplete="username" placeholder="Логин" required>
<input id="otp" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" placeholder="6-значный код Google Authenticator" required>
<button>Войти</button><p id="error" role="alert"></p></form>
<script>
document.getElementById('login').onsubmit=async e=>{
e.preventDefault();const v=id=>document.getElementById(id).value;
try{const r=await fetch('/auth/login',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},
body:JSON.stringify({username:v('username'),otp:v('otp')})});
if(!r.ok)throw Error('Неверный логин или TOTP-код; возможно, превышен лимит попыток');
location.replace('/');}catch(err){document.getElementById('error').textContent=err.message;}};
</script></main></body></html>"""


PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mail2Yandex — управление</title><style>
:root{font-family:system-ui,Arial;background:#f4f7fb;color:#182237}
main{max-width:1050px;margin:40px auto;padding:0 20px}
h1{margin-bottom:6px}p{color:#5b667b}
section{background:white;border-radius:14px;margin:20px 0;padding:22px;box-shadow:0 5px 24px #24365210}
input,select,button{font:inherit;border:1px solid #cdd5e1;border-radius:8px;padding:10px;margin:3px}
button{background:#1c63b9;color:#fff;border:0;cursor:pointer}
button.secondary{background:#52627b}button.danger{background:#a32638}
table{width:100%;border-collapse:collapse}td,th{padding:10px;text-align:left;border-bottom:1px solid #e6eaf1;word-break:break-word}
form{display:flex;flex-wrap:wrap;gap:8px;align-items:center}input{min-width:160px;flex:1}
#status{white-space:pre-wrap;font-weight:600}small{color:#67758a}
</style></head><body><main><h1>Mail2Yandex</h1><p>Яндекс.Почта (IMAP) → Яндекс.Мессенджер · управление и диагностика · v1.6</p>
<button class="secondary" id="logout" type="button" style="float:right">Выйти</button><div id="status" role="status"></div>
<section><h2>Почтовые ящики Яндекс 360</h2><small>Пароли приложений не показываются после сохранения. Для каждого ящика настройте адрес маршрута в разделе ниже.</small><div style="overflow:auto"><table><thead><tr><th>Ящик</th><th>IMAP</th><th>Статус</th><th>Последняя проверка UTC</th><th>Успешная UTC</th><th>Писем</th><th>Отправок</th><th>Ошибка</th><th>Действия</th></tr></thead><tbody id="accounts"></tbody></table></div>
<h3>Добавить или изменить ящик</h3><form id="accountform">
<input id="mailbox" type="email" required placeholder="alerts@example.org">
<input id="mailpass" type="password" autocomplete="new-password" placeholder="Пароль приложения (при изменении можно оставить пустым)">
<label><input id="mailenabled" type="checkbox" checked>Включён</label>
<button>Сохранить ящик</button></form><small>Используется imap.yandex.ru:993, INBOX, TLS. Для существующего ящика пустой пароль означает «не менять».</small></section>
<section><h2>Маршруты — несколько получателей</h2><div style="overflow:auto"><table><thead><tr><th>Адрес</th><th>Тип</th><th>Получатель</th><th></th></tr></thead><tbody id="routes"></tbody></table></div>
<h3>Добавить или изменить</h3><form id="routeform"><input id="address" type="email" placeholder="alerts@example.org" required>
<select id="kind"><option value="chat_id">Чат</option><option value="login">Логин</option><option value="user_id">User ID</option></select>
<input id="target" required placeholder="ID чата или логин"><button>Добавить адресата</button></form><small>Добавьте несколько чатов или пользователей к одному адресу; каждый адресат удаляется отдельно.</small></section>
<section><h2>Диагностика бота</h2><p>Проверка OAuth не отправляет сообщений. Проверка адресата отправляет одно тестовое сообщение указанному пользователю или в чат.</p>
<button class="secondary" id="checkbot" type="button">Проверить OAuth-токен</button>
<form id="testform"><select id="testkind"><option value="chat_id">Чат</option><option value="login">Логин</option><option value="user_id">User ID</option></select>
<input id="testtarget" required placeholder="ID чата или логин"><button>Отправить тест</button></form></section>
<section><h2>Журнал доставки по получателям</h2>
<small>Время UTC; показываются только адрес почтового ящика, UID письма, адресат и статус, без содержимого письма. «Пропущено» означает ранее успешную доставку.</small>
<div style="overflow:auto"><table><thead><tr><th>UTC</th><th>Ящик</th><th>UID</th><th>Тип</th><th>Адресат</th><th>Результат</th><th>Детали</th></tr></thead><tbody id="deliveries"></tbody></table></div></section>
<section><h2>Последние события</h2><button class="secondary" id="refresh">Обновить</button>
<div style="overflow:auto"><table><thead><tr><th>UTC</th><th>Статус</th><th>Канал</th><th>Детали</th></tr></thead><tbody id="events"></tbody></table></div></section>
</main><script>
const $=id=>document.getElementById(id);
let csrfToken='';

function note(s){$('status').textContent=s;}
async function request(path, data){
 const opts=data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken,'X-Mail2Yandex-Admin':'1'},body:JSON.stringify(data)};
 const r=await fetch(path,opts); if(r.status===401){location.replace('/login');throw Error('Сессия завершена');} const j=await r.json(); if(!r.ok)throw Error(j.error||'HTTP '+r.status);return j;
}
function cell(tr,value){const td=document.createElement('td');td.textContent=String(value);tr.append(td);return td;}
async function refresh(){
 try {
 const state=await request('/api/state');csrfToken=state.csrf||'';
 const accounts=$('accounts');accounts.replaceChildren();
 for(const a of (state.accounts||[])){
  const tr=document.createElement('tr');cell(tr,a.email);cell(tr,a.host+':'+a.port);cell(tr,a.enabled?'Включён':'Выключен');
  const h=(state.health||{})[a.email]||{};
  cell(tr,h.last_attempt||'—');cell(tr,h.last_success||'—');cell(tr,h.processed??0);cell(tr,h.delivered??0);cell(tr,h.last_error||'—');
  const td=document.createElement('td');
  const test=document.createElement('button');test.className='secondary';test.textContent='Проверить';
  test.onclick=async()=>{try{await request('/api/account/test',{email:a.email});note('IMAP успешно подключён: '+a.email)}catch(e){note('Проверка не прошла: '+e.message)}};
  const edit=document.createElement('button');edit.className='secondary';edit.textContent='Изменить';
  edit.onclick=()=>{$('mailbox').value=a.email;$('mailenabled').checked=a.enabled;$('mailpass').value='';$('mailbox').focus()};
  const remove=document.createElement('button');remove.className='danger';remove.textContent='Удалить';
  remove.onclick=async()=>{if(!confirm('Удалить ящик '+a.email+'?'))return;try{await request('/api/account/delete',{email:a.email});await refresh();note('Ящик удалён')}catch(e){note(e.message)}};
  td.append(test,edit,remove);tr.append(td);accounts.append(tr);
 }
 const routes=$('routes');routes.replaceChildren();
 for(const [address,raw] of Object.entries(state.routes)){
  for(const dest of (Array.isArray(raw)?raw:[raw])){
    const tr=document.createElement('tr');const [kind,target]=Object.entries(dest)[0];
    cell(tr,address);cell(tr,kind);cell(tr,target);
    const td=document.createElement('td'),b=document.createElement('button');
    b.className='danger';b.textContent='Удалить адресата';
    b.onclick=async()=>{if(!confirm('Удалить получателя '+target+'?'))return;
      try{await request('/api/recipient/delete',{address,kind,target});await refresh();note('Адресат удалён')}catch(e){note(e.message)}};
    const test=document.createElement('button');test.className='secondary';test.textContent='Тест';
    test.onclick=async()=>{if(!confirm('Отправить тестовое сообщение адресату '+target+'?'))return;
      try{await request('/api/test',{kind,target});note('Тестовое сообщение отправлено: '+target)}catch(e){note('Ошибка адресата '+target+': '+e.message)}};
    td.append(test,b);tr.append(td);routes.append(tr);
  }
 }
 const deliveries=$('deliveries');deliveries.replaceChildren();
 for(const entry of (state.deliveries||[])){const tr=document.createElement('tr');for(const val of entry)cell(tr,val);deliveries.append(tr);}
 const events=$('events');events.replaceChildren();
 for(const event of state.events){const tr=document.createElement('tr');for(const val of event)cell(tr,val);events.append(tr);}
 }catch(e){note('Ошибка: '+e.message);}
}
$('accountform').onsubmit=async e=>{e.preventDefault();try{await request('/api/account/save',{email:$('mailbox').value,password:$('mailpass').value,enabled:$('mailenabled').checked});$('mailpass').value='';note('Ящик сохранён, изменения подхватятся на следующем опросе');await refresh()}catch(e){note(e.message)}};
$('routeform').onsubmit=async e=>{e.preventDefault();try{await request('/api/recipient/add',{address:$('address').value,kind:$('kind').value,target:$('target').value});note('Адресат добавлен');await refresh();}catch(e){note(e.message)}};
$('testform').onsubmit=async e=>{e.preventDefault();try{await request('/api/test',{kind:$('testkind').value,target:$('testtarget').value});note('Тестовое сообщение отправлено');await refresh();}catch(e){note(e.message)}};
$('checkbot').onclick=async()=>{try{const bot=await request('/api/bot/check',{});note('Токен действителен. Бот: '+(bot.login||'без логина'));}catch(e){note('Проверка токена: '+e.message)}};
$('logout').onclick=async()=>{try{await request('/auth/logout',{});}finally{location.replace('/login');}};
$('refresh').onclick=refresh;refresh();
</script></body></html>"""


def start_admin(messenger, routes_file):
    if not routes_file:
        raise ValueError("ROUTES_FILE is required for the admin panel")
    path = Path(routes_file)
    if not path.is_file():
        raise ValueError("ROUTES_FILE must point to an existing JSON file")
    host = os.getenv("ADMIN_HOST", "127.0.0.1")
    port = int(os.getenv("ADMIN_PORT", "8080"))
    if host not in ("127.0.0.1", "::1", "localhost") and os.getenv("ADMIN_ALLOW_REMOTE") != "true":
        raise ValueError("Remote admin binding requires ADMIN_ALLOW_REMOTE=true and HTTPS reverse proxy")
    gate = threading.Lock()
    account_file = os.getenv("IMAP_ACCOUNTS_FILE", "").strip()
    accounts = Accounts(account_file) if account_file else None
    admin_user = os.getenv("ADMIN_USER", "admin")
    totp_secret = read_secret(os.getenv("ADMIN_TOTP_SECRET_FILE", "/data/admin-totp.secret"))
    auth = LoginManager(admin_user, totp_secret, os.getenv("ADMIN_SESSION_SECONDS", "3600"))
    cookie_secure = os.getenv("ADMIN_COOKIE_SECURE", "false").lower() == "true"
    db_file = os.getenv("AUDIT_DB", "")
    status_db = os.getenv("IMAP_STATUS_DB", "/data/mailbox-status.sqlite")
    delivery_db = os.getenv("DELIVERY_HISTORY_DB", "/data/delivery-history.sqlite")

    class AdminHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # Never log credentials, TOTP codes, cookies, or request bodies.
            pass

        def session(self):
            return auth.session(self.headers.get("Cookie"))

        def authorized(self):
            return self.session() is not None

        def cookie(self, value, max_age):
            options = ("mail2yandex_session=" + value +
                       "; Path=/; HttpOnly; SameSite=Strict; Max-Age=" + str(max_age))
            if cookie_secure:
                options += "; Secure"
            self.send_header("Set-Cookie", options)

        def login_page(self):
            raw = LOGIN_PAGE.encode("utf-8")
            self.headers_common("text/html; charset=utf-8", len(raw))
            self.wfile.write(raw)

        def headers_common(self, mimetype, length, status=200):
            self.send_response(status)
            self.send_header("Content-Type", mimetype)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; object-src 'none'; frame-ancestors 'none'")
            self.end_headers()

        def output(self, value, status=200):
            raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.headers_common("application/json; charset=utf-8", len(raw), status)
            self.wfile.write(raw)

        def check_auth(self):
            if self.authorized():
                return True
            self.output({"error": "Authentication required"}, 401)
            return False

        def read_routes(self):
            document = json.loads(path.read_text(encoding="utf-8"))
            Router(document.get("routes", {}), document.get("default"))
            return document

        def do_GET(self):
            url = urlsplit(self.path).path
            if url == "/login":
                return self.login_page()
            if not self.authorized():
                if url == "/":
                    self.send_response(303)
                    self.send_header("Location", "/login")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                else:
                    self.output({"error": "Authentication required"}, 401)
                return
            if url == "/":
                raw = PAGE.encode("utf-8")
                self.headers_common("text/html; charset=utf-8", len(raw))
                self.wfile.write(raw)
            elif url == "/api/state":
                try:
                    with gate:
                        document = self.read_routes()
                    events = []
                    if db_file and Path(db_file).exists():
                        with sqlite3.connect(db_file, timeout=5) as db:
                            db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, at TEXT NOT NULL, status TEXT NOT NULL, channel TEXT NOT NULL, detail TEXT NOT NULL)")
                            events = db.execute("SELECT at,status,channel,detail FROM events ORDER BY id DESC LIMIT 100").fetchall()
                    self.output({"routes": document.get("routes", {}), "default": document.get("default"), "events": events, "accounts": accounts.public() if accounts else [], "health": StatusStore(status_db).list() if accounts else {}, "deliveries": DeliveryHistory(delivery_db).recent(100), "csrf": self.session()[1]})
                except (OSError, ValueError, sqlite3.Error) as exc:
                    self.output({"error": type(exc).__name__}, 500)
            else:
                self.output({"error": "Not found"}, 404)

        def do_POST(self):
            endpoint = urlsplit(self.path).path
            if endpoint == "/auth/login":
                if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                    return self.output({"error": "Invalid request"}, 415)
                size = int(self.headers.get("Content-Length", "0"))
                if size <= 0 or size > 2048:
                    return self.output({"error": "Invalid request"}, 400)
                try:
                    data = json.loads(self.rfile.read(size))
                    result = auth.authenticate(data.get("username"), data.get("otp"),
                                               self.client_address[0])
                except (ValueError, TypeError, AttributeError):
                    result = None
                if result is None:
                    audit("login_failed", "admin", "Invalid login or TOTP")
                    return self.output({"error": "Invalid login"}, 401)
                sid, _ = result
                raw = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.cookie(sid, auth.ttl)
                self.end_headers()
                self.wfile.write(raw)
                audit("login_ok", "admin", "TOTP")
                return
            session = self.session()
            if session is None:
                return self.output({"error": "Authentication required"}, 401)
            if endpoint == "/auth/logout":
                if self.headers.get("X-CSRF-Token") != session[1]:
                    return self.output({"error": "Invalid CSRF token"}, 403)
                auth.logout(session[0])
                raw = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(raw)))
                self.cookie("", 0)
                self.end_headers()
                self.wfile.write(raw)
                return
            if self.headers.get("X-CSRF-Token") != session[1]:
                return self.output({"error": "Invalid CSRF token"}, 403)
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json" or self.headers.get("X-Mail2Yandex-Admin") != "1":
                return self.output({"error": "Invalid request headers"}, 403)
            origin = self.headers.get("Origin")
            if origin:
                request_host = self.headers.get("Host", "")
                if origin != "http://" + request_host and origin != "https://" + request_host:
                    return self.output({"error": "Origin mismatch"}, 403)
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 8192:
                return self.output({"error": "Invalid body size"}, 413)
            try:
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("Expected JSON object")
                endpoint = urlsplit(self.path).path
                if endpoint.startswith("/api/account/"):
                    if not accounts:
                        return self.output({"error": "Set IMAP_ACCOUNTS_FILE to manage accounts"}, 409)
                    if endpoint == "/api/account/save":
                        account = accounts.upsert(data)
                        audit("account_updated", "admin", account["email"])
                        return self.output({"ok": True, "account": account})
                    if endpoint == "/api/account/delete":
                        accounts.delete(data["email"])
                        audit("account_deleted", "admin", "Mailbox deleted")
                        return self.output({"ok": True})
                    if endpoint == "/api/account/test":
                        matched = next((a for a in accounts.load() if a["email"] == str(data["email"]).lower()), None)
                        if matched is None:
                            raise ValueError("Mailbox not found")
                        test_connection(matched)
                        audit("account_test", "imap", "Connection succeeded")
                        return self.output({"ok": True})
                    return self.output({"error": "Not found"}, 404)
                if endpoint == "/api/bot/check":
                    bot = messenger.check_token()
                    audit("bot_token_ok", "admin", "manual check")
                    return self.output({"ok": True, "login": bot["login"]})
                if endpoint == "/api/test":
                    dest = validate_destination({data["kind"]: data["target"]})
                    message_id = messenger.send("✅ Mail2Yandex: проверка подключения бота.", dest)
                    audit("test_sent", next(iter(dest)), str(message_id))
                    return self.output({"ok": True})
                if endpoint not in ("/api/save", "/api/delete", "/api/recipient/add", "/api/recipient/delete"):
                    return self.output({"error": "Not found"}, 404)
                address = data["address"].strip().casefold()
                if not re.fullmatch(r"[^\s@]+@[^\s@]+", address):
                    raise ValueError("Invalid email address")
                with gate:
                    doc = self.read_routes()
                    routes = doc.setdefault("routes", {})
                    if endpoint in ("/api/save", "/api/recipient/add"):
                        destination = validate_destination({data["kind"]: data["target"]})
                        if endpoint == "/api/save":
                            routes[address] = destination
                        else:
                            existing = next((v for k, v in routes.items() if k.casefold() == address), None)
                            routes = {k: v for k, v in routes.items() if k.casefold() != address}
                            doc["routes"] = routes
                            recipients = validate_destinations(existing) if existing is not None else []
                            if destination not in recipients:
                                recipients.append(destination)
                            routes[address] = recipients
                    elif endpoint == "/api/recipient/delete":
                        existing = next((v for k, v in routes.items() if k.casefold() == address), None)
                        if existing is None:
                            raise ValueError("Route not found")
                        destination = validate_destination({data["kind"]: data["target"]})
                        current = validate_destinations(existing)
                        remaining = [v for v in current if v != destination]
                        if len(remaining) == len(current):
                            raise ValueError("Recipient not found")
                        routes = {k: v for k, v in routes.items() if k.casefold() != address}
                        if remaining:
                            routes[address] = remaining
                        doc["routes"] = routes
                    else:
                        if address not in {key.casefold() for key in routes}:
                            raise ValueError("Route not found")
                        routes = {k: v for k, v in routes.items() if k.casefold() != address}
                        doc["routes"] = routes
                    Router(routes, doc.get("default"))
                    fd, tmp = tempfile.mkstemp(prefix=".routes-", dir=str(path.parent))
                    try:
                        with os.fdopen(fd, "w", encoding="utf-8") as output:
                            json.dump(doc, output, ensure_ascii=False, indent=2)
                            output.write("\n")
                            output.flush()
                            os.fsync(output.fileno())
                        os.replace(tmp, path)
                    finally:
                        if os.path.exists(tmp):
                            os.unlink(tmp)
                audit("route_changed", "admin", endpoint.removeprefix("/api/"))
                return self.output({"ok": True})
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                return self.output({"error": "Invalid request: " + type(exc).__name__}, 400)
            except ImapAccountCheckError as exc:
                audit("account_test_failed", "imap", str(exc))
                return self.output({"error": str(exc)}, 502)
            except MessengerDeliveryError as exc:
                audit("bot_api_failed", "admin", str(exc))
                return self.output({"error": str(exc)}, 502)
            except Exception as exc:
                audit("admin_error", "admin", type(exc).__name__)
                return self.output({"error": "Operation failed"}, 502)

    server = ThreadingHTTPServer((host, port), AdminHandler)
    threading.Thread(target=server.serve_forever, daemon=True, name="admin-web").start()
    return server
