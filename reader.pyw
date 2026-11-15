"""
Читалка — чтение книг вслух.
Edge-TTS (живые голоса Microsoft, онлайн) + Piper (офлайн), скорость, тон, громкость,
подсветка фразы с автопрокруткой, закладки, сохранение в MP3.
"""
from __future__ import annotations

import bisect
import errno
import os
import queue
import re
import shutil
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)

import errors  # noqa: E402  (без внешних зависимостей)


def _startup_fail(e: BaseException):
    """Программа не может запуститься — объясняем почему и что сделать (pythonw иначе молча закрылся бы)."""
    try:
        with open(os.path.join(APP_DIR, "error.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("\n=== %Y-%m-%d %H:%M:%S запуск ===\n"))
            f.write("".join(traceback.format_exception(type(e), e, e.__traceback__)))
    except Exception:  # noqa: BLE001
        pass
    pr = errors.explain(e, "запуск программы")
    try:
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror("Читалка не запустилась",
                             f"{pr.title}\n\nПочему: {pr.cause}\n\nЧто сделать: {pr.fix}\n\n"
                             f"Подробности записаны в файл:\n{os.path.join(APP_DIR, 'error.log')}")
        r.destroy()
    except Exception:  # noqa: BLE001
        pass
    sys.exit(1)


try:
    import formats  # noqa: E402
    import speech  # noqa: E402
    from speech import VoiceSettings  # noqa: E402
except Exception as _e:  # noqa: BLE001
    _startup_fail(_e)

APP_TITLE = "Читалка"
LIBRARY_GUESS = [r"C:\ИИ Клод\моя библиотека", os.path.join(os.path.dirname(APP_DIR), "моя библиотека")]
AUDIO_FOLDER = "05_Озвучка программой"

THEMES = {
    "Светлая": dict(bg="#ffffff", fg="#1f1f1f", hl="#ffe58a", hlfg="#000000", sel="#cfe3ff"),
    "Сепия": dict(bg="#f5ecd9", fg="#3b2f20", hl="#f2c94c", hlfg="#000000", sel="#e3d3b0"),
    "Тёмная": dict(bg="#1e1f22", fg="#d8d8d8", hl="#5a4a00", hlfg="#ffffff", sel="#334466"),
}


def library_root() -> str | None:
    for p in LIBRARY_GUESS:
        if os.path.isdir(p):
            return p
    return None


def find_audio_base(book_path: str) -> str:
    """Ищет папку «05_Озвучка программой» вверх от книги, затем в библиотеке."""
    d = os.path.dirname(os.path.abspath(book_path))
    for _ in range(6):
        cand = os.path.join(d, AUDIO_FOLDER)
        if os.path.isdir(cand):
            return cand
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    lib = library_root()
    if lib:
        return os.path.join(lib, AUDIO_FOLDER)
    return os.path.join(APP_DIR, "Озвучка")


def book_stem(path: str) -> str:
    name = os.path.basename(path)
    low = name.lower()
    if low.endswith(".fb2.zip"):
        return name[:-8]
    return os.path.splitext(name)[0]


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = speech.Bookmarks()
        self.q: queue.Queue = queue.Queue()
        self.book: formats.Book | None = None
        self.chunks: list[speech.Chunk] = []
        self.chunk_starts: list[int] = []
        self.chapters_ci: list[tuple[str, int]] = []
        self.cur = 0
        self.paused = False
        self._settings_job = None

        self.speaker = speech.Speaker()
        self.speaker.on_status = lambda m: self.q.put(("status", m))
        self._last_err = (None, 0.0)
        self.out = speech.AudioOut()  # без звукового устройства не падает: out.ok = False
        self.reader = speech.ReadAloud(self.speaker, self.out, lambda k, v: self.q.put((k, v)))

        self._build_ui()
        self._load_settings()
        root.report_callback_exception = self._tk_exception
        threading.excepthook = self._thread_exception
        self.root.after(50, self._poll)
        self.root.after(2000, self._audio_watch)
        if self.cfg.get("media_keys", True):
            self.root.after(1000, lambda: self.media_keys(True))
        self._hot_timer = self.root.after(8000, self._hot_scan)
        self.root.after(1500, self._web_autostart)
        self.root.after(20000, self._auto_update_check)
        if self.cfg.problem:
            self.root.after(400, lambda: messagebox.showwarning(
                APP_TITLE, self.cfg.problem + "\n\nЧто сделать: проверьте очередь книг и места чтения. "
                                             "Остальное программа восстановит сама."))
        if not self.out.ok:
            self.root.after(700, lambda: self.show_error(self.out.error, "запуске звука"))
        self._refresh_catalog_bg()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        last = self.cfg.get("last_book")
        args = [a for a in sys.argv[1:] if os.path.isfile(a)]
        if args:
            self.add_to_queue(args)
            self.open_book(args[0])
        elif last and os.path.isfile(last):
            self.open_book(last)

    # ------------------------------------------------------------------ интерфейс
    PAL = {  # мягкие цвета по смыслу панелей
        "Win": dict(bg="#F6F4EF", btn="#E7E3DA", act="#DAD5CA", fg="#3A3A3A", head="#7A7468"),
        "Queue": dict(bg="#E8F3F3", btn="#CDE6E6", act="#B5DADA", fg="#1F4646", head="#3E8A8A"),
        "Read": dict(bg="#E6F2E9", btn="#CDE6D4", act="#B9DCC3", fg="#24452F", head="#4F8A62"),
        "Voice": dict(bg="#E5EDF8", btn="#CADBF1", act="#B5CCEB", fg="#22385A", head="#4C72A8"),
        "Sound": dict(bg="#EFE9F7", btn="#DCD0EE", act="#CCBCE6", fg="#3E2F5A", head="#7A62A8"),
        "Mp3": dict(bg="#FBEDE1", btn="#F4D2B4", act="#EEC29D", fg="#5A3A22", head="#B5713E"),
    }
    UI_SIZES = {"Обычный": 10, "Крупный": 12, "Очень крупный": 14}
    PRESETS = {
        "Обычный": dict(warmth=0, clarity=0, reverb=0, pause=0.15),
        "Бархатный рассказчик": dict(warmth=6, clarity=2, reverb=2, pause=0.25),
        "Чёткий диктор": dict(warmth=1, clarity=6, reverb=0, pause=0.15),
        "Уютная комната": dict(warmth=4, clarity=1, reverb=5, pause=0.2),
        "Радиоспектакль": dict(warmth=3, clarity=4, reverb=3, pause=0.3),
    }

    def _styles(self):
        import tkinter.font as tkfont
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        size = self.UI_SIZES.get(self.cfg.get("ui_size", "Крупный"), 12)
        self.f_ui = tkfont.Font(family="Segoe UI", size=size)
        self.f_bold = tkfont.Font(family="Segoe UI", size=size, weight="bold")
        self.f_head = tkfont.Font(family="Segoe UI", size=max(8, size - 2), weight="bold")
        self.f_play = tkfont.Font(family="Segoe UI", size=size + 3, weight="bold")
        self.root.option_add("*TCombobox*Listbox.font", self.f_ui)
        self.root.option_add("*Menu.font", self.f_ui)
        w = self.PAL["Win"]
        self.root.configure(bg=w["bg"])
        st.configure(".", font=self.f_ui, background=w["bg"], foreground=w["fg"])
        st.configure("TCombobox", padding=2, arrowsize=16)
        st.configure("Horizontal.TProgressbar", troughcolor="#E2DED5", background="#9FCBA9", bordercolor="#D3CEC3")
        for name, c in self.PAL.items():
            st.configure(f"{name}.TFrame", background=c["bg"])
            st.configure(f"{name}.TLabel", background=c["bg"], foreground=c["fg"])
            st.configure(f"{name}.Head.TLabel", background=c["bg"], foreground=c["head"], font=self.f_head)
            st.configure(f"{name}.Value.TLabel", background=c["bg"], foreground=c["head"], font=self.f_bold)
            st.configure(f"{name}.TCheckbutton", background=c["bg"], foreground=c["fg"])
            st.map(f"{name}.TCheckbutton", background=[("active", c["bg"])])
            st.configure(f"{name}.TRadiobutton", background=c["bg"], foreground=c["fg"])
            st.map(f"{name}.TRadiobutton", background=[("active", c["bg"])])
            st.configure(f"{name}.TButton", background=c["btn"], foreground=c["fg"], bordercolor=c["act"],
                         lightcolor=c["btn"], darkcolor=c["act"], padding=(8, 2))
            st.map(f"{name}.TButton", background=[("pressed", c["act"]), ("active", c["act"])])
            st.configure(f"{name}.Horizontal.TScale", background=c["head"], troughcolor=c["btn"],
                         bordercolor=c["act"], lightcolor=c["head"], darkcolor=c["head"])
        v = self.PAL["Voice"]
        st.configure("Voice.TNotebook", background=v["bg"], borderwidth=0, tabmargins=(0, 2, 0, 0))
        st.configure("Voice.TNotebook.Tab", background=v["btn"], foreground=v["fg"], padding=(12, 2),
                     font=self.f_bold, bordercolor=v["act"])
        st.map("Voice.TNotebook.Tab", background=[("selected", v["bg"])], foreground=[("selected", v["head"])])
        st.configure("Treeview", font=self.f_ui, rowheight=int(size * 2.3), background="#FBFCFE",
                     fieldbackground="#FBFCFE", bordercolor=v["act"])
        st.configure("Treeview.Heading", font=self.f_bold, background=v["btn"], foreground=v["fg"])
        st.map("Treeview", background=[("selected", "#BFD3F0")], foreground=[("selected", "#16243B")])
        st.configure("Fav.TButton", background="#FBE7A6", foreground="#5A4610", bordercolor="#EBCF73",
                     lightcolor="#FCEDBA", darkcolor="#EBCF73", padding=(8, 2))
        st.map("Fav.TButton", background=[("pressed", "#F2D77F"), ("active", "#F6DE8E")])
        st.configure("Blue.Horizontal.TProgressbar", troughcolor="#E4EEF8", background="#8CC4F0",
                     bordercolor="#C9DDF0", lightcolor="#A9D4F5", darkcolor="#79B6E8", thickness=22)
        st.configure("Pct.TLabel", background=self.PAL["Mp3"]["bg"], foreground="#2F7FC1",
                     font=(self.f_play.cget("family"), size + 8, "bold"))
        st.configure("Play.TButton", background="#9FD3AE", foreground="#16351F", font=self.f_play,
                     padding=(14, 1), bordercolor="#86C397", lightcolor="#AEDBBB", darkcolor="#86C397")
        st.map("Play.TButton", background=[("pressed", "#86C397"), ("active", "#8FCA9F")])

    def _panel(self, parent, kind, title=None):
        outer = ttk.Frame(parent, style=f"{kind}.TFrame", padding=(6, 3, 6, 3))
        outer.pack(fill="x", padx=6, pady=(3, 0))
        if title:
            ttk.Label(outer, text=title, style=f"{kind}.Head.TLabel").pack(anchor="w")
        body = ttk.Frame(outer, style=f"{kind}.TFrame")
        body.pack(fill="x")
        return body

    def _slider(self, parent, kind, col, row, text, var, frm, to, cmd, length=140):
        ttk.Label(parent, text=text, style=f"{kind}.TLabel").grid(row=row, column=col, sticky="e", padx=(8, 4), pady=1)
        ttk.Scale(parent, from_=frm, to=to, variable=var, command=lambda v: cmd(), length=length,
                  style=f"{kind}.Horizontal.TScale").grid(row=row, column=col + 1, sticky="we", pady=1)
        val = ttk.Label(parent, width=7, style=f"{kind}.Value.TLabel")
        val.grid(row=row, column=col + 2, sticky="w", padx=(4, 10))
        return val

    def _build_ui(self):
        r = self.root
        r.title(APP_TITLE)
        try:
            r.iconbitmap(os.path.join(APP_DIR, "chitalka.ico"))
        except Exception:  # noqa: BLE001
            pass
        r.geometry(self.cfg.get("geometry", "1400x900"))
        r.minsize(1000, 620)
        self._styles()

        # меню
        mb = tk.Menu(r)
        mf = tk.Menu(mb, tearoff=0)
        mf.add_command(label="Открыть книгу (можно несколько)…", accelerator="Ctrl+O", command=self.ask_open)
        mf.add_command(label="➕ Добавить книги в очередь…", command=lambda: self.ask_open(add_only=True))
        self.recent_menu = tk.Menu(mf, tearoff=0)
        mf.add_cascade(label="Недавние", menu=self.recent_menu)
        mf.add_command(label="📚 Библиотека…", accelerator="Ctrl+L", command=self.show_library)
        mf.add_command(label="🔎 Распознать текст скана (OCR)…", command=self.ocr_ask)
        mf.add_separator()
        mf.add_command(label="Сохранить в MP3 (для телефона)…", accelerator="Ctrl+S", command=self.export_dialog)
        mf.add_command(label="Открыть папку с MP3", command=self.open_audio_folder)
        mf.add_command(label="📜 Журнал сохранённых MP3", command=lambda: self.show_export_window(tab=1))
        mf.add_command(label="🗜 Уменьшить размер готовых MP3…", command=self.compress_folder_ask)
        mf.add_command(label="📥 Горячая папка…", command=self.hot_dialog)
        mf.add_command(label="📨 Отправка в Telegram…", command=self.telegram_dialog)
        mf.add_command(label="📱 Доступ с телефона…", command=self.phone_dialog)
        mf.add_separator()
        mf.add_command(label="Выход", command=self.on_close)
        mb.add_cascade(label="Файл", menu=mf)
        mk = tk.Menu(mb, tearoff=0)
        mk.add_command(label="🔍 Найти в тексте…", accelerator="Ctrl+F", command=self.search_open)
        mk.add_command(label="🔖 Добавить закладку…", accelerator="Ctrl+B", command=lambda: self.add_bookmark())
        mk.add_command(label="🔖 Закладки и заметки…", accelerator="Ctrl+Shift+B", command=self.show_bookmarks)
        mk.add_separator()
        self.notes_var = tk.StringVar(value=self.cfg.get("notes_mode", "skip"))
        mk.add_radiobutton(label="Сноски: не читать", variable=self.notes_var, value="skip", command=self._notes_changed)
        mk.add_radiobutton(label="Сноски: читать коротко сразу после абзаца (FB2, EPUB)", variable=self.notes_var,
                           value="inline", command=self._notes_changed)
        mb.add_cascade(label="Книга", menu=mk)
        mv = tk.Menu(mb, tearoff=0)
        mg = tk.Menu(mb, tearoff=0)
        mg.add_command(label="🔊 Прослушать выбранный голос", command=self.preview_voice)
        mg.add_command(label="🎙 Создать голос-копию из аудиозаписи…", command=self.clone_dialog)
        mg.add_command(label="⭐ Добавить текущий голос и настройки в избранное", command=self.add_favorite)
        self.fav_menu = tk.Menu(mg, tearoff=0)
        mg.add_cascade(label="⭐ Избранное", menu=self.fav_menu)
        mg.add_separator()
        mg.add_command(label="📖 Словарь произношения и ударений…", command=self.pron_dialog)
        mg.add_command(label="🗄 Запомненные фразы (кэш)…", command=self.cache_dialog)
        mg.add_separator()
        mg.add_command(label="🚫 Скрыть выбранный голос из списка", command=self.hide_voice)
        mg.add_command(label="↩ Вернуть все скрытые голоса", command=self.unhide_voices)
        mg.add_command(label="Открыть папку «Образцы голосов»", command=lambda: self._open_folder(os.path.join(APP_DIR, "Образцы голосов")))
        mb.add_cascade(label="Голоса", menu=mg)
        self.ui_size_var = tk.StringVar(value=self.cfg.get("ui_size", "Крупный"))
        for nm in self.UI_SIZES:
            mv.add_radiobutton(label=f"Размер интерфейса: {nm}", variable=self.ui_size_var, value=nm,
                               command=self.apply_ui_size)
        mv.add_separator()
        self.show_queue_var = tk.BooleanVar(value=self.cfg.get("show_queue", True))
        mv.add_checkbutton(label="Очередь книг слева (свернуть / развернуть)", accelerator="F4",
                           variable=self.show_queue_var, command=self.toggle_queue)
        self.mediakeys_var = tk.BooleanVar(value=self.cfg.get("media_keys", True))
        mv.add_checkbutton(label="Мультимедийные клавиши ⏯ ⏭ ⏮ управляют чтением", variable=self.mediakeys_var,
                           command=self._media_toggle)
        mb.add_cascade(label="Вид", menu=mv)
        mh = tk.Menu(mb, tearoff=0)
        mh.add_command(label="Горячие клавиши", command=self.show_help)
        mh.add_separator()
        mh.add_command(label="🔄 Проверить обновления…", command=self.check_updates)
        mh.add_command(label="🧩 Компоненты и улучшения…", command=self.components_dialog)
        self.upd_auto_var = tk.BooleanVar(value=self.cfg.get("update_auto", True))
        mh.add_checkbutton(label="Проверять обновления раз в день", variable=self.upd_auto_var,
                           command=lambda: self.cfg.set("update_auto", bool(self.upd_auto_var.get())))
        mh.add_command(label="ℹ О программе", command=self.show_about)
        mb.add_cascade(label="Справка", menu=mh)
        r.config(menu=mb)

        top = ttk.Frame(r, style="Win.TFrame")
        top.pack(fill="x")

        # ---- ЧТЕНИЕ (мятная панель)
        p = self._panel(top, "Read")
        ttk.Button(p, text="📂 Открыть", style="Read.TButton", command=self.ask_open).pack(side="left")
        ttk.Frame(p, width=8, style="Read.TFrame").pack(side="left")
        for txt, cmd in (("⏮ Глава", self.prev_chapter), ("◀ Фраза", lambda: self.step(-1))):
            ttk.Button(p, text=txt, style="Read.TButton", command=cmd).pack(side="left", padx=1)
        self.play_btn = ttk.Button(p, text="▶  Читать", width=10, style="Play.TButton", command=self.toggle_play)
        self.play_btn.pack(side="left", padx=4)
        for txt, cmd in (("⏹ Стоп", self.stop), ("Фраза ▶", lambda: self.step(1)), ("Глава ⏭", self.next_chapter)):
            ttk.Button(p, text=txt, style="Read.TButton", command=cmd).pack(side="left", padx=1)
        row2 = p  # глава — в той же строке, что и кнопки
        ttk.Button(row2, text="💾 Сохранить в MP3", style="Mp3.TButton", command=self.export_dialog).pack(side="right")
        ttk.Label(row2, text="  Глава:", style="Read.TLabel").pack(side="left")
        self.chapter_var = tk.StringVar()
        self.chapter_cb = ttk.Combobox(row2, textvariable=self.chapter_var, state="readonly", font=self.f_ui, height=25)
        self.chapter_cb.pack(side="left", padx=(4, 8), fill="x", expand=True)
        self.chapter_cb.bind("<<ComboboxSelected>>", self.on_chapter_selected)

        # ---- ГОЛОС (голубая панель): вкладки «Голоса» и «Избранное»
        vp = self._panel(top, "Voice")
        self.voice_nb = ttk.Notebook(vp, style="Voice.TNotebook")
        self.voice_nb.pack(fill="x")
        p = ttk.Frame(self.voice_nb, style="Voice.TFrame", padding=(4, 3, 4, 2))
        favp = ttk.Frame(self.voice_nb, style="Voice.TFrame", padding=(4, 3, 4, 2))
        self.voice_nb.add(p, text="🎙 Голоса")
        self.voice_nb.add(favp, text="  ⭐ Избранное  ")
        p.columnconfigure(1, weight=1)
        f1 = ttk.Frame(p, style="Voice.TFrame")
        f1.grid(row=0, column=0, columnspan=4, sticky="we")
        ttk.Label(f1, text="Пол:", style="Voice.TLabel").pack(side="left")
        self.gender_var = tk.StringVar(value=self.cfg.get("gender_filter", "all"))
        for val, txt in (("all", "Все"), ("m", "♂ Мужские"), ("f", "♀ Женские")):
            ttk.Radiobutton(f1, text=txt, value=val, variable=self.gender_var, style="Voice.TRadiobutton",
                            command=self.fill_voices).pack(side="left", padx=(8, 4))
        ttk.Label(f1, text="   Показать:", style="Voice.TLabel").pack(side="left")
        self.kind_var = tk.StringVar(value=self.cfg.get("kind_filter", "Все голоса"))
        kcb = ttk.Combobox(f1, textvariable=self.kind_var, state="readonly", width=24, font=self.f_ui,
                           values=["Все голоса", "🌐 Онлайн (живые, нужен интернет)", "💾 Офлайн (без интернета)",
                                   "🎙 Мои голоса-копии", "🎧 Студийные (XTTS)"])
        kcb.pack(side="left", padx=6)
        kcb.bind("<<ComboboxSelected>>", lambda e: self.fill_voices())
        self.all_langs_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(f1, text="все языки", variable=self.all_langs_var, style="Voice.TCheckbutton",
                        command=self.fill_voices).pack(side="left", padx=8)
        self.found_lbl = ttk.Label(f1, style="Voice.Head.TLabel")
        self.found_lbl.pack(side="left", padx=8)
        ttk.Button(f1, text="🎙 Создать голос-копию…", style="Voice.TButton", command=self.clone_dialog).pack(side="right")
        ttk.Button(f1, text="⭐ В избранное", style="Fav.TButton", command=self.add_favorite).pack(side="right", padx=6)

        ttk.Label(p, text="Голос:", style="Voice.TLabel").grid(row=1, column=0, sticky="w", pady=(3, 0))
        self.voice_var = tk.StringVar()
        self.voice_cb = ttk.Combobox(p, textvariable=self.voice_var, state="readonly", height=30, font=self.f_ui)
        self.voice_cb.grid(row=1, column=1, sticky="we", padx=6, pady=(3, 0))
        self.voice_cb.bind("<<ComboboxSelected>>", lambda e: self.settings_changed())
        ttk.Button(p, text="🔊 Прослушать", style="Voice.TButton", command=self.preview_voice).grid(
            row=1, column=2, sticky="w", pady=(3, 0))
        f2 = ttk.Frame(p, style="Voice.TFrame")
        f2.grid(row=1, column=3, sticky="e", padx=(12, 0), pady=(3, 0))
        ttk.Label(f2, text="Без интернета:", style="Voice.TLabel").pack(side="left")
        self.fb_var = tk.StringVar()
        self.fb_cb = ttk.Combobox(f2, textvariable=self.fb_var, state="readonly", width=30, height=20, font=self.f_ui)
        self.fb_cb.pack(side="left", padx=6)
        self.fb_cb.bind("<<ComboboxSelected>>", lambda e: self.settings_changed())
        # речь героев: отдельные голоса для мужчин, женщин и детей (пол — по словам автора «сказала она»)
        f3 = ttk.Frame(p, style="Voice.TFrame")
        f3.grid(row=2, column=0, columnspan=4, sticky="we", pady=(3, 0))
        ttk.Label(f3, text="Речь героев:", style="Voice.TLabel").pack(side="left")
        self.hero_vars = {}
        self.hero_cbs = {}
        for key, lbl in (("voice2", " ♂"), ("voice2f", "  ♀"), ("voice2c", "  👶")):
            ttk.Label(f3, text=lbl, style="Voice.TLabel").pack(side="left")
            v = tk.StringVar(value=self.SAME_VOICE)
            cb = ttk.Combobox(f3, textvariable=v, state="readonly", width=26, height=25, font=self.f_ui)
            cb.pack(side="left", padx=(2, 4), fill="x", expand=True)
            cb.bind("<<ComboboxSelected>>", lambda e: self.settings_changed())
            self.hero_vars[key], self.hero_cbs[key] = v, cb
        self._build_favorites(favp)

        # ---- ЗВУЧАНИЕ (лавандовая панель)
        p = self._panel(top, "Sound")
        self.speed_var = tk.DoubleVar(value=1.0)
        self.pitch_var = tk.IntVar(value=0)
        self.vol_var = tk.IntVar(value=100)
        self.pause_var = tk.DoubleVar(value=0.15)
        self.warmth_var = tk.IntVar(value=0)
        self.clarity_var = tk.IntVar(value=0)
        self.reverb_var = tk.IntVar(value=0)
        self.norm_var = tk.BooleanVar(value=True)
        f4 = ttk.Frame(p, style="Sound.TFrame")
        f4.grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(f4, text="Стиль:", style="Sound.TLabel").pack(side="left")
        self.preset_var = tk.StringVar(value=self.cfg.get("preset", "Обычный"))
        pcb = ttk.Combobox(f4, textvariable=self.preset_var, state="readonly", width=20, font=self.f_ui,
                           values=list(self.PRESETS) + ["Свой"])
        pcb.pack(side="left", padx=4)
        pcb.bind("<<ComboboxSelected>>", lambda e: self.apply_preset())
        ttk.Checkbutton(f4, text="Ровная громкость", variable=self.norm_var, style="Sound.TCheckbutton",
                        command=self.on_fx).pack(side="left", padx=6)
        ttk.Button(f4, text="Сброс", style="Sound.TButton", command=self.reset_voice).pack(side="left", padx=2)
        # две плотные строки ползунков
        self.pause_lbl = self._slider(p, "Sound", 3, 0, "Паузы", self.pause_var, 0.0, 1.0, self.on_fx)
        self.warmth_lbl = self._slider(p, "Sound", 6, 0, "Бархатность", self.warmth_var, 0, 10, self.on_fx)
        self.clarity_lbl = self._slider(p, "Sound", 9, 0, "Чёткость", self.clarity_var, 0, 10, self.on_fx)
        self.speed_lbl = self._slider(p, "Sound", 0, 1, "Скорость", self.speed_var, 0.5, 2.5, self.on_speed)
        self.pitch_lbl = self._slider(p, "Sound", 3, 1, "Тон", self.pitch_var, -10, 10, self.on_pitch)
        self.vol_lbl = self._slider(p, "Sound", 6, 1, "Громкость", self.vol_var, 0, 150, self.on_volume)
        self.reverb_lbl = self._slider(p, "Sound", 9, 1, "Объём комнаты", self.reverb_var, 0, 10, self.on_fx)
        # третья строка: мягкие «с/ш», компрессор, тихий фон, живые интонации
        self.deess_var = tk.IntVar(value=0)
        self.comp_var = tk.IntVar(value=0)
        self.bg_level_var = tk.IntVar(value=30)
        self.deess_lbl = self._slider(p, "Sound", 0, 2, "Мягкие «с/ш»", self.deess_var, 0, 10, self.on_fx)
        self.comp_lbl = self._slider(p, "Sound", 3, 2, "Компрессор", self.comp_var, 0, 10, self.on_fx)
        fbg = ttk.Frame(p, style="Sound.TFrame")
        fbg.grid(row=2, column=6, sticky="e", padx=(8, 4))
        ttk.Label(fbg, text="Фон:", style="Sound.TLabel").pack(side="left")
        self.bg_var = tk.StringVar(value=self.cfg.get("bg_kind", "нет"))
        bcb = ttk.Combobox(fbg, textvariable=self.bg_var, values=speech.BG_KINDS, state="readonly", width=10,
                           font=self.f_ui)
        bcb.pack(side="left", padx=(4, 0))
        bcb.bind("<<ComboboxSelected>>", lambda e: self.on_bg())
        ttk.Scale(p, from_=0, to=100, variable=self.bg_level_var, command=lambda v: self.on_bg(), length=140,
                  style="Sound.Horizontal.TScale").grid(row=2, column=7, sticky="we", pady=1)
        self.bg_lbl = ttk.Label(p, width=7, style="Sound.Value.TLabel")
        self.bg_lbl.grid(row=2, column=8, sticky="w", padx=(4, 10))
        self.expr_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(p, text="Живые интонации (? ! …)", variable=self.expr_var, style="Sound.TCheckbutton",
                        command=self.on_fx).grid(row=2, column=9, columnspan=3, sticky="w", padx=(8, 0))

        self.autoscroll_var = tk.BooleanVar(value=True)
        self.theme_var = tk.StringVar(value="Сепия")

        # очередь книг (слева) + текст (справа), граница перетаскивается мышью
        self.paned = tk.PanedWindow(r, orient="horizontal", sashwidth=7, sashrelief="flat",
                                    bg=self.PAL["Win"]["bg"], bd=0, opaqueresize=True)
        self.paned.pack(fill="both", expand=True, padx=6, pady=(4, 0))
        self.queue_frame = ttk.Frame(self.paned, style="Queue.TFrame", padding=(6, 4, 6, 4))
        self._build_queue(self.queue_frame)
        # свёрнутая очередь — узкая полоска слева; щелчок разворачивает обратно
        self.queue_strip = tk.Frame(self.paned, bg=self.PAL["Queue"]["btn"], cursor="hand2")
        qs = tk.Label(self.queue_strip, text="▶\n\n" + "\n".join("ОЧЕРЕДЬ"), bg=self.PAL["Queue"]["btn"],
                      fg=self.PAL["Queue"]["fg"], font=self.f_bold, cursor="hand2", justify="center")
        qs.pack(fill="both", expand=True, pady=6)
        for wdg in (self.queue_strip, qs):
            wdg.bind("<Button-1>", lambda e: self.queue_collapse(True))
            wdg.bind("<Enter>", lambda e: qs.config(bg=self.PAL["Queue"]["act"]))
            wdg.bind("<Leave>", lambda e: qs.config(bg=self.PAL["Queue"]["btn"]))
        tf = ttk.Frame(self.paned, style="Win.TFrame")
        self.paned.add(self.queue_frame, minsize=220, width=self.cfg.get("queue_width", 380), stretch="never")
        self.paned.add(tf, minsize=420, stretch="always")
        self.text = tk.Text(tf, wrap="word", padx=32, pady=12, spacing1=2, spacing3=8, undo=False,
                            relief="flat", cursor="arrow", insertwidth=0, highlightthickness=0, borderwidth=0)
        ys = ttk.Scrollbar(tf, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=ys.set)
        ys.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.bind("<Double-Button-1>", self.on_double_click)
        self.text.bind("<Key>", self._text_key)
        self.text.bind("<Button-3>", self._text_menu)
        self._search_bar = self._build_search_bar(tf)
        self.text.tag_configure("found", background="#FFE9A8")
        self.text.tag_configure("found_cur", background="#FFB347")
        self.text.tag_configure("bm", underline=True)
        self.font_px = 15

        # статус
        st = ttk.Frame(r, style="Win.TFrame", padding=(8, 1))
        st.pack(fill="x", side="bottom", before=self.paned)
        self.status_var = tk.StringVar(value="Откройте книгу: Файл → Открыть (Ctrl+O). Двойной щелчок по тексту — читать с этого места.")
        self.pos_var = tk.StringVar()
        ttk.Label(st, textvariable=self.pos_var, style="Win.TLabel").pack(side="right")
        self.progress = ttk.Progressbar(st, length=180, maximum=1000, style="Blue.Horizontal.TProgressbar")
        self.progress.pack(side="right", padx=8)
        tcb = ttk.Combobox(st, textvariable=self.theme_var, values=list(THEMES), state="readonly", width=8, font=self.f_ui)
        tcb.pack(side="right", padx=(0, 10))
        tcb.bind("<<ComboboxSelected>>", lambda e: self.apply_theme())
        ttk.Label(st, text="Тема текста:", style="Win.TLabel").pack(side="right", padx=(8, 4))
        ttk.Button(st, text="A+", width=3, style="Win.TButton", command=lambda: self.font_size(1)).pack(side="right")
        ttk.Button(st, text="A−", width=3, style="Win.TButton", command=lambda: self.font_size(-1)).pack(side="right")
        ttk.Checkbutton(st, text="Автопрокрутка", variable=self.autoscroll_var, style="Win.TCheckbutton").pack(side="right", padx=8)
        self.exp_status_var = tk.StringVar(value="")
        el = ttk.Label(st, textvariable=self.exp_status_var, style="Mp3.Value.TLabel", cursor="hand2")
        el.pack(side="right", padx=8)
        el.bind("<Button-1>", lambda e: self.show_export_window())
        ttk.Label(st, textvariable=self.status_var, style="Win.TLabel").pack(side="left", fill="x", expand=True)

        # клавиши
        r.bind_all("<Control-o>", lambda e: self.ask_open())
        r.bind_all("<F4>", lambda e: self.queue_collapse())
        r.bind_all("<Control-f>", lambda e: self.search_open())
        r.bind_all("<Control-b>", lambda e: self.add_bookmark())
        r.bind_all("<Control-B>", lambda e: self.show_bookmarks())
        r.bind_all("<Control-l>", lambda e: self.show_library())
        r.bind_all("<Control-O>", lambda e: self.ask_open())
        r.bind_all("<Control-s>", lambda e: self.export_dialog())
        r.bind_all("<Control-S>", lambda e: self.export_dialog())
        r.bind("<space>", lambda e: self._key_play(e))
        r.bind("<Control-Right>", lambda e: self.step(1))
        r.bind("<Control-Left>", lambda e: self.step(-1))
        r.bind("<Control-Down>", lambda e: self.next_chapter())
        r.bind("<Control-Up>", lambda e: self.prev_chapter())
        r.bind("<Control-plus>", lambda e: self.bump_speed(0.1))
        r.bind("<Control-equal>", lambda e: self.bump_speed(0.1))
        r.bind("<Control-minus>", lambda e: self.bump_speed(-0.1))
        r.bind("<Escape>", lambda e: self.stop())
        r.bind("<F1>", lambda e: self.show_help())
        self._dnd_register(self.text, "open")
        self._dnd_register(self.queue_tv, "queue")
        self._dnd_register(self.queue_frame, "queue")

    def apply_ui_size(self):
        size = self.UI_SIZES.get(self.ui_size_var.get(), 12)
        self.cfg.set("ui_size", self.ui_size_var.get())
        self.f_ui.configure(size=size)
        self.f_bold.configure(size=size)
        self.f_head.configure(size=max(8, size - 2))
        self.f_play.configure(size=size + 3)

    def _text_key(self, e):
        # текст только для чтения, но прокрутка клавишами и копирование работают
        if e.keysym in ("Up", "Down", "Prior", "Next", "Home", "End", "Left", "Right") or (e.state & 4 and e.keysym.lower() in ("c", "a")):
            return None
        if e.keysym == "space":
            self.toggle_play()
        return "break"

    def _key_play(self, e):
        if isinstance(e.widget, (ttk.Combobox, ttk.Entry, tk.Entry)):
            return None
        self.toggle_play()
        return "break"

    # ------------------------------------------------------------------ настройки
    def _load_settings(self):
        c = self.cfg
        self.all_langs_var.set(c.get("all_langs", False))
        self.fill_voices(keep=False)
        self.speed_var.set(c.get("speed", 1.0))
        self.pitch_var.set(c.get("pitch", 0))
        self.vol_var.set(c.get("volume", 100))
        self.pause_var.set(c.get("pause", 0.15))
        self.warmth_var.set(c.get("warmth", 0))
        self.clarity_var.set(c.get("clarity", 0))
        self.reverb_var.set(c.get("reverb", 0))
        self.norm_var.set(c.get("normalize", True))
        self.deess_var.set(c.get("deess", 0))
        self.comp_var.set(c.get("compress", 0))
        self.expr_var.set(c.get("expressive", True))
        self.bg_level_var.set(c.get("bg_level", 30))
        self.autoscroll_var.set(c.get("autoscroll", True))
        self.theme_var.set(c.get("theme", "Сепия"))
        self.font_px = c.get("font", 15)
        self.apply_theme()
        self.font_size(0)
        self.on_speed(apply=False)
        self.on_pitch(apply=False)
        self.on_fx(apply=False, keep_preset=True)
        self.on_bg(save=False)
        self.on_volume()
        self.reader.set_settings(self.voice_settings())
        self._preload(self.voice_settings())
        self._rebuild_recent()
        self.refresh_favorites()
        self.refresh_queue()
        if not self.show_queue_var.get():
            self.toggle_queue()
        self.root.after(1500, self.check_unfinished_exports)

    def _kind_ok(self, v) -> bool:
        k = self.kind_var.get()
        if k.startswith("🌐"):
            return v.online
        if k.startswith("💾"):
            return not v.online
        if k.startswith("🎙"):
            return v.group == -2
        if k.startswith("🎧"):
            return v.group == -1
        return True

    def fill_voices(self, keep=True):
        """Заполняет списки голосов с учётом фильтров «Пол» и «Показать»."""
        cur_v = self._voice_id(self.voice_var.get()) if keep and self.voice_var.get() else self.cfg.get("voice", speech.DEFAULT_VOICE)
        cur_f = self._voice_id(self.fb_var.get()) if keep and self.fb_var.get() else self.cfg.get("fallback", speech.DEFAULT_FALLBACK)
        self.cfg.set("all_langs", bool(self.all_langs_var.get()))
        self.cfg.set("gender_filter", self.gender_var.get())
        self.cfg.set("kind_filter", self.kind_var.get())
        self.catalog = speech.catalog(all_langs=bool(self.all_langs_var.get()))
        g = self.gender_var.get()
        hidden = set(self.cfg.get("hidden_voices", []))
        self.catalog = [v for v in self.catalog if v.id not in hidden]
        shown = [v for v in self.catalog if (g == "all" or v.gender == g) and self._kind_ok(v)]
        # часто используемые (до 6) — наверх списка с пометкой 🕘, голоса из избранного — ⭐
        usage = self._usage()
        fav_ids = {f["voice"] for f in self._favs()}
        top = [v.id for v in sorted(self.catalog, key=lambda v: -usage.get(v.id, 0)) if usage.get(v.id, 0) >= 3][:6]
        self.titles = {}
        for v in self.catalog:
            mark = "🕘 " if v.id in top else ("⭐ " if v.id in fav_ids else "")
            self.titles[v.id] = mark + speech.voice_title(v)
        rank = {vid: i for i, vid in enumerate(top)}
        shown.sort(key=lambda v: (0, rank[v.id]) if v.id in rank else ((1, 0) if v.id in fav_ids else (2, 0)))
        self.voice_cb.configure(values=[self.titles[v.id] for v in shown] or ["(нет голосов по этому фильтру)"])
        self.found_lbl.config(text=f"найдено: {len(shown)}")
        offline = [v for v in self.catalog if not v.online]
        off_g = offline  # запасной голос не зависит от фильтра «Пол»
        self.fb_cb.configure(values=[self.titles[v.id] for v in off_g] or ["(офлайн-голоса не установлены)"])
        hidden = set(self.cfg.get("hidden_voices", []))
        hero = [self.SAME_VOICE] + [self.titles[v.id] for v in self.catalog
                                    if v.id not in hidden and (v.lang == "ru" or "по-русски" in v.label)]
        for key, cb in getattr(self, "hero_cbs", {}).items():
            cb.configure(values=hero)
            vid = self.cfg.get(key, "")
            if self.hero_vars[key].get() == self.SAME_VOICE and vid in self.titles:
                self.hero_vars[key].set(self.titles[vid])
        changed = False
        if cur_v not in {v.id for v in shown}:
            if shown:
                cur_v = shown[0].id
                changed = True
        self.voice_var.set(self.titles.get(cur_v, "(нет голосов по этому фильтру)"))
        if cur_f not in {v.id for v in off_g}:
            ids = {v.id for v in off_g}
            ru = [v for v in off_g if v.lang == "ru"] or off_g
            cur_f = speech.DEFAULT_FALLBACK if speech.DEFAULT_FALLBACK in ids else (ru[0].id if ru else "")
            changed = True
        self.fb_var.set(self.titles.get(cur_f, "(офлайн-голоса не установлены)"))
        if changed and keep:
            self.settings_changed()

    def hide_voice(self):
        vid = self._voice_id(self.voice_var.get())
        if not vid:
            return
        if not messagebox.askyesno(APP_TITLE, f"Скрыть голос «{self.voice_var.get()[2:]}» из списка?\n"
                                              "Вернуть можно: Голоса → Вернуть все скрытые голоса."):
            return
        self.cfg.set("hidden_voices", sorted(set(self.cfg.get("hidden_voices", [])) | {vid}))
        self.voice_var.set("")
        self.fill_voices(keep=False)
        self._apply_settings_now()
        self.cfg.save()

    def unhide_voices(self):
        n = len(self.cfg.get("hidden_voices", []))
        self.cfg.set("hidden_voices", [])
        self.fill_voices()
        self.cfg.save()
        self.status_var.set(f"Возвращено голосов: {n}")

    # ------------------------------------------------------------------ режимы, избранное, частые голоса
    MODE_KEYS = ("voice", "fallback", "speed", "pitch", "warmth", "clarity", "reverb", "normalize", "pause",
                 "voice2", "voice2f", "voice2c", "expressive", "deess", "compress")
    SAME_VOICE = "(как у автора)"

    def mode_dict(self) -> dict:
        vs = self.voice_settings()
        d = {k: getattr(vs, k) for k in self.MODE_KEYS}
        d["preset"] = self.preset_var.get()
        return d

    def apply_mode(self, d: dict, quiet=False):
        """Включает сохранённый режим: голос + все настройки звучания."""
        vid = d.get("voice", "")
        if vid not in {v.id for v in self.catalog} or self.titles.get(vid) not in self.voice_cb.cget("values"):
            # голос скрыт текущим фильтром — показываем все
            self.gender_var.set("all")
            self.kind_var.set("Все голоса")
            if vid.startswith("edge:") and not vid[5:7] in ("ru", "en", "kk", "uk", "fr", "de", "it", "ko", "pt"):
                self.all_langs_var.set(True)
            self.fill_voices(keep=False)
        if vid in self.titles:
            self.voice_var.set(self.titles[vid])
        elif not quiet:
            messagebox.showinfo(APP_TITLE, "Голос из этого режима сейчас недоступен (возможно, скрыт или удалён).")
        if d.get("fallback") in self.titles:
            self.fb_var.set(self.titles[d["fallback"]])
        for key, var in self.hero_vars.items():
            var.set(self.titles.get(d.get(key, ""), self.SAME_VOICE) if d.get(key) else self.SAME_VOICE)
        self.speed_var.set(d.get("speed", 1.0))
        self.pitch_var.set(d.get("pitch", 0))
        self.warmth_var.set(d.get("warmth", 0))
        self.clarity_var.set(d.get("clarity", 0))
        self.reverb_var.set(d.get("reverb", 0))
        self.norm_var.set(d.get("normalize", True))
        self.pause_var.set(d.get("pause", 0.15))
        self.deess_var.set(d.get("deess", 0))
        self.comp_var.set(d.get("compress", 0))
        self.expr_var.set(d.get("expressive", True))
        self.on_speed(apply=False)
        self.on_pitch(apply=False)
        self.on_fx(apply=False)
        if d.get("preset"):
            self.preset_var.set(d["preset"])
        self._apply_settings_now()

    def _save_soon(self):
        if getattr(self, "_save_job", None):
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(1500, self.cfg.save)

    def _usage(self) -> dict:
        return self.cfg.data.setdefault("voice_usage", {})

    def _bump_usage(self, vid: str, n: float = 1.0):
        if vid:
            u = self._usage()
            u[vid] = round(u.get(vid, 0) + n, 1)

    def mode_summary(self, d: dict) -> str:
        parts = [f"×{d.get('speed', 1):.2f}"]
        if d.get("pitch"):
            parts.append(f"тон {d['pitch']:+d}")
        if d.get("preset") and d["preset"] != "Обычный":
            parts.append(d["preset"])
        else:
            fx = [f"{n} {d.get(k)}" for k, n in (("warmth", "барх."), ("clarity", "чётк."), ("reverb", "объём")) if d.get(k)]
            parts += fx
        return ", ".join(parts)

    def _voice_short(self, vid: str) -> str:
        t = self.titles.get(vid) or next((speech.voice_title(v) for v in speech.catalog(True) if v.id == vid), vid)
        return t.lstrip("⭐🕘 ").replace("🌐 ", "").replace("💾 ", "").split(" — ")[0]

    def _build_favorites(self, parent):
        parent.columnconfigure(0, weight=1)
        cols = ("name", "voice", "mode")
        tv = ttk.Treeview(parent, columns=cols, show="headings", height=2, selectmode="browse")
        for c, txt, w in (("name", "Название", 260), ("voice", "Голос", 260), ("mode", "Настройки", 320)):
            tv.heading(c, text=txt, anchor="w")
            tv.column(c, width=w, anchor="w", stretch=True)
        tv.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(parent, orient="vertical", command=tv.yview)
        sb.grid(row=0, column=1, sticky="ns")
        tv.configure(yscrollcommand=sb.set)
        tv.bind("<Double-1>", lambda e: self.use_favorite())
        tv.bind("<Return>", lambda e: self.use_favorite())
        self.fav_tv = tv
        b = ttk.Frame(parent, style="Voice.TFrame")
        b.grid(row=0, column=2, sticky="n", padx=(8, 0))
        r1 = ttk.Frame(b, style="Voice.TFrame")
        r1.pack(fill="x")
        ttk.Button(r1, text="✔ Включить", style="Fav.TButton", command=self.use_favorite).pack(side="left", fill="x", expand=True)
        ttk.Button(r1, text="🔊 Прослушать", style="Voice.TButton",
                   command=lambda: self.use_favorite(preview=True)).pack(side="left", padx=(3, 0), fill="x", expand=True)
        r = ttk.Frame(b, style="Voice.TFrame")
        r.pack(fill="x", pady=(3, 0))
        for txt, cmd in (("⬆", lambda: self.move_favorite(-1)), ("⬇", lambda: self.move_favorite(1)),
                         ("✏", self.rename_favorite), ("🗑", self.delete_favorite)):
            ttk.Button(r, text=txt, width=3, style="Voice.TButton", command=cmd).pack(side="left", fill="x",
                                                                                     expand=True, padx=(0, 3))
        self.voice_nb.bind("<<NotebookTabChanged>>", lambda e: self.voice_nb.index("current") == 1 and self.status_var.set(
            "⭐ Избранное: настройте голос и звучание, затем «⭐ В избранное». Двойной щелчок по строке — включить."))

    def _favs(self) -> list:
        return self.cfg.data.setdefault("favorites", [])

    def refresh_favorites(self, select: int | None = None):
        tv = self.fav_tv
        tv.delete(*tv.get_children())
        for i, f in enumerate(self._favs()):
            tv.insert("", "end", iid=str(i), values=(f["name"], self._voice_short(f["voice"]), self.mode_summary(f)))
        if select is not None and 0 <= select < len(self._favs()):
            tv.selection_set(str(select))
            tv.see(str(select))
        self.voice_nb.tab(1, text=f"  ⭐ Избранное ({len(self._favs())})  ")
        self._rebuild_fav_menu()

    def _rebuild_fav_menu(self):
        m = getattr(self, "fav_menu", None)
        if m is None:
            return
        m.delete(0, "end")
        if not self._favs():
            m.add_command(label="(пока пусто)", state="disabled")
        for i, f in enumerate(self._favs()):
            m.add_command(label=f"⭐ {f['name']}", command=lambda i=i: self.use_favorite(index=i))

    def add_favorite(self):
        from tkinter import simpledialog
        d = self.mode_dict()
        default = f"{self._voice_short(d['voice'])} · {self.mode_summary(d)}"
        name = simpledialog.askstring(APP_TITLE, "Название для избранного (голос + все настройки звучания):",
                                      initialvalue=default, parent=self.root)
        if not name:
            return
        d["name"] = name.strip()
        favs = self._favs()
        same = next((i for i, f in enumerate(favs) if f["name"] == d["name"]), None)
        if same is not None:
            favs[same] = d
        else:
            favs.append(d)
        self.cfg.save()
        self.refresh_favorites(select=same if same is not None else len(favs) - 1)
        self.voice_nb.select(1)
        self.status_var.set(f"⭐ Добавлено в избранное: {d['name']}")

    def _fav_index(self):
        sel = self.fav_tv.selection()
        return int(sel[0]) if sel else None

    def use_favorite(self, preview=False, index=None):
        i = self._fav_index() if index is None else index
        if i is None:
            if self._favs():
                i = 0
            else:
                return
        f = self._favs()[i]
        if preview:
            keys = {k: f[k] for k in self.MODE_KEYS if k in f}
            self.preview_voice(VoiceSettings(**keys))
            return
        self.apply_mode(f)
        self._bump_usage(f["voice"], 3)
        self.status_var.set(f"⭐ Включено: {f['name']}")

    def move_favorite(self, d):
        i = self._fav_index()
        favs = self._favs()
        if i is None or not 0 <= i + d < len(favs):
            return
        favs[i], favs[i + d] = favs[i + d], favs[i]
        self.cfg.save()
        self.refresh_favorites(select=i + d)

    def rename_favorite(self):
        from tkinter import simpledialog
        i = self._fav_index()
        if i is None:
            return
        f = self._favs()[i]
        name = simpledialog.askstring(APP_TITLE, "Новое название:", initialvalue=f["name"], parent=self.root)
        if name:
            f["name"] = name.strip()
            self.cfg.save()
            self.refresh_favorites(select=i)

    def delete_favorite(self):
        i = self._fav_index()
        if i is None:
            return
        f = self._favs()[i]
        if messagebox.askyesno(APP_TITLE, f"Удалить из избранного «{f['name']}»?"):
            del self._favs()[i]
            self.cfg.save()
            self.refresh_favorites()

    def _voice_id(self, title: str) -> str:
        for vid, t in getattr(self, "titles", {}).items():
            if t == title:
                return vid
        return ""

    def voice_settings(self) -> VoiceSettings:
        return VoiceSettings(voice=self._voice_id(self.voice_var.get()) or speech.DEFAULT_VOICE,
                             fallback=self._voice_id(self.fb_var.get()),
                             speed=round(float(self.speed_var.get()), 2), pitch=int(round(float(self.pitch_var.get()))),
                             warmth=int(self.warmth_var.get()), clarity=int(self.clarity_var.get()),
                             reverb=int(self.reverb_var.get()), normalize=bool(self.norm_var.get()),
                             pause=round(float(self.pause_var.get()), 2),
                             expressive=bool(self.expr_var.get()) if hasattr(self, "expr_var") else True,
                             deess=int(self.deess_var.get()) if hasattr(self, "deess_var") else 0,
                             compress=int(self.comp_var.get()) if hasattr(self, "comp_var") else 0,
                             **{k: self._voice_id(v.get()) for k, v in getattr(self, "hero_vars", {}).items()})

    def on_fx(self, apply=True, keep_preset=False):
        for var, lbl in ((self.warmth_var, self.warmth_lbl), (self.clarity_var, self.clarity_lbl),
                         (self.reverb_var, self.reverb_lbl), (self.deess_var, self.deess_lbl),
                         (self.comp_var, self.comp_lbl)):
            v = int(round(float(var.get())))
            var.set(v)
            lbl.config(text=str(v))
        pv = round(float(self.pause_var.get()) * 20) / 20
        self.pause_var.set(pv)
        self.pause_lbl.config(text=f"{pv:.2f}с")
        if not keep_preset:
            cur = dict(warmth=self.warmth_var.get(), clarity=self.clarity_var.get(),
                       reverb=self.reverb_var.get(), pause=pv)
            self.preset_var.set(next((n for n, p in self.PRESETS.items() if p == cur), "Свой"))
        if apply:
            self.settings_changed()

    def apply_preset(self):
        p = self.PRESETS.get(self.preset_var.get())
        if not p:
            return
        self.warmth_var.set(p["warmth"])
        self.clarity_var.set(p["clarity"])
        self.reverb_var.set(p["reverb"])
        self.pause_var.set(p["pause"])
        self.on_fx(keep_preset=True)

    def preview_voice(self, vs=None, text=None):
        vs = vs or self.voice_settings()
        info = next((v for v in self.catalog if v.id == vs.voice), None)
        lang = info.lang if info else "ru"
        sample = {
            "ru": "Здравствуйте! Так звучит этот голос. Он будет читать вам книги — спокойно и с выражением, как живой человек.",
            "kk": "Сәлеметсіз бе! Бұл дауыс сізге кітап оқып береді.",
            "uk": "Добрий день! Так звучить цей голос.",
        }.get(lang, "Hello! This is how this voice sounds. It will read your books aloud.")
        if text:
            sample = text
        if self.reader.running:
            self.stop()
        if not self.out.ok and not self.out.restart():
            self.show_error(self.out.error, "пробе голоса")
            return
        self.status_var.set("Готовлю пробу голоса…")

        def work():
            try:
                a = self.speaker.synth(sample, vs)
                self.out.paused = False
                self.out.play(a)
                self.q.put(("status", "Проба: " + self._voice_short(vs.voice)))
            except Exception as e:  # noqa: BLE001
                speech.log_error(e)
                self.q.put(("status", "Голос недоступен"))
                self.q.put(("error_ctx", (e, f"пробе голоса «{self._voice_short(vs.voice)}»",
                                          {"retry": lambda: self.preview_voice(vs), "actions": ["other_voice"]})))

        threading.Thread(target=work, daemon=True).start()

    def _refresh_catalog_bg(self):
        """Раз в месяц обновляет полный список голосов Edge (если есть интернет)."""
        try:
            age = time.time() - os.path.getmtime(speech.EDGE_CATALOG)
        except OSError:
            age = 1e9
        if age < 30 * 86400:
            return

        def work():
            if speech.refresh_edge_catalog():
                self.q.put(("catalog", None))

        threading.Thread(target=work, daemon=True).start()

    def settings_changed(self):
        # применяем с задержкой, пока двигают ползунок
        if self._settings_job:
            self.root.after_cancel(self._settings_job)
        self._settings_job = self.root.after(350, self._apply_settings)

    def _apply_settings(self):
        self._settings_job = None
        vs = self.voice_settings()
        self.reader.set_settings(vs)
        c = self.cfg
        for k in self.MODE_KEYS:
            c.set(k, getattr(vs, k))
        if self.book and self.chunks and vs.dialog != getattr(self, "_chunks_dialog", False):
            self._rechunk(vs.dialog)  # включили/выключили голоса героев — речь и слова автора делим заново
        c.set("preset", self.preset_var.get())
        if self.book:  # у каждой книги — свой запомненный голос и настройки
            c.data.setdefault("book_modes", {})[os.path.normcase(self.book.path)] = self.mode_dict()
        self._bump_usage(vs.voice, 0.5)
        self._save_soon()
        self._preload(vs)

    def _preload(self, vs):
        """Голоса XTTS: заранее загружаем модель в видеокарту, чтобы чтение началось сразу."""
        if not (vs.voice.startswith("xtts:") or vs.fallback.startswith("xtts:")):
            return
        try:
            import clone
            if clone.Xtts._model is not None or getattr(self, "_xtts_loading", False):
                return
        except Exception:  # noqa: BLE001
            return
        self._xtts_loading = True

        def work():
            try:
                clone.Xtts.load(lambda m: self.q.put(("status", m)))
            except Exception as e:  # noqa: BLE001
                speech.log_error(e)
                self.q.put(("status", "Не удалось загрузить голоса XTTS: " + speech.err_text(e)))
            finally:
                self._xtts_loading = False

        threading.Thread(target=work, daemon=True).start()

    def on_speed(self, apply=True):
        v = round(float(self.speed_var.get()) * 20) / 20
        self.speed_var.set(v)
        self.speed_lbl.config(text=f"×{v:.2f}")
        if apply:
            self.settings_changed()

    def on_pitch(self, apply=True):
        v = int(round(float(self.pitch_var.get())))
        self.pitch_var.set(v)
        self.pitch_lbl.config(text=f"{v:+d}")
        if apply:
            self.settings_changed()

    def on_bg(self, save=True):
        """Тихий фон (дождь, камин, шум) под голос; пока звучит голос — фон приглушается."""
        lv = int(round(float(self.bg_level_var.get())))
        self.bg_level_var.set(lv)
        kind = self.bg_var.get()
        self.bg_lbl.config(text=f"{lv}%" if kind != "нет" else "—")
        try:
            self.out.bg = speech.make_bg(kind)
        except Exception as e:  # noqa: BLE001
            speech.log_error(e)
            self.out.bg = None
        self.out.bg_level = lv / 100.0 * 0.6
        self.cfg.set("bg_kind", kind)
        self.cfg.set("bg_level", lv)
        if save:
            self._save_soon()

    def on_volume(self):
        v = int(round(float(self.vol_var.get())))
        self.vol_lbl.config(text=f"{v}%")
        self.out.volume = v / 100.0
        self.cfg.set("volume", v)
        if hasattr(self, "fav_tv"):
            self._save_soon()

    def bump_speed(self, d):
        self.speed_var.set(min(2.5, max(0.5, float(self.speed_var.get()) + d)))
        self.on_speed()

    def reset_voice(self):
        self.speed_var.set(1.0)
        self.pitch_var.set(0)
        self.vol_var.set(100)
        self.norm_var.set(True)
        self.preset_var.set("Обычный")
        self.on_speed()
        self.on_pitch()
        self.on_volume()
        self.apply_preset()

    def font_size(self, d):
        self.font_px = max(10, min(36, self.font_px + d))
        self.text.configure(font=("Georgia", self.font_px))
        self.cfg.set("font", self.font_px)

    def apply_theme(self):
        t = THEMES.get(self.theme_var.get(), THEMES["Сепия"])
        self.text.configure(bg=t["bg"], fg=t["fg"], selectbackground=t["sel"], insertbackground=t["fg"])
        self.text.tag_configure("cur", background=t["hl"], foreground=t["hlfg"])
        self.cfg.set("theme", self.theme_var.get())

    # ------------------------------------------------------------------ книга
    def ask_open(self, add_only=False):
        start = os.path.dirname(self.book.path) if self.book else (library_root() or os.path.expanduser("~"))
        exts = " ".join("*" + e for e in formats.SUPPORTED)
        paths = filedialog.askopenfilenames(
            title="Добавить книги в очередь" if add_only else "Открыть книгу (можно выбрать несколько — Ctrl или Shift)",
            initialdir=start, filetypes=[("Книги", exts + " *.zip"), ("Все файлы", "*.*")])
        paths = [p for p in (paths or []) if p]
        if not paths:
            return
        added = self.add_to_queue(paths)
        if add_only:
            self.status_var.set(f"В очередь добавлено книг: {added}")
        else:
            self.open_book(paths[0])
            if len(paths) > 1:
                self.status_var.set(f"Открыта первая книга, в очередь добавлено: {added}")

    # ------------------------------------------------------------------ очередь книг
    MAX_QUEUE = 100

    def _build_queue(self, f):
        K = "Queue"
        head = ttk.Frame(f, style=f"{K}.TFrame")
        head.pack(fill="x")
        self.queue_title = ttk.Label(head, text="ОЧЕРЕДЬ КНИГ", style=f"{K}.Head.TLabel")
        self.queue_title.pack(side="left")
        ttk.Button(head, text="◀", width=3, style=f"{K}.TButton",
                   command=lambda: self.queue_collapse(False)).pack(side="right")
        btns = ttk.Frame(f, style=f"{K}.TFrame")
        btns.pack(fill="x", pady=(4, 2))
        ttk.Button(btns, text="➕ Добавить", style=f"{K}.TButton",
                   command=lambda: self.ask_open(add_only=True)).pack(side="left", fill="x", expand=True)
        ttk.Button(btns, text="💾 В MP3", style="Mp3.TButton", command=self.queue_to_mp3).pack(
            side="left", fill="x", expand=True, padx=(4, 0))
        btns2 = ttk.Frame(f, style=f"{K}.TFrame")
        btns2.pack(fill="x", pady=(0, 6))
        for txt, cmd in (("⬆", lambda: self.queue_move(-1)), ("⬇", lambda: self.queue_move(1)),
                         ("✖", self.queue_remove), ("🗑", self.queue_clear)):
            ttk.Button(btns2, text=txt, width=3, style=f"{K}.TButton", command=cmd).pack(
                side="left", fill="x", expand=True, padx=(0, 4))
        lf = ttk.Frame(f, style=f"{K}.TFrame")
        lf.pack(fill="both", expand=True)
        tv = ttk.Treeview(lf, columns=("n", "book", "pct"), show="headings", selectmode="extended")
        tv.heading("n", text="№", anchor="center")
        tv.heading("book", text="Книга", anchor="w")
        tv.heading("pct", text="%", anchor="e")
        tv.column("n", width=38, minwidth=30, stretch=False, anchor="center")
        tv.column("book", width=220, minwidth=120, stretch=True, anchor="w")
        tv.column("pct", width=56, minwidth=44, stretch=False, anchor="e")
        sb = ttk.Scrollbar(lf, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tv.pack(side="left", fill="both", expand=True)
        tv.tag_configure("cur", background="#CBE9D3", font=self.f_bold)
        tv.tag_configure("missing", foreground="#9A9A9A")
        tv.tag_configure("done", foreground="#5E8F6B")
        tv.bind("<Double-1>", lambda e: self.queue_open())
        tv.bind("<Return>", lambda e: self.queue_open())
        tv.bind("<Delete>", lambda e: self.queue_remove())
        self.queue_tv = tv
        tv.bind("<Motion>", self._queue_tip_motion)
        tv.bind("<Leave>", lambda e: self._tip_hide())
        self.autonext_var = tk.BooleanVar(value=self.cfg.get("autonext", True))
        ttk.Checkbutton(f, text="Дочитав, переходить к следующей", variable=self.autonext_var,
                        style=f"{K}.TCheckbutton",
                        command=lambda: (self.cfg.set("autonext", bool(self.autonext_var.get())), self._save_soon())
                        ).pack(anchor="w", pady=(6, 0))
        ttk.Label(f, text="Двойной щелчок — открыть книгу", style=f"{K}.TLabel").pack(anchor="w")

    # всплывающая подсказка с полным названием книги
    def _queue_tip_motion(self, e):
        row = self.queue_tv.identify_row(e.y)
        if row == getattr(self, "_tip_row", None):
            if getattr(self, "_tip_win", None):
                self._tip_win.geometry(f"+{e.x_root + 16}+{e.y_root + 14}")
            return
        self._tip_hide()
        self._tip_row = row
        if not row:
            return
        self._tip_job = self.root.after(350, lambda: self._tip_show(row, e.x_root, e.y_root))

    def _tip_show(self, row, x, y):
        try:
            p = self._queue()[int(row)]
        except (IndexError, ValueError):
            return
        info = self.cfg.data.get("book_info", {}).get(os.path.normcase(p), {})
        lines = [self._book_label(p)]
        pct = self._book_pct(p)
        if pct:
            lines.append(f"Прочитано: {pct}" + (f" · фраз: {info['total']}" if info.get("total") else ""))
        lines.append(p if os.path.isfile(p) else p + "  (файл не найден)")
        if info.get("mp3_dir"):
            lines.append(f"MP3: {info['mp3_dir']}")
        w = tk.Toplevel(self.root)
        w.wm_overrideredirect(True)
        w.attributes("-topmost", True)
        tk.Label(w, text="\n".join(lines), justify="left", bg="#FFF8DC", fg="#2B2B2B", relief="solid", bd=1,
                 font=self.f_ui, padx=8, pady=5, wraplength=700).pack()
        w.geometry(f"+{x + 16}+{y + 14}")
        self._tip_win = w

    def _tip_hide(self):
        if getattr(self, "_tip_job", None):
            self.root.after_cancel(self._tip_job)
            self._tip_job = None
        if getattr(self, "_tip_win", None):
            try:
                self._tip_win.destroy()
            except tk.TclError:
                pass
            self._tip_win = None
        self._tip_row = None

    def _queue(self) -> list:
        return self.cfg.data.setdefault("queue", [])

    def _book_label(self, path: str) -> str:
        info = self.cfg.data.get("book_info", {}).get(os.path.normcase(path))
        if info:
            return info["title"] + (f" — {info['author']}" if info.get("author") else "")
        stem = book_stem(path)
        parts = stem.split("_")
        return f"{parts[0]} — {parts[1]}" if len(parts) >= 3 else stem

    def _book_pct(self, path: str) -> str:
        key = os.path.normcase(path)
        info = self.cfg.data.get("book_info", {}).get(key)
        if not info or not info.get("total"):
            return ""
        pos = self.cfg.data["positions"].get(key, 0)
        if info.get("done") and pos == 0:
            return "✓"
        return f"{100 * (pos + 1) // info['total']}%" if pos else "0%"

    def refresh_queue(self, select=None):
        tv = self.queue_tv
        tv.delete(*tv.get_children())
        cur = os.path.normcase(self.book.path) if self.book else ""
        for i, p in enumerate(self._queue()):
            tags = []
            if os.path.normcase(p) == cur:
                tags.append("cur")
            elif not os.path.isfile(p):
                tags.append("missing")
            elif self._book_pct(p) == "✓":
                tags.append("done")
            mark = "▶ " if "cur" in tags else ""
            tv.insert("", "end", iid=str(i), values=(i + 1, mark + self._book_label(p), self._book_pct(p)),
                      tags=tags)
        if select is not None and str(select) in tv.get_children():
            tv.selection_set(str(select))
            tv.see(str(select))
        elif cur:
            idx = self._queue_index()
            if idx is not None:
                tv.see(str(idx))
        self.queue_title.config(text=f"ОЧЕРЕДЬ КНИГ ({len(self._queue())}/{self.MAX_QUEUE})")

    def _queue_index(self):
        if not self.book:
            return None
        cur = os.path.normcase(self.book.path)
        return next((i for i, p in enumerate(self._queue()) if os.path.normcase(p) == cur), None)

    def add_to_queue(self, paths, front=False) -> int:
        q = self._queue()
        have = {os.path.normcase(p) for p in q}
        new = []
        for p in paths:
            p = os.path.abspath(p)
            if os.path.normcase(p) not in have:
                have.add(os.path.normcase(p))
                new.append(p)
        room = self.MAX_QUEUE - len(q)
        if len(new) > room:
            messagebox.showinfo(APP_TITLE, f"В очереди может быть не больше {self.MAX_QUEUE} книг.\n"
                                           f"Добавлено {max(0, room)} из {len(new)}.")
            new = new[:max(0, room)]
        if front:
            q[:0] = new
        else:
            q.extend(new)
        self.cfg.save()
        self.refresh_queue()
        return len(new)

    def queue_open(self):
        sel = self.queue_tv.selection()
        if not sel:
            return
        p = self._queue()[int(sel[0])]
        if not os.path.isfile(p):
            self.show_error(formats.BookError(f"Файл книги не найден: {p}"), "открытии книги из очереди",
                            {"path": p})
            return
        if self.book and os.path.normcase(p) == os.path.normcase(self.book.path):
            return
        self.open_book(p)

    def queue_move(self, d):
        sel = sorted(int(i) for i in self.queue_tv.selection())
        q = self._queue()
        if not sel or sel[0] + d < 0 or sel[-1] + d >= len(q):
            return
        order = sel if d < 0 else list(reversed(sel))
        for i in order:
            q[i], q[i + d] = q[i + d], q[i]
        self.cfg.save()
        self.refresh_queue()
        self.queue_tv.selection_set([str(i + d) for i in sel])
        self.queue_tv.see(str(sel[0] + d))

    def queue_remove(self):
        sel = sorted((int(i) for i in self.queue_tv.selection()), reverse=True)
        if not sel:
            return
        q = self._queue()
        for i in sel:
            del q[i]
        self.cfg.save()
        self.refresh_queue(select=min(sel[-1], len(q) - 1) if q else None)

    def queue_to_mp3(self):
        """Сохранить в MP3 выделенные в очереди книги (или все, если ничего не выделено)."""
        sel = [int(i) for i in self.queue_tv.selection()]
        q = self._queue()
        paths = [q[i] for i in sorted(sel)] if sel else list(q)
        paths = [p for p in paths if os.path.isfile(p)]
        if not paths:
            messagebox.showinfo(APP_TITLE, "В очереди нет книг для сохранения.")
            return
        if len(paths) == 1 and self.book and os.path.normcase(paths[0]) == os.path.normcase(self.book.path):
            self.export_dialog()
        else:
            self.export_dialog(paths=paths)

    def queue_clear(self):
        if self._queue() and messagebox.askyesno(APP_TITLE, "Очистить всю очередь книг?\n(Сами файлы не удаляются.)"):
            self._queue().clear()
            self.cfg.save()
            self.refresh_queue()

    def queue_next(self) -> str | None:
        i = self._queue_index()
        q = self._queue()
        start = 0 if i is None else i + 1
        for p in q[start:]:
            if os.path.isfile(p):
                return p
        return None

    def toggle_queue(self):
        """Очередь книг: свернуть влево (место — тексту) и развернуть обратно на прежнюю ширину."""
        show = bool(self.show_queue_var.get())
        self.cfg.set("show_queue", show)
        panes = [str(x) for x in self.paned.panes()]
        if show and str(self.queue_frame) not in panes:
            if str(self.queue_strip) in panes:
                self.paned.forget(self.queue_strip)
            self.paned.add(self.queue_frame, minsize=220, width=self.cfg.get("queue_width", 340),
                           stretch="never", before=self.paned.panes()[0])
        elif not show:
            if str(self.queue_frame) in panes:
                w = self.queue_frame.winfo_width()
                if w > 100:
                    self.cfg.set("queue_width", w)  # вернём на ту же ширину
                self.paned.forget(self.queue_frame)
            if str(self.queue_strip) not in [str(x) for x in self.paned.panes()]:
                self.paned.add(self.queue_strip, minsize=30, width=34, stretch="never",
                               before=self.paned.panes()[0])
        self._save_soon()

    def queue_collapse(self, show=None):
        self.show_queue_var.set(not self.show_queue_var.get() if show is None else show)
        self.toggle_queue()

    def open_book(self, path):
        self._opening = path
        self.stop()
        self.status_var.set(f"Открываю «{os.path.basename(path)}»…")
        self.root.config(cursor="watch")
        dialog = self.voice_settings().dialog

        def work():
            try:
                book = formats.load_book(path, notes=self.cfg.get("notes_mode", "skip"))
                full, chunks, par_pos = speech.build_text_and_chunks(book.paragraphs, dialog=dialog)
                self.q.put(("opened", (book, full, chunks, par_pos, dialog)))
            except formats.BookError as e:
                self.q.put(("open_error", e))
            except Exception as e:  # noqa: BLE001
                speech.log_error(e)
                self.q.put(("open_error", e))

        threading.Thread(target=work, daemon=True).start()

    def _set_chunks(self, book, chunks, par_pos, dialog):
        self.chunks = chunks
        self._chunks_dialog = dialog
        self._par_pos = par_pos
        self.chunk_starts = [c.start for c in chunks]
        self.chapters_ci = []
        for title, pi in book.chapters:
            if pi < len(par_pos):
                ci = bisect.bisect_left(self.chunk_starts, par_pos[pi])
                if ci < len(chunks) and (not self.chapters_ci or self.chapters_ci[-1][1] != ci):
                    self.chapters_ci.append((title, ci))
        self._pseudo_chapters = False
        if not self.chapters_ci and len(chunks) > 600:
            # в книге нет оглавления — делим на 10 частей, чтобы работали «Глава ⏮ ⏭» и выбор места
            step = len(chunks) // 10
            self.chapters_ci = [(f"Часть {k + 1} из 10 ({k * 10}%)", k * step) for k in range(10)]
            self._pseudo_chapters = True
        self.reader.set_chunks(chunks, set() if self._pseudo_chapters else {ci for _t, ci in self.chapters_ci})

    def _idx_for_char(self, ch):
        return max(0, min(len(self.chunks) - 1, bisect.bisect_right(self.chunk_starts, ch) - 1))

    def _rechunk(self, dialog):
        """Заново делит текст на фразы (с голосами героев речь и слова автора — отдельно), место не теряется."""
        was = self.reader.running and not self.paused
        ch = self.chunks[self.cur].start if self.chunks else 0
        self.reader.stop()
        _full, chunks, par_pos = speech.build_text_and_chunks(self.book.paragraphs, dialog=dialog)
        self._set_chunks(self.book, chunks, par_pos, dialog)
        self.cur = self._idx_for_char(ch)
        self.highlight(self.cur)
        if was:
            self.reader.start(self.cur)

    def _on_opened(self, data):
        book, full, chunks, par_pos, dialog = data
        self.root.config(cursor="")
        self.book = book
        self._set_chunks(book, chunks, par_pos, dialog)
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", full)
        self.text.mark_set("insert", "1.0")
        names = [t for t, _ in self.chapters_ci] or ["(глав нет)"]
        self.chapter_cb.configure(values=names)
        self.chapter_var.set(names[0])
        head = book.title + (f" — {book.author}" if book.author else "")
        self.root.title(f"{head} · {APP_TITLE}")
        bm = self.cfg.get("book_modes", {}).get(os.path.normcase(book.path))
        if bm and {k: bm.get(k) for k in self.MODE_KEYS} != {k: self.mode_dict().get(k) for k in self.MODE_KEYS}:
            self.apply_mode(bm, quiet=True)
            self.root.after(50, lambda: self.status_var.set(
                f"Для этой книги включён её голос: {self._voice_short(bm['voice'])} ({self.mode_summary(bm)})"))
        elif not bm:
            self.cfg.data.setdefault("book_modes", {})[os.path.normcase(book.path)] = self.mode_dict()
        if self.voice_settings().dialog != self._chunks_dialog:  # у книги свой режим голосов героев
            _f, chunks, par_pos = speech.build_text_and_chunks(book.paragraphs, dialog=self.voice_settings().dialog)
            self._set_chunks(book, chunks, par_pos, self.voice_settings().dialog)
        pc = self.cfg.position_char(book.path)
        self.cur = self._idx_for_char(pc) if pc is not None else min(self.cfg.position(book.path), max(0, len(chunks) - 1))
        self.highlight(self.cur, center=True)
        info = self.cfg.data.setdefault("book_info", {}).setdefault(os.path.normcase(book.path), {})
        info.update(title=book.title, author=book.author, total=len(chunks))
        if self.cur:
            info.pop("done", None)
        if not any(os.path.normcase(p) == os.path.normcase(book.path) for p in self._queue()):
            if len(self._queue()) < self.MAX_QUEUE:
                self._queue().append(os.path.abspath(book.path))
        self.refresh_queue()
        if getattr(self, "_autoplay", False):
            self._autoplay = False
            self.root.after(400, self.toggle_play)
        self.cfg.set("last_book", book.path)
        recent = [p for p in self.cfg.get("recent", []) if os.path.normcase(p) != os.path.normcase(book.path)]
        self.cfg.set("recent", [book.path] + recent[:9])
        self.cfg.save()
        self._rebuild_recent()
        self._bm_mark()
        if getattr(self, "_search_bar", None) is not None and self._search_bar.winfo_ismapped():
            self.search_close()
        where = "с начала" if self.cur == 0 else "с места, где остановились"
        glav = "оглавления нет — разделил на 10 частей" if self._pseudo_chapters else f"глав: {len(self.chapters_ci)}"
        self.status_var.set(f"{head}: {len(self.chunks)} фраз, {glav}. Нажмите «Читать» ({where}).")

    def _rebuild_recent(self):
        self.recent_menu.delete(0, "end")
        rec = [p for p in self.cfg.get("recent", []) if os.path.isfile(p)]
        if not rec:
            self.recent_menu.add_command(label="(пусто)", state="disabled")
        for p in rec:
            self.recent_menu.add_command(label=os.path.basename(p), command=lambda p=p: self.open_book(p))

    # ------------------------------------------------------------------ чтение
    def toggle_play(self):
        if not self.chunks:
            self.ask_open()
            return
        if self.reader.running:
            self.paused = not self.paused
            self.out.paused = self.paused
            self.play_btn.config(text="▶  Дальше" if self.paused else "⏸  Пауза")
            self.status_var.set("Пауза" if self.paused else "Читаю…")
            if self.paused:
                self._save_pos(self.cur, force=True)
        else:
            if not self.out.ok and not self.restart_audio(quiet=True):
                self._last_err = (None, 0.0)
                self.show_error(self.out.error, "начале чтения")
                return
            self._apply_settings_now()
            self.paused = False
            self.reader.start(self.cur)
            self.play_btn.config(text="⏸  Пауза")
            self.status_var.set("Читаю…")

    def resume_reading(self):
        """Повторить чтение с текущей фразы (после ошибки)."""
        if self.chunks and not self.reader.running:
            self.toggle_play()

    def _apply_settings_now(self):
        if self._settings_job:
            self.root.after_cancel(self._settings_job)
        self._apply_settings()

    def stop(self):
        self.reader.stop()
        self.paused = False
        self.out.paused = False
        self.play_btn.config(text="▶  Читать")
        if self.book:
            self._save_pos(self.cur, force=True)

    def goto(self, idx):
        if not self.chunks:
            return
        idx = max(0, min(idx, len(self.chunks) - 1))
        was = self.reader.running and not self.paused
        self.cur = idx
        self.highlight(idx, center=True)
        if was:
            self.reader.start(idx)
        elif self.reader.running:  # на паузе — переходим и остаёмся на паузе
            self.reader.stop()
            self.paused = False
            self.out.paused = False
            self.play_btn.config(text="▶  Читать")
        if self.book:
            self._save_pos(idx)

    def _save_pos(self, idx, force=False):
        ch = self.chunks[idx].start if self.chunks and 0 <= idx < len(self.chunks) else None
        self.cfg.set_position(self.book.path, idx, force=force, char=ch)

    def step(self, d):
        self.goto(self.cur + d)

    def _chapter_index(self):
        starts = [ci for _, ci in self.chapters_ci]
        return bisect.bisect_right(starts, self.cur) - 1

    def next_chapter(self):
        k = self._chapter_index()
        if k + 1 < len(self.chapters_ci):
            self.goto(self.chapters_ci[k + 1][1])

    def prev_chapter(self):
        k = self._chapter_index()
        if k >= 0:
            start = self.chapters_ci[k][1]
            # если мы в самом начале главы — на предыдущую
            if self.cur - start <= 2:
                start = self.chapters_ci[k - 1][1] if k > 0 else 0  # в начале первой главы — в начало книги
            self.goto(start)
        else:
            self.goto(0)

    def on_chapter_selected(self, _e=None):
        name = self.chapter_var.get()
        for i, (t, ci) in enumerate(self.chapters_ci):
            if t == name and self.chapter_cb.current() == i:
                self.goto(ci)
                break
        self.text.focus_set()

    def on_double_click(self, e):
        if not self.chunks:
            return "break"
        idx = self.text.index(f"@{e.x},{e.y}")
        pos = int(self.text.count("1.0", idx, "chars")[0] or 0) if idx != "1.0" else 0
        ci = max(0, bisect.bisect_right(self.chunk_starts, pos) - 1)
        self.cur = ci
        self.highlight(ci)
        self._apply_settings_now()
        self.paused = False
        self.out.paused = False
        self.reader.start(ci)
        self.play_btn.config(text="⏸  Пауза")
        self.status_var.set("Читаю…")
        return "break"

    def highlight(self, idx, center=False):
        if not self.chunks:
            return
        c = self.chunks[idx]
        s, e = f"1.0+{c.start}c", f"1.0+{c.end}c"
        self.text.tag_remove("cur", "1.0", "end")
        self.text.tag_add("cur", s, e)
        if self.autoscroll_var.get() or center:
            self._scroll_to(s, e)
        total = len(self.chunks)
        self.progress["value"] = int(1000 * idx / max(1, total - 1))
        k = self._chapter_index()
        if 0 <= k < len(self.chapters_ci) and self.chapter_cb.current() != k:
            self.chapter_cb.current(k)
        self.pos_var.set(f"{idx + 1} / {total}  ({100 * (idx + 1) // max(1, total)}%)")

    def _scroll_to(self, s, e):
        h = self.text.winfo_height()
        bs, be = self.text.bbox(s), self.text.bbox(e)
        # фраза целиком видна в «удобной» зоне — не дёргаем текст
        if bs and be and h > 50 and bs[1] > h * 0.08 and be[1] + be[3] < h * 0.8:
            return
        self.text.see(s)
        self.text.update_idletasks()
        bs = self.text.bbox(s)
        if bs and h > 50:
            delta = bs[1] - int(h * 0.25)
            if abs(delta) > 4:
                self.text.yview_scroll(delta, "pixels")

    # ------------------------------------------------------------------ события из потоков
    def _poll(self):
        self.out.bg_on = bool(self.reader.running and not self.paused)  # фон звучит только во время чтения
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "chunk":
                    self.cur = val
                    self.highlight(val)
                    self._bump_usage(self.voice_settings().voice, 0.2)
                    if val % 10 == 0 and self.book:
                        qi = self._queue_index()
                        if qi is not None and str(qi) in self.queue_tv.get_children():
                            self.queue_tv.set(str(qi), "pct", f"{100 * (val + 1) // max(1, len(self.chunks))}%")
                    if self.book:
                        self._save_pos(val)
                    if self.status_var.get().startswith("Готовлю"):
                        self.status_var.set("Читаю…")
                elif kind == "status":
                    self.status_var.set(val)
                elif kind == "finished":
                    self.play_btn.config(text="▶  Читать")
                    self.status_var.set("Книга дочитана до конца.")
                    if self.book:
                        self.cfg.data.setdefault("book_info", {}).setdefault(
                            os.path.normcase(self.book.path), {})["done"] = True
                        self._save_pos(0, force=True)
                    self.cur = 0
                    nxt = self.queue_next() if self.autonext_var.get() else None
                    if nxt:
                        self._autoplay = True
                        self.status_var.set("Книга дочитана — открываю следующую в очереди…")
                        self.open_book(nxt)
                    else:
                        self.refresh_queue()
                elif kind == "error":
                    self.play_btn.config(text="▶  Читать")
                    self.paused = False
                    self.show_error(val, "чтении вслух", {"retry": self.resume_reading, "actions": ["other_voice"]})
                elif kind == "error_ctx":
                    self.show_error(*val)
                elif kind == "media":
                    self._media_key(val)
                elif kind == "ocr_prog":
                    try:
                        val[0].configure(value=val[2])
                        val[1].config(text=val[3])
                    except tk.TclError:
                        pass
                elif kind == "ocr_done":
                    self._ocr_done(val)
                elif kind == "lib_books":
                    self._lib_books = val
                    self._lib_fill()
                elif kind == "lib_cover":
                    self._lib_cover(val)
                elif kind == "tray":
                    self._tray_event(val)
                elif kind == "ipc":
                    self.root.deiconify()
                    self.root.lift()
                    self.root.attributes("-topmost", True)
                    self.root.after(300, lambda: self.root.attributes("-topmost", False))
                    self.root.focus_force()
                    if val:
                        self.add_to_queue(val)
                        self.open_book(val[0])
                        if len(val) > 1:
                            self.status_var.set(f"Добавлено в очередь книг: {len(val)}")
                    else:
                        self.status_var.set("Читалка уже была открыта — вот она.")
                elif kind == "bg_error":
                    self.show_error(val, "фоновой работе программы")
                elif kind == "opened":
                    self._on_opened(val)
                elif kind == "open_error":
                    self.root.config(cursor="")
                    bad = getattr(self, "_opening", "")
                    if getattr(self, "_autoplay", False):
                        # идём по очереди дальше, пропуская книгу, которую не удалось открыть
                        q = self._queue()
                        i = next((k for k, p in enumerate(q) if os.path.normcase(p) == os.path.normcase(bad)), None)
                        nxt = next((p for p in q[(i + 1 if i is not None else len(q)):] if os.path.isfile(p)), None)
                        if nxt:
                            self.status_var.set(f"Пропускаю «{os.path.basename(bad)}» (не открывается) — следующая…")
                            self.open_book(nxt)
                            continue
                        self._autoplay = False
                    self.status_var.set("Не удалось открыть книгу")
                    self.show_error(val, f"открытии книги «{os.path.basename(bad)}»",
                                    {"path": bad, "retry": lambda b=bad: self.open_book(b)})
                elif kind == "catalog":
                    self.fill_voices()
                elif kind == "clone":
                    self._clone_event(*val)
                elif kind == "exp":
                    self._exp_event(*val)
                elif kind in ("tunnel_url", "tunnel_fail"):
                    self._tunnel_event(kind, val)
                elif kind == "upd":
                    self._upd_event(*val)
                elif kind == "comp":
                    cb = getattr(self, "_comp_line", None)
                    if cb:
                        cb(val)
                elif kind == "web_job":
                    try:
                        self._web_job(val)
                    except Exception as e:  # noqa: BLE001
                        speech.log_error(e)
                        self.status_var.set(f"📱 Задание с телефона не принято: {e}")
                elif kind == "web_cancel":
                    self._web_cancel(val)
                elif kind == "web_save":
                    self.cfg.save()
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    # ------------------------------------------------------------------ MP3
    BITRATES = speech.MP3_RATES
    QUALITY = [  # (кбит/с, подпись)
        (24, "Экономно — 24 кбит/с"),
        (32, "Хорошо, как у Audible — 32 кбит/с"),
        (48, "Очень хорошо — 48 кбит/с"),
        (64, "Отлично для речи — 64 кбит/с"),
        (96, "Студийно — 96 кбит/с"),
        (192, "★ Как образец «Transhumanism Inc.» — 192 кбит/с, 48 кГц  (рекомендовано)"),
    ]
    RECOMMENDED = 192
    SPLITS = [  # (подпись, минут в части, по главам, склеить в один файл в конце)
        ("Один цельный файл (с главами внутри)", 45, True, True),
        ("По главам (длинные главы — по ~45 мин)", 45, True, False), ("Части по ~30 минут", 30, False, False),
        ("Части по ~1 часу", 60, False, False), ("Части по ~2 часа", 120, False, False)]

    def _dialog(self, title, kind):
        w = tk.Toplevel(self.root)
        w.title(title)
        w.transient(self.root)
        w.resizable(False, False)
        w.configure(bg=self.PAL[kind]["bg"])
        f = ttk.Frame(w, padding=16, style=f"{kind}.TFrame")
        f.pack(fill="both", expand=True)
        return w, f

    def _export_range(self, sel):
        if sel == "all":
            return 0, len(self.chunks)
        if sel == "from":
            return self.cur, len(self.chunks)
        k = self._chapter_index()
        if k < 0:
            return 0, (self.chapters_ci[0][1] if self.chapters_ci else len(self.chunks))
        end = self.chapters_ci[k + 1][1] if k + 1 < len(self.chapters_ci) else len(self.chunks)
        return self.chapters_ci[k][1], end

    def export_dialog(self, paths=None):
        """Окно «Сохранить в MP3». paths — несколько книг из очереди (пакетное сохранение)."""
        batch = bool(paths)
        if not batch and not self.chunks:
            messagebox.showinfo(APP_TITLE, "Сначала откройте книгу.")
            return
        K = "Mp3"
        w, f = self._dialog("Сохранить в MP3 — «Аудио рассказ»", K)
        vs = self.voice_settings()
        volume = int(round(float(self.vol_var.get())))
        head = f"СОХРАНИТЬ В MP3: {len(paths)} КНИГ(И) ИЗ ОЧЕРЕДИ" if batch else "СОХРАНИТЬ КНИГУ В MP3"
        ttk.Label(f, text=head, style=f"{K}.Head.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        if batch:
            base = find_audio_base(paths[0])
            last = self.cfg.get("mp3_last_dir")
            dir_var = tk.StringVar(value=last if last and os.path.isdir(last) else base)
            ttk.Label(f, text="Общая папка (для каждой книги — своя папка «Аудио рассказ - …»):",
                      style=f"{K}.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 2))
        else:
            base = find_audio_base(self.book.path)
            dir_var = tk.StringVar(value=self._last_mp3_dir(self.book.path))
            ttk.Label(f, text="Папка:", style=f"{K}.TLabel").grid(row=1, column=0, sticky="w", pady=(8, 2))
        ttk.Entry(f, textvariable=dir_var, width=72, font=self.f_ui).grid(row=2, column=0, columnspan=2, sticky="we")

        def browse():
            d = filedialog.askdirectory(initialdir=base, parent=w)
            if d:
                dir_var.set(d)

        ttk.Button(f, text="Обзор…", style=f"{K}.TButton", command=browse).grid(row=2, column=2, padx=(6, 0))

        left = ttk.Frame(f, style=f"{K}.TFrame")
        left.grid(row=3, column=0, sticky="nw", pady=(12, 0))
        right = ttk.Frame(f, style=f"{K}.TFrame")
        right.grid(row=3, column=1, columnspan=2, sticky="nw", padx=(24, 0), pady=(12, 0))

        what = tk.StringVar(value="all")
        ttk.Label(left, text="Что сохранить", style=f"{K}.Head.TLabel").pack(anchor="w")
        opts = (("all", "Всю книгу"),) if batch else (("all", "Всю книгу"), ("from", "С текущего места до конца"),
                                                      ("chapter", "Только текущую главу"))
        for val, txt in opts:
            ttk.Radiobutton(left, text=txt, variable=what, value=val, style=f"{K}.TRadiobutton",
                            command=lambda: update()).pack(anchor="w", pady=1)
        split = tk.StringVar(value=self.cfg.get("mp3_split2", self.SPLITS[0][0]))
        if split.get() not in [x[0] for x in self.SPLITS]:
            split.set(self.SPLITS[0][0])
        ttk.Label(left, text="Деление на файлы", style=f"{K}.Head.TLabel").pack(anchor="w", pady=(12, 0))
        scb = ttk.Combobox(left, textvariable=split, state="readonly", width=38, font=self.f_ui,
                           values=[x[0] for x in self.SPLITS])
        scb.pack(anchor="w", pady=2)
        scb.bind("<<ComboboxSelected>>", lambda e: update())
        refs_var = tk.BooleanVar(value=self.cfg.get("mp3_skip_refs", True))
        ttk.Checkbutton(left, text="Не озвучивать библиографию,\nпримечания и указатели в конце",
                        variable=refs_var, style=f"{K}.TCheckbutton").pack(anchor="w", pady=(6, 0))
        ttk.Label(left, text="Дополнительно", style=f"{K}.Head.TLabel").pack(anchor="w", pady=(10, 0))
        m4b_var = tk.BooleanVar(value=self.cfg.get("mp3_m4b", False))
        ttk.Checkbutton(left, text="Ещё M4B — аудиокнига с главами и обложкой", variable=m4b_var,
                        style=f"{K}.TCheckbutton").pack(anchor="w")
        sp_vars = {}
        srow = ttk.Frame(left, style=f"{K}.TFrame")
        srow.pack(anchor="w")
        ttk.Label(srow, text="Ещё версии быстрее:", style=f"{K}.TLabel").pack(side="left")
        for fct in (1.2, 1.5):
            sp_vars[fct] = tk.BooleanVar(value=fct in self.cfg.get("mp3_speeds", []))
            ttk.Checkbutton(srow, text=f"×{fct:g}", variable=sp_vars[fct], style=f"{K}.TCheckbutton").pack(side="left", padx=4)
        bgk = self.bg_var.get()
        bg_mp3 = tk.BooleanVar(value=False)
        if bgk != "нет":
            ttk.Checkbutton(left, text=f"Добавить в MP3 фон «{bgk}» ({self.bg_level_var.get()}%)", variable=bg_mp3,
                            style=f"{K}.TCheckbutton").pack(anchor="w")
        draft_var = tk.BooleanVar(value=False)
        dv = self.cfg.get("draft_voice") or self._offline_voice()
        if dv and dv != vs.voice:
            ttk.Checkbutton(left, text=f"Сначала быстрый черновик ({self._voice_short(dv)}),\n"
                                       "чистовик потом заменит его", variable=draft_var,
                            style=f"{K}.TCheckbutton").pack(anchor="w")

        mode = tk.StringVar(value=str(self.cfg.get("mp3_mode", self.RECOMMENDED)))
        if mode.get() not in [str(q[0]) for q in self.QUALITY] + ["size"]:
            mode.set(str(self.RECOMMENDED))
        ttk.Label(right, text="Качество звука", style=f"{K}.Head.TLabel").pack(anchor="w")
        q_radios = []
        for kbps, txt in self.QUALITY:
            rb = ttk.Radiobutton(right, text=txt, variable=mode, value=str(kbps),
                                 style=f"{K}.TRadiobutton", command=lambda: update())
            rb.pack(anchor="w", pady=1)
            q_radios.append((kbps, txt, rb))
        row = ttk.Frame(right, style=f"{K}.TFrame")
        row.pack(anchor="w", pady=(6, 0))
        ttk.Radiobutton(row, text="Лучшее качество, но не больше:", variable=mode, value="size",
                        style=f"{K}.TRadiobutton", command=lambda: update()).pack(side="left")
        size_var = tk.StringVar(value=str(self.cfg.get("mp3_size_mb2", speech.BOOK_MAX_MB)))
        ttk.Entry(row, textvariable=size_var, width=7, font=self.f_ui, justify="right").pack(side="left", padx=4)
        ttk.Label(row, text="МБ", style=f"{K}.TLabel").pack(side="left")
        size_scope = tk.StringVar(value="на всю книгу")
        sc = ttk.Combobox(row, textvariable=size_scope, state="readonly", width=13, font=self.f_ui,
                          values=["на всю книгу", "на один файл"])
        sc.pack(side="left", padx=6)
        sc.bind("<<ComboboxSelected>>", lambda e: update())
        size_var.trace_add("write", lambda *a: (mode.set("size"), update()))

        est = ttk.Label(f, style=f"{K}.Value.TLabel", justify="left")
        est.grid(row=4, column=0, columnspan=3, sticky="w", pady=(14, 2))
        kind = vs.voice.split(":")[0]
        speed_hint = {"edge": "онлайн-голос: ~×30 (зависит от сервера Microsoft)", "silero": "Silero: ~×120",
                      "piper": "Piper: ~×20", "sapi": "голос Windows: ~×400", "xtts": "XTTS: ~×1,5 (очень медленно)"}
        ttk.Label(f, style=f"{K}.TLabel", justify="left", text=(
            f"Будут записаны ВСЕ текущие настройки: голос «{self._voice_short(vs.voice)}», скорость ×{vs.speed:.2f}, "
            f"тон {vs.pitch:+d}, стиль «{self.preset_var.get()}», паузы {vs.pause:.2f} с, громкость {volume}%.\n"
            f"Скорость сохранения — {speed_hint.get(kind, '')}. Чтение вслух и другие книги сохранению не мешают.\n"
            "Если сохранение прервётся, программа предложит продолжить с места остановки.")
        ).grid(row=5, column=0, columnspan=3, sticky="w")
        state = {}

        def update():
            sp = next(x for x in self.SPLITS if x[0] == split.get())
            if batch:
                secs = None
                n_files = None
                rng = None
            else:
                s, e = self._export_range(what.get())
                rng = (s, e)
                chars = sum(len(c.text) for c in self.chunks[s:e])
                secs = chars / (15.0 * max(0.5, vs.speed)) + (e - s) * vs.pause * 1.4
                n_files = 1 if sp[3] else max(1, round(secs / (sp[1] * 60) + 0.49))
            if mode.get() == "size" and secs:
                try:
                    mb = float(size_var.get().replace(",", "."))
                except ValueError:
                    mb = 0
                total_mb = mb * (n_files if size_scope.get() == "на один файл" else 1)
                kbps = speech.pick_bitrate(secs, total_mb)
                exact = total_mb * 0.92 * 1048576 * 8 / 1000 / max(1, secs)
                warn = "  ⚠ книга очень длинная — меньше 24 кбит/с нельзя, файл будет чуть больше" if exact < 24 else ""
            else:
                kbps = int(mode.get()) if mode.get() != "size" else 48
                warn = ""
                if secs and kbps * 1000 / 8 * secs / 1048576 > speech.BOOK_MAX_MB:
                    warn = f"  · больше {speech.BOOK_MAX_MB} МБ; нужен файл меньше — выберите «не больше … МБ»"
            for qk, qt, rb in q_radios:  # размер результата — сразу у каждого варианта качества
                mb_q = qk * 1000 / 8 * secs / 1048576 if secs else qk * 3600 * 1000 / 8 / 1048576
                rb.config(text=f"{qt}   →  ≈ {mb_q:.0f} МБ" + ("" if secs else " на час"))
            if secs:
                size = kbps * 1000 / 8 * secs / 1048576
                h, m = int(secs // 3600), int(secs % 3600 // 60)
                files = "1 файл" if n_files == 1 else f"≈ {n_files} файлов"
                est.config(text=f"Звучание ≈ {h} ч {m:02d} мин · {files} · битрейт {kbps} кбит/с · размер ≈ {size:.0f} МБ{warn}")
            else:
                est.config(text=(f"Книг: {len(paths)} · для каждой книги битрейт подберётся по её длине, "
                                 f"чтобы она была не больше {size_var.get()} МБ") if mode.get() == "size" else
                           f"Книг: {len(paths)} · битрейт {kbps} кбит/с (~{kbps * 0.43:.0f} МБ на час звучания)")
            if mode.get() == "size":
                try:
                    state["total_mb"] = float(size_var.get().replace(",", "."))
                except ValueError:
                    state["total_mb"] = speech.BOOK_MAX_MB
            state.update(kbps=kbps, rng=rng, split=sp, need_mb=(kbps * 1000 / 8 * secs / 1048576) if secs else 0)

        update()

        def go():
            out = dir_var.get().strip()
            if not out:
                return
            self.cfg.set("mp3_mode", mode.get())
            self.cfg.set("mp3_bitrate", state["kbps"])
            self.cfg.set("mp3_split2", split.get())
            self.cfg.set("mp3_skip_refs", bool(refs_var.get()))
            od = out.rstrip("\\/")
            # запоминаем папку-«контейнер»: если выбрали свою подпапку книги — её родителя, иначе — саму папку
            self.cfg.set("mp3_last_dir", od if batch or not os.path.basename(od).startswith("Аудио рассказ")
                         else os.path.dirname(od))
            if mode.get() == "size":
                self.cfg.set("mp3_size_mb2", size_var.get())
            out = os.path.abspath(out)
            if not batch and state["rng"][1] <= state["rng"][0]:
                messagebox.showinfo(APP_TITLE, "В выбранной части книги нет текста для озвучки "
                                               "(вы в самом конце книги или в главе нет фраз).\n\n"
                                               "Что сделать: выберите «Всю книгу» или другую главу.", parent=w)
                return
            if len(out) > 200:
                self.show_error(OSError(errno.ENAMETOOLONG, "путь слишком длинный", out), "выборе папки для MP3",
                                {"on_folder": dir_var.set}, parent=w)
                return
            if not self._check_out_dir(out, state.get("need_mb", 0), parent=w):
                return
            snap = {k: getattr(vs, k) for k in VoiceSettings.__dataclass_fields__}
            jobs = []
            for p in (paths if batch else [self.book.path]):
                od = os.path.join(out, self._fit_name(out, self._audio_folder_name(p), 90, reserve=1 + 70 + 9)) \
                    if batch else out
                jobs.append(dict(
                    id=f"{time.time():.6f}-{len(jobs)}", path=p,
                    title=self._book_title(p),
                    out_dir=od,
                    name_base=self._fit_name(od, "Аудио рассказ - " + self._book_title(p), 70, reserve=9),
                    range=None if batch or what.get() == "all" else list(state["rng"]),
                    vs=snap, volume=volume / 100.0, bitrate=state["kbps"],
                    size_mb=(state.get("total_mb") if mode.get() == "size" and size_scope.get() == "на всю книгу"
                             and batch else None),
                    split_minutes=state["split"][1], split_chapters=state["split"][2],
                    one_file=state["split"][3], skip_refs=bool(refs_var.get()),
                    m4b=bool(m4b_var.get()) and state["split"][3],
                    speeds=[f for f, v in sp_vars.items() if v.get()] if state["split"][3] else [],
                    bg_kind=bgk if bg_mp3.get() else "нет", bg_level=self.bg_level_var.get() / 100.0 * 0.6,
                    notes=self.cfg.get("notes_mode", "skip"),
                    voice_name=self._voice_short(vs.voice), status="wait", done=0, total=0))
            self.cfg.set("mp3_m4b", bool(m4b_var.get()))
            self.cfg.set("mp3_speeds", [f for f, v in sp_vars.items() if v.get()])
            if draft_var.get():
                jobs = self._with_drafts(jobs, dv)
            w.destroy()
            self.enqueue_export(jobs)

        b = ttk.Frame(f, style=f"{K}.TFrame")
        b.grid(row=6, column=0, columnspan=3, sticky="e", pady=(14, 0))
        ttk.Button(b, text="💾 Сохранить", style=f"{K}.TButton", command=go).pack(side="left", padx=4)
        ttk.Button(b, text="Отмена", style="Win.TButton", command=w.destroy).pack(side="left")
        w.grab_set()

    # ------------------------------------------------------------------ очередь сохранения в MP3
    def _book_title(self, path):
        if self.book and os.path.normcase(path) == os.path.normcase(self.book.path):
            return self.book.title
        info = self.cfg.data.get("book_info", {}).get(os.path.normcase(path))
        if info and info.get("title"):
            return info["title"]
        parts = book_stem(path).split("_")
        return parts[0] if len(parts) >= 3 else book_stem(path)

    def _audio_folder_name(self, path):
        return speech.safe_name("Аудио рассказ - " + book_stem(path), 140)

    def _jobs(self) -> list:
        return self.cfg.data.setdefault("export_jobs", [])

    def enqueue_export(self, jobs, show=True):
        """Добавляет задания. Одна и та же книга в одну и ту же папку дважды не ставится."""
        added, dup = [], []
        for j in jobs:
            same = next((x for x in self._jobs()
                         if os.path.normcase(x["path"]) == os.path.normcase(j["path"])
                         and os.path.normcase(os.path.abspath(x["out_dir"])) == os.path.normcase(os.path.abspath(j["out_dir"]))
                         and x.get("name_base", "") == j.get("name_base", "")), None)
            if same:
                if same.get("status") in ("stopped", "error"):
                    same.update({k: v for k, v in j.items() if k not in ("id",)})  # новые настройки, продолжение
                    same["status"] = "wait"
                    added.append(same)
                else:
                    dup.append(j)
            else:
                self._jobs().append(j)
                added.append(j)
        if dup and show:
            messagebox.showinfo(APP_TITLE, "Эта книга уже сохраняется в ту же папку:\n" +
                                "\n".join(f"• {j['title']}" for j in dup))
        if not added:
            return added
        self._save_jobs()
        self.status_var.set(f"💾 В очередь сохранения добавлено: {len(added)}")
        self._start_export_worker()
        if show:
            self.show_export_window()
        else:
            self._refresh_export_list()
        return added

    MAX_PARALLEL = 5

    def _parallel(self) -> int:
        return max(1, min(self.MAX_PARALLEL, int(self.cfg.get("mp3_parallel", 2))))

    def _start_export_worker(self):
        """Запускает до N фоновых потоков: каждый берёт следующее задание из очереди."""
        self._exp_stop = False
        if not hasattr(self, "_exp_lock"):
            self._exp_lock = threading.Lock()
            self._exp_threads = []
            self._exp_rows = {}
        self._exp_threads = [t for t in self._exp_threads if t.is_alive()]
        while len(self._exp_threads) < self._parallel():
            t = threading.Thread(target=self._export_worker, daemon=True)
            self._exp_threads.append(t)
            t.start()

    def _take_job(self):
        with self._exp_lock:
            if self._exp_stop:
                return None
            running = sum(1 for j in self._jobs() if j.get("status") == "run" and j.get("_taken"))
            if running >= self._parallel():
                return None
            busy = {os.path.normcase(os.path.abspath(j["out_dir"])) for j in self._jobs() if j.get("_taken")}
            job = next((j for j in self._jobs() if j.get("status") in ("wait", "run") and not j.get("_taken")
                        and os.path.normcase(os.path.abspath(j["out_dir"])) not in busy), None)
            if job is not None:
                job["_taken"] = True
                job["status"] = "run"
                job["_cancel"] = False
            return job

    def _export_worker(self):
        """Фоновый поток. Не зависит от чтения вслух, паузы и того, какая книга открыта в окне."""
        while not self._exp_stop:
            job = self._take_job()
            if job is None:
                break
            self.q.put(("exp", ("start", job)))
            self._save_jobs()
            t_hist = []

            def prog(d, t, m, cd=0, ct=0, job=job, t_hist=t_hist):
                job["done"], job["total"] = d, t
                now = time.time()
                if "Пауза" in m:
                    t_hist.clear()  # время паузы не портит оценку «осталось»
                elif "уже готов" not in m:
                    t_hist.append((now, cd))
                    while t_hist and now - t_hist[0][0] > 90:
                        t_hist.pop(0)
                rate = ((t_hist[-1][1] - t_hist[0][1]) / (t_hist[-1][0] - t_hist[0][0])
                        if len(t_hist) > 3 and t_hist[-1][0] - t_hist[0][0] > 5 else 0)
                self.q.put(("exp", ("progress", (job, d, t, m, rate, ct - cd))))

            try:
                (written, cancelled, skipped), nparts = speech.export_book_job(
                    job, prog, cancel=lambda job=job: job.get("_cancel") or self._exp_stop,
                    status=lambda msg, job=job: self.q.put(("exp", ("note", (job, msg)))),
                    parallel=self._parallel())
                if cancelled:
                    job["status"] = "stopped"
                    if job.get("_delete_files"):
                        self._delete_job_files(job)
                    if job.get("_remove"):
                        with self._exp_lock:
                            if job in self._jobs():
                                self._jobs().remove(job)
                    self.q.put(("exp", ("stopped", job)))
                else:
                    with self._exp_lock:
                        if job in self._jobs():
                            self._jobs().remove(job)
                    self._after_done(job, written)
                    self.q.put(("exp", ("done", (job, written, skipped))))
            except Exception as e:  # noqa: BLE001
                if "after shutdown" in str(e) or self._exp_stop:
                    # программу закрывают посреди сохранения — это не ошибка: при запуске предложим продолжить
                    job["status"] = "stopped"
                    job.pop("_taken", None)
                    self._save_jobs()
                    return
                speech.log_error(e)
                job["status"] = "error"
                job["error"] = errors.explain(e).title + ": " + speech.err_text(e)
                job["_exc"] = e
                self.q.put(("exp", ("fail", job)))
            job.pop("_taken", None)
            self._save_jobs()
        self.q.put(("exp", ("idle", None)))

    def _save_jobs(self):
        """Сохраняем очередь заданий (без служебных пометок) — чтобы продолжить после сбоя."""
        with getattr(self, "_exp_lock", threading.Lock()):
            clean = [{k: v for k, v in j.items() if not k.startswith("_")} for j in self._jobs()]
        data = dict(self.cfg.data)
        data["export_jobs"] = clean
        try:
            import json
            tmp = self.cfg.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.cfg.path)
        except Exception:  # noqa: BLE001
            pass

    def show_export_window(self, tab=None):
        self._tray_remove()
        w = getattr(self, "_exp_win", None)
        if w is not None and w.winfo_exists():
            w.deiconify()
            w.lift()
            self._refresh_export_list()
            if tab is not None:
                self._exp_nb.select(tab)
            return
        w, fr = self._dialog("Сохранение в MP3", "Mp3")
        # обычное окно: в заголовке «свернуть», «развернуть» и «закрыть», своя кнопка на панели задач
        w.wm_transient("")
        w.resizable(True, True)
        w.minsize(760, 420)
        self._exp_win = w
        top = ttk.Frame(fr, style="Mp3.TFrame")
        top.pack(fill="x")
        ttk.Label(top, text="СОХРАНЕНИЕ «АУДИО РАССКАЗ» В MP3", style="Mp3.Head.TLabel").pack(side="left")
        ttk.Label(top, text="   В очередь:", style="Mp3.TLabel").pack(side="left")
        self._par_var = tk.StringVar(value=str(self._parallel()))
        sp = ttk.Spinbox(top, from_=1, to=self.MAX_PARALLEL, width=3, textvariable=self._par_var, font=self.f_ui,
                         command=self._set_parallel, state="readonly")
        sp.pack(side="left", padx=6)
        self._exp_rows_frame = ttk.Frame(fr, style="Mp3.TFrame")
        self._exp_rows_frame.pack(fill="x", pady=(8, 0))
        if not hasattr(self, "_exp_rows"):
            self._exp_rows = {}
        self._exp_rows.clear()
        # вкладки: очередь заданий и журнал готовых MP3
        st = ttk.Style()
        st.configure("Mp3.TNotebook", background=self.PAL["Mp3"]["bg"], borderwidth=0)
        st.configure("Mp3.TNotebook.Tab", background=self.PAL["Mp3"]["btn"], padding=(12, 2), font=self.f_bold)
        st.map("Mp3.TNotebook.Tab", background=[("selected", self.PAL["Mp3"]["bg"])])
        self._exp_nb = ttk.Notebook(fr, style="Mp3.TNotebook")
        self._exp_nb.pack(fill="both", expand=True, pady=(10, 0))
        qtab = ttk.Frame(self._exp_nb, style="Mp3.TFrame", padding=4)
        htab = ttk.Frame(self._exp_nb, style="Mp3.TFrame", padding=4)
        self._exp_nb.add(qtab, text="⏳ Очередь сохранения")
        self._exp_nb.add(htab, text="📜 Готовые MP3")
        self._build_history(htab)
        lf = ttk.Frame(qtab, style="Mp3.TFrame")
        lf.pack(fill="both", expand=True)
        tv = ttk.Treeview(lf, columns=("st", "book", "voice", "dir"), show="headings", height=5, selectmode="extended")
        for c, txt, wdt in (("st", "Состояние", 130), ("book", "Книга", 330), ("voice", "Голос", 110), ("dir", "Папка", 260)):
            tv.heading(c, text=txt, anchor="w")
            tv.column(c, width=wdt, anchor="w", stretch=c in ("book", "dir"))
        sb = ttk.Scrollbar(lf, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tv.pack(side="left", fill="both", expand=True)
        tv.bind("<Delete>", lambda e: self.delete_jobs(self._sel_jobs()))
        tv.bind("<space>", lambda e: self._toggle_pause(self._sel_jobs()))
        self._exp_tv = tv
        jb = ttk.Frame(qtab, style="Mp3.TFrame")
        jb.pack(fill="x", pady=(4, 0))
        ttk.Button(jb, text="➕ Добавить книги…", style="Mp3.TButton", command=self.jobs_add).pack(side="left")
        ttk.Button(jb, text="⏸ Пауза", style="Mp3.TButton", command=lambda: self._toggle_pause(
            self._sel_jobs() or self._jobs(), want=True)).pack(side="left", padx=(3, 0))
        ttk.Button(jb, text="▶ Продолжить", style="Mp3.TButton", command=self.jobs_resume).pack(side="left", padx=3)
        ttk.Button(jb, text="⏹ Остановить", style="Mp3.TButton", command=self.jobs_stop).pack(side="left")
        ttk.Button(jb, text="⬆", width=3, style="Mp3.TButton", command=lambda: self.jobs_move(-1)).pack(side="left", padx=(3, 0))
        ttk.Button(jb, text="⬇", width=3, style="Mp3.TButton", command=lambda: self.jobs_move(1)).pack(side="left", padx=3)
        ttk.Button(jb, text="✖ Убрать", style="Mp3.TButton", command=self.jobs_remove).pack(side="left")
        ttk.Button(jb, text="🗑 Удалить с файлами…", style="Mp3.TButton",
                   command=lambda: self.delete_jobs(self._sel_jobs())).pack(side="left", padx=3)
        ttk.Button(jb, text="📂 Папка", style="Mp3.TButton", command=self.jobs_folder).pack(side="left")
        b = ttk.Frame(fr, style="Mp3.TFrame")
        b.pack(anchor="e", pady=(10, 0))
        self._pause_all_btn = ttk.Button(b, text="⏸ Пауза всё", style="Mp3.TButton",
                                         command=lambda: self._toggle_pause(self._jobs()))
        self._pause_all_btn.pack(side="left", padx=3)
        ttk.Button(b, text="⏹ Остановить всё", style="Mp3.TButton", command=self.stop_export).pack(side="left", padx=3)
        ttk.Button(b, text="Свернуть в трей", style="Win.TButton", command=self._hide_export_window).pack(
            side="left", padx=3)
        w.protocol("WM_DELETE_WINDOW", self._hide_export_window)
        # «свернуть» (—) в заголовке: окно уходит значком в трей у часов, оттуда разворачивается обратно
        w.bind("<Unmap>", lambda e: self.root.after(30, self._exp_minimized) if e.widget is w else None)
        for j in self._jobs():
            if j.get("_taken"):
                self._exp_row(j)
        self._refresh_export_list()
        self._dnd_register(w, "mp3")
        self._dnd_register(self._exp_tv, "mp3")
        if tab is not None:
            self._exp_nb.select(tab)

    def _set_parallel(self):
        try:
            n = max(1, min(self.MAX_PARALLEL, int(self._par_var.get())))
        except ValueError:
            return
        self.cfg.set("mp3_parallel", n)
        self._save_soon()
        if any(j.get("status") in ("wait", "run") for j in self._jobs()):
            self._start_export_worker()

    def _exp_row(self, job):
        """Строка прогресса для одной книги (создаётся по требованию)."""
        if not getattr(self, "_exp_win", None) or not self._exp_win.winfo_exists():
            return None
        r = self._exp_rows.get(job["id"])
        if r:
            return r
        fr = ttk.Frame(self._exp_rows_frame, style="Mp3.TFrame", padding=(0, 4))
        fr.pack(fill="x")
        title = ttk.Label(fr, text=f"▶ {job['title'][:70]} — {job.get('voice_name', '')}", style="Mp3.Value.TLabel")
        title.grid(row=0, column=0, columnspan=3, sticky="w")
        pb = ttk.Progressbar(fr, length=520, maximum=1000, style="Blue.Horizontal.TProgressbar")
        pb.grid(row=1, column=0, sticky="we", pady=3)
        pct = ttk.Label(fr, text="0%", width=5, anchor="e", style="Pct.TLabel")
        pct.grid(row=1, column=1, padx=(10, 6))
        pbtn = ttk.Button(fr, text="⏸", width=3, style="Mp3.TButton", command=lambda j=job: self._toggle_pause([j]))
        pbtn.grid(row=1, column=2)
        ttk.Button(fr, text="⏹", width=3, style="Mp3.TButton",
                   command=lambda j=job: self._stop_job(j)).grid(row=1, column=3, padx=(4, 0))
        ttk.Button(fr, text="🗑", width=3, style="Mp3.TButton",
                   command=lambda j=job: self.delete_jobs([j])).grid(row=1, column=4, padx=(4, 0))
        ttk.Button(fr, text="📂", width=3, style="Mp3.TButton",
                   command=lambda j=job: (os.makedirs(j["out_dir"], exist_ok=True), self._open_folder(j["out_dir"]))
                   ).grid(row=1, column=5, padx=(4, 0))
        info = ttk.Label(fr, text="Открываю книгу…", style="Mp3.TLabel")
        info.grid(row=2, column=0, columnspan=6, sticky="w")
        fr.columnconfigure(0, weight=1)
        r = dict(frame=fr, title=title, pb=pb, pct=pct, info=info, note="", pbtn=pbtn)
        self._exp_rows[job["id"]] = r
        return r

    def _drop_row(self, job):
        r = getattr(self, "_exp_rows", {}).pop(job["id"], None)
        if r:
            try:
                r["frame"].destroy()
            except tk.TclError:
                pass

    def _stop_job(self, job):
        if messagebox.askyesno(APP_TITLE, f"Остановить сохранение «{job['title']}»?\n"
                                          "Готовые файлы сохранятся, продолжить можно позже.",
                               parent=self._exp_win):
            job["_cancel"] = True

    def stop_export(self):
        if not any(j.get("status") in ("run", "wait") for j in self._jobs()):
            return
        if not messagebox.askyesno(APP_TITLE, "Остановить все сохранения в MP3?\n\n"
                                              "Готовые файлы сохранятся. Продолжить можно позже — программа "
                                              "предложит это при следующем запуске.",
                                   parent=getattr(self, "_exp_win", None)):
            return
        self._exp_stop = True
        for j in self._jobs():
            j["_cancel"] = True
            if j.get("status") == "wait":
                j["status"] = "stopped"
        self._save_jobs()

    def _refresh_export_list(self):
        tv = getattr(self, "_exp_tv", None)
        if tv is None or not self._exp_win.winfo_exists():
            return
        sel = set(tv.selection())
        tv.delete(*tv.get_children())
        for j in self._jobs():
            if j.get("_taken"):
                st = ("⏸ пауза " if j.get("_paused") else "▶ идёт ") + \
                    f"{100 * j.get('done', 0) // max(1, j.get('total', 1))}%"
            else:
                st = {"wait": "⏳ ждёт", "stopped": "⏹ остановлено", "error": "⚠ ошибка", "paused": "⏸ пауза",
                      "run": "⏳ ждёт"}.get(j.get("status"), "•")
                if j.get("status") in ("stopped", "error", "paused") and j.get("total"):
                    st += f" ({100 * j.get('done', 0) // max(1, j['total'])}%)"
            tv.insert("", "end", iid=j["id"], values=(st, j["title"], j.get("voice_name", ""), j["out_dir"]))
        keep = [i for i in sel if tv.exists(i)]
        if keep:
            tv.selection_set(keep)

    # ------------------------------------------------------------------ журнал готовых MP3
    def _history(self) -> list:
        return self.cfg.data.setdefault("export_history", [])

    def _build_history(self, f):
        lf = ttk.Frame(f, style="Mp3.TFrame")
        lf.pack(fill="both", expand=True)
        tv = ttk.Treeview(lf, columns=("date", "book", "voice", "n", "mb", "dir"), show="headings", height=6,
                          selectmode="extended")
        for c, txt, wdt in (("date", "Дата", 130), ("book", "Книга", 300), ("voice", "Голос", 100),
                            ("n", "Файлов", 70), ("mb", "Размер", 80), ("dir", "Папка", 320)):
            tv.heading(c, text=txt, anchor="w")
            tv.column(c, width=wdt, anchor="w", stretch=c in ("book", "dir"))
        sb = ttk.Scrollbar(lf, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tv.pack(side="left", fill="both", expand=True)
        tv.bind("<Double-1>", lambda e: self.history_open_folder())
        tv.bind("<Delete>", lambda e: self.history_remove())
        self._hist_tv = tv
        b = ttk.Frame(f, style="Mp3.TFrame")
        b.pack(fill="x", pady=(4, 0))
        ttk.Button(b, text="📂 Открыть папку", style="Mp3.TButton", command=self.history_open_folder).pack(side="left")
        ttk.Button(b, text="📖 Открыть книгу", style="Mp3.TButton", command=self.history_open_book).pack(side="left", padx=3)
        ttk.Button(b, text="✖ Убрать запись", style="Mp3.TButton", command=self.history_remove).pack(side="left")
        ttk.Button(b, text="🗜 Уменьшить размер…", style="Mp3.TButton", command=self.history_compress).pack(
            side="left", padx=3)
        ttk.Button(b, text="🗑 Удалить с файлами…", style="Mp3.TButton", command=self.history_delete).pack(side="left")
        ttk.Button(b, text="📨 В Telegram", style="Mp3.TButton", command=self.history_telegram).pack(side="left", padx=3)
        ttk.Button(b, text="⏩ ×1.2 / ×1.5 / M4B…", style="Mp3.TButton", command=self.history_versions).pack(side="left")
        ttk.Label(b, text="  (сами файлы не удаляются; двойной щелчок — открыть папку)",
                  style="Mp3.TLabel").pack(side="left")
        self.refresh_history()

    def refresh_history(self):
        tv = getattr(self, "_hist_tv", None)
        if tv is None:
            return
        try:
            tv.delete(*tv.get_children())
        except tk.TclError:
            return
        for i, h in enumerate(reversed(self._history())):
            exists = os.path.isdir(h["out_dir"])
            mb = ""
            if exists:
                try:
                    mb = f"{sum(e.stat().st_size for e in os.scandir(h['out_dir']) if e.name.lower().endswith('.mp3')) / 1048576:.0f} МБ"
                except OSError:
                    pass
            tv.insert("", "end", iid=str(len(self._history()) - 1 - i),
                      values=(h["date"], h["title"], h.get("voice", ""), h.get("files", ""), mb,
                              h["out_dir"] + ("" if exists else "  (папки нет)")))

    def _hist_sel(self):
        tv = getattr(self, "_hist_tv", None)
        return [int(i) for i in tv.selection()] if tv else []

    def history_open_folder(self):
        sel = self._hist_sel()
        if sel:
            d = self._history()[sel[0]]["out_dir"]
            if os.path.isdir(d):
                self._open_folder(d)
            else:
                messagebox.showinfo(APP_TITLE, f"Папки больше нет:\n{d}", parent=self._exp_win)

    def history_open_book(self):
        sel = self._hist_sel()
        if sel:
            p = self._history()[sel[0]]["path"]
            if os.path.isfile(p):
                self.add_to_queue([p])
                self.open_book(p)

    def history_remove(self):
        sel = sorted(self._hist_sel(), reverse=True)
        if sel and messagebox.askyesno(APP_TITLE, f"Убрать из журнала записей: {len(sel)}?\n(Файлы MP3 останутся.)",
                                       parent=self._exp_win):
            for i in sel:
                del self._history()[i]
            self._save_jobs()
            self.refresh_history()

    def _add_history(self, job, written, skipped):
        self._history().append(dict(
            date=time.strftime("%Y-%m-%d %H:%M"), title=job["title"], path=job["path"], out_dir=job["out_dir"],
            files=len(written), voice=job.get("voice_name", ""), bitrate=job.get("bitrate"),
            name_base=job.get("name_base", ""), written=[os.path.basename(x) for x in written],
            volume=job.get("volume"), skipped=len(skipped), phone=bool(job.get("web"))))
        del self._history()[:-500]
        info = self.cfg.data.setdefault("book_info", {}).setdefault(os.path.normcase(job["path"]), {})
        info["mp3_dir"] = job["out_dir"]
        self._save_jobs()
        self.refresh_history()

    def _last_mp3_dir(self, path):
        """Куда сохранять: туда же, куда эту книгу сохраняли раньше, иначе — в последнюю выбранную папку."""
        prev = next((h["out_dir"] for h in reversed(self._history())
                     if os.path.normcase(h["path"]) == os.path.normcase(path) and os.path.isdir(h["out_dir"])), None)
        if prev:
            return prev
        last = self.cfg.get("mp3_last_dir")
        lib = library_root()
        if last and os.path.isdir(last) and not (lib and os.path.normcase(os.path.abspath(last)) ==
                                                 os.path.normcase(os.path.abspath(lib))):  # не мусорим в корне библиотеки
            return os.path.join(last, self._audio_folder_name(path))
        return os.path.join(find_audio_base(path), self._audio_folder_name(path))

    # ------------------------------------------------------------------ уменьшить размер готовых MP3
    def history_compress(self):
        sel = self._hist_sel()
        if not sel:
            messagebox.showinfo(APP_TITLE, "Выделите запись в списке «Готовые MP3».", parent=self._exp_win)
            return
        h = self._history()[sel[0]]
        self.compress_dialog(h["out_dir"], h["title"], parent=self._exp_win)

    def compress_folder_ask(self):
        d = filedialog.askdirectory(title="Папка с MP3, которые нужно уменьшить",
                                    initialdir=find_audio_base(self.book.path) if self.book else (library_root() or "~"))
        if d:
            name = os.path.basename(d.rstrip("\\/"))
            title = name.replace("Аудио рассказ - ", "").split("_")[0]
            self.compress_dialog(d, title)

    def compress_dialog(self, folder, title, parent=None):
        """Готовая книга → один цельный файл не больше N МБ (или просто уменьшить файлы)."""
        if not os.path.isdir(folder):
            self.show_error(FileNotFoundError(errno.ENOENT, "папки нет", folder), "уменьшении размера MP3")
            return
        busy = [j for j in self._jobs() if (j.get("status") in ("run", "wait") or j.get("_taken"))
                and os.path.normcase(os.path.abspath(j["out_dir"])) == os.path.normcase(os.path.abspath(folder))]
        if busy:
            messagebox.showinfo(APP_TITLE, "В эту папку сейчас идёт сохранение. Дождитесь его окончания — "
                                           "потом можно уменьшить размер всей книги сразу.", parent=parent)
            return
        files = sorted(os.path.join(folder, n) for n in os.listdir(folder) if n.lower().endswith(".mp3"))
        if not files:
            messagebox.showinfo(APP_TITLE, f"В папке нет MP3:\n{folder}", parent=parent)
            return
        self.status_var.set("Считаю длительность MP3…")
        self.root.update_idletasks()
        info = []
        for f in files:
            with open(f, "rb") as fh:
                tags = speech.read_id3_text(fh.read(65536))
            t = re.sub(r"^\d+\.\s*", "", tags.get("TIT2", "")) or os.path.splitext(os.path.basename(f))[0]
            info.append(dict(path=f, title=t, artist=tags.get("TPE1", ""), secs=speech.mp3_seconds(f),
                             size=os.path.getsize(f)))
        refs = [x for x in info if speech.is_reference(x["title"])]
        size = sum(x["size"] for x in info) / 1048576
        K = "Mp3"
        w, f = self._dialog("Один файл / уменьшить размер", K)
        ttk.Label(f, text="ОДИН ЦЕЛЬНЫЙ ФАЙЛ И МЕНЬШИЙ РАЗМЕР", style=f"{K}.Head.TLabel").pack(anchor="w")
        all_secs = sum(x["secs"] for x in info)
        ttk.Label(f, style=f"{K}.TLabel", justify="left", text=(
            f"«{title}»\n{len(files)} файлов · звучание {int(all_secs // 3600)} ч {int(all_secs % 3600 // 60):02d} мин"
            f" · сейчас {size:.0f} МБ")).pack(anchor="w", pady=(6, 8))
        merge_var = tk.BooleanVar(value=len(files) > 1)
        ttk.Checkbutton(f, text="Склеить в один файл (главы сохранятся внутри — плеер покажет оглавление)",
                        variable=merge_var, style=f"{K}.TCheckbutton", command=lambda: upd()).pack(anchor="w")
        refs_var = tk.BooleanVar(value=bool(refs))
        if refs:
            rs = sum(x["secs"] for x in refs)
            rnames = ", ".join(sorted({re.sub(r" \(\d+\)$", "", x["title"]) for x in refs}))
            ttk.Checkbutton(f, text=f"Убрать справочные разделы ({rnames} — {int(rs // 3600)} ч {int(rs % 3600 // 60):02d} мин)",
                            variable=refs_var, style=f"{K}.TCheckbutton", command=lambda: upd()).pack(anchor="w")
        row = ttk.Frame(f, style=f"{K}.TFrame")
        row.pack(anchor="w", pady=(8, 0))
        ttk.Label(row, text="Не больше:", style=f"{K}.TLabel").pack(side="left")
        mb_var = tk.StringVar(value=str(speech.BOOK_MAX_MB))
        ttk.Entry(row, textvariable=mb_var, width=6, font=self.f_ui, justify="right").pack(side="left", padx=4)
        ttk.Label(row, text="МБ на всю книгу", style=f"{K}.TLabel").pack(side="left")
        est = ttk.Label(f, style=f"{K}.Value.TLabel", justify="left")
        est.pack(anchor="w", pady=(8, 0))
        st = {}

        def upd(*_):
            try:
                mb = float(mb_var.get().replace(",", "."))
            except ValueError:
                return
            keep = [x for x in info if not (refs_var.get() and x in refs)]
            secs = sum(x["secs"] for x in keep)
            k = speech.pick_bitrate(secs, mb)
            new = k * 1000 / 8 * secs / 1048576
            st.update(k=k, keep=keep)
            q = {24: "экономно, речь разборчивая", 32: "хорошо (как у Audible)", 40: "хорошо",
                 48: "очень хорошо", 56: "отлично", 64: "отлично"}.get(k, "")
            what = "один файл" if merge_var.get() else f"{len(keep)} файлов"
            est.config(text=f"Станет {what} ≈ {new:.0f} МБ · {k} кбит/с — {q}.\n"
                            "Голос, громкость и названия глав не меняются. Старые файлы — в Корзину.")

        mb_var.trace_add("write", upd)
        upd()
        b = ttk.Frame(f, style=f"{K}.TFrame")
        b.pack(anchor="e", pady=(14, 0))

        def go():
            w.destroy()
            self._compress_run(folder, info, st["keep"], st["k"], title, merge_var.get())

        ttk.Button(b, text="🗜 Сделать", style=f"{K}.TButton", command=go).pack(side="left", padx=4)
        ttk.Button(b, text="Отмена", style="Win.TButton", command=w.destroy).pack(side="left")
        self.status_var.set("")
        w.grab_set()

    def _compress_run(self, folder, info, keep, kbps, title, merge):
        from concurrent.futures import ThreadPoolExecutor
        album = f"Аудио рассказ — {title}"
        drop = [x for x in info if x not in keep]
        artist = next((x["artist"] for x in info if x["artist"]), "")

        def trash(paths):
            try:
                from send2trash import send2trash
            except Exception:  # noqa: BLE001
                send2trash = os.remove
            for pth in paths:
                try:
                    send2trash(pth)
                except OSError:
                    pass

        def work():
            try:
                before = sum(x["size"] for x in info)
                if merge:
                    out = os.path.join(folder, self._fit_name(folder, "Аудио рассказ - " + title, 70, reserve=4) + ".mp3")
                    if any(os.path.normcase(out) == os.path.normcase(x["path"]) for x in info):
                        out = out[:-4] + " (один файл).mp3"
                    same = all(abs(x["size"] * 8 / 1000 / max(1, x["secs"]) - kbps) < 3 for x in keep)
                    speech.merge_mp3([x["path"] for x in keep], out, [x["title"] for x in keep], title, artist,
                                     kbps=None if same else kbps,
                                     progress=lambda i, n, m: self.q.put(("status", f"🗜 «{title}»: {m}…")))
                    trash([x["path"] for x in info])
                    after = os.path.getsize(out)
                    res = f"один файл {after / 1048576:.0f} МБ ({kbps} кбит/с)"
                else:
                    todo = [x for x in keep if x["size"] * 8 / 1000 / max(1, x["secs"]) > kbps + 4]
                    n = [0]

                    def one(x):
                        speech.recompress_mp3(x["path"], kbps, album=album)
                        n[0] += 1
                        self.q.put(("status", f"🗜 Уменьшаю MP3 «{title}»: {n[0]} из {len(todo)}…"))

                    with ThreadPoolExecutor(max_workers=3) as ex:
                        list(ex.map(one, todo))
                    trash([x["path"] for x in drop])
                    after = sum(os.path.getsize(x["path"]) for x in keep)
                    res = f"{len(keep)} файлов, {after / 1048576:.0f} МБ ({kbps} кбит/с)"
                self.q.put(("status", f"🗜 Готово: «{title}» — {res}. Освобождено {(before - after) / 1048576:.0f} МБ, "
                                      "старые файлы в Корзине."))
                for h in self._history():
                    if os.path.normcase(h["out_dir"]) == os.path.normcase(folder):
                        h["bitrate"] = kbps
                        h["files"] = 1 if merge else len(keep)
                self.q.put(("exp", ("idle", None)))
            except Exception as e:  # noqa: BLE001
                speech.log_error(e)
                self.q.put(("error_ctx", (e, f"обработке MP3 «{title}»",
                                          {"folder": folder, "actions": ["open_folder"],
                                           "note": "Исходные файлы не тронуты — их удаляют только после успеха."})))

        self.status_var.set(f"🗜 «{title}»: начинаю…")
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ перетаскивание файлов
    def _dnd_register(self, widget, target):
        try:
            from tkinterdnd2 import DND_FILES
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", lambda e, t=target: self._on_drop(e, t))
            widget.dnd_bind("<<DropEnter>>", lambda e, t=target: self._drop_hint(t))
            widget.dnd_bind("<<DropLeave>>", lambda e: self._drop_hint(None))
        except Exception:  # noqa: BLE001  (нет tkinterdnd2 — работаем без перетаскивания)
            pass

    def _drop_hint(self, target):
        if target is None:
            if getattr(self, "_status_before_drop", None) is not None:
                self.status_var.set(self._status_before_drop)
                self._status_before_drop = None
            return "copy"
        if getattr(self, "_status_before_drop", None) is None:
            self._status_before_drop = self.status_var.get()
        self.status_var.set({"open": "📥 Отпустите — книга откроется (и встанет в очередь)",
                             "queue": "📥 Отпустите — книги добавятся в очередь",
                             "mp3": "📥 Отпустите — книги встанут в очередь сохранения MP3"}[target])
        return "copy"

    def _dropped_books(self, data) -> list:
        exts = tuple(formats.SUPPORTED) + (".zip",)
        out = []
        for p in self.root.tk.splitlist(data):
            if os.path.isdir(p):
                for dp, _dn, fn in os.walk(p):
                    out += [os.path.join(dp, f) for f in sorted(fn) if f.lower().endswith(exts)]
            elif os.path.isfile(p) and p.lower().endswith(exts):
                out.append(p)
        return out[:self.MAX_QUEUE]

    def _on_drop(self, e, target):
        self._drop_hint(None)
        books = self._dropped_books(e.data)
        if not books:
            self.status_var.set("Перетащенные файлы не похожи на книги (FB2, EPUB, PDF, DOCX, TXT…)")
            return "copy"
        if target == "mp3":
            self.export_dialog(paths=books)
        elif target == "queue":
            n = self.add_to_queue(books)
            self.status_var.set(f"📥 В очередь добавлено книг: {n}")
        else:
            self.add_to_queue(books)
            self.open_book(books[0])
            if len(books) > 1:
                self.status_var.set(f"📥 Открыта первая книга, остальные ({len(books) - 1}) — в очереди")
        return "copy"

    def _sel_jobs(self) -> list:
        ids = set(self._exp_tv.selection()) if getattr(self, "_exp_tv", None) else set()
        return [j for j in self._jobs() if j["id"] in ids]

    def jobs_add(self):
        start = os.path.dirname(self.book.path) if self.book else (library_root() or os.path.expanduser("~"))
        exts = " ".join("*" + e for e in formats.SUPPORTED)
        paths = filedialog.askopenfilenames(parent=self._exp_win, title="Книги для сохранения в MP3",
                                            initialdir=start, filetypes=[("Книги", exts + " *.zip"), ("Все файлы", "*.*")])
        paths = [p for p in (paths or []) if p]
        if paths:
            self.export_dialog(paths=paths)

    def jobs_resume(self):
        sel = self._sel_jobs()
        if any(j.get("_paused") or j.get("status") == "paused" for j in (sel or self._jobs())):
            self._toggle_pause(sel or self._jobs(), want=False)
        js = [j for j in sel if j.get("status") in ("stopped", "error")] or \
             ([] if sel else [j for j in self._jobs() if j.get("status") in ("stopped", "error")])
        if not js:
            return
        for j in js:
            j["status"] = "wait"
            j.pop("error", None)
        self._save_jobs()
        self._start_export_worker()
        self._refresh_export_list()

    def _toggle_pause(self, js, want=None):
        """Пауза / продолжение. Идущее сохранение замирает на месте — ничего не теряется."""
        js = [j for j in js if j.get("_taken") or j.get("status") in ("wait", "paused")]
        if not js:
            return
        if want is None:
            want = not all(j.get("_paused") or j.get("status") == "paused" for j in js)
        for j in js:
            if j.get("_taken"):
                j["_paused"] = want
            else:
                j["status"] = "paused" if want else "wait"
        if not want:
            self._start_export_worker()
        self._save_jobs()
        self._pause_refresh()
        self.status_var.set("⏸ Сохранение в MP3 на паузе — «▶ Продолжить», когда нужно." if want
                            else "▶ Сохранение в MP3 продолжается.")

    def _pause_refresh(self):
        for j in self._jobs():
            r = getattr(self, "_exp_rows", {}).get(j["id"])
            if r:
                try:
                    r["pbtn"].config(text="▶" if j.get("_paused") else "⏸")
                    if j.get("_paused"):
                        r["info"].config(text="⏸ Пауза. Нажмите ▶, чтобы продолжить с этого же места.")
                except tk.TclError:
                    pass
        act = [j for j in self._jobs() if j.get("_taken") or j.get("status") in ("wait", "paused")]
        allp = bool(act) and all(j.get("_paused") or j.get("status") == "paused" for j in act)
        try:
            self._pause_all_btn.config(text="▶ Продолжить всё" if allp else "⏸ Пауза всё")
        except (AttributeError, tk.TclError):
            pass
        self._refresh_export_list()
        self._update_exp_status()
        self._tray_update()

    def jobs_stop(self):
        js = [j for j in (self._sel_jobs() or self._jobs()) if j.get("_taken") or j.get("status") in ("wait", "paused")]
        if not js:
            return
        if not messagebox.askyesno(APP_TITLE, f"Остановить сохранение: {len(js)}?\n\nГотовые файлы останутся, "
                                              "продолжить можно позже кнопкой «▶ Продолжить».", parent=self._exp_win):
            return
        for j in js:
            if j.get("_taken"):
                j["_cancel"] = True
                j["_paused"] = False
            else:
                j["status"] = "stopped"
        self._save_jobs()
        self._refresh_export_list()

    def _job_files(self, job) -> list:
        """Файлы, которые записало именно это задание (другие файлы в папке не трогаем)."""
        import re as _re
        d = job["out_dir"]
        if not os.path.isdir(d):
            return []
        base = job.get("name_base", "")
        pat = _re.compile(r"^(?:" + (_re.escape(base) + " - " if base else "") + r")?\d\d\.mp3(?:\.part)?$")
        whole = {f"{base}.mp3", f"{base}.mp3.part", f"{base} (один файл).mp3"} if base else set()
        listed = {os.path.basename(x) for x in job.get("written", [])}
        return [os.path.join(d, n) for n in os.listdir(d)
                if pat.match(n) or n in whole or n in listed or n == "Содержание.txt"]

    def _delete_job_files(self, job):
        """Удаляет MP3 задания в Корзину (если можно) и пустую папку."""
        files = self._job_files(job)
        try:
            from send2trash import send2trash
        except Exception:  # noqa: BLE001
            send2trash = None
        for f in files:
            for _ in range(10):  # файл мог ещё держать поток записи
                try:
                    if send2trash:
                        send2trash(f)
                    else:
                        os.remove(f)
                    break
                except FileNotFoundError:
                    break
                except OSError:
                    time.sleep(0.3)
        try:
            if os.path.isdir(job["out_dir"]) and not os.listdir(job["out_dir"]):
                os.rmdir(job["out_dir"])
        except OSError:
            pass
        self.q.put(("exp", ("deleted", job)))

    def delete_jobs(self, js):
        if not js:
            messagebox.showinfo(APP_TITLE, "Выделите задания в списке.", parent=self._exp_win)
            return
        files = [f for j in js for f in self._job_files(j)]
        mb = sum(os.path.getsize(f) for f in files if os.path.isfile(f)) / 1048576
        names = "\n".join(f"• {j['title']}" for j in js[:5]) + ("\n…" if len(js) > 5 else "")
        msg = (f"Удалить сохранение в MP3?\n\n{names}\n\n"
               f"Задание будет остановлено и убрано из очереди, а его файлы удалены с компьютера "
               f"(в Корзину): {len([f for f in files if f.endswith('.mp3')])} MP3, {mb:.0f} МБ.\n"
               "Другие файлы в папке не трогаются.")
        if not messagebox.askyesno(APP_TITLE + " — удаление", msg, icon="warning", default="no", parent=self._exp_win):
            return
        self._delete_jobs_now(js)

    def _delete_jobs_now(self, js):
        for j in js:
            if j.get("_taken"):
                j["_cancel"] = j["_remove"] = j["_delete_files"] = True
                j["_paused"] = False
            else:
                with self._exp_lock if hasattr(self, "_exp_lock") else threading.Lock():
                    if j in self._jobs():
                        self._jobs().remove(j)
                threading.Thread(target=self._delete_job_files, args=(j,), daemon=True).start()
        self._save_jobs()
        self._refresh_export_list()
        self.status_var.set("🗑 Удаляю сохранение и его файлы…")

    def history_versions(self):
        """Для готовой книги: ускоренные версии без изменения тембра и/или M4B."""
        sel = self._hist_sel()
        if not sel:
            messagebox.showinfo(APP_TITLE, "Выделите книгу в списке.", parent=self._exp_win)
            return
        h = self._history()[sel[0]]
        files = [os.path.join(h["out_dir"], n) for n in h.get("written", []) if n.lower().endswith(".mp3")
                 and "(×" not in n and os.path.isfile(os.path.join(h["out_dir"], n))]
        if len(files) != 1:
            messagebox.showinfo(APP_TITLE, "Версии ×1.2/×1.5 и M4B делаются из цельного файла. Сначала склейте книгу: "
                                           "«🗜 Уменьшить размер…» → «Склеить в один файл».", parent=self._exp_win)
            return
        w, f = self._dialog("Версии книги", "Mp3")
        ttk.Label(f, text=f"«{h['title']}»", style="Mp3.Head.TLabel").pack(anchor="w")
        vv = {k: tk.BooleanVar(value=k == "1.2") for k in ("1.2", "1.5", "m4b")}
        for k, t in (("1.2", "Версия ×1.2 (тембр не меняется)"), ("1.5", "Версия ×1.5"),
                     ("m4b", "M4B — аудиокнига с главами и обложкой")):
            ttk.Checkbutton(f, text=t, variable=vv[k], style="Mp3.TCheckbutton").pack(anchor="w", pady=1)

        def go():
            job = dict(bitrate=speech.mp3_bitrate(files[0]), m4b=vv["m4b"].get(),
                       speeds=[float(k) for k in ("1.2", "1.5") if vv[k].get()])
            w.destroy()

            def work():
                try:
                    out = speech.post_formats(files[0], job, status=lambda m: self.q.put(("status", m)))
                    h.setdefault("written", []).extend(os.path.basename(x) for x in out)
                    self.q.put(("status", "Готово: " + ", ".join(os.path.basename(x) for x in out)))
                    self.q.put(("exp", ("idle", None)))
                except Exception as e:  # noqa: BLE001
                    speech.log_error(e)
                    self.q.put(("error_ctx", (e, "создании версий книги", {"folder": h["out_dir"], "actions": ["open_folder"]})))

            self.status_var.set("Делаю версии книги…")
            threading.Thread(target=work, daemon=True).start()

        ttk.Button(f, text="⏩ Сделать", style="Mp3.TButton", command=go).pack(anchor="e", pady=(10, 0))

    def history_delete(self):
        sel = sorted(self._hist_sel(), reverse=True)
        if not sel:
            messagebox.showinfo(APP_TITLE, "Выделите запись в списке.", parent=self._exp_win)
            return
        hs = [self._history()[i] for i in sel]
        jobs = [dict(out_dir=h["out_dir"], title=h["title"], id=f"h{i}", written=h.get("written", []),
                     name_base=h.get("name_base") or speech.safe_name("Аудио рассказ - " + h["title"], 100))
                for i, h in zip(sel, hs)]
        files = [f for j in jobs for f in self._job_files(j)]
        mb = sum(os.path.getsize(f) for f in files if os.path.isfile(f)) / 1048576
        if not messagebox.askyesno(APP_TITLE + " — удаление",
                                   f"Удалить с компьютера (в Корзину) готовые MP3: {len(hs)} книг(и), "
                                   f"{len([f for f in files if f.endswith('.mp3')])} файлов, {mb:.0f} МБ?",
                                   icon="warning", default="no", parent=self._exp_win):
            return
        for i in sel:
            del self._history()[i]
        self._save_jobs()
        self.refresh_history()
        for j in jobs:
            threading.Thread(target=self._delete_job_files, args=(j,), daemon=True).start()

    def jobs_move(self, d):
        q = self._jobs()
        ids = [j["id"] for j in self._sel_jobs()]
        idx = sorted(i for i, j in enumerate(q) if j["id"] in ids)
        if not idx or idx[0] + d < 0 or idx[-1] + d >= len(q):
            return
        for i in (idx if d < 0 else reversed(idx)):
            q[i], q[i + d] = q[i + d], q[i]
        self._save_jobs()
        self._refresh_export_list()

    def jobs_remove(self):
        js = self._sel_jobs()
        if not js:
            messagebox.showinfo(APP_TITLE, "Выделите задания в списке.", parent=self._exp_win)
            return
        running = [j for j in js if j.get("_taken")]
        msg = f"Убрать из очереди сохранения: {len(js)}?"
        if running:
            msg += "\n\nИдущие сейчас будут остановлены. Уже готовые MP3-файлы останутся в папке."
        if not messagebox.askyesno(APP_TITLE, msg, parent=self._exp_win):
            return
        for j in js:
            if j.get("_taken"):
                j["_cancel"] = True
                j["_remove"] = True  # уберём, когда поток остановится
            elif j in self._jobs():
                self._jobs().remove(j)
        self._save_jobs()
        self._refresh_export_list()
        self._update_exp_status()

    def jobs_folder(self):
        js = self._sel_jobs() or self._jobs()
        if js:
            os.makedirs(js[0]["out_dir"], exist_ok=True)
            self._open_folder(js[0]["out_dir"])

    def _update_exp_status(self):
        runs = [j for j in self._jobs() if j.get("_taken")]
        if not runs:
            self.exp_status_var.set("")
            return
        pct = sum(j.get("done", 0) / max(1, j.get("total", 1)) for j in runs) / len(runs)
        self.exp_status_var.set(f"💾 MP3: {len(runs)} кн. · {pct * 100:.0f}%")

    def _exp_event(self, kind, val):
        if kind == "start":
            self._exp_row(val)
            self._refresh_export_list()
            self._update_exp_status()
        elif kind == "progress":
            job, d, t, m, rate, chars_left = val
            pct = d / max(1, t)
            r = self._exp_row(job)
            if r:
                r["pb"]["value"] = int(pct * 1000)
                r["pct"].config(text=f"{pct * 100:.0f}%")
                txt = f"{m} · фраза {d} из {t}"
                if rate > 0:
                    rest = chars_left / rate
                    hh, mm = int(rest // 3600), int(rest % 3600 // 60)
                    left = f"{hh} ч {mm:02d} мин" if hh else f"{mm} мин {int(rest % 60):02d} с"
                    txt += f" · осталось ~{left} · ×{rate / 15:.0f} быстрее живой речи"
                    job["_eta"] = left
                if r.get("note"):
                    txt += f"\n{r['note']}"
                r["info"].config(text=txt)
                if len(self._exp_rows) == 1:
                    self._exp_win.title(f"{pct * 100:.0f}% — сохранение в MP3")
                else:
                    self._exp_win.title(f"Сохранение в MP3 — {len(self._exp_rows)} книг")
            self._update_exp_status()
            self._tray_update()
            if d % 50 == 0 or d == t:
                self._refresh_export_list()
        elif kind == "note":
            job, msg = val
            r = self._exp_row(job)
            if r:
                r["note"] = msg
        elif kind == "done":
            job, written, skipped = val
            self._drop_row(job)
            if job.get("legacy"):
                written = self._legacy_rename(job, written)
            self._add_history(job, written, skipped)
            msg = f"Готово: «Аудио рассказ — {job['title']}», файлов: {len(written)}."
            if skipped:
                msg += f" Не озвучено фраз: {len(skipped)} (список — в «Содержание.txt»)."
            self.status_var.set("💾 " + msg)
            self._refresh_export_list()
            self._update_exp_status()
            if not any(j.get("status") in ("wait", "run") for j in self._jobs()):
                if getattr(self, "_exp_win", None) and self._exp_win.winfo_exists():
                    self._exp_win.withdraw()
                self._tray_remove()
                if not job.get("hot") and not job.get("draft") and not job.get("web") and \
                        messagebox.askyesno(APP_TITLE, msg + "\n\nОткрыть папку?"):
                    self._open_folder(job["out_dir"])
        elif kind == "deleted":
            self._drop_row(val)
            self.status_var.set(f"🗑 Сохранение «{val['title']}» удалено вместе с файлами (они в Корзине).")
            self._refresh_export_list()
            self.refresh_history()
            self._update_exp_status()
        elif kind == "stopped":
            self._drop_row(val)
            if not val.get("_delete_files"):
                self.status_var.set(f"Сохранение «{val['title']}» остановлено. Готовые файлы сохранены, продолжить можно позже.")
            self._refresh_export_list()
            self._update_exp_status()
        elif kind == "fail":
            self._drop_row(val)
            self._refresh_export_list()
            self._update_exp_status()
            self.show_error(val.get("_exc") or val.get("error", ""), f"сохранении «{val['title']}» в MP3",
                            {"job": val, "retry": lambda j=val: self._retry_job(j), "folder": val.get("out_dir"),
                             "actions": ["retry", "open_folder"],
                             "note": "Уже готовые файлы сохранены — повтор продолжит с места остановки "
                                     "(или при следующем запуске программа предложит продолжить)."})
        elif kind == "idle":
            self._update_exp_status()
            self._refresh_export_list()

    def _find_legacy_exports(self) -> list:
        """Незаконченные сохранения старой версии (файлы 01.mp3, 02.mp3… без «Содержание.txt»)."""
        import re as _re
        found = []
        known = {os.path.normcase(os.path.abspath(j["out_dir"])) for j in self._jobs()}
        bases = {find_audio_base(p) for p in self._queue()[:100]} | ({find_audio_base(self.cfg.get("last_book"))}
                                                                     if self.cfg.get("last_book") else set())
        books = {book_stem(p): p for p in list(self._queue()) + list(self.cfg.get("recent", [])) if os.path.isfile(p)}
        ignored = set(self.cfg.get("legacy_ignored", []))
        for base in bases:
            if not os.path.isdir(base):
                continue
            for d in os.listdir(base):
                full = os.path.join(base, d)
                if not os.path.isdir(full) or os.path.normcase(full) in known or full in ignored:
                    continue
                names = os.listdir(full)
                mp3 = [n for n in names if _re.fullmatch(r"\d\d\.mp3", n)]
                if mp3 and "Содержание.txt" not in names and d in books:
                    found.append(dict(folder=full, path=books[d], done=len(mp3)))
        return found

    def _legacy_rename(self, job, written):
        """Дочитанное старой версией — в новые имена: «Аудио рассказ - Название - 01.mp3»."""
        try:
            base = speech.safe_name("Аудио рассказ - " + job["title"], 100)
            new_dir = os.path.join(os.path.dirname(job["out_dir"]), self._audio_folder_name(job["path"]))
            if os.path.exists(new_dir):
                return written
            out = []
            for f in sorted(written):
                nn = os.path.basename(f)[:2]
                nf = os.path.join(job["out_dir"], f"{base} - {nn}.mp3")
                os.replace(f, nf)
                out.append(nf)
            toc = os.path.join(job["out_dir"], "Содержание.txt")
            if os.path.isfile(toc):
                with open(toc, encoding="utf-8") as fh:
                    lines = fh.read().splitlines()
                lines = [f"{base} - {ln[:2]}.mp3{ln[6:]}" if len(ln) > 6 and ln[:2].isdigit() and ln[2:6] == ".mp3" else ln
                         for ln in lines]
                with open(toc, "w", encoding="utf-8") as fh:
                    fh.write("Аудио рассказ\n" + "\n".join(lines))
            for n in os.listdir(job["out_dir"]):  # недописанные куски старой версии больше не нужны
                if n.endswith(".part"):
                    os.remove(os.path.join(job["out_dir"], n))
            os.replace(job["out_dir"], new_dir)
            job["out_dir"] = new_dir
            return [os.path.join(new_dir, os.path.basename(f)) for f in out]
        except OSError:
            return written

    def _legacy_job(self, lg) -> dict:
        p = lg["path"]
        mode = self.cfg.get("book_modes", {}).get(os.path.normcase(p)) or self.mode_dict()
        vs = {k: mode.get(k, getattr(VoiceSettings(), k)) for k in VoiceSettings.__dataclass_fields__}
        return dict(id=f"legacy-{time.time():.6f}", path=p, title=self._book_title(p), out_dir=lg["folder"],
                    name_base="", range=None, vs=vs, volume=1.0,  # как у уже готовых файлов старой версии
                    bitrate=int(self.cfg.get("mp3_bitrate", 64)) if str(self.cfg.get("mp3_bitrate", 64)).isdigit() else 64,
                    split_minutes=45, split_chapters=True, voice_name=self._voice_short(vs["voice"]),
                    status="wait", done=0, total=0, legacy=True)

    def check_unfinished_exports(self):
        """При запуске: предложить продолжить прерванное сохранение (в том числе начатое старой версией)."""
        if any(t.is_alive() for t in getattr(self, "_exp_threads", [])):
            return  # сохранение уже идёт — спрашивать нечего
        for lg in self._find_legacy_exports():
            ans = messagebox.askyesnocancel(
                APP_TITLE, f"Найдено незаконченное сохранение в MP3:\n«{self._book_title(lg['path'])}»\n"
                           f"готово файлов: {lg['done']}\nпапка: {lg['folder']}\n\n"
                           "«Да» — докачать с места остановки (готовые файлы останутся)\n"
                           "«Нет» — больше не предлагать\n«Отмена» — спросить в следующий раз")
            if ans:
                lj = self._legacy_job(lg)
                lj["_accepted"] = True
                self._jobs().append(lj)
            elif ans is False:
                self.cfg.set("legacy_ignored", self.cfg.get("legacy_ignored", []) + [lg["folder"]])
        accepted = [j for j in self._jobs() if j.pop("_accepted", False)]
        if accepted:
            self._save_jobs()
        jobs = [j for j in self._jobs() if j not in accepted]
        for j in jobs:  # служебные пометки прошлого запуска не нужны
            for k in [k for k in j if k.startswith("_")]:
                del j[k]
        if not jobs:
            if accepted:
                self._start_export_worker()
                self.show_export_window()
            return
        names = "\n".join(f"• Аудио рассказ — {j['title'][:60]} ({j.get('voice_name', '')})" for j in jobs[:6])
        more = f"\n… и ещё {len(jobs) - 6}" if len(jobs) > 6 else ""
        ans = messagebox.askyesnocancel(
            APP_TITLE, f"Есть незавершённое сохранение в MP3:\n{names}{more}\n\n"
                       "«Да» — продолжить с места остановки\n«Нет» — отменить эти задания\n«Отмена» — спросить в следующий раз")
        if ans is None:
            return
        if ans:
            for j in jobs:
                j["status"] = "wait"
                j.pop("_taken", None)
            self._save_jobs()
            self._start_export_worker()
            self.show_export_window()
        else:
            for j in jobs:
                self._jobs().remove(j)
            self._save_jobs()
        if accepted and not ans:
            self._start_export_worker()
            self.show_export_window()

    # ------------------------------------------------------------------ голос-копия
    def clone_dialog(self):
        import clone
        K = "Voice"
        if not clone.Xtts.available():
            messagebox.showinfo(APP_TITLE, "Для голосов-копий нужен дополнительный компонент.\n"
                                           "Запустите install.bat из папки программы (нужен интернет).")
            return
        w, f = self._dialog("Создать голос-копию", K)
        ttk.Label(f, text="ГОЛОС-КОПИЯ ПО АУДИОЗАПИСИ", style=f"{K}.Head.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(f, style=f"{K}.TLabel", justify="left", text=(
            "Программа послушает отрывок записи (аудиокнигу, лекцию) и сделает голос с таким же тембром.\n"
            "Лучше всего — чистая речь одного человека без музыки, 5–10 минут.")).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(4, 10))
        src = tk.StringVar()
        lib = library_root()
        start_dir = os.path.join(lib, "04_Аудиокниги") if lib else os.path.expanduser("~")
        ttk.Label(f, text="Запись:", style=f"{K}.TLabel").grid(row=2, column=0, sticky="w")
        ttk.Entry(f, textvariable=src, width=60, font=self.f_ui).grid(row=2, column=1, sticky="we", padx=6)
        name = tk.StringVar()

        def browse():
            p = filedialog.askopenfilename(parent=w, initialdir=start_dir, title="Аудиозапись с голосом",
                                           filetypes=[("Аудио", "*.mp3 *.wav *.flac *.ogg"), ("Все файлы", "*.*")])
            if p:
                src.set(p)
                if not name.get():
                    parent = os.path.basename(os.path.dirname(p))
                    name.set(("Диктор «" + parent.split("_")[0] + "»") if "_" in parent else "Мой голос")

        ttk.Button(f, text="Обзор…", style=f"{K}.TButton", command=browse).grid(row=2, column=2)
        ttk.Label(f, text="Название голоса:", style=f"{K}.TLabel").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(f, textvariable=name, width=40, font=self.f_ui).grid(row=3, column=1, sticky="w", padx=6, pady=(8, 0))
        gender = tk.StringVar(value="m")
        g = ttk.Frame(f, style=f"{K}.TFrame")
        g.grid(row=4, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(f, text="Голос:", style=f"{K}.TLabel").grid(row=4, column=0, sticky="w", pady=(6, 0))
        ttk.Radiobutton(g, text="♂ мужской", value="m", variable=gender, style=f"{K}.TRadiobutton").pack(side="left")
        ttk.Radiobutton(g, text="♀ женский", value="f", variable=gender, style=f"{K}.TRadiobutton").pack(side="left", padx=10)
        t = ttk.Frame(f, style=f"{K}.TFrame")
        t.grid(row=5, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(f, text="Отрывок:", style=f"{K}.TLabel").grid(row=5, column=0, sticky="w", pady=(6, 0))
        start_v, mins_v = tk.StringVar(value="3"), tk.StringVar(value="10")
        ttk.Label(t, text="с", style=f"{K}.TLabel").pack(side="left")
        ttk.Spinbox(t, from_=0, to=600, textvariable=start_v, width=5, font=self.f_ui).pack(side="left", padx=4)
        ttk.Label(t, text="минуты, длиной", style=f"{K}.TLabel").pack(side="left")
        ttk.Spinbox(t, from_=1, to=30, textvariable=mins_v, width=4, font=self.f_ui).pack(side="left", padx=4)
        ttk.Label(t, text="мин", style=f"{K}.TLabel").pack(side="left")
        prog = ttk.Label(f, text="", style=f"{K}.Value.TLabel")
        prog.grid(row=6, column=0, columnspan=3, sticky="w", pady=(12, 0))
        btns = ttk.Frame(f, style=f"{K}.TFrame")
        btns.grid(row=7, column=0, columnspan=3, sticky="e", pady=(12, 0))

        def go():
            if not os.path.isfile(src.get()):
                messagebox.showinfo(APP_TITLE, "Выберите файл записи.", parent=w)
                return
            try:
                st, mn = float(start_v.get()), float(mins_v.get())
            except ValueError:
                return
            go_btn.state(["disabled"])
            nm = name.get().strip() or "Мой голос"

            def work():
                try:
                    vid = clone.make_voice(src.get(), nm, st, mn,
                                           progress=lambda m: self.q.put(("clone", ("progress", (prog, m)))),
                                           gender=gender.get())
                    self.q.put(("clone", ("done", (w, vid))))
                except Exception as e:  # noqa: BLE001
                    speech.log_error(e)
                    self.q.put(("clone", ("fail", (w, go_btn, e))))

            threading.Thread(target=work, daemon=True).start()

        go_btn = ttk.Button(btns, text="🎙 Создать голос", style=f"{K}.TButton", command=go)
        go_btn.pack(side="left", padx=4)
        ttk.Button(btns, text="Закрыть", style="Win.TButton", command=w.destroy).pack(side="left")

    def _clone_event(self, kind, val):
        if kind == "progress":
            lbl, m = val
            try:
                lbl.config(text=m)
            except tk.TclError:
                pass
            self.status_var.set(m)
        elif kind == "done":
            w, vid = val
            try:
                w.destroy()
            except tk.TclError:
                pass
            self.kind_var.set("Все голоса")
            self.gender_var.set("all")
            self.fill_voices()
            if vid in self.titles:
                self.voice_var.set(self.titles[vid])
                self._apply_settings_now()
            messagebox.showinfo(APP_TITLE, "Голос-копия готов и выбран в списке «Голос».\nНажмите «🔊 Прослушать».")
        elif kind == "fail":
            w, btn, msg = val
            try:
                btn.state(["!disabled"])
            except tk.TclError:
                pass
            self.show_error(msg, "создании голоса-копии", parent=w)

    def open_audio_folder(self):
        p = find_audio_base(self.book.path) if self.book else (os.path.join(library_root(), AUDIO_FOLDER) if library_root() else APP_DIR)
        os.makedirs(p, exist_ok=True)
        self._open_folder(p)

    @staticmethod
    def _open_folder(p):
        try:
            if sys.platform == "win32":
                os.startfile(p)  # noqa: S606
            else:
                import subprocess
                subprocess.Popen(["xdg-open", p])
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ прочее
    def show_help(self):
        messagebox.showinfo(APP_TITLE, (
            "Пробел — читать / пауза\n"
            "Esc — стоп\n"
            "Двойной щелчок по тексту — читать с этого места\n"
            "Ctrl+→ / Ctrl+← — следующая / предыдущая фраза\n"
            "Ctrl+↓ / Ctrl+↑ — следующая / предыдущая глава\n"
            "Ctrl + / Ctrl − — быстрее / медленнее\n"
            "Ctrl+O — открыть книгу\n"
            "Ctrl+S — сохранить в MP3 для телефона\n\n"
            "Место чтения запоминается для каждой книги.\n"
            "🌐 — онлайн-голоса Microsoft (самые живые, нужен интернет).\n"
            "💾 — офлайн-голоса (работают без интернета).\n"
            "Если выбран онлайн-голос, а интернета нет — программа сама читает\n"
            "голосом из списка «Без интернета» и через минуту снова пробует онлайн."))

    # ================================================================== 📱 доступ с телефона
    def _web_cfg(self) -> dict:
        w = self.cfg.data.setdefault("web", {})
        if not w.get("pin"):
            import web
            w["pin"] = web.new_pin()
        return w

    def web_start(self, quiet=False) -> bool:
        import web
        try:
            if not getattr(self, "web", None):
                self.web = web.WebServer(WebBridge(self), port=int(self._web_cfg().get("port", web.DEFAULT_PORT)))
            if not self.web.running:
                self.web.start()
            return True
        except Exception as e:  # noqa: BLE001
            speech.log_error(e)
            if not quiet:
                self.show_error(e, "запуске доступа с телефона")
            return False

    def web_stop(self):
        if getattr(self, "web", None):
            self.web.stop()
        self._tunnel_url = ""

    def tunnel_start(self):
        if not self.web_start():
            return
        self._tunnel_url = ""
        self._tunnel_err = ""
        self.web.start_tunnel(lambda u: self.q.put(("tunnel_url", u)), lambda m: self.q.put(("tunnel_fail", m)))

    def _web_autostart(self):
        w = self._web_cfg()
        if w.get("on") and self.web_start(quiet=True):
            if w.get("tunnel"):
                self.tunnel_start()

    def _tunnel_event(self, kind, val):
        if kind == "tunnel_url":
            self._tunnel_url = val
            self.status_var.set("📱 Ссылка для телефона из интернета готова: " + val)
            w = self._web_cfg()
            tg = self.cfg.get("telegram", {})
            if w.get("tg_link") and tg.get("token") and tg.get("chat"):
                def send(u=val):
                    try:
                        speech.tg_api(tg["token"], "sendMessage", {
                            "chat_id": tg["chat"], "text": f"📱 Читалка: ссылка для телефона\n{u}\nPIN — в программе."})
                    except Exception as e:  # noqa: BLE001
                        speech.log_error(e)
                threading.Thread(target=send, daemon=True).start()
        else:
            self._tunnel_url = ""
            self._tunnel_err = val
            self.status_var.set("📱 " + val.split("\n")[0])
        cb = getattr(self, "_phone_refresh", None)
        if cb:
            cb()

    def phone_dialog(self):
        import web
        K = "Voice"
        w, f = self._dialog("Доступ с телефона", K)
        wc = self._web_cfg()
        ttk.Label(f, text="📱 АУДИО РАССКАЗ С ТЕЛЕФОНА", style=f"{K}.Head.TLabel").pack(anchor="w")
        ttk.Label(f, style=f"{K}.TLabel", justify="left", text=(
            "Откройте ссылку на телефоне (или наведите камеру на QR-код), введите PIN, загрузите книгу или PDF\n"
            "и нажмите «Создать аудио рассказ». Компьютер озвучит её сам, а готовый MP3 можно будет\n"
            "послушать и скачать там же. Компьютер и Читалка должны быть включены.")).pack(anchor="w", pady=(4, 8))
        on = tk.BooleanVar(value=bool(getattr(self, "web", None) and self.web.running))
        net = tk.BooleanVar(value=bool(getattr(self, "web", None) and self.web.tunnel))
        tgl = tk.BooleanVar(value=bool(wc.get("tg_link")))
        ttk.Checkbutton(f, text="Включить доступ с телефона (и включать при запуске Читалки)", variable=on,
                        style=f"{K}.TCheckbutton", command=lambda: toggle()).pack(anchor="w")
        pinrow = ttk.Frame(f, style=f"{K}.TFrame")
        pinrow.pack(fill="x", pady=(8, 4))
        ttk.Label(pinrow, text="PIN для входа:", style=f"{K}.TLabel").pack(side="left")
        pin_lbl = ttk.Label(pinrow, text=wc["pin"], style=f"{K}.Head.TLabel")
        pin_lbl.pack(side="left", padx=8)

        def new_pin():
            if messagebox.askyesno(APP_TITLE, "Сменить PIN? Телефоны, где уже выполнен вход, нужно будет войти заново.",
                                   parent=w):
                wc["pin"] = web.new_pin()
                wc["tokens"] = []
                self.cfg.save()
                pin_lbl.config(text=wc["pin"])

        ttk.Button(pinrow, text="🔁 Новый PIN", style=f"{K}.TButton", command=new_pin).pack(side="left", padx=6)

        cols = ttk.Frame(f, style=f"{K}.TFrame")
        cols.pack(fill="x", pady=(6, 0))
        boxes = {}
        for i, (key, title) in enumerate((("lan", "🏠 Дома, по Wi-Fi"), ("net", "🌍 Из интернета (Cloudflare)"))):
            c = ttk.Frame(cols, style=f"{K}.TFrame", padding=(0, 0, 18, 0))
            c.grid(row=0, column=i, sticky="n")
            ttk.Label(c, text=title, style=f"{K}.TLabel").pack(anchor="w")
            if key == "net":
                ttk.Checkbutton(c, text="Открыть защищённую ссылку https", variable=net, style=f"{K}.TCheckbutton",
                                command=lambda: toggle_net()).pack(anchor="w")
            url = ttk.Label(c, style=f"{K}.Value.TLabel", wraplength=330, justify="left")
            url.pack(anchor="w", pady=(4, 2))
            img = tk.Label(c, bg=self.PAL[K]["bg"])
            img.pack(anchor="w")
            bb = ttk.Frame(c, style=f"{K}.TFrame")
            bb.pack(anchor="w", pady=(4, 0))
            ttk.Button(bb, text="📋 Копировать", style=f"{K}.TButton",
                       command=lambda u=url: (self.root.clipboard_clear(), self.root.clipboard_append(u.cget("text")),
                                              self.status_var.set("Ссылка скопирована"))
                       ).pack(side="left")
            boxes[key] = (url, img, bb)
        ttk.Button(boxes["net"][2], text="📨 В Telegram", style=f"{K}.TButton",
                   command=lambda: send_tg()).pack(side="left", padx=4)
        ttk.Checkbutton(boxes["net"][0].master, text="Присылать новую ссылку в Telegram при запуске",
                        variable=tgl, style=f"{K}.TCheckbutton",
                        command=lambda: (wc.__setitem__("tg_link", bool(tgl.get())), self.cfg.save())).pack(anchor="w", pady=(4, 0))
        hint = ttk.Label(f, style=f"{K}.TLabel", justify="left", wraplength=720)
        hint.pack(anchor="w", pady=(10, 0))

        def set_qr(lbl, text):
            im = web.qr_image(text, box=5) if text.startswith("http") else None
            if im is None:
                lbl.config(image="", text="")
                lbl.image = None
                return
            from PIL import ImageTk
            ph = ImageTk.PhotoImage(im)
            lbl.config(image=ph)
            lbl.image = ph

        def refresh():
            if not w.winfo_exists():
                self._phone_refresh = None
                return
            running = bool(getattr(self, "web", None) and self.web.running)
            lan = self.web.lan_url() if running else "выключено"
            boxes["lan"][0].config(text=lan)
            set_qr(boxes["lan"][1], lan)
            if not web.cloudflared_exe():
                nt = "нужен компонент «Доступ с телефона из интернета» (Справка → 🧩 Компоненты)"
            elif not (running and self.web.tunnel):
                nt = getattr(self, "_tunnel_err", "") or "выключено"
            else:
                nt = getattr(self, "_tunnel_url", "") or "открываю туннель… (5–20 секунд)"
            boxes["net"][0].config(text=nt)
            set_qr(boxes["net"][1], nt)
            hint.config(text=(
                "Если телефон не открывает ссылку по Wi-Fi: телефон должен быть в той же сети, а Windows при первом "
                "включении спросит про брандмауэр — разрешите доступ в частных сетях. Ссылка из интернета меняется "
                "при каждом запуске туннеля; без PIN по ней ничего не открыть, после 5 неверных PIN вход закрывается "
                "на 10 минут."))

        def toggle():
            wc["on"] = bool(on.get())
            if on.get():
                self.web_start()
            else:
                net.set(False)
                wc["tunnel"] = False
                self.web_stop()
            self.cfg.save()
            refresh()

        def toggle_net():
            if net.get() and not web.cloudflared_exe():
                net.set(False)
                if messagebox.askyesno(APP_TITLE, "Для ссылки из интернета нужен компонент cloudflared (≈ 60 МБ).\n"
                                                  "Установить сейчас?", parent=w):
                    self.components_dialog(preselect={"phone"})
                return
            wc["tunnel"] = bool(net.get())
            if net.get():
                on.set(True)
                wc["on"] = True
                self.tunnel_start()
            elif getattr(self, "web", None):
                self.web.stop_tunnel()
                self._tunnel_url = ""
                self._tunnel_err = ""
            self.cfg.save()
            refresh()

        def send_tg():
            tg = self.cfg.get("telegram", {})
            u = getattr(self, "_tunnel_url", "")
            if not (tg.get("token") and tg.get("chat")):
                self.telegram_dialog()
                return
            if not u:
                messagebox.showinfo(APP_TITLE, "Сначала включите ссылку из интернета и дождитесь адреса.", parent=w)
                return
            try:
                speech.tg_api(tg["token"], "sendMessage", {"chat_id": tg["chat"], "text": f"📱 Читалка: {u}\nPIN: {wc['pin']}"})
                self.status_var.set("Ссылка отправлена в Telegram")
            except Exception as e:  # noqa: BLE001
                self.show_error(e, "отправке ссылки в Telegram", parent=w)

        self._phone_refresh = lambda: self.root.after(0, refresh)
        refresh()
        b = ttk.Frame(f, style=f"{K}.TFrame")
        b.pack(fill="x", pady=(12, 0))
        ttk.Button(b, text="📂 Папка загруженных книг", style=f"{K}.TButton",
                   command=lambda: (os.makedirs(phone_upload_dir(), exist_ok=True),
                                    self._open_folder(phone_upload_dir()))).pack(side="left")
        ttk.Button(b, text="Закрыть", style="Win.TButton", command=w.destroy).pack(side="right")

    def _web_job(self, d):
        """Задание с телефона (в главном потоке): книга из «С телефона» → аудио рассказ рядом с другими."""
        path, vid, rate, kbps = d["path"], d["voice"], d["rate"], d["quality"]
        base = self.voice_settings()
        snap = {k: getattr(base, k) for k in VoiceSettings.__dataclass_fields__}
        fav = next((x for x in self._favs() if "fav:" + x.get("name", "") == vid), None)
        if fav:
            snap.update({k: v for k, v in fav.items() if k in snap})
        else:
            snap.update(voice=vid, voice2="", voice2f="", voice2c="")
        snap["speed"] = round(max(0.5, min(2.5, snap.get("speed", 1.0) * rate if fav else rate)), 2)
        out = find_audio_base(path)
        od = os.path.join(out, self._fit_name(out, self._audio_folder_name(path), 90, reserve=1 + 70 + 9))
        title = self._book_title(path)
        job = dict(id=f"{time.time():.6f}-w", path=path, title=title, out_dir=od,
                   name_base=self._fit_name(od, "Аудио рассказ - " + title, 70, reserve=9),
                   range=None, vs=snap, volume=1.0, bitrate=kbps, size_mb=None, split_minutes=45,
                   split_chapters=True, one_file=True, skip_refs=True, m4b=False, speeds=[], bg_kind="нет",
                   bg_level=0, notes=self.cfg.get("notes_mode", "skip"), web=True,
                   voice_name=self._voice_short(snap["voice"]), status="wait", done=0, total=0)
        added = self.enqueue_export([job], show=False)
        self.status_var.set(f"📱 С телефона: «{title}» — " + ("в очереди озвучки" if added else "уже озвучивается"))

    def _web_cancel(self, jid):
        js = [j for j in self._jobs() if j.get("id") == jid and j.get("web")]
        if js:
            self._delete_jobs_now(js)

    # ================================================================== обновления и компоненты
    def _version(self) -> str:
        import updater
        return updater.local_version().get("version", "?")

    def show_about(self):
        import updater
        messagebox.showinfo(APP_TITLE, f"Читалка, версия {self._version()}\n\nЧтение книг вслух и аудио рассказы.\n"
                                       f"Страница программы:\n{updater.PAGE_URL}")

    def _auto_update_check(self):
        if not self.cfg.get("update_auto", True):
            return
        today = time.strftime("%Y-%m-%d")
        if self.cfg.get("update_checked") == today:
            return
        self.cfg.set("update_checked", today)
        self.check_updates(quiet=True)

    def check_updates(self, quiet=False):
        import updater

        def work():
            try:
                info = updater.check()
                self.q.put(("upd", ("found" if info else "none", info, quiet)))
            except Exception as e:  # noqa: BLE001
                self.q.put(("upd", ("err", e, quiet)))
        if not quiet:
            self.status_var.set("🔄 Проверяю обновления…")
        threading.Thread(target=work, daemon=True).start()

    def _upd_event(self, kind, info, quiet):
        if kind == "none":
            if not quiet:
                self.status_var.set(f"У вас последняя версия Читалки ({self._version()}).")
                messagebox.showinfo(APP_TITLE, f"У вас последняя версия ({self._version()}).")
            return
        if kind == "err":
            if not quiet:
                self.show_error(info, "проверке обновлений")
            return
        if kind == "found":
            if quiet and self.cfg.get("update_skip") == info.get("version"):
                return
            notes = "\n".join("• " + n for n in info.get("notes", [])[:12])
            r = messagebox.askyesnocancel(
                APP_TITLE + " — обновление",
                f"Есть новая версия Читалки: {info.get('version')} (у вас {self._version()}).\n\n{notes}\n\n"
                "Обновить сейчас? Настройки, голоса, закладки и очередь сохранятся.\n\n"
                "«Нет» — напомнить завтра, «Отмена» — пропустить эту версию.")
            if r is None:
                self.cfg.set("update_skip", info.get("version"))
            elif r:
                self.download_update()
            return
        if kind == "prog":
            self.status_var.set(f"🔄 Скачиваю обновление… {info}%")
        elif kind == "ready":
            if messagebox.askyesno(APP_TITLE, "Обновление скачано. Перезапустить Читалку сейчас?\n\n"
                                              "Если идёт сохранение в MP3 — оно продолжится после перезапуска с того же места."):
                self.restart(apply_zip=info)
            else:
                self._pending_update = info
                self.status_var.set("🔄 Обновление установится при следующем закрытии Читалки.")

    def download_update(self):
        import updater

        def work():
            try:
                zp = updater.download_update(lambda p: self.q.put(("upd", ("prog", p, True))))
                self.q.put(("upd", ("ready", zp, True)))
            except Exception as e:  # noqa: BLE001
                self.q.put(("upd", ("err", e, False)))
        threading.Thread(target=work, daemon=True).start()

    def restart(self, apply_zip=None):
        """Закрывает программу и открывает снова (по пути — ставит скачанное обновление)."""
        import updater
        if not self.on_close(restart=True):
            return
        try:
            if apply_zip:
                updater.start_apply(apply_zip)
            else:
                updater.start_restart()
        except Exception as e:  # noqa: BLE001
            speech.log_error(e)

    def components_dialog(self, preselect=None):
        import updater
        K = "Win"
        w, f = self._dialog("Компоненты и улучшения", K)
        w.resizable(True, True)
        ttk.Label(f, text="🧩 КОМПОНЕНТЫ И УЛУЧШЕНИЯ", style=f"{K}.Head.TLabel").pack(anchor="w")
        ttk.Label(f, style=f"{K}.TLabel", text="Отметьте, что доустановить. Нужен интернет; уже установленное отмечено ✔.").pack(anchor="w", pady=(2, 8))
        have = updater.detect_installed()
        vars_ = {}
        g = ttk.Frame(f, style=f"{K}.TFrame")
        g.pack(fill="x")
        for i, c in enumerate(updater.components()):
            inst = c["id"] in have
            v = tk.BooleanVar(value=bool(preselect and c["id"] in preselect and not inst))
            vars_[c["id"]] = v
            cb = ttk.Checkbutton(g, text=("✔ " if inst else "") + c["title"], variable=v, style=f"{K}.TCheckbutton")
            cb.grid(row=2 * i, column=0, sticky="w", pady=(4, 0))
            if inst or c.get("required"):
                cb.state(["disabled"])
            ttk.Label(g, text=c.get("size", ""), style=f"{K}.Value.TLabel").grid(row=2 * i, column=1, sticky="e", padx=8)
            ttk.Label(g, text=("установлено · " if inst else "") + c.get("desc", ""), style=f"{K}.TLabel",
                      wraplength=640, justify="left", foreground="#777").grid(row=2 * i + 1, column=0, columnspan=2, sticky="w", padx=(24, 0))
        lbl = ttk.Label(f, style=f"{K}.Value.TLabel")
        lbl.pack(anchor="w", pady=(10, 2))
        pb = ttk.Progressbar(f, maximum=100)
        pb.pack(fill="x")
        log = tk.Text(f, height=9, width=96, font=("Consolas", 9), wrap="word")
        log.pack(fill="both", expand=True, pady=(6, 0))
        b = ttk.Frame(f, style=f"{K}.TFrame")
        b.pack(fill="x", pady=(10, 0))
        go_btn = ttk.Button(b, text="⬇ Установить выбранное", style=f"{K}.TButton")
        go_btn.pack(side="left")
        rs_btn = ttk.Button(b, text="🔄 Перезапустить Читалку", style=f"{K}.TButton", command=lambda: self.restart())
        ttk.Button(b, text="Закрыть", style="Win.TButton", command=w.destroy).pack(side="right")

        def go():
            sel = [c for c, v in vars_.items() if v.get()]
            if not sel:
                messagebox.showinfo(APP_TITLE, "Отметьте хотя бы один компонент.", parent=w)
                return
            if "xtts" in sel and "silero" not in have and "silero" not in sel:
                sel.append("silero")
            go_btn.state(["disabled"])
            lbl.config(text="Устанавливаю…")

            def work():
                try:
                    p = updater.start_components(sel)
                    for raw in p.stdout:
                        self.q.put(("comp", raw.decode("utf-8", "replace").rstrip()))
                    p.wait()
                    self.q.put(("comp", f"@@EXIT {p.returncode}"))
                except Exception as e:  # noqa: BLE001
                    self.q.put(("comp", f"@@EXIT ошибка: {e}"))
            threading.Thread(target=work, daemon=True).start()

        def on_line(line):
            if not w.winfo_exists():
                return
            if line.startswith("@@PCT "):
                pb["value"] = int(line.split()[1])
            elif line.startswith("@@STEP "):
                lbl.config(text=line[7:])
                pb["value"] = 0
                log.insert("end", "▶ " + line[7:] + "\n")
            elif line.startswith("@@OK "):
                log.insert("end", "  ✔ готово\n")
            elif line.startswith("@@FAIL "):
                log.insert("end", "  ✖ не получилось: " + line.split(" ", 2)[-1] + "\n")
            elif line.startswith("@@EXIT"):
                ok = line.strip() == "@@EXIT 0"
                lbl.config(text="✔ Установка завершена. Перезапустите Читалку, чтобы новое появилось в программе."
                           if ok else "Установка закончилась с ошибками — см. журнал ниже. Можно повторить.")
                pb["value"] = 100 if ok else pb["value"]
                go_btn.state(["!disabled"])
                rs_btn.pack(side="left", padx=6)
            elif line.startswith(("@@COMP", "@@END")):
                pass
            else:
                log.insert("end", line + "\n")
            log.see("end")

        self._comp_line = on_line
        go_btn.config(command=go)

    # ================================================================== черновик → чистовик
    def _with_drafts(self, jobs, draft_voice):
        """К каждому заданию — быстрый черновик (офлайн-голосом) впереди; чистовик потом его заменит."""
        out = []
        for j in jobs:
            d = dict(j)
            d["id"] = j["id"] + "-draft"
            d["vs"] = dict(j["vs"], voice=draft_voice, voice2="", voice2f="", voice2c="")
            d["name_base"] = j["name_base"] + " (черновик)"
            d["bitrate"] = min(64, int(j.get("bitrate") or 64))
            d.update(size_mb=None, m4b=False, speeds=[], draft=True,
                     voice_name=self._voice_short(draft_voice) + " · черновик")
            j["replaces"] = d["name_base"]
            out += [d, j]
        return out

    def _after_done(self, job, written):
        """В фоне после готовой книги: убрать черновик, отправить в Telegram, разобрать горячую папку."""
        if job.get("replaces"):
            old = self._job_files(dict(out_dir=job["out_dir"], name_base=job["replaces"]))
            self._trash([f for f in old if not f.endswith("Содержание.txt")])
            self.q.put(("status", f"Чистовик «{job['title']}» готов — черновик убран в Корзину."))
        tg = self.cfg.get("telegram", {})
        if tg.get("auto") and tg.get("token") and tg.get("chat") and not job.get("draft"):
            try:
                speech.tg_send_book(tg["token"], tg["chat"], written, f"Аудио рассказ — {job['title']}",
                                    status=lambda m: self.q.put(("status", m)))
                self.q.put(("status", f"📨 «{job['title']}» отправлена в Telegram."))
            except Exception as e:  # noqa: BLE001
                speech.log_error(e)
                self.q.put(("error_ctx", (e, f"отправке «{job['title']}» в Telegram",
                                          {"retry": lambda j=job, w=written: threading.Thread(
                                              target=self._tg_resend, args=(j, w), daemon=True).start()})))
        if job.get("hot") and not job.get("draft"):
            try:
                done_dir = os.path.join(os.path.dirname(job["path"]), "озвучено")
                os.makedirs(done_dir, exist_ok=True)
                os.replace(job["path"], os.path.join(done_dir, os.path.basename(job["path"])))
            except OSError as e:
                speech.log_error(e)

    def _tg_resend(self, job, written):
        tg = self.cfg.get("telegram", {})
        try:
            speech.tg_send_book(tg["token"], tg["chat"], written, f"Аудио рассказ — {job['title']}",
                                status=lambda m: self.q.put(("status", m)))
            self.q.put(("status", f"📨 «{job['title']}» отправлена в Telegram."))
        except Exception as e:  # noqa: BLE001
            self.q.put(("error_ctx", (e, "повторной отправке в Telegram", {})))

    def _trash(self, paths):
        try:
            from send2trash import send2trash
        except Exception:  # noqa: BLE001
            send2trash = os.remove
        for p in paths:
            try:
                send2trash(p)
            except OSError:
                pass

    # ================================================================== Telegram
    def telegram_dialog(self):
        tg = dict(self.cfg.get("telegram", {}))
        w, f = self._dialog("Отправка в Telegram", "Mp3")
        K = "Mp3"
        ttk.Label(f, text="📨 ГОТОВЫЕ КНИГИ — В TELEGRAM", style=f"{K}.Head.TLabel").pack(anchor="w")
        ttk.Label(f, style=f"{K}.TLabel", justify="left", text=(
            "1. В Telegram откройте @BotFather → /newbot → придумайте имя → получите токен.\n"
            "2. Вставьте токен ниже, затем напишите своему боту любое сообщение (например, /start).\n"
            "3. Нажмите «Найти мой чат». Готовые книги будут приходить вам в этот чат\n"
            "   (большие файлы — частями по 48 МБ: таково ограничение Telegram для ботов).")).pack(anchor="w", pady=(4, 8))
        g = ttk.Frame(f, style=f"{K}.TFrame")
        g.pack(fill="x")
        tok = tk.StringVar(value=tg.get("token", ""))
        chat = tk.StringVar(value=str(tg.get("chat", "")))
        auto = tk.BooleanVar(value=tg.get("auto", False))
        ttk.Label(g, text="Токен бота:", style=f"{K}.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Entry(g, textvariable=tok, width=52, font=self.f_ui, show="•").grid(row=0, column=1, sticky="we", padx=4)
        ttk.Label(g, text="Мой чат:", style=f"{K}.TLabel").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(g, textvariable=chat, width=20, font=self.f_ui).grid(row=1, column=1, sticky="w", padx=4)
        info = ttk.Label(f, style=f"{K}.Value.TLabel")
        ttk.Checkbutton(f, text="Отправлять каждую готовую книгу автоматически", variable=auto,
                        style=f"{K}.TCheckbutton").pack(anchor="w", pady=(6, 0))
        info.pack(anchor="w", pady=(6, 0))

        def find_chat():
            try:
                ups = speech.tg_api(tok.get().strip(), "getUpdates", timeout=20)
                chats = [u.get("message", {}).get("chat", {}) for u in ups if u.get("message")]
                if not chats:
                    info.config(text="Сообщений боту не найдено — напишите ему /start и нажмите снова.")
                    return
                c = chats[-1]
                chat.set(str(c.get("id")))
                info.config(text=f"Найден чат: {c.get('first_name', '')} {c.get('username', '')}".strip())
            except Exception as e:  # noqa: BLE001
                info.config(text=errors.explain(e).title + ": " + speech.err_text(e)[:120])

        def test():
            try:
                speech.tg_api(tok.get().strip(), "sendMessage", {"chat_id": chat.get().strip(),
                                                                "text": "✅ Читалка подключена. Сюда будут приходить книги."})
                info.config(text="Проверочное сообщение отправлено — посмотрите в Telegram.")
            except Exception as e:  # noqa: BLE001
                info.config(text=errors.explain(e).title + ": " + speech.err_text(e)[:120])

        def save():
            self.cfg.set("telegram", dict(token=tok.get().strip(), chat=chat.get().strip(), auto=bool(auto.get())))
            self.cfg.save()
            w.destroy()
            self.status_var.set("Telegram: " + ("книги будут отправляться автоматически." if auto.get() else "настройки сохранены."))

        b = ttk.Frame(f, style=f"{K}.TFrame")
        b.pack(fill="x", pady=(10, 0))
        ttk.Button(b, text="🔎 Найти мой чат", style=f"{K}.TButton", command=find_chat).pack(side="left")
        ttk.Button(b, text="✉ Проверить", style=f"{K}.TButton", command=test).pack(side="left", padx=4)
        ttk.Button(b, text="✔ Сохранить", style=f"{K}.TButton", command=save).pack(side="right")
        ttk.Button(b, text="Отмена", style="Win.TButton", command=w.destroy).pack(side="right", padx=4)

    def history_telegram(self):
        sel = self._hist_sel()
        tg = self.cfg.get("telegram", {})
        if not (tg.get("token") and tg.get("chat")):
            self.telegram_dialog()
            return
        for i in sel:
            h = self._history()[i]
            files = [os.path.join(h["out_dir"], n) for n in h.get("written", [])
                     if os.path.isfile(os.path.join(h["out_dir"], n))]
            if files:
                threading.Thread(target=self._tg_resend, args=(dict(title=h["title"]), files), daemon=True).start()

    # ================================================================== горячая папка
    def hot_dialog(self):
        hot = dict(self.cfg.get("hot", {}))
        lib = library_root() or os.path.expanduser("~")
        w, f = self._dialog("Горячая папка", "Mp3")
        K = "Mp3"
        ttk.Label(f, text="📥 ГОРЯЧАЯ ПАПКА", style=f"{K}.Head.TLabel").pack(anchor="w")
        ttk.Label(f, style=f"{K}.TLabel", justify="left", text=(
            "Положите в эту папку книгу (EPUB, FB2, PDF…) — Читалка сама озвучит её\n"
            "одним цельным файлом и положит MP3 в папку озвучки. Книга потом переедет в «озвучено».\n"
            "Голос и настройки — те, что запомнены кнопкой ниже. Работает, пока Читалка открыта\n"
            "(можно свернуть). Готовые книги могут сразу уходить в Telegram.")).pack(anchor="w", pady=(4, 8))
        on = tk.BooleanVar(value=hot.get("on", False))
        d_in = tk.StringVar(value=hot.get("dir") or os.path.join(lib, "00_Горячая папка"))
        d_out = tk.StringVar(value=hot.get("out") or os.path.join(lib, "05_Озвучка программой"))
        ttk.Checkbutton(f, text="Следить за горячей папкой", variable=on, style=f"{K}.TCheckbutton").pack(anchor="w")
        g = ttk.Frame(f, style=f"{K}.TFrame")
        g.pack(fill="x", pady=(6, 0))
        for r_, (lbl, var) in enumerate((("Горячая папка:", d_in), ("Куда класть MP3:", d_out))):
            ttk.Label(g, text=lbl, style=f"{K}.TLabel").grid(row=r_, column=0, sticky="w", pady=2)
            ttk.Entry(g, textvariable=var, width=60, font=self.f_ui).grid(row=r_, column=1, sticky="we", padx=4)
            ttk.Button(g, text="Обзор…", style=f"{K}.TButton",
                       command=lambda v=var: v.set(filedialog.askdirectory(initialdir=v.get()) or v.get())).grid(row=r_, column=2)
        snap = hot.get("vs")
        lbl_vs = ttk.Label(f, style=f"{K}.Value.TLabel")
        lbl_vs.pack(anchor="w", pady=(8, 0))

        def show():
            v = hot.get("vs")
            lbl_vs.config(text=("Голос: " + self._voice_short(v["voice"]) + f", качество {hot.get('bitrate', 48)} кбит/с")
                          if v else "Голос ещё не запомнен — нажмите «Запомнить текущий голос».")

        def remember():
            vs = self.voice_settings()
            hot["vs"] = {k: getattr(vs, k) for k in VoiceSettings.__dataclass_fields__}
            hot["volume"] = self.vol_var.get() / 100.0
            m = self.cfg.get("mp3_mode", self.RECOMMENDED)
            hot["bitrate"] = int(m) if str(m).isdigit() else 48
            show()

        show()
        del snap
        b = ttk.Frame(f, style=f"{K}.TFrame")
        b.pack(fill="x", pady=(10, 0))
        ttk.Button(b, text="🎙 Запомнить текущий голос и настройки", style=f"{K}.TButton", command=remember).pack(side="left")

        def save():
            if on.get() and not hot.get("vs"):
                remember()
            hot.update(on=bool(on.get()), dir=d_in.get(), out=d_out.get())
            try:
                os.makedirs(hot["dir"], exist_ok=True)
            except OSError as e:
                self.show_error(e, "создании горячей папки", parent=w)
                return
            self.cfg.set("hot", hot)
            self.cfg.save()
            w.destroy()
            self.status_var.set("📥 Горячая папка: " + (f"слежу за «{hot['dir']}»" if hot["on"] else "выключена"))
            if hot["on"]:
                self._hot_scan()

        ttk.Button(b, text="✔ Сохранить", style=f"{K}.TButton", command=save).pack(side="right")
        ttk.Button(b, text="📂 Открыть папку", style=f"{K}.TButton",
                   command=lambda: (os.makedirs(d_in.get(), exist_ok=True), self._open_folder(d_in.get()))).pack(side="right", padx=4)

    def _hot_scan(self):
        """Раз в 20 секунд: новые книги в горячей папке → в очередь сохранения."""
        try:
            hot = self.cfg.get("hot", {})
            if hot.get("on") and hot.get("vs") and os.path.isdir(hot.get("dir", "")):
                seen = self.cfg.data.setdefault("hot_seen", {})
                sizes = getattr(self, "_hot_sizes", {})
                new = []
                for fn in sorted(os.listdir(hot["dir"])):
                    p = os.path.join(hot["dir"], fn)
                    low = fn.lower()
                    if not os.path.isfile(p) or not (low.endswith(tuple(formats.SUPPORTED)) or low.endswith(".fb2.zip")):
                        continue
                    st = os.stat(p)
                    key = f"{os.path.normcase(p)}|{st.st_size}|{int(st.st_mtime)}"
                    if key in seen:
                        continue
                    if sizes.get(p) != st.st_size:  # файл ещё копируется — ждём, пока размер перестанет меняться
                        sizes[p] = st.st_size
                        continue
                    seen[key] = time.strftime("%Y-%m-%d %H:%M")
                    new.append(p)
                self._hot_sizes = sizes
                if new:
                    jobs = []
                    for p in new:
                        od = os.path.join(hot["out"], self._fit_name(hot["out"], self._audio_folder_name(p), 90, 80))
                        jobs.append(dict(
                            id=f"{time.time():.6f}-h{len(jobs)}", path=p, title=self._book_title(p), out_dir=od,
                            name_base=self._fit_name(od, "Аудио рассказ - " + self._book_title(p), 70, reserve=9),
                            range=None, vs=hot["vs"], volume=hot.get("volume", 1.0), bitrate=hot.get("bitrate", 48),
                            size_mb=None, split_minutes=45, split_chapters=True, one_file=True, skip_refs=True,
                            m4b=self.cfg.get("mp3_m4b", False), speeds=[], bg_kind="нет", bg_level=0,
                            notes=self.cfg.get("notes_mode", "skip"), hot=True,
                            voice_name=self._voice_short(hot["vs"]["voice"]), status="wait", done=0, total=0))
                    self.enqueue_export(jobs)
                    self.status_var.set(f"📥 Горячая папка: взял в озвучку — {', '.join(os.path.basename(p) for p in new)}")
                    self.cfg.save()
        except Exception as e:  # noqa: BLE001
            speech.log_error(e)
        finally:
            if getattr(self, "_hot_timer", None):
                self.root.after_cancel(self._hot_timer)
            self._hot_timer = self.root.after(20000, self._hot_scan)

    # ================================================================== поиск по тексту (Ctrl+F)
    def search_open(self):
        if not self.chunks:
            return
        if not self._search_bar.winfo_ismapped():
            self._search_bar.pack(fill="x", side="top", before=self.text)
        self._search_entry.focus_set()
        self._search_entry.select_range(0, "end")

    def search_close(self):
        self._search_bar.pack_forget()
        self.text.tag_remove("found", "1.0", "end")
        self.text.tag_remove("found_cur", "1.0", "end")
        self._found = []

    def _search_run(self, *_):
        q = self._search_var.get().strip()
        self.text.tag_remove("found", "1.0", "end")
        self.text.tag_remove("found_cur", "1.0", "end")
        self._found, self._found_i = [], -1
        if len(q) < 2:
            self._search_lbl.config(text="")
            return
        full = self.text.get("1.0", "end-1c").lower()
        ql, i = q.lower(), 0
        while len(self._found) < 5000:
            i = full.find(ql, i)
            if i < 0:
                break
            self._found.append(i)
            i += len(ql)
        for a in self._found[:2000]:
            self.text.tag_add("found", f"1.0+{a}c", f"1.0+{a + len(ql)}c")
        if not self._found:
            self._search_lbl.config(text="не найдено")
            return
        # ближайшее совпадение после текущего места
        here = self.chunks[self.cur].start if self.chunks else 0
        self._found_i = max(0, bisect.bisect_left(self._found, here) - 1)
        self._search_step(1 if self._found[self._found_i] < here else 0)

    def _search_step(self, d):
        if not self._found:
            self._search_run()
            return
        self._found_i = (self._found_i + d) % len(self._found)
        a = self._found[self._found_i]
        n = len(self._search_var.get().strip())
        self.text.tag_remove("found_cur", "1.0", "end")
        self.text.tag_add("found_cur", f"1.0+{a}c", f"1.0+{a + n}c")
        self._scroll_to(f"1.0+{a}c", f"1.0+{a + n}c")
        self.text.see(f"1.0+{a}c")
        self._search_lbl.config(text=f"{self._found_i + 1} из {len(self._found)}"
                                     + (" (показаны первые 5000)" if len(self._found) >= 5000 else ""))

    def _search_read_here(self):
        if self._found and self._found_i >= 0:
            self.goto(self._idx_for_char(self._found[self._found_i]))
            if not self.reader.running:
                self.toggle_play()

    def _build_search_bar(self, parent):
        bar = ttk.Frame(parent, style="Queue.TFrame", padding=(6, 3))
        ttk.Label(bar, text="🔍 Найти:", style="Queue.TLabel").pack(side="left")
        self._search_var = tk.StringVar()
        e = ttk.Entry(bar, textvariable=self._search_var, width=40, font=self.f_ui)
        e.pack(side="left", padx=4)
        e.bind("<Return>", lambda ev: self._search_step(1))
        e.bind("<Shift-Return>", lambda ev: self._search_step(-1))
        e.bind("<Escape>", lambda ev: self.search_close())
        self._search_var.trace_add("write", lambda *a: self.root.after(250, self._search_run)
                                   if not getattr(self, "_search_pending", False) else None)
        ttk.Button(bar, text="▲", width=3, style="Queue.TButton", command=lambda: self._search_step(-1)).pack(side="left")
        ttk.Button(bar, text="▼", width=3, style="Queue.TButton", command=lambda: self._search_step(1)).pack(side="left", padx=2)
        ttk.Button(bar, text="▶ Читать отсюда", style="Queue.TButton", command=self._search_read_here).pack(side="left", padx=4)
        self._search_lbl = ttk.Label(bar, style="Queue.TLabel")
        self._search_lbl.pack(side="left", padx=6)
        ttk.Button(bar, text="✖", width=3, style="Queue.TButton", command=self.search_close).pack(side="right")
        self._search_entry = e
        self._found, self._found_i = [], -1
        return bar

    # ================================================================== закладки с заметками
    def _bookmarks(self, path=None) -> list:
        path = path or (self.book.path if self.book else "")
        return self.cfg.data.setdefault("bookmarks", {}).setdefault(os.path.normcase(path), [])

    def _bm_mark(self):
        self.text.tag_remove("bm", "1.0", "end")
        if not self.book:
            return
        for b in self._bookmarks():
            i = self._idx_for_char(b["char"])
            c = self.chunks[i]
            self.text.tag_add("bm", f"1.0+{c.start}c", f"1.0+{c.end}c")

    def add_bookmark(self, char=None):
        if not self.chunks:
            return
        if char is None:
            char = self.chunks[self.cur].start
        i = self._idx_for_char(char)
        snippet = self.chunks[i].text[:90]
        note = simpledialog.askstring(APP_TITLE, f"Закладка:\n«{snippet}…»\n\nЗаметка (можно оставить пустой):",
                                      parent=self.root)
        if note is None:
            return
        self._bookmarks().append(dict(char=self.chunks[i].start, note=note.strip(), snippet=snippet,
                                      date=time.strftime("%Y-%m-%d %H:%M"),
                                      pct=round(100 * (i + 1) / max(1, len(self.chunks)))))
        self._bookmarks().sort(key=lambda b: b["char"])
        self.cfg.save()
        self._bm_mark()
        self.status_var.set("🔖 Закладка добавлена" + (f": {note.strip()}" if note.strip() else ""))
        if getattr(self, "_bm_win", None) and self._bm_win.winfo_exists():
            self._bm_fill()

    def show_bookmarks(self):
        if not self.book:
            messagebox.showinfo(APP_TITLE, "Сначала откройте книгу.")
            return
        if getattr(self, "_bm_win", None) and self._bm_win.winfo_exists():
            self._bm_win.deiconify()
            self._bm_win.lift()
            self._bm_fill()
            return
        w, f = self._dialog("Закладки и заметки", "Queue")
        w.wm_transient("")
        w.resizable(True, True)
        self._bm_win = w
        ttk.Label(f, text="🔖 ЗАКЛАДКИ И ЗАМЕТКИ", style="Queue.Head.TLabel").pack(anchor="w")
        self._bm_title = ttk.Label(f, style="Queue.TLabel")
        self._bm_title.pack(anchor="w", pady=(2, 6))
        tv = ttk.Treeview(f, columns=("pct", "note", "snip", "date"), show="headings", height=14)
        for c, t, wd in (("pct", "Место", 70), ("note", "Заметка", 260), ("snip", "Фрагмент текста", 420),
                         ("date", "Дата", 130)):
            tv.heading(c, text=t)
            tv.column(c, width=wd, anchor="w", stretch=c in ("note", "snip"))
        tv.pack(fill="both", expand=True)
        tv.bind("<Double-Button-1>", lambda e: self._bm_go())
        tv.bind("<Delete>", lambda e: self._bm_del())
        self._bm_tv = tv
        b = ttk.Frame(f, style="Queue.TFrame")
        b.pack(fill="x", pady=(8, 0))
        for txt, cmd in (("➡ Перейти", self._bm_go), ("▶ Читать отсюда", lambda: self._bm_go(play=True)),
                         ("✏ Заметка…", self._bm_edit), ("✖ Удалить", self._bm_del),
                         ("🔖 Добавить здесь", lambda: self.add_bookmark())):
            ttk.Button(b, text=txt, style="Queue.TButton", command=cmd).pack(side="left", padx=(0, 4))
        ttk.Button(b, text="Закрыть", style="Win.TButton", command=w.destroy).pack(side="right")
        self._bm_fill()

    def _bm_fill(self):
        tv = self._bm_tv
        tv.delete(*tv.get_children())
        self._bm_title.config(text=f"«{self.book.title}» — {len(self._bookmarks())} шт. "
                                   "Двойной щелчок — перейти. Добавить: Ctrl+B или правой кнопкой по тексту.")
        for i, b in enumerate(self._bookmarks()):
            tv.insert("", "end", iid=str(i), values=(f"{b.get('pct', '')}%", b.get("note", ""),
                                                     b.get("snippet", ""), b.get("date", "")))

    def _bm_sel(self):
        s = self._bm_tv.selection()
        return int(s[0]) if s else None

    def _bm_go(self, play=False):
        i = self._bm_sel()
        if i is None:
            return
        self.goto(self._idx_for_char(self._bookmarks()[i]["char"]))
        if play and not self.reader.running:
            self.toggle_play()

    def _bm_edit(self):
        i = self._bm_sel()
        if i is None:
            return
        b = self._bookmarks()[i]
        note = simpledialog.askstring(APP_TITLE, "Заметка:", initialvalue=b.get("note", ""), parent=self._bm_win)
        if note is not None:
            b["note"] = note.strip()
            self.cfg.save()
            self._bm_fill()

    def _bm_del(self):
        i = self._bm_sel()
        if i is None:
            return
        if messagebox.askyesno(APP_TITLE, "Удалить эту закладку?", parent=self._bm_win):
            del self._bookmarks()[i]
            self.cfg.save()
            self._bm_fill()
            self._bm_mark()

    # ================================================================== меню по правой кнопке
    def _text_menu(self, e):
        if not self.chunks:
            return "break"
        idx = self.text.index(f"@{e.x},{e.y}")
        pos = int(self.text.count("1.0", idx, "chars")[0] or 0) if idx != "1.0" else 0
        try:
            word = self.text.get("sel.first", "sel.last").strip()
        except tk.TclError:
            word = self.text.get(f"{idx} wordstart", f"{idx} wordend").strip()
        word = word.strip(".,;:!?«»\"()[]—–-… ")[:60]
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="▶ Читать отсюда", command=lambda: self.on_double_click(e))
        m.add_command(label="🔖 Закладка здесь…", command=lambda: self.add_bookmark(pos))
        if word and any(ch.isalpha() for ch in word):
            m.add_command(label=f"📖 Как читать «{word}»…", command=lambda: self.pron_dialog(word))
        m.add_separator()
        m.add_command(label="🔍 Найти в тексте…", command=self.search_open)
        try:
            self.text.get("sel.first", "sel.last")
            m.add_command(label="📋 Копировать", command=lambda: self.text.event_generate("<<Copy>>"))
        except tk.TclError:
            pass
        m.tk_popup(e.x_root, e.y_root)
        return "break"

    # ================================================================== словарь произношения
    def pron_dialog(self, word=None):
        if getattr(self, "_pron_win", None) and self._pron_win.winfo_exists():
            self._pron_win.lift()
            if word:
                self._pron_a.set(word)
                self._pron_b.set("")
            return
        w, f = self._dialog("Словарь произношения", "Voice")
        w.resizable(True, True)
        self._pron_win = w
        ttk.Label(f, text="📖 СЛОВАРЬ ПРОИЗНОШЕНИЯ", style="Voice.Head.TLabel").pack(anchor="w")
        ttk.Label(f, style="Voice.TLabel", justify="left", text=(
            "Как читать имена, термины, сокращения. Ударение — знак + перед ударной гласной: Тал+еб, з+амок.\n"
            "Действует сразу при чтении и при сохранении в MP3, для всех голосов.")).pack(anchor="w", pady=(2, 8))
        row = ttk.Frame(f, style="Voice.TFrame")
        row.pack(fill="x")
        self._pron_a = tk.StringVar(value=word or "")
        self._pron_b = tk.StringVar()
        ttk.Label(row, text="Как написано:", style="Voice.TLabel").pack(side="left")
        ea = ttk.Entry(row, textvariable=self._pron_a, width=24, font=self.f_ui)
        ea.pack(side="left", padx=4)
        ttk.Label(row, text="Как читать:", style="Voice.TLabel").pack(side="left", padx=(8, 0))
        eb = ttk.Entry(row, textvariable=self._pron_b, width=28, font=self.f_ui)
        eb.pack(side="left", padx=4)
        ttk.Button(row, text="🔊", width=3, style="Voice.TButton",
                   command=lambda: self.preview_voice(text=self._pron_test_text())).pack(side="left")
        ttk.Button(row, text="✔ Сохранить", style="Voice.TButton", command=self._pron_save).pack(side="left", padx=4)
        eb.bind("<Return>", lambda e: self._pron_save())
        tv = ttk.Treeview(f, columns=("a", "b"), show="headings", height=14)
        tv.heading("a", text="Как написано")
        tv.heading("b", text="Как читать")
        tv.column("a", width=260)
        tv.column("b", width=320)
        tv.pack(fill="both", expand=True, pady=(8, 0))
        tv.bind("<<TreeviewSelect>>", lambda e: self._pron_pick())
        tv.bind("<Delete>", lambda e: self._pron_del())
        self._pron_tv = tv
        b = ttk.Frame(f, style="Voice.TFrame")
        b.pack(fill="x", pady=(8, 0))
        ttk.Button(b, text="✖ Удалить", style="Voice.TButton", command=self._pron_del).pack(side="left")
        ttk.Button(b, text="📄 Открыть файл словаря", style="Voice.TButton",
                   command=lambda: (self._pron_ensure(), self._open_folder(speech.PRON_FILE))).pack(side="left", padx=4)
        ttk.Button(b, text="Закрыть", style="Win.TButton", command=w.destroy).pack(side="right")
        self._pron_fill()
        (eb if word else ea).focus_set()

    def _pron_ensure(self):
        if not os.path.exists(speech.PRON_FILE):
            speech.PRON.save([])

    def _pron_test_text(self):
        a, b = self._pron_a.get().strip(), self._pron_b.get().strip()
        if not a:
            return "Проверка словаря."
        if b:  # слушаем, как будет с новой записью
            return speech.plus_to_stress(b)
        return a

    def _pron_fill(self):
        tv = self._pron_tv
        tv.delete(*tv.get_children())
        for i, (a, b) in enumerate(speech.PRON.entries()):
            tv.insert("", "end", iid=str(i), values=(a, b))

    def _pron_pick(self):
        s = self._pron_tv.selection()
        if s:
            a, b = self._pron_tv.item(s[0], "values")
            self._pron_a.set(a)
            self._pron_b.set(b)

    def _pron_save(self):
        a, b = self._pron_a.get().strip(), self._pron_b.get().strip()
        if not a or not b:
            return
        ent = [(x, y) for x, y in speech.PRON.entries() if x.lower() != a.lower()] + [(a, b)]
        ent.sort(key=lambda t: t[0].lower())
        speech.PRON.save(ent)
        self._pron_fill()
        self.reader.set_settings(self.voice_settings())  # заново озвучить фразы впереди
        self.status_var.set(f"📖 В словаре: {a} → {b}")
        self._pron_a.set("")
        self._pron_b.set("")

    def _pron_del(self):
        s = self._pron_tv.selection()
        if not s:
            return
        a = self._pron_tv.item(s[0], "values")[0]
        speech.PRON.save([(x, y) for x, y in speech.PRON.entries() if x != a])
        self._pron_fill()

    # ================================================================== запомненные фразы (кэш)
    def cache_dialog(self):
        n, size = speech.cache_stats()
        if messagebox.askyesno(APP_TITLE, (
                f"Запомнено озвученных фраз онлайн-голосами: {n}, {size / 1048576:.0f} МБ "
                f"(лимит {speech.CACHE_LIMIT_GB:.0f} ГБ — старое удаляется само).\n\n"
                "Повторное чтение и сохранение этих мест идут мгновенно и без интернета.\n\n"
                "Очистить запомненное?"), icon="question", default="no"):
            speech.cache_clear()
            self.status_var.set("Запомненные фразы очищены.")

    # ================================================================== мультимедийные клавиши
    MEDIA_KEYS = {1: 0xB3, 2: 0xB0, 3: 0xB1, 4: 0xB2}  # пауза, следующий, предыдущий, стоп

    def media_keys(self, on):
        """Клавиши ⏯ ⏭ ⏮ ⏹ клавиатуры управляют Читалкой даже в свёрнутом окне."""
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes
        if not on:
            tid = getattr(self, "_mk_tid", None)
            if tid:
                ctypes.windll.user32.PostThreadMessageW(tid, 0x0012, 0, 0)  # WM_QUIT
                self._mk_tid = None
            return
        if getattr(self, "_mk_tid", None):
            return
        ready = threading.Event()

        def loop():
            u = ctypes.windll.user32
            self._mk_tid = ctypes.windll.kernel32.GetCurrentThreadId()
            ok = [u.RegisterHotKey(None, i, 0x4000, vk) for i, vk in self.MEDIA_KEYS.items()]
            self._mk_ok = sum(1 for x in ok if x)
            ready.set()
            msg = wintypes.MSG()
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == 0x0312:  # WM_HOTKEY
                    self.q.put(("media", int(msg.wParam)))
            for i in self.MEDIA_KEYS:
                u.UnregisterHotKey(None, i)

        threading.Thread(target=loop, daemon=True).start()
        ready.wait(2)

    def _media_key(self, k):
        if k == 1:
            self.toggle_play()
        elif k == 2:
            self.step(5)
        elif k == 3:
            self.step(-5)
        elif k == 4:
            self.stop()

    def _media_toggle(self):
        on = bool(self.mediakeys_var.get())
        self.cfg.set("media_keys", on)
        self.media_keys(on)
        self._save_soon()
        if on and getattr(self, "_mk_ok", 4) < 4:
            self.status_var.set("Часть мультимедийных клавиш уже занята другой программой (например, плеером).")

    # ================================================================== сноски
    def _notes_changed(self):
        self.cfg.set("notes_mode", self.notes_var.get())
        self._save_soon()
        if self.book:
            self.open_book(self.book.path)
        self.status_var.set("Сноски: " + ("читаю коротко сразу после абзаца (FB2, EPUB)"
                                          if self.notes_var.get() == "inline" else "не читаю"))

    # ================================================================== распознавание сканов
    def ocr_ask(self):
        p = filedialog.askopenfilename(title="Скан книги (PDF или DJVU)", filetypes=[("Сканы", "*.pdf *.djvu *.djv")],
                                       initialdir=library_root() or None)
        if p:
            self.ocr_run(os.path.abspath(p))

    def ocr_run(self, path):
        why = formats.ocr_available()
        if why:
            self.show_error(why, "распознавании текста")
            return
        w, f = self._dialog("Распознавание текста", "Queue")
        ttk.Label(f, text="🔎 РАСПОЗНАЮ ТЕКСТ СКАНА", style="Queue.Head.TLabel").pack(anchor="w")
        ttk.Label(f, text=os.path.basename(path), style="Queue.TLabel").pack(anchor="w", pady=(4, 6))
        pb = ttk.Progressbar(f, length=460, maximum=100, style="Blue.Horizontal.TProgressbar")
        pb.pack(fill="x")
        lbl = ttk.Label(f, text="Готовлю страницы…", style="Queue.TLabel")
        lbl.pack(anchor="w", pady=(4, 0))
        ttk.Label(f, style="Queue.TLabel", justify="left", text=(
            "Распознаёт встроенное в Windows распознавание (русский и английский).\n"
            "Один раз для книги — потом она открывается сразу. Сам файл книги не меняется.")).pack(anchor="w", pady=(6, 0))
        stop = {"v": False}
        ttk.Button(f, text="Отмена", style="Win.TButton", command=lambda: stop.update(v=True)).pack(anchor="e", pady=(8, 0))
        t0 = time.time()

        def prog(i, n):  # вызывается из фонового потока — в окно передаём через очередь
            el = time.time() - t0
            left = el / max(1, i) * (n - i)
            self.q.put(("ocr_prog", (pb, lbl, 100 * i / max(1, n),
                                     f"Страница {i} из {n} · осталось ~{int(left // 60)} мин {int(left % 60)} с")))

        def work():
            try:
                res = formats.ocr_book(path, prog, cancel=lambda: stop["v"])
                self.q.put(("ocr_done", (w, path, res, None)))
            except Exception as e:  # noqa: BLE001
                speech.log_error(e)
                self.q.put(("ocr_done", (w, path, "", e)))

        threading.Thread(target=work, daemon=True).start()

    def _ocr_done(self, val):
        w, path, res, err = val
        try:
            w.destroy()
        except tk.TclError:
            pass
        if err:
            self.show_error(err, f"распознавании «{os.path.basename(path)}»", {"path": path})
        elif res:
            self.status_var.set("🔎 Текст распознан — открываю книгу.")
            self.open_book(path)
        else:
            self.status_var.set("Распознавание отменено.")

    # ================================================================== библиотека
    def show_library(self):
        root_dir = library_root()
        if not root_dir:
            messagebox.showinfo(APP_TITLE, "Не нашёл папку «моя библиотека».")
            return
        if getattr(self, "_lib_win", None) and self._lib_win.winfo_exists():
            self._lib_win.deiconify()
            self._lib_win.lift()
            self._lib_fill()
            return
        w, f = self._dialog("Библиотека", "Queue")
        w.wm_transient("")
        w.resizable(True, True)
        w.geometry("1150x720")
        self._lib_win = w
        top = ttk.Frame(f, style="Queue.TFrame")
        top.pack(fill="x")
        ttk.Label(top, text="📚 МОЯ БИБЛИОТЕКА", style="Queue.Head.TLabel").pack(side="left")
        ttk.Label(top, text="   Найти:", style="Queue.TLabel").pack(side="left")
        self._lib_q = tk.StringVar()
        e = ttk.Entry(top, textvariable=self._lib_q, width=28, font=self.f_ui)
        e.pack(side="left", padx=4)
        self._lib_filter = tk.StringVar(value="Все книги")
        cb = ttk.Combobox(top, textvariable=self._lib_filter, state="readonly", width=18, font=self.f_ui,
                          values=["Все книги", "Читаю", "Прочитано", "Не начато", "Озвучено в MP3", "Не озвучено"])
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda ev: self._lib_fill())
        self._lib_q.trace_add("write", lambda *a: self._lib_fill())
        self._lib_count = ttk.Label(top, style="Queue.TLabel")
        self._lib_count.pack(side="right")
        st = ttk.Style()
        st.configure("Lib.Treeview", rowheight=58, font=self.f_ui)
        tv = ttk.Treeview(f, columns=("author", "year", "fmt", "pct", "mp3"), style="Lib.Treeview", height=10)
        tv.heading("#0", text="Книга")
        tv.column("#0", width=480)
        for c, t, wd in (("author", "Автор", 200), ("year", "Год", 60), ("fmt", "Формат", 70),
                         ("pct", "Прочитано", 100), ("mp3", "MP3", 110)):
            tv.heading(c, text=t)
            tv.column(c, width=wd, anchor="w" if c == "author" else "center")
        ys = ttk.Scrollbar(f, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=ys.set)
        ys.pack(side="right", fill="y", pady=(8, 0))
        tv.pack(fill="both", expand=True, pady=(8, 0))
        tv.bind("<Double-Button-1>", lambda ev: self._lib_open())
        tv.bind("<<TreeviewOpen>>", lambda ev: self.root.after(50, self._lib_covers_visible))
        tv.bind("<MouseWheel>", lambda ev: self.root.after(150, self._lib_covers_visible), add="+")
        ys.bind("<ButtonRelease-1>", lambda ev: self.root.after(100, self._lib_covers_visible))
        self._lib_tv = tv
        b = ttk.Frame(f, style="Queue.TFrame")
        b.pack(fill="x", pady=(8, 0))
        for txt, cmd in (("📖 Открыть", self._lib_open), ("➕ В очередь", self._lib_queue),
                         ("💾 В MP3…", self._lib_mp3), ("🎧 Папка MP3", self._lib_mp3_folder),
                         ("📂 Где лежит", self._lib_folder), ("↻ Обновить", lambda: self._lib_scan(force=True))):
            ttk.Button(b, text=txt, style="Queue.TButton", command=cmd).pack(side="left", padx=(0, 4))
        ttk.Button(b, text="Закрыть", style="Win.TButton", command=w.destroy).pack(side="right")
        self._lib_imgs = {}
        self._lib_books = []
        self._lib_scan()

    def _lib_scan(self, force=False):
        root_dir = library_root()
        skip = {"04_аудиокниги", "05_озвучка программой"}
        self._lib_count.config(text="Читаю список книг…")

        def work():
            books = []
            for d, dirs, files in os.walk(root_dir):
                dirs[:] = sorted(x for x in dirs if x.lower() not in skip and not x.startswith("."))
                for fn in sorted(files):
                    low = fn.lower()
                    if low.endswith(tuple(formats.SUPPORTED)) or low.endswith(".fb2.zip"):
                        p = os.path.join(d, fn)
                        rel = os.path.relpath(d, root_dir)
                        genre = rel.split(os.sep)[0] if rel != "." else "(без жанра)"
                        stem = book_stem(p)
                        parts = stem.split("_")
                        title, author, year = parts[0], (parts[1] if len(parts) > 1 else ""), \
                            (parts[2] if len(parts) > 2 else "")
                        books.append(dict(path=p, genre=genre, title=title, author=author, year=year,
                                          fmt=os.path.splitext(fn)[1].lstrip(".").upper()))
            self.q.put(("lib_books", books))

        threading.Thread(target=work, daemon=True).start()

    def _lib_status(self, b):
        key = os.path.normcase(b["path"])
        info = self.cfg.data.get("book_info", {}).get(key, {})
        tot = info.get("total") or 0
        pos = self.cfg.data.get("positions", {}).get(key, 0)
        if info.get("done"):
            pct, st = "✔ прочитано", "done"
        elif tot and pos:
            pct, st = f"{100 * (pos + 1) // tot}%", "reading"
        else:
            pct, st = "", "new"
        mp3 = ""
        d = info.get("mp3_dir")
        cands = [d] if d else []
        base = find_audio_base(b["path"])
        cands += [os.path.join(base, self._audio_folder_name(b["path"])), os.path.join(base, book_stem(b["path"]))]
        for c in cands:
            try:
                if c and os.path.isdir(c) and any(n.lower().endswith(".mp3") for n in os.listdir(c)):
                    mp3 = c
                    break
            except OSError:
                pass
        return pct, st, mp3

    def _lib_fill(self):
        tv = getattr(self, "_lib_tv", None)
        if tv is None or not tv.winfo_exists():
            return
        q = self._lib_q.get().strip().lower()
        flt = self._lib_filter.get()
        opened = {iid for iid in tv.get_children() if tv.item(iid, "open")}
        tv.delete(*tv.get_children())
        groups, shown = {}, 0
        for i, b in enumerate(self._lib_books):
            if q and q not in (b["title"] + " " + b["author"]).lower():
                continue
            pct, st, mp3 = self._lib_status(b)
            b["_mp3"] = mp3
            if (flt == "Читаю" and st != "reading") or (flt == "Прочитано" and st != "done") or \
                    (flt == "Не начато" and st != "new") or (flt == "Озвучено в MP3" and not mp3) or \
                    (flt == "Не озвучено" and mp3):
                continue
            g = b["genre"]
            if g not in groups:
                groups[g] = tv.insert("", "end", iid="g:" + g, text=f"📁 {g}", open=bool(q) or flt != "Все книги"
                                      or ("g:" + g) in opened or len(self._lib_books) < 40)
            img = self._lib_imgs.get(b["path"], "")
            tv.insert(groups[g], "end", iid=str(i), text="  " + b["title"], image=img,
                      values=(b["author"], b["year"], b["fmt"], pct, "🎧 есть" if mp3 else ""))
            shown += 1
        for g, iid in groups.items():
            n = len(tv.get_children(iid))
            tv.item(iid, text=f"📁 {g}  ({n})")
        self._lib_count.config(text=f"Книг: {shown} из {len(self._lib_books)}")
        self.root.after(100, self._lib_covers_visible)

    def _lib_covers_visible(self):
        """Обложки грузим только для видимых строк (и запоминаем маленькие копии)."""
        tv = getattr(self, "_lib_tv", None)
        if tv is None or not tv.winfo_exists():
            return
        need = []
        h = tv.winfo_height()
        y = 0
        while y < h:
            iid = tv.identify_row(y)
            if iid and not iid.startswith("g:"):
                b = self._lib_books[int(iid)]
                if b["path"] not in self._lib_imgs and not b.get("_cov_try"):
                    need.append(b)
            y += 20
        if not need or getattr(self, "_lib_cov_busy", False):
            return
        self._lib_cov_busy = True

        def work():
            try:
                cover_work()
            finally:
                self.q.put(("lib_cover", None))

        def cover_work():
            import hashlib
            import io
            from PIL import Image
            cdir = os.path.join(APP_DIR, "cache", "covers")
            os.makedirs(cdir, exist_ok=True)
            for b in need:
                b["_cov_try"] = True
                try:
                    st = os.stat(b["path"])
                    key = hashlib.sha1(f"{b['path']}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()
                    cp = os.path.join(cdir, key + ".png")
                    if not os.path.isfile(cp):
                        raw = formats.get_cover(b["path"])
                        im = Image.open(io.BytesIO(raw)).convert("RGB") if raw else None
                        if im is None:
                            continue
                        im.thumbnail((40, 54))
                        im.save(cp, "PNG")
                    self.q.put(("lib_cover", (b["path"], cp)))
                except Exception:  # noqa: BLE001
                    continue

        threading.Thread(target=work, daemon=True).start()

    def _lib_cover(self, val):
        if val is None:
            self._lib_cov_busy = False
            self.root.after(200, self._lib_covers_visible)
            return
        path, cp = val
        try:
            img = tk.PhotoImage(file=cp)
        except tk.TclError:
            return
        self._lib_imgs[path] = img
        tv = getattr(self, "_lib_tv", None)
        if tv is not None and tv.winfo_exists():
            for i, b in enumerate(self._lib_books):
                if b["path"] == path and tv.exists(str(i)):
                    tv.item(str(i), image=img)

    def _lib_sel(self) -> list:
        return [self._lib_books[int(i)] for i in self._lib_tv.selection() if not i.startswith("g:")]

    def _lib_open(self):
        s = self._lib_sel()
        if s:
            self.add_to_queue([b["path"] for b in s])
            self.open_book(s[0]["path"])

    def _lib_queue(self):
        s = self._lib_sel()
        if s:
            self.add_to_queue([b["path"] for b in s])
            self.status_var.set(f"Добавлено в очередь: {len(s)}")

    def _lib_mp3(self):
        s = self._lib_sel()
        if s:
            self.export_dialog([b["path"] for b in s]) if len(s) > 1 else (
                self.open_book(s[0]["path"]), self.root.after(1500, self.export_dialog))

    def _lib_mp3_folder(self):
        s = self._lib_sel()
        if s and s[0].get("_mp3"):
            self._open_folder(s[0]["_mp3"])
        elif s:
            messagebox.showinfo(APP_TITLE, "Эта книга ещё не озвучена в MP3.", parent=self._lib_win)

    def _lib_folder(self):
        s = self._lib_sel()
        if s:
            self._open_folder(os.path.dirname(s[0]["path"]))

    # ------------------------------------------------------------------ значок в трее (окно сохранения MP3)
    def _exp_minimized(self):
        w = getattr(self, "_exp_win", None)
        try:
            if w is not None and w.winfo_exists() and w.state() == "iconic":
                self._to_tray()
        except tk.TclError:
            pass

    def _hide_export_window(self):
        if any(j.get("status") in ("run", "wait") for j in self._jobs()):
            self._to_tray()
        else:
            self._exp_win.withdraw()

    def _tray_image(self, pct=None):
        from PIL import Image, ImageDraw
        im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.ellipse((1, 1, 63, 63), fill=(214, 233, 250, 255), outline=(47, 127, 193, 255), width=3)
        if pct:
            d.pieslice((1, 1, 63, 63), -90, -90 + 360 * min(1.0, pct), fill=(140, 196, 240, 255))
        d.ellipse((16, 16, 48, 48), fill=(255, 255, 255, 255))
        d.rectangle((33, 22, 37, 41), fill=(181, 113, 62, 255))  # нота
        d.polygon([(37, 22), (45, 26), (37, 29)], fill=(181, 113, 62, 255))
        d.ellipse((24, 36, 37, 46), fill=(181, 113, 62, 255))
        return im

    def _to_tray(self):
        """Окно сохранения — в трей. Щелчок по значку у часов возвращает окно."""
        w = getattr(self, "_exp_win", None)
        if getattr(self, "_tray", None) is None:
            try:
                import pystray
                show = pystray.MenuItem("Показать окно сохранения MP3", lambda i, it: self.q.put(("tray", "show")),
                                        default=True)
                main = pystray.MenuItem("Показать Читалку", lambda i, it: self.q.put(("tray", "main")))
                self._tray = pystray.Icon("chitalka_mp3", self._tray_image(self._tray_pct()),
                                          self._tray_title(), pystray.Menu(show, main))
                self._tray.run_detached()
            except Exception as e:  # noqa: BLE001  нет pystray — оставляем окно просто свёрнутым
                speech.log_error(e)
                self._tray = None
                return
        if w is not None:
            w.withdraw()
        self.status_var.set("Окно сохранения MP3 — в трее (значок с нотой у часов). Щелчок по значку — развернуть.")

    def _tray_pct(self):
        js = [j for j in self._jobs() if j.get("status") == "run" and j.get("total")]
        return sum(j["done"] for j in js) / max(1, sum(j["total"] for j in js)) if js else None

    def _tray_title(self):
        p = self._tray_pct()
        n = sum(1 for j in self._jobs() if j.get("status") in ("run", "wait"))
        return (f"{p * 100:.0f}% — сохранение в MP3" if p is not None else "Сохранение в MP3") + \
            (f" ({n} книг)" if n > 1 else "") + " · Читалка"

    def _tray_update(self):
        t = getattr(self, "_tray", None)
        if t is None:
            return
        p = self._tray_pct()
        try:
            t.title = self._tray_title()
            step = int((p or 0) * 20)
            if step != getattr(self, "_tray_step", -1):
                self._tray_step = step
                t.icon = self._tray_image(p)
        except Exception:  # noqa: BLE001
            pass

    def _tray_remove(self):
        t, self._tray = getattr(self, "_tray", None), None
        if t is not None:
            try:
                t.stop()
            except Exception:  # noqa: BLE001
                pass

    def _tray_event(self, what):
        if what == "show":
            self._tray_remove()
            self.show_export_window()
            try:
                self._exp_win.state("normal")
                self._exp_win.focus_force()
            except tk.TclError:
                pass
        elif what == "main":
            self.root.deiconify()
            self.root.state("normal") if self.root.state() == "iconic" else None
            self.root.lift()
            self.root.focus_force()

    # ------------------------------------------------------------------ понятные ошибки
    def _tk_exception(self, exc, val, tb):
        """Любая непойманная ошибка в окне — не молча, а с объяснением."""
        try:
            val.__traceback__ = tb
        except Exception:  # noqa: BLE001
            pass
        speech.log_error(val)
        self.show_error(val, "действии в окне")

    def _thread_exception(self, a):
        if a.exc_type in (SystemExit, KeyboardInterrupt) or a.exc_type.__name__ == "Cancelled":
            return
        speech.log_error(a.exc_value)
        self.q.put(("bg_error", a.exc_value))

    def show_error(self, err, context="", ctx=None, parent=None):
        """Окно ошибки: что случилось, почему, что сделать — и кнопки, которые это делают.

        ctx: path — книга; job — задание MP3; retry — что повторить; note — добавка к «Что сделать»."""
        ctx = ctx or {}
        pr = errors.explain(err, context)
        if pr.title == "Операция отменена":
            return
        key = (pr.title, pr.cause)
        now = time.time()
        if self._last_err[0] == key and now - self._last_err[1] < 8:
            self.status_var.set("⚠ " + pr.title)  # та же ошибка только что показана — не заваливаем окнами
            return
        self._last_err = (key, now)
        self.status_var.set("⚠ " + pr.title)

        if isinstance(err, BaseException):
            detail = "".join(traceback.format_exception(type(err), err, err.__traceback__)).strip()
        else:
            detail = str(err)

        actions = []
        for a in list(pr.actions) + list(ctx.get("actions", [])):
            if a in actions:
                continue
            if a in ("remove_book", "locate_book") and not ctx.get("path"):
                continue
            if a == "remove_book" and not any(os.path.normcase(q) == os.path.normcase(ctx["path"])
                                              for q in self._queue()):
                continue
            if a == "retry" and not ctx.get("retry"):
                continue
            if a == "choose_folder" and not (ctx.get("job") or ctx.get("on_folder")):
                continue
            if a == "fewer_parallel" and not ctx.get("job") and self._parallel() == 1:
                continue
            if a == "offline_voice" and not self._offline_voice():
                continue
            if a == "run_install" and not os.path.exists(os.path.join(APP_DIR, "install.bat")):
                continue
            if a == "open_folder" and not ctx.get("folder"):
                continue
            if a == "ocr" and not (ctx.get("path") and ctx["path"].lower().endswith((".pdf", ".djvu", ".djv"))):
                continue
            actions.append(a)
        if os.path.exists(os.path.join(APP_DIR, "error.log")) and "open_log" not in actions:
            actions.append("open_log")

        par = parent if parent is not None and parent.winfo_exists() else self.root
        w = tk.Toplevel(par)
        w.title(f"{APP_TITLE} — что-то пошло не так")
        w.transient(par)
        w.configure(bg="#FFF6F2")
        st = ttk.Style()
        st.configure("Err.TFrame", background="#FFF6F2")
        st.configure("Err.TLabel", background="#FFF6F2", foreground="#3A2A26")
        st.configure("ErrHead.TLabel", background="#F8DDD5", foreground="#8A2F1E", font=self.f_play, padding=(14, 8))
        st.configure("ErrSec.TLabel", background="#FFF6F2", foreground="#B5543E", font=self.f_bold)
        st.configure("Err.TButton", background="#F6D9CF", foreground="#5A2A1E", bordercolor="#E8BBAD",
                     lightcolor="#F9E4DC", darkcolor="#E8BBAD", padding=(10, 3))
        st.map("Err.TButton", background=[("pressed", "#EFC6B8"), ("active", "#F2CDBF")])
        ttk.Label(w, text="⚠  " + pr.title, style="ErrHead.TLabel").pack(fill="x")
        f = ttk.Frame(w, style="Err.TFrame", padding=(16, 10, 16, 12))
        f.pack(fill="both", expand=True)
        wrap = 640
        fix = pr.fix + (("\n" + ctx["note"]) if ctx.get("note") else "")
        for head, body in (("Что случилось", context and f"Ошибка при {context}."),
                           ("Почему", pr.cause), ("Что сделать", fix)):
            if not body:
                continue
            ttk.Label(f, text=head, style="ErrSec.TLabel").pack(anchor="w", pady=(6, 0))
            ttk.Label(f, text=body, style="Err.TLabel", wraplength=wrap, justify="left").pack(anchor="w")

        bf = ttk.Frame(f, style="Err.TFrame")
        bf.pack(fill="x", pady=(14, 0))
        row = None
        for i, a in enumerate(actions):
            if i % 3 == 0:
                row = ttk.Frame(bf, style="Err.TFrame")
                row.pack(fill="x", pady=2)

            def run(a=a):
                w.destroy()
                try:
                    self._do_action(a, ctx)
                except Exception as e2:  # noqa: BLE001
                    speech.log_error(e2)
                    self.show_error(e2, f"выполнении «{errors.ACTION_LABELS.get(a, a)}»")

            ttk.Button(row, text=errors.ACTION_LABELS.get(a, a), style="Err.TButton", command=run).pack(
                side="left", padx=(0, 6))

        det = ttk.Frame(f, style="Err.TFrame")
        txt = tk.Text(det, height=10, width=90, wrap="none", font=("Consolas", 9), bg="#FFFDFB", relief="flat")
        txt.insert("1.0", detail)
        txt.configure(state="disabled")
        txt.pack(fill="both", expand=True)
        low = ttk.Frame(f, style="Err.TFrame")
        low.pack(fill="x", pady=(10, 0))

        def toggle():
            if det.winfo_ismapped():
                det.pack_forget()
                tb.config(text="Подробности ▸")
            else:
                det.pack(fill="both", expand=True, pady=(8, 0), before=low)
                tb.config(text="Подробности ▾")

        def copy():
            w.clipboard_clear()
            w.clipboard_append(f"{pr.title}\n{pr.cause}\n\n{detail}")
            cb.config(text="✓ Скопировано")

        tb = ttk.Button(low, text="Подробности ▸", style="Win.TButton", command=toggle)
        tb.pack(side="left")
        cb = ttk.Button(low, text="📋 Копировать текст ошибки", style="Win.TButton", command=copy)
        cb.pack(side="left", padx=6)
        ttk.Button(low, text="Закрыть", style="Win.TButton", command=w.destroy).pack(side="right")
        w.bind("<Escape>", lambda e: w.destroy())
        w.update_idletasks()
        try:
            x = par.winfo_rootx() + max(0, (par.winfo_width() - w.winfo_width()) // 2)
            y = par.winfo_rooty() + max(0, (par.winfo_height() - w.winfo_height()) // 3)
            w.geometry(f"+{x}+{y}")
        except tk.TclError:
            pass
        w.lift()
        w.focus_force()
        return w

    def _offline_voice(self) -> str:
        fb = self._voice_id(self.fb_var.get())
        if fb:
            return fb
        off = [v for v in getattr(self, "catalog", []) if not v.online]
        ru = [v for v in off if v.lang == "ru"] or off
        return ru[0].id if ru else ""

    def _do_action(self, a, ctx):
        """Выполняет кнопку из окна ошибки."""
        if a == "offline_voice":
            vid = self._offline_voice()
            d = self.mode_dict()
            d["voice"] = vid
            self.apply_mode(d, quiet=True)
            self.status_var.set(f"Включён офлайн-голос: {self._voice_short(vid)} — работает без интернета.")
            if ctx.get("retry"):
                ctx["retry"]()
        elif a == "other_voice":
            self.voice_cb.focus_set()
            self.root.after(100, lambda: self.voice_cb.event_generate("<Down>"))
            self.status_var.set("Выберите другой голос в списке «Голос» и нажмите «Читать».")
        elif a == "choose_folder":
            d = filedialog.askdirectory(title="Куда сохранять MP3", initialdir=library_root() or os.path.expanduser("~"))
            if not d:
                return
            if ctx.get("on_folder"):
                ctx["on_folder"](d)
            elif ctx.get("job"):
                job = ctx["job"]
                job["out_dir"] = os.path.join(d, self._fit_name(d, os.path.basename(job["out_dir"].rstrip("\\/")), 80))
                self._retry_job(job)
                self.status_var.set(f"«{job['title']}» продолжит сохраняться в новую папку: {job['out_dir']}")
        elif a == "remove_book":
            q = self._queue()
            key = os.path.normcase(ctx["path"])
            q[:] = [p for p in q if os.path.normcase(p) != key]
            self.cfg.save()
            self.refresh_queue()
            self.status_var.set("Книга убрана из очереди (сам файл не тронут).")
        elif a == "locate_book":
            old = ctx["path"]
            start = os.path.dirname(old)
            while start and not os.path.isdir(start) and os.path.dirname(start) != start:
                start = os.path.dirname(start)
            exts = " ".join("*" + e for e in formats.SUPPORTED)
            p = filedialog.askopenfilename(title=f"Где теперь «{os.path.basename(old)}»?", initialdir=start or None,
                                           filetypes=[("Книги", exts + " *.zip"), ("Все файлы", "*.*")])
            if not p:
                return
            p = os.path.abspath(p)
            self._relink_book(old, p)
            self.open_book(p)
        elif a == "retry":
            ctx["retry"]()
        elif a == "fewer_parallel":
            self.cfg.set("mp3_parallel", 1)
            if getattr(self, "_par_var", None) is not None:
                self._par_var.set("1")
            self.cfg.save()
            self.status_var.set("Теперь книги сохраняются по одной — так нужно меньше памяти.")
            if ctx.get("job"):
                self._retry_job(ctx["job"])
        elif a == "open_log":
            self._open_folder(os.path.join(APP_DIR, "error.log"))
        elif a == "restart_audio":
            self.restart_audio()
        elif a == "run_install":
            if messagebox.askyesno(APP_TITLE, "Установка докачает недостающие компоненты и голоса (нужен интернет).\n"
                                              "Читалка на время установки закроется. Продолжить?"):
                self._open_folder(os.path.join(APP_DIR, "install.bat"))
                self.on_close()
        elif a == "open_folder":
            self._open_folder(ctx["folder"])
        elif a == "ocr":
            self.ocr_run(ctx["path"])

    def _relink_book(self, old, new):
        """Книгу переместили: переносим её место в очереди, закладку, режим голоса и сведения."""
        ko, kn = os.path.normcase(old), os.path.normcase(new)
        q = self._queue()
        for i, p in enumerate(q):
            if os.path.normcase(p) == ko:
                q[i] = new
        for sect in ("positions", "book_modes", "book_info"):
            d = self.cfg.data.get(sect)
            if isinstance(d, dict) and ko in d and kn not in d:
                d[kn] = d.pop(ko)
        self.cfg.save()
        self.refresh_queue()

    def _retry_job(self, job):
        job["status"] = "wait"
        job.pop("error", None)
        job.pop("_exc", None)
        if job not in self._jobs():
            self._jobs().append(job)
        self._save_jobs()
        self._start_export_worker()
        self._refresh_export_list()

    def restart_audio(self, quiet=False):
        was = self.reader.running and not self.paused
        self.reader.stop()
        ok = self.out.restart()
        if ok:
            self.out.volume = float(self.vol_var.get()) / 100.0
            self.status_var.set("🔊 Звук подключён.")
            if was and self.chunks:
                self.reader.start(self.cur)
                self.play_btn.config(text="⏸  Пауза")
            else:
                self.play_btn.config(text="▶  Читать")
        else:
            self.play_btn.config(text="▶  Читать")
            if not quiet:
                self.show_error(self.out.error, "подключении звука")
        return ok

    def _audio_watch(self):
        """Наушники выдернули / сменилось устройство — переподключаемся сами."""
        try:
            se = getattr(self.cfg, "save_error", None)
            if se is not None and not getattr(self, "_save_err_shown", False):
                self._save_err_shown = True
                self.show_error(se, "сохранении настроек и закладок (settings.json)")
            elif se is None:
                self._save_err_shown = False
            if self.out.stalled():
                if self.restart_audio(quiet=True):
                    self.status_var.set("🔊 Звуковое устройство сменилось — звук переподключён, читаю дальше.")
                else:
                    self._last_err = (None, 0.0)
                    self.show_error(self.out.error, "чтении вслух (звуковое устройство отключилось)")
        finally:
            self.root.after(1500, self._audio_watch)

    def _fit_name(self, parent, name, limit, reserve=0):
        """Имя файла/папки, укороченное так, чтобы полный путь не превысил ограничение Windows (260)."""
        room = 240 - len(os.path.abspath(parent)) - 1 - reserve
        return speech.safe_name(name, max(20, min(limit, room)))

    def _check_out_dir(self, out_dir, need_mb, parent=None) -> bool:
        """Перед сохранением: можно ли писать в папку и хватит ли места."""
        try:
            os.makedirs(out_dir, exist_ok=True)
            test = os.path.join(out_dir, ".проверка_записи.tmp")
            with open(test, "wb") as f:
                f.write(b"1")
            os.remove(test)
        except OSError as e:
            self._last_err = (None, 0.0)
            self.show_error(e, "проверке папки для MP3",
                            {"note": "Выберите другую папку в окне сохранения (кнопка «Обзор…»)."}, parent=parent)
            return False
        try:
            free = shutil.disk_usage(out_dir).free / 1048576
        except OSError:
            return True
        if need_mb and free < need_mb * 1.1 + 50:
            return messagebox.askyesno(
                APP_TITLE, f"На диске свободно {free:.0f} МБ, а MP3 займут примерно {need_mb:.0f} МБ.\n\n"
                           "Места может не хватить. Лучше выбрать папку на другом диске или освободить место.\n"
                           "Всё равно начать сохранение?", parent=parent)
        return True

    def on_close(self, restart=False) -> bool:
        running = any(j.get("status") == "run" for j in self._jobs())
        if running and not restart and not messagebox.askyesno(
                APP_TITLE, "Идёт сохранение в MP3. Выйти?\n\nГотовые файлы сохранятся, а при следующем запуске "
                           "программа предложит продолжить с места остановки."):
            return False
        self._exp_stop = True
        for j in self._jobs():
            j["_cancel"] = True
        self.stop()
        self.cfg.set("geometry", self.root.geometry())
        try:
            if str(self.queue_frame) in [str(x) for x in self.paned.panes()]:
                self.cfg.set("queue_width", self.queue_frame.winfo_width())
        except tk.TclError:
            pass
        self.cfg.set("autoscroll", bool(self.autoscroll_var.get()))
        self._apply_settings_now()
        self.cfg.save()
        self.out.close()
        self._tray_remove()
        self.media_keys(False)
        try:
            self.web_stop()
        except Exception:  # noqa: BLE001
            pass
        self.root.destroy()
        if getattr(self, "_pending_update", None) and not restart:
            try:
                import updater
                updater.start_apply(self._pending_update)
            except Exception as e:  # noqa: BLE001
                speech.log_error(e)
        return True


def phone_upload_dir() -> str:
    lib = library_root()
    return os.path.join(lib, "00_С телефона") if lib else os.path.join(APP_DIR, "С телефона")


class WebBridge:
    """Всё, что веб-серверу (web.py) нужно от программы. Вызывается из потоков сервера:
    здесь только чтение, а изменения — через очередь app.q в главный поток."""
    QUALITIES = [(192, "★ Рекомендовано — 192 кбит/с (как образец)"), (96, "Студийно — 96 кбит/с"),
                 (64, "Отлично для речи — 64 кбит/с, файл меньше"), (48, "Экономно — 48 кбит/с")]

    def __init__(self, app: "App"):
        self.app = app
        self.lock = threading.Lock()

    def _w(self) -> dict:
        return self.app._web_cfg()

    def pin(self) -> str:
        return str(self._w().get("pin", ""))

    def tokens(self) -> set:
        return set(self._w().get("tokens", []))

    def add_token(self, tok):
        with self.lock:
            t = self._w().setdefault("tokens", [])
            t.append(tok)
            del t[:-20]
        self.app.q.put(("web_save", None))

    def log(self, e):
        speech.log_error(e)

    def upload_dir(self) -> str:
        return phone_upload_dir()

    def find_same_upload(self, folder, sha, size):
        for name, (s, n) in list(self._w().get("uploads", {}).items()):
            p = os.path.join(folder, name)
            if s == sha and n == size and os.path.isfile(p) and os.path.getsize(p) == size:
                return p
        return None

    def remember_upload(self, path, sha, size):
        with self.lock:
            u = self._w().setdefault("uploads", {})
            u[os.path.basename(path)] = [sha, size]
            for k in list(u)[:-200]:
                del u[k]
        self.app.q.put(("web_save", None))

    # ------------------------------------------------------------------ что показать на телефоне
    def voices(self) -> list:
        app = self.app
        out = [{"id": "fav:" + f["name"], "title": "⭐ " + f["name"], "group": "Избранное"}
               for f in app._favs() if f.get("name")]
        cat = list(getattr(app, "catalog", []) or [])
        cur = app.cfg.get("voice", speech.DEFAULT_VOICE)
        hidden = set(app.cfg.get("hidden_voices", []))
        for v in sorted(cat, key=lambda v: (v.id != cur, not v.online, v.lang != "ru")):
            if v.id in hidden or (v.lang not in ("ru", "kk", "uk", "en") and v.id != cur):
                continue
            grp = "Онлайн (лучше всего)" if v.online else "Без интернета"
            out.append({"id": v.id, "title": speech.voice_title(v), "group": grp})
        return out

    def _vid_ok(self, vid) -> bool:
        return any(v["id"] == vid for v in self.voices())

    def state(self) -> dict:
        app = self.app
        jobs = []
        for j in list(app._jobs()):
            st = j.get("status")
            if st not in ("wait", "run", "paused", "error", "stopped"):
                continue
            pct = 100 * j.get("done", 0) // max(1, j.get("total", 0) or 1)
            text = {"wait": "ждёт очереди", "run": "озвучивается", "paused": "пауза", "stopped": "остановлено",
                    "error": "ошибка: " + str(j.get("error", ""))[:160]}.get(st, st)
            if j.get("_paused"):
                text = "пауза"
            jobs.append({"id": j.get("id"), "title": j.get("title"), "voice": j.get("voice_name", ""), "pct": pct,
                         "state": text, "eta": j.get("_eta", "") if st == "run" else "", "mine": bool(j.get("web"))})
        ready = []
        for h in reversed(list(app._history())[-40:]):
            files = []
            for i, n in enumerate(h.get("written", [])):
                p = os.path.join(h["out_dir"], n)
                if os.path.isfile(p) and n.lower().endswith((".mp3", ".m4b")):
                    files.append({"n": i, "name": n, "mb": round(os.path.getsize(p) / 1048576, 1), "audio": True})
            if files:
                ready.append({"id": self._hid(h), "title": h["title"], "date": h.get("date", ""),
                              "voice": h.get("voice", ""), "phone": bool(h.get("phone")), "files": files})
            if len(ready) >= 25:
                break
        d = self.upload_dir()
        ups = []
        if os.path.isdir(d):
            for n in os.listdir(d):
                p = os.path.join(d, n)
                if os.path.isfile(p) and not n.endswith(".part"):
                    ups.append((os.path.getmtime(p), {"name": n, "mb": round(os.path.getsize(p) / 1048576, 1)}))
        ups.sort(key=lambda x: -x[0])
        q = app.cfg.get("web", {}).get("quality") or 192
        return {"voices": self.voices(), "default_voice": app.cfg.get("voice", speech.DEFAULT_VOICE),
                "qualities": [{"id": str(k), "title": t} for k, t in self.QUALITIES], "default_quality": q,
                "jobs": jobs, "ready": ready, "uploads": [u for _, u in ups[:60]]}

    @staticmethod
    def _hid(h) -> str:
        import hashlib
        return hashlib.sha1(f"{h.get('date')}|{h.get('out_dir')}|{h.get('name_base')}".encode("utf-8")).hexdigest()[:16]

    def file_path(self, hid, n):
        for h in reversed(list(self.app._history())):
            if self._hid(h) == hid:
                names = h.get("written", [])
                if 0 <= n < len(names) and names[n].lower().endswith((".mp3", ".m4b")):
                    return os.path.join(h["out_dir"], names[n])
                return None
        return None

    def submit(self, d) -> str:
        import web
        name = web.safe_filename(str(d.get("file", "")))
        path = os.path.join(self.upload_dir(), name)
        if not os.path.isfile(path):
            raise ValueError("Книга не найдена — загрузите её ещё раз")
        vid = str(d.get("voice", ""))
        if not self._vid_ok(vid):
            raise ValueError("Такого голоса нет в Читалке")
        rate = float(d.get("rate") or 1.0)
        if not 0.5 <= rate <= 2.0:
            raise ValueError("Неверная скорость")
        kbps = int(d.get("quality") or 192)
        if kbps not in {k for k, _ in self.QUALITIES}:
            raise ValueError("Неверное качество")
        self._w()["quality"] = kbps
        self.app.q.put(("web_job", {"path": path, "voice": vid, "rate": rate, "quality": kbps}))
        return "Книга поставлена в очередь озвучки. Ход работы — ниже."

    def cancel(self, jid):
        self.app.q.put(("web_cancel", jid))


IPC_PORT = 47613  # второй запуск передаёт книгу уже открытой Читалке


def _ipc_send(paths) -> bool:
    """Если Читалка уже запущена — передаём ей файлы и выходим. True — передали."""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", IPC_PORT), timeout=1.5) as c:
            c.sendall(("CHITALKA\n" + "\n".join(os.path.abspath(p) for p in paths) + "\n\n").encode("utf-8"))
            c.settimeout(2)
            return c.recv(16).startswith(b"OK")
    except OSError:
        return False


def _ipc_server(app):
    """Первая копия программы слушает: второй запуск не открывает второе окно (две копии портили бы
    настройки и писали бы MP3 в одни и те же файлы), а передаёт книги сюда."""
    import socket
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        srv.bind(("127.0.0.1", IPC_PORT))
    except OSError:
        srv.close()
        return False
    srv.listen(4)

    def loop():
        while True:
            try:
                c, _ = srv.accept()
                with c:
                    c.settimeout(3)
                    data = b""
                    while not data.endswith(b"\n\n"):
                        part = c.recv(65536)
                        if not part:
                            break
                        data += part
                    lines = data.decode("utf-8", "replace").split("\n")
                    if lines and lines[0] == "CHITALKA":
                        app.q.put(("ipc", [x for x in lines[1:] if x]))
                        c.sendall(b"OK")
            except Exception:  # noqa: BLE001
                time.sleep(0.2)

    threading.Thread(target=loop, daemon=True).start()
    return True


def main():
    files = [a for a in sys.argv[1:] if os.path.isfile(a)]
    if _ipc_send(files):
        return  # Читалка уже открыта — она сама покажет окно и откроет книгу
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
    try:
        from tkinterdnd2 import TkinterDnD  # перетаскивание файлов из Проводника
        root = TkinterDnD.Tk()
    except Exception:  # noqa: BLE001
        root = tk.Tk()
    try:
        app = App(root)
    except Exception as e:  # noqa: BLE001
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass
        _startup_fail(e)
        return
    _ipc_server(app)
    root.mainloop()


if __name__ == "__main__":
    main()
