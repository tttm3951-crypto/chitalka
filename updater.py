"""Обновления Читалки с GitHub и учёт установленных компонентов.

Из программы:  check() → есть ли новая версия; download_update() → zip; start_apply() — программа закрывается,
а этот же файл, запущенный отдельно (--apply), заменяет файлы, доустанавливает пакеты и снова открывает Читалку.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

APP = os.path.dirname(os.path.abspath(__file__))
REPO = "tttm3951-crypto/chitalka"
BRANCH = "main"
RAW = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/"
ZIP_URL = f"https://codeload.github.com/{REPO}/zip/refs/heads/{BRANCH}"
PAGE_URL = f"https://github.com/{REPO}"
RELEASE_URL = f"https://github.com/{REPO}/releases/latest"
NOWIN = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
# Это никогда не трогаем при обновлении: настройки, голоса, кэш, словарь, окружение Python
KEEP = {"settings.json", "settings.json.bak", "settings.backup.json", "installed.json", "error.log",
        "Словарь произношения.txt", ".venv", "voices", "cache", "tools", "backup", "Образцы голосов", "тест"}


def _get(url, timeout=20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Chitalka-updater", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def local_version() -> dict:
    try:
        with open(os.path.join(APP, "version.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"version": "0.0.0"}


def vtuple(v: str):
    out = []
    for p in str(v).split("."):
        try:
            out.append(int("".join(ch for ch in p if ch.isdigit()) or 0))
        except ValueError:
            out.append(0)
    return tuple(out + [0] * (3 - len(out)))


def check(timeout=15) -> dict | None:
    """Новая версия на GitHub (словарь из version.json) или None, если обновлять нечего."""
    remote = json.loads(_get(RAW + "version.json?t=" + str(int(time.time())), timeout).decode("utf-8"))
    if vtuple(remote.get("version", "0")) > vtuple(local_version().get("version", "0")):
        return remote
    return None


def components() -> list[dict]:
    try:
        with open(os.path.join(APP, "components.json"), encoding="utf-8") as f:
            return json.load(f)["components"]
    except (OSError, ValueError, KeyError):
        return []


def detect_installed() -> set:
    """Что уже стоит. Для установок, сделанных до появления installed.json, — определяем по файлам и пакетам."""
    have = set()
    try:
        with open(os.path.join(APP, "installed.json"), encoding="utf-8") as f:
            have |= set(json.load(f).get("components", []))
    except (OSError, ValueError):
        pass
    v = os.path.join(APP, "voices")
    have.add("core")
    if os.path.isfile(os.path.join(v, "ru_RU-irina-medium.onnx")):
        have.add("piper_ru")
    if any(os.path.isfile(os.path.join(v, f)) for f in ("kk_KZ-issai-high.onnx", "en_US-ryan-high.onnx")):
        have.add("piper_more")
    sil = os.path.join(v, "silero")
    if os.path.isdir(sil) and any(f.endswith(".pt") for f in os.listdir(sil)):
        have.add("silero")
    import importlib.util as iu
    if iu.find_spec("TTS") is not None:
        have.add("xtts")
    if iu.find_spec("imageio_ffmpeg") is not None:
        have.add("ffmpeg")
    try:
        if iu.find_spec("winrt.windows.media.ocr") is not None:
            have.add("ocr")
    except (ImportError, ValueError):
        pass
    if os.path.isfile(os.path.join(APP, "tools", "cloudflared.exe")):
        have.add("phone")
    return have


def download_update(progress=lambda p: None) -> str:
    """Скачивает архив программы во временную папку, возвращает путь к zip."""
    d = os.path.join(APP, "cache", "update")
    os.makedirs(d, exist_ok=True)
    dest = os.path.join(d, "chitalka.zip")
    req = urllib.request.Request(ZIP_URL, headers={"User-Agent": "Chitalka-updater"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest + ".part", "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            b = r.read(1 << 16)
            if not b:
                break
            f.write(b)
            done += len(b)
            progress(done * 100 // total if total else min(95, done // 20000))
    os.replace(dest + ".part", dest)
    with zipfile.ZipFile(dest) as z:
        if not any(n.endswith("/reader.pyw") for n in z.namelist()):
            raise RuntimeError("скачанный архив не похож на Читалку")
    return dest


def extract_into(zip_path: str, target: str, log=print) -> list[str]:
    """Распаковывает программу поверх target, не трогая KEEP. Старые файлы — в backup/<версия>."""
    changed = []
    old_ver = local_version().get("version", "old") if os.path.abspath(target) == APP else "old"
    bdir = os.path.join(target, "backup", f"{old_ver}-{time.strftime('%Y%m%d-%H%M%S')}")
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        root = names[0].split("/")[0] + "/"
        for n in names:
            if not n.startswith(root) or n.endswith("/"):
                continue
            rel = n[len(root):]
            if not rel or rel.split("/")[0] in KEEP:
                continue
            dest = os.path.join(target, *rel.split("/"))
            data = z.read(n)
            if os.path.isfile(dest):
                with open(dest, "rb") as f:
                    if f.read() == data:
                        continue
                os.makedirs(os.path.dirname(os.path.join(bdir, rel)), exist_ok=True)
                shutil.copy2(dest, os.path.join(bdir, *rel.split("/")))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = dest + ".new"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, dest)
            changed.append(rel)
            log(f"  обновлён: {rel}")
    # хранить только 3 последние резервные копии
    broot = os.path.join(target, "backup")
    if os.path.isdir(broot):
        olds = sorted(os.listdir(broot))
        for o in olds[:-3]:
            shutil.rmtree(os.path.join(broot, o), ignore_errors=True)
    return changed


def python_exe(gui=False) -> str:
    venv = os.path.join(APP, ".venv", "Scripts" if sys.platform == "win32" else "bin")
    exe = os.path.join(venv, ("pythonw.exe" if gui else "python.exe") if sys.platform == "win32" else "python")
    return exe if os.path.isfile(exe) else sys.executable


def start_apply(zip_path: str):
    """Запускает применение обновления отдельным процессом; вызывающая программа после этого закрывается."""
    subprocess.Popen([python_exe(gui=True), os.path.join(APP, "updater.py"), "--apply", zip_path,
                      "--wait", str(os.getpid())], cwd=APP, close_fds=True, **NOWIN)


def start_restart():
    """Перезапуск программы: новый экземпляр откроется, когда этот закроется."""
    subprocess.Popen([python_exe(gui=True), os.path.join(APP, "updater.py"), "--restart",
                      "--wait", str(os.getpid())], cwd=APP, close_fds=True, **NOWIN)


def start_components(comps: list[str]) -> subprocess.Popen:
    """Установка компонентов в фоне; вывод — строки install_helper (с префиксами @@)."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    return subprocess.Popen([python_exe(), "-u", os.path.join(APP, "install_helper.py"), "--components",
                             ",".join(comps)], cwd=APP, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=env, **NOWIN)


