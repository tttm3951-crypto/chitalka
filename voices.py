"""
Голоса и движки озвучки.

  edge:<ShortName>      — Microsoft Edge (Azure Neural), онлайн, 400+ голосов
  silero:<модель>/<имя> — Silero, офлайн, самая живая русская интонация
  piper:<модель>        — Piper, офлайн, быстрый
  sapi:<id>             — голоса, установленные в Windows, офлайн

Скорость и высота тона работают у всех: где движок не умеет сам —
досчитываем обработкой звука (пересэмплирование + WSOLA).
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from xml.sax.saxutils import escape

import numpy as np

SR = 24000  # частота, на которой всё воспроизводится и сохраняется
JOB = threading.local()  # JOB.valid() → False, если фраза уже не нужна (сменили голос/место)


class Cancelled(Exception):
    pass


def job_valid() -> bool:
    f = getattr(JOB, "valid", None)
    return f() if f else True
APP_DIR = os.path.dirname(os.path.abspath(__file__))
VOICES_DIR = os.path.join(APP_DIR, "voices")
EDGE_CACHE = os.path.join(VOICES_DIR, "edge_voices.json")

DEFAULT_VOICE = "edge:ru-RU-SvetlanaNeural"
DEFAULT_FALLBACK = "silero:auto/eugene"

# ============================================================================ каталог
@dataclass
class VoiceInfo:
    id: str
    label: str
    online: bool
    group: int  # порядок в списке (меньше — выше)
    lang: str = ""
    gender: str = ""  # "m" — мужской, "f" — женский


LOCALE_RU = {
    "ru": "русский", "kk": "казахский", "uk": "украинский", "be": "белорусский", "uz": "узбекский",
    "en": "английский", "de": "немецкий", "fr": "французский", "it": "итальянский", "es": "испанский",
    "pt": "португальский", "pl": "польский", "cs": "чешский", "tr": "турецкий", "zh": "китайский",
    "ja": "японский", "ko": "корейский", "ar": "арабский", "hi": "хинди", "nl": "нидерландский",
    "sv": "шведский", "fi": "финский", "el": "греческий", "he": "иврит", "az": "азербайджанский",
    "ka": "грузинский", "hy": "армянский", "ky": "киргизский", "tg": "таджикский", "mn": "монгольский",
}

# Голоса Edge, которые есть всегда (если каталог ещё не загружен из интернета)
EDGE_BUILTIN = [
    ("ru-RU-SvetlanaNeural", "Female"), ("ru-RU-DmitryNeural", "Male"),
    ("en-US-AndrewMultilingualNeural", "Male"), ("en-US-BrianMultilingualNeural", "Male"),
    ("en-US-AvaMultilingualNeural", "Female"), ("en-US-EmmaMultilingualNeural", "Female"),
    ("en-AU-WilliamMultilingualNeural", "Male"),
    ("fr-FR-RemyMultilingualNeural", "Male"),
    ("fr-FR-VivienneMultilingualNeural", "Female"), ("it-IT-GiuseppeMultilingualNeural", "Male"),
    ("ko-KR-HyunsuMultilingualNeural", "Male"), ("pt-BR-ThalitaMultilingualNeural", "Female"),
    ("kk-KZ-AigulNeural", "Female"), ("kk-KZ-DauletNeural", "Male"),
    ("uk-UA-PolinaNeural", "Female"), ("uk-UA-OstapNeural", "Male"),
    ("en-US-ChristopherNeural", "Male"), ("en-US-GuyNeural", "Male"), ("en-US-JennyNeural", "Female"),
    ("en-GB-RyanNeural", "Male"), ("en-GB-SoniaNeural", "Female"),
]

RU_NAMES = {
    "Svetlana": "Светлана", "Dmitry": "Дмитрий", "Aigul": "Айгуль", "Daulet": "Даулет",
    "Polina": "Поліна", "Ostap": "Остап",
}
SILERO_GENDER = {"eugene": "m", "aidar": "m", "baya": "f", "kseniya": "f", "xenia": "f"}
PIPER_GENDER = {"irina": "f", "denis": "m", "dmitri": "m", "ruslan": "m", "ryan": "m", "lessac": "f",
                "issai": "m", "ukrainian_tts": "f"}
SILERO_NAMES = {
    "eugene": "Евгений (муж., бархатный)", "aidar": "Айдар (муж.)", "baya": "Бая (жен.)",
    "kseniya": "Ксения (жен.)", "xenia": "Ксюша (жен., мягкий)", "random": "Случайный",
}
PIPER_NAMES = {
    "ru_RU-irina-medium": "Ирина (рус., жен.)", "ru_RU-denis-medium": "Денис (рус., муж.)",
    "ru_RU-dmitri-medium": "Дмитрий (рус., муж.)", "ru_RU-ruslan-medium": "Руслан (рус., муж.)",
    "kk_KZ-issai-high": "Иссай (каз.)", "en_US-lessac-medium": "Lessac (англ.)",
    "en_US-ryan-high": "Ryan (англ., муж.)", "uk_UA-ukrainian_tts-medium": "Український (укр.)",
}


def _gender_ru(g: str) -> str:
    return {"Male": "муж.", "Female": "жен."}.get(g, "")


def _edge_entries():
    try:
        with open(EDGE_CACHE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list) and len(data) > 20:
            return [(v["ShortName"], v.get("Gender", ""), v.get("VoiceTag", {}).get("VoicePersonalities", []))
                    for v in data]
    except Exception:  # noqa: BLE001
        pass
    return [(n, g, []) for n, g in EDGE_BUILTIN]


def refresh_edge_catalog(timeout=15) -> bool:
    """Скачивает полный список голосов Edge (нужен интернет). Возвращает True при успехе."""
    try:
        import edge_tts

        async def run():
            return await asyncio.wait_for(edge_tts.list_voices(), timeout)

        data = asyncio.run(run())
        if data:
            os.makedirs(VOICES_DIR, exist_ok=True)
            with open(EDGE_CACHE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def edge_label(short: str, gender: str, pers=()) -> tuple[str, int, str]:
    loc, _, name = short.partition("-")[0], None, short.split("-", 2)[-1]
    lang = short.split("-")[0]
    region = short.split("-")[1] if short.count("-") >= 2 else ""
    multi = "Multilingual" in name
    base = name.replace("MultilingualNeural", "").replace("Neural", "")
    base = RU_NAMES.get(base, base)
    g = _gender_ru(gender)
    if lang == "ru":
        return f"{base} — русский, {g}", 0, lang
    if multi:
        p = ", ".join(pers[:2]).lower() if pers else ""
        extra = f"; {p}" if p else ""
        return f"{base} — мультиязычный ({lang}-{region}), {g}, читает и по-русски{extra}", 1, lang
    lname = LOCALE_RU.get(lang, lang)
    grp = {"kk": 2, "uk": 3, "be": 3, "en": 4}.get(lang, 6)
    also = ", читает и по-русски (с акцентом)" if lang in ("kk", "uk") else ""
    return f"{base} — {lname} ({region}), {g}{also}", grp, lang


def silero_models() -> list[str]:
    return sorted(glob.glob(os.path.join(VOICES_DIR, "silero", "*.pt")), reverse=True)


def catalog(all_langs=True) -> list[VoiceInfo]:
    out: list[VoiceInfo] = []
    # офлайн: голоса-копии (XTTS)
    try:
        import clone
        for slug, name, gender, kind in clone.list_clones():
            if kind == "studio":
                out.append(VoiceInfo(f"xtts:{slug}", f"{name.replace(' (студийный)', '')} — студийный XTTS, "
                                     f"{'муж.' if gender == 'm' else 'жен.'}", False, -1, "ru", gender))
            else:
                out.append(VoiceInfo(f"xtts:{slug}", f"{name} — копия голоса", False, -2, "ru", gender))
    except Exception:  # noqa: BLE001
        pass
    # офлайн: Silero
    if silero_models():
        for spk in SileroEngine.speakers():
            out.append(VoiceInfo(f"silero:auto/{spk}", f"{SILERO_NAMES.get(spk, spk)} — Silero", False, 0, "ru",
                                 SILERO_GENDER.get(spk, "")))
    # офлайн: Piper
    for p in sorted(glob.glob(os.path.join(VOICES_DIR, "*.onnx"))):
        vid = os.path.splitext(os.path.basename(p))[0]
        if os.path.isfile(p + ".json"):
            lang = vid[:2]
            nm = vid.split("-")[1] if vid.count("-") >= 2 else vid
            out.append(VoiceInfo(f"piper:{vid}", f"{PIPER_NAMES.get(vid, vid)} — Piper", False,
                                 1 if lang == "ru" else 5, lang, PIPER_GENDER.get(nm, "")))
    # офлайн: Windows
    for vid, name, lang, gender in Sapi.list_voices():
        out.append(VoiceInfo(f"sapi:{vid}", f"{name} — Windows", False, 2 if lang == "ru" else 5, lang, gender))
    # онлайн: Edge
    for short, gender, pers in _edge_entries():
        label, grp, lang = edge_label(short, gender, pers)
        if not all_langs and grp > 4:
            continue
        out.append(VoiceInfo(f"edge:{short}", label, True, grp, lang, {"Male": "m", "Female": "f"}.get(gender, "")))
    out.sort(key=lambda v: (v.online, v.group, v.label))
    return out


def voice_title(v: VoiceInfo) -> str:
    return ("🌐 " if v.online else "💾 ") + v.label


# ============================================================================ обработка звука
def _silence(sec: float) -> np.ndarray:
    return np.zeros(int(SR * sec), dtype=np.float32)


def resample(x: np.ndarray, sr_from: float, sr_to: float) -> np.ndarray:
    if abs(sr_from - sr_to) < 1e-6 or len(x) == 0:
        return x.astype(np.float32)
    n = max(1, int(round(len(x) * sr_to / sr_from)))
    xp = np.arange(len(x), dtype=np.float64)
    fp = np.linspace(0, len(x) - 1, num=n)
    return np.interp(fp, xp, x).astype(np.float32)


def wsola(x: np.ndarray, rate: float, sr: int = SR) -> np.ndarray:
    """Изменение темпа без изменения тона (rate > 1 — быстрее)."""
    if abs(rate - 1.0) < 0.02 or len(x) < sr // 10:
        return x.astype(np.float32)
    frame = int(0.030 * sr)
    hop_out = frame // 2
    hop_in = hop_out * rate
    tol = int(0.008 * sr)
    win = np.hanning(frame).astype(np.float32)
    n_frames = int((len(x) - frame) / hop_in) + 1
    if n_frames < 2:
        return resample(x, rate, 1.0)
    xp = np.concatenate([np.zeros(tol, np.float32), x.astype(np.float32), np.zeros(frame + tol, np.float32)])
    out = np.zeros(n_frames * hop_out + frame, np.float32)
    norm = np.zeros_like(out)
    prev = tol
    for k in range(n_frames):
        ideal = int(k * hop_in) + tol
        if k == 0:
            start = ideal
        else:
            templ = xp[prev + hop_out: prev + hop_out + frame]
            region = xp[ideal - tol: ideal + tol + frame]
            if len(region) < frame + 2 * tol or not templ.any():
                start = ideal
            else:
                cc = np.correlate(region, templ, mode="valid")
                start = ideal - tol + int(np.argmax(cc))
        seg = xp[start: start + frame]
        if len(seg) < frame:
            break
        o = k * hop_out
        out[o: o + frame] += seg * win
        norm[o: o + frame] += win
        prev = start
    norm[norm < 1e-3] = 1.0
    return (out / norm)[: int(len(x) / rate)]


def tempo_pitch(x: np.ndarray, sr: int, speed: float, pitch_steps: int) -> np.ndarray:
    """Для движков без своих настроек: приводит к SR, меняет тон и темп."""
    k = 2 ** (pitch_steps * 0.7 / 12.0)  # множитель частоты тона
    y = resample(x, sr * k, SR)  # тон ×k, темп ×k
    return wsola(y, speed / k)


def number_words_ru(text: str) -> str:
    """Числа словами (для Silero, который цифры не читает)."""
    try:
        from num2words import num2words
    except ImportError:
        return text

    def rep(m):
        s = m.group(0)
        try:
            if "," in s or "." in s:
                a, b = re.split(r"[.,]", s, maxsplit=1)
                return num2words(int(a), lang="ru") + " целых " + num2words(int(b), lang="ru")
            if len(s) > 12:
                return " ".join(num2words(int(c), lang="ru") for c in s)
            return num2words(int(s), lang="ru")
        except Exception:  # noqa: BLE001
            return s

    text = re.sub(r"(?<=\d)[  ](?=\d{3}\b)", "", text)
    return re.sub(r"\d+(?:[.,]\d+)?", rep, text)


_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10,
          "XI": 11, "XII": 12, "XIII": 13, "XIV": 14, "XV": 15, "XVI": 16, "XVII": 17, "XVIII": 18,
          "XIX": 19, "XX": 20, "XXI": 21}


_LAT2CYR = str.maketrans("aeopcxyAEOPCXHKMTB", "аеорсхуАЕОРСХНКМТВ")
_MIXED = re.compile(r"\b(?=\w*[а-яёА-ЯЁ])(?=\w*[aeopcxyAEOPCXHKMTB])\w+\b")


_TR = [("shch", "щ"), ("sch", "ш"), ("tch", "ч"), ("ch", "ч"), ("sh", "ш"), ("zh", "ж"), ("kh", "х"), ("ts", "ц"),
       ("ph", "ф"), ("th", "т"), ("ya", "я"), ("yu", "ю"), ("yo", "ё"), ("ee", "и"), ("oo", "у"), ("qu", "кв"),
       ("a", "а"), ("b", "б"), ("c", "к"), ("d", "д"), ("e", "е"), ("f", "ф"), ("g", "г"), ("h", "х"), ("i", "и"),
       ("j", "дж"), ("k", "к"), ("l", "л"), ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"), ("q", "к"), ("r", "р"),
       ("s", "с"), ("t", "т"), ("u", "у"), ("v", "в"), ("w", "в"), ("x", "кс"), ("y", "й"), ("z", "з")]


def latin_to_cyr(text: str) -> str:
    """Латинские слова — русскими буквами (для движков, читающих только кириллицу)."""
    def conv(m):
        w = m.group(0)
        low = w.lower()
        out, i = "", 0
        while i < len(low):
            for a, b in _TR:
                if low.startswith(a, i):
                    out += b
                    i += len(a)
                    break
            else:
                out += low[i]
                i += 1
        return out.capitalize() if w[0].isupper() else out
    return re.sub(r"[A-Za-z]+", conv, text)


def speakable(text: str) -> str:
    # латинские буквы-двойники внутри русских слов (частая беда PDF): «зaмечaтельнoе»
    t = _MIXED.sub(lambda m: m.group(0).translate(_LAT2CYR), text)
    t = re.sub(r"https?://\S+", " ссылка ", t)
    t = re.sub(r"[_*#~=|<>]{2,}", " ", t)
    t = t.replace("…", "...")
    try:
        import textproc
        t = textproc.expand(t)  # «Глава IV» → «Глава четвёртая», «т. д.» → «так далее», формулы
    except Exception:  # noqa: BLE001
        t = re.sub(r"\b(" + "|".join(sorted(_ROMAN, key=len, reverse=True)) + r")(?=\s+(век|веке|века|веков|съезд|глава|часть|том)\b)",
                   lambda m: str(_ROMAN[m.group(1)]), t)
    return t.strip()


# ============================================================================ настройки
@dataclass(frozen=True)
class VoiceSettings:
    voice: str = DEFAULT_VOICE
    fallback: str = DEFAULT_FALLBACK  # офлайн-голос, если нет интернета
    speed: float = 1.0                # 0.5 … 2.5
    pitch: int = 0                    # -10 … +10
    warmth: int = 0                   # бархатность 0 … 10 (подъём низов)
    clarity: int = 0                  # чёткость 0 … 10 (подъём разборчивости)
    reverb: int = 0                   # объём комнаты 0 … 10
    normalize: bool = True            # ровная громкость фраз
    pause: float = 0.15               # пауза между фразами, с (между абзацами — ×2,5)
    voice2: str = ""                  # речь героев (мужчины и все, чей пол не понятен); "" — как у автора
    voice2f: str = ""                 # речь героинь; "" — как у героев
    voice2c: str = ""                 # речь детей; "" — голос героинь чуть выше
    expressive: bool = True           # живые интонации: вопрос — чуть выше, восклицание — живее
    deess: int = 0                    # мягкие «с/ш/щ/ц» 0 … 10 (деэссер)
    compress: int = 0                 # компрессор 0 … 10 (тихое громче, громкое мягче)

    @property
    def dialog(self) -> bool:
        return bool(self.voice2 or self.voice2f or self.voice2c)


def expr_vs(vs: "VoiceSettings", text: str) -> "VoiceSettings":
    """Интонация фразы: вопрос — тон чуть выше, восклицание — выше и живее."""
    if not vs.expressive:
        return vs
    end = text.rstrip(" »\"')”….")[-1:]
    if end == "?":
        from dataclasses import replace
        return replace(vs, pitch=min(10, vs.pitch + 1))
    if end == "!":
        from dataclasses import replace
        return replace(vs, pitch=min(10, vs.pitch + 1), speed=round(vs.speed * 1.04, 2))
    return vs


def role_vs(vs: "VoiceSettings", role: str) -> "VoiceSettings":
    """Настройки для фразы: прямую речь читает голос героя (мужской / женский / детский)."""
    if not role.startswith("speech") or not vs.dialog:
        return vs
    from dataclasses import replace
    g = role[7:]  # m / f / c / ""
    if g == "c":
        v = vs.voice2c or vs.voice2f or vs.voice2 or vs.voice
        return replace(vs, voice=v, pitch=min(10, vs.pitch + (0 if vs.voice2c else 3)))
    if g == "f":
        v = vs.voice2f or vs.voice2 or vs.voice
    else:
        v = vs.voice2 or vs.voice
    return replace(vs, voice=v) if v != vs.voice else vs


# ============================================================================ словарь произношения
PRON_FILE = os.path.join(APP_DIR, "Словарь произношения.txt")
STRESS = "\u0301"  # знак ударения (ставится после ударной гласной)
_VOW = "аеёиоуыэюяАЕЁИОУЫЭЮЯ"
PRON_HEADER = """# Словарь произношения Читалки
# Каждая строка:  как написано в книге = как читать
# Ударение — знак + перед ударной гласной.  Строки с # — комментарии.
# Примеры:
# Талеб = Тал+еб
# СН РК = эс-эн эр-ка
# т. д. = так далее
"""


def plus_to_stress(t: str) -> str:
    """«Тал+еб» → «Тале́б» (знак ударения после гласной)."""
    return re.sub(r"\+([" + _VOW + r"])", lambda m: m.group(1) + STRESS, t)


def stress_to_plus(t: str) -> str:
    return re.sub(r"([" + _VOW + r"])" + STRESS, r"+\1", t)


class Pronunciation:
    """Пользовательский словарь: замены целых слов и фраз (без учёта регистра)."""

    def __init__(self, path=PRON_FILE):
        self.path = path
        self._mtime = None
        self._rx = None
        self._map: dict[str, str] = {}
        self._lock = threading.Lock()

    def entries(self) -> list[tuple[str, str]]:
        out = []
        try:
            with open(self.path, encoding="utf-8-sig") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    a, _, b = line.partition("=")
                    a, b = a.strip(), b.strip()
                    if a:
                        out.append((a, b))
        except FileNotFoundError:
            pass
        return out

    def save(self, entries: list[tuple[str, str]]):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(PRON_HEADER)
            for a, b in entries:
                f.write(f"{a} = {b}\n")
        os.replace(tmp, self.path)
        self._mtime = None

    def _load(self):
        try:
            mt = os.path.getmtime(self.path)
        except OSError:
            mt = -1
        if mt == self._mtime:
            return
        with self._lock:
            self._mtime = mt
            ent = self.entries()
            self._map = {a.lower(): plus_to_stress(b) for a, b in ent}
            if ent:
                keys = sorted(self._map, key=len, reverse=True)
                self._rx = re.compile(r"(?<!\w)(" + "|".join(re.escape(k) for k in keys) + r")(?!\w)", re.I)
            else:
                self._rx = None

    def apply(self, text: str) -> str:
        self._load()
        if self._rx is None:
            return text

        def sub(m):
            w = m.group(0)
            r = self._map.get(w.lower(), w)
            return r[:1].upper() + r[1:] if w[:1].isupper() and r else r

        return self._rx.sub(sub, text)


PRON = Pronunciation()


def stress_for(kind: str, text: str) -> str:
    """Ударения — так, как понимает движок: Silero — «+», Microsoft — знак ударения, остальные — без них."""
    if STRESS not in text:
        return text
    if kind == "silero":
        return stress_to_plus(text)
    if kind == "edge":
        return text
    return text.replace(STRESS, "")


# ============================================================================ улучшение звучания
def _biquad(kind: str, f0: float, gain_db: float, q: float = 0.7, sr: int = SR):
    """Коэффициенты фильтра (RBJ Audio EQ Cookbook)."""
    import math
    A = 10 ** (gain_db / 40)
    w0 = 2 * math.pi * f0 / sr
    cw, sw = math.cos(w0), math.sin(w0)
    alpha = sw / (2 * q)
    if kind == "lowshelf":
        b0 = A * ((A + 1) - (A - 1) * cw + 2 * math.sqrt(A) * alpha)
        b1 = 2 * A * ((A - 1) - (A + 1) * cw)
        b2 = A * ((A + 1) - (A - 1) * cw - 2 * math.sqrt(A) * alpha)
        a0 = (A + 1) + (A - 1) * cw + 2 * math.sqrt(A) * alpha
        a1 = -2 * ((A - 1) + (A + 1) * cw)
        a2 = (A + 1) + (A - 1) * cw - 2 * math.sqrt(A) * alpha
    elif kind == "highshelf":
        b0 = A * ((A + 1) + (A - 1) * cw + 2 * math.sqrt(A) * alpha)
        b1 = -2 * A * ((A - 1) + (A + 1) * cw)
        b2 = A * ((A + 1) + (A - 1) * cw - 2 * math.sqrt(A) * alpha)
        a0 = (A + 1) - (A - 1) * cw + 2 * math.sqrt(A) * alpha
        a1 = 2 * ((A - 1) - (A + 1) * cw)
        a2 = (A + 1) - (A - 1) * cw - 2 * math.sqrt(A) * alpha
    else:  # peak
        b0, b1, b2 = 1 + alpha * A, -2 * cw, 1 - alpha * A
        a0, a1, a2 = 1 + alpha / A, -2 * cw, 1 - alpha / A
    return np.array([b0, b1, b2]) / a0, np.array([1.0, a1 / a0, a2 / a0])


_IR_CACHE: dict[int, np.ndarray] = {}


def _room_ir(level: int) -> np.ndarray:
    if level not in _IR_CACHE:
        rng = np.random.default_rng(7)
        dur = 0.25 + 0.06 * level
        n = int(SR * dur)
        t = np.arange(n) / SR
        ir = rng.standard_normal(n) * np.exp(-t * (6.9 / dur))
        b, a = _biquad("highshelf", 3000, -9)  # тёплое, не звенящее эхо
        from scipy.signal import lfilter
        ir = lfilter(b, a, ir)
        _IR_CACHE[level] = (ir / np.sqrt(np.sum(ir ** 2))).astype(np.float32)
    return _IR_CACHE[level]


def apply_gain(a: np.ndarray, gain: float) -> np.ndarray:
    """Громкость (например 150%) с мягким ограничителем — без хрипа на пиках."""
    if abs(gain - 1.0) < 0.01 or len(a) == 0:
        return a
    y = a * gain
    k = 0.85
    big = np.abs(y) > k
    if np.any(big):
        y[big] = np.sign(y[big]) * (k + (1 - k) * np.tanh((np.abs(y[big]) - k) / (1 - k)))
    return y.astype(np.float32)


_SOS_CACHE: dict = {}


def _eq_sos(warmth: int, clarity: int):
    key = (warmth, clarity)
    if key not in _SOS_CACHE:
        rows = []
        if warmth:
            rows += [_biquad("lowshelf", 220, 0.7 * warmth), _biquad("highshelf", 7500, -0.5 * warmth)]
        if clarity:
            rows += [_biquad("peak", 3200, 0.6 * clarity, q=0.9), _biquad("lowshelf", 120, -0.4 * clarity)]
        _SOS_CACHE[key] = np.array([np.concatenate([b, a]) for b, a in rows], dtype=np.float64) if rows else None
    return _SOS_CACHE[key]


_DEESS_SOS = {}


def _env(x: np.ndarray, ms: float) -> np.ndarray:
    from scipy.signal import lfilter
    a = 1.0 - np.exp(-1.0 / (SR * ms / 1000.0))
    return lfilter([a], [1, a - 1], np.abs(x)).astype(np.float32)


def _deess(x: np.ndarray, amount: int) -> np.ndarray:
    """Деэссер: приглушает резкие свистящие (4,5–11 кГц), только в те мгновения, когда они выпирают."""
    from scipy.signal import butter, sosfilt
    if "hp" not in _DEESS_SOS:
        _DEESS_SOS["hp"] = butter(4, 4500, "highpass", fs=SR, output="sos")
    hi = sosfilt(_DEESS_SOS["hp"], x).astype(np.float32)
    e_hi, e_all = _env(hi, 4), _env(x, 4) + 1e-5
    thr = 0.55 - 0.035 * amount            # доля «свиста» в звуке, выше которой — приглушаем
    ratio = e_hi / e_all
    g = np.where(ratio > thr, np.clip(thr / np.maximum(ratio, 1e-6), 1 - 0.07 * amount, 1.0), 1.0)
    g = _env(g - 1.0, 3) * -1.0             # плавно (без щелчков)
    g = 1.0 + np.clip(-g, -1, 0)
    return (x - hi * (1.0 - g)).astype(np.float32)


def _compress(x: np.ndarray, amount: int) -> np.ndarray:
    """Компрессор: громкие места мягче, тихие слышнее (удобно в транспорте)."""
    env = np.maximum(_env(x, 8), _env(x, 60) * 0.9) + 1e-6
    thr = 10 ** ((-16 - amount * 1.2) / 20)
    ratio = 1.0 + amount * 0.35            # до ~4,5 : 1
    over = env > thr
    g = np.ones_like(x)
    g[over] = (thr * (env[over] / thr) ** (1.0 / ratio)) / env[over]
    g = 1.0 - _env(1.0 - g, 5)              # плавная атака/отпускание
    return (x * g * (1.0 + amount * 0.05)).astype(np.float32)


# ============================================================================ тихий фон
BG_KINDS = ["нет", "дождь", "камин", "мягкий шум"]
_BG = {}


def make_bg(kind: str, seconds: float = 24.0) -> np.ndarray | None:
    """Фон, зацикленный без шва: дождь, треск камина, мягкий (розовый) шум. Уровень ~ −20 дБ."""
    if kind not in BG_KINDS or kind == "нет":
        return None
    if kind in _BG:
        return _BG[kind]
    from scipy.signal import butter, sosfilt
    rng = np.random.default_rng(7)
    n = int(SR * seconds)
    white = rng.standard_normal(n + SR).astype(np.float32)
    if kind == "мягкий шум":
        sos = butter(1, 400, "lowpass", fs=SR, output="sos")
        x = 0.6 * sosfilt(sos, white) + 0.12 * sosfilt(butter(2, 3000, "lowpass", fs=SR, output="sos"), white)
    elif kind == "дождь":
        x = sosfilt(butter(2, [300, 6000], "bandpass", fs=SR, output="sos"), white) * 0.5
        drops = np.zeros(len(white), np.float32)
        for t in rng.integers(0, len(white) - 400, int(seconds * 30)):
            k = np.arange(300)
            drops[t:t + 300] += rng.uniform(0.2, 1.0) * np.exp(-k / 40) * np.sin(k * rng.uniform(0.6, 1.4))
        x = x + 0.35 * sosfilt(butter(2, 1500, "highpass", fs=SR, output="sos"), drops)
    else:  # камин: глухое пламя + потрескивание
        brown = np.cumsum(white) / 300
        brown -= sosfilt(butter(1, 5, "lowpass", fs=SR, output="sos"), brown)
        x = 0.8 * sosfilt(butter(2, 500, "lowpass", fs=SR, output="sos"), brown)
        cr = np.zeros(len(white), np.float32)
        t = 0
        while t < len(white) - 2000:
            t += int(rng.exponential(SR / 5))
            for _ in range(rng.integers(1, 4)):  # треск идёт «пачками»
                tt = min(len(white) - 1500, t + int(rng.integers(0, 600)))
                k = np.arange(1200)
                cr[tt:tt + 1200] += rng.uniform(0.1, 1.0) * np.exp(-k / rng.uniform(15, 120)) * rng.standard_normal(1200)
        x = x + 0.5 * sosfilt(butter(2, [800, 6000], "bandpass", fs=SR, output="sos"), cr)
    x = np.asarray(x, np.float32)
    # зацикливание без шва: последнюю секунду плавно смешиваем с началом
    head, body = x[:SR], x[SR:]
    fade = np.linspace(0, 1, SR, dtype=np.float32)
    body[-SR:] = body[-SR:] * (1 - fade) + head * fade
    body = body / (np.sqrt(np.mean(body ** 2)) + 1e-9) * 0.1   # −20 дБ
    _BG[kind] = body.astype(np.float32)
    return _BG[kind]


def duck_mix(speech: np.ndarray, bg: np.ndarray, pos: int, level: float) -> tuple[np.ndarray, int]:
    """Добавляет фон к речи; пока звучит голос — фон тише (ducking). Возвращает (звук, новая позиция фона)."""
    n = len(speech)
    idx = (np.arange(n) + pos) % len(bg)
    env = _env(speech, 40)
    duck = 1.0 - 0.7 * np.clip(env / 0.04, 0.0, 1.0)
    return (speech + bg[idx] * level * duck).astype(np.float32), (pos + n) % len(bg)


def enhance(a: np.ndarray, vs: "VoiceSettings") -> np.ndarray:
    """Бархатность, чёткость, объём комнаты, выравнивание громкости."""
    if len(a) < 100:
        return a
    try:
        from scipy.signal import lfilter, fftconvolve
    except ImportError:
        return a
    from scipy.signal import sosfilt
    x = a.astype(np.float32)
    sos = _eq_sos(vs.warmth, vs.clarity)  # бархатность (низы +, верх мягче) и чёткость (3 кГц +, бубнение −)
    if sos is not None:
        x = sosfilt(sos, x).astype(np.float32)
    if vs.reverb:
        wet = fftconvolve(x, _room_ir(vs.reverb))[: len(x) + int(0.15 * SR)]
        dry = np.concatenate([x, np.zeros(len(wet) - len(x))])
        mix = 0.035 * vs.reverb
        x = dry * (1 - mix * 0.5) + wet * mix
    if vs.deess:
        x = _deess(x, vs.deess)
    if vs.compress:
        x = _compress(x, vs.compress)
    if vs.normalize:
        loud = np.abs(x) > 0.01
        rms = float(np.sqrt(np.mean(np.square(x[loud])))) if np.any(loud) else 0.0
        if rms > 1e-4:
            x = x * min(4.0, 0.12 / rms)  # ~ −18 дБ
    peak = float(np.max(np.abs(x)))
    if peak > 0.97:  # мягкий ограничитель
        x = np.tanh(x * 1.2) / np.tanh(1.2)
    return x.astype(np.float32)


class OfflineVoiceMissing(Exception):
    pass


# ============================================================================ движки
def err_text(e: BaseException) -> str:
    """Понятный текст ошибки (у некоторых ошибок Python текст пустой)."""
    names = {"MemoryError": "не хватает оперативной памяти — закройте лишние программы",
             "TimeoutError": "сервер голосов не ответил вовремя (медленный интернет)",
             "CancelledError": "запрос к серверу голосов прерван",
             "ClientConnectorError": "нет подключения к интернету"}
    msg = str(e).strip()
    name = type(e).__name__
    return f"{names[name]}" + (f" ({msg})" if msg else "") if name in names else (f"{name}: {msg}" if msg else name)


_EDGE_SLOTS = threading.BoundedSemaphore(3)  # чтение вслух: не больше 3 одновременных запросов к Microsoft
EXPORT_EDGE_SLOTS = threading.BoundedSemaphore(48)  # все сохранения в MP3 вместе — не больше 48 запросов


# ============================================================================ запоминание озвученных фраз
EDGE_CACHE = os.path.join(APP_DIR, "cache", "edge")
CACHE_LIMIT_GB = 3.0
_cache_writes = [0]


def _cache_put(path: str, data: bytes):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{threading.get_ident()}.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        _cache_writes[0] += 1
        if _cache_writes[0] % 2000 == 0:
            threading.Thread(target=cache_prune, daemon=True).start()
    except OSError:
        pass


def cache_stats() -> tuple[int, int]:
    n = size = 0
    for root, _d, files in os.walk(EDGE_CACHE):
        for fn in files:
            if fn.endswith(".mp3"):
                n += 1
                try:
                    size += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    pass
    return n, size


def cache_prune(limit_gb: float | None = None):
    """Держим кэш в пределах лимита: удаляем то, что дольше всего не использовалось."""
    limit = (limit_gb or CACHE_LIMIT_GB) * 1024 ** 3
    items, total = [], 0
    for root, _d, files in os.walk(EDGE_CACHE):
        for fn in files:
            p = os.path.join(root, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if fn.endswith(".tmp") and time.time() - st.st_mtime > 3600:
                try:
                    os.remove(p)
                except OSError:
                    pass
                continue
            items.append((st.st_mtime, st.st_size, p))
            total += st.st_size
    if total <= limit:
        return
    items.sort()
    for _mt, sz, p in items:
        try:
            os.remove(p)
            total -= sz
        except OSError:
            pass
        if total <= limit * 0.9:
            break


def cache_clear():
    import shutil
    shutil.rmtree(EDGE_CACHE, ignore_errors=True)


class EdgeEngine:
    def __init__(self, slots=None):
        # у сохранения в MP3 — свои «каналы», чтобы не мешать чтению вслух
        if isinstance(slots, int):
            self.slots = threading.BoundedSemaphore(slots)
        else:
            self.slots = slots or _EDGE_SLOTS

    def synth(self, text: str, voice: str, speed: float, pitch: int) -> np.ndarray:
        with self.slots:
            return self._synth(text, voice, speed, pitch)

    def _synth(self, text: str, voice: str, speed: float, pitch: int) -> np.ndarray:
        import edge_tts
        import miniaudio

        rate = f"{int(round((speed - 1.0) * 100)):+d}%"
        ptch = f"{int(pitch * 6):+d}Hz"

        async def run() -> bytes:
            com = edge_tts.Communicate(text, voice, rate=rate, pitch=ptch, connect_timeout=10, receive_timeout=60)
            buf = bytearray()
            async for ch in com.stream():
                if ch.get("type") == "audio":
                    buf.extend(ch["data"])
            return bytes(buf)

        import hashlib
        key = hashlib.sha1(f"{voice}|{rate}|{ptch}|{text}".encode("utf-8")).hexdigest()
        cpath = os.path.join(EDGE_CACHE, key[:2], key + ".mp3")
        mp3 = b""
        try:
            with open(cpath, "rb") as f:  # эту фразу уже озвучивали — берём готовый звук, без интернета
                mp3 = f.read()
            os.utime(cpath)
        except OSError:
            pass
        if not mp3:
            mp3 = asyncio.run(run())
            if mp3:
                _cache_put(cpath, mp3)
        if not mp3:
            return _silence(0.2)
        dec = miniaudio.decode(mp3, output_format=miniaudio.SampleFormat.FLOAT32, nchannels=1, sample_rate=SR)
        return np.frombuffer(dec.samples, dtype=np.float32).copy()


def _ascii_dir(src: str) -> str:
    """espeak-ng (внутри Piper) не понимает пути с кириллицей — даём ему путь латиницей."""
    if src.isascii():
        return src
    if sys.platform == "win32":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(1024)
            if ctypes.windll.kernel32.GetShortPathNameW(src, buf, 1024) and buf.value.isascii():
                return buf.value
        except Exception:  # noqa: BLE001
            pass
    import shutil
    import tempfile
    for base in (os.environ.get("PROGRAMDATA", ""), os.environ.get("PUBLIC", ""), tempfile.gettempdir(), "C:\\"):
        if not base or not base.isascii():
            continue
        dst = os.path.join(base, "Chitalka", "espeak-ng-data")
        try:
            if not os.path.isfile(os.path.join(dst, "phontab")):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            return dst
        except OSError:
            continue
    return src


class PiperEngine:
    def __init__(self):
        self._voices = {}
        self._lock = threading.Lock()

    def _get(self, vid: str):
        with self._lock:
            if vid not in self._voices:
                path = os.path.join(VOICES_DIR, vid + ".onnx")
                if not os.path.isfile(path):
                    raise OfflineVoiceMissing(f"Офлайн-голос Piper «{vid}» не скачан. Запустите install.bat.")
                import piper
                from piper import PiperVoice
                kw = {}
                data = os.path.join(os.path.dirname(piper.__file__), "espeak-ng-data")
                if os.path.isdir(data):
                    kw["espeak_data_dir"] = _ascii_dir(data)
                try:
                    self._voices[vid] = PiperVoice.load(path, **kw)
                except TypeError:  # старый piper без espeak_data_dir
                    self._voices[vid] = PiperVoice.load(path)
            return self._voices[vid]

    def synth(self, text: str, vid: str, speed: float, pitch: int) -> np.ndarray:
        voice = self._get(vid)
        k = 2 ** (pitch * 0.7 / 12.0)
        length_scale = (1.0 / max(0.3, speed)) * k
        sr = voice.config.sample_rate
        try:
            from piper import SynthesisConfig
            parts = []
            for ch in voice.synthesize(text, syn_config=SynthesisConfig(length_scale=length_scale)):
                if getattr(ch, "audio_float_array", None) is not None:
                    parts.append(np.asarray(ch.audio_float_array, dtype=np.float32))
                else:
                    parts.append(np.frombuffer(ch.audio_int16_bytes, dtype=np.int16).astype(np.float32) / 32768)
                sr = ch.sample_rate
            audio = np.concatenate(parts) if parts else np.zeros(0, np.float32)
        except ImportError:  # piper-tts < 1.3
            raw = b"".join(voice.synthesize_stream_raw(text, length_scale=length_scale))
            audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768
        return resample(audio, sr * k, SR)


CPU_SHARE = 0.6  # сколько процессора можно занять при сохранении в MP3


def cpu_workers(share: float = CPU_SHARE) -> int:
    return max(1, int((os.cpu_count() or 4) * share))


class SileroEngine:
    """Silero. На видеокарте держим несколько копий модели — они работают параллельно."""
    _models: list = []
    _free: "queue.Queue | None" = None
    _lock = threading.Lock()
    _bytes = None
    copies = 1

    @classmethod
    def _new_model(cls):
        import io
        import torch
        imp = torch.package.PackageImporter(io.BytesIO(cls._bytes))  # torch не открывает пути с кириллицей
        m = imp.load_pickle("tts_models", "model")
        dev = "cuda" if torch.cuda.is_available() else "cpu"  # на видеокарте в разы быстрее
        try:
            m.to(torch.device(dev))
        except Exception:  # noqa: BLE001
            m.to(torch.device("cpu"))
        return m

    @classmethod
    def load(cls, copies: int = 1):
        import queue as _q
        with cls._lock:
            if cls._bytes is None:
                models = silero_models()
                if not models:
                    raise OfflineVoiceMissing("Голоса Silero не скачаны. Запустите install.bat.")
                import torch
                torch.set_num_threads(max(1, min(6, cpu_workers())))
                with open(models[0], "rb") as fh:
                    cls._bytes = fh.read()
                cls._free = _q.Queue()
            while len(cls._models) < copies:
                m = cls._new_model()
                cls._models.append(m)
                cls._free.put(m)
            return cls._models[0]

    @classmethod
    def ensure_copies(cls, n: int):
        """Для сохранения в MP3: n копий на видеокарте (≈150 МБ каждая)."""
        try:
            import torch
            if not torch.cuda.is_available():
                n = 1
        except Exception:  # noqa: BLE001
            n = 1
        cls.load(n)

    @classmethod
    def speakers(cls) -> list[str]:
        known = ["eugene", "aidar", "xenia", "kseniya", "baya"]
        meta = os.path.join(VOICES_DIR, "silero", "speakers.json")
        try:
            with open(meta, encoding="utf-8") as f:
                spk = json.load(f)
            return [s for s in spk if s != "random"] or known
        except Exception:  # noqa: BLE001
            return known

    def synth(self, text: str, speaker: str, speed: float, pitch: int) -> np.ndarray:
        import torch

        self.load()
        text = number_words_ru(text)
        text = latin_to_cyr(text)  # Silero читает только кириллицу
        text = re.sub(r"[^\w\s.,!?;:\-—–«»\"'()…]", " ", text)
        text = re.sub(r"[a-zA-Z0-9_]", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        if not re.search(r"[а-яёА-ЯЁ]", text):
            return _silence(0.3)
        sr = 48000
        model = self._free.get()  # свободная копия модели
        try:
            with torch.no_grad():
                try:
                    audio = model.apply_tts(text=text, speaker=speaker, sample_rate=sr, put_accent=True, put_yo=True)
                except ValueError:
                    # Silero не принял какой-то символ — оставляем только русские буквы и знаки препинания
                    t2 = re.sub(r"[^а-яёА-ЯЁ\s.,!?—-]", " ", text)
                    t2 = re.sub(r"\s+", " ", t2).strip()
                    if not re.search(r"[а-яёА-ЯЁ]", t2):
                        return _silence(0.3)
                    audio = model.apply_tts(text=t2, speaker=speaker, sample_rate=sr, put_accent=True, put_yo=True)
        finally:
            self._free.put(model)
        a = audio.numpy().astype(np.float32) if hasattr(audio, "numpy") else np.asarray(audio, np.float32)
        peak = float(np.abs(a).max()) if len(a) else 0.0
        if peak > 0.9:
            a = a * (0.9 / peak)
        return tempo_pitch(a, sr, speed, pitch)


class Sapi:
    """Голоса Windows (SAPI 5 и OneCore: Irina, Pavel и др.)."""
    _tls = threading.local()
    _cache = None

    @classmethod
    def _com(cls):
        if not getattr(cls._tls, "ok", False):
            import pythoncom
            pythoncom.CoInitialize()
            cls._tls.ok = True
        import win32com.client
        return win32com.client

    @classmethod
    def _tokens(cls):
        wc = cls._com()
        toks = []
        v = wc.Dispatch("SAPI.SpVoice")
        for t in v.GetVoices():
            toks.append(t)
        try:
            cat = wc.Dispatch("SAPI.SpObjectTokenCategory")
            cat.SetId(r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech_OneCore\Voices", False)
            for t in cat.EnumerateTokens():
                toks.append(t)
        except Exception:  # noqa: BLE001
            pass
        return toks

    @classmethod
    def list_voices(cls):
        if sys.platform != "win32":
            return []
        if cls._cache is not None:
            return cls._cache
        res, seen = [], set()
        try:
            for t in cls._tokens():
                name = t.GetDescription()
                try:
                    lang_hex = t.GetAttribute("Language").split(";")[0]
                    lcid = int(lang_hex, 16)
                    lang = {0x419: "ru", 0x43f: "kk", 0x422: "uk"}.get(lcid, "en" if lcid in (0x409, 0x809) else "xx")
                except Exception:  # noqa: BLE001
                    lang = "xx"
                try:
                    g = {"male": "m", "female": "f"}.get(t.GetAttribute("Gender").lower(), "")
                except Exception:  # noqa: BLE001
                    g = ""
                key = name.split(" - ")[0].replace("Desktop", "").strip()
                if key in seen:
                    continue
                seen.add(key)
                res.append((t.Id, name, lang, g))
        except Exception:  # noqa: BLE001
            res = []
        cls._cache = res
        return res

    def synth(self, text: str, token_id: str, speed: float, pitch: int) -> np.ndarray:
        import math

        wc = self._com()
        voice = wc.Dispatch("SAPI.SpVoice")
        tok = next((t for t in self._tokens() if t.Id == token_id), None)
        if tok is None:
            raise OfflineVoiceMissing("Этот голос Windows не найден.")
        voice.Voice = tok
        stream = wc.Dispatch("SAPI.SpMemoryStream")
        fmt = wc.Dispatch("SAPI.SpAudioFormat")
        fmt.Type = 22  # 22 кГц, 16 бит, моно
        stream.Format = fmt
        voice.AudioOutputStream = stream
        voice.Rate = int(max(-10, min(10, round(10 * math.log(max(0.3, speed)) / math.log(3)))))
        xml = f'<pitch absmiddle="{int(pitch)}">{escape(text)}</pitch>'
        voice.Speak(xml, 8)  # 8 = текст в формате SAPI XML
        data = bytes(stream.GetData())
        a = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768
        return resample(a, 22050, SR)


# ============================================================================ диспетчер
class Speaker:
    """Озвучивает фразу выбранным голосом; если онлайн-голос недоступен — запасным офлайн."""

    def __init__(self, edge_slots=None, strict=False):
        self.strict = strict  # сохранение в MP3: всегда пробуем выбранный голос (без «паузы на 10 минут»)
        self.edge = EdgeEngine(edge_slots)
        self.piper = PiperEngine()
        self.silero = SileroEngine()
        self.sapi = Sapi()
        self._edge_down_until = 0.0
        self._bad_voice: dict[str, float] = {}
        self.last_engine = ""
        self.on_status = lambda msg: None

    def _engine_synth(self, vid: str, text: str, vs: VoiceSettings) -> np.ndarray:
        kind, _, name = vid.partition(":")
        text = stress_for(kind, text)
        if kind == "edge":
            return self.edge.synth(text, name, vs.speed, vs.pitch)
        if kind == "piper":
            return self.piper.synth(text, name, vs.speed, vs.pitch)
        if kind == "silero":
            return self.silero.synth(text, name.split("/")[-1], vs.speed, vs.pitch)
        if kind == "sapi":
            return self.sapi.synth(text, name, vs.speed, vs.pitch)
        if kind == "xtts":
            import clone
            return clone.synth(text, name, vs.speed, vs.pitch, status=self.on_status)
        raise ValueError(f"Неизвестный голос: {vid}")

    def synth(self, text: str, vs: VoiceSettings) -> np.ndarray:
        return enhance(self._synth_raw(text, vs), vs)

    def _synth_raw(self, text: str, vs: VoiceSettings) -> np.ndarray:
        text = PRON.apply(speakable(text))
        try:
            import textproc
            text = textproc.homographs(text)  # «висячий замок» → замо́к (после вашего словаря — он главнее)
        except Exception:  # noqa: BLE001
            pass
        if not re.search(r"\w", text):
            return _silence(0.3)
        vid = vs.voice
        if vid.startswith("edge:"):
            now = time.time()
            bad_until = self._bad_voice.get(vid, 0)
            if self.strict or (now >= self._edge_down_until and now >= bad_until):
                err = None
                for attempt in range(2):  # одна повторная попытка при сбое связи
                    try:
                        a = self.edge.synth(stress_for("edge", text), vid[5:], vs.speed, vs.pitch)
                        if self.last_engine != "edge":
                            self.on_status("Читает онлайн-голос")
                        self.last_engine = "edge"
                        return a
                    except Exception as e:  # noqa: BLE001
                        err = e
                        if type(e).__name__ == "NoAudioReceived" or isinstance(e, ValueError):
                            break
                        time.sleep(0.5)
                if type(err).__name__ == "NoAudioReceived":
                    # сервер Microsoft не отдаёт звук этим голосом (бывает с отдельными голосами)
                    self._bad_voice[vid] = time.time() + 600
                    msg = "Этот онлайн-голос сейчас не отвечает у Microsoft"
                elif isinstance(err, ValueError) or "invalid voice" in str(err).lower():
                    self._bad_voice[vid] = time.time() + 3600
                    msg = "Такого онлайн-голоса у Microsoft больше нет"
                else:
                    self._edge_down_until = time.time() + 60  # снова проверим интернет через минуту
                    msg = "Нет связи с сервером голосов"
                if not vs.fallback:
                    raise RuntimeError(f"{msg}. Выберите другой голос или запасной офлайн-голос.\n({err_text(err)})") from err
                self.on_status(msg + " — читаю запасным офлайн-голосом")
            if not vs.fallback:
                raise RuntimeError("Онлайн-голос недоступен, а запасной офлайн-голос не выбран.")
            vid = vs.fallback
        try:
            a = self._engine_synth(vid, text, vs)
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            if vid != vs.voice:
                raise RuntimeError(f"Онлайн-голос недоступен, а запасной голос тоже не сработал: {err_text(e)}") from e
            raise RuntimeError(err_text(e)) from e
        self.last_engine = vid.split(":")[0]
        return a
