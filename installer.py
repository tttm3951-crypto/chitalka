"""Установщик Читалки одним кликом (собирается в «Установить Читалку.exe» через PyInstaller).

Что делает:
  1. Находит Python 3.10–3.13, а если его нет — скачивает Python 3.12 с python.org и тихо ставит
     только для текущего пользователя (права администратора не нужны).
  2. Скачивает программу с GitHub и распаковывает в выбранную папку (настройки и голоса не трогаются).
  3. Создаёт отдельное окружение Python (.venv) и ставит отмеченные компоненты.
  4. Создаёт ярлыки на рабочем столе и в меню «Пуск».
"""
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
import zipfile
from tkinter import filedialog, messagebox, ttk

REPO = "tttm3951-crypto/chitalka"
ZIP_URL = f"https://codeload.github.com/{REPO}/zip/refs/heads/main"
RAW = f"https://raw.githubusercontent.com/{REPO}/main/"
PY_URL = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
TITLE = "Установка Читалки"
NOWIN = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
KEEP = {"settings.json", "settings.json.bak", "settings.backup.json", "installed.json", "error.log",
        "Словарь произношения.txt", ".venv", "voices", "cache", "tools", "backup", "Образцы голосов", "тест", "setup"}
BG, CARD, ACC, FG, MUTED = "#F6F4EF", "#FFFFFF", "#B5713E", "#2E2E2E", "#77706A"


