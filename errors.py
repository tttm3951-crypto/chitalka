"""
Понятные объяснения ошибок: что случилось, почему и что сделать.

explain(exc, context) -> Problem(title, cause, fix, actions)
actions — список кодов действий, которые окно ошибки предлагает кнопками
(сами действия выполняет reader.pyw).
"""
from __future__ import annotations

import errno
from dataclasses import dataclass, field


@dataclass
class Problem:
    title: str
    cause: str
    fix: str
    actions: list = field(default_factory=list)  # коды: "offline_voice", "other_voice", "choose_folder", ...


ACTION_LABELS = {
    "offline_voice": "💾 Читать офлайн-голосом",
    "other_voice": "🎙 Выбрать другой голос",
    "choose_folder": "📂 Выбрать другую папку",
    "remove_book": "✖ Убрать книгу из очереди",
    "locate_book": "🔎 Найти файл книги",
    "retry": "↻ Повторить",
    "fewer_parallel": "⬇ Сохранять по 1 книге",
    "open_log": "📄 Открыть журнал ошибок",
    "restart_audio": "🔊 Переподключить звук",
    "run_install": "⚙ Запустить установку",
    "open_folder": "📂 Открыть папку",
    "ocr": "🔎 Распознать текст (OCR)",
}


def _msg(e) -> str:
    try:
        return f"{type(e).__name__}: {e}".lower()
    except Exception:  # noqa: BLE001
        return type(e).__name__.lower()


def _chain(e):
    """Исключение и все его причины (raise ... from ...)."""
    seen = []
    while e is not None and e not in seen:
        seen.append(e)
        e = e.__cause__ or e.__context__
    return seen


