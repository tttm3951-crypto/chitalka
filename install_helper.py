"""Установка компонентов Читалки. Запускается установщиком, программой (окно «Компоненты») и install.bat.

    python install_helper.py --components core,piper_ru,silero [--shortcut]
    python install_helper.py --setup          (как раньше: всё основное)

Строки, начинающиеся с @@, читает окно установки:
    @@STEP текст      — что делается сейчас
    @@PCT 0..100      — ход скачивания текущего шага
    @@OK id / @@FAIL id причина — итог по компоненту
"""
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request

APP = os.path.dirname(os.path.abspath(__file__))
VOICES = os.path.join(APP, "voices")
TOOLS = os.path.join(APP, "tools")
INSTALLED = os.path.join(APP, "installed.json")
NOWIN = {"creationflags": 0x08000000} if sys.platform == "win32" else {}

PIPER_RU = [("ru", "ru_RU", "irina", "medium"), ("ru", "ru_RU", "denis", "medium"),
            ("ru", "ru_RU", "dmitri", "medium"), ("ru", "ru_RU", "ruslan", "medium")]
PIPER_MORE = [("kk", "kk_KZ", "issai", "high"), ("en", "en_US", "ryan", "high"),
              ("uk", "uk_UA", "ukrainian_tts", "medium")]
PIPER = PIPER_RU + PIPER_MORE  # для старых вызовов
PIPER_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/{f}/{c}/{n}/{q}/{c}-{n}-{q}{ext}?download=true"
SILERO_URLS = [
    "https://models.silero.ai/models/tts/ru/v5_ru.pt",
    "https://models.silero.ai/models/tts/ru/v4_ru.pt",
]
CLOUDFLARED_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
OCR_PKGS = ["winrt-runtime", "winrt-Windows.Media.Ocr", "winrt-Windows.Graphics.Imaging",
            "winrt-Windows.Storage.Streams", "winrt-Windows.Globalization", "winrt-Windows.Foundation",
            "winrt-Windows.Foundation.Collections"]
ORDER = ["core", "ffmpeg", "ocr", "piper_ru", "piper_more", "silero", "xtts", "phone"]


def say(msg):
    print(msg, flush=True)


def step(msg):
    say("@@STEP " + msg)


def pct(p):
    say(f"@@PCT {int(p)}")


# ----------------------------------------------------------------------------- учёт установленного
def load_installed() -> dict:
    try:
        with open(INSTALLED, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
    except (OSError, ValueError):
        pass
    return {"components": []}


def mark_installed(cid, **extra):
    d = load_installed()
    comps = set(d.get("components", []))
    comps.add(cid)
    d["components"] = [c for c in ORDER if c in comps]
    d.update(extra)
    tmp = INSTALLED + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, INSTALLED)


# ----------------------------------------------------------------------------- скачивание и pip
def download(url, dest, label):
    if os.path.isfile(dest) and os.path.getsize(dest) > 1000:
        say(f"  уже есть: {label}")
        return True
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 Chitalka"})
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done, last = 0, -1
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                f.write(b)
                done += len(b)
                if total:
                    p = done * 100 // total
                    if p != last:
                        pct(p)
                        last = p
                    if p // 10 != (done - len(b)) * 100 // total // 10:
                        say(f"  {label}: {p}% из {total // 1048576} МБ")
        os.replace(tmp, dest)
        say(f"  готово: {label}")
        return True
    except Exception as e:  # noqa: BLE001
        say(f"  не удалось скачать {label}: {e}")
        if os.path.exists(tmp):
            os.remove(tmp)
        return False


def pip(*args) -> bool:
    """pip с ходом скачивания (крупные пакеты вроде torch качаются минутами)."""
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--progress-bar", "raw", *args]
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PIP_NO_INPUT="1")
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, **NOWIN)
    except OSError as e:
        say(f"  pip не запустился: {e}")
        return False
    last = -1
    tail = []
    for raw in p.stdout:
        line = raw.decode("utf-8", "replace").rstrip()
        m = re.match(r"Progress (\d+) of (\d+)", line)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            if total:
                v = done * 100 // total
                if v != last:
                    pct(v)
                    last = v
            continue
        if line.startswith(("Collecting", "Downloading")) and len(line) < 200:
            say("  " + line.replace("Collecting", "Получаю").replace("Downloading", "Скачиваю"))
        elif line.startswith("Successfully installed"):
            say("  установлено: " + line[len("Successfully installed "):][:300])
        elif "ERROR" in line or "error:" in line.lower():
            say("  " + line[:300])
        tail = (tail + [line])[-15:]
    code = p.wait()
    if code != 0:
        say("  pip завершился с ошибкой. Последние строки:\n    " + "\n    ".join(tail[-6:]))
    return code == 0