def _wait_pid(pid: int, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if sys.platform == "win32":
                import ctypes
                h = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
                if not h:
                    return
                r = ctypes.windll.kernel32.WaitForSingleObject(h, 500)
                ctypes.windll.kernel32.CloseHandle(h)
                if r == 0:
                    return
            else:
                os.kill(pid, 0)
                time.sleep(0.5)
        except OSError:
            return


def _apply_gui(zip_path: str, pid: int):
    """Маленькое окно «Обновляю Читалку…»: замена файлов, доустановка пакетов, перезапуск."""
    import threading
    import tkinter as tk
    from tkinter import ttk
    root = tk.Tk()
    root.title("Обновление Читалки")
    root.geometry("560x300")
    lbl = ttk.Label(root, text="Жду, пока Читалка закроется…", font=("Segoe UI", 11))
    lbl.pack(padx=14, pady=(14, 6), anchor="w")
    bar = ttk.Progressbar(root, mode="indeterminate")
    bar.pack(fill="x", padx=14)
    bar.start(12)
    txt = tk.Text(root, height=10, font=("Consolas", 9), wrap="word")
    txt.pack(fill="both", expand=True, padx=14, pady=10)
    q = []

    def log(m):
        q.append(("log", m))

    def work():
        try:
            _wait_pid(pid)
            q.append(("lbl", "Заменяю файлы программы…"))
            old_req = _read(os.path.join(APP, "requirements.txt"))
            changed = extract_into(zip_path, APP, log)
            log(f"Файлов обновлено: {len(changed)}")
            if _read(os.path.join(APP, "requirements.txt")) != old_req:
                q.append(("lbl", "Доустанавливаю пакеты Python…"))
                r = subprocess.run([python_exe(), "-m", "pip", "install", "-q", "--disable-pip-version-check",
                                    "-r", os.path.join(APP, "requirements.txt")],
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", **NOWIN)
                log("пакеты: " + ("готово" if r.returncode == 0 else "ошибка\n" + (r.stdout + r.stderr)[-800:]))
            try:
                os.remove(zip_path)
            except OSError:
                pass
            q.append(("lbl", f"Готово: версия {local_version().get('version')}. Запускаю Читалку…"))
            time.sleep(1.2)
            subprocess.Popen([python_exe(gui=True), os.path.join(APP, "reader.pyw")], cwd=APP, close_fds=True, **NOWIN)
            q.append(("close", None))
        except Exception as e:  # noqa: BLE001
            q.append(("lbl", "Не получилось обновить: " + str(e)))
            log("Программа осталась прежней версии (изменённые файлы — в папке backup). "
                "Запустите Читалку ярлыком и попробуйте ещё раз.")
            q.append(("stop", None))

    def poll():
        while q:
            k, v = q.pop(0)
            if k == "log":
                txt.insert("end", v + "\n")
                txt.see("end")
            elif k == "lbl":
                lbl.config(text=v)
            elif k == "stop":
                bar.stop()
            elif k == "close":
                root.after(800, root.destroy)
        root.after(100, poll)

    threading.Thread(target=work, daemon=True).start()
    poll()
    root.mainloop()


def _read(p):
    try:
        with open(p, "rb") as f:
            return f.read()
    except OSError:
        return b""


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--apply" in a:
        zp = a[a.index("--apply") + 1]
        pid = int(a[a.index("--wait") + 1]) if "--wait" in a else 0
        _apply_gui(zp, pid)
    elif "--restart" in a:
        _wait_pid(int(a[a.index("--wait") + 1]) if "--wait" in a else 0, timeout=120)
        time.sleep(0.5)
        subprocess.Popen([python_exe(gui=True), os.path.join(APP, "reader.pyw")], cwd=APP, close_fds=True, **NOWIN)
    elif "--check" in a:
        print(check() or "обновлений нет")
    else:
        print("версия", local_version().get("version"), "| установлено:", ", ".join(sorted(detect_installed())))