def clean_env(**extra):
    """Окружение для дочерних программ без следов PyInstaller (иначе Tk у Читалки искал бы свои файлы
    во временной папке установщика, которая исчезнет)."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("TCL_LIBRARY", "TK_LIBRARY", "TKPATH", "PYTHONHOME", "PYTHONPATH", "_MEIPASS2")
           and not k.startswith("_PYI")}
    env.update(extra)
    return env


def res(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def load_components():
    try:  # самый свежий список — с GitHub
        with urllib.request.urlopen(urllib.request.Request(RAW + "components.json", headers={"User-Agent": "Chitalka"}),
                                    timeout=8) as r:
            return json.loads(r.read().decode("utf-8"))["components"]
    except Exception:  # noqa: BLE001
        with open(res("components.json"), encoding="utf-8") as f:
            return json.load(f)["components"]


def default_dir():
    if os.path.isdir(r"C:\ИИ Клод"):
        return r"C:\ИИ Клод\Читалка"
    return os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), "Читалка")


def has_gpu() -> bool:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return False
    try:
        return subprocess.call([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, **NOWIN) == 0
    except Exception:  # noqa: BLE001
        return False


def find_python():
    """Подходящий Python (3.10–3.13) или None."""
    cands = []
    if shutil.which("py"):
        for v in ("3.12", "3.13", "3.11", "3.10"):
            cands.append(["py", f"-{v}"])
    la = os.environ.get("LOCALAPPDATA", "")
    for v in ("312", "313", "311", "310"):
        for root in (os.path.join(la, "Programs", "Python"), r"C:\Program Files", r"C:\Python" + v):
            p = os.path.join(root, f"Python{v}", "python.exe") if not root.endswith(v) else os.path.join(root, "python.exe")
            if os.path.isfile(p):
                cands.append([p])
    if shutil.which("python"):
        cands.append([shutil.which("python")])
    for c in cands:
        try:
            r = subprocess.run(c + ["-c", "import sys,tkinter,venv;print(sys.executable);print(sys.version_info[:2])"],
                               capture_output=True, text=True, timeout=30, env=clean_env(), **NOWIN)
            if r.returncode == 0:
                exe, ver = r.stdout.strip().splitlines()[:2]
                major, minor = [int(x) for x in re.findall(r"\d+", ver)[:2]]
                if (3, 10) <= (major, minor) <= (3, 13) and "WindowsApps" not in exe:
                    return exe
        except Exception:  # noqa: BLE001
            continue
    return None


class Installer:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.busy = False
        root.title(TITLE)
        root.configure(bg=BG)
        root.geometry("760x700")
        root.minsize(680, 560)
        try:
            root.iconbitmap(res("chitalka.ico"))
        except Exception:  # noqa: BLE001
            pass
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=FG, font=("Segoe UI", 11))
        st.configure("Card.TFrame", background=CARD)
        st.configure("Card.TLabel", background=CARD)
        st.configure("Card.TCheckbutton", background=CARD, font=("Segoe UI", 11, "bold"))
        st.configure("Muted.TLabel", background=CARD, foreground=MUTED, font=("Segoe UI", 9))
        st.configure("Head.TLabel", background=BG, foreground=ACC, font=("Segoe UI", 18, "bold"))
        st.configure("Go.TButton", font=("Segoe UI", 12, "bold"), padding=(18, 8), background="#F4D2B4")
        st.map("Go.TButton", background=[("active", "#EEC29D"), ("disabled", "#E7E3DA")])
        st.configure("TButton", padding=(10, 5))
        st.configure("Horizontal.TProgressbar", background=ACC, troughcolor="#E7E3DA")

        top = ttk.Frame(root, padding=(18, 14, 18, 6))
        top.pack(fill="x")
        ttk.Label(top, text="📖 Читалка", style="Head.TLabel").pack(anchor="w")
        ttk.Label(top, text="Чтение книг вслух и аудио рассказы в MP3. Установка займёт несколько минут; "
                            "всё нужное скачается само.", wraplength=700).pack(anchor="w", pady=(2, 0))

        fr = ttk.Frame(root, padding=(18, 6))
        fr.pack(fill="x")
        ttk.Label(fr, text="Папка программы:").pack(side="left")
        self.dir_var = tk.StringVar(value=default_dir())
        ttk.Entry(fr, textvariable=self.dir_var, font=("Segoe UI", 11)).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(fr, text="Обзор…", command=self.browse).pack(side="left")
        self.dir_var.trace_add("write", lambda *_: self.root.after(300, self.mark_installed))

        self.gpu = has_gpu()
        ttk.Label(root, padding=(18, 0), foreground=MUTED, text=(
            "Видеокарта NVIDIA найдена — нейросетевые голоса будут работать быстро." if self.gpu else
            "Видеокарта NVIDIA не найдена — нейросетевые голоса будут работать на процессоре (медленнее).")).pack(anchor="w")

        box = tk.Frame(root, bg=CARD, highlightthickness=1, highlightbackground="#E4DED2")
        box.pack(fill="both", expand=True, padx=18, pady=8)
        cv = tk.Canvas(box, bg=CARD, highlightthickness=0)
        sb = ttk.Scrollbar(box, orient="vertical", command=cv.yview)
        inner = ttk.Frame(cv, style="Card.TFrame", padding=(12, 8))
        inner.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.create_window((0, 0), window=inner, anchor="nw")
        cv.configure(yscrollcommand=sb.set)
        cv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        cv.bind_all("<MouseWheel>", lambda e: cv.yview_scroll(-e.delta // 120, "units"))
        ttk.Label(inner, text="Что установить (можно доустановить потом: Справка → 🧩 Компоненты):",
                  style="Card.TLabel").pack(anchor="w", pady=(0, 6))
        self.vars, self.cbs = {}, {}
        for c in load_components():
            v = tk.BooleanVar(value=bool(c.get("default") or c.get("required")))
            cb = ttk.Checkbutton(inner, text=f"{c['title']}   ·   {c.get('size', '')}", variable=v,
                                 style="Card.TCheckbutton")
            cb.pack(anchor="w", pady=(6, 0))
            if c.get("required"):
                cb.state(["disabled"])
            ttk.Label(inner, text=c.get("desc", ""), style="Muted.TLabel", wraplength=640,
                      justify="left").pack(anchor="w", padx=(26, 0))
            self.vars[c["id"]], self.cbs[c["id"]] = v, cb
        self.vars["xtts"].trace_add("write", lambda *_: self.vars["xtts"].get() and self.vars["silero"].set(True))

        st_fr = ttk.Frame(root, padding=(18, 0))
        st_fr.pack(fill="x")
        self.step = ttk.Label(st_fr, text="", font=("Segoe UI", 11, "bold"))
        self.step.pack(anchor="w")
        self.pb = ttk.Progressbar(st_fr, maximum=100)
        self.pb.pack(fill="x", pady=(4, 0))
        self.total = ttk.Label(st_fr, text="", foreground=MUTED)
        self.total.pack(anchor="w")
        self.log = tk.Text(root, height=6, font=("Consolas", 9), bg="#FBFAF7", relief="flat", wrap="word")
        self.log.pack(fill="x", padx=18, pady=(4, 0))

        bt = ttk.Frame(root, padding=(18, 10))
        bt.pack(fill="x")
        self.go_btn = ttk.Button(bt, text="⬇ Установить", style="Go.TButton", command=self.start)
        self.go_btn.pack(side="right")
        self.run_btn = ttk.Button(bt, text="▶ Запустить Читалку", style="Go.TButton", command=self.launch)
        self.close_btn = ttk.Button(bt, text="Закрыть", command=self.on_close)
        self.close_btn.pack(side="right", padx=8)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.mark_installed()
        self.poll()

    # ------------------------------------------------------------------ интерфейс
    def browse(self):
        d = filedialog.askdirectory(title="Куда установить Читалку", initialdir=os.path.dirname(self.dir_var.get()))
        if d:
            d = os.path.normpath(d)
            if os.path.basename(d).lower() not in ("читалка", "chitalka") and os.listdir(d):
                d = os.path.join(d, "Читалка")
            self.dir_var.set(d)

    def mark_installed(self):
        """Если в папке уже есть Читалка — установленное отмечено ✔, кнопка становится «Обновить»."""
        d = self.dir_var.get()
        have = set()
        try:
            with open(os.path.join(d, "installed.json"), encoding="utf-8") as f:
                have = set(json.load(f).get("components", []))
        except (OSError, ValueError):
            pass
        v, sp = os.path.join(d, "voices"), os.path.join(d, ".venv", "Lib", "site-packages")
        for cid, probe in (("piper_ru", os.path.join(v, "ru_RU-irina-medium.onnx")),
                           ("piper_more", os.path.join(v, "kk_KZ-issai-high.onnx")),
                           ("silero", os.path.join(v, "silero", "speakers.json")),
                           ("phone", os.path.join(d, "tools", "cloudflared.exe")),
                           ("xtts", os.path.join(sp, "TTS")), ("ffmpeg", os.path.join(sp, "imageio_ffmpeg")),
                           ("ocr", os.path.join(sp, "winrt", "windows", "media", "ocr"))):
            if os.path.exists(probe):
                have.add(cid)
        exists = os.path.isfile(os.path.join(d, "reader.pyw"))
        if exists:
            have.add("core")
        for cid, cb in self.cbs.items():
            t = cb.cget("text").lstrip("✔ ")
            cb.config(text=("✔ " if cid in have else "") + t)
        self.go_btn.config(text="⬇ Обновить и доустановить" if exists else "⬇ Установить")

    def say(self, m):
        self.q.put(("log", m))

    def poll(self):
        try:
            while True:
                k, v = self.q.get_nowait()
                if k == "log":
                    self.log.insert("end", v + "\n")
                    self.log.see("end")
                elif k == "step":
                    self.step.config(text=v)
                    self.pb.config(value=0)
                    self.log.insert("end", "▶ " + v + "\n")
                    self.log.see("end")
                elif k == "pct":
                    self.pb.config(value=v)
                elif k == "total":
                    self.total.config(text=v)
                elif k == "done":
                    self.finish(*v)
        except queue.Empty:
            pass
        self.root.after(80, self.poll)

    def on_close(self):
        if self.busy and not messagebox.askyesno(TITLE, "Установка ещё идёт. Прервать?\n"
                                                        "Можно будет запустить установщик снова — он продолжит."):
            return
        self.root.destroy()
        os._exit(0)

    def start(self):
        d = os.path.abspath(self.dir_var.get().strip())
        if len(d) > 120:
            messagebox.showwarning(TITLE, "Слишком длинный путь к папке. Выберите покороче.")
            return
        comps = [c for c, v in self.vars.items() if v.get()]
        if "core" not in comps:
            comps.insert(0, "core")
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".probe")
            open(probe, "w").close()
            os.remove(probe)
        except OSError as e:
            messagebox.showerror(TITLE, f"В эту папку нельзя записать:\n{d}\n\n{e}\n\nВыберите другую.")
            return
        free = shutil.disk_usage(d).free / 1073741824
        need = 1 + (3 if "silero" in comps and self.gpu else 0.5 if "silero" in comps else 0) + (2.5 if "xtts" in comps else 0)
        if free < need and not messagebox.askyesno(TITLE, f"На диске свободно {free:.1f} ГБ, а нужно около {need:.1f} ГБ.\n"
                                                          "Продолжить всё равно?"):
            return
        self.busy = True
        self.go_btn.state(["disabled"])
        for cb in self.cbs.values():
            cb.state(["disabled"])
        self.target = d
        threading.Thread(target=self.work, args=(d, comps), daemon=True).start()

    # ------------------------------------------------------------------ работа
    def download(self, url, dest):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 Chitalka-installer"})
        with urllib.request.urlopen(req, timeout=60) as r, open(dest + ".part", "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                b = r.read(1 << 17)
                if not b:
                    break
                f.write(b)
                done += len(b)
                if total:
                    self.q.put(("pct", done * 100 // total))
        os.replace(dest + ".part", dest)

    def work(self, d, comps):
        logf = None
        try:
            logf = open(os.path.join(d, "install.log"), "a", encoding="utf-8")
            logf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {comps}\n")
        except OSError:
            pass

        def say(m):
            self.say(m)
            if logf:
                logf.write(m + "\n")
                logf.flush()
        total_steps = 3 + len(comps)
        n = [0]

        def step(m):
            n[0] += 1
            self.q.put(("step", m))
            self.q.put(("total", f"Шаг {min(n[0], total_steps)} из {total_steps}"))
            if logf:
                logf.write("== " + m + "\n")
        try:
            step("Ищу Python")
            py = find_python()
            if py:
                say(f"  найден: {py}")
            else:
                step("Скачиваю Python 3.12 с python.org (≈ 25 МБ)")
                tmp = os.path.join(os.environ.get("TEMP", d), "python-3.12-setup.exe")
                self.download(PY_URL, tmp)
                say("  ставлю Python только для этого пользователя (права администратора не нужны)…")
                self.q.put(("pct", 50))
                r = subprocess.run([tmp, "/quiet", "InstallAllUsers=0", "PrependPath=0", "Include_launcher=1",
                                    "InstallLauncherAllUsers=0", "Include_test=0", "Include_doc=0", "Include_tcltk=1",
                                    "Include_pip=1", "Shortcuts=0", "AssociateFiles=0"], env=clean_env(), **NOWIN)
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                py = find_python()
                if not py:
                    raise RuntimeError(f"Python не установился (код {r.returncode}). Установите Python 3.12 с python.org "
                                       "вручную и запустите установщик снова.")
                say(f"  готово: {py}")

            step("Скачиваю программу с GitHub")
            zp = os.path.join(os.environ.get("TEMP", d), "chitalka-main.zip")
            self.download(ZIP_URL, zp)
            cnt = 0
            with zipfile.ZipFile(zp) as z:
                names = z.namelist()
                root = names[0].split("/")[0] + "/"
                if root + "reader.pyw" not in names:
                    raise RuntimeError("скачанный архив не похож на Читалку")
                for nm in names:
                    rel = nm[len(root):]
                    if not rel or nm.endswith("/") or rel.split("/")[0] in KEEP:
                        continue
                    dest = os.path.join(d, *rel.split("/"))
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with open(dest + ".new", "wb") as f:
                        f.write(z.read(nm))
                    os.replace(dest + ".new", dest)
                    cnt += 1
            os.remove(zp)
            say(f"  файлов программы: {cnt}")

            step("Готовлю окружение Python")
            vpy = os.path.join(d, ".venv", "Scripts", "python.exe")
            if not os.path.isfile(vpy):
                r = subprocess.run([py, "-m", "venv", os.path.join(d, ".venv")], capture_output=True, text=True,
                                   env=clean_env(), **NOWIN)
                if r.returncode != 0 or not os.path.isfile(vpy):
                    raise RuntimeError("не удалось создать окружение Python:\n" + (r.stderr or r.stdout)[-600:])
            say("  готово")

            env = clean_env(PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
            p = subprocess.Popen([vpy, "-u", os.path.join(d, "install_helper.py"), "--components", ",".join(comps)]
                                 + ([] if os.environ.get("CHITALKA_NO_SHORTCUT") else ["--shortcut"]), cwd=d, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, **NOWIN)
            fails = []
            for raw in p.stdout:
                line = raw.decode("utf-8", "replace").rstrip()
                if line.startswith("@@STEP "):
                    step(line[7:])
                elif line.startswith("@@PCT "):
                    self.q.put(("pct", int(line.split()[1])))
                elif line.startswith("@@FAIL "):
                    parts = line.split(" ", 2)
                    fails.append(parts[1])
                    say("  ✖ не получилось: " + (parts[2] if len(parts) > 2 else ""))
                elif line.startswith("@@OK "):
                    say("  ✔ готово")
                elif line.startswith(("@@COMP", "@@END")):
                    pass
                else:
                    say(line)
            code = p.wait()
            if code != 0 and "core" in fails:
                raise RuntimeError("не установилась основная часть программы — проверьте интернет и запустите снова")
            self.q.put(("done", (True, fails)))
        except Exception as e:  # noqa: BLE001
            say(f"ОШИБКА: {e}")
            self.q.put(("done", (False, str(e))))
        finally:
            if logf:
                logf.close()

    def finish(self, ok, info):
        self.busy = False
        self.go_btn.state(["!disabled"])
        for cid, cb in self.cbs.items():
            if cid != "core":
                cb.state(["!disabled"])
        self.mark_installed()
        if ok:
            self.pb.config(value=100)
            self.step.config(text="✔ Читалка установлена" + (f" (не получилось: {', '.join(info)} — можно доустановить "
                                                             "позже)" if info else ""))
            self.total.config(text="Ярлык «Читалка» — на рабочем столе и в меню «Пуск». Обновления программа проверяет сама.")
            self.go_btn.pack_forget()
            self.run_btn.pack(side="right")
        else:
            self.step.config(text="✖ Установка не завершена")
            messagebox.showerror(TITLE, f"Не получилось:\n{info}\n\nПроверьте интернет и нажмите «Установить» ещё раз — "
                                        "уже скачанное повторно качаться не будет.\nЖурнал: install.log в папке программы.")

    def launch(self):
        d = self.target
        pyw = os.path.join(d, ".venv", "Scripts", "pythonw.exe")
        subprocess.Popen([pyw, os.path.join(d, "reader.pyw")], cwd=d, close_fds=True, env=clean_env(), **NOWIN)
        self.root.after(500, lambda: (self.root.destroy(), os._exit(0)))


def main():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
    root = tk.Tk()
    Installer(root)
    root.mainloop()


if __name__ == "__main__":
    main()
