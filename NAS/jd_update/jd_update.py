#!/usr/bin/env python3
"""
jd_update.py - Todo en uno para tu MiSTer_NEO: actualizar, organizar MRA, revisar y copiar ROMs.

Puede vivir en la RAIZ (donde estan _Arcade y games) o en una subcarpeta de ella llamada jd_update
(MiSTer_NEO/jd_update/): en ese caso la raiz es la carpeta padre. Sus listas, caches, jd_roms.db y logs quedan junto al script.
Esta pensado para correr en el NAS por SSH.
Sin argumentos abre un MENU numerado. Tambien se puede usar directo (cron, scripts):

    python3 jd_update.py                 # menu
    python3 jd_update.py update          # descarga las DBs de jd_update.list
    python3 jd_update.py organize        # enlaces de MRA en _@JD_Organizer (_CORE, _A-Z, _DEBUT, _UPDATED...)
    python3 jd_update.py check           # revisa MRA/.rbf/ROMs (solo lee) -> JD_Check.log
    python3 jd_update.py copy            # check + copia los zips que falten desde la fuente de ROMs
    python3 jd_update.py rebuild         # check + crea zips "solucionables" a partir de tus zips
    python3 jd_update.py rescan          # regenera jd_roms.db (indice de la fuente)
    python3 jd_update.py all             # update + organize + copy
    python3 jd_update.py torrents        # lee torrents/*.torrent y dats/ y planea que zips bajar (no descarga todavia)
    python3 jd_update.py torrents --find tekken   # busca un juego/archivo dentro de los .torrent (zips, CHD...)
    python3 jd_update.py log             # lee JD_Check.log en la terminal (por secciones, con busqueda)
    python3 jd_update.py github          # trae de GitHub el script y las listas (jd_update.list, jd_exclude.list)
    python3 jd_update.py github-lists    # igual, pero solo las listas
    python3 jd_update.py undo-organize   # quita lo que creo el organizador
    python3 jd_update.py undo-rebuild    # borra los zips que creo rebuild

Opciones utiles: --dry-run (no cambia nada), --yes (sin preguntas), --rescan (regenera el indice
antes de copiar), --source RUTA (otra fuente de ROMs), --mode copy (si el disco no admite symlinks).

UN SOLO CRITERIO DE "FALTANTE"
  Antes el update contaba como faltante todo zip declarado en la MRA que no existiera como archivo
  (miles, porque en un set MERGED los zips de los clones no existen) y el check replicaba el cargador
  del MiSTer (decenas). Ahora todo usa el del check: una MRA solo cuenta como problema si el
  MiSTer NO podria cargarla. Replica Main_MiSTer / mra_loader.cpp: recorre los zips de cada <part>
  en orden, salta los que no existen, busca por CRC y luego por nombre.

FUENTE DE ROMS (sin FTP)
  Los zips que hagan falta se copian de SOURCE_DIR (MAME 0.289 merged, en el mismo NAS).
  Para no recorrer 10 000+ archivos cada vez se usa jd_roms.db (texto plano, una linea por zip:
  nombre<TAB>ruta relativa<TAB>tamano). Se crea sola la primera vez; usa --rescan o la opcion 6
  del menu cuando actualices la fuente. Nunca sobrescribe un zip existente.
  Rutas tipo /hbmame/x.zip no se buscan en la fuente (no tiene HBMAME).

Solo usa la libreria estandar de Python 3.8+.
"""
import argparse
import configparser
import difflib
import fnmatch
import hashlib
import io
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from urllib.parse import quote

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR   # listas, caches, jd_roms.db y logs viven junto al script
UA = "jd-update/2.0"
IS_WIN = platform.system() == "Windows"
WIN_BAD = '<>:"|?*'
TMP_DIRNAME = ".jd_update_tmp"

ORGANIZER = "_@JD_Organizer"
NO_CORE = "_SIN_CORE"
DEFAULT_SCAN = ["_Arcade", "_Ongo"]                 # donde busca MRA el organizador
CHECK_SCAN = ["_Arcade", "_Ongo", ORGANIZER]        # donde las busca la revision
DEFAULT_ROM_DIRS = ["games/mame", "games/hbmame"]
DEFAULT_CORES_DIR = "_Arcade/cores"
SOURCE_DIR = "/volumeUSB1/usbshare/MAME 0.289 ROMs (merged)"
ROMS_DB = "jd_roms.db"
BIG_COPY = 2 * 1024 ** 3        # pregunta antes de copiar mas de 2 GB
CHECK_LOG = "JD_Check.log"
CACHE_FILE = "JD_Check.cache.json"
REBUILT_FILE = "JD_Check_rebuilt.json"
EXCLUDE_FILE = "jd_exclude.list"
MOVES_FILE = "JD_Organizer_moves.json"
GITHUB_REPO = "jemaeld/jd_MiSTer"           # de donde se bajan las listas (y el script) con 'github'
GITHUB_BRANCH = "main"
GITHUB_PATH = "NAS/jd_update"
HISTORY_FILE = "JD_Update_history.json"   # que MRA bajo cada update (nuevas / actualizadas)
CORE_DIR = "_CORE"                         # dentro de _@JD_Organizer: aqui van las carpetas por core
SPECIAL_DIRS = ["_DEBUT", "_PAST DEBUT", "_UPDATED", "_PAST UPDATED", "_A-Z"]  # dentro de _@JD_Organizer
OLD_SPECIAL = ["_PAST UPDATES"]            # nombre de una version anterior (se limpia sola)
PAST_KEEP = 3                              # cuantos updates anteriores se conservan en _PAST ...
HISTORY_MAX = 30                           # corridas guardadas en el historial
BAD_CHARS = '<>:"/\\|?*'
MODES = ["symlink", "hardlink", "copy", "move"]
VERB = {"symlink": "enlaces simbolicos creados", "hardlink": "enlaces duros creados",
        "copy": "copias creadas", "move": "MRA movidas"}
EXCLUDE_TEMPLATE = """# jd_exclude.list - zips que se IGNORAN al revisar/copiar ROMs.
# Un nombre por linea (con o sin .zip, sin distinguir mayusculas). Admite comodines * y ?
# Lo que empiece con # es comentario.
# Ejemplos:
#   jtbeta.zip
#   jt*beta*.zip
"""

print_lock = threading.Lock()


def log(msg=""):
    with print_lock:
        print(msg, flush=True)


# =====================================================================
#  PARTE 1 - DESCARGA (jd_update.list)
# =====================================================================
def unquote(v):
    v = (v or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        v = v[1:-1]
    return v.strip()


def collect_ini_files(inputs):
    files = []
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            files += sorted(p.glob("jd_update*.list"))
        elif p.is_file():
            files.append(p)
        else:
            log(f"[!] No existe: {item}")
    return files


def parse_inis(ini_files):
    """Devuelve (config_global, [dbs]). Cada db: dict(id, url, filter)."""
    glob_cfg = {"filter": ""}
    dbs, seen = [], set()
    for ini in ini_files:
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        cp.optionxform = str.lower
        cp.read(ini, encoding="utf-8-sig")
        for sec in cp.sections():
            items = {k: unquote(v) for k, v in cp.items(sec)}
            if sec.lower() == "mister":
                if "filter" in items:
                    glob_cfg["filter"] = items["filter"]
                continue
            if "db_url" not in items or sec in seen:
                continue
            seen.add(sec)
            dbs.append({"id": sec, "url": items["db_url"], "filter": items.get("filter")})
    return glob_cfg, dbs


def find_default_ini(root):
    for base in (root, BASE_DIR, Path.cwd()):
        p = base / "jd_update.list"
        if p.is_file():
            return p
    return None


def ask_ini():
    if not sys.stdin or not sys.stdin.isatty():
        sys.exit("No encontre jd_update.list y no hay terminal para preguntar. "
                 "Pasa la ruta como argumento.")
    log("No encontre jd_update.list en la raiz.")
    while True:
        try:
            raw = input("Ruta del .list (o carpeta con jd_update*.list; Enter para salir): ")
        except (EOFError, KeyboardInterrupt):
            sys.exit("\nCancelado.")
        raw = raw.strip().strip("'\"")
        if not raw:
            sys.exit("Cancelado.")
        p = Path(raw).expanduser()
        if p.is_file() or (p.is_dir() and any(p.glob("jd_update*.list"))):
            return p
        log(f"[!] No existe o no hay .list ahi: {raw}")


def http_get(url, retries=3, timeout=60):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def fix_url(url):
    return quote(url, safe=":/%?=&#@+,;~!$'()*[]")


def maybe_unzip_json(data):
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".json"))
            data = z.read(name)
    return json.loads(data.decode("utf-8"))


def load_db(url):
    data = Path(url).read_bytes() if os.path.isfile(url) else http_get(fix_url(url))
    return maybe_unzip_json(data)


# ---- filtros
def effective_filter(db_cfg, glob_cfg, db_json):
    """ini de la db > default de la db; [mister] = filtro global."""
    raw = db_cfg["filter"]
    if raw is None:
        raw = (db_json.get("default_options") or {}).get("filter", "")
    terms = []
    for t in raw.split():
        if t.lower() == "[mister]":
            terms += glob_cfg["filter"].split()
        else:
            terms.append(t.lower())
    pos = {t for t in terms if not t.startswith("!")}
    neg = {t[1:] for t in terms if t.startswith("!")}
    return pos, neg


def tag_names(entry, tag_dict):
    inv = {v: k for k, v in (tag_dict or {}).items()}
    out = set()
    for t in entry.get("tags") or []:
        if isinstance(t, int):
            if t in inv:
                out.add(str(inv[t]).lower())
        else:
            out.add(str(t).lower())
    return out


def passes_filter(entry, tag_dict, pos, neg):
    if not pos and not neg:
        return True
    tags = tag_names(entry, tag_dict)
    if tags & neg:
        return False
    if pos and tags and not (tags & pos):
        return False
    return True


# ---- archivos
HASH_CACHE_FILE = "JD_Update.cache.json"


class HashCache:
    """Recuerda el MD5 de archivos ya verificados, con su tamaño y fecha de modificacion. Si no cambiaron,
    no se vuelven a leer del disco (evita releer GB en cada corrida)."""

    def __init__(self):
        self.path, self.data, self.dirty, self.enabled = None, {}, False, False
        self.lock = threading.Lock()
        self.hits = self.reads = 0

    def load(self, root, enabled=True):
        self.enabled = enabled
        self.root = root
        self.path = STATE_DIR / HASH_CACHE_FILE
        self.data = {}
        if enabled:
            try:
                d = json.loads(self.path.read_text(encoding="utf-8"))
                if d.get("root") == str(root):
                    self.data = d.get("files", {})
            except (OSError, ValueError):
                pass

    def md5(self, p):
        """MD5 de un archivo, desde la cache si sigue igual (tamaño + mtime)."""
        st = p.stat()
        key = str(p)
        c = self.data.get(key) if self.enabled else None
        if c and c[0] == st.st_size and c[1] == st.st_mtime_ns:
            self.hits += 1
            return c[2]
        h = md5_of(p)
        self.reads += 1
        self.remember(p, h)
        return h

    def remember(self, p, md5):
        if not self.enabled:
            return
        try:
            st = p.stat()
        except OSError:
            return
        with self.lock:
            self.data[str(p)] = [st.st_size, st.st_mtime_ns, md5.lower()]
            self.dirty = True

    def save(self):
        if self.enabled and self.dirty and self.path:
            # solo conserva lo que sigue existiendo
            self.data = {k: v for k, v in self.data.items() if os.path.exists(k)}
            try:
                tmp = self.path.with_name(self.path.name + ".tmp")
                tmp.write_text(json.dumps({"root": str(self.root), "files": self.data},
                                          separators=(",", ":")), encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError:
                pass


HASHES = HashCache()


def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def local_path(dest, rel_path):
    """Quita el prefijo '|' (ruta de sistema) y protege contra '..'."""
    rel_path = rel_path.lstrip("|").replace("\\", "/")
    parts = [p for p in PurePosixPath(rel_path).parts if p not in ("", "/", ".")]
    if any(p == ".." for p in parts):
        raise ValueError(f"ruta insegura: {rel_path}")
    if IS_WIN:
        parts = ["".join("_" if c in WIN_BAD else c for c in p).rstrip(" .") for p in parts]
    return Path(dest).joinpath(*parts)


def file_url(entry, rel_path, base_url):
    if entry.get("url"):
        return fix_url(entry["url"])
    base = base_url if base_url.endswith("/") else base_url + "/"
    return base + quote(rel_path.lstrip("|"), safe="/")


def is_up_to_date(path, entry, fast):
    if not path.is_file():
        return False
    if entry.get("size") is not None and path.stat().st_size != entry["size"]:
        return False
    if fast or not entry.get("hash"):
        return True
    return HASHES.md5(path).lower() == str(entry["hash"]).lower()


def verify(path, entry):
    if entry.get("size") is not None and path.stat().st_size != entry["size"]:
        raise IOError("tamaño distinto al esperado")
    if entry.get("hash") and md5_of(path).lower() != str(entry["hash"]).lower():
        raise IOError("MD5 no coincide")


CHANGES = {"new": set(), "updated": set(), "rbf": set()}
_changes_lock = threading.Lock()


def note_change(path, existed):
    """Anota lo que baja el update. MRA: nueva (no existia) o actualizada (ya existia y cambio).
    .rbf: solo si es una version nueva de un core que ya tenias (mismo nombre o con otra fecha); sus MRA
    cuentan como actualizadas."""
    low = str(path).lower()
    if low.endswith(".mra"):
        with _changes_lock:
            CHANGES["updated" if existed else "new"].add(str(path))
    elif low.endswith(".rbf"):
        base = RE_DATED.sub("", path.stem).lower()
        try:
            others = any(f != path.name and f.lower().endswith(".rbf")
                         and RE_DATED.sub("", Path(f).stem).lower() == base for f in os.listdir(path.parent))
        except OSError:
            others = False
        if existed or others:
            with _changes_lock:
                CHANGES["rbf"].add(base)


def download_one(url, path, entry):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    last = None
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            verify(tmp, entry)
            existed = path.exists()
            os.replace(tmp, path)
            note_change(path, existed)
            if entry.get("hash"):
                HASHES.remember(path, str(entry["hash"]))
            return
        except Exception as e:  # noqa
            last = e
            if isinstance(e, urllib.error.HTTPError) and e.code in (403, 404):
                break  # no existe / bloqueado: reintentar no sirve
            time.sleep(1.5 * (i + 1))
    if tmp.exists():
        tmp.unlink()
    raise last


def plan_file(rel_path, entry, dest, tag_dict, pos, neg, fast):
    """('filtered'|'skip'|'todo'|'bad', path_o_mensaje)."""
    if not passes_filter(entry, tag_dict, pos, neg):
        return "filtered", None
    try:
        path = local_path(dest, rel_path)
    except ValueError as e:
        return "bad", str(e)
    if path.exists() and entry.get("overwrite") is False:
        return "skip", None
    if is_up_to_date(path, entry, fast):
        return "skip", None
    return "todo", path


# ---- archives (zips con archivos dentro)
def load_archive_summary(arc):
    if arc.get("summary_inline"):
        return arc["summary_inline"]
    sf = arc.get("summary_file")
    if sf and sf.get("url"):
        return maybe_unzip_json(http_get(fix_url(sf["url"])))
    return {}


def slug(s):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)


def extract_archive(db_id, arc_id, arc, needed, tmp_dir):
    """Descarga el zip, lo verifica y extrae solo los archivos de `needed`."""
    af = arc["archive_file"]
    zip_tmp = tmp_dir / f"{slug(db_id)}__{slug(arc_id)}.zip"
    download_one(fix_url(af["url"]), zip_tmp, af)
    ok, errors = 0, []
    try:
        with zipfile.ZipFile(zip_tmp) as zf:
            names = zf.namelist()
            lower = {n.lower(): n for n in names}
            for rel_path, path, entry in needed:
                at = (entry.get("arc_at") or rel_path).replace("\\", "/")
                name = at if at in names else lower.get(at.lower())
                if name is None:
                    errors.append((rel_path, f"no esta dentro del zip ({at})"))
                    continue
                part = path.with_name(path.name + ".part")
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(name) as src, open(part, "wb") as dst:
                        shutil.copyfileobj(src, dst, 1 << 20)
                    verify(part, entry)
                    existed = path.exists()
                    os.replace(part, path)
                    note_change(path, existed)
                    if entry.get("hash"):
                        HASHES.remember(path, str(entry["hash"]))
                    ok += 1
                except Exception as e:  # noqa
                    errors.append((rel_path, str(e)))
                    if part.exists():
                        part.unlink()
    finally:
        if zip_tmp.exists():
            zip_tmp.unlink()
    return ok, errors


def process_db(db_cfg, glob_cfg, dest, workers, dry_run, fast):
    log(f"\n=== {db_cfg['id']} ===")
    try:
        db = load_db(db_cfg["url"])
    except Exception as e:  # noqa
        log(f"[ERROR] No se pudo leer la DB: {e}")
        return {"ok": 0, "skipped": 0, "failed": 1}

    pos, neg = effective_filter(db_cfg, glob_cfg, db)
    if pos or neg:
        log(f"filtro: {' '.join(sorted(pos) + ['!' + n for n in sorted(neg)])}")
    tag_dict = db.get("tag_dictionary") or {}
    base_url = db.get("base_files_url", "")
    failed = 0

    def make_folders(folders):
        for folder, entry in (folders or {}).items():
            if not passes_filter(entry or {}, tag_dict, pos, neg):
                continue
            try:
                if not dry_run:
                    local_path(dest, folder).mkdir(parents=True, exist_ok=True)
            except ValueError as e:
                log(f"[!] {e}")

    make_folders(db.get("folders"))

    todo, skipped = [], 0
    for rel_path, entry in (db.get("files") or {}).items():
        status, path = plan_file(rel_path, entry, dest, tag_dict, pos, neg, fast)
        if status == "todo":
            todo.append((rel_path, path, entry, file_url(entry, rel_path, base_url)))
        elif status == "skip":
            skipped += 1
        elif status == "bad":
            log(f"[!] {path}")

    arc_jobs = []
    for arc_id, arc in (db.get("archives") or {}).items():
        fmt = (arc.get("format") or "zip").lower()
        if fmt != "zip" or not (arc.get("archive_file") or {}).get("url"):
            log(f"[aviso] archive '{arc_id}' formato '{fmt}' no soportado, se omite.")
            continue
        try:
            summary = load_archive_summary(arc)
        except Exception as e:  # noqa
            log(f"[ERROR] archive '{arc_id}': no se pudo leer el summary: {e}")
            failed += 1
            continue
        make_folders(summary.get("folders"))
        needed = []
        for rel_path, entry in (summary.get("files") or {}).items():
            status, path = plan_file(rel_path, entry, dest, tag_dict, pos, neg, fast)
            if status == "todo":
                needed.append((rel_path, path, entry))
            elif status == "skip":
                skipped += 1
            elif status == "bad":
                log(f"[!] {path}")
        if needed:
            arc_jobs.append((arc_id, arc, needed))

    if db.get("zips"):
        log(f"[aviso] {len(db['zips'])} 'zips' (formato antiguo) en esta DB: no soportado, se omiten.")

    n_arc_files = sum(len(j[2]) for j in arc_jobs)
    log(f"{len(todo)} archivos + {n_arc_files} dentro de {len(arc_jobs)} zip(s) por procesar, {skipped} al dia")

    if dry_run:
        for rel_path, *_ in todo:
            log(f"  (dry-run) {rel_path}")
        for arc_id, arc, needed in arc_jobs:
            log(f"  (dry-run) zip '{arc_id}' -> {arc.get('description', '')}")
            for rel_path, *_ in needed:
                log(f"  (dry-run)   {rel_path}")
        return {"ok": 0, "skipped": skipped, "failed": failed}

    tmp_dir = Path(dest) / TMP_DIRNAME
    if arc_jobs:
        tmp_dir.mkdir(parents=True, exist_ok=True)

    ok, done = 0, 0
    total = len(todo) + len(arc_jobs)

    def job_file(item):
        rel_path, path, entry, url = item
        download_one(url, path, entry)
        return rel_path

    def job_arc(item):
        arc_id, arc, needed = item
        return extract_archive(db_cfg["id"], arc_id, arc, needed, tmp_dir)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {}
        for it in todo:
            futs[ex.submit(job_file, it)] = ("file", it)
        for it in arc_jobs:
            futs[ex.submit(job_arc, it)] = ("arc", it)
        for fut in as_completed(futs):
            kind, it = futs[fut]
            done += 1
            if kind == "file":
                try:
                    fut.result()
                    ok += 1
                    log(f"  [{done}/{total}] OK  {it[0]}")
                except Exception as e:  # noqa
                    failed += 1
                    log(f"  [{done}/{total}] ERR {it[0]}: {e}")
            else:
                arc_id, _, needed = it
                try:
                    n_ok, errors = fut.result()
                    ok += n_ok
                    failed += len(errors)
                    log(f"  [{done}/{total}] ZIP '{arc_id}': {n_ok}/{len(needed)} extraidos")
                    for rel_path, err in errors:
                        log(f"      ERR {rel_path}: {err}")
                except Exception as e:  # noqa
                    failed += len(needed)
                    log(f"  [{done}/{total}] ERR zip '{arc_id}': {e}")

    try:
        tmp_dir.rmdir()
    except OSError:
        pass
    return {"ok": ok, "skipped": skipped, "failed": failed}


def run_update(args, root):
    inputs = list(args.ini)
    if not inputs:
        default_ini = find_default_ini(root)
        inputs = [default_ini if default_ini else ask_ini()]
    if args.all_lists:
        inputs.append(root)  # carpeta -> todos los jd_update*.list
    ini_files, seen = [], set()
    for f in collect_ini_files(inputs):
        if f.resolve() not in seen:
            seen.add(f.resolve())
            ini_files.append(f)
    if not ini_files:
        sys.exit("No encontre ningun .list")
    glob_cfg, dbs = parse_inis(ini_files)
    if args.only:
        dbs = [d for d in dbs if any(o.lower() in d["id"].lower() for o in args.only)]
    if not dbs:
        sys.exit("No hay DBs que procesar")
    log(f"Lista: {', '.join(f.name for f in ini_files)} | DBs: {len(dbs)}")

    total = {"ok": 0, "skipped": 0, "failed": 0}
    HASHES.load(root, not args.no_hash_cache)
    try:
        for d in dbs:
            r = process_db(d, glob_cfg, root, args.workers, args.dry_run, args.fast)
            for k in total:
                total[k] += r[k]
    finally:
        HASHES.save()
    record_history(root, args.dry_run, args.mode)
    if not args.fast and (HASHES.hits or HASHES.reads):
        log(f"MD5: {HASHES.hits} archivos sin releer (cache) | {HASHES.reads} leidos del disco")
    log(f"\nDescarga lista. Descargados: {total['ok']} | Al dia: {total['skipped']} | Fallidos: {total['failed']}")
    return total



# =====================================================================
#  PARTE 2 - ORGANIZADOR DE MRA POR CORE
def core_dir_name(rbf):
    """Carpeta del core. Debe empezar con '_' o el menu del MiSTer no la muestra."""
    clean = "".join("_" if c in BAD_CHARS else c for c in rbf_basename(rbf)).strip(" .")
    if not clean:
        return NO_CORE
    return clean if clean.startswith("_") else "_" + clean


def find_mras(root, scan_dirs):
    mras, seen = [], set()
    for sd in scan_dirs:
        base = root / sd
        if not base.is_dir():
            continue
        for dp, _dirs, files in os.walk(base):
            for f in files:
                if f.lower().endswith(".mra"):
                    p = Path(dp) / f
                    if p.is_symlink():  # los enlaces no son MRAs "reales"
                        continue
                    rp = p.resolve()
                    if rp not in seen:
                        seen.add(rp)
                        mras.append(p)
    return sorted(mras, key=lambda p: str(p).lower())

def same_target(src, dst, mode):
    """True si dst ya es el enlace/copia correcta de src."""
    try:
        if mode == "symlink":
            return dst.is_symlink() and dst.resolve() == src.resolve()
        if mode == "hardlink":
            return dst.exists() and os.path.samefile(src, dst)
        if mode == "copy":
            return (dst.exists() and not dst.is_symlink()
                    and dst.stat().st_size == src.stat().st_size
                    and dst.read_bytes() == src.read_bytes())
    except OSError:
        pass
    return False


def place(src, dst, mode):
    """Crea el enlace/copia/movimiento. Si dst existe, es un enlace/copia nuestro a refrescar."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if mode == "move":
        shutil.move(str(src), str(dst))
        return
    if os.path.lexists(dst):
        dst.unlink()
    if mode == "symlink":
        os.symlink(os.path.relpath(src, dst.parent), dst)  # relativo: sobrevive si mueves la raiz
    elif mode == "hardlink":
        os.link(src, dst)
    else:
        shutil.copy2(src, dst)


def prune_broken_links(org, dry_run):
    """Quita enlaces rotos dentro de _@JD_Organizer (MRA que el updater ya borro)."""
    n = 0
    if not org.is_dir():
        return n
    for dp, _d, files in os.walk(org):
        for f in files:
            p = Path(dp) / f
            if p.is_symlink() and not p.exists():
                n += 1
                if not dry_run:
                    p.unlink()
    return n


def link_cores(root, org, mode, cores_rel, dry_run):
    """El MiSTer busca el .rbf de una MRA en <primera carpeta que empieza con '_' de su ruta>/cores.
    Para las MRA de _@JD_Organizer eso es _@JD_Organizer/cores, asi que debe existir (si no: 'No rbf found!').
    symlink/move -> enlace a la carpeta de cores real (siempre al dia). hardlink/copy -> archivos."""
    src = root / cores_rel
    dst = org / "cores"
    if not src.is_dir():
        log(f"[!] No existe {cores_rel}: no se puede crear {ORGANIZER}/cores (las MRA daran 'No rbf found!').")
        return
    if dry_run:
        log(f"(dry-run) se enlazaria {ORGANIZER}/cores -> {cores_rel}")
        return
    org.mkdir(parents=True, exist_ok=True)
    if mode in ("symlink", "move"):
        if dst.is_symlink():
            if dst.resolve() == src.resolve():
                return
            dst.unlink()
        if not os.path.lexists(dst):
            try:
                os.symlink(os.path.relpath(src, org), dst, target_is_directory=True)
                log(f"{ORGANIZER}/cores -> {cores_rel} (el MiSTer busca ahi el .rbf)")
                return
            except OSError as e:
                log(f"[!] No pude enlazar {ORGANIZER}/cores ({e}); se usan copias de los .rbf.")
        elif not dst.is_dir():
            log(f"[!] {ORGANIZER}/cores existe y no es una carpeta: bórralo.")
            return
    if dst.is_symlink():
        dst.unlink()
    dst.mkdir(exist_ok=True)
    have = {f.name: f for f in src.iterdir() if f.is_file() and f.suffix.lower() == ".rbf"}
    n = 0
    for name, f in have.items():
        d = dst / name
        if d.exists() and d.stat().st_size == f.stat().st_size and d.stat().st_mtime_ns >= f.stat().st_mtime_ns:
            continue
        if os.path.lexists(d):
            d.unlink()
        try:
            if mode == "copy":
                raise OSError
            os.link(f, d)
        except OSError:
            shutil.copy2(f, d)
        n += 1
    for old in dst.iterdir():
        if old.is_file() and old.name not in have:
            old.unlink()
    if n:
        log(f"{ORGANIZER}/cores: {n} .rbf sincronizados")


def remove_empty_dirs(org, remove_org=True):
    if not org.is_dir():
        return
    for d in org.iterdir():
        if d.is_dir() and not d.is_symlink():
            try:
                d.rmdir()  # solo borra carpetas vacias
            except OSError:
                pass
    if remove_org:
        try:
            org.rmdir()
        except OSError:
            pass


# ---- historial (para --undo)
def load_moves(root):
    try:
        data = json.loads((STATE_DIR / MOVES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data.get("moves", []) if data.get("root") == str(root) else []


def save_moves(root, moves):
    moves = list({m["to"]: m for m in moves}.values())  # una entrada por destino (la mas reciente)
    moves = [m for m in moves if os.path.lexists(root / m["to"])]
    (STATE_DIR / MOVES_FILE).write_text(json.dumps({"root": str(root), "moves": moves}, indent=1,
                                              ensure_ascii=False), encoding="utf-8")


def migrate_legacy_dirs(root, org, dry_run):
    """Versiones anteriores dejaban las carpetas de core sueltas en _@JD_Organizer (algunas sin '_').
    Las mueve a _@JD_Organizer/_CORE/_<nombre> y actualiza el historial del organizador."""
    done = []
    if not org.is_dir():
        return done
    stray = org / "_cores"  # una version anterior renombraba por error el enlace 'cores'
    if stray.is_symlink() and not dry_run:
        stray.unlink()
        log(f"Quitado el enlace sobrante {ORGANIZER}/_cores")
    core_root = org / CORE_DIR
    skip = {CORE_DIR.lower(), "cores"} | {d.lower() for d in SPECIAL_DIRS + OLD_SPECIAL}
    for d in sorted(org.iterdir(), key=lambda p: p.name.lower()):
        if not d.is_dir() or d.is_symlink() or d.name.lower() in skip:
            continue
        newname = d.name if d.name.startswith("_") else "_" + d.name
        new = core_root / newname
        if os.path.lexists(new):
            log(f"[!] No se mueve {d.name}: ya existe {CORE_DIR}/{newname}. Unelas a mano.")
            continue
        if not dry_run:
            core_root.mkdir(exist_ok=True)
            d.rename(new)
        done.append((d.name, newname))
    if done and not dry_run:
        moves = load_moves(root)
        if moves:
            for m in moves:
                for old, new in done:
                    pre = f"{ORGANIZER}/{old}/"
                    if m["to"].startswith(pre):
                        m["to"] = f"{ORGANIZER}/{CORE_DIR}/{new}/" + m["to"][len(pre):]
            save_moves(root, moves)
    return done


def undo(root):
    mf = STATE_DIR / MOVES_FILE
    if not mf.exists():
        sys.exit("No hay movimientos que deshacer.")
    try:
        data = json.loads(mf.read_text(encoding="utf-8"))
    except ValueError:
        sys.exit(f"No pude leer {mf}")
    if data.get("root") != str(root):
        sys.exit(f"El historial es de otra raiz ({data.get('root')}).")
    moves = data.get("moves", [])
    if not moves:
        sys.exit("No hay movimientos que deshacer.")
    restored = removed = skipped = 0
    for mv in reversed(moves):
        mode = mv.get("mode", "move")
        src, dst = root / mv["to"], root / mv["from"]
        if mode == "move":
            if src.exists() and not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                restored += 1
            else:
                skipped += 1
                log(f"[!] Se omite (falta el origen o el destino ya existe): {mv['to']}")
        else:  # enlace/copia: solo se borra lo creado por el script; la MRA original no se toca
            if os.path.lexists(src):
                src.unlink()
                removed += 1
            else:
                skipped += 1
    oc = root / ORGANIZER / "cores"
    if oc.is_symlink():
        oc.unlink()
    elif oc.is_dir():
        for f in oc.glob("*.rbf"):
            f.unlink()
        try:
            oc.rmdir()
        except OSError:
            pass
    clear_special(root / ORGANIZER)
    remove_empty_dirs(root / ORGANIZER / CORE_DIR)
    remove_empty_dirs(root / ORGANIZER)
    mf.unlink()
    log(f"Enlaces/copias eliminados: {removed} | MRA restauradas: {restored} | omitidos: {skipped}")


def build_plan(root, mras, mode, org):
    """Lee cada MRA y decide que hacer: state = inplace | todo | conflict."""
    core_dirs = {}  # minusculas -> nombre real (evita '_BallySente' y '_ballysente' separadas)
    if org.is_dir():
        for d in org.iterdir():
            if d.is_dir():
                core_dirs[d.name.lower()] = d.name
    owned = {m["to"]: m["from"] for m in load_moves(root) if m.get("mode", "move") == mode}

    plan, errors = [], []
    for p in mras:
        try:
            _m = parse_mra(p.read_bytes()); rbf, setname, zips = _m["rbf"], _m["setname"], _m["zips"]
        except OSError as e:
            errors.append((rel(root, p), str(e)))
            continue
        core = core_dir_name(rbf)
        core = core_dirs.setdefault(core.lower(), core)
        target = org / core / p.name
        if mode == "move":
            if p.parent.resolve() == (org / core).resolve():
                state = "inplace"
            elif target.exists():
                state = "conflict"
            else:
                state = "todo"
        elif same_target(p, target, mode):
            state = "inplace"
        elif os.path.lexists(target):
            stale = target.is_symlink() and not target.exists()
            state = "todo" if (stale or owned.get(rel(root, target)) == rel(root, p)) else "conflict"
        else:
            state = "todo"
        plan.append({"path": p, "core": core, "rbf": rbf, "setname": setname, "zips": zips,
                     "target": target, "state": state})

    claimed = set()  # nombres repetidos en la misma corrida: el segundo no puede pisar al primero
    for it in plan:
        if it["state"] == "todo":
            key = str(it["target"]).lower()
            if key in claimed:
                it["state"] = "conflict"
            claimed.add(key)
    return plan, errors


def organize(root, plan, errors, mode, org, dry_run, yes, cores_rel=DEFAULT_CORES_DIR, core_root=None):
    """Crea los enlaces/copias/movimientos. Devuelve cuantos hizo (o -1 si el modo no funciona)."""
    core_root = core_root or org  # carpeta con las subcarpetas por core (_@JD_Organizer/_CORE)
    todo = [it for it in plan if it["state"] == "todo"]
    conflicts = [it for it in plan if it["state"] == "conflict"]
    inplace = [it for it in plan if it["state"] == "inplace"]
    per_core = Counter(it["core"] for it in plan)
    log(f"Cores distintos: {len(per_core)} | por procesar: {len(todo)} | ya en su lugar: {len(inplace)} | "
        f"conflictos de nombre: {len(conflicts)} | con error de lectura: {len(errors)}")
    log("Top cores: " + ", ".join(f"{c} ({n})" for c, n in per_core.most_common(12))
        + (f" ... y {len(per_core) - 12} mas" if len(per_core) > 12 else ""))
    for it in conflicts[:10]:
        log(f"  [conflicto] {rel(root, it['path'])} -> ya existe {rel(root, it['target'])} (no se toca)")
    if len(conflicts) > 10:
        log(f"  ... y {len(conflicts) - 10} conflictos mas")
    for r, e in errors[:10]:
        log(f"  [error] {r}: {e}")

    pruned = 0
    if mode == "symlink":
        pruned = prune_broken_links(org, dry_run)
        if pruned:
            log(f"Enlaces rotos {'por quitar' if dry_run else 'quitados'} (la MRA original ya no existe): {pruned}")
    if dry_run:
        if todo:
            log(f"(dry-run) se procesarian {len(todo)} MRA en modo {mode}")
        return 0
    if pruned:
        remove_empty_dirs(core_root, remove_org=False)  # carpetas de core que quedaron vacias
    if todo or any(core_root.glob("_*/*.mra")):
        link_cores(root, org, mode, cores_rel, dry_run)
    if not todo:
        return 0

    if mode == "move" and sys.stdin and sys.stdin.isatty() and not yes:
        try:
            ans = input(f"Se moveran {len(todo)} MRA a {ORGANIZER}/_<core>/ . Continuar? [s/N] ")
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans.strip().lower() not in ("s", "si", "sí", "y", "yes"):
            log("Organizacion cancelada, no se movio nada.")
            return 0

    done = []
    for it in todo:
        src, dst = it["path"], it["target"]
        try:
            place(src, dst, mode)
            done.append({"from": rel(root, src), "to": rel(root, dst), "mode": mode})
            if mode == "move":
                it["path"] = dst
        except OSError as e:
            if not done:  # fallo desde el primero: el sistema no soporta este modo
                remove_empty_dirs(org)
                log(f"[!] No pude crear '{mode}' en este sistema/volumen: {e}\n"
                    "    - Windows: activa el Modo de desarrollador (Configuracion > Para desarrolladores)\n"
                    "      o ejecuta como administrador.\n"
                    "    - exFAT/FAT32 no admiten enlaces: usa --mode copy.\n"
                    "    - Alternativa sin permisos especiales: --mode hardlink (mismo volumen).\n"
                    "    Se omite la organizacion; el analisis Missing continua.")
                return -1
            errors.append((rel(root, src), f"no se pudo crear {mode}: {e}"))
    save_moves(root, load_moves(root) + done)
    log(f"{VERB[mode][0].upper() + VERB[mode][1:]}: {len(done)}  (para revertir: --undo)")
    return len(done)


# ====================================================================
#  PARTE 3 - REVISION (cargador del MiSTer) - unica fuente de verdad
# ====================================================================
RE_COMMENT = re.compile(r"<!--.*?-->", re.S)
RE_RBF = re.compile(r"<rbf\b[^>]*>\s*(.*?)\s*</rbf>", re.I | re.S)
RE_SET = re.compile(r"<setname>\s*(.*?)\s*</setname>", re.I | re.S)
RE_ROM = re.compile(r"<rom\b([^>]*?)(?:/>|>(.*?)</rom>)", re.I | re.S)
RE_PART = re.compile(r"<part\b([^>]*?)/?>", re.I)
RE_FILE = re.compile(r"<file\b([^>]*?)/?>", re.I)
RE_DISKWORD = re.compile(r"(?<![\w.\-])((?:[\w][\w.\-]* ){0,2}[\w][\w.\-]*\.(?:chd|hdd|img))(?![\w])", re.I)
RE_DATED = re.compile(r"_(\d{6,8}[a-z]?)$", re.I)
# ------------------------------------------------------------- PARSEO ----
def attr(attrs, key):
    m = re.search(rf"""\b{key}\s*=\s*["']([^"']*)["']""", attrs, re.I)
    return m.group(1) if m else None


def split_zips(value):
    return [n.strip() for n in value.split("|") if n.strip()] if value else []


def parse_mra(raw):
    """-> dict(rbf, setname, zips[], roms[{index, zips[], parts[{name, crc, zips[]}]}], files[{root, path, index}]).
    Regex a proposito: varias MRA no son XML valido."""
    full = raw.decode("utf-8", errors="replace")
    cdisks, cforms = [], {}
    for cm in RE_COMMENT.findall(full):            # discos que la MRA solo menciona en comentarios (ranuras del OSD)
        for fullname in RE_DISKWORD.findall(cm):
            toks = fullname.split(" ")
            w = toks[-1]
            if w.lower() not in [x.lower() for x in cdisks]:
                cdisks.append(w)
            lst = cforms.setdefault(w.lower(), [])      # formas con palabras previas: "gq974-ja c01.chd" (nombres con espacios)
            for i in range(len(toks) - 1):
                f = " ".join(toks[i:])
                if f.lower() not in [x.lower() for x in lst]:
                    lst.append(f)
    text = RE_COMMENT.sub("", full)  # los comentarios pueden nombrar tags
    m = RE_RBF.search(text)
    rbf = m.group(1) if m else ""
    m = RE_SET.search(text)
    setname = m.group(1) if m else ""
    roms, zips, seen = [], [], set()

    def add_zip(n):
        if n.lower() not in seen:
            seen.add(n.lower())
            zips.append(n)

    for attrs, body in RE_ROM.findall(text):
        try:
            index = int(attr(attrs, "index") or 0)
        except ValueError:
            index = 0
        rzips = split_zips(attr(attrs, "zip"))
        for n in rzips:
            add_zip(n)
        parts = []
        for pa in RE_PART.findall(body or ""):
            name = attr(pa, "name")
            if not name:  # las <part> con datos hex no tienen nombre
                continue
            crc = None
            cs = (attr(pa, "crc") or "").strip()
            if cs:
                try:
                    crc = int(cs, 16)
                except ValueError:
                    crc = None
            pz = split_zips(attr(pa, "zip"))
            for n in pz:
                add_zip(n)
            parts.append({"name": name.strip(), "crc": crc or None, "zips": pz})
        roms.append({"index": index, "zips": rzips, "parts": parts})
    files = []
    for fa in RE_FILE.findall(text):          # <file ... path="cojag/x.hdd"/>: discos (HDD/IMG/CHD)
        fp = attr(fa, "path")
        if fp:
            files.append({"root": attr(fa, "root"), "path": fp.strip(), "index": attr(fa, "index")})
    return {"rbf": rbf, "setname": setname, "zips": zips, "roms": roms, "files": files, "cdisks": cdisks, "cforms": cforms}


def load_excludes(root, extra=()):
    """Lee jd_exclude.list de la raiz -> lista de patrones en minusculas con .zip."""
    pats = list(extra)
    f = STATE_DIR / EXCLUDE_FILE
    if f.is_file():
        try:
            for ln in f.read_text(encoding="utf-8-sig", errors="replace").splitlines():
                ln = ln.split("#", 1)[0].strip()
                if ln:
                    pats.append(ln)
        except OSError:
            pass
    out = []
    for p in pats:
        p = p.replace("\\", "/").split("/")[-1].strip().lower()
        if p and not p.endswith(".zip"):
            p += ".zip"
        if p:
            out.append(p)
    return out


def is_excluded(name, pats):
    n = name.replace("\\", "/").split("/")[-1].strip().lower()
    return any(fnmatch.fnmatchcase(n, p) for p in pats)


def apply_exclude(e, pats):
    """Quita de la MRA los zips excluidos. Un <rom> cuyos zips quedan todos excluidos no se revisa."""
    if not pats:
        return
    gone = []
    roms = []
    for rom in e["roms"]:
        rz = [z for z in rom["zips"] if not is_excluded(z, pats)]
        gone += [z for z in rom["zips"] if is_excluded(z, pats)]
        for p in rom["parts"]:
            gone += [z for z in p["zips"] if is_excluded(z, pats)]
            p["zips"] = [z for z in p["zips"] if not is_excluded(z, pats)]
        if rom["zips"] and not rz and not any(p["zips"] for p in rom["parts"]):
            continue  # solo usaba zips excluidos
        rom["zips"] = rz
        roms.append(rom)
    e["roms"] = roms
    e["zips"] = [z for z in e["zips"] if not is_excluded(z, pats)]
    e["excluded"] = sorted(set(gone), key=str.lower)


def rbf_basename(rbf):
    rbf = rbf.replace("\\", "/").split("/")[-1].strip()
    return rbf[:-4] if rbf.lower().endswith(".rbf") else rbf
# ---------------------------------------------------------- ARCHIVOS ----
def rel(root, p):
    return str(p.relative_to(root)).replace("\\", "/")


_ls_cache = {}


def find_ci(base, parts):
    """Devuelve base/parts[0]/... ignorando mayusculas (como el MiSTer en exFAT), o None."""
    cur = base
    for part in parts:
        if cur not in _ls_cache:
            try:
                _ls_cache[cur] = {n.lower(): n for n in os.listdir(cur)}
            except OSError:
                return None
        real = _ls_cache[cur].get(part.lower())
        if real is None:
            return None
        cur = cur / real
    return cur


def scan_mras(root, scan_dirs, problems):
    """MRAs unicas. Ignora symlinks (son la misma MRA) y deduplica copias/hardlinks por (nombre, contenido)."""
    entries, seen = [], set()
    for sd in scan_dirs:
        base = root / sd
        if not base.is_dir():
            continue
        for dp, _dirs, files in os.walk(base):
            for f in files:
                if not f.lower().endswith(".mra"):
                    continue
                p = Path(dp) / f
                if p.is_symlink():
                    if not p.exists():
                        problems.append((rel(root, p), "enlace roto (la MRA original ya no existe)"))
                    continue
                try:
                    raw = p.read_bytes()
                except OSError as e:
                    problems.append((rel(root, p), f"no se pudo leer: {e}"))
                    continue
                key = (f.lower(), hashlib.md5(raw).hexdigest())
                if key in seen:
                    continue
                seen.add(key)
                m = parse_mra(raw)
                m["path"] = p
                entries.append(m)
    return sorted(entries, key=lambda e: str(e["path"]).lower())


def index_rbfs(cores_dir):
    out = []
    if cores_dir.is_dir():
        for dp, _d, files in os.walk(cores_dir):
            out += [f for f in files if f.lower().endswith(".rbf")]
    return out


def make_rbf_checker(root, rbf_files):
    """Acepta  <rbf>.rbf  o  <rbf>_AAAAMMDD[x].rbf. Si el <rbf> trae carpeta se busca en ella."""
    lower = [f.lower() for f in rbf_files]
    cache = {}

    def check(raw):
        raw = raw.replace("\\", "/").strip("/")
        name = rbf_basename(raw)
        base_dir = raw.rsplit("/", 1)[0] if "/" in raw else None
        key = (base_dir or "", name.lower())
        if key not in cache:
            pat = re.compile(re.escape(name.lower()) + r"(_\d{6,8}[a-z]?)?\.rbf$")
            pool = lower
            if base_dir:
                d = root / base_dir
                pool = [f.lower() for f in os.listdir(d)] if d.is_dir() else []
            cache[key] = any(pat.match(f) for f in pool)
        return cache[key]
    return check


class ZipIndex:
    """Indice de los zips: por cada uno, sus entradas (nombre, CRC). Con cache en disco
    (tamaño + fecha) para que las siguientes corridas no relean miles de zips."""

    def __init__(self, root, use_cache):
        self.root = root
        self.use_cache = use_cache
        self.mem = {}      # path -> (set nombres minusculas, set CRCs) | None si esta danado
        self.disk = {}     # ruta relativa -> [[size, mtime_ns], [[nombre, crc], ...]]
        self.dirty = False
        self.global_map = None
        self.bad = {}      # path -> motivo
        if use_cache:
            try:
                data = json.loads((STATE_DIR / CACHE_FILE).read_text(encoding="utf-8"))
                if data.get("root") == str(root):
                    self.disk = data.get("zips", {})
            except (OSError, ValueError):
                pass

    def _entries(self, path):
        """Lista [[nombre, crc]] del zip, usando la cache si sigue vigente. Lanza si esta danado."""
        st = path.stat()
        if st.st_size == 0:
            raise zipfile.BadZipFile("archivo vacio (0 bytes)")
        key = rel(self.root, path) if self.root in path.parents else str(path)
        sig = [st.st_size, st.st_mtime_ns]
        d = self.disk.get(key)
        if d and d[0] == sig:
            return d[1]
        with zipfile.ZipFile(path) as zf:
            ents = [[zi.filename.replace("\\", "/"), zi.CRC] for zi in zf.infolist() if not zi.is_dir()]
        self.disk[key] = [sig, ents]
        self.dirty = True
        return ents

    def info(self, path):
        if path not in self.mem:
            try:
                ents = self._entries(path)
                self.mem[path] = ({n.lower() for n, _c in ents}, {c for _n, c in ents})
            except zipfile.BadZipFile as e:
                self.bad[path] = str(e) if "vacio" in str(e) else "no es un zip valido"
                self.mem[path] = None
            except OSError as e:
                self.bad[path] = f"no se pudo leer: {e}"
                self.mem[path] = None
        return self.mem[path]

    def build_global(self, dirs):
        """CRC -> [(zip, entrada)] de TODOS los zips de las carpetas de ROMs (para buscar donde esta algo)."""
        if self.global_map is None:
            self.global_map = defaultdict(list)
            for d in dirs:
                if not d.is_dir():
                    continue
                for n in sorted(os.listdir(d)):
                    if not n.lower().endswith(".zip"):
                        continue
                    p = d / n
                    try:
                        for name, crc in self._entries(p):
                            self.global_map[crc].append((p, name))
                    except (zipfile.BadZipFile, OSError):
                        continue
        return self.global_map

    def save(self):
        if self.use_cache and self.dirty:
            try:
                (STATE_DIR / CACHE_FILE).write_text(json.dumps({"root": str(self.root), "zips": self.disk},
                                                               separators=(",", ":")), encoding="utf-8")
            except OSError:
                pass

# ----------------------------------------------- EMULACION DEL CARGADOR ----
def locate_part(part, zlist, resolve, zidx):
    """Replica el bucle del MiSTer. -> ('crc'|'name', zip) o None.
    Por cada zip (en orden): si existe, busca por CRC y si no por nombre; el primero que acierta gana."""
    for z in zlist:
        p = resolve(z)
        if p is None:
            continue  # zip inexistente: se salta
        info = zidx.info(p)
        if info is None:
            continue  # zip danado: tampoco sirve
        names, crcs = info
        if part["crc"] is not None and part["crc"] in crcs:
            return "crc", z
        if part["name"].replace("\\", "/").lower() in names:
            return "name", z
    return None


def check_mra(e, resolve, zidx):
    """-> dict(loads, fails[(rom, [partes que faltan])], warns[partes solo por nombre con CRC distinto])."""
    results, warns = [], []
    for rom in e["roms"]:
        if not rom["parts"]:
            continue
        lack = []
        for part in rom["parts"]:
            zl = part["zips"] or rom["zips"]  # el zip= de la <part> sustituye al del <rom>
            hit = locate_part(part, zl, resolve, zidx)
            if hit is None:
                lack.append(part)
            elif hit[0] == "name" and part["crc"] is not None:
                warns.append((part, hit[1]))
        results.append((rom, lack))
    by_index = defaultdict(list)
    for rom, lack in results:
        by_index[rom["index"]].append((rom, lack))
    fails = []
    for index, items in by_index.items():
        if index == 0 and len(items) > 1:
            if any(not lack for _r, lack in items):
                continue  # de varios <rom index="0"> alternativos basta con uno completo
            fails.append(min(items, key=lambda it: len(it[1])))
        else:
            fails += [(r, l) for r, l in items if l]
    return {"loads": not fails, "fails": fails, "warns": warns}


def fmt_part(p):
    return f"{p['name']} ({format(p['crc'], '08x') if p['crc'] is not None else 'sin crc'})"
# ------------------------------------------------------------ REBUILD ----
def plan_rebuild(root, solvable, resolve_target, found_map):
    """solvable: [(entrada MRA, [(rom, [partes que faltan])])]. -> ({zip destino: {entrada: (zip origen, entrada origen)}}, [no automatizables])."""
    targets, manual = {}, []
    for e, fails in solvable:
        for rom, lack in fails:
            declared = rom["zips"]
            missing = [z for z in declared if resolve_target(z)[0] is None]  # (existente, destino)
            if not missing:
                manual.append((rel(root, e["path"]), "todos los zips declarados existen pero estan incompletos"))
                continue
            dest = resolve_target(missing[0])[1]
            bucket = targets.setdefault(dest, {})
            for part in lack:
                src = found_map[part["crc"]][0]
                bucket.setdefault(part["name"], src)
    return targets, manual


def write_zip(dest, entries):
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    by_src = defaultdict(list)
    for ename, (sp, sname) in entries.items():
        by_src[sp].append((ename, sname))
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
        for sp, items in by_src.items():
            with zipfile.ZipFile(sp) as zin:
                for ename, sname in items:
                    zi = zipfile.ZipInfo(ename, date_time=zin.getinfo(sname).date_time)
                    zi.compress_type = zipfile.ZIP_DEFLATED
                    with zin.open(sname) as s, out.open(zi, "w") as d:
                        shutil.copyfileobj(s, d, 1 << 20)
    os.replace(tmp, dest)


def undo_rebuild(root):
    f = STATE_DIR / REBUILT_FILE
    if not f.exists():
        sys.exit("No hay zips creados por --rebuild que deshacer.")
    data = json.loads(f.read_text(encoding="utf-8"))
    n = 0
    for r in data.get("created", []):
        p = root / r
        if p.is_file():
            p.unlink()
            n += 1
    f.unlink()
    log(f"Zips eliminados: {n}")


# =====================================================================
#  PARTE 4 - FUENTE DE ROMS LOCAL (jd_roms.db) Y COPIA
# =====================================================================
def confirm(msg, args, strict=False):
    """Pregunta s/N. Sin terminal: strict -> no; si no -> si. --yes siempre si."""
    if args.yes:
        return True
    if not (sys.stdin and sys.stdin.isatty()):
        return not strict
    try:
        return input(msg + " [s/N] ").strip().lower() in ("s", "si", "sí", "y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def db_source(db_path):
    """Ruta de la fuente con la que se genero la DB (primeras lineas '# source=...')."""
    try:
        with open(db_path, encoding="utf-8") as fh:
            for _ in range(4):
                ln = fh.readline()
                if ln.startswith("# source="):
                    return ln[len("# source="):].rstrip("\n")
    except OSError:
        pass
    return None


def build_roms_db(source, db_path):
    if not source.is_dir():
        sys.exit(f"No existe la carpeta de ROMs (fuente): {source}\n"
                 "Revisa SOURCE_DIR en el script, usa --source, o monta/enciende el disco.")
    log(f"Escaneando la fuente (solo la primera vez o con rescan): {source}")
    rows = []
    for dp, dirs, files in os.walk(source):
        dirs[:] = [d for d in dirs if not d.startswith(("@", "#"))]  # @eaDir, #recycle de Synology
        for f in files:
            if f.lower().endswith(".zip"):
                p = Path(dp) / f
                try:
                    rows.append((f, p.relative_to(source).as_posix(), p.stat().st_size))
                except OSError:
                    continue
    rows.sort(key=lambda r: r[0].lower())
    tmp = db_path.with_name(db_path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("# jd_roms.db - indice de zips de la fuente (texto plano: nombre<TAB>ruta relativa<TAB>tamano)\n")
        fh.write(f"# source={source}\n")
        fh.write(f"# generated={datetime.now():%Y-%m-%d %H:%M:%S} | zips={len(rows)}\n")
        for n, r, s in rows:
            fh.write(f"{n}\t{r}\t{s}\n")
    os.replace(tmp, db_path)
    log(f"jd_roms.db: {len(rows)} zips indexados -> {db_path}")
    return len(rows)


def load_roms_db(source, db_path, rescan=False, create=True):
    """-> {nombre_minuscula: (nombre, ruta_relativa, tamano)} o None (create=False y no hay DB valida)."""
    exists = db_path.is_file()
    same = exists and db_source(db_path) == str(source)
    if exists and not same:
        log("[aviso] jd_roms.db es de otra ruta de fuente" + (": se regenera" if create else ": se ignora"))
    if not create and not same:
        return None
    if rescan or not same:
        build_roms_db(source, db_path)
    idx = {}
    for ln in db_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not ln or ln.startswith("#"):
            continue
        p = ln.split("\t")
        if len(p) >= 2:
            size = int(p[2]) if len(p) > 2 and p[2].isdigit() else None
            idx.setdefault(p[0].lower(), (p[0], p[1], size))
    return idx


def copy_missing(root, source, idx, names, args):
    """Copia de la fuente los zips `names`. Nombre simple -> games/mame. Con ruta (/hbmame/x.zip) -> no se
    busca (la fuente no tiene HBMAME). Nunca sobrescribe. -> reporte (dict)."""
    dest_dir = root / DEFAULT_ROM_DIRS[0]
    rep = {"source": str(source), "dest": rel(root, dest_dir), "copied": [], "failed": [],
           "not_in_source": [], "other_source": [], "note": ""}
    todo = []
    for z in sorted(names, key=str.lower):
        norm = z.replace("\\", "/").strip()
        if "/" in norm.strip("/"):
            rep["other_source"].append(z)
            continue
        e = idx.get(norm.strip("/").lower())
        if e is None:
            rep["not_in_source"].append(z)
        else:
            todo.append((z.strip("/"), dest_dir / z.strip("/"), source / e[1], e[2] or 0))
    total = sum(t[3] for t in todo)
    log(f"Zips pedidos por las MRA que no cargan: {len(names)} | en la fuente: {len(todo)} "
        f"({total / 1024 ** 2:,.0f} MB) | no estan en la fuente: {len(rep['not_in_source'])} "
        f"(normal para clones de un set merged) | otra fuente (HBMAME): {len(rep['other_source'])}")
    if not todo:
        return rep
    if args.dry_run:
        for z, _d, _s, size in todo:
            log(f"  (dry-run) {z}  {size / 1024 ** 2:,.1f} MB")
        rep["note"] = "dry-run: no se copio nada"
        return rep
    if total > BIG_COPY and not confirm(f"Se copiaran {total / 1024 ** 3:,.1f} GB a {rep['dest']}. Continuar?", args):
        rep["note"] = "copia cancelada por el usuario"
        log("Copia cancelada.")
        return rep
    dest_dir.mkdir(parents=True, exist_ok=True)
    for i, (z, dest, src, size) in enumerate(todo, 1):
        tmp = dest.with_name(dest.name + ".part")
        try:
            if not src.is_file():
                raise IOError("ya no existe en la fuente (la DB esta vieja: usa --rescan)")
            if dest.exists():
                raise IOError("ya existe en destino (no se sobrescribe)")
            with open(src, "rb") as fi, open(tmp, "wb") as fo:
                shutil.copyfileobj(fi, fo, 4 << 20)
            if size and tmp.stat().st_size != size:
                raise IOError("tamano distinto al de la DB")
            if not zipfile.is_zipfile(tmp):
                raise IOError("lo copiado no es un zip valido")
            os.replace(tmp, dest)
            rep["copied"].append(z)
            log(f"  [{i}/{len(todo)}] OK  {z}  ({dest.stat().st_size / 1024 ** 2:,.1f} MB)")
        except Exception as ex:  # noqa
            rep["failed"].append((z, str(ex)))
            log(f"  [{i}/{len(todo)}] ERR {z}: {ex}")
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
    return rep


# =====================================================================
#  PARTE 5 - ANALISIS UNIFICADO Y REPORTE (JD_Check.log)
# =====================================================================
def prepare_excludes(root, args):
    if not (STATE_DIR / EXCLUDE_FILE).exists():
        try:
            (STATE_DIR / EXCLUDE_FILE).write_text(EXCLUDE_TEMPLATE, encoding="utf-8")
        except OSError:
            pass
    return load_excludes(root, args.exclude)


def analyze(root, args, excl):
    """Revision completa (replica el cargador del MiSTer). Devuelve un SimpleNamespace con todo."""
    _ls_cache.clear()
    scan_dirs = list(args.scan or CHECK_SCAN)
    rom_dirs = list(args.rom_dir or DEFAULT_ROM_DIRS)
    cores_dir = root / args.cores_dir
    mra_problems = []
    entries = scan_mras(root, scan_dirs, mra_problems)
    for e in entries:
        apply_exclude(e, excl)
    if not entries:
        sys.exit(f"No encontre ninguna .mra en: {', '.join(scan_dirs)}")
    log(f"MRAs unicas: {len(entries)} | zips excluidos ({EXCLUDE_FILE}): {len(excl)}")

    notes = []
    rbf_files = index_rbfs(cores_dir)
    cores_ok = cores_dir.is_dir()
    if not cores_ok:
        notes.append(f"[!] No existe la carpeta de cores ({args.cores_dir}): no se reviso el .rbf.")
    rbf_ok = make_rbf_checker(root, rbf_files)
    oc = root / ORGANIZER / "cores"
    n_org = 0
    if (root / ORGANIZER).is_dir():
        for dp, _d, fs in os.walk(root / ORGANIZER):
            n_org += sum(1 for f in fs if f.lower().endswith(".mra"))
    org_cores_bad = bool(n_org) and not (oc.is_dir() and index_rbfs(oc))
    org_rbf_ok = None
    if org_cores_bad:
        notes.append(f"[!] {ORGANIZER}/cores no existe o esta vacio: las {n_org} MRA de {ORGANIZER} daran "
                     f"'No rbf found!' en el MiSTer. Corre 'organize' (lo crea).")
    elif n_org:
        org_rbf_ok = make_rbf_checker(root, index_rbfs(oc))
    rom_paths = [root / d for d in rom_dirs]
    rom_found = [d for d in rom_dirs if (root / d).is_dir()]
    if not rom_found:
        notes.append(f"[!] No existe ninguna carpeta de ROMs ({', '.join(rom_dirs)}): se asume vacia.")
    primary = rom_paths[0]

    def resolve(name):
        """Como el MiSTer: nombre simple -> <primera carpeta de ROMs>/nombre ; '/x/y.zip' -> games/x/y.zip."""
        name = name.replace("\\", "/")
        if name.startswith("/") or "/" in name.strip("/"):
            return find_ci(root / "games", [x for x in name.split("/") if x])
        return find_ci(primary.parent, [primary.name, name]) if primary.parent.is_dir() else None

    def target_path(name):
        """(ruta existente o None, ruta donde se crearia)."""
        name = name.replace("\\", "/")
        if name.startswith("/") or "/" in name.strip("/"):
            parts = [x for x in name.split("/") if x]
            return find_ci(root / "games", parts), root / "games" / Path(*parts)
        return resolve(name), primary / name

    zidx = ZipIndex(root, not args.no_cache)
    log("Leyendo el contenido de los zips (la primera vez tarda; despues usa la cache)...")

    missing_cores = Counter()
    by_core = defaultdict(list)
    used_rbf = set()
    status, fail_info = {}, {}
    need_zips, zip_orig = Counter(), {}
    unneeded, warn_list, solvable = [], [], []
    n_flagged = 0

    for e in entries:
        probs = []
        rp = rel(root, e["path"])
        if not e["rbf"].strip():
            mra_problems.append((rp, "sin <rbf>"))
        else:
            used_rbf.add(rbf_basename(e["rbf"]).lower())
        if not e["setname"].strip():
            mra_problems.append((rp, "sin <setname>"))
        if not e["zips"] and not e.get("excluded"):
            mra_problems.append((rp, "sin <rom zip=...> declarado"))
        core = rbf_basename(e["rbf"]) or "(sin rbf)"
        if cores_ok and e["rbf"].strip() and not rbf_ok(e["rbf"]):
            probs.append(f"core .rbf: {core}")
            missing_cores[core] += 1

        res = check_mra(e, resolve, zidx)
        declared_absent = [z for z in e["zips"] if resolve(z) is None]
        if res["warns"]:
            warn_list.append((rp, res["warns"]))
        if res["loads"]:
            status[id(e)] = "CARGA"
            if declared_absent:
                unneeded.append((rp, declared_absent))
        else:
            gmap = zidx.build_global(rom_paths)
            lacking_all = [p for _r, lack in res["fails"] for p in lack]
            elsewhere = [p for p in lacking_all if p["crc"] is not None and p["crc"] in gmap]
            nowhere = [p for p in lacking_all if p not in elsewhere]
            fail_info[id(e)] = (res["fails"], nowhere, elsewhere)
            if not nowhere:
                status[id(e)] = "SOLUCIONABLE"
                solvable.append((e, res["fails"]))
                probs.append(f"NO CARGA (solucionable): {len(elsewhere)} parte(s) estan en otro zip")
            else:
                status[id(e)] = "FALTA"
                probs.append(f"NO CARGA - FALTA ROM: {len(nowhere)} parte(s) no estan en ningun zip")
            # zips declarados que no existen y que esta MRA necesita (para copiarlos de la fuente)
            seen_z = set()
            for rom, lack in res["fails"]:
                for part in lack:
                    for z in (part["zips"] or rom["zips"]):
                        if z.lower() not in seen_z and resolve(z) is None:
                            seen_z.add(z.lower())
                            need_zips[z.lower()] += 1
                            zip_orig.setdefault(z.lower(), z)
        if n_org and not org_cores_bad and org_rbf_ok and e["rbf"].strip() and cores_ok \
                and not org_rbf_ok(e["rbf"]) and rbf_ok(e["rbf"]):
            mra_problems.append((rp, f"el .rbf esta en {args.cores_dir} pero falta en {ORGANIZER}/cores"))
        if probs:
            n_flagged += 1
            by_core[core].append((e["setname"], rp, probs))

    bad_zips = dict(zidx.bad)
    zidx.save()

    groups = defaultdict(list)
    for f in rbf_files:
        groups[RE_DATED.sub("", f[:-4]).lower()].append(f)
    orphans = sorted(f for b, fs in groups.items() if b not in used_rbf for f in fs)
    multi = {b: sorted(fs) for b, fs in groups.items() if len(fs) > 1}

    R = SimpleNamespace(
        entries=entries, mra_problems=mra_problems, notes=notes, cores_ok=cores_ok, rom_found=rom_found,
        rom_dirs=rom_dirs, rom_paths=rom_paths, resolve=resolve, target_path=target_path, zidx=zidx,
        status=status, fail_info=fail_info, need_zips=need_zips, zip_orig=zip_orig, unneeded=unneeded,
        warn_list=warn_list, solvable=solvable, missing_cores=missing_cores, by_core=by_core,
        orphans=orphans, multi=multi, bad_zips=bad_zips, excl=excl, n_flagged=n_flagged,
        n_load=sum(1 for v in status.values() if v == "CARGA"),
        n_solv=sum(1 for v in status.values() if v == "SOLUCIONABLE"),
        n_falta=sum(1 for v in status.values() if v == "FALTA"))
    return R


def do_rebuild(root, R, args):
    """Crea los zips de las MRA SOLUCIONABLES a partir de tus zips. -> (creados, manual, targets)."""
    if not R.solvable:
        log("rebuild: no hay nada solucionable.")
        return [], [], {}
    gmap = dict(R.zidx.build_global(R.rom_paths))
    targets, manual = plan_rebuild(root, R.solvable, R.target_path, gmap)
    targets = {d: ents for d, ents in targets.items() if not d.exists()}
    log(f"rebuild: {len(targets)} zip(s) por crear")
    for d, ents in sorted(targets.items(), key=lambda kv: str(kv[0]).lower()):
        log(f"  {rel(root, d)}  <- {len(ents)} archivo(s)")
    for r, why in manual[:10]:
        log(f"  [manual] {r}: {why}")
    created = []
    if targets and not args.dry_run:
        if confirm(f"Crear {len(targets)} zip(s) en games/? Nunca sobrescribe.", args, strict=True):
            for d, ents in targets.items():
                try:
                    write_zip(d, ents)
                    created.append(rel(root, d))
                except (OSError, zipfile.BadZipFile) as ex:
                    log(f"  [error] {rel(root, d)}: {ex}")
            if created:
                prev = []
                try:
                    prev = json.loads((STATE_DIR / REBUILT_FILE).read_text(encoding="utf-8")).get("created", [])
                except (OSError, ValueError):
                    pass
                (STATE_DIR / REBUILT_FILE).write_text(json.dumps({"created": sorted(set(prev + created))}, indent=1),
                                                encoding="utf-8")
                log(f"Zips creados: {len(created)} (para deshacer: undo-rebuild)")
        else:
            log("(sin confirmacion: no se creo nada; usa --yes si corre sin terminal)")
    return created, manual, targets


def write_report(root, R, args, source_idx, copy_rep, rb, tor_names=None):
    L = [
        "CHECK - JD Update (MRA / .rbf / ZIP)",
        f"Fecha: {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"Raiz: {root}",
        f"Cores (.rbf): {args.cores_dir}" + ("" if R.cores_ok else "  (NO EXISTE)"),
        f"ROMs: {', '.join(R.rom_found) if R.rom_found else '(ninguna carpeta existe)'}  "
        f"(nombres simples se buscan en {R.rom_dirs[0]})",
        f"Fuente de ROMs: {args.source}",
        "Criterio: replica el cargador del MiSTer, parte por parte (un zip declarado que no existe solo "
        "cuenta si la MRA no carga)",
        f"MRAs: {len(R.entries)} | CARGAN: {R.n_load} | NO CARGAN: {R.n_solv + R.n_falta} "
        f"(solucionables: {R.n_solv} | falta ROM: {R.n_falta}) | con aviso de CRC: {len(R.warn_list)}",
        f"Cores .rbf Missing: {len(R.missing_cores)} | problemas de MRA: {len(R.mra_problems)} | "
        f"zips danados: {len(R.bad_zips)}",
    ]
    L += R.notes
    n_excl = sum(1 for e in R.entries if e.get("excluded"))
    L.append(f"Excluidos ({EXCLUDE_FILE}): {len(R.excl)} patron(es): {', '.join(R.excl) if R.excl else '(ninguno)'} "
             f"| MRAs afectadas: {n_excl}")
    L += ["", "=== MRA: PROBLEMAS ==="]
    L += [f"{t}\t{p}" for p, t in sorted(R.mra_problems, key=lambda x: (x[1], x[0].lower()))] or ["(ninguno)"]
    L += ["", "=== MISSING CORES (.rbf) - cuantas MRA los necesitan ==="]
    L += [f"{n}\t{c}" for c, n in sorted(R.missing_cores.items(), key=lambda kv: (-kv[1], kv[0].lower()))] or ["(ninguno)"]
    L += ["", "=== ZIPS DANADOS (existen pero no sirven) ==="]
    L += [f"{rel(root, p) if root in p.parents else p}\t{why}"
          for p, why in sorted(R.bad_zips.items(), key=lambda kv: str(kv[0]).lower())] or ["(ninguno)"]

    if copy_rep:
        r = copy_rep
        L += ["", f"=== COPIA DESDE LA FUENTE ({r['source']}) ===",
              f"Copiados a {r['dest']}: {len(r['copied'])} | con error: {len(r['failed'])} | "
              f"no estan en la fuente: {len(r['not_in_source'])} | de otra fuente (HBMAME): {len(r['other_source'])}"]
        if r["note"]:
            L.append(f"Nota: {r['note']}")
        if r["copied"]:
            L.append("Copiados: " + ", ".join(sorted(r["copied"], key=str.lower)))
        if r["failed"]:
            L.append("Con error: " + "; ".join(f"{z} ({e})" for z, e in r["failed"]))

    L += ["", f"=== NO CARGAN - FALTA ROM ({R.n_falta} MRA): hay archivos que no estan en NINGUN zip tuyo ==="]
    for e in R.entries:
        if R.status.get(id(e)) == "FALTA":
            fails, nowhere, _el = R.fail_info[id(e)]
            L.append(f"{rel(root, e['path'])}\t[{e['setname'] or '-'}]\tzips declarados: {', '.join(e['zips']) or '-'}")
            L += [f"    falta: {fmt_part(p)}" for p in nowhere[:12]]
            if len(nowhere) > 12:
                L.append(f"    ... y {len(nowhere) - 12} mas")
    if not R.n_falta:
        L.append("(ninguna)")

    L += ["", "=== ZIPS QUE HAY QUE CONSEGUIR (declarados, no existen y una MRA que no carga los necesita) ==="]
    if R.need_zips:
        for z, n in sorted(R.need_zips.items(), key=lambda kv: (-kv[1], kv[0])):
            if source_idx is None:
                where = ""
            elif "/" in R.zip_orig[z].strip("/"):
                where = "  [ruta: no se busca en la fuente]"
            else:
                where = "  [en la fuente: SI -> corre copy]" if z in source_idx else "  [en la fuente: no]"
            if tor_names is not None:
                base = R.zip_orig[z].replace("\\", "/").strip("/").split("/")[-1].lower()
                where += "  [torrent: SI]" if base in tor_names else "  [torrent: no]"
            L.append(f"{n}\t{R.zip_orig[z]}{where}")
    else:
        L.append("(ninguno)")

    L += ["", f"=== NO CARGAN - SOLUCIONABLES ({R.n_solv} MRA): los archivos estan en otro zip tuyo; usa rebuild ==="]
    gm = R.zidx.global_map or {}
    for e in R.entries:
        if R.status.get(id(e)) == "SOLUCIONABLE":
            fails, nowhere, elsewhere = R.fail_info[id(e)]
            L.append(f"{rel(root, e['path'])}\t[{e['setname'] or '-'}]\tzips declarados: {', '.join(e['zips']) or '-'}")
            for p in elsewhere[:6]:
                sp, sname = gm[p["crc"]][0]
                L.append(f"    {fmt_part(p)} -> esta en {rel(root, sp)} ({sname})")
            if len(elsewhere) > 6:
                L.append(f"    ... y {len(elsewhere) - 6} mas")
    if not R.n_solv:
        L.append("(ninguna)")

    if rb is not None:
        created, manual, targets = rb
        L += ["", f"=== REBUILD: {len(created)} zip(s) creados" + (" (dry-run)" if args.dry_run else "") + " ==="]
        if args.dry_run or not created:
            L += [f"{rel(root, d)}\t{len(ents)} archivo(s)" for d, ents in sorted(targets.items(), key=lambda kv: str(kv[0]).lower())]
        else:
            L += created
        L += [f"[manual] {r}: {why}" for r, why in manual]

    L += ["", f"=== AVISO: partes que solo coinciden por nombre, con CRC distinto ({len(R.warn_list)} MRA; el MiSTer las carga) ==="]
    for rp, ws in sorted(R.warn_list, key=lambda x: x[0].lower()):
        L.append(f"{rp}\t{len(ws)} parte(s), ej.: " + ", ".join(f"{p['name']} en {z}" for p, z in ws[:3]))
    if not R.warn_list:
        L.append("(ninguno)")
    L += ["", f"=== INFO: zips declarados que no existen pero NO hacen falta ({len(R.unneeded)} MRA; sets merged) ==="]
    L += [f"{rp}\t{', '.join(zs)}" for rp, zs in sorted(R.unneeded, key=lambda x: x[0].lower())] or ["(ninguno)"]
    L += ["", "=== INFO: .rbf ===",
          f".rbf sin ninguna MRA ({len(R.orphans)}): " + (", ".join(R.orphans) if R.orphans else "(ninguno)"),
          f"Cores con varias versiones ({len(R.multi)}; el MiSTer usa la mas reciente, las otras sobran):"]
    L += [f"  {b}: {', '.join(fs)}" for b, fs in sorted(R.multi.items())] or ["  (ninguno)"]
    L += ["", "=== DETALLE POR CORE ==="]
    for core in sorted(R.by_core, key=str.lower):
        items = R.by_core[core]
        L.append(f"\n[{core}]  {len(items)} MRA con Missing")
        for setname, rp, probs in sorted(items, key=lambda x: x[1].lower()):
            L.append(f"  {setname or '-'}\t{rp}\tMissing -> {' | '.join(probs)}")
    if not R.by_core:
        L.append("\n(Nothing missing)")
    (STATE_DIR / args.log).write_text("\n".join(L) + "\n", encoding="utf-8")


def print_summary(root, R, args):
    for n in R.notes:
        log(n)
    log(f"MRAs: {len(R.entries)} | CARGAN: {R.n_load} | NO CARGAN: {R.n_solv + R.n_falta} "
        f"(solucionables con rebuild: {R.n_solv} | falta ROM: {R.n_falta}) | con aviso de CRC: {len(R.warn_list)}")
    log(f"Zips declarados ausentes pero innecesarios (merged): {len(R.unneeded)} MRA | zips por conseguir: {len(R.need_zips)}")
    log(f"Cores .rbf Missing: {len(R.missing_cores)} | problemas de MRA: {len(R.mra_problems)} | zips danados: {len(R.bad_zips)}")
    log(f"Info: .rbf sin MRA: {len(R.orphans)} | cores con varias versiones: {len(R.multi)}")
    log(f"Log: {STATE_DIR / args.log}   (para leerlo aqui: python3 jd_update.py log, o la opcion v del menu)")


# =====================================================================
#  PARTE 6 - COMANDOS Y MENU
# =====================================================================
# =====================================================================
#  CARPETAS ESPECIALES: _DEBUT, _PAST DEBUT, _UPDATED, _PAST UPDATED, _A-Z
# =====================================================================
def load_history(root):
    try:
        d = json.loads((STATE_DIR / HISTORY_FILE).read_text(encoding="utf-8"))
        return d.get("runs", []) if d.get("root") == str(root) else []
    except (OSError, ValueError):
        return []


def record_history(root, dry_run, mode="symlink"):
    """Guarda en el historial las MRA que bajo este update (solo si hubo alguna nueva o actualizada).
    Una MRA cuenta como actualizada si cambio ella misma O si cambio el .rbf de su core."""
    with _changes_lock:
        new, upd, bases = set(CHANGES["new"]), set(CHANGES["updated"]), set(CHANGES["rbf"])
        for k in CHANGES:
            CHANGES[k].clear()
    if dry_run:
        return

    def rels(paths):
        out = []
        for p in paths:
            try:
                out.append(rel(root, Path(p)))
            except ValueError:
                pass
        return sorted(out, key=str.lower)
    new, upd = rels(new), rels(upd)
    by_core = set()
    if bases:  # MRA cuyo core (.rbf) se actualizo
        scan = list(DEFAULT_SCAN) + ([ORGANIZER] if mode == "move" else [])
        new_set = set(new)
        for mp in find_mras(root, scan):
            try:
                m = parse_mra(mp.read_bytes())
            except OSError:
                continue
            r = rel(root, mp)
            if r not in new_set and rbf_basename(m["rbf"]).lower() in bases:
                by_core.add(r)
        upd = sorted(set(upd) | by_core, key=str.lower)
    if not new and not upd:
        return
    runs = load_history(root)
    runs.append({"date": f"{datetime.now():%Y-%m-%d %H.%M.%S}", "new": new, "updated": upd})
    runs = runs[-HISTORY_MAX:]
    try:
        (STATE_DIR / HISTORY_FILE).write_text(json.dumps({"root": str(root), "runs": runs}, indent=1,
                                                         ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log(f"[!] No pude guardar {HISTORY_FILE}: {e}")
    por_core = f" (de ellas {len(by_core)} por actualizarse su core: {', '.join(sorted(bases)[:6])}" \
               f"{'...' if len(bases) > 6 else ''})" if by_core else ""
    log(f"MRA nuevas en este update: {len(new)} | actualizadas: {len(upd)}{por_core}  "
        "(van a _DEBUT / _UPDATED al organizar)")


def az_letter(name):
    """Carpeta segun la primera letra del nombre (sin acentos): _A ... _Z; cualquier otra cosa -> _#.
    El prefijo '_' es obligatorio: el MiSTer solo muestra las carpetas que empiezan con _."""
    n = unicodedata.normalize("NFD", name.strip())
    c = n[:1]
    return "_" + (c.upper() if c.isascii() and c.isalpha() else "#")


def build_special(root, plan, runs):
    """-> ({ruta dentro de _@JD_Organizer: MRA origen}, conflictos de nombre, cuentas por carpeta)."""
    want, seen, conflicts = {}, set(), 0
    counts = Counter()
    by_name = {}
    for it in plan:
        by_name.setdefault(it["path"].name.lower(), it["path"])

    def src_for(r):
        p = root / r
        return p if p.is_file() else by_name.get(Path(r).name.lower())

    def add(folder, src, top):
        nonlocal conflicts
        key = f"{folder}/{src.name}"
        if key.lower() in seen:
            conflicts += 1
            return
        seen.add(key.lower())
        want[key] = src
        counts[top] += 1

    for key, cur, past in (("new", "_DEBUT", "_PAST DEBUT"), ("updated", "_UPDATED", "_PAST UPDATED")):
        rr = [r for r in runs if r.get(key)]
        if not rr:
            continue
        for r in rr[-1][key]:
            s = src_for(r)
            if s:
                add(cur, s, cur)
        used = set()
        for run in reversed(rr[-1 - PAST_KEEP:-1]):
            label = "_" + run["date"][:16]          # con '_' para que el MiSTer la muestre
            if label in used:
                label = "_" + run["date"]
            used.add(label)
            for r in run[key]:
                s = src_for(r)
                if s:
                    add(f"{past}/{label}", s, past)
    for it in sorted(plan, key=lambda i: (i["path"].name.lower(), str(i["path"]).lower())):
        add(f"_A-Z/{az_letter(it['path'].name)}", it["path"], "_A-Z")
    return want, conflicts, counts


def link_ok(src, dst, mode):
    if mode == "symlink":
        try:
            return os.readlink(dst) == os.path.relpath(src, dst.parent)
        except OSError:
            return False
    return same_target(src, dst, mode)


def sync_special(root, org, plan, mode, dry_run):
    """Deja las carpetas especiales exactamente como deben estar: crea lo que falta y quita lo que sobra.
    Son solo enlaces (los que no lo admiten usan el mismo modo que el organizador; 'move' usa enlaces)."""
    emode = mode if mode in ("symlink", "hardlink", "copy") else "symlink"
    want, conflicts, counts = build_special(root, plan, load_history(root))
    have = {}
    for d in SPECIAL_DIRS + OLD_SPECIAL:
        base = org / d
        if base.is_dir():
            for dp, _dn, fn in os.walk(base):
                for f in fn:
                    p = Path(dp) / f
                    have[p.relative_to(org).as_posix()] = p
    stale = [k for k in have if k not in want]
    todo = [k for k, s in want.items() if not (k in have and link_ok(s, org / k, emode))]
    resumen = " | ".join(f"{d}: {counts[d]}" for d in SPECIAL_DIRS if counts[d])
    log(f"Carpetas especiales -> {resumen or '(vacias)'} | por crear/refrescar: {len(todo)} | por quitar: "
        f"{len(stale)} | nombres repetidos en la misma carpeta (se deja el primero): {conflicts}")
    if dry_run:
        return
    for k in stale:
        try:
            have[k].unlink()
        except OSError:
            pass
    errors = 0
    for k in todo:
        try:
            place(want[k], org / k, emode)
        except OSError as e:
            errors += 1
            if errors == 1:
                log(f"[!] No pude crear {emode} en {k}: {e}")
    if errors:
        log(f"[!] Fallaron {errors} enlaces de las carpetas especiales (exFAT/FAT32: usa --mode copy).")
    for d in SPECIAL_DIRS + OLD_SPECIAL:  # quita subcarpetas y carpetas vacias
        base = org / d
        if base.is_dir():
            for dp, _dn, _fn in os.walk(base, topdown=False):
                try:
                    Path(dp).rmdir()
                except OSError:
                    pass


def clear_special(org):
    """Quita por completo las carpetas especiales (solo enlaces / MRA copiadas por el script)."""
    for d in SPECIAL_DIRS + OLD_SPECIAL:
        base = org / d
        if not base.is_dir():
            continue
        for dp, _dn, fn in os.walk(base, topdown=False):
            for f in fn:
                p = Path(dp) / f
                if p.is_symlink() or p.suffix.lower() == ".mra":
                    try:
                        p.unlink()
                    except OSError:
                        pass
            try:
                Path(dp).rmdir()
            except OSError:
                pass


# =====================================================================
#  ACTUALIZAR EL SCRIPT Y LAS LISTAS DESDE GITHUB
# =====================================================================
_RESTART = False


def gh_fetch(repo, branch, path, name):
    """Contenido de <path>/<name> en GitHub (raw). `repo` puede ser tambien una carpeta local (un clon).
    None si no existe."""
    if os.path.isdir(repo):
        f = Path(repo) / path / name
        return f.read_bytes() if f.is_file() else None
    url = f"https://raw.githubusercontent.com/{repo}/{branch}/{path.strip('/')}/{quote(name)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def list_sections(data):
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    try:
        cp.read_string(data.decode("utf-8-sig", errors="replace"))
        return set(cp.sections())
    except configparser.Error:
        return set()


def diff_summary(name, cur, remote):
    if cur is None:
        return "archivo nuevo"
    a = cur.decode("utf-8", errors="replace").splitlines()
    b = remote.decode("utf-8", errors="replace").splitlines()
    add = rem = 0
    for ln in difflib.unified_diff(a, b, lineterm="", n=0):
        if ln.startswith("+") and not ln.startswith("+++"):
            add += 1
        elif ln.startswith("-") and not ln.startswith("---"):
            rem += 1
    txt = f"+{add} / -{rem} lineas"
    if name.endswith(".list") and name != EXCLUDE_FILE:
        s1, s2 = list_sections(cur), list_sections(remote)
        if s2 - s1:
            txt += " | DBs nuevas: " + ", ".join(sorted(s2 - s1)[:8]) + ("..." if len(s2 - s1) > 8 else "")
        if s1 - s2:
            txt += " | DBs que se quitan: " + ", ".join(sorted(s1 - s2)[:8]) + ("..." if len(s1 - s2) > 8 else "")
    return txt


def cmd_github(root, args, only_lists=False):
    """Trae de GitHub jd_update.list, jd_exclude.list y (si no only_lists) el propio script.
    Guarda copia .bak de lo que reemplaza. El script nuevo se valida antes de instalarlo."""
    global _RESTART
    repo, branch, path = args.gh_repo, args.gh_branch, args.gh_path
    log(f"Fuente: {repo if os.path.isdir(repo) else f'github.com/{repo}'}"
        f"{'' if os.path.isdir(repo) else f' ({branch})'} / {path}")
    ini = find_default_ini(root) or (STATE_DIR / "jd_update.list")
    targets = [("jd_update.list", ini), (EXCLUDE_FILE, STATE_DIR / EXCLUDE_FILE)]
    if not only_lists:
        targets.append(("jd_update.py", Path(__file__).resolve()))
    changed = 0
    for name, local in targets:
        try:
            remote = gh_fetch(repo, branch, path, name)
        except Exception as e:  # noqa
            log(f"  [ERROR] {name}: no pude bajarlo ({e})")
            continue
        if remote is None:
            log(f"  {name}: no esta en el repositorio, se deja el local")
            continue
        cur = local.read_bytes() if local.is_file() else None
        if cur is not None and cur.replace(b"\r\n", b"\n") == remote.replace(b"\r\n", b"\n"):
            log(f"  {name}: ya esta al dia")
            continue
        if name.endswith(".py"):
            try:
                compile(remote, name, "exec")
                if b"def main(" not in remote:
                    raise SyntaxError("no parece el script completo")
            except SyntaxError as e:
                log(f"  [ERROR] {name}: la version de GitHub no es valida ({e}); no se instala")
                continue
        log(f"  {name}: HAY CAMBIOS ({diff_summary(name, cur, remote)})")
        if args.dry_run:
            continue
        if not confirm(f"  Reemplazar {local.name}? (copia en {local.name}.bak)", args):
            log("  omitido")
            continue
        try:
            if cur is not None:
                shutil.copy2(local, local.with_name(local.name + ".bak"))
            tmp = local.with_name(local.name + ".new")
            tmp.write_bytes(remote)
            if local.is_file():
                shutil.copymode(local, tmp)
            os.replace(tmp, local)
        except OSError as e:
            log(f"  [ERROR] {name}: no pude escribirlo ({e})")
            continue
        changed += 1
        log(f"  {name}: actualizado")
        if name.endswith(".py"):
            _RESTART = True
    if args.dry_run:
        log("(dry-run: no se cambio nada)")
    elif not changed:
        log("Nada que actualizar.")
    if _RESTART:
        log("El script se actualizo: vuelve a ejecutarlo para usar la version nueva.")


# =====================================================================
#  VER EL LOG EN LA TERMINAL
# =====================================================================
def read_log_sections(path):
    """-> (encabezado, [[titulo, [lineas]]]). Las secciones empiezan con '=== '."""
    head, secs, cur = [], [], None
    for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if ln.startswith("=== "):
            cur = [ln.strip("= ").strip(), []]
            secs.append(cur)
        elif cur is None:
            head.append(ln)
        else:
            cur[1].append(ln)
    return head, secs


def page_lines(lines):
    """Muestra las lineas con paginador (less si existe; si no, de a pantalla con Enter)."""
    if not lines:
        print("(vacio)")
        return
    if not (sys.stdout.isatty() and sys.stdin and sys.stdin.isatty()):
        print("\n".join(lines))
        return
    size = shutil.get_terminal_size((100, 30))
    if len(lines) <= size.lines - 2:
        print("\n".join(lines))
        return
    less = shutil.which("less")
    if less:
        try:
            subprocess.run([less, "-R"], input=("\n".join(lines) + "\n").encode("utf-8", errors="replace"))
            return
        except OSError:
            pass
    step = max(10, size.lines - 2)
    for i in range(0, len(lines), step):
        print("\n".join(lines[i:i + step]))
        if i + step < len(lines):
            try:
                if input(f"-- {min(i + step, len(lines))}/{len(lines)} | Enter: seguir, q: salir -- ").strip().lower() == "q":
                    return
            except (EOFError, KeyboardInterrupt):
                return


def short_title(t, n=96):
    return t if len(t) <= n else t[:n - 3] + "..."


def grep_log(head, secs, text, limit=500):
    needle, out = text.lower(), []
    for ln in head:
        if needle in ln.lower():
            out.append(f"[encabezado] {ln}")
    for i, (title, body) in enumerate(secs, 1):
        for ln in body:
            if needle in ln.lower():
                out.append(f"[{i}] {ln}")
    total = len(out)
    out = out[:limit] + ([f"... {total - limit} coincidencias mas (afina la busqueda)"] if total > limit else [])
    return out or [f"(sin coincidencias para '{text}')"]


def pick_section(secs, key):
    key = (key or "").strip()
    if key.isdigit() and 1 <= int(key) <= len(secs):
        return int(key) - 1
    for i, (title, _b) in enumerate(secs):
        if key.lower() in title.lower():
            return i
    return None


def cmd_log(root, args):
    path = STATE_DIR / args.log
    if not path.is_file():
        log(f"No hay log todavia ({path}). Corre antes la accion que lo genera (revisar, discos o torrents).")
        return
    head, secs = read_log_sections(path)
    if args.grep:
        page_lines(grep_log(head, secs, args.grep))
        return
    if args.section:
        i = pick_section(secs, args.section)
        if i is None:
            log(f"No encontre la seccion '{args.section}'. Secciones: " + "; ".join(f"{n}) {short_title(t, 40)}" for n, (t, _b) in enumerate(secs, 1)))
            return
        page_lines([f"=== {secs[i][0]} ==="] + secs[i][1])
        return

    def resumen():
        print("\n".join(l for l in head if l.strip()))
        print("\nSecciones:")
        for n, (t, b) in enumerate(secs, 1):
            print(f" {n:>2}) {short_title(t)}   [{len(b)} lineas]")
    resumen()
    if not (sys.stdin and sys.stdin.isatty()):
        return
    while True:
        try:
            op = input("\nNumero de seccion | /texto para buscar | r resumen | 0 volver: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if op in ("0", "q", ""):
            if op == "":
                continue
            return
        if op.lower() == "r":
            resumen()
        elif op.startswith("/") and len(op) > 1:
            page_lines(grep_log(head, secs, op[1:].strip()))
        else:
            i = pick_section(secs, op)
            if i is None:
                print("Opcion no valida.")
            else:
                page_lines([f"=== {secs[i][0]} ==="] + secs[i][1])


# =====================================================================
#  TORRENTS: leer .torrent + datfiles (Pleasuredome) y planear que bajar (solo lo necesario)
# =====================================================================
TORRENTS_DIR = "torrents"      # junto al script: aqui van los .torrent
DATS_DIR = "dats"              # junto al script: aqui van los datfiles (.zip / .dat / .xml)
TORRENT_LOG = "JD_Torrents.log"
DISKS_LOG = "JD_Discos.log"
DISKS_JSON = "JD_Discos.json"
MAGNETS_FILE = "magnets.list"
DELUGE_CONF = "jd_deluge.ini"
DISKS_IGNORE = "jd_disks_ignore.list"


def bdecode(data):
    """Bencode -> (valor, (inicio, fin) de la clave 'info' del diccionario raiz).
    El rango de 'info' sirve para sacar el infohash exacto sin volver a codificar."""
    span = [None]

    def parse(i, depth):
        c = data[i:i + 1]
        if c == b"i":
            j = data.index(b"e", i)
            return int(data[i + 1:j]), j + 1
        if c == b"l":
            i += 1
            out = []
            while data[i:i + 1] != b"e":
                v, i = parse(i, depth + 1)
                out.append(v)
            return out, i + 1
        if c == b"d":
            i += 1
            out = {}
            while data[i:i + 1] != b"e":
                k, i = parse(i, depth + 1)
                start = i
                v, i = parse(i, depth + 1)
                if depth == 0 and k == b"info":
                    span[0] = (start, i)
                out[k] = v
            return out, i + 1
        if c.isdigit():
            j = data.index(b":", i)
            n = int(data[i:j])
            j += 1
            if j + n > len(data):
                raise ValueError("bencode truncado")
            return data[j:j + n], j + n
        raise ValueError(f"bencode invalido en la posicion {i}")

    value, _end = parse(0, 0)
    return value, span[0]


def _dec(b):
    return b.decode("utf-8", errors="replace") if isinstance(b, (bytes, bytearray)) else str(b)


def read_torrent(path):
    """-> dict(file, name, infohash, piece_length, files[{index, path, size}], count, total).
    'index' es la posicion real del archivo en el torrent (la que usan los clientes para
    marcar prioridades); 'count' es cuantos archivos tiene en total (incluye relleno)."""
    if str(path).lower().endswith(".json"):          # lista de archivos tomada de un torrent ya presente en Deluge
        j = json.loads(Path(path).read_text(encoding="utf-8"))
        files = [{"index": int(f["index"]), "path": str(f["path"]), "size": int(f["size"])} for f in j["files"]]
        return {"file": Path(path).name, "name": j["name"], "infohash": j["infohash"].lower(),
                "piece_length": int(j.get("piece_length") or 0), "files": files,
                "count": int(j.get("count") or len(files)), "total": sum(f["size"] for f in files)}
    data = Path(path).read_bytes()
    meta, span = bdecode(data)
    if not isinstance(meta, dict) or span is None or not isinstance(meta.get(b"info"), dict):
        raise ValueError("no es un .torrent valido")
    info = meta[b"info"]
    name = _dec(info.get(b"name.utf-8") or info[b"name"])
    files = []
    if b"files" in info:
        count = len(info[b"files"])
        for idx, f in enumerate(info[b"files"]):
            if b"p" in (f.get(b"attr") or b""):          # archivo de relleno (BEP 47)
                continue
            parts = f.get(b"path.utf-8") or f[b"path"]
            files.append({"index": idx, "path": "/".join([name] + [_dec(p) for p in parts]),
                          "size": int(f[b"length"])})
    else:
        count = 1
        files.append({"index": 0, "path": name, "size": int(info[b"length"])})
    return {"file": Path(path).name, "name": name, "infohash": hashlib.sha1(data[span[0]:span[1]]).hexdigest(),
            "piece_length": int(info.get(b"piece length") or 0), "files": files, "count": count,
            "total": sum(f["size"] for f in files)}


def kind_of(name):
    """'hbmame' o 'mame' segun el nombre del torrent / datfile."""
    return "hbmame" if "hbmame" in name.lower() else "mame"


def index_torrents(tdir):
    """-> (torrents, {nombre de archivo en minusculas: [(torrent, archivo)]}, [(archivo, error)])."""
    torrents, idx, errors = [], defaultdict(list), []
    if not tdir.is_dir():
        return torrents, idx, errors
    for p in sorted(list(tdir.glob("*.torrent")) + list(tdir.glob("*.torrent.json")), key=lambda x: x.name.lower()):
        try:
            t = read_torrent(p)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as e:
            errors.append((p.name, f"no se pudo leer ({e})"))
            continue
        t["kind"] = kind_of(t["name"] + " " + t["file"])
        torrents.append(t)
        for f in t["files"]:
            idx[f["path"].rsplit("/", 1)[-1].lower()].append((t, f))
    return torrents, idx, errors


def read_dat(path, wanted):
    """Lee un datfile (XML tipo Logiqx/MAME, suelto o dentro de un .zip) en streaming.
    Solo guarda los CRC de `wanted`. -> (hits {crc: [(juego, es_dueno_del_rom)]}, cloneof {juego: padre})."""
    import xml.etree.ElementTree as ET
    hits, cloneof = defaultdict(list), {}
    zf = None
    try:
        if str(path).lower().endswith(".zip"):
            zf = zipfile.ZipFile(path)
            member = next((n for n in zf.namelist() if n.lower().endswith((".dat", ".xml"))), None)
            if member is None:
                raise ValueError("el zip no trae ningun .dat/.xml")
            fh = zf.open(member)
        else:
            fh = open(path, "rb")
        with fh:
            root_el, n = None, 0
            for ev, el in ET.iterparse(fh, events=("start", "end")):
                if ev == "start":
                    if root_el is None:
                        root_el = el
                    continue
                if el.tag not in ("game", "machine"):
                    continue
                name = el.get("name") or ""
                if el.get("cloneof"):
                    cloneof[name] = el.get("cloneof")
                for r in el.iter("rom"):
                    crc = r.get("crc")
                    if not crc:
                        continue
                    try:
                        c = int(crc, 16)
                    except ValueError:
                        continue
                    if c in wanted:
                        hits[c].append((name, not r.get("merge")))
                el.clear()
                n += 1
                if n % 2000 == 0 and root_el is not None:
                    root_el.clear()
    finally:
        if zf is not None:
            zf.close()
    return hits, cloneof


def dat_zips(crc, entries):
    """Zips (en un set MERGED) que contienen ese CRC segun los DAT: el del padre raiz del juego que
    es 'dueno' del rom. -> lista de nombres de zip, el mas probable primero."""
    out = []
    for hits, cloneof in entries:
        for name, owner in sorted(hits.get(crc, ()), key=lambda x: not x[1]):
            seen = set()
            while cloneof.get(name) and name not in seen:
                seen.add(name)
                name = cloneof[name]
            z = name + ".zip"
            if z not in out:
                out.append(z)
    return out


def fmt_size(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:,.0f} {u}" if u in ("B", "KB") else f"{n:,.1f} {u}"
        n /= 1024


def missing_parts(R):
    """Partes que no estan en NINGUN zip local de las MRA que no cargan (las SOLUCIONABLES se arreglan con
    rebuild). -> [(entrada MRA, rom, parte, zips declarados)]."""
    out = []
    for e in R.entries:
        if R.status.get(id(e)) != "FALTA":
            continue
        _fails, nowhere, _el = R.fail_info[id(e)]
        ids = {id(p) for p in nowhere}
        for rom, lack in R.fail_info[id(e)][0]:
            for part in lack:
                if id(part) in ids:
                    out.append((e, rom, part, list(part["zips"] or rom["zips"])))
    return out


def plan_torrents(root, R, args, tidx, dat_entries, local_idx):
    """Decide que zips bajar de los torrents. Por cada parte que falta:
      1) con DAT: el zip del set MERGED que contiene ese CRC (aunque la MRA declare el zip de un clon);
      2) si no, los zips que la MRA declara y que existen en algun torrent.
    El destino sigue lo que declara la MRA (nombre simple -> games/mame; '/hbmame/x.zip' -> games/hbmame)."""
    primary = (args.rom_dir or DEFAULT_ROM_DIRS)[0]
    targets, local, unresolved = {}, {}, []
    notes = Counter()

    def dest_dir(declared):
        for z in declared:
            z = z.replace("\\", "/").strip()
            if "/" in z.strip("/"):
                return "games/" + "/".join(p for p in z.split("/")[:-1] if p)
        return primary

    for e, rom, part, declared in missing_parts(R):
        rp = rel(root, e["path"])
        kinds = ["hbmame", "mame"] if any("hbmame" in z.lower() for z in declared) else ["mame", "hbmame"]
        cands = []   # (nombre de zip, viene del DAT, tipo preferido)
        if part["crc"] is not None and dat_entries:
            found = False
            for k in kinds:
                for z in dat_zips(part["crc"], dat_entries.get(k, [])):
                    cands.append((z, True, k))
                    found = True
            if not found:
                notes["crc_fuera_del_dat"] += 1
        for z in declared:
            cands.append((z.replace("\\", "/").split("/")[-1], False, None))

        chosen = None
        for zname, from_dat, k in cands:
            options = tidx.get(zname.lower())
            if not options:
                continue
            order = [k] if k else kinds
            options = sorted(options, key=lambda o: order.index(o[0]["kind"]) if o[0]["kind"] in order else 9)
            chosen = (options[0][0], options[0][1], from_dat)
            break
        if chosen is None:
            unresolved.append((rp, part["name"], part["crc"], declared))
            continue
        t, f, from_dat = chosen
        fname = f["path"].rsplit("/", 1)[-1]
        ddir = dest_dir(declared)
        dest = f"{ddir}/{fname}"
        if (root / dest).exists():
            notes["ya_existe_distinto"] += 1       # existe pero no trae lo que pide la MRA (otra version)
            continue
        if local_idx is not None and fname.lower() in local_idx:
            local.setdefault(fname, set()).add(rp)
            continue
        tg = targets.setdefault(dest.lower(), {"name": fname, "dest": dest, "torrent": t, "file": f,
                                               "mras": set(), "parts": 0, "dat": False})
        tg["mras"].add(rp)
        tg["parts"] += 1
        tg["dat"] = tg["dat"] or from_dat
    return {"targets": targets, "local": local, "unresolved": unresolved, "notes": notes}


def render_plan(torrents, dats, plan, terrors, dat_errors):
    L = ["TORRENTS - plan de descarga selectiva (no se descargo nada)",
         f"Fecha: {datetime.now():%Y-%m-%d %H:%M:%S}", "", "=== TORRENTS LEIDOS ==="]
    for t in torrents:
        L.append(f"{t['file']}\t[{t['kind']}] {t['name']} | {len(t['files'])} archivos | {fmt_size(t['total'])} | "
                 f"pieza {fmt_size(t['piece_length'])} | infohash {t['infohash']}")
    L += [f"[!] {n}: {why}" for n, why in terrors]
    L += ["", "=== DATFILES ===" ]
    L += [f"{p.name}\t[{kind_of(p.name)}]" for p in dats] or ["(ninguno: sin DAT se usan solo los nombres de zip que declaran las MRA)"]
    L += [f"[!] {n}: {why}" for n, why in dat_errors]
    tg = plan["targets"]
    by_t = defaultdict(list)
    for v in tg.values():
        by_t[v["torrent"]["file"]].append(v)
    total = sum(v["file"]["size"] for v in tg.values())
    L += ["", f"=== A BAJAR POR TORRENT ({len(tg)} zips, {fmt_size(total)}) ==="]
    for tf, items in sorted(by_t.items()):
        t = items[0]["torrent"]
        s = sum(v["file"]["size"] for v in items)
        extra = len(items) * 2 * t["piece_length"]
        L.append(f"\n{tf}: {len(items)} zips, {fmt_size(s)}  (extra posible por piezas compartidas: hasta ~{fmt_size(extra)})")
        for v in sorted(items, key=lambda x: x["name"].lower()):
            tag = "DAT" if v["dat"] else "nombre"
            L.append(f"  {fmt_size(v['file']['size']):>10}  {v['dest']}   ({len(v['mras'])} MRA, verificado por {tag}, "
                     f"indice {v['file']['index']})")
    if not tg:
        L.append("(nada por bajar de los torrents)")
    L += ["", f"=== YA ESTAN EN TU FUENTE LOCAL ({len(plan['local'])} zips): usa 'copy' en vez de torrent ==="]
    L += [f"{z}\t({len(m)} MRA)" for z, m in sorted(plan["local"].items(), key=lambda kv: kv[0].lower())] or ["(ninguno)"]
    un = plan["unresolved"]
    L += ["", f"=== SIN FUENTE: no estan en ningun torrent ni en la fuente local ({len(un)} partes) ==="]
    for rp, pname, crc, declared in un[:200]:
        L.append(f"{rp}\t{pname} ({format(crc, '08x') if crc is not None else 'sin crc'})\tzips: {', '.join(declared) or '-'}")
    if len(un) > 200:
        L.append(f"... y {len(un) - 200} mas")
    if not un:
        L.append("(ninguno)")
    n = plan["notes"]
    L += ["", "=== AVISOS ===",
          f"Partes cuyo CRC no esta en ningun DAT (otra version de MAME?): {n.get('crc_fuera_del_dat', 0)}",
          f"Zips que ya existen en games/ pero no traen lo que pide la MRA (no se tocan): {n.get('ya_existe_distinto', 0)}",
          "CHD: no salen de las MRA (los pide el core al cargar, ya convertidos a .hdd). Se buscan por nombre "
          "con: torrents --find <juego>. La conversion a .hdd no esta en esta version."]
    return L


def find_in_torrents(torrents, text, limit=150):
    """Busca `text` (sin distinguir mayusculas) en las rutas de TODOS los torrents (zips, CHD, lo que sea).
    -> lineas para mostrar."""
    needle = text.lower()
    rows = [(t, f) for t in torrents for f in t["files"] if needle in f["path"].lower()]
    if not rows:
        return [f"(sin coincidencias para '{text}')"]
    tot = sum(f["size"] for _t, f in rows)
    out = [f"{len(rows)} archivo(s) con '{text}' | {fmt_size(tot)} en total"]
    for t, f in rows[:limit]:
        out.append(f"  {fmt_size(f['size']):>10}  [{t['file']}] {f['path']}   (indice {f['index']})")
    if len(rows) > limit:
        out.append(f"  ... y {len(rows) - limit} mas (afina la busqueda)")
    return out


def zip_plan(root, args, R, torrents, tidx):
    """Cruza lo que falta (R) con datfiles y torrents. -> (plan, dats, dat_errors)."""
    ddir = STATE_DIR / DATS_DIR
    dats =[p for p in sorted(ddir.glob("*")) if p.suffix.lower() in (".zip", ".dat", ".xml")
            and "chd" not in p.name.lower()] if ddir.is_dir() else []

    parts = missing_parts(R)
    wanted = {p["crc"] for _e, _r, p, _d in parts if p["crc"] is not None}
    dat_entries, dat_errors = {}, []
    if dats and wanted:
        for p in dats:
            log(f"Leyendo datfile {p.name} (puede tardar un poco)...")
            try:
                hits, clone = read_dat(p, wanted)
            except Exception as e:  # noqa
                dat_errors.append((p.name, f"no se pudo leer ({e})"))
                log(f"[!] {p.name}: {e}")
                continue
            dat_entries.setdefault(kind_of(p.name), []).append((hits, clone))
    local_idx = load_roms_db(Path(args.source).expanduser(), STATE_DIR / ROMS_DB, False, create=False)
    plan = plan_torrents(root, R, args, tidx, dat_entries, local_idx)
    return plan, dats, dat_errors, parts


def cmd_torrents(root, args):
    """Lee los .torrent (torrents/) y los datfiles (dats/), cruza con lo que faltan las MRA y muestra
    exactamente que zips y cuantos MB habria que bajar. NO descarga nada."""
    tdir, ddir = STATE_DIR / TORRENTS_DIR, STATE_DIR / DATS_DIR
    if not args.dry_run:
        tdir.mkdir(exist_ok=True)
        ddir.mkdir(exist_ok=True)
    torrents, tidx, terrors = index_torrents(tdir)
    for n, why in terrors:
        log(f"[!] {n}: {why}")
    if not torrents:
        log(f"No hay .torrent en {tdir}. Copia ahi los de Pleasuredome (MAME / HBMAME ROMs merged) y vuelve a correr.")
        return
    log(f"Torrents: {len(torrents)} | archivos indexados: {sum(len(t['files']) for t in torrents)}")
    if args.find:
        print("\n".join(find_in_torrents(torrents, args.find)))
        return
    excl = prepare_excludes(root, args)
    R = analyze(root, args, excl)
    plan, dats, dat_errors, parts = zip_plan(root, args, R, torrents, tidx)
    L = render_plan(torrents, dats, plan, terrors, dat_errors)
    try:
        (STATE_DIR / TORRENT_LOG).write_text("\n".join(L) + "\n", encoding="utf-8")
    except OSError:
        pass
    tg = plan["targets"]
    total = sum(v["file"]["size"] for v in tg.values())
    log("")
    log(f"Partes que faltan en las MRA que no cargan: {len(parts)}")
    log(f"  -> a bajar de torrents: {len(tg)} zips, {fmt_size(total)}")
    log(f"  -> ya en tu fuente local (usa copy): {len(plan['local'])} zips")
    log(f"  -> sin fuente: {len(plan['unresolved'])} partes")
    for k, v in plan["notes"].items():
        log(f"  aviso: {k.replace('_', ' ')}: {v}")
    log(f"Detalle: {STATE_DIR / TORRENT_LOG}")
    log("(Esta version solo planea: todavia no descarga.)")


# ====================================================================
#  DISCOS (HDD / IMG / CHD): que MRA los piden, cuales faltan y donde van
# ====================================================================
DISK_EXTS = (".hdd", ".img", ".chd")
CHD_LOCAL_DIRS = ["games/mame", "games/hbmame"]


def mra_disks(e):
    """Discos que declara una MRA con <file>: [(ruta relativa a la raiz, partes de la ruta)]."""
    out = []
    for f in e.get("files", []):
        path = f["path"].replace("\\", "/").strip("/")
        if not path.lower().endswith(DISK_EXTS):
            continue
        base = (f["root"] or "games").replace("\\", "/").strip("/")
        parts = [x for x in (base + "/" + path).split("/") if x and x != "."]
        if ".." in parts:
            continue
        out.append(parts)
    return out


def collect_disks(root, entries):
    """Discos con <file> (destino fijo), agrupados por archivo de destino."""
    disks = {}
    for e in entries:
        for parts in mra_disks(e):
            key = "/".join(parts).lower()
            d = disks.get(key)
            if d is None:
                stem, ext = os.path.splitext(parts[-1])
                d = disks[key] = {"parts": parts, "rel": "/".join(parts), "stem": stem, "ext": ext.lower(),
                                  "mras": [], "rbfs": set(), "cands": []}
            d["mras"].append(rel(root, e["path"]))
            if e["rbf"].strip():
                d["rbfs"].add(rbf_basename(e["rbf"]))
            # el CHD puede llamarse como el disco o como el set padre (zip) de la MRA
            for c in [d["stem"]] + set_names(e):
                if c and c.lower() not in [x.lower() for x in d["cands"]]:
                    d["cands"].append(c)
    return disks


def set_names(e):
    """Nombres de set de una MRA: setname y zips (padre/clon), sin .zip y sin carpeta."""
    out = []
    for n in ([e.get("setname", "")] + [os.path.splitext(z.replace("\\", "/").split("/")[-1])[0] for z in e["zips"]]):
        if n and n.lower() not in [x.lower() for x in out]:
            out.append(n)
    return out


def local_disk_search(root, name, sets, rbfs):
    """Busca un archivo de disco ya presente: games/<carpeta>/<name> o games/<carpeta>/<set>/<name>."""
    gdir = root / "games"
    try:
        subs = os.listdir(gdir)
    except OSError:
        return None
    for sub in subs:
        base = gdir / sub
        if not base.is_dir():
            continue
        for mid in [[]] + [[c] for c in sets]:
            p = find_ci(gdir, [sub] + mid + [name])
            if p is not None and p.is_file() and p.stat().st_size > 0:
                return p
    return None


def find_local_chd(root, d):
    """CHD ya presente (junto al destino o en games/mame|hbmame, suelto o en carpeta del juego)."""
    folders = [d["parts"][:-1]] + [x.split("/") for x in CHD_LOCAL_DIRS]
    for c in d["cands"]:
        for fo in folders:
            for sub in ([], [c]):
                p = find_ci(root, fo + sub + [c + ".chd"])
                if p is not None and p.is_file():
                    return p
    return None


def pick_torrent_row(rows, prefer):
    """De varias coincidencias elige la que esta en la carpeta de un set preferido."""
    pl = [x.lower() for x in prefer]
    return sorted(rows, key=lambda tf: (tf[1]["path"].split("/")[-2:-1] or [""])[0].lower() not in pl)[0]


IGNORE_TEMPLATE = """# jd_disks_ignore.list - discos (CHD/HDD/IMG) que la opcion "Discos" debe IGNORAR.
# Un criterio por linea; sin distinguir mayusculas; admite comodines * y ?. # = comentario.
#   core:NOMBRE   ignora todo lo de ese core (el nombre del .rbf, ej. core:TaitoGNET)
#   NOMBRE        ignora por nombre de set o de disco (ej. wargods, a12jaa01, 825*)
# Este archivo es SOLO de tu NAS: la actualizacion desde GitHub no lo toca.
core:TaitoGNET
"""


def load_disk_ignores(args):
    """-> [(tipo, patron)] con tipo 'core' o 'name'. Crea el archivo con ejemplos la primera vez."""
    f = STATE_DIR / DISKS_IGNORE
    if not f.is_file():
        if not args.dry_run:
            try:
                f.write_text(IGNORE_TEMPLATE, encoding="utf-8")
            except OSError:
                pass
        text = IGNORE_TEMPLATE
    else:
        text = f.read_text(encoding="utf-8", errors="replace")
    out = []
    for ln in text.splitlines():
        ln = ln.split("#", 1)[0].strip().lower()
        if not ln:
            continue
        if ln.startswith("core:"):
            out.append(("core", ln[5:].strip()))
        else:
            out.append(("name", ln))
    return out


def disk_ignored(it, pats):
    names = {it["name"].lower(), os.path.splitext(it["name"])[0].lower()}
    for n in (it.get("chd_name") or "").split(","):
        n = n.strip().lower()
        if n:
            names |= {n, os.path.splitext(n)[0]}
    if it.get("dest"):
        names.add(it["dest"].lower())
    for kind, pat in pats:
        if kind == "core" and any(fnmatch.fnmatch(r.lower(), pat) for r in it["rbfs"]):
            return True
        if kind == "name" and any(fnmatch.fnmatch(n, pat) for n in names):
            return True
    return False


def cmd_disks(root, args):
    """Que discos (CHD / .hdd / .img) necesitan las MRA y de donde saldrian. Solo lee: no descarga ni convierte.
    Tres origenes, de mas a menos seguro:
      FIJO     la MRA trae <file ... .hdd/.img/.chd> (destino exacto en games/)
      OSD      la MRA solo nombra el disco en un comentario (el core lo pide por las ranuras del OSD)
      POSIBLE  el set de MAME tiene CHD en el torrent pero la MRA no lo menciona (revisar a mano)
    Escribe JD_Discos.log y JD_Discos.json."""
    _ls_cache.clear()
    try:
        (STATE_DIR / DISKS_JSON).unlink()
    except OSError:
        pass
    problems = []
    entries = scan_mras(root, list(args.scan or CHECK_SCAN), problems)
    if not entries:
        sys.exit(f"No encontre ninguna .mra en: {', '.join(args.scan or CHECK_SCAN)}")
    tdir = STATE_DIR / TORRENTS_DIR
    torrents, tidx, terrors = index_torrents(tdir)
    disks = collect_disks(root, entries)
    fixed_stems = {d["stem"].lower() for d in disks.values()}
    items = []

    def tor_row(name, prefer):
        rows = tidx.get(name.lower(), [])
        return pick_torrent_row(rows, prefer) if rows else None

    # ---- FIJO ----
    for key in sorted(disks):
        d = disks[key]
        dest = find_ci(root, d["parts"])
        it = {"kind": "FIJO", "dest": d["rel"], "name": d["parts"][-1], "ext": d["ext"], "rbfs": sorted(d["rbfs"]),
              "mras": d["mras"], "chd_name": None, "chd_local": None, "torrent": None, "size": 0,
              "group": "/".join(d["parts"][:2]) if len(d["parts"]) > 2 else "/".join(d["parts"][:-1])}
        if dest is not None and dest.is_file() and dest.stat().st_size > 0:
            it["size"] = dest.stat().st_size
            it["status"] = "OK"
        else:
            it["convert"] = d["ext"] != ".chd"
            local = find_local_chd(root, d)
            if local is not None:
                it.update(chd_local=rel(root, local), chd_name=local.name,
                          status="CONVERTIR" if it["convert"] else "COPIAR")
            else:
                hit = None
                for c in d["cands"]:
                    r = tor_row(c + ".chd", d["cands"])
                    if r:
                        hit = (c, r)
                        break
                if hit:
                    c, (t, f) = hit
                    it.update(chd_name=c + ".chd", status="BAJAR", size=f["size"],
                              torrent={"file": t["file"], "path": f["path"], "size": f["size"], "index": f["index"]})
                else:
                    it["chd_name"] = d["cands"][0] + ".chd" if d["cands"] else None
                    it["status"] = "SIN FUENTE" if torrents else "SIN TORRENT CARGADO"
        items.append(it)

    # ---- OSD: nombres citados en comentarios. Se resuelve CADA nombre con el contexto de su MRA (los CHD pueden
    #      llevar espacios: "gq974-ja c01.chd") y se agrupa por el CHD resuelto (x.chd + x.hdd = un disco) ----
    chd_folders = defaultdict(list)                   # carpeta del juego (minusculas) -> [(torrent, archivo)]
    for t in torrents:
        for f in t["files"]:
            if f["path"].lower().endswith(".chd") and f["path"].count("/") >= 2:
                chd_folders[f["path"].split("/")[-2].lower()].append((t, f))
    osd = {}
    for e in entries:
        cf = e.get("cforms", {})
        sets = set_names(e)
        for w in e.get("cdisks", []):
            plain_stem = os.path.splitext(w)[0]
            if plain_stem.lower() in fixed_stems:
                continue
            hit, used = None, plain_stem
            for fm in cf.get(w.lower(), []) + [w]:        # primero la forma mas larga
                fs = os.path.splitext(fm)[0]
                r = tor_row(fs + ".chd", sets)
                if r:
                    hit, used = r, fs
                    break
            key = ("t", hit[1]["path"]) if hit else ("n", plain_stem.lower())
            o = osd.setdefault(key, {"stem": used, "names": [], "mras": [], "rbfs": set(), "sets": [], "hit": hit})
            if w.lower() not in [x.lower() for x in o["names"]]:
                o["names"].append(w)
            o["mras"].append(rel(root, e["path"]))
            if e["rbf"].strip():
                o["rbfs"].add(rbf_basename(e["rbf"]))
            for c in sets:
                if c.lower() not in [x.lower() for x in o["sets"]]:
                    o["sets"].append(c)
    for k in sorted(osd, key=lambda k: (k[1].lower(), k[0])):
        o = osd[k]
        chd = o["stem"] + ".chd"
        raw = [n for n in o["names"] if not n.lower().endswith(".chd")]     # la MRA pide el .hdd/.img convertido
        want = raw[0] if raw else chd
        it = {"kind": "OSD", "dest": None, "name": want, "ext": os.path.splitext(want)[1].lower(),
              "pide": " + ".join(sorted({os.path.splitext(n)[1].lower() for n in o["names"]})),
              "rbfs": sorted(o["rbfs"]), "mras": list(dict.fromkeys(o["mras"])), "chd_name": chd, "chd_local": None,
              "torrent": None, "size": 0, "convert": bool(raw), "group": ", ".join(sorted(o["rbfs"])) or "?"}
        have = local_disk_search(root, want, o["sets"], o["rbfs"])
        have_chd = local_disk_search(root, chd, o["sets"], o["rbfs"]) if raw else have
        if have is not None:
            it.update(chd_local=rel(root, have), status="LOCAL", size=have.stat().st_size)
        elif have_chd is not None:
            it.update(chd_local=rel(root, have_chd), status="CONVERTIR", size=have_chd.stat().st_size)
        elif o["hit"]:
            t, f = o["hit"]
            it.update(status="BAJAR", size=f["size"], chd_name=f["path"].rsplit("/", 1)[-1],
                      torrent={"file": t["file"], "path": f["path"], "size": f["size"], "index": f["index"]})
        else:
            it["status"] = "SIN FUENTE" if torrents else "SIN TORRENT CARGADO"
            near = []                                  # CHD de la carpeta del set cuyo nombre termina o empieza igual
            for c in o["sets"]:
                near += list(chd_folders.get(c.lower(), []))
            sl = o["stem"].lower()
            pref = [x for x in near if x[1]["path"].rsplit("/", 1)[-1].lower().startswith(sl)
                    or x[1]["path"].rsplit("/", 1)[-1].lower().endswith(" " + sl + ".chd")]
            cand = pref or near
            if cand:
                it["candidatos"] = [{"file": t["file"], "path": f["path"], "size": f["size"], "index": f["index"]}
                                    for t, f in cand[:12]]
                if len(pref) == 1:                     # un solo CHD coincide: casi seguro es ese
                    t, f = pref[0]
                    it.update(status="APROX", size=f["size"], chd_name=f["path"].rsplit("/", 1)[-1],
                              torrent={"file": t["file"], "path": f["path"], "size": f["size"], "index": f["index"]})
        items.append(it)

    # ---- POSIBLE: CHD del set de MAME que la MRA no menciona ----
    covered = {it["torrent"]["path"] for it in items if it["torrent"]}
    folders = chd_folders
    named = {it["name"].lower() for it in items} | {(it["chd_name"] or "").lower() for it in items}
    poss = {}
    for e in entries:
        if mra_disks(e) or any(os.path.splitext(w)[0].lower() in fixed_stems or w.lower() in named for w in e.get("cdisks", [])):
            continue
        for c in set_names(e):
            for t, f in folders.get(c.lower(), []):
                if f["path"] in covered:
                    continue
                g = poss.setdefault(c.lower(), {"set": c, "files": {}, "mras": [], "rbfs": set()})
                g["files"][f["path"]] = (t, f)
                if rel(root, e["path"]) not in g["mras"]:
                    g["mras"].append(rel(root, e["path"]))
                if e["rbf"].strip():
                    g["rbfs"].add(rbf_basename(e["rbf"]))
    for k in sorted(poss):
        g = poss[k]
        fl = list(g["files"].values())
        items.append({"kind": "POSIBLE", "dest": None, "name": g["set"], "ext": ".chd", "rbfs": sorted(g["rbfs"]),
                      "mras": g["mras"], "chd_name": ", ".join(f["path"].rsplit("/", 1)[-1] for _t, f in fl),
                      "chd_local": None, "status": "POSIBLE", "size": sum(f["size"] for _t, f in fl),
                      "torrent": {"file": fl[0][0]["file"], "path": fl[0][1]["path"], "size": sum(f["size"] for _t, f in fl),
                                  "index": fl[0][1]["index"], "paths": [f["path"] for _t, f in fl]},
                      "group": ", ".join(sorted(g["rbfs"])) or "?"})

    ign = load_disk_ignores(args)
    ignored = [i for i in items if disk_ignored(i, ign)]
    if ignored:
        items = [i for i in items if not disk_ignored(i, ign)]
    for i in items:                                    # destino sugerido de los discos por OSD: games/<set de MAME>/<archivo>
        if i["kind"] == "OSD" and i["torrent"]:
            i["destino"] = "games/" + i["torrent"]["path"].split("/")[-2] + "/" + i["name"]
    nm = len(entries)
    if not items:
        log(f"MRAs unicas: {nm} | ninguna pide discos (ni <file>, ni comentario con .chd/.hdd/.img, ni set con CHD en los torrents).")
        if not torrents:
            log(f"Aviso: no hay .torrent en {tdir}; sin el de CHDs no se pueden detectar los sets con CHD.")
        return

    kinds = ("FIJO", "OSD", "POSIBLE")
    desc = {"FIJO": "la MRA trae <file> con el destino exacto en games/",
            "OSD": "la MRA solo nombra el disco en un comentario (ranura del OSD)",
            "POSIBLE": "el set de MAME tiene CHD pero la MRA no lo menciona (revisar)"}
    order = {"OK": 6, "LOCAL": 6, "COPIAR": 1, "CONVERTIR": 1, "BAJAR": 2, "APROX": 3, "POSIBLE": 4, "SIN FUENTE": 0, "SIN TORRENT CARGADO": 0}
    L = [f"===== DISCOS (HDD / IMG / CHD) =====   {datetime.now():%Y-%m-%d %H:%M:%S}",
         f"Raiz: {root}", f"MRAs unicas: {nm}", f"Torrents cargados: {len(torrents)} ({tdir})",
         (f"Ignorados por {DISKS_IGNORE}: {len(ignored)} ({', '.join(sorted({k + ':' + p for k, p in ign}))})" if ignored else ""), ""]
    for kd in kinds:
        sel = [i for i in items if i["kind"] == kd]
        if not sel:
            continue
        cnt = Counter(i["status"] for i in sel)
        tb = {(i["torrent"]["file"], i["torrent"]["path"]): i["torrent"]["size"] for i in sel
              if i["torrent"] and i["status"] in ("BAJAR", "POSIBLE")}
        nmra = len({m for i in sel for m in i["mras"]})
        L.append(f"{kd}: {len(sel)} ({nmra} MRA)  -  {desc[kd]}")
        L.append("    " + " | ".join(f"{k}: {v}" for k, v in sorted(cnt.items(), key=lambda kv: order.get(kv[0], 9)))
                 + (f"   -> a bajar de torrents: {fmt_size(sum(tb.values()))}" if tb else ""))
    n_head = len(L)
    L += ["", "--- Por carpeta de core / destino ---"]
    byg = defaultdict(list)
    for i in items:
        byg[(i["kind"], i["group"])].append(i)
    for (kd, g) in sorted(byg):
        c = Counter(i["status"] for i in byg[(kd, g)])
        L.append(f"  {kd:<8} {g:<30} " + ", ".join(f"{k} {v}" for k, v in sorted(c.items(), key=lambda kv: order.get(kv[0], 9))))
    for kd in kinds:
        for st in ("SIN FUENTE", "SIN TORRENT CARGADO", "APROX", "BAJAR", "POSIBLE", "CONVERTIR", "COPIAR", "LOCAL", "OK"):
            sel = [i for i in items if i["kind"] == kd and i["status"] == st]
            if not sel:
                continue
            L += ["", f"=== {kd}: {st} ({len(sel)}) ==="]
            for i in sel:
                if kd == "POSIBLE":
                    L.append(f"set {i['name']}   [core: {', '.join(i['rbfs']) or '?'}]   {fmt_size(i['size'])}")
                    L.append(f"    MRA: {i['mras'][0]}" + (f"  (+{len(i['mras']) - 1} mas)" if len(i["mras"]) > 1 else ""))
                    for pth in i["torrent"]["paths"]:
                        L.append(f"    CHD: {pth}")
                    continue
                L.append((i["dest"] or i["name"]) + f"   [core: {', '.join(i['rbfs']) or '?'}]")
                L.append(f"    MRA: {i['mras'][0]}" + (f"  (+{len(i['mras']) - 1} mas)" if len(i["mras"]) > 1 else ""))
                if i.get("pide"):
                    L.append(f"    la MRA pide: {i['pide']}" + ("  (el .hdd/.img sale de convertir el CHD)" if i.get("convert") else ""))
                if st in ("OK", "LOCAL"):
                    L.append(f"    {i['chd_local'] or i['dest']}  {fmt_size(i['size'])}")
                    continue
                if i["chd_name"]:
                    L.append(f"    CHD de origen: {i['chd_name']}")
                if i["chd_local"]:
                    L.append(f"    CHD ya local: {i['chd_local']}  -> falta " + ("convertir a " + i["ext"] if i.get("convert") else "copiarlo"))
                if i["torrent"]:
                    t = i["torrent"]
                    L.append(f"    torrent: [{t['file']}] {t['path']}  {fmt_size(t['size'])}  (indice {t['index']})")
                if i.get("destino"):
                    L.append(f"    destino sugerido: {i['destino']}" + ("  (convertir el CHD a " + i["ext"] + ")" if i.get("convert") else ""))
                if st == "APROX":
                    L.append("    (nombre aproximado: es el unico CHD del set que empieza igual; confirmar)")
                for cd in i.get("candidatos", []) if st == "SIN FUENTE" else []:
                    L.append(f"    candidato en el set: {cd['path']}  {fmt_size(cd['size'])}")
                if st.startswith("SIN"):
                    L.append("    (busca el nombre con:  python3 jd_update.py torrents --find " + (i["chd_name"] or i["name"]).rsplit(".", 1)[0] + ")")
    try:
        (STATE_DIR / DISKS_LOG).write_text("\n".join(L) + "\n", encoding="utf-8")
        (STATE_DIR / DISKS_JSON).write_text(json.dumps(items, indent=1, ensure_ascii=False, default=list), encoding="utf-8")
    except OSError as ex:
        log(f"[!] no se pudo escribir el reporte: {ex}")
    for n, why in terrors:
        log(f"[!] {n}: {why}")
    log("")
    log("\n".join(L[4:n_head]))
    log(f"\nDetalle: {STATE_DIR / DISKS_LOG}   (datos para el siguiente paso: {DISKS_JSON})")
    if not torrents:
        log(f"Aviso: no hay .torrent en {tdir}; pon ahi el de MAME CHDs para saber cuales bajar.")
    log("(Esto solo revisa. Para bajar y convertir: la opcion 1 del menu, o 5 > 5.)")


# ====================================================================
#  MAGNETS -> .torrent (pide solo los METADATOS a tu Deluge; no descarga datos)
# ====================================================================
DELUGE_TEMPLATE = """[deluge]
# URL del Web UI de Deluge (con o sin /json al final)
url = {url}
# contrasena del Web UI
password = {password}
# solo si tu Web UI esta detras de usuario/contrasena HTTP (basic auth); si no, dejalos vacios
http_user =
http_pass =
# pon no si el certificado HTTPS es autofirmado
verify_ssl = yes
# segundos que Deluge espera los metadatos de cada magnet
timeout = 120
# etiqueta (plugin Label de Deluge) para los torrents de MiSTer que ya esten en tu Deluge; vacio = no etiquetar
label = mister
"""


def parse_magnet(uri):
    """-> dict(uri, infohash, name, size) o None."""
    import base64 as b64
    from urllib.parse import urlparse, parse_qs
    uri = uri.strip()
    if not uri.lower().startswith("magnet:"):
        return None
    q = parse_qs(urlparse(uri).query)
    for xt in q.get("xt", []):
        if xt.lower().startswith("urn:btih:"):
            h = xt[9:].strip()
            if re.fullmatch(r"[0-9a-fA-F]{40}", h):
                h = h.lower()
            elif re.fullmatch(r"[A-Za-z2-7]{32}", h):
                h = b64.b32decode(h.upper()).hex()
            else:
                return None
            try:
                size = int(q.get("xl", ["0"])[0])
            except ValueError:
                size = 0
            return {"uri": uri, "infohash": h, "name": q.get("dn", [""])[0], "size": size}
    return None


def read_magnets(path):
    """-> (magnets[], errores[]). Una por linea; admite lineas vacias y # comentarios."""
    mags, errs, seen = [], [], set()
    for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = parse_magnet(line)
        if m is None:
            errs.append(f"linea {n}: no es un magnet valido")
        elif m["infohash"] not in seen:
            seen.add(m["infohash"])
            mags.append(m)
    return mags, errs


class DelugeWeb:
    """Cliente minimo del JSON-RPC del Web UI de Deluge 2.x."""

    def __init__(self, url, password, http_user="", http_pass="", verify=True, timeout=120):
        import http.cookiejar
        import ssl
        import base64 as b64
        url = url.strip().rstrip("/")
        self.url = url if url.lower().endswith("/json") else url + "/json"
        self.password, self.timeout, self._id = password, timeout, 0
        handlers = [urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())]
        if not verify:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self.opener = urllib.request.build_opener(*handlers)
        self.headers = {"Content-Type": "application/json", "User-Agent": UA}
        if http_user:
            tok = b64.b64encode(f"{http_user}:{http_pass}".encode()).decode()
            self.headers["Authorization"] = "Basic " + tok

    def call(self, method, params=None, timeout=None):
        self._id += 1
        body = json.dumps({"method": method, "params": params or [], "id": self._id}).encode()
        req = urllib.request.Request(self.url, data=body, headers=self.headers, method="POST")
        try:
            with self.opener.open(req, timeout=timeout or self.timeout) as r:
                data = json.loads(r.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            hint = " (usuario/contrasena HTTP: revisa http_user/http_pass)" if e.code == 401 else ""
            raise RuntimeError(f"HTTP {e.code} en {self.url}{hint}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise RuntimeError(f"no se pudo hablar con {self.url}: {getattr(e, 'reason', e)}")
        if data.get("error"):
            raise RuntimeError(f"{method}: {data['error'].get('message', data['error'])}")
        return data.get("result")

    def login(self):
        if not self.call("auth.login", [self.password]):
            raise RuntimeError("contrasena del Web UI incorrecta")
        if not self.call("web.connected"):
            hosts = self.call("web.get_hosts") or []
            if not hosts:
                raise RuntimeError("el Web UI no tiene ningun daemon configurado (Connection Manager)")
            self.call("web.connect", [hosts[0][0]])

    label = ""

    def label_torrents(self, ids, label):
        """Pone la etiqueta (plugin Label de Deluge) a los torrents que YA estan en la sesion. -> cuantos."""
        if not label or not ids:
            return 0
        if "Label" not in (self.call("core.get_enabled_plugins") or []):
            raise RuntimeError("el plugin Label de Deluge no esta activado (Preferencias > Plugins > Label)")
        lid = re.sub(r"[^a-z0-9_.\-]", "-", label.lower())
        present = set(self.call("core.get_session_state") or [])
        mine = [i for i in ids if i in present]
        if not mine:
            return 0
        if lid not in (self.call("label.get_labels") or []):
            self.call("label.add", [lid])
        for i in mine:
            self.call("label.set_torrent", [i, lid])
        return len(mine)

    def magnet_metadata(self, uri, wait):
        """Metadatos bencodeados (el diccionario 'info') del magnet, sin agregarlo a Deluge."""
        import base64 as b64
        res = self.call("core.prefetch_magnet_metadata", [uri, int(wait)], timeout=wait + 60)
        if not res or len(res) < 2 or not res[1]:
            raise RuntimeError("Deluge no devolvio metadatos (sin peers/seeds a tiempo; sube 'timeout' y reintenta)")
        meta = res[1]
        return b64.b64decode(meta if isinstance(meta, (str, bytes)) else str(meta))


def session_file_list(cli, m):
    """Si el torrent YA esta en la sesion de Deluge, toma su lista de archivos de ahi (sin tocarlo).
    -> dict listo para guardar como .torrent.json, o lanza RuntimeError."""
    st = cli.call("core.get_torrent_status", [m["infohash"], ["name", "files", "piece_length", "total_size"]])
    files = (st or {}).get("files") or []
    if not files:
        raise RuntimeError("esta en Deluge pero todavia sin metadatos (espera a que muestre su tamano y reintenta)")
    out = []
    for f in files:
        path = str(f["path"])
        if "/.pad/" in path or "_____padding_file" in path:
            continue
        out.append({"index": int(f["index"]), "path": path, "size": int(f["size"])})
    return {"name": st.get("name") or m["name"] or m["infohash"], "infohash": m["infohash"],
            "piece_length": int(st.get("piece_length") or 0), "count": len(files), "files": out}


def load_deluge(args):
    """Lee jd_deluge.ini (o lo pide una vez y lo guarda con permisos 600). Devuelve DelugeWeb o None."""
    conf = STATE_DIR / DELUGE_CONF
    cp = configparser.ConfigParser(interpolation=None)
    if conf.is_file():
        cp.read(conf, encoding="utf-8")
    sec = cp["deluge"] if cp.has_section("deluge") else {}
    url, pw = (sec.get("url", "") or "").strip(), sec.get("password", "") or ""
    if not url or not pw or "TU_" in url:
        if not sys.stdin.isatty():
            if not conf.is_file():
                conf.write_text(DELUGE_TEMPLATE.format(url="https://TU_SERVIDOR/deluge", password="TU_PASSWORD"), encoding="utf-8")
            log(f"Falta configurar Deluge. Edita {conf} (url y password) y vuelve a correr.")
            return None
        import getpass
        log(f"\nNo hay datos de Deluge. Se guardan en {conf} (permisos 600, solo en tu NAS; no va a GitHub).")
        url = input("URL del Web UI de Deluge (ej. https://xxxx.appboxes.co/deluge): ").strip()
        pw = getpass.getpass("Contrasena del Web UI (no se muestra): ")
        if not url or not pw:
            log("Cancelado.")
            return None
        if not args.dry_run:
            conf.write_text(DELUGE_TEMPLATE.format(url=url, password=pw), encoding="utf-8")
            try:
                os.chmod(conf, 0o600)
            except OSError:
                pass
        cp.read_string(DELUGE_TEMPLATE.format(url=url, password=pw))
        sec = cp["deluge"]
    try:
        tmo = int((sec.get("timeout", "120") or "120").strip())
    except ValueError:
        tmo = 120
    cli = DelugeWeb(url, pw, (sec.get("http_user", "") or "").strip(), sec.get("http_pass", "") or "",
                    (sec.get("verify_ssl", "yes") or "yes").strip().lower() not in ("no", "false", "0"), tmo)
    cli.label = (sec.get("label", "mister") or "").strip()
    return cli


def cmd_magnets(root, args):
    """magnets.list -> torrents/*.torrent. Le pide a TU Deluge los metadatos (lista de archivos) de cada
    magnet sin agregarlo ni bajar datos. Los que ya tienen .torrent (mismo infohash) se saltan."""
    mf = STATE_DIR / MAGNETS_FILE
    if not mf.is_file():
        log(f"No existe {mf}. Crea ese archivo con un magnet por linea y vuelve a correr.")
        return
    mags, errs = read_magnets(mf)
    for e in errs:
        log(f"[!] {MAGNETS_FILE}: {e}")
    if not mags:
        log("No hay magnets validos.")
        return
    tdir = STATE_DIR / TORRENTS_DIR
    have = {t["infohash"]: t for t in index_torrents(tdir)[0]}
    todo = [m for m in mags if m["infohash"] not in have]
    log(f"Magnets: {len(mags)} | ya tienen .torrent: {len(mags) - len(todo)} | por obtener: {len(todo)}")
    for m in mags:
        mark = "ya tiene .torrent" if m["infohash"] in have else "por obtener"
        log(f"  - {m['name'] or m['infohash']}  ({fmt_size(m['size']) if m['size'] else '?'})  [{mark}]")
    if args.dry_run:
        if todo:
            log("[DRY-RUN] no se contacta a Deluge.")
        return
    if not todo and not (STATE_DIR / DELUGE_CONF).is_file():
        return
    cli = load_deluge(args)
    if cli is None or (not todo and not cli.label):
        return
    try:
        cli.login()
    except RuntimeError as e:
        log(f"[!] Deluge: {e}")
        return
    tdir.mkdir(exist_ok=True)
    ok = 0
    for m in todo:
        label = m["name"] or m["infohash"]
        log(f"\nPidiendo metadatos de: {label} (puede tardar hasta {cli.timeout}s)...")
        try:
            meta = cli.magnet_metadata(m["uri"], cli.timeout)
        except RuntimeError as e:
            if "already exists" not in str(e):
                log(f"[!] {label}: {e}")
                continue
            log("  Ya esta en tu Deluge: se toma la lista de archivos de ahi (no se modifica nada en Deluge).")
            try:
                j = session_file_list(cli, m)
                fname = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", label).strip(" .") or m["infohash"]
                dest = tdir / (fname + ".torrent.json")
                dest.write_text(json.dumps(j, ensure_ascii=False), encoding="utf-8")
                t = read_torrent(dest)
            except (RuntimeError, OSError, ValueError, KeyError, TypeError) as e2:
                log(f"[!] {label}: {e2}")
                continue
            ok += 1
            log(f"  OK {dest.name}: {len(t['files'])} archivos, {fmt_size(t['total'])}")
            continue
        if hashlib.sha1(meta).hexdigest() != m["infohash"]:
            log(f"[!] {label}: los metadatos recibidos NO coinciden con el hash del magnet; se descartan.")
            continue
        fname = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", label).strip(" .") or m["infohash"]
        dest = tdir / (fname + ".torrent")
        tmp = dest.with_name(dest.name + ".part")
        tmp.write_bytes(b"d4:info" + meta + b"e")
        try:
            t = read_torrent(tmp)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as e:
            tmp.unlink()
            log(f"[!] {label}: metadatos ilegibles ({e})")
            continue
        os.replace(tmp, dest)
        ok += 1
        extra = ""
        if m["size"] and abs(t["total"] - m["size"]) > max(1024 * 1024, m["size"] // 100):
            extra = f"  [aviso: el magnet decia {fmt_size(m['size'])}]"
        log(f"  OK {dest.name}: {len(t['files'])} archivos, {fmt_size(t['total'])}{extra}")
    if todo:
        log(f"\nTorrents obtenidos: {ok} de {len(todo)}. Siguiente: menu 5 -> 2 (discos) o 5 -> 3 (zips).")
    if cli.label:
        try:
            n = cli.label_torrents([m["infohash"] for m in mags], cli.label)
            if n:
                log(f"Etiqueta '{cli.label}' puesta en {n} torrent(s) que ya estan en tu Deluge.")
        except RuntimeError as e:
            log(f"[!] No se pudo poner la etiqueta '{cli.label}': {e}")


# ====================================================================
#  DESCARGA SELECTIVA (Deluge del Appbox -> FTP -> tu NAS) + CONVERSION DE CHD
# ====================================================================
FETCH_LOG = "JD_Fetch.log"
FETCH_TMP = ".fetch"
POLL_SECS = 20
DOCKER_IMAGE = "jd-chdman"
DOCKERFILE = ("FROM debian:stable-slim\n"
              "RUN apt-get update && apt-get install -y --no-install-recommends mame-tools "
              "&& rm -rf /var/lib/apt/lists/*\n")
FTP_TEMPLATE = """
[ftp]
# FTP de tu Appbox (para traer los archivos que Deluge ya bajo)
host = {host}
port = {port}
user = {user}
password = {password}
# auto = intenta FTP con TLS y si no se puede usa FTP normal; tambien: yes / no
tls = auto
# ruta remota equivalente a la carpeta de descargas de Deluge; vacio = se detecta sola
base =
# si pones yes, borra del Appbox cada archivo YA traido y verificado (cuidado si ahi tienes el set completo)
remote_cleanup = no
# si pones yes, conserva el .chd descargado junto al .hdd/.img convertido
keep_chd = no
"""


def flog(msg=""):
    log(msg)
    try:
        with open(STATE_DIR / FETCH_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")
    except OSError:
        pass


def chd_header(path):
    """Cabecera de un CHD v5: dict(logical, rawsha1, sha1) o {'version': n} si es otra version; None si no es CHD."""
    try:
        with open(path, "rb") as fh:
            h = fh.read(124)
    except OSError:
        return None
    if len(h) < 16 or h[:8] != b"MComprHD":
        return None
    ver = struct.unpack(">I", h[12:16])[0]
    if ver != 5 or len(h) < 124:
        return {"version": ver}
    return {"version": 5, "logical": struct.unpack(">Q", h[32:40])[0], "rawsha1": h[64:84].hex(), "sha1": h[84:104].hex()}


def sha1_file(path, chunk=8 << 20):
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def torrent_with_trackers(tpath, trackers):
    """Reempaqueta un .torrent (solo 'info') agregandole los trackers del magnet. -> bytes."""
    data = Path(tpath).read_bytes()
    _meta, span = bdecode(data)
    info = data[span[0]:span[1]]
    out = b"d"
    if trackers:
        out += b"13:announce-listl" + b"".join(b"l%d:%se" % (len(u.encode()), u.encode()) for u in trackers) + b"e"
    return out + b"4:info" + info + b"e"


class Chdman:
    """Convierte CHD -> .hdd/.img con chdman: local (en el PATH o jd_update/chdman) o por Docker."""

    def __init__(self):
        self.bin = None
        self.docker = None
        self.why = ""

    def detect(self):
        local = shutil.which("chdman")
        mine = STATE_DIR / "chdman"
        if not local and mine.is_file():
            try:
                os.chmod(mine, 0o755)
            except OSError:
                pass
            local = str(mine)
        if local:
            self.bin = local
            return True
        if not shutil.which("docker"):
            self.why = ("no hay chdman ni Docker. Opciones: instala Container Manager (Docker) en el NAS, o copia un "
                        f"binario de chdman a {mine}")
            return False
        for dk in (["docker"], ["sudo", "-n", "docker"]):
            try:
                r = subprocess.run(dk + ["info"], capture_output=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if r.returncode == 0:
                self.docker = dk
                break
        if not self.docker:
            self.why = ("Docker existe pero este usuario no puede usarlo. Una sola vez, en el NAS: "
                        "sudo synogroup --add docker $USER  (y vuelve a abrir la sesion SSH), o corre el script con sudo")
            return False
        r = subprocess.run(self.docker + ["image", "inspect", DOCKER_IMAGE], capture_output=True)
        if r.returncode != 0:
            flog("Construyendo la imagen de chdman (una sola vez; necesita internet y tarda unos minutos)...")
            r = subprocess.run(self.docker + ["build", "-t", DOCKER_IMAGE, "-"], input=DOCKERFILE.encode(),
                               capture_output=True)
            if r.returncode != 0:
                self.docker = None
                self.why = "no se pudo construir la imagen de Docker: " + r.stderr.decode(errors="replace")[-400:]
                return False
        return True

    def extract(self, chd, out):
        """CHD de disco duro -> imagen cruda. `chd` y `out` deben estar en la MISMA carpeta."""
        chd, out = Path(chd), Path(out)
        if self.bin:
            cmd = [self.bin, "extracthd", "-i", str(chd), "-o", str(out)]
        else:
            cmd = self.docker + ["run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}", "-v", f"{chd.parent}:/work",
                                 DOCKER_IMAGE, "chdman", "extracthd", "-i", f"/work/{chd.name}", "-o", f"/work/{out.name}"]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode != 0 or not out.is_file():
            tail = (r.stderr or r.stdout).decode(errors="replace").strip().splitlines()[-3:]
            raise RuntimeError("chdman fallo: " + " | ".join(tail))


def convert_chd(tool, chd, out_final, keep_chd_at=None):
    """Convierte y VERIFICA contra el SHA1 de datos de la cabecera del CHD. out_final: Path destino."""
    chd, out_final = Path(chd), Path(out_final)
    hdr = chd_header(chd)
    if hdr is None:
        raise RuntimeError("no es un CHD valido")
    if hdr.get("logical"):
        free = shutil.disk_usage(chd.parent).free
        if free < hdr["logical"] + (200 << 20):
            raise RuntimeError(f"falta espacio: la imagen pesara {fmt_size(hdr['logical'])} y hay {fmt_size(free)} libres")
    tmp = chd.with_name(out_final.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    tool.extract(chd, tmp)
    if hdr.get("rawsha1"):
        got = sha1_file(tmp)
        if got != hdr["rawsha1"]:
            tmp.unlink()
            raise RuntimeError(f"la imagen no coincide con el SHA1 del CHD ({got[:10]} != {hdr['rawsha1'][:10]})")
        note = "SHA1 verificado"
    else:
        note = f"sin verificar (CHD version {hdr.get('version')})"
    out_final.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(tmp), str(out_final))
    return f"{out_final.name} ({fmt_size(out_final.stat().st_size)}, {note})"


class FtpLayoutError(RuntimeError):
    pass


class FtpFetch:
    def __init__(self, cfg):
        self.cfg, self.ftp, self.prefix, self.found = cfg, None, {}, {}

    def connect(self):
        import ftplib
        host, port = self.cfg["host"], int(self.cfg.get("port") or 21)
        tls = (self.cfg.get("tls") or "auto").lower()
        last = None
        for use_tls in ([True, False] if tls == "auto" else [tls in ("yes", "true", "1")]):
            try:
                f = ftplib.FTP_TLS(timeout=60) if use_tls else ftplib.FTP(timeout=60)
                f.connect(host, port)
                f.login(self.cfg["user"], self.cfg["password"])
                if use_tls:
                    f.prot_p()
                f.set_pasv(True)
                f.voidcmd("TYPE I")
                self.ftp = f
                return
            except (OSError, ftplib.Error) as e:
                last = e
        raise RuntimeError(f"no se pudo conectar al FTP {host}:{port}: {last}")

    def alive(self):
        try:
            self.ftp.voidcmd("NOOP")
            return True
        except Exception:  # noqa
            return False

    def ensure(self):
        if self.ftp is None or not self.alive():
            self.connect()

    def _size(self, path):
        import ftplib
        try:
            self.ftp.voidcmd("TYPE I")
            return self.ftp.size(path)
        except ftplib.Error:
            return None

    def _ls(self, path):
        """[(nombre, es_carpeta)] de una carpeta remota (MLSD si se puede; si no, NLST)."""
        try:
            return self._ls_raw(path)
        finally:
            try:
                self.ftp.voidcmd("TYPE I")          # los listados dejan el FTP en modo ASCII, y SIZE ahi falla
            except Exception:  # noqa
                pass

    def _ls_raw(self, path):
        import ftplib
        out = []
        try:
            for name, facts in self.ftp.mlsd(path or "/", facts=["type"]):
                t = (facts.get("type") or "").lower()
                if name in (".", "..") or t in ("cdir", "pdir"):
                    continue
                out.append((name, t == "dir"))
            return out
        except (ftplib.Error, OSError, AttributeError):
            pass
        try:
            names = self.ftp.nlst(path or "/")
        except (ftplib.Error, OSError):
            return []
        cwd = self.ftp.pwd()
        for n in names:
            n = n.rsplit("/", 1)[-1]
            if n in (".", "..", ""):
                continue
            try:
                self.ftp.cwd((path.rstrip("/") + "/" + n) if path else n)
                out.append((n, True))
            except ftplib.Error:
                out.append((n, False))
        try:
            self.ftp.cwd(cwd)
        except ftplib.Error:
            pass
        return out

    FTP_HINTS = ("torrents", "completed", "downloading", "deluge", "downloads", "download", "data", "storage",
                 "files", "complete", "mame", "mister")

    def _discover(self, top, maxlist=600, tmax=150):
        """Carpetas del FTP llamadas `top` (el nombre del torrent). Busqueda por prioridad desde la raiz (primero las
        carpetas con nombres tipicos de Deluge). -> lista de rutas padre."""
        import heapq
        if top in self.found:
            return self.found[top]
        t0, hits, cnt, listed = time.time(), [], 0, 0
        heap = [(0, 0, "")]
        visited = []
        while heap and listed < maxlist and time.time() - t0 < tmax:
            _pr, _n, base = heapq.heappop(heap)
            listed += 1
            visited.append(base or "/")
            for name, isdir in self._ls(base or "/"):
                if not isdir:
                    continue
                if name == top:
                    hits.append(base)
                    continue
                depth = base.count("/") + 1
                if depth >= 9:
                    continue
                cnt += 1
                heapq.heappush(heap, (depth + (0 if name.lower() in self.FTP_HINTS else 3), cnt, base + "/" + name))
            if hits:
                break
        self.found[top] = hits
        self.visited = visited
        return hits

    def _find_mount(self, first, maxdepth=3, maxlist=80):
        """Carpeta del FTP que contiene la carpeta `first` (ej. 'torrents'): el prefijo que separa la ruta de Deluge de la
        del FTP (ej. /apps/deluge.tuusuario.appboxes.co). -> ruta ('' = raiz) o None."""
        key = "mount:" + first
        if key in self.found:
            return self.found[key]
        level, listed, hit = [""], 0, None
        for _d in range(maxdepth):
            nxt = []
            for base in level:
                if listed >= maxlist:
                    break
                listed += 1
                items = self._ls(base or "/")
                if any(isdir and n == first for n, isdir in items):
                    hit = base
                    break
                nxt += [base + "/" + n for n, isdir in items if isdir][:60]
            if hit is not None:
                break
            level = nxt
        self.found[key] = hit
        return hit

    def locate(self, save_path, rel, size):
        """Ruta REMOTA del archivo. Primero se prueba save_path de Deluge (quitando carpetas del principio, porque el
        FTP suele estar 'enjaulado'); si no, se busca por el nombre de la carpeta del torrent en todo el FTP."""
        self.ensure()
        base = (self.cfg.get("base") or "").strip().rstrip("/")
        if base:
            p = base + "/" + rel
            if self._size(p) == size:
                return p
        parts = [x for x in (save_path or "").split("/") if x]
        tried = []
        for k in range(len(parts) + 1):
            pre = "/".join(parts[k:])
            for lead in ("/", ""):
                p = lead + (pre + "/" if pre else "") + rel
                tried.append(p)
                if self._size(p) == size:
                    return p
        if parts:
            mount = self._find_mount(parts[0])
            if mount is not None:
                for sp in [save_path] + ["/" + "/".join(parts[:-1] + [x]) for x in ("completed", "downloading") if parts[-1] != x]:
                    p = mount + "/" + sp.strip("/") + "/" + rel
                    tried.append(p)
                    if self._size(p) == size:
                        self.prefix[save_path] = mount
                        return p
        top = rel.split("/")[0]
        self.found.pop(top, None)               # el torrent puede haberse movido (downloading -> completed)
        for parent in self._discover(top):
            p = parent + "/" + rel
            if self._size(p) == size:
                return p
        root_names = "?"
        try:
            mount = self.found.get("mount:" + parts[0]) if parts else None
            sims = []
            if mount is not None:
                word = top.split()[0].lower()
                for sub in ("completed", "downloading"):
                    base_dir = mount + "/" + "/".join(parts[:-1] + [sub]) if parts[:-1] else mount + "/" + sub
                    for n, d in self._ls(base_dir):
                        if d and word in n.lower():
                            sims.append(f"{base_dir}/{n}")
            root_names = ("carpetas parecidas: " + "; ".join(sims[:6])) if sims else "no hay carpetas parecidas en completed/downloading (prefijo FTP: " + str(mount) + ")"
        except Exception:  # noqa
            pass
        raise FtpLayoutError("no encuentro el archivo en el FTP (tamano esperado " + fmt_size(size) + "). Deluge dice "
                           + (save_path or "?") + "; " + (root_names or "?")
                           + f". Busque en {len(getattr(self, 'visited', []))} carpetas del FTP sin hallar la carpeta '{top}'. "
                           + "Ejecuta  python3 jd_update.py ftpscan  para ver el arbol, o pon la ruta en 'base' del [ftp] de " + DELUGE_CONF)

    def download(self, remote, local_part, size, label=""):
        """Descarga con reanudacion. Devuelve cuando el archivo local tiene exactamente `size` bytes."""
        import ftplib
        local_part = Path(local_part)
        local_part.parent.mkdir(parents=True, exist_ok=True)
        last_print = [0.0]
        for attempt in range(1, 6):
            have = local_part.stat().st_size if local_part.exists() else 0
            if have > size:
                local_part.unlink()
                have = 0
            if have == size:
                return
            try:
                self.ensure()
                with open(local_part, "ab" if have else "wb") as fh:
                    done = [have]

                    def cb(chunk):
                        fh.write(chunk)
                        done[0] += len(chunk)
                        now = time.time()
                        if now - last_print[0] > 30:
                            last_print[0] = now
                            log(f"    {label}: {fmt_size(done[0])} / {fmt_size(size)} ({done[0] * 100 // max(size, 1)}%)")
                    self.ftp.retrbinary("RETR " + remote, cb, blocksize=1 << 20, rest=have or None)
            except (OSError, ftplib.Error, EOFError) as e:
                self.ftp = None
                flog(f"    FTP interrumpido ({e}); reintento {attempt}/5 (se reanuda)")
                time.sleep(min(5 * attempt, 30))
        have = local_part.stat().st_size if local_part.exists() else 0
        if have != size:
            raise RuntimeError(f"descarga incompleta ({fmt_size(have)} de {fmt_size(size)})")

    def delete(self, remote):
        self.ensure()
        self.ftp.delete(remote)

    def close(self):
        try:
            if self.ftp:
                self.ftp.quit()
        except Exception:  # noqa
            pass


def load_ftp(args):
    """[ftp] de jd_deluge.ini (se pide una vez y se guarda con permisos 600). -> dict o None."""
    conf = STATE_DIR / DELUGE_CONF
    cp = configparser.ConfigParser(interpolation=None)
    if conf.is_file():
        cp.read(conf, encoding="utf-8")
    sec = dict(cp["ftp"]) if cp.has_section("ftp") else {}
    if not sec.get("host") or not sec.get("user") or not sec.get("password") or "TU_" in sec.get("host", ""):
        if not sys.stdin.isatty():
            log(f"Falta el [ftp] en {conf} (host, user, password). Editalo y vuelve a correr.")
            return None
        import getpass
        log(f"\nNo hay datos del FTP del Appbox. Se guardan en {conf} (permisos 600, solo en tu NAS).")
        host = input("Servidor FTP (ej. tuusuario.appboxes.co): ").strip()
        port = input("Puerto [21]: ").strip() or "21"
        user = input("Usuario FTP: ").strip()
        pw = getpass.getpass("Contrasena FTP (no se muestra): ")
        if not host or not user or not pw:
            log("Cancelado.")
            return None
        block = FTP_TEMPLATE.format(host=host, port=port, user=user, password=pw)
        if not args.dry_run:
            text = conf.read_text(encoding="utf-8") if conf.is_file() else ""
            text = re.sub(r"(?ms)^\[ftp\].*?(?=^\[|\Z)", "", text)
            conf.write_text(text.rstrip("\n") + "\n" + block, encoding="utf-8")
            try:
                os.chmod(conf, 0o600)
            except OSError:
                pass
        cp3 = configparser.ConfigParser(interpolation=None)
        cp3.read_string(block)
        sec = dict(cp3["ftp"])
    return sec


def ftp_flag(sec, key):
    return (sec.get(key, "no") or "no").strip().lower() in ("yes", "true", "1", "si")


def deluge_add_or_update(cli, t, tdir, wanted_idx, magnets):
    """Asegura que el torrent este en la sesion de Deluge con (al menos) esos archivos activos. Nunca baja la
    prioridad de lo que ya estaba. -> torrent_id."""
    tid = t["infohash"]
    present = set(cli.call("core.get_session_state") or [])
    n = t["count"]
    if tid not in present:
        tfile = tdir / t["file"]
        if t["file"].lower().endswith(".json"):          # solo teniamos la lista: hay que pedir el torrent real
            m = magnets.get(tid)
            if not m:
                raise RuntimeError(f"{t['file']} no esta en Deluge y no hay magnet en {MAGNETS_FILE} para recrearlo")
            meta = cli.magnet_metadata(m["uri"], cli.timeout)
            real = tdir / (t["file"][:-len(".json")])
            real.write_bytes(b"d4:info" + meta + b"e")
            tfile = real
        prios = [0] * n
        for i in wanted_idx:
            prios[i] = 4
        dump = torrent_with_trackers(tfile, (magnets.get(tid) or {}).get("trackers", []))
        import base64 as b64
        res = cli.call("core.add_torrent_file", [tfile.name, b64.b64encode(dump).decode(),
                                                 {"add_paused": True, "auto_managed": False, "file_priorities": prios}])
        if not res:
            raise RuntimeError("Deluge no acepto el torrent")
        flog(f"  Torrent agregado a Deluge solo con {len(wanted_idx)} archivo(s) activos de {n}")
    else:
        st = cli.call("core.get_torrent_status", [tid, ["file_priorities", "state"]]) or {}
        cur = list(st.get("file_priorities") or [])
        if len(cur) == n:
            new = list(cur)
            for i in wanted_idx:
                if new[i] == 0:
                    new[i] = 4
            if new != cur:
                cli.call("core.set_torrent_options", [[tid], {"file_priorities": new}])
                flog(f"  Torrent ya estaba en Deluge: se activaron {sum(1 for a, b in zip(cur, new) if a != b)} archivo(s) mas")
        flog("  (el torrent ya estaba en Deluge; no se quita nada)")
    if cli.label:
        try:
            cli.label_torrents([tid], cli.label)
        except RuntimeError as e:
            flog(f"  [!] etiqueta: {e}")
    st = cli.call("core.get_torrent_status", [tid, ["state"]]) or {}
    if (st.get("state") or "").lower() in ("paused", "queued"):
        cli.call("core.resume_torrent", [tid])
    return tid


def process_outputs(root, job, staged, tool, keep_chd, report):
    """Coloca/convierte un archivo ya traido. job['outs']: [{type: zip|chd|conv, dest}]. -> (ok, fallos)."""
    fails = 0
    placed = False
    outs = job["outs"]
    for o in [x for x in outs if x["type"] == "conv"]:
        dest = root / o["dest"]
        if dest.exists() and dest.stat().st_size > 0:
            continue
        if tool is None:
            fails += 1
            flog(f"  [pendiente] {o['dest']}: sin chdman ({report['tool_why']}); el CHD queda para convertirlo despues")
            o["pending"] = True
            continue
        try:
            msg = convert_chd(tool, staged, dest)
            flog(f"  CONVERTIDO {o['dest']}: {msg}")
            report["converted"] += 1
        except (RuntimeError, OSError) as e:
            fails += 1
            flog(f"  [X] conversion de {o['dest']}: {e}")
            if "SHA1" in str(e):
                flog("      el CHD llego danado: se descarta y se volvera a bajar en la siguiente corrida")
                o["corrupt"] = True
            else:
                o["pending"] = True
    keep = [x for x in outs if x["type"] in ("zip", "chd")]
    pend = [x for x in outs if x.get("pending")]
    if any(x.get("corrupt") for x in outs):
        keep = []
    for i, o in enumerate(keep):
        dest = root / o["dest"]
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        last = (i == len(keep) - 1) and not pend and not (keep_chd and any(x["type"] == "conv" for x in outs))
        try:
            if last:
                shutil.move(str(staged), str(dest))
                placed = True
            else:
                shutil.copy2(staged, dest)
            report["placed"] += 1
            flog(f"  COLOCADO {o['dest']}")
        except OSError as e:
            fails += 1
            flog(f"  [X] no se pudo colocar {o['dest']}: {e}")
    leftovers = (pend or (keep_chd and any(x["type"] == "conv" for x in outs))) and not any(x.get("corrupt") for x in outs)
    if leftovers and not placed and staged.exists():           # el CHD se queda junto al destino para convertirlo luego
        conv = [x for x in outs if x["type"] == "conv"][0]
        target = (root / conv["dest"]).parent / job["f"]["path"].rsplit("/", 1)[-1]
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staged), str(target))
            flog(f"  CHD guardado en {rel(root, target)}")
            placed = True
        except OSError as e:
            fails += 1
            flog(f"  [X] no se pudo guardar el CHD: {e}")
    if not placed and staged.exists():
        try:
            staged.unlink()
        except OSError:
            pass
    return fails


def fetch_torrent(root, args, cli, ftp, t, jobs, tdir, magnets, tool, keep_chd, cleanup, report):
    """Descarga (y procesa) todos los archivos de un torrent. -> fallos."""
    fails = 0
    wanted = sorted(j["f"]["index"] for j in jobs)
    tid = deluge_add_or_update(cli, t, tdir, wanted, magnets)
    by_idx = {j["f"]["index"]: j for j in jobs}
    pending = set(wanted)
    total = sum(j["f"]["size"] for j in jobs)
    last_line, last_prog, last_move, bad_polls = 0.0, -1, time.time(), 0
    flog(f"  Esperando {len(jobs)} archivo(s), {fmt_size(total)} (Ctrl+C para pausar: Deluge sigue y al volver a correr se retoma)")
    while pending:
        try:
            st = cli.call("core.get_torrent_status", [tid, ["state", "file_progress", "save_path", "download_payload_rate",
                                                             "num_peers", "num_seeds", "total_done", "message"]]) or {}
            bad_polls = 0
        except RuntimeError as e:
            bad_polls += 1
            if bad_polls > 30:
                raise
            flog(f"  [!] Deluge no respondio ({e}); reintento {bad_polls}/30")
            time.sleep(min(15 * bad_polls, 60))
            try:
                cli.login()
            except RuntimeError:
                pass
            continue
        state = (st.get("state") or "").lower()
        if state == "error":
            raise RuntimeError("Deluge marco el torrent con error: " + str(st.get("message") or "?"))
        fp = st.get("file_progress") or []
        done_now = [i for i in sorted(pending) if i < len(fp) and fp[i] >= 0.99999]
        for i in done_now:
            j = by_idx[i]
            f = j["f"]
            staged = STATE_DIR / FETCH_TMP / tid[:8] / f"{i}_{f['path'].rsplit('/', 1)[-1]}"
            try:
                if not (staged.is_file() and staged.stat().st_size == f["size"]):
                    time.sleep(3)                           # que Deluge termine de escribir a disco
                    free = shutil.disk_usage(STATE_DIR).free
                    if free < f["size"] + (1 << 30):
                        raise RuntimeError(f"falta espacio en el NAS ({fmt_size(free)} libres para {fmt_size(f['size'])})")
                    remote = None
                    for attempt in range(3):
                        try:
                            remote = ftp.locate(st.get("save_path") or "", f["path"], f["size"])
                            break
                        except FtpLayoutError:
                            if attempt == 2:
                                raise
                            flog(f"  (aun no aparece en el FTP; Deluge dice {st.get('save_path')}; reintento)")
                            time.sleep(20)
                            st = cli.call("core.get_torrent_status", [tid, ["save_path"]]) or st
                    flog(f"  Trayendo por FTP: {f['path'].rsplit('/', 1)[-1]} ({fmt_size(f['size'])})")
                    ftp.download(remote, str(staged) + ".part", f["size"], f["path"].rsplit("/", 1)[-1])
                    shutil.move(str(staged) + ".part", str(staged))
                else:
                    remote = None
                if staged.name.lower().endswith(".zip") or any(o["type"] == "zip" for o in j["outs"]):
                    with zipfile.ZipFile(staged) as zf:
                        bad = zf.testzip()
                    if bad:
                        raise RuntimeError(f"zip danado (falla {bad})")
                elif any(o["type"] in ("chd", "conv") for o in j["outs"]) and chd_header(staged) is None:
                    raise RuntimeError("el archivo traido no es un CHD valido")
                fails += process_outputs(root, j, staged, tool, keep_chd, report)
                report["files"] += 1
                report["bytes"] += f["size"]
                if cleanup and remote:
                    try:
                        ftp.delete(remote)
                        flog(f"  (borrado del Appbox: {remote})")
                    except Exception as e:  # noqa
                        flog(f"  [!] no se pudo borrar del Appbox: {e}")
            except FtpLayoutError:
                raise
            except (RuntimeError, OSError, zipfile.BadZipFile) as e:
                fails += 1
                flog(f"  [X] {f['path'].rsplit('/', 1)[-1]}: {e}")
                if staged.exists():
                    try:
                        staged.unlink()
                    except OSError:
                        pass
            pending.discard(i)
            last_move = time.time()
        if not pending:
            break
        got = sum(by_idx[i]["f"]["size"] * min(fp[i], 1.0) for i in pending if i < len(fp))
        now = time.time()
        if now - last_line > 60:
            last_line = now
            flog(f"  [{state or '?'}] {len(wanted) - len(pending)}/{len(wanted)} archivos | pendiente ~{fmt_size(sum(by_idx[i]['f']['size'] for i in pending) - got)}"
                 f" | {fmt_size(st.get('download_payload_rate') or 0)}/s | peers {st.get('num_peers', 0)} (seeds {st.get('num_seeds', 0)})")
        if got != last_prog:
            last_prog, last_move = got, now
        if now - last_move > 1800 and not (st.get("download_payload_rate") or 0):
            raise RuntimeError("30 minutos sin avance ni velocidad (sin seeds?). Revisa el torrent en Deluge y vuelve a correr")
        time.sleep(POLL_SECS)
    return fails


def disk_jobs(root, args, tmap):
    """Trabajos de discos a partir de JD_Discos.json (lo genera cmd_disks). -> (descargas, conversiones locales, avisos)."""
    jf = STATE_DIR / DISKS_JSON
    if not jf.is_file():
        return {}, [], []
    items = json.loads(jf.read_text(encoding="utf-8"))
    dl, local, notes = {}, [], []
    for it in items:
        st = it.get("status")
        if it["kind"] == "POSIBLE":
            continue
        want_rel = it.get("dest") or it.get("destino")
        if st == "APROX":
            notes.append(f"{it['name']}: nombre aproximado, no se descarga solo (revisa el log de discos)")
            continue
        if st == "CONVERTIR" and it.get("chd_local"):
            out = (it["dest"] if it.get("dest") else str(Path(it["chd_local"]).parent / it["name"]).replace("\\", "/"))
            local.append({"chd": it["chd_local"], "dest": out})
            continue
        if st == "COPIAR" and it.get("chd_local") and it.get("dest"):
            local.append({"chd": it["chd_local"], "dest": it["dest"], "copy": True})
            continue
        if st != "BAJAR" or not it.get("torrent") or not want_rel:
            continue
        tr = it["torrent"]
        t = tmap.get(tr["file"])
        f = next((x for x in t["files"] if x["path"] == tr["path"]), None) if t else None
        if not f:
            continue
        j = dl.setdefault((t["infohash"], f["index"]), {"t": t, "f": f, "outs": []})
        typ = "conv" if it.get("convert") else "chd"
        if not any(o["dest"] == want_rel for o in j["outs"]):
            j["outs"].append({"type": typ, "dest": want_rel})
    return dl, local, notes


def cmd_ftpscan(args):
    """Muestra el arbol del FTP del Appbox (hasta 3 niveles) para saber donde esta lo que baja Deluge."""
    sec = load_ftp(args)
    if not sec:
        return 1
    ft = FtpFetch(sec)
    ft.connect()
    log(f"Arbol del FTP {sec.get('host')} (3 niveles; carpetas):")
    shown = [0]

    def walk(path, depth):
        if depth > 3 or shown[0] > 60:
            return
        items = ft._ls(path or "/")
        dirs = [n for n, d in items if d]
        files = [n for n, d in items if not d]
        for n in sorted(dirs)[:15]:
            shown[0] += 1
            log("  " * depth + "/" + n + "/")
            if n.lower() in ("storage", "movies", "series", "music", "tv"):
                log("  " * (depth + 1) + "(no se lista)")
                continue
            walk(path + "/" + n, depth + 1)
        if files and depth >= 1:
            log("  " * depth + f"({len(files)} archivo(s), ej.: {', '.join(sorted(files)[:3])})")
    walk("", 0)
    ft.close()
    return 0


def cmd_fetch(root, args):
    """TODO en uno: copia lo local, y lo que falte (zips de ROMs y CHD de las MRA) lo baja de los torrents usando
    tu Deluge (solo esos archivos), lo trae por FTP, convierte los CHD a .hdd/.img y verifica."""
    what = getattr(args, "fetch_only", None)
    tdir = STATE_DIR / TORRENTS_DIR
    torrents, tidx, terrors = index_torrents(tdir)
    for n, why in terrors:
        log(f"[!] {n}: {why}")
    if not torrents:
        log(f"No hay torrents en {tdir}. Primero: menu 5 -> 1 (Magnets).")
        return 1
    tmap = {t["file"]: t for t in torrents}
    jobs = {}
    failed = 0
    if what != "disks":
        log("\n=== Paso 1: copiar de tu fuente local lo que ya tienes ===")
        if getattr(args, "skip_copy", False):
            log("(ya hecho en el paso anterior)")
        elif Path(args.source).expanduser().is_dir():
            cmd_check(root, args, copy=True)
        else:
            log(f"(fuente local no disponible: {args.source}; se salta este paso)")
        log("\n=== Paso 2: zips que faltan y estan en los torrents ===")
        excl = prepare_excludes(root, args)
        R = analyze(root, args, excl)
        plan, dats, dat_errors, _parts = zip_plan(root, args, R, torrents, tidx)
        for tg in plan["targets"].values():
            j = jobs.setdefault((tg["torrent"]["infohash"], tg["file"]["index"]),
                                {"t": tg["torrent"], "f": tg["file"], "outs": []})
            j["outs"].append({"type": "zip", "dest": tg["dest"]})
        log(f"Zips por bajar: {len(plan['targets'])} ({fmt_size(sum(v['file']['size'] for v in plan['targets'].values()))}); "
            f"sin fuente: {len(plan['unresolved'])} partes")
    local_conv, notes = [], []
    if what != "zips":
        log("\n=== Paso 3: discos (CHD / HDD / IMG) ===")
        if getattr(args, "skip_disks_scan", False) and (STATE_DIR / DISKS_JSON).is_file():
            log("(comprobacion de discos ya hecha en el paso anterior)")
        else:
            try:
                (STATE_DIR / DISKS_JSON).unlink()
            except OSError:
                pass
            cmd_disks(root, args)
        dj, local_conv, notes = disk_jobs(root, args, tmap)
        for k, v in dj.items():
            if k in jobs:
                jobs[k]["outs"] += v["outs"]
            else:
                jobs[k] = v
    for n in notes:
        log(f"  aviso: {n}")
    total = sum(j["f"]["size"] for j in jobs.values())
    n_conv = sum(1 for j in jobs.values() for o in j["outs"] if o["type"] == "conv") + len(local_conv)
    log(f"\nRESUMEN: {len(jobs)} archivo(s) a bajar de torrents = {fmt_size(total)} | {n_conv} conversion(es) CHD->imagen | "
        f"{len(local_conv)} ya con CHD local")
    if not jobs and not local_conv:
        log("Nada que descargar ni convertir.")
        return 0
    free = shutil.disk_usage(STATE_DIR).free
    log(f"Espacio libre en el NAS: {fmt_size(free)}")
    if free < total + (2 << 30):
        log("[!] Espacio insuficiente para esta descarga (se necesita ademas margen para convertir). Libera espacio y vuelve a correr.")
        return 1
    if args.dry_run:
        by_t = defaultdict(list)
        for j in jobs.values():
            by_t[j["t"]["file"]].append(j)
        for tf, js in by_t.items():
            log(f"  [{tf}] {len(js)} archivo(s), {fmt_size(sum(j['f']['size'] for j in js))}")
        log("[DRY-RUN] no se contacta a Deluge ni se descarga nada.")
        return 0
    if not confirm(f"Descargar {fmt_size(total)} y convertir? ", args):
        log("Cancelado.")
        return 0

    report = {"files": 0, "bytes": 0, "placed": 0, "converted": 0, "tool_why": ""}
    cli = load_deluge(args) if jobs else None
    sec = load_ftp(args) if (jobs and cli is not None) else {}
    if jobs and (cli is None or not sec):
        return 1
    tool = None
    need_tool = any(o["type"] == "conv" for j in jobs.values() for o in j["outs"]) or local_conv
    if need_tool and not getattr(args, 'no_convert', False):
        ch = Chdman()
        if ch.detect():
            tool = ch
            flog(f"Conversion con {'chdman local' if ch.bin else 'Docker (' + DOCKER_IMAGE + ')'}")
        else:
            report["tool_why"] = ch.why
            flog(f"[!] No se puede convertir ahora: {ch.why}")
    elif getattr(args, 'no_convert', False):
        report["tool_why"] = "--no-convert"
    keep_chd = ftp_flag(sec, "keep_chd") if sec else False
    cleanup = ftp_flag(sec, "remote_cleanup") if sec else False

    # conversiones/copias de CHD que ya estaban en el NAS
    for lc in local_conv:
        src, dest = root / lc["chd"], root / lc["dest"]
        if dest.exists() and dest.stat().st_size > 0:
            continue
        try:
            if lc.get("copy"):
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                flog(f"COPIADO {lc['dest']}")
            elif tool is None:
                raise RuntimeError("sin chdman: " + report["tool_why"])
            else:
                flog(f"Convirtiendo {lc['chd']} ...")
                flog(f"  CONVERTIDO {lc['dest']}: {convert_chd(tool, src, dest)}")
                report["converted"] += 1
        except (RuntimeError, OSError) as e:
            failed += 1
            flog(f"  [X] {lc['dest']}: {e}")

    if jobs:
        magnets = {}
        mf = STATE_DIR / MAGNETS_FILE
        if mf.is_file():
            magnets = {m["infohash"]: m for m in read_magnets(mf)[0]}
        try:
            cli.login()
        except RuntimeError as e:
            flog(f"[!] Deluge: {e}")
            return 1
        ftp = FtpFetch(sec)
        by_t = defaultdict(list)
        for j in jobs.values():
            by_t[j["t"]["infohash"]].append(j)
        try:
            for ih, js in by_t.items():
                t = js[0]["t"]
                flog(f"\n### {t['name']}: {len(js)} archivo(s), {fmt_size(sum(j['f']['size'] for j in js))}")
                try:
                    failed += fetch_torrent(root, args, cli, ftp, t, js, tdir, magnets, tool, keep_chd, cleanup, report)
                except RuntimeError as e:
                    failed += len(js)
                    flog(f"  [X] {t['name']}: {e}")
        except KeyboardInterrupt:
            flog("\nInterrumpido: Deluge sigue bajando; vuelve a correr 'fetch' y se retoma lo que falte.")
            failed += 1
        finally:
            ftp.close()
    flog(f"\nLISTO: {report['files']} archivo(s) traidos ({fmt_size(report['bytes'])}), {report['placed']} colocados, "
         f"{report['converted']} convertidos, {failed} fallo(s). Log: {STATE_DIR / FETCH_LOG}")
    if what != "disks" and report["files"]:
        log("\nRevision final de las MRA con lo nuevo:")
        if Path(args.source).expanduser().is_dir():
            cmd_check(root, args)
    return failed


def cmd_organize(root, args):
    mode = args.mode
    scan_dirs = list(args.scan or DEFAULT_SCAN)
    if mode == "move" and not args.scan:
        scan_dirs.append(ORGANIZER)  # al mover, volver a correr debe reconocer lo ya ordenado
    org = root / ORGANIZER
    legacy = migrate_legacy_dirs(root, org, args.dry_run)
    if legacy:
        log(f"Carpetas de core {'por mover' if args.dry_run else 'movidas'} a {CORE_DIR}/: "
            f"{len(legacy)}  (ej. {legacy[0][0]} -> {CORE_DIR}/{legacy[0][1]})")
    mras = find_mras(root, scan_dirs)
    if not mras:
        log(f"No encontre ninguna .mra en: {', '.join(scan_dirs)}")
        return
    log(f"MRAs encontradas: {len(mras)}")
    core_root = org / CORE_DIR
    plan, errors = build_plan(root, mras, mode, core_root)
    made = organize(root, plan, errors, mode, org, args.dry_run, args.yes, args.cores_dir, core_root)
    if made >= 0 and not args.no_extra:
        sync_special(root, org, plan, mode, args.dry_run)


def cmd_check(root, args, copy=False, rebuild=False):
    excl = prepare_excludes(root, args)
    source = Path(args.source).expanduser()
    db_path = STATE_DIR / ROMS_DB
    R = analyze(root, args, excl)
    copy_rep, rb, idx = None, None, None

    if copy:
        if R.need_zips or args.rescan:
            idx = load_roms_db(source, db_path, args.rescan, create=True)
        if R.need_zips:
            log(f"\nFaltan {len(R.need_zips)} zip(s) que las MRA necesitan: se buscan en la fuente ({source})")
            copy_rep = copy_missing(root, source, idx, [R.zip_orig[z] for z in R.need_zips], args)
            if copy_rep["copied"]:
                log("\nSe vuelve a revisar con lo copiado:")
                R = analyze(root, args, excl)
        else:
            log("\nNo falta ningun zip por copiar.")
    if rebuild:
        log("")
        rb = do_rebuild(root, R, args)
        if rb[0]:
            log("\nSe vuelve a revisar con los zips creados:")
            R = analyze(root, args, excl)
    if idx is None:
        idx = load_roms_db(source, db_path, False, create=False)
    tor_names = None
    _tdir = STATE_DIR / TORRENTS_DIR
    if _tdir.is_dir() and (any(_tdir.glob("*.torrent")) or any(_tdir.glob("*.torrent.json"))):
        tor_names = set(index_torrents(_tdir)[1])
    write_report(root, R, args, idx, copy_rep, rb, tor_names)
    log("")
    print_summary(root, R, args)
    return R


def cmd_rescan(root, args):
    source = Path(args.source).expanduser()
    load_roms_db(source, STATE_DIR / ROMS_DB, rescan=True, create=True)


def run_action(act, root, args):
    """Ejecuta una accion. Devuelve el numero de fallos (para el codigo de salida)."""
    failed = 0
    log(f"Raiz: {root}" + ("   [DRY-RUN]" if args.dry_run else ""))
    if act in ("update", "organize", "all") and not any((root / m).is_dir() for m in ("_Arcade", "games")):
        if not confirm(f"[!] {root} no tiene _Arcade ni games. Se crearia todo desde cero ahi. "
                       "Es la raiz correcta (instalacion nueva)?", args, strict=True):
            sys.exit("Cancelado. Si la raiz es otra, usa --root /ruta/a/MiSTer_NEO")
    if act == "update":
        failed = run_update(args, root)["failed"]
    elif act == "organize":
        cmd_organize(root, args)
    elif act == "check":
        cmd_check(root, args)
        log("\n--- Discos (CHD / HDD / IMG) ---")
        cmd_disks(root, args)
    elif act == "copy":
        cmd_check(root, args, copy=True)
        log("\n--- Discos (CHD / HDD / IMG) ---")
        cmd_disks(root, args)
    elif act == "rebuild":
        cmd_check(root, args, rebuild=True)
    elif act == "rescan":
        cmd_rescan(root, args)
    elif act == "all":
        log("\n##### 1/4  ACTUALIZAR #####")
        failed = run_update(args, root)["failed"]
        log("\n##### 2/4  ORGANIZAR MRA POR CORE #####")
        cmd_organize(root, args)
        log("\n##### 3/4  REVISAR Y COPIAR ROMS + COMPROBAR DISCOS (CHD/HDD/IMG) #####")
        cmd_check(root, args, copy=True)
        cmd_disks(root, args)
        log("\n##### 4/4  DESCARGAR LO QUE FALTA (torrents via Deluge) Y CONVERTIR CHD #####")
        args.skip_copy = True
        args.skip_disks_scan = True
        try:
            failed += cmd_fetch(root, args) or 0
        finally:
            args.skip_copy = False
            args.skip_disks_scan = False
    elif act == "torrents":
        cmd_torrents(root, args)
    elif act == "disks":
        cmd_disks(root, args)
    elif act == "magnets":
        cmd_magnets(root, args)
    elif act == "fetch":
        failed = cmd_fetch(root, args)
    elif act == "ftpscan":
        failed = cmd_ftpscan(args)
    elif act == "log":
        cmd_log(root, args)
    elif act == "github":
        cmd_github(root, args)
    elif act == "github-lists":
        cmd_github(root, args, only_lists=True)
    elif act == "undo-organize":
        undo(root)
    elif act == "undo-rebuild":
        undo_rebuild(root)
    return failed


ACCIONES = ["update", "organize", "check", "copy", "rebuild", "rescan", "all", "torrents", "magnets", "disks", "fetch", "ftpscan", "log", "github", "github-lists",
            "undo-organize", "undo-rebuild"]
# Menu en arbol: (tecla, texto, accion) o (tecla, texto, [submenu]) o (tecla, texto, accion, {ajustes a args}).
# Los comandos de linea (python3 jd_update.py update, check, ...) no cambian: esto es solo el menu.
MENU = [
    ("1", "TODO: actualizar + organizar + copiar ROMs + descargar lo que falte", "all"),
    ("2", "Actualizar DBs                (descarga lo de jd_update.list)", "update"),
    ("3", "Organizar MRA                 (_CORE, _A-Z, _DEBUT, _UPDATED...)", "organize"),
    ("4", "ROMs: revisar, copiar, reconstruir", [
        ("1", "Revisar                (ROMs + discos CHD/HDD/IMG; solo lee)", "check"),
        ("2", "Revisar y COPIAR los zips que faltan (y comprobar discos)", "copy"),
        ("3", "Reconstruir zips solucionables (con lo que ya tienes)", "rebuild"),
        ("4", "Reescanear la fuente de ROMs (regenera jd_roms.db)", "rescan"),
    ]),
    ("5", "Torrents y discos (CHD / HDD / IMG)", [
        ("1", "Magnets -> .torrent     (magnets.list; pide solo metadatos a tu Deluge)", "magnets"),
        ("2", "Discos: que MRA piden HDD/IMG/CHD y cuales faltan (solo revisa)", "disks"),
        ("3", "Zips: planear que bajar de los torrents (solo planea)", "torrents"),
        ("4", "Buscar un archivo o juego dentro de los torrents", "torrents", {"_ask_find": True}),
        ("5", "DESCARGAR Y CONVERTIR todo lo que falta (zips + CHD -> .hdd/.img) via tu Deluge", "fetch"),
        ("6", "Descargar y convertir SOLO discos (CHD)", "fetch", {"fetch_only": "disks"}),
        ("7", "Descargar SOLO zips de ROMs", "fetch", {"fetch_only": "zips"}),
        ("8", "Ver el arbol del FTP del Appbox (para localizar donde baja Deluge)", "ftpscan"),
    ]),
    ("6", "Ver logs", [
        ("1", "Revision de ROMs (JD_Check.log)", "log", {"log": CHECK_LOG}),
        ("2", "Discos (JD_Discos.log)", "log", {"log": DISKS_LOG}),
        ("3", "Torrents (JD_Torrents.log)", "log", {"log": TORRENT_LOG}),
    ]),
    ("7", "Mantenimiento", [
        ("1", "Actualizar el script y las listas desde GitHub", "github"),
        ("2", "Actualizar solo las listas (jd_update.list, jd_exclude.list)", "github-lists"),
        ("3", "Deshacer el organizador", "undo-organize"),
        ("4", "Deshacer los zips creados por 'Reconstruir'", "undo-rebuild"),
    ]),
]


def _run_leaf(act, extra, root, args):
    """Ejecuta una accion del menu con ajustes temporales a args. Devuelve False si se debe salir del menu."""
    saved = {}
    extra = dict(extra or {})
    if extra.pop("_ask_find", False):
        try:
            txt = input("Texto a buscar (nombre de juego o archivo, ej. area51): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return True
        if not txt:
            return True
        extra["find"] = txt
    for k, v in extra.items():
        saved[k] = getattr(args, k, None)
        setattr(args, k, v)
    try:
        run_action(act, root, args)
    except SystemExit as e:
        if e.code not in (None, 0):
            log(str(e.code))
    except KeyboardInterrupt:
        log("\nInterrumpido.")
    finally:
        for k, v in saved.items():
            setattr(args, k, v)
    if _RESTART:
        print("\nEl script se actualizo: vuelve a ejecutarlo para usar la version nueva.")
        return False
    if act != "log":
        try:
            input("\nEnter para continuar...")
        except (EOFError, KeyboardInterrupt):
            print()
            return False
    return True


def run_menu(title, items, root, args, top=False):
    """Un nivel del menu. Devuelve False si hay que salir de todo."""
    keys = {it[0]: it for it in items}
    while True:
        print(f"\n===== {title} =====" + (f"   Raiz: {root}" if top else ""))
        for it in items:
            print(f" {it[0]}) {it[1]}" + ("   >" if isinstance(it[2], list) else ""))
        print(f" d) Dry-run (simular, no cambia nada): {'SI' if args.dry_run else 'no'}")
        print(" 0) " + ("Salir" if top else "Volver"))
        try:
            op = input("Opcion: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if op == "":
            continue
        if op in ("0", "q", "s", "salir", "volver"):
            return not top
        if op == "d":
            args.dry_run = not args.dry_run
            continue
        it = keys.get(op)
        if it is None:
            print("Opcion no valida.")
            continue
        if isinstance(it[2], list):
            if not run_menu(f"JD Update > {it[1].split('(')[0].strip()}", it[2], root, args):
                return False
        elif not _run_leaf(it[2], it[3] if len(it) > 3 else None, root, args):
            return False


def menu(root, args):
    run_menu("JD Update", MENU, root, args, top=True)


def detect_root():
    """Raiz de MiSTer (donde estan _Arcade y games).
    - Si el script vive en una carpeta llamada 'jd_update', la raiz es SU CARPETA PADRE (siempre).
    - Si no: su propia carpeta si ya tiene _Arcade/games; si no, la padre si los tiene; si no, su carpeta."""
    if BASE_DIR.name.lower() == "jd_update":
        return BASE_DIR.parent
    for d in (BASE_DIR, BASE_DIR.parent):
        if (d / "_Arcade").is_dir() or (d / "games").is_dir():
            return d
    return BASE_DIR


def main():
    ap = argparse.ArgumentParser(
        description="Actualizar, organizar MRA, revisar y copiar ROMs (sin accion: menu)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="acciones: " + ", ".join(ACCIONES))
    ap.add_argument("accion", nargs="?", choices=ACCIONES, help="accion a ejecutar; sin ella abre el menu")
    ap.add_argument("--root", help="raiz de trabajo (por defecto: la carpeta del script)")
    ap.add_argument("--list", action="append", default=[], metavar="ARCHIVO",
                    help="otro jd_update.list (o carpeta con jd_update*.list); por defecto <raiz>/jd_update.list")
    ap.add_argument("--all-lists", action="store_true", help="incluir todos los jd_update*.list de la raiz")
    ap.add_argument("--only", action="append", default=[], help="update: solo DBs cuyo nombre contenga este texto")
    ap.add_argument("--workers", type=int, default=8, help="update: descargas en paralelo (def. 8)")
    ap.add_argument("--no-hash-cache", action="store_true", help=f"update: no usar {HASH_CACHE_FILE} (lento)")
    ap.add_argument("--fast", action="store_true", help="update: comparar solo tamano (no MD5) en archivos existentes")
    ap.add_argument("--mode", choices=MODES, default="symlink",
                    help="organize: symlink (defecto), hardlink, copy (exFAT/FAT32), move")
    ap.add_argument("--no-extra", action="store_true",
                    help="organize: no crear _DEBUT, _UPDATED, _PAST ... ni _A-Z (solo _CORE)")
    ap.add_argument("--scan", action="append", default=None, metavar="CARPETA", help="carpeta donde buscar MRA (repetible)")
    ap.add_argument("--rom-dir", action="append", default=None, metavar="CARPETA",
                    help=f"carpetas de zips de ROMs (la primera es la de nombres simples). Def.: {', '.join(DEFAULT_ROM_DIRS)}")
    ap.add_argument("--cores-dir", default=DEFAULT_CORES_DIR, help=f"carpeta de los .rbf (def. {DEFAULT_CORES_DIR})")
    ap.add_argument("--source", default=SOURCE_DIR, help="carpeta fuente de ROMs merged (def. la fija del script)")
    ap.add_argument("--fetch-only", choices=["zips", "disks"], help="fetch: solo zips de ROMs o solo discos")
    ap.add_argument("--no-convert", action="store_true", help="fetch: no convertir CHD (los deja para despues)")
    ap.add_argument("--rescan", action="store_true", help="regenerar jd_roms.db antes de copiar")
    ap.add_argument("--find", metavar="TEXTO", help="torrents: buscar un archivo/juego dentro de los .torrent")
    ap.add_argument("--section", metavar="N|TEXTO", help="log: mostrar una seccion (numero o parte del titulo)")
    ap.add_argument("--grep", metavar="TEXTO", help="log: buscar texto en el log (sin distinguir mayusculas)")
    ap.add_argument("--gh-repo", default=GITHUB_REPO, help=f"github: repositorio (def. {GITHUB_REPO}) o carpeta local")
    ap.add_argument("--gh-branch", default=GITHUB_BRANCH, help=f"github: rama (def. {GITHUB_BRANCH})")
    ap.add_argument("--gh-path", default=GITHUB_PATH, help=f"github: carpeta del repo (def. {GITHUB_PATH})")
    ap.add_argument("--exclude", action="append", default=[], metavar="ZIP", help=f"zip a ignorar (ademas de {EXCLUDE_FILE})")
    ap.add_argument("--no-cache", action="store_true", help=f"no usar {CACHE_FILE}")
    ap.add_argument("--log", default=CHECK_LOG, help=f"nombre del log de revision (def. {CHECK_LOG})")
    ap.add_argument("--dry-run", action="store_true", help="no descarga, no copia, no crea nada")
    ap.add_argument("--yes", "-y", action="store_true", help="no pedir confirmaciones")
    args = ap.parse_args()
    args.ini = args.list

    root = Path(args.root).expanduser().resolve() if args.root else detect_root()
    if not root.is_dir():
        sys.exit(f"No existe la carpeta: {root}")
    if not args.accion:
        if sys.stdin and sys.stdin.isatty():
            return menu(root, args)
        sys.exit("Indica una accion (" + ", ".join(ACCIONES) + ") o ejecuta en una terminal para ver el menu.")
    failed = run_action(args.accion, root, args)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
