"""Работа с телефона: маленький веб-сервер внутри Читалки.

Телефон открывает ссылку (по Wi-Fi или через защищённый туннель Cloudflare), вводит PIN, загружает книгу
(PDF, EPUB, FB2, DOCX, TXT…), выбирает голос — Читалка на компьютере делает аудио рассказ, а телефон
показывает ход работы, даёт послушать и скачать готовый MP3.

Сервер ничего не знает об окне программы: всё, что ему нужно, даёт объект bridge (см. reader.WebBridge).
"""
import hashlib
import http.server
import json
import os
import re
import secrets
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse

APP = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(APP, "web", "index.html")
DEFAULT_PORT = 8765
MAX_UPLOAD = 400 * 1048576
BOOK_EXT = (".pdf", ".epub", ".fb2", ".fb2.zip", ".docx", ".doc", ".txt", ".rtf", ".mobi", ".azw3", ".djvu",
            ".djv", ".html", ".htm", ".odt")
COOKIE = "chitalka"
NOWIN = {"creationflags": 0x08000000} if sys.platform == "win32" else {}


def lan_ip() -> str:
    """Адрес компьютера в домашней сети (без отправки чего-либо наружу)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        if not ip.startswith("127."):
            return ip
    except OSError:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


def new_pin() -> str:
    return f"{secrets.randbelow(900000) + 100000}"


def qr_image(text: str, box=6):
    """QR-код ссылки (картинка PIL) или None, если нет пакета qrcode."""
    try:
        import qrcode
        q = qrcode.QRCode(border=2, box_size=box, error_correction=qrcode.constants.ERROR_CORRECT_M)
        q.add_data(text)
        q.make(fit=True)
        return q.make_image(fill_color="black", back_color="white").convert("RGB")
    except Exception:  # noqa: BLE001
        return None


def safe_filename(name: str) -> str:
    name = os.path.basename(name.replace("\\", "/"))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:150] or "книга.pdf"


class _Guard:
    """Защита PIN от подбора: 5 ошибок с одного адреса — пауза 10 минут, 30 ошибок за час — пауза всем."""

    def __init__(self):
        self.lock = threading.Lock()
        self.fails: dict[str, list[float]] = {}
        self.all_fails: list[float] = []

    def blocked(self, ip) -> int:
        now = time.time()
        with self.lock:
            self.all_fails = [t for t in self.all_fails if now - t < 3600]
            if len(self.all_fails) >= 30:
                return int(900 - (now - self.all_fails[-1])) if now - self.all_fails[-1] < 900 else 0
            f = [t for t in self.fails.get(ip, []) if now - t < 600]
            self.fails[ip] = f
            if len(f) >= 5:
                return int(600 - (now - f[-1]))
        return 0

    def fail(self, ip):
        with self.lock:
            self.fails.setdefault(ip, []).append(time.time())
            self.all_fails.append(time.time())

    def ok(self, ip):
        with self.lock:
            self.fails.pop(ip, None)


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "Chitalka"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------ служебное
    @property
    def srv(self) -> "WebServer":
        return self.server.owner  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # тихо
        pass

    def client_ip(self) -> str:
        ip = self.client_address[0]
        if ip.startswith("127.") or ip == "::1":  # через туннель Cloudflare настоящий адрес — в заголовке
            ip = self.headers.get("CF-Connecting-IP") or self.headers.get("X-Forwarded-For", "").split(",")[0].strip() or ip
        return ip

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200, extra=None):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), extra=extra)

    def _authed(self) -> bool:
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and v and self.srv.token_ok(v):
                return True
        return False

    def _body(self, limit=65536) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("слишком большой запрос")
        return self.rfile.read(n) if n else b""

    # ------------------------------------------------------------------ GET
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        try:
            if u.path in ("/", "/index.html"):
                try:
                    with open(PAGE, "rb") as f:
                        page = f.read()
                except OSError:
                    page = b"<h1>web/index.html not found</h1>"
                return self._send(200, page, "text/html; charset=utf-8")
            if u.path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if not self._authed():
                return self._json({"error": "pin"}, 401)
            if u.path == "/api/state":
                return self._json(self.srv.bridge.state())
            if u.path == "/api/file":
                return self._file(urllib.parse.parse_qs(u.query))
            return self._json({"error": "нет такой страницы"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            self.srv.log(e)
            try:
                self._json({"error": str(e)}, 500)
            except OSError:
                pass

    def _file(self, qs):
        try:
            hid, n = qs["id"][0], int(qs.get("n", ["0"])[0])
        except (KeyError, ValueError):
            return self._json({"error": "неверный запрос"}, 400)
        path = self.srv.bridge.file_path(hid, n)
        if not path or not os.path.isfile(path):
            return self._json({"error": "файл не найден"}, 404)
        size = os.path.getsize(path)
        start, end = 0, size - 1
        rng = self.headers.get("Range", "")
        m = re.match(r"bytes=(\d*)-(\d*)", rng)
        code = 200
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            code = 206
        name = os.path.basename(path)
        ext = os.path.splitext(name)[1].lower()
        ctype = {".mp3": "audio/mpeg", ".m4b": "audio/mp4", ".txt": "text/plain; charset=utf-8"}.get(ext, "application/octet-stream")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        disp = "attachment" if qs.get("dl") else "inline"
        self.send_header("Content-Disposition", f"{disp}; filename*=UTF-8''{urllib.parse.quote(name)}")
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                b = f.read(min(1 << 18, left))
                if not b:
                    break
                self.wfile.write(b)
                left -= len(b)

    # ------------------------------------------------------------------ POST
    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        ip = self.client_ip()
        try:
            if u.path == "/api/login":
                wait = self.srv.guard.blocked(ip)
                if wait:
                    return self._json({"error": f"Слишком много попыток. Подождите {wait // 60 + 1} мин."}, 429)
                try:
                    pin = str(json.loads(self._body(1024) or b"{}").get("pin", "")).strip()
                except ValueError:
                    pin = ""
                if pin and secrets.compare_digest(pin, self.srv.pin()):
                    self.srv.guard.ok(ip)
                    tok = self.srv.new_token()
                    secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" or \
                        self.headers.get("CF-Visitor", "").find("https") >= 0 else ""
                    return self._json({"ok": True}, extra={
                        "Set-Cookie": f"{COOKIE}={tok}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Strict{secure}"})
                self.srv.guard.fail(ip)
                time.sleep(1.0)
                return self._json({"error": "Неверный PIN"}, 403)
            if not self._authed():
                return self._json({"error": "pin"}, 401)
            if u.path == "/api/upload":
                return self._upload(urllib.parse.parse_qs(u.query))
            if u.path == "/api/job":
                d = json.loads(self._body() or b"{}")
                msg = self.srv.bridge.submit(d)
                return self._json({"ok": True, "msg": msg})
            if u.path == "/api/cancel":
                d = json.loads(self._body() or b"{}")
                self.srv.bridge.cancel(str(d.get("id", "")))
                return self._json({"ok": True})
            if u.path == "/api/logout":
                return self._json({"ok": True}, extra={"Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0"})
            return self._json({"error": "нет такой команды"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except ValueError as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            self.srv.log(e)
            try:
                self._json({"error": str(e)}, 500)
            except OSError:
                pass

    def _upload(self, qs):
        name = safe_filename(qs.get("name", ["книга.pdf"])[0])
        if not name.lower().endswith(BOOK_EXT):
            return self._json({"error": "Этот формат не поддерживается. Подойдут: PDF, EPUB, FB2, DOCX, TXT, RTF, MOBI, DJVU"}, 400)
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return self._json({"error": "пустой файл"}, 400)
        if n > MAX_UPLOAD:
            return self._json({"error": f"Файл больше {MAX_UPLOAD // 1048576} МБ"}, 413)
        folder = self.srv.bridge.upload_dir()
        os.makedirs(folder, exist_ok=True)
        dest = os.path.join(folder, name)
        if os.path.exists(dest):  # такой файл уже есть — если тот же самый, не дублируем
            stem, ext = (name[:-8], name[-8:]) if name.lower().endswith(".fb2.zip") else os.path.splitext(name)
            i = 2
            while os.path.exists(dest):
                dest = os.path.join(folder, f"{stem} ({i}){ext}")
                i += 1
        tmp = dest + ".part"
        h = hashlib.sha1()
        left = n
        try:
            with open(tmp, "wb") as f:
                while left > 0:
                    b = self.rfile.read(min(1 << 18, left))
                    if not b:
                        raise ConnectionError("передача прервалась")
                    f.write(b)
                    h.update(b)
                    left -= len(b)
            same = self.srv.bridge.find_same_upload(folder, h.hexdigest(), n)
            if same:
                os.remove(tmp)
                dest = same
            else:
                os.replace(tmp, dest)
                self.srv.bridge.remember_upload(dest, h.hexdigest(), n)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return self._json({"ok": True, "file": os.path.basename(dest)})


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class WebServer:
    def __init__(self, bridge, port=DEFAULT_PORT):
        self.bridge = bridge
        self.port = port
        self.guard = _Guard()
        self.httpd = None
        self.thread = None
        self.tunnel = None

    def pin(self) -> str:
        return self.bridge.pin()

    def token_ok(self, tok) -> bool:
        return tok in self.bridge.tokens()

    def new_token(self) -> str:
        tok = secrets.token_urlsafe(24)
        self.bridge.add_token(tok)
        return tok

    def log(self, e):
        try:
            self.bridge.log(e)
        except Exception:  # noqa: BLE001
            pass

    @property
    def running(self) -> bool:
        return self.httpd is not None

    def start(self):
        if self.httpd:
            return self.port
        last = None
        for p in [self.port] + list(range(self.port + 1, self.port + 10)):
            try:
                self.httpd = _Server(("0.0.0.0", p), _Handler)
                self.port = p
                break
            except OSError as e:
                last = e
        if not self.httpd:
            raise last or OSError("нет свободного порта")
        self.httpd.owner = self  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True)
        self.thread.start()
        return self.port

    def stop(self):
        self.stop_tunnel()
        if self.httpd:
            h, self.httpd = self.httpd, None
            threading.Thread(target=lambda: (h.shutdown(), h.server_close()), daemon=True).start()

    def lan_url(self) -> str:
        return f"http://{lan_ip()}:{self.port}/"

    # ------------------------------------------------------------------ туннель Cloudflare
    def start_tunnel(self, on_url, on_fail):
        exe = cloudflared_exe()
        if not exe:
            on_fail("Нет программы cloudflared — установите компонент «Доступ с телефона из интернета».")
            return
        self.stop_tunnel()
        self.tunnel = Tunnel(exe, self.port, on_url, on_fail)
        self.tunnel.start()

    def stop_tunnel(self):
        if self.tunnel:
            self.tunnel.stop()
            self.tunnel = None


def cloudflared_exe() -> str:
    p = os.path.join(APP, "tools", "cloudflared.exe" if sys.platform == "win32" else "cloudflared")
    if os.path.isfile(p):
        return p
    import shutil
    return shutil.which("cloudflared") or ""


class Tunnel:
    """Быстрый туннель Cloudflare (trycloudflare.com): адрес https, без регистрации. Адрес меняется при каждом запуске."""

    def __init__(self, exe, port, on_url, on_fail):
        self.exe, self.port, self.on_url, self.on_fail = exe, port, on_url, on_fail
        self.proc = None
        self.url = ""
        self._stop = False

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        tries = 0
        while not self._stop and tries < 5:
            tries += 1
            self.url = ""
            try:
                self.proc = subprocess.Popen(
                    [self.exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self.port}"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **NOWIN)
            except OSError as e:
                self.on_fail(f"cloudflared не запустился: {e}")
                return
            tail = []
            t0 = time.time()
            for raw in self.proc.stdout:
                line = raw.decode("utf-8", "replace")
                tail = (tail + [line.strip()])[-8:]
                m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
                if m and not self.url:
                    self.url = m.group(0) + "/"
                    self.on_url(self.url)
                    tries = 0
            self.proc.wait()
            if self._stop:
                return
            if not self.url and time.time() - t0 < 60:
                self.on_fail("Туннель Cloudflare не открылся. Проверьте интернет.\n" + "\n".join(tail[-3:]))
                return
            time.sleep(3)  # обрыв — переподключаемся (адрес будет новый)
        if not self._stop:
            self.on_fail("Туннель Cloudflare несколько раз обрывался и остановлен.")

    def stop(self):
        self._stop = True
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass
