"""
Голос-копия по аудиозаписи (XTTS v2, офлайн).

Создание:  python clone.py "запись.mp3" "Имя голоса" [начало_мин] [длительность_мин]
Результат: voices/clone/<имя>.pt (отпечаток голоса) + <имя>.json (описание).
Сама запись после этого не нужна.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import threading
import time

import numpy as np

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CLONE_DIR = os.path.join(APP_DIR, "voices", "clone")
XTTS_NAME = "tts_models/multilingual/multi-dataset/xtts_v2"
SR = 24000


# ============================================================================ звук
def decode_mp3_part(path: str, start_sec: float, dur_sec: float, sr: int = 22050) -> np.ndarray:
    """Декодирует кусок большого MP3, не распаковывая весь файл."""
    import miniaudio

    with open(path, "rb") as f:
        head = f.read(4 * 1024 * 1024)
        size = os.fstat(f.fileno()).st_size
        d = miniaudio.decode(head, output_format=miniaudio.SampleFormat.FLOAT32, nchannels=1, sample_rate=sr)
        bytes_per_sec = len(head) / max(0.1, d.num_frames / sr)
        total_sec = size / bytes_per_sec
        start_sec = max(0.0, min(start_sec, total_sec - dur_sec - 1))
        f.seek(int(start_sec * bytes_per_sec))
        data = f.read(int(dur_sec * bytes_per_sec) + 65536)
    d = miniaudio.decode(data, output_format=miniaudio.SampleFormat.FLOAT32, nchannels=1, sample_rate=sr)
    return np.frombuffer(d.samples, dtype=np.float32)[: int(dur_sec * sr)].copy()


def load_any_audio(path: str, start_sec: float, dur_sec: float, sr: int = 22050) -> np.ndarray:
    if path.lower().endswith(".mp3"):
        return decode_mp3_part(path, start_sec, dur_sec, sr)
    import soundfile as sf
    with open(path, "rb") as fh:
        a, file_sr = sf.read(io.BytesIO(fh.read()), dtype="float32", always_2d=True)
    a = a.mean(axis=1)[int(start_sec * file_sr): int((start_sec + dur_sec) * file_sr)]
    from voices import resample
    return resample(a, file_sr, sr)


def split_on_pauses(a: np.ndarray, sr: int, min_len=4.0, max_len=14.0) -> list[np.ndarray]:
    """Режет речь по паузам на куски 4–14 с."""
    hop = int(0.02 * sr)
    n = len(a) // hop
    rms = np.sqrt(np.mean(a[: n * hop].reshape(n, hop) ** 2, axis=1) + 1e-10)
    db = 20 * np.log10(rms)
    thr = np.percentile(db, 20) + 6  # тишина — около нижних 20% громкости
    silent = db < thr
    cuts = [0]
    run = 0
    for i, s in enumerate(silent):
        run = run + 1 if s else 0
        if run == 12:  # пауза ≥ 0.24 с
            cuts.append((i - 6) * hop)
    cuts.append(len(a))
    segs, cur_start = [], cuts[0]
    for c in cuts[1:]:
        if (c - cur_start) / sr >= min_len:
            seg = a[cur_start:c]
            while len(seg) / sr > max_len:
                segs.append(seg[: int(max_len * sr)])
                seg = seg[int(max_len * sr):]
            if len(seg) / sr >= min_len:
                segs.append(seg)
            cur_start = c
    # убираем куски, где мало речи (музыка/тишина): доля громких кадров
    good = []
    for s in segs:
        k = len(s) // hop
        r = 20 * np.log10(np.sqrt(np.mean(s[: k * hop].reshape(k, hop) ** 2, axis=1) + 1e-10))
        voiced = np.mean(r > thr + 10)
        if 0.45 < voiced < 0.97:
            good.append(s)
    return good


def median_f0(a: np.ndarray, sr: int) -> float:
    """Средняя высота голоса (Гц) — чтобы понять, мужской он или женский."""
    frame, hop = int(0.04 * sr), int(0.02 * sr)
    lo, hi = int(sr / 400), int(sr / 70)
    f0s = []
    for i in range(0, len(a) - frame, hop * 3):
        x = a[i:i + frame]
        if np.sqrt(np.mean(x ** 2)) < 0.02:
            continue
        x = x - x.mean()
        c = np.correlate(x, x, "full")[frame - 1:]
        if c[0] <= 0:
            continue
        lag = lo + int(np.argmax(c[lo:hi]))
        if c[lag] / c[0] > 0.45:
            f0s.append(sr / lag)
    return float(np.median(f0s)) if f0s else 0.0


def guess_gender(a: np.ndarray, sr: int) -> str:
    f0 = median_f0(a, sr)
    return "" if f0 == 0 else ("m" if f0 < 165 else "f")


def save_voice(slug: str, name: str, gender: str, gpt, spk, **meta) -> str:
    import torch
    os.makedirs(CLONE_DIR, exist_ok=True)
    buf = io.BytesIO()
    torch.save({"gpt_cond_latent": gpt.cpu(), "speaker_embedding": spk.cpu()}, buf)
    with open(os.path.join(CLONE_DIR, slug + ".pt"), "wb") as f:
        f.write(buf.getvalue())
    with open(os.path.join(CLONE_DIR, slug + ".json"), "w", encoding="utf-8") as f:
        json.dump({"name": name, "gender": gender, "created": time.strftime("%Y-%m-%d %H:%M"), **meta},
                  f, ensure_ascii=False, indent=1)
    _latents.pop(slug, None)
    return "xtts:" + slug


# ============================================================================ модель
class Xtts:
    _model = None
    _lock = threading.Lock()
    device = "cpu"

    @classmethod
    def available(cls) -> bool:
        try:
            import importlib.util
            return importlib.util.find_spec("TTS") is not None and importlib.util.find_spec("torch") is not None
        except Exception:  # noqa: BLE001
            return False

    @classmethod
    def load(cls, status=lambda m: None):
        with cls._lock:
            if cls._model is not None:
                return cls._model
            os.environ.setdefault("COQUI_TOS_AGREED", "1")  # некоммерческая лицензия CPML — личное использование
            import torch
            import TTS.tts.models.xtts as xmod
            from TTS.tts.configs.xtts_config import XttsConfig
            from TTS.tts.models.xtts import Xtts as XttsModel
            from TTS.utils.manage import ModelManager

            # загрузка аудио без ffmpeg/torchcodec
            def _load_audio(audiopath, sampling_rate):
                a = load_any_audio(audiopath, 0, 600, sampling_rate)
                return torch.from_numpy(a).unsqueeze(0)
            xmod.load_audio = _load_audio

            status("Загружаю модель голоса-копии (первый раз скачивается ~1,9 ГБ)…")
            path, _, _ = ModelManager().download_model(XTTS_NAME)
            path = str(path)
            if not os.path.isdir(path):
                path = os.path.dirname(path)
            cfg = XttsConfig()
            cfg.load_json(os.path.join(path, "config.json"))
            model = XttsModel.init_from_config(cfg)
            model.load_checkpoint(cfg, checkpoint_dir=path, use_deepspeed=False)
            cls.device = "cuda" if torch.cuda.is_available() else "cpu"
            model.to(cls.device)
            model.eval()
            cls._model = model
            status("Модель голоса-копии загружена (" + ("видеокарта" if cls.device == "cuda" else "процессор") + ")")
            return model


def make_voice(src: str, name: str, start_min: float = 3.0, minutes: float = 10.0,
               progress=lambda m: print(m, flush=True), gender: str = "m") -> str:
    """Снимает «отпечаток» голоса с записи и сохраняет его. Возвращает id голоса."""
    import torch

    progress(f"Читаю {minutes:g} мин записи с {start_min:g}-й минуты…")
    a = load_any_audio(src, start_min * 60, minutes * 60, 22050)
    if len(a) < 22050 * 20:
        raise ValueError("Слишком короткая запись: нужно хотя бы 20 секунд речи.")
    segs = split_on_pauses(a, 22050)
    progress(f"Найдено фрагментов речи: {len(segs)}")
    if len(segs) < 3:
        raise ValueError("В записи не найдено достаточно чистой речи.")
    model = Xtts.load(progress)
    dev = Xtts.device
    progress("Снимаю отпечаток тембра…")
    with torch.no_grad():
        embs = [model.get_speaker_embedding(torch.from_numpy(s).unsqueeze(0).to(dev), 22050) for s in segs]
        spk = torch.stack(embs).mean(dim=0)
        # манера речи: до 60 с из разных мест записи
        pick = segs[:: max(1, len(segs) // 8)][:8]
        full = torch.from_numpy(np.concatenate(pick)).unsqueeze(0).to(dev)
        gpt = model.get_gpt_cond_latents(full, 22050, length=60, chunk_length=6)
    slug = re.sub(r"[^\w\-]+", "_", name, flags=re.U).strip("_") or "голос"
    if gender not in ("m", "f"):
        gender = guess_gender(a, 22050)
    vid = save_voice(slug, name, gender, gpt, spk, source=os.path.basename(src), start_min=start_min,
                     minutes=minutes, segments=len(segs))
    progress(f"Голос «{name}» готов.")
    return vid


def list_clones() -> list[tuple[str, str, str, str]]:
    out = []
    if not os.path.isdir(CLONE_DIR):
        return out
    for fn in sorted(os.listdir(CLONE_DIR)):
        if fn.endswith(".json") and os.path.isfile(os.path.join(CLONE_DIR, fn[:-5] + ".pt")):
            try:
                with open(os.path.join(CLONE_DIR, fn), encoding="utf-8") as f:
                    meta = json.load(f)
                out.append((fn[:-5], meta.get("name", fn[:-5]), meta.get("gender", ""), meta.get("kind", "clone")))
            except Exception:  # noqa: BLE001
                pass
    return out


_latents: dict[str, tuple] = {}


def split_for_xtts(text: str, limit: int = 180) -> list[str]:
    """XTTS для русского берёт не больше ~180 символов за раз."""
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for piece in re.split(r"(?<=[.!?…;:,—])\s+", text):
        while len(piece) > limit:
            cut = piece.rfind(" ", 0, limit)
            cut = cut if cut > 20 else limit
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(piece[:cut])
            piece = piece[cut:].lstrip()
        if len(cur) + len(piece) + 1 <= limit:
            cur = (cur + " " + piece).strip()
        else:
            if cur:
                parts.append(cur)
            cur = piece
    if cur:
        parts.append(cur)
    return parts


def synth(text: str, slug: str, speed: float = 1.0, pitch: int = 0, status=lambda m: None) -> np.ndarray:
    import torch
    from voices import tempo_pitch

    model = Xtts.load(status)
    if slug not in _latents:
        with open(os.path.join(CLONE_DIR, slug + ".pt"), "rb") as f:
            d = torch.load(io.BytesIO(f.read()), map_location="cpu")
        _latents[slug] = (d["gpt_cond_latent"].to(Xtts.device), d["speaker_embedding"].to(Xtts.device))
    gpt, spk = _latents[slug]
    own_speed = min(1.25, max(0.8, speed))
    out = []
    from voices import job_valid, Cancelled
    for part in split_for_xtts(text):
        with Xtts._lock, torch.no_grad():
            if not job_valid():  # голос или место чтения сменили — не тратим время
                raise Cancelled()
            r = model.inference(part, "ru", gpt, spk, temperature=0.65, length_penalty=1.0,
                                repetition_penalty=5.0, top_k=50, top_p=0.85, speed=own_speed,
                                enable_text_splitting=False)
            w = r["wav"]
            w = w.cpu().numpy() if hasattr(w, "cpu") else np.asarray(w)
            out.append(w.astype(np.float32))
            out.append(np.zeros(int(0.08 * SR), np.float32))
    a = np.concatenate(out) if out else np.zeros(0, np.float32)
    peak = float(np.abs(a).max()) if len(a) else 0
    if peak > 0.95:
        a *= 0.95 / peak
    return tempo_pitch(a, SR, speed / own_speed, pitch)


if __name__ == "__main__":
    src, nm = sys.argv[1], sys.argv[2]
    st = float(sys.argv[3]) if len(sys.argv) > 3 else 3
    mn = float(sys.argv[4]) if len(sys.argv) > 4 else 10
    print(make_voice(src, nm, st, mn))
