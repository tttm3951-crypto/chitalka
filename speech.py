"""
Озвучка: разбиение текста на фразы, движки Edge-TTS (онлайн) и Piper (офлайн),
вывод звука, предзагрузка следующих фраз, экспорт в MP3.
"""
from __future__ import annotations

import asyncio
import glob
import json
import shutil
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

import numpy as np

APP_DIR = os.path.dirname(os.path.abspath(__file__))

# ----------------------------------------------------------------------------- разбиение на фразы
@dataclass
class Chunk:
    text: str
    start: int  # позиция в полном тексте (символы)
    end: int
    role: str = ""  # "speech" — прямая речь героя (если включён «голос героев»)


_DASHES = "—–"
_LOWER_AFTER = re.compile(r"\s*[a-zа-яё]")


_CHILD = re.compile(r"\b(мальчик\w*|девочк\w*|ребён\w*|ребен\w*|малыш\w*|малыш|сынишк\w*|дочк\w*|"
                    r"внучк\w*|внучек|детск\w*|дети|ребята)\b", re.I)
_FEM_PRON = re.compile(r"\b(она|девушка|женщина|мама|мать|бабушка|старушка|жена|сестра|тётя|тетя)\b", re.I)
_MALE_PRON = re.compile(r"\b(он|мужчина|папа|отец|дедушка|старик|муж|брат|дядя|парень)\b", re.I)


def remark_gender(remark: str) -> str:
    """Кто говорит — по словам автора: «сказала она» → f, «ответил Петя» → m, «пискнул малыш» → c."""
    r = remark.lower()
    if _CHILD.search(r):
        return "c"
    words = re.findall(r"[а-яё]+", r)[:4]
    for w in words:
        if len(w) >= 4 and re.search(r"(ла|лась)$", w):
            return "f"
        if len(w) >= 4 and re.search(r"[^аеёиоуыэюя]л$|лся$", w) or re.search(r"[аеёиоуыэюя]л$", w) and len(w) >= 4:
            return "m"
    if _FEM_PRON.search(r):
        return "f"
    if _MALE_PRON.search(r):
        return "m"
    return ""


def dialog_spans(par: str) -> list[tuple[int, int, str]]:
    spans = _dialog_spans(par)
    if any(r == "" for _a, _b, r in spans) and any(r == "speech" for _a, _b, r in spans):
        g = ""
        for a, b, r in spans:
            if r == "" and not g:
                g = remark_gender(par[a:b])
        if g:
            spans = [(a, b, "speech_" + g if r == "speech" else r) for a, b, r in spans]
    return spans


def _dialog_spans(par: str) -> list[tuple[int, int, str]]:
    """Делит абзац на речь героев и слова автора.
    «— Привет, — сказал он. — Как дела?»  →  речь | автор | речь.
    Он сказал: «Пойдём».  →  автор | речь."""
    n = len(par)
    spans = []
    st = par.lstrip()
    lead = n - len(st)
    first = re.search(r"[A-Za-zА-Яа-яЁё]", st)
    if (st[:1] in _DASHES or st.startswith("- ")) and first and first.group(0).islower():
        return [(0, n, "")]  # «— скидка», «- стоимость …» — пункт списка, а не реплика
    if st[:1] in _DASHES or st.startswith("- "):
        role, start = "speech", 0
        i = lead + 1
        while i < n:
            if par[i] in _DASHES and i > 0 and par[i - 1].isspace() and i + 1 < n and par[i + 1].isspace():
                prev = par[:i].rstrip()[-1:] if par[:i].rstrip() else ""
                nxt = par[i + 1:].lstrip()[:1]
                if role == "speech" and prev in ",!?…" and nxt and nxt.islower():
                    spans.append((start, i, "speech"))
                    role, start = "", i
                elif role == "" and prev in ".,:" and nxt:
                    spans.append((start, i, ""))
                    role, start = "speech", i
            i += 1
        spans.append((start, n, role))
        return [x for x in spans if par[x[0]:x[1]].strip(" " + _DASHES)]
    # цитата после двоеточия: Он сказал: «…»
    pos, out = 0, []
    for m in re.finditer(r":\s*«([^«»]{2,})»", par):
        a, b = m.start(1) - 1, m.end(1) + 1
        while b < n and par[b] in ".,;!?…":
            b += 1
        if a > pos:
            out.append((pos, a, ""))
        out.append((a, b, "speech"))
        pos = b
    if out:
        if pos < n:
            out.append((pos, n, ""))
        return [x for x in out if par[x[0]:x[1]].strip()]
    return [(0, n, "")]


_SENT = re.compile(r"(?<=[.!?…])[»\"”')\]]*\s+(?=[«\"“(\[—–\-A-ZА-ЯЁӘҒҚҢӨҰҮҺІ0-9])")


def build_text_and_chunks(paragraphs: list[str], max_len=350, min_len=60, dialog=False):
    """Склеивает абзацы в один текст (через пустую строку) и режет на фразы.
    dialog=True — речь героев и слова автора становятся отдельными фразами (для двух голосов).

    Возвращает (полный_текст, список Chunk, позиции_начала_абзацев).
    """
    parts = []
    par_pos = []
    pos = 0
    chunks: list[Chunk] = []
    for full_par in paragraphs:
        par_pos.append(pos)
        parts.append(full_par)
        for sp_a, sp_b, role in (dialog_spans(full_par) if dialog else [(0, len(full_par), "")]):
            _split_into(full_par, sp_a, sp_b, role, pos, chunks, max_len, min_len)
        pos += len(full_par) + 2
    return "\n\n".join(parts), chunks, par_pos


def _split_into(full_par, sp_a, sp_b, role, pos, chunks, max_len, min_len):
    """Режет кусок абзаца [sp_a, sp_b) на фразы."""
    par = full_par[:sp_b]
    if True:
        # предложения внутри куска
        sents = []
        last = sp_a
        for m in _SENT.finditer(par, sp_a):
            sents.append((last, m.start() + len(m.group(0).rstrip())))
            last = m.end()
        sents.append((last, len(par)))
        # склеиваем короткие, режем длинные
        merged: list[tuple[int, int]] = []
        for s, e in sents:
            if merged and (e - merged[-1][0]) <= max_len and (merged[-1][1] - merged[-1][0]) < min_len:
                merged[-1] = (merged[-1][0], e)
            else:
                merged.append((s, e))
        for s, e in merged:
            while e - s > max_len:
                seg = par[s:s + max_len]
                cut = max(seg.rfind(", "), seg.rfind("; "), seg.rfind(" — "), seg.rfind(": "))
                if cut < max_len // 3:
                    cut = seg.rfind(" ")
                if cut <= 0:
                    cut = max_len
                else:
                    cut += 1
                piece = par[s:s + cut].strip()
                if piece:
                    chunks.append(Chunk(piece, pos + s, pos + s + cut, role))
                s += cut
            piece = par[s:e].strip()
            if piece:
                lead = len(par[s:e]) - len(par[s:e].lstrip())
                chunks.append(Chunk(piece, pos + s + lead, pos + e, role))


from voices import *  # noqa: E402,F401,F403  (движки и каталог голосов)
from voices import _silence  # noqa: E402


def log_error(e: BaseException):
    """Подробности ошибки — в error.log рядом с программой."""
    import traceback
    try:
        with open(os.path.join(APP_DIR, "error.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("\n=== %Y-%m-%d %H:%M:%S ===\n"))
            f.write("".join(traceback.format_exception(type(e), e, e.__traceback__)))
    except Exception:  # noqa: BLE001
        pass


# ----------------------------------------------------------------------------- вывод звука
class AudioOut:
    """Вывод звука. Если устройства нет или его выдернули — программа не падает:
    ok=False, error — причина; restart() переподключает (в т.ч. к новым наушникам)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._buf: np.ndarray | None = None
        self._pos = 0
        self.done = threading.Event()
        self.done.set()
        self.paused = False
        self.volume = 1.0
        self.bg = None          # тихий фон (зацикленный звук), играет пока bg_on
        self.bg_level = 0.0
        self.bg_on = False
        self._bg_pos = 0
        self._bg_gain = 0.0
        self._stream = None
        self.ok = False
        self.error: BaseException | None = None
        self._last_cb = time.time()
        self._open()

    def _open(self, rescan=False):
        try:
            import sounddevice as sd
            if rescan:  # заново ищем устройства (подключили наушники/колонки)
                try:
                    sd._terminate()
                    sd._initialize()
                except Exception:  # noqa: BLE001
                    pass
            self._stream = sd.OutputStream(samplerate=SR, channels=1, dtype="float32",
                                           callback=self._cb, blocksize=2048, latency="high")
            self._stream.start()
            self.ok, self.error = True, None
            self._last_cb = time.time()
        except Exception as e:  # noqa: BLE001
            self._stream = None
            self.ok, self.error = False, e
            log_error(e)
        return self.ok

    def restart(self) -> bool:
        self.close()
        with self._lock:
            had = self._buf is not None
        ok = self._open(rescan=True)
        if not ok and had:
            self.stop()
        return ok

    def stalled(self) -> bool:
        """Звук «завис»: есть что играть, не пауза, а устройство перестало запрашивать данные."""
        if not self.ok:
            return False
        try:
            active = bool(self._stream and self._stream.active)
        except Exception:  # noqa: BLE001
            active = False
        with self._lock:
            busy = self._buf is not None and not self.paused
        return busy and (not active or time.time() - self._last_cb > 3.0)

    def _cb(self, outdata, frames, _time, _status):
        self._last_cb = time.time()
        with self._lock:
            outdata.fill(0)
            speaking = False
            if self._buf is not None and not self.paused:
                n = min(frames, len(self._buf) - self._pos)
                seg = self._buf[self._pos:self._pos + n] * self.volume
                outdata[:n, 0] = seg
                speaking = bool(n) and float(np.abs(seg).max(initial=0)) > 0.02
                self._pos += n
                if self._pos >= len(self._buf):
                    self._buf = None
                    self.done.set()
            bg = self.bg
            if bg is not None and self.bg_on and not self.paused and self.bg_level > 0:
                # пока звучит голос — фон тише (ducking), в паузах — чуть громче; плавно
                target = self.bg_level * (0.3 if speaking else 1.0)
                g0 = self._bg_gain
                self._bg_gain = g0 + (target - g0) * 0.35
                idx = (np.arange(frames) + self._bg_pos) % len(bg)
                outdata[:, 0] += bg[idx] * np.linspace(g0, self._bg_gain, frames, dtype=np.float32) * self.volume
                self._bg_pos = (self._bg_pos + frames) % len(bg)
            np.clip(outdata, -1.0, 1.0, out=outdata)

    def play(self, audio: np.ndarray):
        if not self.ok:
            raise RuntimeError(f"PortAudio: звуковое устройство недоступно ({self.error})")
        with self._lock:
            self._buf = audio
            self._pos = 0
            self.done.clear()

    def stop(self):
        with self._lock:
            self._buf = None
            self.done.set()

    def close(self):
        st, self._stream = self._stream, None
        self.ok = False
        if st is None:
            return
        try:
            st.stop()
            st.close()
        except Exception:  # noqa: BLE001
            pass


# ----------------------------------------------------------------------------- чтение вслух
class ReadAloud:
    """Читает фразы по порядку, заранее готовя следующие (без пауз между фразами).

    Сообщения в окно: callback(kind, value): ("chunk", индекс) | ("status", текст) |
    ("finished", None) | ("error", текст)
    """

    AHEAD = 6
    WORKERS = 3  # параллельная подготовка фраз (онлайн-голосу нужно время на ответ сервера)

    def __init__(self, speaker: Speaker, out: AudioOut, callback):
        self.speaker = speaker
        self.out = out
        self.cb = callback
        self.chunks: list[Chunk] = []
        self.chapter_starts = frozenset()
        self.settings = VoiceSettings()
        self._ver = 0
        self._cache: dict[int, tuple[int, np.ndarray]] = {}
        self._errors: dict[int, str] = {}
        self._cond = threading.Condition()
        self._cur = 0
        self._running = False
        self._gen = 0
        self._busy: set[int] = set()
        for _ in range(self.WORKERS):
            threading.Thread(target=self._synth_loop, daemon=True).start()

    # --- управление
    def set_chunks(self, chunks, chapter_starts=()):
        self.stop()
        with self._cond:
            self.chapter_starts = frozenset(chapter_starts)
            self.chunks = chunks
            self._cache.clear()
            self._errors.clear()
            self._busy.clear()
            self._ver += 1
            self._cond.notify_all()

    def set_settings(self, vs: VoiceSettings):
        with self._cond:
            if vs != self.settings:
                self.settings = vs
                self._ver += 1
                self._cache.clear()
                self._errors.clear()
                self._busy.clear()
                self._cond.notify_all()

    @property
    def running(self):
        return self._running

    def start(self, idx: int):
        self.stop()
        with self._cond:
            self._cur = max(0, min(idx, len(self.chunks) - 1))
            self._running = True
            self._gen += 1
            gen = self._gen
            self._cond.notify_all()
        self.out.paused = False
        threading.Thread(target=self._play_loop, args=(gen,), daemon=True).start()

    def stop(self):
        with self._cond:
            self._running = False
            self._gen += 1
            self._cond.notify_all()
        self.out.stop()

    # --- потоки
    def _synth_loop(self):
        while True:
            with self._cond:
                job = None
                while job is None:
                    if self.chunks and self._running:  # готовим фразы только во время чтения
                        for i in range(self._cur, min(len(self.chunks), self._cur + self.AHEAD + 1)):
                            if i not in self._cache and i not in self._errors and i not in self._busy:
                                job = (i, self.chunks[i].text, self.settings, self._ver, self.chunks[i].role)
                                self._busy.add(i)
                                break
                        # освобождаем память от прочитанного
                        for k in [k for k in self._cache if k < self._cur - 1 or k > self._cur + self.AHEAD + 2]:
                            del self._cache[k]
                    if job is None:
                        self._cond.wait(0.5)
            i, text, vs, ver, role = job
            JOB.valid = lambda ver=ver, i=i: ver == self._ver and self._running and i >= self._cur
            try:
                audio = self.speaker.synth(text, expr_vs(role_vs(vs, role), text))
                err = None
            except Cancelled:
                with self._cond:
                    self._busy.discard(i)
                continue
            except Exception as e:  # noqa: BLE001
                audio, err = None, e
                log_error(e)
            with self._cond:
                self._busy.discard(i)
                if ver == self._ver:
                    if err:
                        self._errors[i] = err
                    else:
                        self._cache[i] = (ver, audio)
                    self._cond.notify_all()

    def _play_loop(self, gen):
        while True:
            with self._cond:
                if gen != self._gen or not self._running:
                    return
                i = self._cur
                if i >= len(self.chunks):
                    self._running = False
                    self.cb("finished", None)
                    return
                t0 = time.time()
                told = False
                while i not in self._cache and i not in self._errors:
                    if not told and time.time() - t0 > 1.0:
                        told = True
                        self.cb("status", "Готовлю озвучку…")
                    self._cond.wait(0.2)
                    if gen != self._gen:
                        return
                if i in self._errors:
                    self._running = False
                    self.cb("error", self._errors.pop(i))
                    return
                _, audio = self._cache[i]
            self.cb("chunk", i)
            # пауза после фразы; в конце абзаца — длиннее
            p = pause_after(self.chunks, i, self.settings.pause, self.chapter_starts)
            if p > 0.01:
                audio = np.concatenate([audio, _silence(p)])
            try:
                self.out.play(audio)
            except Exception as e:  # noqa: BLE001  нет звукового устройства
                with self._cond:
                    self._running = False
                self.cb("error", e)
                return
            while not self.out.done.wait(0.1):
                if gen != self._gen:
                    return
            with self._cond:
                if gen != self._gen:
                    return
                self._cur = i + 1
                self._cond.notify_all()


# ----------------------------------------------------------------------------- экспорт в MP3
def safe_name(s: str, limit=80) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s).strip(" .")
    return s[:limit] or "Книга"


def plan_parts(chunks: list[Chunk], chapters_chunk_idx: list[tuple[str, int]], max_chars=40000, min_chars=3000):
    """Делит книгу на части для MP3: по главам; слишком длинные главы режутся,
    слишком короткие склеиваются. Возвращает список (название, [индексы фраз])."""
    bounds = sorted({0} | {ci for _, ci in chapters_chunk_idx if 0 <= ci < len(chunks)})
    titles = {ci: t for t, ci in chapters_chunk_idx}
    raw = []
    for bi, s in enumerate(bounds):
        e = bounds[bi + 1] if bi + 1 < len(bounds) else len(chunks)
        if e > s:
            raw.append((titles.get(s, ""), list(range(s, e))))
    # склейка коротких
    merged: list[tuple[str, list[int]]] = []
    for t, idxs in raw:
        size = sum(len(chunks[i].text) for i in idxs)
        if merged and (size < min_chars or sum(len(chunks[i].text) for i in merged[-1][1]) < min_chars):
            merged[-1] = (merged[-1][0] or t, merged[-1][1] + idxs)
        else:
            merged.append((t, idxs))
    # разрезка длинных
    parts: list[tuple[str, list[int]]] = []
    for t, idxs in merged:
        cur, size, n = [], 0, 1
        total = sum(len(chunks[i].text) for i in idxs)
        for i in idxs:
            cur.append(i)
            size += len(chunks[i].text)
            if size >= max_chars and total - size > min_chars:
                parts.append((f"{t} ({n})" if t else "", cur))
                cur, size, n = [], 0, n + 1
        if cur:
            parts.append((f"{t} ({n})" if (t and n > 1) else t, cur))
    return parts


def prepare_cover(raw: bytes | None, size: int = 600) -> bytes:
    """Обложка для MP3: JPEG не больше 600×600 (плееры телефона показывают её вместо серого диска)."""
    if not raw:
        return b""
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        im = im.convert("RGB")
        im.thumbnail((size, size))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=85, optimize=True)
        return out.getvalue()
    except Exception:  # noqa: BLE001
        return b""


def apic_frame(jpeg: bytes) -> bytes:
    if not jpeg:
        return b""
    d = b"\x00image/jpeg\x00\x03\x00" + jpeg  # кодировка, MIME, тип 3 = «обложка спереди», пустое описание
    return b"APIC" + len(d).to_bytes(4, "big") + b"\x00\x00" + d


def read_apic(data: bytes) -> bytes:
    """Обложка (JPEG/PNG) из тега ID3 готового MP3."""
    if data[:3] != b"ID3":
        return b""
    size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
    ver, pos, end = data[3], 10, 10 + size
    while pos + 10 <= end:
        fid = data[pos:pos + 4]
        if not fid.strip(b"\x00"):
            break
        raw = data[pos + 4:pos + 8]
        n = ((raw[0] << 21) | (raw[1] << 14) | (raw[2] << 7) | raw[3]) if ver == 4 else int.from_bytes(raw, "big")
        body = data[pos + 10:pos + 10 + n]
        pos += 10 + n
        if fid == b"APIC" and body:
            i = body.index(b"\x00", 1) + 2  # после MIME и типа картинки
            enc = body[0]
            if enc in (1, 2):
                j = i
                while j + 1 < len(body) and body[j:j + 2] != b"\x00\x00":
                    j += 2
                return body[j + 2:]
            return body[body.index(b"\x00", i) + 1:]
    return b""


def id3_tag(title: str, album: str, artist: str, track: str, cover: bytes = b"") -> bytes:
    """Простой тег ID3v2.3, чтобы плеер на телефоне показывал книгу, автора, номер части и обложку."""
    def frame(fid, text):
        if not text:
            return b""
        data = b"\x01" + text.encode("utf-16")  # UTF-16 с BOM
        return fid.encode() + len(data).to_bytes(4, "big") + b"\x00\x00" + data
    body = frame("TIT2", title) + frame("TALB", album) + frame("TPE1", artist) + frame("TRCK", track) + \
        frame("TCON", "Audiobook") + apic_frame(cover)
    n = len(body)
    size = bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])
    return b"ID3\x03\x00\x00" + size + body


# ----------------------------------------------------------------------------- размер и качество MP3
MP3_RATES = [24, 32, 40, 48, 56, 64, 80, 96]  # допустимые для речи (моно 24 кГц, MPEG-2: максимум 160)
BOOK_MAX_MB = 150  # вся книга в хорошем качестве — не больше


def make_encoder(kbps: int, in_rate: int = SR):
    """Кодировщик MP3, настроенный под речь. На малых битрейтах — 16 кГц и тщательный режим:
    так 24–32 кбит/с звучат чисто (как «стандарт» Audible), а файл в 5–6 раз меньше 160 кбит/с."""
    import lameenc
    kbps = int(kbps)
    enc = lameenc.Encoder()
    enc.set_bit_rate(kbps)
    enc.set_in_sample_rate(in_rate)
    # 192 кбит/с бывает только у MPEG-1 (32–48 кГц) — как у образца «Transhumanism Inc.» (48 кГц)
    enc.set_out_sample_rate(16000 if kbps <= 32 else 48000 if kbps > 160 else 24000)
    enc.set_channels(1)
    enc.set_quality(2 if kbps <= 48 or kbps >= 192 else 5)
    return enc


def estimate_seconds(chunks, speed=1.0, pause=0.15) -> float:
    """Примерная длительность звучания (≈15 знаков в секунду при скорости ×1)."""
    chars = sum(len(c.text) for c in chunks)
    return chars / (15.0 * max(0.5, speed)) + len(chunks) * pause * 1.4


def pick_bitrate(seconds: float, size_mb: float, lo: int = 24, hi: int = 64) -> int:
    """Битрейт, при котором звук уложится в size_mb (с запасом 8% на неточность оценки)."""
    if not seconds or not size_mb:
        return 48
    exact = size_mb * 0.92 * 1048576 * 8 / 1000 / seconds
    ok = [b for b in MP3_RATES if lo <= b <= min(hi, exact)]
    return ok[-1] if ok else lo


def read_id3_text(data: bytes) -> dict:
    """Текстовые поля тега ID3v2.3/2.4 (TIT2, TALB, TPE1, TRCK…)."""
    out = {}
    if data[:3] != b"ID3":
        return out
    size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
    ver = data[3]
    pos, end = 10, 10 + size
    while pos + 10 <= end:
        fid = data[pos:pos + 4]
        if not fid.strip(b"\x00"):
            break
        raw = data[pos + 4:pos + 8]
        n = ((raw[0] << 21) | (raw[1] << 14) | (raw[2] << 7) | raw[3]) if ver == 4 else int.from_bytes(raw, "big")
        body = data[pos + 10:pos + 10 + n]
        pos += 10 + n
        if fid.startswith(b"T") and body:
            enc, txt = body[0], body[1:]
            try:
                t = txt.decode({0: "latin-1", 1: "utf-16", 2: "utf-16-be", 3: "utf-8"}.get(enc, "latin-1"))
            except UnicodeDecodeError:
                t = ""
            out[fid.decode("latin-1")] = t.strip("\x00")
    return out


def recompress_mp3(path: str, kbps: int, album: str | None = None) -> tuple[int, int]:
    """Пережимает готовый MP3 в меньший битрейт (тег сохраняется, альбом можно выровнять).
    Возвращает (байт было, байт стало)."""
    import miniaudio
    with open(path, "rb") as f:
        head = f.read(4096)
    tags = read_id3_text(head) if head[:3] == b"ID3" else {}
    if head[:3] == b"ID3":
        size = (head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9]
        if 10 + size > len(head):
            with open(path, "rb") as f:
                tags = read_id3_text(f.read(10 + size))
    with open(path, "rb") as fh:  # decode_file не открывает пути с русскими буквами — читаем сами
        whole = fh.read()
    cover = prepare_cover(read_apic(whole))
    dec = miniaudio.decode(whole, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1)
    del whole
    enc = make_encoder(kbps, dec.sample_rate)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(id3_tag(tags.get("TIT2", os.path.splitext(os.path.basename(path))[0]),
                        album or tags.get("TALB", ""), tags.get("TPE1", ""), tags.get("TRCK", ""), cover))
        pcm = dec.samples.tobytes()
        step = dec.sample_rate * 2 * 60  # по минуте
        for i in range(0, len(pcm), step):
            f.write(enc.encode(pcm[i:i + step]))
        f.write(enc.flush())
    before = os.path.getsize(path)
    os.replace(tmp, path)
    return before, os.path.getsize(path)


def mp3_seconds(path: str) -> float:
    try:
        import miniaudio
        with open(path, "rb") as fh:
            return miniaudio.mp3_get_info(fh.read()).duration
    except Exception:  # noqa: BLE001
        return 0.0


# Настройки скорости сохранения для каждого движка (замерено на RTX 3050 / Ryzen 5 3600):
#   edge   — сервер Microsoft: 32 параллельных запроса, текст кусками ~1500 симв.  (~×30)
#   silero — видеокарта: один поток, куски ~700 симв.                              (~×120)
#   piper  — процессор уже загружен полностью, куски ~1200 симв.                   (~×20)
#   sapi   — голоса Windows: 8 процессов, куски ~1500 симв.                        (~×400…800)
#   xtts   — видеокарта, куски по фразам                                            (~×1,5)
EXPORT_PROFILE = {
    "edge": dict(workers=32, group=450, mode="thread"),  # длинные куски онлайн-голос отдаёт слишком медленно
    "silero": dict(workers=3, group=700, mode="thread"),
    "piper": dict(workers=1, group=1200, mode="thread"),
    "sapi": dict(workers=cpu_workers(), group=1500, mode="process"),
    "xtts": dict(workers=1, group=0, mode="thread"),
}

_proc_speaker = None


def _proc_synth(text, vs):
    """Синтез в отдельном процессе (для голосов Windows)."""
    global _proc_speaker
    if _proc_speaker is None:
        _proc_speaker = Speaker()
    return _proc_speaker.synth(text, vs)


def pause_after(chunks, i, base: float, chapter_starts=frozenset()) -> float:
    """Пауза после фразы по смыслу: в конце абзаца длиннее, перед новой главой — долгая,
    после многоточия — задумчивая, между речью героя и словами автора — короткая смена."""
    c = chunks[i]
    nxt = chunks[i + 1] if i + 1 < len(chunks) else None
    p = base
    if nxt is not None and nxt.start - c.end >= 2:
        p *= 2.5
    if c.text.rstrip(" »\"')”").endswith(("…", "...")):
        p += 0.4
    if nxt is not None and (i + 1) in chapter_starts:
        p = max(p, 1.8)                      # перед главой
    elif i in chapter_starts and len(c.text) < 120:
        p = max(p, 1.0)                      # после названия главы
    elif nxt is not None and bool(c.role) != bool(nxt.role):
        p += 0.08
    return p


def make_groups(chunks, idxs, limit, split_expr=False):
    """Склеивает соседние фразы в куски до limit символов (меньше запросов — быстрее).
    Режет по концам абзацев, когда кусок уже достаточно длинный.
    Возвращает [(текст, [индексы], кончается_абзацем)]."""
    groups, cur, cur_i = [], "", []

    def par_end(n):
        return n + 1 >= len(idxs) or chunks[idxs[n + 1]].start - chunks[idxs[n]].end >= 2

    for n, i in enumerate(idxs):
        t = chunks[i].text
        prev_t = chunks[cur_i[-1]].text.rstrip(" »\"')”") if cur_i else ""
        if cur and (not limit or len(cur) + len(t) + 1 > limit or chunks[cur_i[-1]].role != chunks[i].role
                    or (split_expr and prev_t.endswith(("?", "!", "…", "...")))):
            groups.append((cur, cur_i, par_end(idxs.index(cur_i[-1]))))
            cur, cur_i = "", []
        if cur:
            cur += ("\n" if par_end(n - 1) else " ") + t
        else:
            cur = t
        cur_i.append(i)
        if par_end(n) and (not limit or len(cur) > limit * 0.6):
            groups.append((cur, cur_i, True))
            cur, cur_i = "", []
    if cur:
        groups.append((cur, cur_i, True))
    return [(t, ii, pe, chunks[ii[0]].role) for t, ii, pe in groups]


def export_mp3(chunks, parts, out_dir, speaker: Speaker, vs: VoiceSettings, book_title="", author="",
               progress=lambda done, total, msg: None, cancel=lambda: False, bitrate=64, workers=None,
               resume=False, volume=1.0, name_base="", status=lambda m: None, paused=lambda: False, cover=b"",
               chapter_starts=frozenset(), bg_kind="нет", bg_level=0.0):
    """Озвучивает части книги в MP3. Возвращает (файлы, остановлено, пропущенные_фразы)."""
    import lameenc
    from collections import deque
    from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor

    kind = vs.voice.split(":")[0]
    prof = dict(EXPORT_PROFILE.get(kind, EXPORT_PROFILE["piper"]))
    if workers:
        prof["workers"] = workers
    # в MP3 голос не меняется посреди книги: при сбое сервера ждём и повторяем, а не берём запасной голос
    vs_exp = VoiceSettings(**{**vs.__dict__, "fallback": ""}) if kind == "edge" or vs.voice2.startswith("edge:") else vs
    os.makedirs(out_dir, exist_ok=True)
    try:
        bg = make_bg(bg_kind) if bg_level > 0 else None
    except Exception as e:  # noqa: BLE001  фон — не главное: без него книга всё равно сохранится
        log_error(e)
        bg = None
    bg_pos = 0
    total = sum(len(p[1]) for p in parts)
    done = 0
    written, skipped = [], []
    base = name_base or ""

    def fname_for(pi):
        return os.path.join(out_dir, f"{base} - {pi:02d}.mp3" if base else f"{pi:02d}.mp3")

    abort = {"e": None, "streak": 0}
    abort_lock = threading.Lock()

    def synth_retry(text, role=""):
        delay = 2
        vr = expr_vs(role_vs(vs_exp, role), text)
        for attempt in range(12):  # до ~10 минут ожидания связи
            try:
                if prof["mode"] == "process" and vr.voice.split(":")[0] == kind:
                    return pool.submit(_proc_synth, text, vr).result()
                return speaker.synth(text, vr)
            except Exception as e:  # noqa: BLE001
                if cancel() or abort["e"] is not None:
                    raise
                # голос «не отвечает у Microsoft» (а интернет есть) — долго ждать бессмысленно: 4 попытки
                noaudio = "NoAudioReceived" in repr(e.__cause__) + repr(e) or "не отвечает" in str(e)
                last = 3 if noaudio else 11
                if attempt >= 2:
                    status(f"Нет ответа от голоса ({err_text(e)[:60]}) — повтор через {delay} с…")
                if attempt >= last:
                    raise
                time.sleep(delay)
                delay = min(60, delay * 2)

    def job(text, role=""):
        try:
            a = synth_retry(text, role)
            with abort_lock:
                abort["streak"] = 0
            return a, None
        except Exception as e:  # noqa: BLE001
            # одна фраза не должна обрывать всю книгу: вместо неё — короткая тишина.
            # Но если голос не отвечает фраза за фразой — останавливаемся, а не пишем книгу из тишины.
            log_error(e)
            with abort_lock:
                abort["streak"] += 1
                if abort["streak"] >= 4 and abort["e"] is None:
                    abort["e"] = e
            return _silence(0.5), text

    if kind == "silero":
        SileroEngine.ensure_copies(prof["workers"])  # несколько копий модели на видеокарте
    pool = ProcessPoolExecutor(max_workers=prof["workers"]) if prof["mode"] == "process" else None
    threads = ThreadPoolExecutor(max_workers=prof["workers"] if prof["mode"] == "thread" else prof["workers"] * 2)
    try:
        for pi, (title, idxs) in enumerate(parts, 1):
            fname = fname_for(pi)
            if resume and os.path.isfile(fname) and os.path.getsize(fname) > 1000:
                done += len(idxs)  # этот файл уже готов с прошлого раза
                written.append(fname)
                progress(done, total, f"Файл {pi:02d} уже готов — пропускаю")
                continue
            enc = make_encoder(bitrate)
            tmp = fname + ".part"
            groups = make_groups(chunks, idxs, prof["group"], split_expr=vs.expressive)
            with open(tmp, "wb") as f:
                album = f"Аудио рассказ — {book_title}" if book_title else "Аудио рассказ"
                f.write(id3_tag(f"{pi:02d}. {title}" if title else f"{book_title} — часть {pi:02d}", album, author,
                                f"{pi}/{len(parts)}", cover))
                pending = deque()
                it = iter(groups)
                for _ in range(prof["workers"] * 2):
                    g = next(it, None)
                    if g is not None:
                        pending.append((g, threads.submit(job, g[0], g[3])))
                while pending:
                    g, fut = pending.popleft()
                    if paused() and not cancel():  # пауза: ждём на месте, ничего не теряя
                        progress(done, total, f"⏸ Пауза — файл {pi:02d} из {len(parts)}")
                        while paused() and not cancel():
                            time.sleep(0.2)
                    if cancel():
                        for _, fu in pending:
                            fu.cancel()
                        f.close()
                        os.remove(tmp)
                        return written, True, skipped
                    audio, failed = fut.result()
                    if abort["e"] is not None:
                        for _, fu in pending:
                            fu.cancel()
                        raise RuntimeError(f"Голос перестал отвечать: несколько фраз подряд не озвучились "
                                           f"({err_text(abort['e'])[:160]}). Готовые файлы сохранены — "
                                           "можно продолжить позже или выбрать другой голос.") from abort["e"]
                    if failed:
                        skipped.append(failed)
                    g2 = next(it, None)
                    if g2 is not None:
                        pending.append((g2, threads.submit(job, g2[0], g2[3])))
                    audio = np.concatenate([apply_gain(audio, volume),
                                            _silence(pause_after(chunks, g[1][-1], vs.pause, chapter_starts))])
                    if bg is not None:
                        audio, bg_pos = duck_mix(audio, bg, bg_pos, bg_level)
                    f.write(enc.encode((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()))
                    done += len(g[1])
                    progress(done, total, f"Файл {pi:02d} из {len(parts)}")
                f.write(enc.flush())
            os.replace(tmp, fname)
            written.append(fname)
    finally:
        threads.shutdown(wait=False, cancel_futures=True)
        if pool:
            pool.shutdown(wait=False, cancel_futures=True)
    with open(os.path.join(out_dir, "Содержание.txt"), "w", encoding="utf-8") as f:
        f.write(f"Аудио рассказ\n{book_title}\n{author}\n\n")
        for pi, (title, _) in enumerate(parts, 1):
            f.write(f"{os.path.basename(fname_for(pi))}  {title}\n")
        if skipped:
            f.write("\nНе озвучены (вместо них тишина):\n" + "\n".join(f"- {t[:200]}" for t in skipped))
    return written, False, skipped


# ----------------------------------------------------------------------------- один цельный файл
# Справочные разделы в конце книги — их не озвучиваем (часы чтения списков литературы никто не слушает)
REF_TITLES = re.compile(r"^\s*(?:\d+\.\s*)?(?:библиограф|список\s+(?:литературы|источников|сокращений)|литература\b|"
                        r"примечани|комментари|указател|предметный\s+указатель|именной\s+указатель|источники\b|"
                        r"bibliograph|references\b|notes\b|index\b|glossary\b)", re.I)


def is_reference(title: str) -> bool:
    return bool(title and REF_TITLES.search(title))


def strip_id3(data: bytes) -> bytes:
    if data[:3] == b"ID3":
        size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
        return data[10 + size:]
    return data


def book_tag(title: str, album: str, artist: str, chapters: list, cover: bytes = b"") -> bytes:
    """ID3v2.3 с главами (CHAP/CTOC): плееры аудиокниг показывают оглавление и переходят по главам."""
    def text(fid, t):
        if not t:
            return b""
        d = b"\x01" + t.encode("utf-16")
        return fid.encode() + len(d).to_bytes(4, "big") + b"\x00\x00" + d

    def frame(fid, d):
        return fid.encode() + len(d).to_bytes(4, "big") + b"\x00\x00" + d

    body = text("TIT2", title) + text("TALB", album) + text("TPE1", artist) + text("TCON", "Audiobook") + \
        apic_frame(cover)
    ids = [f"ch{i:03d}".encode() for i in range(len(chapters))]
    if chapters and len(chapters) <= 255:
        body += frame("CTOC", b"toc\x00" + b"\x03" + bytes([len(chapters)]) + b"".join(i + b"\x00" for i in ids)
                      + text("TIT2", "Оглавление"))
        for eid, (t, a, b) in zip(ids, chapters):
            body += frame("CHAP", eid + b"\x00" + int(a).to_bytes(4, "big") + int(b).to_bytes(4, "big")
                          + b"\xff\xff\xff\xff" * 2 + text("TIT2", t or eid.decode()))
    n = len(body)
    size = bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])
    return b"ID3\x03\x00\x00" + size + body


def _hms(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def _encode_segment(src: str, dst: str, kbps: int) -> str:
    """Перекодирует один файл (для параллельной склейки в отдельных процессах)."""
    import miniaudio
    with open(src, "rb") as fh:
        d = fh.read()
    enc = make_encoder(kbps)
    with open(dst, "wb") as out:
        for blk in miniaudio.stream_memory(d, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1,
                                           sample_rate=SR, frames_to_read=SR * 30):
            out.write(enc.encode(blk.tobytes()))
        out.write(enc.flush())
    return dst


def merge_mp3(files: list, out_path: str, titles: list, book_title: str, author: str, kbps: int | None = None,
              progress=lambda i, n, m: None, cancel=lambda: False, cover: bytes | None = None) -> str:
    """Склеивает части в один файл с главами.
    kbps=None — без перекодирования (части уже одного битрейта: мгновенно и без потерь);
    kbps=N    — перекодирует всё в N кбит/с (для старых файлов большого размера)."""
    import miniaudio
    album = f"Аудио рассказ — {book_title}"
    tmp = out_path + ".part"
    durs, datas = [], []
    for f in files:
        with open(f, "rb") as fh:
            d = fh.read()
        try:
            durs.append(miniaudio.mp3_get_info(d).duration)
        except Exception:  # noqa: BLE001
            durs.append(0.0)
        datas.append(None if kbps else d)  # при перекодировании читаем снова по одному — экономим память
        del d
    if cover is None:  # обложку берём из первой части
        try:
            with open(files[0], "rb") as fh:
                cover = prepare_cover(read_apic(fh.read(4 * 1048576)))
        except (OSError, IndexError, ValueError):
            cover = b""
    chapters, t = [], 0.0
    for ti, du in zip(titles, durs):
        chapters.append((ti, t * 1000, (t + du) * 1000))
        t += du
    segs = []
    if kbps:
        # перекодирование: все ядра процессора параллельно, по файлу на процесс (в 5–8 раз быстрее)
        from concurrent.futures import ProcessPoolExecutor
        segs = [out_path + f".seg{i:03d}" for i in range(len(files))]
        with ProcessPoolExecutor(max_workers=max(1, min(len(files), (os.cpu_count() or 4) // 2))) as ex:
            futs = [ex.submit(_encode_segment, f, g, kbps) for f, g in zip(files, segs)]
            for i, fu in enumerate(futs):
                if cancel():
                    for x in futs:
                        x.cancel()
                    break
                fu.result()
                progress(i, len(files), f"Перекодирую: {i + 1} из {len(files)}")
        if cancel():
            for g in segs:
                try:
                    os.remove(g)
                except OSError:
                    pass
            return ""
    with open(tmp, "wb") as out:
        out.write(book_tag(book_title, album, author, chapters, cover))
        for i, f in enumerate(files):
            if cancel():
                out.close()
                os.remove(tmp)
                return ""
            progress(i, len(files), f"Склеиваю в один файл: часть {i + 1} из {len(files)}")
            if segs:
                with open(segs[i], "rb") as fh:
                    out.write(fh.read())
                os.remove(segs[i])
            else:
                out.write(strip_id3(datas[i]))
                datas[i] = None
    os.replace(tmp, out_path)
    with open(os.path.join(os.path.dirname(out_path), "Содержание.txt"), "w", encoding="utf-8") as fh:
        fh.write(f"Аудио рассказ\n{book_title}\n{author}\n\nФайл: {os.path.basename(out_path)}\n"
                 f"Звучание: {_hms(t)}\n\n")
        for ti, a, _b in chapters:
            fh.write(f"{_hms(a / 1000)}  {ti}\n")
    return out_path


# ----------------------------------------------------------------------------- задания на MP3
def export_book_job(job: dict, progress, cancel, status=lambda m: None, parallel: int = 1):
    """Задание целиком: открыть книгу, разбить, озвучить. Не зависит от того, что открыто в окне."""
    import bisect
    import formats

    book = formats.load_book(job["path"], notes=job.get("notes", "skip"))
    vs = VoiceSettings(**job["vs"])
    _full, chunks, par_pos = build_text_and_chunks(book.paragraphs, dialog=bool(vs.voice2))
    cover = prepare_cover(book.cover)
    starts = [c.start for c in chunks]
    chapters = []
    for t, pi in book.chapters:
        if pi < len(par_pos):
            ci = bisect.bisect_left(starts, par_pos[pi])
            if ci < len(chunks) and (not chapters or chapters[-1][1] != ci):
                chapters.append((t, ci))
    s, e = job.get("range") or (0, len(chunks))
    e = min(e, len(chunks))
    sub = chunks[s:e]
    minutes, by_chapters = job["split_minutes"], job["split_chapters"]
    if minutes == 0:
        parts = [("", list(range(len(sub))))]
    else:
        max_chars = int(minutes * 60 * 15 * max(0.5, vs.speed))
        chap = [(t, ci - s) for t, ci in chapters if s <= ci < e] if by_chapters else []
        parts = plan_parts(sub, chap, max_chars=max_chars, min_chars=min(3000, max_chars // 4))
    if job.get("skip_refs", True) and len(parts) > 1:
        # библиография, примечания, указатели в конце книги — не озвучиваем
        k = len(parts)
        while k > 1 and is_reference(parts[k - 1][0]) and k - 1 >= len(parts) * 0.5:
            k -= 1
        if k < len(parts):
            names = sorted({re.sub(r" \(\d+\)$", "", p[0]) for p in parts[k:]})
            status("Не озвучиваю справочные разделы в конце: " + ", ".join(names))
            parts = parts[:k]
    sp = Speaker(edge_slots=EXPORT_EDGE_SLOTS, strict=True)  # общий лимит запросов на все параллельные сохранения
    kind = vs.voice.split(":")[0]
    workers = {"edge": max(8, 48 // max(1, parallel)), "sapi": max(1, cpu_workers() // max(1, parallel))}.get(kind)
    sp.on_status = status
    title = job.get("title") or book.title
    cum = [0]
    for c in sub:
        cum.append(cum[-1] + len(c.text))

    def prog(d, t, m):
        progress(d, t, m, cum[min(d, len(cum) - 1)], cum[-1])

    if job.get("size_mb") and not job.get("bitrate_fixed"):
        # «вся книга не больше N МБ»: битрейт по длительности именно этой книги; выбирается один раз,
        # чтобы при продолжении после сбоя все части были одинаковыми
        job["bitrate"] = pick_bitrate(estimate_seconds([sub[i] for _, idx in parts for i in idx], vs.speed, vs.pause),
                                      job["size_mb"])
        job["bitrate_fixed"] = True
    written, cancelled, skipped = export_mp3(
        sub, parts, job["out_dir"], sp, vs, title, book.author, progress=prog, cancel=cancel,
        bitrate=job["bitrate"], resume=True, volume=job.get("volume", 1.0), workers=workers,
        paused=lambda: bool(job.get("_paused")), name_base=job.get("name_base", ""), status=status, cover=cover,
        chapter_starts=frozenset(ci - s for _t, ci in chapters if s <= ci < e),
        bg_kind=job.get("bg_kind", "нет"), bg_level=float(job.get("bg_level", 0.0)))
    if job.get("one_file") and not cancelled and len(written) == 1:
        single = os.path.join(job["out_dir"], (job.get("name_base") or safe_name("Аудио рассказ - " + title, 70)) + ".mp3")
        os.replace(written[0], single)
        written = [single]
    if job.get("one_file") and not cancelled and len(written) > 1:
        # части были нужны только для продолжения после сбоя — теперь склеиваем в один цельный файл
        base = job.get("name_base") or safe_name("Аудио рассказ - " + title, 70)
        single = os.path.join(job["out_dir"], base + ".mp3")
        merge_mp3(written, single, [t or f"Часть {i}" for i, (t, _) in enumerate(parts, 1)], title, book.author,
                  cover=cover,
                  progress=lambda i, k, m: progress(sum(len(x[1]) for x in parts), sum(len(x[1]) for x in parts),
                                                    m, cum[-1], cum[-1]), cancel=cancel)
        if os.path.isfile(single):
            for f in written:
                try:
                    os.remove(f)
                except OSError:
                    pass
            written = [single]
    if job.get("one_file") and not cancelled and written and (job.get("m4b") or job.get("speeds")):
        written = written + post_formats(written[0], job, status)
    return (written, cancelled, skipped), len(parts)


# ----------------------------------------------------------------------------- закладки
class Bookmarks:
    def __init__(self, path=os.path.join(APP_DIR, "settings.json")):
        self.path = path
        self.save_error = None
        self.problem = ""  # сообщение для окна, если настройки пришлось восстанавливать
        self.data = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.data = json.load(f)
                if not isinstance(self.data, dict):
                    raise ValueError("не словарь")
            except Exception as e:  # noqa: BLE001
                bad = path + time.strftime(".повреждён-%Y%m%d-%H%M%S")
                try:
                    os.replace(path, bad)
                except OSError:
                    bad = ""
                self.data = {}
                for alt in (path + ".bak", os.path.join(os.path.dirname(path), "settings.backup.json")):
                    try:
                        with open(alt, encoding="utf-8") as f:
                            d = json.load(f)
                        if isinstance(d, dict):
                            self.data = d
                            self.problem = (f"Файл настроек был повреждён ({type(e).__name__}) — скорее всего, "
                                            "компьютер выключился во время записи. Восстановлены настройки "
                                            f"из резервной копии «{os.path.basename(alt)}».")
                            break
                    except Exception:  # noqa: BLE001
                        continue
                if not self.problem:
                    self.problem = (f"Файл настроек был повреждён ({type(e).__name__}) — скорее всего, компьютер "
                                    "выключился во время записи. Программа начала с настроек по умолчанию: "
                                    "места чтения и очередь книг могли сброситься.")
                if bad:
                    self.problem += f"\nПовреждённый файл сохранён как «{os.path.basename(bad)}»."
        self.data.setdefault("positions", {})
        self._last_save = 0.0

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value

    def position(self, book_path) -> int:
        return int(self.data["positions"].get(os.path.normcase(book_path), 0))

    def position_char(self, book_path):
        """Место чтения как позиция символа в тексте (не зависит от того, как текст поделён на фразы)."""
        v = self.data.get("pos_char", {}).get(os.path.normcase(book_path))
        return int(v) if v is not None else None

    def set_position(self, book_path, idx, force=False, char=None):
        self.data["positions"][os.path.normcase(book_path)] = int(idx)
        if char is not None:
            self.data.setdefault("pos_char", {})[os.path.normcase(book_path)] = int(char)
        if force or time.time() - self._last_save > 10:
            self.save()

    def save(self):
        self._last_save = time.time()
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            # раз в сутки — резервная копия рабочих настроек
            bak = self.path + ".bak"
            try:
                if os.path.exists(self.path) and (not os.path.exists(bak) or
                                                  time.time() - os.path.getmtime(bak) > 86400):
                    shutil.copy2(self.path, bak)
            except OSError:
                pass
            os.replace(tmp, self.path)
            self.save_error = None
        except Exception as e:  # noqa: BLE001
            self.save_error = e


# ----------------------------------------------------------------------------- M4B, версии ×1.2/×1.5, Telegram
def ffmpeg_exe() -> str:
    """ffmpeg (идёт вместе с программой через пакет imageio-ffmpeg)."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        import shutil as _sh
        return _sh.which("ffmpeg") or ""


def _no_window():
    return {"creationflags": 0x08000000} if sys.platform == "win32" else {}  # без чёрного окна консоли


def read_chapters(path: str) -> list[tuple[str, float, float]]:
    """Главы (название, начало мс, конец мс) из тега готового MP3."""
    with open(path, "rb") as f:
        head = f.read(10)
        if head[:3] != b"ID3":
            return []
        size = (head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9]
        data = head + f.read(size)
    out, pos, end = [], 10, 10 + size
    while pos + 10 <= end:
        fid = data[pos:pos + 4]
        if not fid.strip(b"\x00"):
            break
        n = int.from_bytes(data[pos + 4:pos + 8], "big")
        body = data[pos + 10:pos + 10 + n]
        pos += 10 + n
        if fid == b"CHAP":
            z = body.index(b"\x00")
            a = int.from_bytes(body[z + 1:z + 5], "big")
            b = int.from_bytes(body[z + 5:z + 9], "big")
            title = ""  # подкадр TIT2 внутри CHAP
            sp = z + 17
            while sp + 10 <= len(body):
                sfid = body[sp:sp + 4]
                sn = int.from_bytes(body[sp + 4:sp + 8], "big")
                sb = body[sp + 10:sp + 10 + sn]
                sp += 10 + sn
                if sfid == b"TIT2" and sb:
                    title = sb[1:].decode({0: "latin-1", 1: "utf-16", 3: "utf-8"}.get(sb[0], "latin-1")).strip("\x00")
            out.append((title, a, b))
    return out


def make_m4b(mp3_path: str, out_path: str, kbps: int = 64, progress=lambda m: None) -> str:
    """Аудиокнига M4B (AAC) с главами и обложкой — стандарт Apple Books, Smart AudioBook Player и др."""
    import tempfile
    exe = ffmpeg_exe()
    if not exe:
        raise RuntimeError("Нет ffmpeg (No module named 'imageio_ffmpeg') — запустите install.bat")
    with open(mp3_path, "rb") as f:
        tags = read_id3_text(f.read(65536))
    with open(mp3_path, "rb") as f:
        cover = read_apic(f.read(8 * 1048576))
    chaps = read_chapters(mp3_path)
    td = tempfile.mkdtemp(prefix="chitalka_")
    try:
        meta = os.path.join(td, "meta.txt")

        def esc(t):
            return re.sub(r"([=;#\\\n])", r"\\\1", t or "")

        with open(meta, "w", encoding="utf-8") as f:
            f.write(";FFMETADATA1\n")
            f.write(f"title={esc(tags.get('TIT2', ''))}\nartist={esc(tags.get('TPE1', ''))}\n"
                    f"album={esc(tags.get('TALB', ''))}\ngenre=Audiobook\n")
            for t, a, b in chaps:
                f.write(f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={int(a)}\nEND={int(b)}\ntitle={esc(t)}\n")
        cmd = [exe, "-y", "-hide_banner", "-loglevel", "error", "-i", mp3_path, "-i", meta]
        if cover:
            cp = os.path.join(td, "cover.jpg")
            with open(cp, "wb") as f:
                f.write(prepare_cover(cover))
            cmd += ["-i", cp, "-map", "0:a", "-map", "2:v", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
        else:
            cmd += ["-map", "0:a"]
        tmp = out_path + ".part"
        cmd += ["-map_metadata", "1", "-map_chapters", "1", "-c:a", "aac", "-b:a", f"{int(kbps)}k", "-ac", "1",
                "-movflags", "+faststart", "-f", "mp4", tmp]
        progress("Делаю M4B (AAC с главами)…")
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **_no_window())
        if r.returncode != 0 or not os.path.isfile(tmp):
            raise RuntimeError("ffmpeg не смог сделать M4B: " + (r.stderr or "")[-400:])
        os.replace(tmp, out_path)
        return out_path
    finally:
        import shutil as _sh
        _sh.rmtree(td, ignore_errors=True)


def speed_version(mp3_path: str, factor: float, out_path: str, kbps: int, progress=lambda m: None) -> str:
    """Версия книги быстрее (×1.2, ×1.5) без изменения тембра (растяжение rubberband / atempo)."""
    exe = ffmpeg_exe()
    if not exe:
        raise RuntimeError("Нет ffmpeg (No module named 'imageio_ffmpeg') — запустите install.bat")
    with open(mp3_path, "rb") as f:
        tags = read_id3_text(f.read(65536))
    with open(mp3_path, "rb") as f:
        cover = prepare_cover(read_apic(f.read(8 * 1048576)))
    chaps = [(t, a / factor, b / factor) for t, a, b in read_chapters(mp3_path)]
    filters = subprocess.run([exe, "-hide_banner", "-filters"], capture_output=True, text=True, **_no_window()).stdout
    flt = f"rubberband=tempo={factor}" if "rubberband" in filters else f"atempo={factor}"
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-i", mp3_path, "-vn", "-af", flt,
           "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    tmp = out_path + ".part"
    title = (tags.get("TIT2", "") + f" (×{factor:g})").strip()
    progress(f"Делаю версию ×{factor:g}…")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **_no_window())
    enc = make_encoder(kbps)
    with open(tmp, "wb") as out:
        out.write(book_tag(title, tags.get("TALB", ""), tags.get("TPE1", ""), chaps, cover))
        while True:
            blk = proc.stdout.read(SR * 2 * 20)
            if not blk:
                break
            out.write(enc.encode(blk))
        out.write(enc.flush())
    proc.wait()
    if proc.returncode != 0:
        os.remove(tmp)
        raise RuntimeError("ffmpeg не смог ускорить файл: " + proc.stderr.read().decode("utf-8", "replace")[-400:])
    os.replace(tmp, out_path)
    return out_path


def mp3_bitrate(path: str) -> int:
    d = mp3_seconds(path)
    return int(round(os.path.getsize(path) * 8 / 1000 / d)) if d else 64


def post_formats(single: str, job: dict, status=lambda m: None) -> list[str]:
    """После готового цельного файла: M4B и ускоренные версии (по галочкам в окне сохранения)."""
    extra = []
    base = os.path.splitext(single)[0]
    k = int(job.get("bitrate") or mp3_bitrate(single))
    for f in job.get("speeds", []) or []:
        extra.append(speed_version(single, float(f), f"{base} (×{float(f):g}).mp3", k, status))
    if job.get("m4b"):
        extra.append(make_m4b(single, base + ".m4b", min(128, max(48, k)), status))
    return extra


# --- Telegram
TG_LIMIT = 48 * 1048576  # бот может отправить файл до 50 МБ


def tg_api(token: str, method: str, data=None, files=None, timeout=60):
    import requests
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=data or {}, files=files, timeout=timeout)
    try:
        j = r.json()
    except ValueError:
        raise RuntimeError(f"Telegram ответил странно (код {r.status_code})")
    if not j.get("ok"):
        desc = j.get("description", "")
        if r.status_code == 401:
            raise RuntimeError("Telegram: неверный токен бота (проверьте токен от @BotFather).")
        if "chat not found" in desc.lower():
            raise RuntimeError("Telegram: чат не найден — напишите боту /start и нажмите «Найти мой чат».")
        raise RuntimeError(f"Telegram: {desc or r.status_code}")
    return j["result"]


def split_mp3_for_tg(path: str, limit: int = TG_LIMIT) -> list[str]:
    """Режет большой MP3 на куски до 48 МБ по границам кадров (каждый кусок — со своим тегом)."""
    if os.path.getsize(path) <= limit:
        return [path]
    import tempfile
    with open(path, "rb") as f:
        data = f.read()
    tags = read_id3_text(data[:65536])
    cover = prepare_cover(read_apic(data[:8 * 1048576]), 300)
    audio = strip_id3(data)
    n = (len(audio) + limit - 1) // limit
    out, pos = [], 0
    td = tempfile.mkdtemp(prefix="chitalka_tg_")
    for k in range(1, n + 1):
        cut = min(len(audio), pos + len(audio) // n + 1) if k < n else len(audio)
        while k < n and cut < len(audio) - 1 and not (audio[cut] == 0xFF and (audio[cut + 1] & 0xE0) == 0xE0):
            cut += 1  # к началу ближайшего MP3-кадра
        p = os.path.join(td, f"{os.path.splitext(os.path.basename(path))[0]} — часть {k} из {n}.mp3")
        with open(p, "wb") as f:
            f.write(id3_tag(f"{tags.get('TIT2', '')} — часть {k} из {n}", tags.get("TALB", ""), tags.get("TPE1", ""),
                            f"{k}/{n}", cover))
            f.write(audio[pos:cut])
        out.append(p)
        pos = cut
    return out


def tg_send_book(token: str, chat: str, files: list[str], caption: str = "", status=lambda m: None):
    """Отправляет готовые MP3 в Telegram (большие — частями)."""
    tg_api(token, "sendMessage", {"chat_id": chat, "text": "🎧 " + caption})
    for fp in files:
        if not fp.lower().endswith(".mp3"):
            continue
        parts = split_mp3_for_tg(fp)
        for i, p in enumerate(parts, 1):
            status(f"📨 Отправляю в Telegram: {os.path.basename(p)} ({i} из {len(parts)})…")
            with open(p, "rb") as f:
                tags = read_id3_text(f.read(65536))
                f.seek(0)
                tg_api(token, "sendAudio", {"chat_id": chat, "title": tags.get("TIT2", "")[:60],
                                            "performer": tags.get("TPE1", "")[:60]},
                       files={"audio": (os.path.basename(p), f, "audio/mpeg")}, timeout=600)
            if p != fp:
                try:
                    os.remove(p)
                except OSError:
                    pass
