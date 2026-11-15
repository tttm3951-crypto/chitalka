"""
Извлечение текста из книг: FB2, FB2.ZIP, EPUB, PDF, DOCX, ODT, TXT, RTF, HTML,
MOBI/AZW/AZW3, DJVU и прочее (через Calibre, если установлен).

Результат — объект Book: название, автор, список абзацев и список глав
(название главы + индекс абзаца, с которого она начинается).
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass, field


class BookError(Exception):
    """Понятная пользователю ошибка открытия книги."""


@dataclass
class Book:
    path: str
    title: str = ""
    author: str = ""
    paragraphs: list[str] = field(default_factory=list)
    chapters: list[tuple[str, int]] = field(default_factory=list)  # (название, индекс абзаца)
    cover: bytes | None = None  # картинка обложки (JPEG/PNG), если есть


def _short_note(t: str, limit: int = 350) -> str:
    """Сноска «коротко»: до ~350 знаков, обрезаем по концу предложения."""
    t = _clean(t)
    if len(t) <= limit:
        return t
    cut = max(t.rfind(". ", 0, limit), t.rfind("! ", 0, limit), t.rfind("? ", 0, limit))
    return (t[:cut + 1] if cut > limit // 3 else t[:limit].rsplit(" ", 1)[0] + "…")


SUPPORTED = (
    ".fb2", ".fb2.zip", ".epub", ".pdf", ".docx", ".odt", ".txt", ".rtf",
    ".html", ".htm", ".xhtml", ".mobi", ".azw", ".azw3", ".prc", ".djvu", ".djv",
    ".doc", ".md",
)

_WS = re.compile(r"[ \t\u00a0\u2009\u200b\r\f\v]+")


def _clean(s: str) -> str:
    s = s.replace("\u00ad", "")  # мягкие переносы
    return _WS.sub(" ", s).strip()


def _add_par(book: Book, text: str):
    text = _clean(text)
    if text and re.search(r"\w", text):
        book.paragraphs.append(text)


def _add_chapter(book: Book, title: str):
    title = _clean(title)
    if not title:
        return
    idx = len(book.paragraphs)
    # не плодим главы, указывающие на одно и то же место
    if book.chapters and book.chapters[-1][1] == idx:
        book.chapters[-1] = (book.chapters[-1][0] + ". " + title, idx)
    else:
        book.chapters.append((title[:120], idx))


# ----------------------------------------------------------------------------- FB2
def _local(tag) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _load_fb2_bytes(data: bytes, path: str, notes: str = "skip") -> Book:
    from lxml import etree

    parser = etree.XMLParser(recover=True, huge_tree=True, resolve_entities=False)
    try:
        root = etree.fromstring(data, parser)
    except Exception as e:  # noqa: BLE001
        raise BookError(f"Не удалось разобрать FB2: {e}")
    if root is None:
        raise BookError("Файл FB2 повреждён.")

    book = Book(path)

    def first(el, name):
        for ch in el.iter():
            if _local(ch.tag) == name:
                return ch
        return None

    desc = first(root, "title-info")
    if desc is not None:
        bt = first(desc, "book-title")
        if bt is not None:
            book.title = _clean("".join(bt.itertext()))
        a = first(desc, "author")
        if a is not None:
            parts = []
            for n in ("last-name", "first-name", "middle-name"):
                e = first(a, n)
                if e is not None and e.text:
                    parts.append(e.text.strip())
            book.author = " ".join(parts)

    # сноски: id -> текст (из <body name="notes">)
    note_text: dict[str, str] = {}
    if notes == "inline":
        for body in root:
            if _local(body.tag) == "body" and (body.get("name") or "").lower() in ("notes", "comments", "footnotes"):
                for sec in body.iter():
                    if _local(sec.tag) == "section" and sec.get("id"):
                        ps = [" ".join("".join(p.itertext()).split()) for p in sec
                              if _local(p.tag) in ("p", "poem", "cite", "v")]
                        note_text[sec.get("id")] = " ".join(x for x in ps if x)
    refs: list[str] = []

    def text_of(el) -> str:
        """Текст элемента без ссылок на сноски (номера сносок запоминаем в refs)."""
        out = []

        def walk(e):
            name = _local(e.tag)
            if name == "a":
                t = (e.get("type") or "").lower()
                href = ""
                for k, v in e.attrib.items():
                    if k.endswith("href"):
                        href = v
                txt = "".join(e.itertext()).strip()
                if t == "note" or (href.startswith("#") and re.fullmatch(r"[\[\(\{]?\*?\d*\*?[\]\)\}]?", txt or "")):
                    if href.startswith("#"):
                        refs.append(href[1:])
                    if e.tail:
                        out.append(e.tail)
                    return
            if e.text:
                out.append(e.text)
            for c in e:
                walk(c)
            if e.tail and e is not el:
                out.append(e.tail)

        walk(el)
        return "".join(out)

    def walk_section(sec, depth):
        for ch in sec:
            name = _local(ch.tag)
            if name == "title":
                lines = [_clean(text_of(p)) for p in ch if _local(p.tag) in ("p", "empty-line")]
                title = ". ".join(l for l in lines if l)
                if depth >= 1:
                    _add_chapter(book, title)
                _add_par(book, title)
            elif name == "section":
                walk_section(ch, depth + 1)
            elif name in ("p", "subtitle", "text-author", "v"):
                refs.clear()
                _add_par(book, text_of(ch))
                for rid in refs:
                    if note_text.get(rid):
                        _add_par(book, "Примечание: " + _short_note(note_text[rid]))
            elif name in ("epigraph", "cite", "poem", "stanza", "annotation"):
                walk_section(ch, depth)
            elif name in ("image", "empty-line", "binary"):
                continue
            elif name == "table":
                for row in ch.iter():
                    if _local(row.tag) == "tr":
                        _add_par(book, " ; ".join(_clean(text_of(c)) for c in row))
            else:
                if len(ch):
                    walk_section(ch, depth)
                else:
                    _add_par(book, text_of(ch))

    # обложка: <coverpage><image l:href="#cover.jpg"/> -> <binary id="cover.jpg">
    try:
        import base64
        cp = first(root, "coverpage")
        img = first(cp, "image") if cp is not None else None
        if img is not None:
            href = next((v for k, v in img.attrib.items() if k.endswith("href")), "").lstrip("#")
            for b in root:
                if _local(b.tag) == "binary" and b.get("id") == href and b.text:
                    book.cover = base64.b64decode(b.text)
                    break
    except Exception:  # noqa: BLE001
        pass

    bodies = [e for e in root if _local(e.tag) == "body"]
    for body in bodies:
        if (body.get("name") or "").lower() in ("notes", "comments", "footnotes"):
            continue
        walk_section(body, 0)

    if not book.paragraphs:
        raise BookError("В FB2 не найден текст.")
    return book


def load_fb2(path: str, notes: str = "skip") -> Book:
    with open(path, "rb") as f:
        return _load_fb2_bytes(f.read(), path, notes)


def load_fb2_zip(path: str, notes: str = "skip") -> Book:
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".fb2")]
        if not names:
            raise BookError("В архиве нет файла .fb2")
        return _load_fb2_bytes(z.read(names[0]), path, notes)


# ----------------------------------------------------------------------------- HTML / EPUB
_BLOCK = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "dd", "dt", "pre",
          "blockquote", "div", "td", "th", "caption", "figcaption", "section", "article", "tr"}
_HEAD = {"h1", "h2", "h3", "h4"}


NOTE_MARK = "\ue000"  # временная метка «здесь была сноска №…» внутри абзаца


def _html_into_book(html: bytes | str, book: Book, chapter_hint: str | None = None, notes: dict | None = None):
    from bs4 import BeautifulSoup, NavigableString

    soup = BeautifulSoup(html, "lxml")
    for bad in soup(["script", "style", "head", "noscript", "svg", "math"]):
        bad.decompose()
    # ссылки на сноски: <a epub:type="noteref">, <sup><a>1</a></sup>, <a href="#n1">[1]</a>
    for a in soup.find_all("a"):
        et = (a.get("epub:type") or a.get("role") or "").lower()
        txt = a.get_text(strip=True)
        if "noteref" in et or (a.get("href", "").find("#") >= 0 and re.fullmatch(r"[\[\(\{]?\*?\d{0,4}\*?[\]\)\}]?", txt) and txt):
            frag = a.get("href", "").split("#")[-1]
            if notes is not None and notes.get(frag):
                a.replace_with(NavigableString(NOTE_MARK + frag + NOTE_MARK))
            else:
                a.decompose()
    for sup in soup.find_all("sup"):
        if re.fullmatch(r"[\[\(]?\*?\d{0,4}\*?[\]\)]?", sup.get_text(strip=True)):
            sup.decompose()
    for asideel in soup.find_all(["aside"]):
        et = (asideel.get("epub:type") or "").lower()
        if "footnote" in et or "endnote" in et or "rearnote" in et:
            asideel.decompose()
    for br in soup.find_all("br"):
        br.replace_with(NavigableString("\n"))

    body = soup.body or soup
    first_heading_done = False
    start_len = len(book.paragraphs)

    def has_block_child(el) -> bool:
        return any(getattr(c, "name", None) in _BLOCK for c in el.children)

    def add(txt):
        """Абзац; сноски из него — отдельными короткими абзацами сразу после."""
        if NOTE_MARK not in txt:
            _add_par(book, txt)
            return
        ids = re.findall(NOTE_MARK + r"(.*?)" + NOTE_MARK, txt)
        _add_par(book, re.sub(NOTE_MARK + r".*?" + NOTE_MARK, "", txt))
        for i in ids:
            _add_par(book, "Примечание: " + _short_note(notes[i]))

    def walk(el):
        nonlocal first_heading_done
        for c in el.children:
            name = getattr(c, "name", None)
            if name is None:
                if isinstance(c, NavigableString) and el is body:
                    add(str(c))
                continue
            if name in _BLOCK and not has_block_child(c):
                txt = c.get_text()
                if name in _HEAD:
                    _add_chapter(book, txt)
                    first_heading_done = True
                for line in txt.split("\n") if name == "pre" else [txt.replace("\n", " ")]:
                    add(line)
            elif name in _BLOCK or name in ("body", "html", "main", "nav", "header", "footer", "span", "font", "center", "table", "tbody", "thead", "ul", "ol", "dl"):
                if name in _HEAD:
                    _add_chapter(book, c.get_text())
                    first_heading_done = True
                walk(c)
            else:
                add(c.get_text())

    walk(body)
    if chapter_hint and not first_heading_done and len(book.paragraphs) > start_len:
        book.chapters.append((chapter_hint[:120], start_len))


def load_html(path: str) -> Book:
    book = Book(path)
    with open(path, "rb") as f:
        _html_into_book(f.read(), book)
    if not book.paragraphs:
        raise BookError("В HTML не найден текст.")
    return book


def load_epub(path: str, notes: str = "skip") -> Book:
    from lxml import etree
    import posixpath
    from urllib.parse import unquote

    book = Book(path)
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise BookError("EPUB повреждён (не zip-архив).")
    with z:
        try:
            cont = etree.fromstring(z.read("META-INF/container.xml"))
            opf_path = [e.get("full-path") for e in cont.iter() if _local(e.tag) == "rootfile"][0]
        except Exception:  # noqa: BLE001
            opfs = [n for n in z.namelist() if n.lower().endswith(".opf")]
            if not opfs:
                raise BookError("В EPUB не найдено описание книги (OPF).")
            opf_path = opfs[0]
        opf = etree.fromstring(z.read(opf_path), etree.XMLParser(recover=True))
        base = posixpath.dirname(opf_path)
        for e in opf.iter():
            n = _local(e.tag)
            if n == "title" and not book.title and e.text:
                book.title = e.text.strip()
            elif n == "creator" and not book.author and e.text:
                book.author = e.text.strip()
        manifest = {}
        for e in opf.iter():
            if _local(e.tag) == "item":
                manifest[e.get("id")] = (e.get("href"), e.get("media-type", ""), e.get("properties", ""))
        spine = [e.get("idref") for e in opf.iter() if _local(e.tag) == "itemref" and e.get("linear", "yes") != "no"]

        # названия глав из оглавления (toc.ncx или nav.xhtml)
        toc_titles: dict[str, str] = {}
        for _id, (href, mt, props) in manifest.items():
            try:
                full = posixpath.normpath(posixpath.join(base, unquote(href)))
                if mt == "application/x-dtbncx+xml":
                    ncx = etree.fromstring(z.read(full), etree.XMLParser(recover=True))
                    for np_ in ncx.iter():
                        if _local(np_.tag) == "navPoint":
                            label = next((t for t in np_.iter() if _local(t.tag) == "text"), None)
                            content = next((t for t in np_.iter() if _local(t.tag) == "content"), None)
                            if label is not None and content is not None:
                                src = posixpath.normpath(posixpath.join(posixpath.dirname(full), unquote(content.get("src", "").split("#")[0])))
                                toc_titles.setdefault(src, _clean("".join(label.itertext())))
            except Exception:  # noqa: BLE001
                pass

        # обложка: <meta name="cover" content="id"> или properties="cover-image"
        try:
            cid = next((e.get("content") for e in opf.iter() if _local(e.tag) == "meta"
                        and (e.get("name") or "").lower() == "cover"), None)
            cands = [manifest[cid]] if cid in manifest else []
            cands += [v for v in manifest.values() if "cover-image" in v[2]]
            cands += [v for k, v in manifest.items() if v[1].startswith("image/") and "cover" in (k + v[0]).lower()]
            for href, mt, _p in cands:
                if mt.startswith("image/"):
                    book.cover = z.read(posixpath.normpath(posixpath.join(base, unquote(href))))
                    break
        except Exception:  # noqa: BLE001
            pass

        note_map = None
        if notes == "inline":
            # тексты сносок из всех файлов книги: id элемента -> текст
            from bs4 import BeautifulSoup
            note_map = {}
            for idref in spine:
                if idref not in manifest:
                    continue
                try:
                    sp = BeautifulSoup(z.read(posixpath.normpath(posixpath.join(base, unquote(manifest[idref][0])))), "lxml")
                except Exception:  # noqa: BLE001
                    continue
                for el in sp.find_all(["aside", "li", "p", "div", "section", "dd"], id=True):
                    t = " ".join(el.get_text(" ").split())
                    t = re.sub(r"^\s*[\[\(]?\d{1,4}[\]\)]?\.?\s*", "", t)
                    if 2 < len(t) < 3000:
                        note_map.setdefault(el["id"], t)

        for idref in spine:
            if idref not in manifest:
                continue
            href = manifest[idref][0]
            full = posixpath.normpath(posixpath.join(base, unquote(href)))
            try:
                data = z.read(full)
            except KeyError:
                continue
            _html_into_book(data, book, chapter_hint=toc_titles.get(full), notes=note_map)
    if not book.paragraphs:
        raise BookError("В EPUB не найден текст (возможно, книга с DRM-защитой).")
    return book


# ----------------------------------------------------------------------------- PDF
def load_pdf(path: str) -> Book:
    try:
        import pymupdf as fitz
    except ImportError:  # старые версии
        import fitz  # type: ignore

    try:
        doc = fitz.open(path)
    except Exception as e:  # noqa: BLE001
        raise BookError(f"Не удалось открыть PDF: {e}")
    if doc.needs_pass:
        raise BookError("PDF защищён паролем.")
    book = Book(path)
    meta = doc.metadata or {}
    book.title = (meta.get("title") or "").strip()
    book.author = (meta.get("author") or "").strip()

    pages: list[list[list[str]]] = []  # страница -> блоки -> строки
    total_chars = 0
    for page in doc:
        blocks = []
        for b in page.get_text("blocks", sort=True):
            if b[6] != 0:  # не текст (картинка)
                continue
            lines = [l.strip() for l in b[4].split("\n")]
            lines = [l for l in lines if l]
            if lines:
                blocks.append(lines)
                total_chars += sum(len(l) for l in lines)
        pages.append(blocks)

    if total_chars < 50 * max(1, min(len(pages), 20)) and total_chars < 2000:
        raise BookError(
            "В этом PDF нет текстового слоя — это скан (картинки страниц).\n"
            "Читалка может распознать текст сама (OCR) — нажмите «Распознать текст»."
        )
    try:  # обложка — первая страница
        book.cover = doc[0].get_pixmap(matrix=fitz.Matrix(0.8, 0.8)).tobytes("png")
    except Exception:  # noqa: BLE001
        pass

    # --- колонтитулы: строки, повторяющиеся сверху/снизу на многих страницах
    def norm(s):
        return re.sub(r"\d+", "#", s.lower()).strip()

    edge_counter: Counter = Counter()
    for blocks in pages:
        cand = set()
        if blocks:
            for l in blocks[0][:2] + blocks[-1][-2:]:
                cand.add(norm(l))
        edge_counter.update(cand)
    n_pages = len(pages)
    junk = {k for k, v in edge_counter.items() if n_pages >= 4 and v >= max(3, n_pages * 0.3)}
    page_num_re = re.compile(r"^[\-–—\s]*(стр\.?|с\.|page)?\s*\d{1,4}\s*(из\s*\d+)?[\-–—\s]*$", re.I)

    # оглавление PDF -> главы
    toc_by_page: dict[int, list[str]] = {}
    try:
        parent = ""
        for level, title, pno in doc.get_toc(simple=True):
            title = _clean(title)
            if level == 1:
                parent = title
            if level <= 2 and pno >= 1:
                # «Часть первая · 3» вместо просто «3»
                if level == 2 and parent and len(title) <= 12:
                    title = f"{parent} · {title}"
                toc_by_page.setdefault(pno - 1, []).append(title)
    except Exception:  # noqa: BLE001
        pass

    def join_lines(lines: list[str]) -> list[str]:
        """Склеивает строки блока в абзацы; диалоги и короткие концовки начинают новый абзац."""
        maxlen = max(len(l) for l in lines)
        pars: list[str] = []
        out = ""
        prev = ""
        for l in lines:
            new_par = False
            if out:
                if re.match(r"^[–—\-]\s", l) and re.search(r"[.!?…:»\"]$", prev):
                    new_par = True
                elif re.search(r"[.!?…»\"]$", prev) and len(prev) < 0.7 * maxlen and maxlen > 40:
                    new_par = True
            if new_par:
                pars.append(out)
                out = l
            elif not out:
                out = l
            elif re.search(r"\w[\-\u00ad]$", out) and re.match(r"^[a-zа-яё]", l):
                out = out[:-1] + l  # перенос слова
            else:
                out += " " + l
            prev = l
        if out:
            pars.append(out)
        return pars

    pending = ""
    for pi, blocks in enumerate(pages):
        for title in toc_by_page.get(pi, []):
            if pending:
                _add_par(book, pending)
                pending = ""
            _add_chapter(book, title)
        cleaned_blocks = []
        for bi, lines in enumerate(blocks):
            lines = [l for l in lines if not page_num_re.match(l)]
            if bi in (0, len(blocks) - 1):
                lines = [l for l in lines if norm(l) not in junk]
            if lines:
                cleaned_blocks.append(lines)
        flat = [(bi, t) for bi, lines in enumerate(cleaned_blocks) for t in join_lines(lines)]
        for k, (bi, text) in enumerate(flat):
            if k == 0 and pending:
                # абзац перенесён со страницы на страницу
                if not re.search(r"[.!?…:»\"]$", pending) and re.match(r"^[a-zа-яё,;—–\-]", text):
                    if re.search(r"\w[\-\u00ad]$", pending):
                        pending = pending[:-1] + text
                    else:
                        pending = pending + " " + text
                    continue
            if pending:
                _add_par(book, pending)
            pending = text
    if pending:
        _add_par(book, pending)
    if not book.chapters:
        _detect_chapters(book)
    return book


# ----------------------------------------------------------------------------- DOCX / ODT
def load_docx(path: str) -> Book:
    import docx  # python-docx

    try:
        d = docx.Document(path)
    except Exception as e:  # noqa: BLE001
        raise BookError(f"Не удалось открыть DOCX: {e}")
    book = Book(path)
    cp = d.core_properties
    book.title = (cp.title or "").strip()
    book.author = (cp.author or "").strip()
    for p in d.paragraphs:
        style = (p.style.name if p.style is not None else "") or ""
        if re.match(r"(Heading|Заголовок|Title|Название)\s*[1-2]?$", style, re.I):
            _add_chapter(book, p.text)
        _add_par(book, p.text)
    if not book.paragraphs:
        raise BookError("В документе не найден текст.")
    return book


def load_odt(path: str) -> Book:
    from lxml import etree

    book = Book(path)
    with zipfile.ZipFile(path) as z:
        root = etree.fromstring(z.read("content.xml"))
    for e in root.iter():
        n = _local(e.tag)
        if n == "h":
            _add_chapter(book, "".join(e.itertext()))
            _add_par(book, "".join(e.itertext()))
        elif n == "p":
            _add_par(book, "".join(e.itertext()))
    return book


# ----------------------------------------------------------------------------- TXT / RTF
def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig",):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(data[:200000]).best()
        if best is not None:
            return data.decode(best.encoding, errors="replace")
    except Exception:  # noqa: BLE001
        pass
    return data.decode("cp1251", errors="replace")


_CHAPTER_RE = re.compile(
    r"^\s*((глава|часть|книга|запись|пролог|эпилог|послесловие|предисловие|chapter|part|prologue|epilogue|бөлім|тарау)\b.{0,80}|[IVXLC]{1,6}\.?|\d{1,3}\.?)\s*$",
    re.I,
)


def _detect_chapters(book: Book):
    """Главы по заголовкам вида «Глава 5», «Часть II», «Запись 3-я» (если оглавления нет)."""
    found = []
    for i, p in enumerate(book.paragraphs):
        if len(p) <= 90 and _CHAPTER_RE.match(p) and not re.fullmatch(r"\d{1,3}\.?", p.strip()):
            found.append((p[:120], i))
    if 2 <= len(found) <= max(3, len(book.paragraphs) // 5):
        book.chapters = found


def _text_into_book(text: str, book: Book):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    nonempty = [l for l in lines if l.strip()]
    blank_ratio = 1 - len(nonempty) / max(1, len(lines))
    # если абзацы разделены пустыми строками — склеиваем строки внутри абзаца
    if blank_ratio > 0.15:
        for chunk in re.split(r"\n\s*\n", text):
            ls = [l.strip() for l in chunk.split("\n") if l.strip()]
            if not ls:
                continue
            if len(ls) == 1 and _CHAPTER_RE.match(ls[0]):
                _add_chapter(book, ls[0])
            par = ""
            for l in ls:
                if par and re.search(r"\w-$", par) and re.match(r"^[a-zа-яё]", l):
                    par = par[:-1] + l
                else:
                    par = (par + " " + l) if par else l
            _add_par(book, par)
    else:
        for l in nonempty:
            if _CHAPTER_RE.match(l) and len(l.strip()) < 90:
                _add_chapter(book, l)
            _add_par(book, l)


def load_txt(path: str) -> Book:
    with open(path, "rb") as f:
        text = _decode(f.read())
    book = Book(path)
    _text_into_book(text, book)
    if not book.paragraphs:
        raise BookError("Файл пустой.")
    return book


def load_rtf(path: str) -> Book:
    from striprtf.striprtf import rtf_to_text

    with open(path, "rb") as f:
        raw = f.read().decode("latin-1")
    # striprtf понимает \'xx в кодировке из \ansicpg
    m = re.search(r"\\ansicpg(\d+)", raw)
    enc = f"cp{m.group(1)}" if m else "cp1251"
    try:
        text = rtf_to_text(raw, encoding=enc, errors="replace")
    except TypeError:
        text = rtf_to_text(raw)
    book = Book(path)
    _text_into_book(text, book)
    return book


# ----------------------------------------------------------------------------- MOBI / AZW
def load_mobi(path: str) -> Book:
    try:
        import mobi
    except ImportError:
        return load_via_calibre(path)
    try:
        tmpdir, extracted = mobi.extract(path)
    except Exception as e:  # noqa: BLE001
        try:
            return load_via_calibre(path)
        except BookError:
            raise BookError(f"Не удалось открыть MOBI/AZW (возможно, защита DRM): {e}")
    try:
        ext = extracted.lower()
        if ext.endswith(".epub"):
            book = load_epub(extracted)
        elif ext.endswith(".pdf"):
            book = load_pdf(extracted)
        else:
            book = load_html(extracted)
        book.path = path
        if book.title.lower() in ("book", "index"):
            book.title = ""
        return book
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ----------------------------------------------------------------------------- DJVU / прочее
def _find_exe(names: list[str], extra_dirs: list[str]) -> str | None:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    for d in extra_dirs:
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p
    return None


_PF = [os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")]


def load_djvu(path: str) -> Book:
    exe = _find_exe(["djvutxt", "djvutxt.exe"], [os.path.join(p, "DjVuLibre") for p in _PF])
    if exe:
        out = subprocess.run([exe, path], capture_output=True, timeout=600,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        text = _decode(out.stdout)
        if len(text.strip()) > 200:
            book = Book(path)
            _text_into_book(text, book)
            return book
        raise BookError("В этом DJVU нет текстового слоя (скан). Нужно распознавание (OCR).")
    try:
        return load_via_calibre(path)
    except BookError:
        raise BookError(
            "Для DJVU нужна бесплатная программа DjVuLibre (djvulibre.djvuzone.org)\n"
            "или Calibre (calibre-ebook.com). Установите любую и откройте файл снова.\n"
            "Если DJVU — скан без текста, его нужно сначала распознать (OCR)."
        )


def load_via_calibre(path: str) -> Book:
    exe = _find_exe(["ebook-convert", "ebook-convert.exe"],
                    [os.path.join(p, d) for p in _PF for d in ("Calibre2", "Calibre")])
    if not exe:
        raise BookError(
            f"Формат «{os.path.splitext(path)[1]}» читается через бесплатную программу Calibre.\n"
            "Установите Calibre (calibre-ebook.com) и откройте файл снова."
        )
    tmp = tempfile.mkdtemp(prefix="chitalka_")
    try:
        out = os.path.join(tmp, "book.epub")
        subprocess.run([exe, path, out], capture_output=True, timeout=1800,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if not os.path.isfile(out):
            raise BookError("Calibre не смог преобразовать файл.")
        book = load_epub(out)
        book.path = path
        return book
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------------------------- общий вход
def load_book(path: str, notes: str = "skip") -> Book:
    """notes: "skip" — сноски не читать; "inline" — коротко сразу после абзаца (FB2, EPUB)."""
    if not os.path.isfile(path):
        raise BookError("Файл не найден.")
    low = path.lower()
    ocr = ocr_cache_path(path)
    if os.path.isfile(ocr):  # скан, уже распознанный программой
        book = load_ocr_text(path, ocr)
    elif low.endswith(".fb2.zip") or (low.endswith(".zip") and _zip_has(path, ".fb2")):
        book = load_fb2_zip(path, notes)
    elif low.endswith((".fb2", ".epub")):
        book = (load_fb2 if low.endswith(".fb2") else load_epub)(path, notes)
    else:
        ext = os.path.splitext(low)[1]
        loader = {
            ".fb2": load_fb2, ".epub": load_epub, ".pdf": load_pdf, ".docx": load_docx,
            ".odt": load_odt, ".txt": load_txt, ".md": load_txt, ".rtf": load_rtf,
            ".html": load_html, ".htm": load_html, ".xhtml": load_html,
            ".mobi": load_mobi, ".azw": load_mobi, ".azw3": load_mobi, ".prc": load_mobi,
            ".djvu": load_djvu, ".djv": load_djvu,
        }.get(ext, load_via_calibre)
        book = loader(path)
    _fill_title_from_filename(book)
    try:
        import textproc  # ISBN, УДК/ББК, ©, сноски [1] и * — не читаем
        book.paragraphs, book.chapters = textproc.clean_paragraphs(book.paragraphs, book.chapters)
    except Exception:  # noqa: BLE001
        pass
    if not book.paragraphs:
        raise BookError("В книге не найден текст.")
    return book


def _zip_has(path, ext) -> bool:
    try:
        with zipfile.ZipFile(path) as z:
            return any(n.lower().endswith(ext) for n in z.namelist())
    except Exception:  # noqa: BLE001
        return False


def _fill_title_from_filename(book: Book):
    """Имена файлов в библиотеке: Название_Фамилия И.О._год.расширение"""
    name = os.path.basename(book.path)
    for ext in (".fb2.zip",) + SUPPORTED:
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
            break
    parts = name.split("_")
    library_style = len(parts) >= 3 and (
        re.fullmatch(r"\d{4}|без года", parts[-1].strip()) or re.search(r"[A-ZА-ЯЁ]\.", parts[1]))
    if library_style:  # имена файлов библиотеки надёжнее метаданных
        book.title = parts[0].strip()
        book.author = parts[1].strip()
    if not book.title:
        book.title = name


# ----------------------------------------------------------------------------- распознавание сканов (OCR)
APP_DIR = os.path.dirname(os.path.abspath(__file__))
OCR_DIR = os.path.join(APP_DIR, "cache", "ocr")


def ocr_cache_path(path: str) -> str:
    """Где лежит распознанный текст этой книги (книгу саму не трогаем)."""
    import hashlib
    try:
        st = os.stat(path)
        key = f"{os.path.normcase(os.path.abspath(path))}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        key = os.path.normcase(os.path.abspath(path))
    return os.path.join(OCR_DIR, hashlib.sha1(key.encode("utf-8")).hexdigest() + ".txt")


def load_ocr_text(path: str, ocr_path: str) -> Book:
    book = Book(path)
    with open(ocr_path, encoding="utf-8") as f:
        text = f.read()
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        if block.startswith("## "):
            _add_chapter(book, block[3:])
            _add_par(book, block[3:])
        else:
            _add_par(book, block)
    try:
        if path.lower().endswith(".pdf"):
            import pymupdf as fitz
            with fitz.open(path) as d:
                book.cover = d[0].get_pixmap(matrix=fitz.Matrix(0.8, 0.8)).tobytes("png")
    except Exception:  # noqa: BLE001
        pass
    return book


def ocr_available() -> str:
    """'' — можно распознавать; иначе причина, почему нельзя."""
    try:
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.globalization import Language
        if not OcrEngine.is_language_supported(Language("ru")):
            return "В Windows не установлено распознавание русского текста (языковой пакет «Русский»)."
        return ""
    except Exception as e:  # noqa: BLE001
        return f"Нет компонента распознавания Windows ({type(e).__name__}). Запустите install.bat."


def _page_images(path: str):
    """Картинки страниц скана: (номер, всего, PNG-байты)."""
    low = path.lower()
    if low.endswith(".pdf"):
        import pymupdf as fitz
        with fitz.open(path) as d:
            n = len(d)
            for i, page in enumerate(d):
                zoom = 300 / 72 if page.rect.width < 500 else 220 / 72  # мелкие страницы — крупнее
                yield i, n, page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY).tobytes("png")
    elif low.endswith((".djvu", ".djv")):
        exe = _find_exe(["ddjvu", "ddjvu.exe"], [r"C:\Program Files (x86)\DjVuLibre", r"C:\Program Files\DjVuLibre"])
        if not exe:
            raise BookError("Для распознавания DJVU нужна бесплатная программа DjVuLibre:\n"
                            "https://djvu.sourceforge.net/ — установите и повторите.")
        djvused = os.path.join(os.path.dirname(exe), "djvused.exe" if exe.endswith(".exe") else "djvused")
        try:
            n = int(subprocess.run([djvused, "-e", "n", path], capture_output=True, text=True, timeout=60).stdout.strip())
        except Exception:  # noqa: BLE001
            n = 0
        with tempfile.TemporaryDirectory() as td:
            for i in range(max(1, n)):
                out = os.path.join(td, f"p{i}.tif")
                r = subprocess.run([exe, "-format=tiff", "-mode=black", "-scale=300", f"-page={i + 1}", path, out],
                                   capture_output=True, timeout=120)
                if r.returncode != 0 or not os.path.isfile(out):
                    if n == 0:
                        break
                    continue
                from PIL import Image
                buf = io.BytesIO()
                Image.open(out).convert("L").save(buf, "PNG")
                yield i, max(1, n), buf.getvalue()
    else:
        raise BookError("Распознавать можно PDF и DJVU.")


def ocr_book(path: str, progress=lambda i, n: None, cancel=lambda: False) -> str:
    """Распознаёт скан встроенным распознаванием Windows (русский и английский), сохраняет текст в кэш.
    Возвращает путь к распознанному тексту ('' — отменено)."""
    import asyncio
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.storage.streams import InMemoryRandomAccessStream, DataWriter

    engine = OcrEngine.try_create_from_language(Language("ru"))
    if engine is None:
        raise BookError("Распознавание русского текста в Windows недоступно.")

    async def recognize(png: bytes) -> list[tuple[float, float, str]]:
        stream = InMemoryRandomAccessStream()
        w = DataWriter(stream)
        w.write_bytes(png)
        await w.store_async()
        await w.flush_async()
        w.detach_stream()
        stream.seek(0)
        dec = await BitmapDecoder.create_async(stream)
        bmp = await dec.get_software_bitmap_async()
        res = await engine.recognize_async(bmp)
        out = []
        for ln in res.lines:
            words = list(ln.words)
            if not words:
                continue
            r = words[0].bounding_rect
            out.append((r.y, r.x, r.height, " ".join(x.text for x in words)))
        return out

    pages_text = []
    loop = asyncio.new_event_loop()
    try:
        for i, n, png in _page_images(path):
            if cancel():
                return ""
            progress(i + 1, n)
            lines = loop.run_until_complete(recognize(png))
            pages_text.append(lines)
    finally:
        loop.close()

    # строки -> абзацы: новая строка с отступом или после короткой строки с точкой — новый абзац
    out_pars = []
    for lines in pages_text:
        if not lines:
            continue
        xs = sorted(l[1] for l in lines)
        left = xs[len(xs) // 10] if xs else 0
        h = sorted(l[2] for l in lines)[len(lines) // 2] or 10
        maxw = max(len(l[3]) for l in lines)
        cur = ""
        prev_len = 0
        for y, x, _hh, t in lines:
            if re.fullmatch(r"[\-–—\s]*\d{1,4}[\-–—\s]*", t):  # номер страницы
                continue
            indent = x - left > h * 1.2
            new = not cur or indent or (prev_len < 0.7 * maxw and re.search(r"[.!?…:»\"]$", cur)) \
                or re.match(r"^[–—]\s", t)
            if new and cur:
                out_pars.append(cur)
                cur = ""
            if cur and re.search(r"\w-$", cur) and re.match(r"^[a-zа-яё]", t):
                cur = cur[:-1] + t
            else:
                cur = (cur + " " + t).strip()
            prev_len = len(t)
        if cur:
            out_pars.append(cur)
    # склеиваем абзацы, разорванные между страницами
    merged = []
    for p in out_pars:
        if merged and not re.search(r"[.!?…:»\"]$", merged[-1]) and re.match(r"^[a-zа-яё,;]", p):
            merged[-1] = (merged[-1][:-1] + p) if merged[-1].endswith("-") else merged[-1] + " " + p
        else:
            merged.append(p)
    text = []
    for p in merged:
        if len(p) < 60 and re.match(r"^(глава|часть|раздел|chapter|part)\b", p, re.I):
            text.append("## " + p)
        else:
            text.append(p)
    if not text:
        raise BookError("Распознавание не нашло текста на страницах (возможно, очень плохое качество скана).")
    dst = ocr_cache_path(path)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst + ".tmp", "w", encoding="utf-8") as f:
        f.write("\n\n".join(text))
    os.replace(dst + ".tmp", dst)
    return dst


# ----------------------------------------------------------------------------- обложки для библиотеки
def get_cover(path: str) -> bytes | None:
    """Только обложка (быстро, без разбора всего текста)."""
    low = path.lower()
    try:
        if low.endswith(".epub"):
            from lxml import etree
            import posixpath
            from urllib.parse import unquote
            with zipfile.ZipFile(path) as z:
                try:
                    cont = etree.fromstring(z.read("META-INF/container.xml"))
                    opf_path = [e.get("full-path") for e in cont.iter() if _local(e.tag) == "rootfile"][0]
                except Exception:  # noqa: BLE001
                    opf_path = next(n for n in z.namelist() if n.lower().endswith(".opf"))
                opf = etree.fromstring(z.read(opf_path), etree.XMLParser(recover=True))
                base = posixpath.dirname(opf_path)
                man = {e.get("id"): (e.get("href"), e.get("media-type", ""), e.get("properties", ""))
                       for e in opf.iter() if _local(e.tag) == "item"}
                cid = next((e.get("content") for e in opf.iter() if _local(e.tag) == "meta"
                            and (e.get("name") or "").lower() == "cover"), None)
                cands = ([man[cid]] if cid in man else []) + [v for v in man.values() if "cover-image" in v[2]] + \
                    [v for k, v in man.items() if v[1].startswith("image/") and "cover" in (k + v[0]).lower()]
                for href, mt, _p in cands:
                    if mt.startswith("image/"):
                        return z.read(posixpath.normpath(posixpath.join(base, unquote(href))))
        elif low.endswith((".fb2", ".fb2.zip", ".zip")):
            if low.endswith(".fb2"):
                with open(path, "rb") as f:
                    data = f.read()
            else:
                with zipfile.ZipFile(path) as z:
                    n = next(n for n in z.namelist() if n.lower().endswith(".fb2"))
                    data = z.read(n)
            m = re.search(rb"<coverpage>.*?href=\"#([^\"]+)\"", data, re.S)
            if m:
                import base64
                b = re.search(rb"<binary[^>]*id=\"" + re.escape(m.group(1)) + rb"\"[^>]*>(.*?)</binary>", data, re.S)
                if b:
                    return base64.b64decode(b.group(1))
        elif low.endswith(".pdf"):
            import pymupdf as fitz
            with fitz.open(path) as d:
                return d[0].get_pixmap(matrix=fitz.Matrix(0.5, 0.5)).tobytes("png")
    except Exception:  # noqa: BLE001
        return None
    return None