def has_gpu() -> bool:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return False
    try:
        return subprocess.call([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, **NOWIN) == 0
    except Exception:  # noqa: BLE001
        return False


def importable(mod) -> bool:
    return subprocess.call([sys.executable, "-c", f"import {mod}"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, **NOWIN) == 0


# ----------------------------------------------------------------------------- компоненты
def c_core():
    step("Основные компоненты программы")
    subprocess.call([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check",
                     "--upgrade", "pip"], **NOWIN)
    if not pip("-r", os.path.join(APP, "requirements.txt")):
        raise RuntimeError("не удалось установить основные пакеты — проверьте интернет")
    step("Список онлайн-голосов Microsoft")
    try:
        sys.path.insert(0, APP)
        import voices
        if voices.refresh_edge_catalog(timeout=30):
            with open(voices.EDGE_CACHE, encoding="utf-8") as f:
                say(f"  голосов: {len(json.load(f))}")
        else:
            say("  не удалось (нет интернета?) — будет встроенный список")
    except Exception as e:  # noqa: BLE001
        say(f"  пропущено: {e}")


def _piper(lst, title):
    step(title)
    os.makedirs(VOICES, exist_ok=True)
    bad = []
    for fam, code, name, q in lst:
        base = os.path.join(VOICES, f"{code}-{name}-{q}")
        ok = download(PIPER_URL.format(f=fam, c=code, n=name, q=q, ext=".onnx.json"), base + ".onnx.json",
                      f"{name} (настройки)")
        if not (ok and download(PIPER_URL.format(f=fam, c=code, n=name, q=q, ext=".onnx"), base + ".onnx", name)):
            bad.append(name)
    if len(bad) == len(lst):
        raise RuntimeError("не скачался ни один голос")
    if bad:
        say("  не скачались: " + ", ".join(bad) + " — повторите позже")


def c_piper_ru():
    _piper(PIPER_RU, "Офлайн-голоса Piper (русские)")


def c_piper_more():
    _piper(PIPER_MORE, "Офлайн-голоса Piper (казахский, английский, украинский)")


def _torch():
    if importable("torch"):
        say("  движок нейросетей (torch) уже установлен")
        return
    gpu = has_gpu()
    step("Движок нейросетей torch — " + ("для видеокарты NVIDIA (≈ 2,5 ГБ)" if gpu else "для процессора (≈ 200 МБ)"))
    url = "https://download.pytorch.org/whl/" + ("cu128" if gpu else "cpu")
    if not pip("torch", "torchaudio", "--index-url", url):
        raise RuntimeError("не удалось установить torch")
    mark_installed("torch", torch="cu128" if gpu else "cpu")


def c_silero():
    _torch()
    step("Голоса Silero")
    d = os.path.join(VOICES, "silero")
    os.makedirs(d, exist_ok=True)
    for url in SILERO_URLS:
        dest = os.path.join(d, os.path.basename(url))
        if not download(url, dest, os.path.basename(url)):
            continue
        code = ("import io,json,sys,torch\n"
                "b=open(sys.argv[1],'rb').read()\n"  # через байты: torch не открывает пути с кириллицей
                "m=torch.package.PackageImporter(io.BytesIO(b)).load_pickle('tts_models','model')\n"
                "s=list(getattr(m,'speakers',[]) or [])\n"
                "json.dump(s,open(sys.argv[2],'w',encoding='utf-8'),ensure_ascii=False)\n"
                "print(', '.join(s))")
        r = subprocess.run([sys.executable, "-c", code, dest, os.path.join(d, "speakers.json")],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", **NOWIN)
        if r.returncode == 0:
            say("  голоса: " + r.stdout.strip())
            for other in os.listdir(d):  # оставляем одну (самую новую) модель
                if other.endswith(".pt") and other != os.path.basename(dest):
                    os.remove(os.path.join(d, other))
            return
        say(f"  модель {os.path.basename(url)} не загрузилась: {(r.stderr or '').strip()[-300:]}")
        os.remove(dest)
    raise RuntimeError("модель Silero не скачалась")


def c_xtts():
    _torch()
    step("Голоса-копии XTTS v2")
    if not (pip("coqui-tts", "six") and pip("--no-deps", "torchcodec")):
        raise RuntimeError("не удалось установить XTTS")
    say("  модель (~1,8 ГБ) скачается при первом создании голоса-копии")


def c_ffmpeg():
    step("ffmpeg для M4B и ускоренных версий")
    if not pip("imageio-ffmpeg"):
        raise RuntimeError("не удалось установить imageio-ffmpeg")
    subprocess.call([sys.executable, "-c", "import imageio_ffmpeg as m; print(m.get_ffmpeg_exe())"], **NOWIN)


def c_ocr():
    step("Распознавание сканов (OCR Windows)")
    if sys.platform != "win32":
        say("  только для Windows")
        return
    if not pip(*OCR_PKGS):
        raise RuntimeError("не удалось установить пакеты распознавания")
    r = subprocess.run([sys.executable, "-c",
                        "from winrt.windows.media.ocr import OcrEngine\n"
                        "from winrt.windows.globalization import Language\n"
                        "print(OcrEngine.is_language_supported(Language('ru')))"],
                       capture_output=True, text=True, **NOWIN)
    if "True" not in r.stdout:
        say("  ВНИМАНИЕ: в Windows нет русского языкового пакета распознавания.\n"
            "  Параметры Windows → Время и язык → Язык → Русский → Распознавание текста.")


def c_phone():
    step("Доступ с телефона из интернета (cloudflared)")
    if sys.platform != "win32":
        say("  на этой системе поставьте cloudflared вручную")
        return
    if not download(CLOUDFLARED_URL, os.path.join(TOOLS, "cloudflared.exe"), "cloudflared.exe"):
        raise RuntimeError("не скачался cloudflared")


COMPONENTS = {"core": c_core, "piper_ru": c_piper_ru, "piper_more": c_piper_more, "silero": c_silero,
              "xtts": c_xtts, "ffmpeg": c_ffmpeg, "ocr": c_ocr, "phone": c_phone}


def shortcut():
    step("Ярлыки на рабочем столе и в меню «Пуск»")
    if sys.platform != "win32":
        return
    try:
        import win32com.client
        shell = win32com.client.Dispatch("WScript.Shell")
        places = [shell.SpecialFolders("Desktop"), shell.SpecialFolders("Programs")]
        for d in places:
            lnk = shell.CreateShortcut(os.path.join(d, "Читалка.lnk"))
            lnk.TargetPath = os.path.join(APP, ".venv", "Scripts", "pythonw.exe")
            lnk.Arguments = f'"{os.path.join(APP, "reader.pyw")}"'
            lnk.WorkingDirectory = APP
            ico = os.path.join(APP, "chitalka.ico")
            lnk.IconLocation = ico if os.path.isfile(ico) else r"%SystemRoot%\System32\imageres.dll,186"
            lnk.Description = "Чтение книг вслух и аудиокниги"
            lnk.Save()
        say("  созданы ярлыки «Читалка»")
    except Exception as e:  # noqa: BLE001
        say(f"  не удалось: {e}")


def run(comps, make_shortcut=False) -> int:
    comps = [c for c in ORDER if c in comps]
    fails = 0
    for i, cid in enumerate(comps):
        say(f"@@COMP {cid} {i + 1} {len(comps)}")
        try:
            COMPONENTS[cid]()
            mark_installed(cid)
            say(f"@@OK {cid}")
        except Exception as e:  # noqa: BLE001
            fails += 1
            say(f"@@FAIL {cid} {e}")
            if cid == "core":
                return 1
    if make_shortcut:
        shortcut()
    say("@@END " + ("ok" if not fails else f"fail {fails}"))
    return 1 if fails else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    args = sys.argv[1:]
    comps = []
    if "--components" in args:
        comps = [c.strip() for c in args[args.index("--components") + 1].split(",") if c.strip()]
    if "--setup" in args:  # прежний install.bat: основное + рекомендованное
        comps = ["core", "ffmpeg", "ocr", "piper_ru", "piper_more", "silero", "xtts"]
        if "--no-piper" in args:
            comps = [c for c in comps if not c.startswith("piper")]
        if "--no-silero" in args:
            comps = [c for c in comps if c not in ("silero", "xtts")]
    if "--only-silero" in args:
        comps = ["silero"]
    bad = [c for c in comps if c not in COMPONENTS]
    if bad:
        say("Неизвестные компоненты: " + ", ".join(bad))
        sys.exit(2)
    code = run(comps, make_shortcut=("--shortcut" in args or "--setup" in args) and "--no-shortcut" not in args)
    if "--setup" in args:
        say("\nУстановка завершена. Запуск: ярлык «Читалка» на рабочем столе или start.bat")
    sys.exit(code)