def explain(e: BaseException | str, context: str = "") -> Problem:
    if isinstance(e, str):
        text = e.lower()
        chain = []
        name = ""
    else:
        chain = _chain(e)
        text = " | ".join(_msg(x) for x in chain)
        name = type(e).__name__
    names = {type(x).__name__ for x in chain} | {name}

    def has(*words):
        return any(w in text for w in words)

    # --- книга
    if "BookError" in names:
        if has("скан", "текстового слоя"):
            return Problem("В книге нет текста — это скан",
                           "PDF или DJVU состоит из картинок страниц, текста в нём нет.",
                           "Нажмите «Распознать текст»: Читалка сама превратит страницы в текст встроенным "
                           "распознаванием Windows (несколько минут, один раз для каждой книги). "
                           "Либо найдите эту книгу в формате FB2/EPUB.",
                           ["ocr", "remove_book"])
        if has("drm", "защит"):
            return Problem("Книга защищена (DRM)", str(e), "Нужна версия книги без защиты (FB2, EPUB, PDF).",
                           ["remove_book"])
        if has("calibre", "djvulibre"):
            return Problem("Для этого формата нужна дополнительная программа", str(e),
                           "Установите бесплатную программу по ссылке из сообщения и откройте книгу снова.",
                           ["remove_book"])
        if has("не найден"):
            return Problem("Файл книги не найден", str(e),
                           "Книгу переместили, переименовали или удалили. Найдите её или уберите из очереди.",
                           ["locate_book", "remove_book"])
        return Problem("Книгу не удалось открыть", str(e),
                       "Возможно, файл повреждён или недокачан. Попробуйте другой файл этой книги "
                       "(лучше FB2 или EPUB).", ["remove_book"])

    # --- файлы и папки
    if isinstance(e, BaseException):
        oserr = next((x for x in chain if isinstance(x, OSError)), None)
        if oserr is not None:
            no = getattr(oserr, "errno", None)
            win = getattr(oserr, "winerror", None)
            fn = getattr(oserr, "filename", "") or ""
            if no == errno.ENOSPC or win == 112:
                return Problem("На диске закончилось место",
                               f"Не хватило места при записи{': ' + str(fn) if fn else ''}.",
                               "Освободите место (очистите Корзину, удалите ненужные файлы) или выберите "
                               "папку на другом диске. Сохранение потом можно продолжить — готовые файлы останутся.",
                               ["choose_folder", "retry"])
            if no in (errno.EACCES, errno.EPERM) or win in (5, 32, 33):
                if win in (32, 33):
                    return Problem("Файл занят другой программой",
                                   f"Файл открыт в другой программе: {fn}",
                                   "Закройте плеер или программу, которая держит этот файл, и повторите.",
                                   ["retry"])
                return Problem("Нет доступа к папке или файлу",
                               f"Windows не разрешает запись: {fn}",
                               "Выберите другую папку (например, в «Документах» или в библиотеке) "
                               "или закройте программу, которая держит файл.", ["choose_folder", "retry"])
            if no == errno.ENOENT or win in (2, 3):
                return Problem("Файл или папка не найдены", f"Не найдено: {fn}",
                               "Проверьте, что диск подключён и файл не перемещён.", ["locate_book", "remove_book"])
            if win in (21, 53, 64, 67, 1167, 1231) or no in (errno.ENODEV, errno.ENXIO, errno.EIO):
                return Problem("Диск или сетевая папка недоступны",
                               f"Устройство не отвечает: {fn or oserr}",
                               "Проверьте, что флешка/внешний диск подключены или есть доступ к сети, "
                               "затем повторите — или выберите папку на диске компьютера.",
                               ["choose_folder", "retry"])
            if win == 206 or no == errno.ENAMETOOLONG:
                return Problem("Слишком длинный путь к файлу", f"Путь длиннее, чем разрешает Windows: {fn}",
                               "Выберите папку поближе к корню диска (например, C:\\Аудиокниги).", ["choose_folder"])

            if not isinstance(oserr, (ConnectionError, TimeoutError)) and type(oserr).__name__ not in (
                    "ClientConnectorError", "ClientOSError", "ServerDisconnectedError", "gaierror"):
                return Problem("Не удалось записать или прочитать файл",
                               f"Windows вернула ошибку для {fn or 'файла'}: {oserr.strerror or oserr}",
                               "Проверьте, что папка существует и вы можете в неё записывать (это не файл, "
                               "не защищённая системная папка). Выберите другую папку и повторите.",
                               ["choose_folder", "retry"])

    # --- память
    if "MemoryError" in names or has("out of memory", "cuda error: out of memory", "not enough memory"):
        if has("cuda"):
            return Problem("Не хватило памяти видеокарты",
                           "Нейросетевой голос (XTTS/Silero) не поместился в память видеокарты.",
                           "Закройте игры и программы, использующие видеокарту, или сохраняйте по одной книге. "
                           "Можно выбрать онлайн-голос — он не нагружает видеокарту.",
                           ["fewer_parallel", "other_voice", "retry"])
        return Problem("Не хватило оперативной памяти",
                       "Компьютеру не хватило памяти (она была почти полностью занята).",
                       "Закройте лишние вкладки браузера и программы, затем повторите. "
                       "При сохранении в MP3 уменьшите число книг одновременно.", ["fewer_parallel", "retry"])

    # --- онлайн-голоса
    if has("запасной голос тоже не сработал"):
        return Problem("Не сработал ни онлайн-голос, ни запасной",
                       "Онлайн-голос Microsoft не ответил, а запасной офлайн-голос не смог прочитать эту фразу.",
                       "Выберите в «Без интернета» другой запасной голос (например, Silero) или другой основной "
                       "голос. Если нет интернета — включите офлайн-голос основным.",
                       ["offline_voice", "other_voice", "retry"])
    if has("запасной офлайн-голос не выбран"):
        return Problem("Онлайн-голос недоступен, запасного нет",
                       "Нет связи с сервером Microsoft, а в поле «Без интернета» не выбран запасной голос.",
                       "Включите офлайн-голос или выберите запасной голос в поле «Без интернета».",
                       ["offline_voice", "retry"])

    if has("голоса у microsoft больше нет"):
        return Problem("Такого онлайн-голоса больше нет",
                       "Microsoft убрал или переименовал этот голос — сервер его не знает.",
                       "Выберите другой голос в списке. Список голосов обновится при следующем запуске install.bat.",
                       ["other_voice", "offline_voice"])
    if has("noaudioreceived", "не отвечает у microsoft"):
        return Problem("Онлайн-голос сейчас не отвечает",
                       "Сервер Microsoft временно не отдаёт звук этим голосом (бывает с отдельными голосами "
                       "или при большой нагрузке).",
                       "Подождите несколько минут или выберите другой голос. Для чтения программа может сама "
                       "перейти на офлайн-голос.", ["offline_voice", "other_voice", "retry"])
    if has("clientconnector", "cannot connect", "getaddrinfo", "name resolution", "нет связи", "нет подключения",
           "network is unreachable", "connection reset", "connectionreset", "timeouterror", "не ответил вовремя",
           "serverdisconnected", "wsserverhandshake", "403", "429"):
        return Problem("Нет связи с сервером голосов",
                       "Онлайн-голосу нужен интернет, а сервер Microsoft не ответил (нет интернета, "
                       "медленная сеть или временная блокировка из-за большого числа запросов).",
                       "Проверьте интернет. Можно читать офлайн-голосом (Silero, Piper, Windows) — "
                       "они работают без сети. Сохранение в MP3 продолжится с места остановки.",
                       ["offline_voice", "retry"])

    # --- голоса и движки
    if "OfflineVoiceMissing" in names or has("не скачан", "не установлен"):
        return Problem("Голос не установлен", str(chain[0]) if chain else str(e),
                       "Запустите install.bat в папке программы (нужен интернет один раз) или выберите другой голос.",
                       ["run_install", "other_voice"])
    if has("no module named"):
        mod = text.split("no module named")[-1].strip(" '\"|")
        return Problem("Не хватает компонента программы", f"Не найден компонент «{mod}».",
                       "Запустите install.bat в папке программы — он доустановит недостающее.", ["run_install"])
    if has("portaudio", "sounddevice", "audio device", "invalid device", "error opening outputstream",
           "unanticipated host error", "device unavailable"):
        return Problem("Нет звука: звуковое устройство недоступно",
                       "Колонки или наушники отключены, выбрано другое устройство или звук занят другой программой.",
                       "Подключите колонки/наушники, проверьте громкость Windows и нажмите «Переподключить звук». "
                       "Сохранять в MP3 можно и без звука.", ["restart_audio"])
    if has("torch", "cuda") and has("driver", "device-side", "cublas", "cudnn"):
        return Problem("Ошибка видеокарты", "Драйвер видеокарты вернул ошибку при работе нейросетевого голоса.",
                       "Обновите драйвер NVIDIA, перезапустите программу. Пока можно выбрать онлайн- или Piper-голос.",
                       ["other_voice", "retry"])
    if has("слишком короткая", "чистой речи"):
        return Problem("Для голоса-копии мало речи", str(chain[0]) if chain else str(e),
                       "Выберите отрывок, где 5–10 минут говорит один человек без музыки, "
                       "или укажите другую минуту начала.", [])
    if "Cancelled" in names:
        return Problem("Операция отменена", "Вы сменили голос или место чтения.", "Ничего делать не нужно.", [])

    # --- общий случай
    detail = str(chain[0]) if chain else str(e)
    return Problem("Непредвиденная ошибка",
                   detail or name or "без описания",
                   "Попробуйте повторить. Если ошибка повторяется — откройте журнал ошибок "
                   "(подробности записаны в error.log) и пришлите его, чтобы её исправить.",
                   ["retry", "open_log"])
