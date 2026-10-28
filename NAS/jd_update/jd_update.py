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
RE_DATED = re.compile(r"_(\d{6,8}[a-z]?)$", re.I)
# ------------------------------------------------------------- PARSEO ----
def attr(attrs, key):
    m = re.search(rf"""\b{key}\s*=\s*["']([^"']*)["']""", attrs, re.I)
    return m.group(1) if m else None


def split_zips(value):
    return [n.strip() for n in value.split("|") if n.strip()] if value else []


def parse_mra(raw):
    """-> dict(rbf, setname, zips[], roms[{index, zips[], parts[{name, crc, zips[]}]}]).
    Regex a proposito: varias MRA no son XML valido."""
    text = RE_COMMENT.sub("", raw.decode("utf-8", errors="replace"))  # los comentarios pueden nombrar tags
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
    return {"rbf": rbf, "setname": setname, "zips": zips, "roms": roms}


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


def write_report(root, R, args, source_idx, copy_rep, rb):
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
    log(f"Log: {STATE_DIR / args.log}")


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
    """A-Z segun la primera letra del nombre (sin acentos); cualquier otra cosa -> '#'."""
    n = unicodedata.normalize("NFD", name.strip())
    c = n[:1]
    return c.upper() if c.isascii() and c.isalpha() else "#"


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
            label = run["date"][:16]
            if label in used:
                label = run["date"]
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
    write_report(root, R, args, idx, copy_rep, rb)
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
    elif act == "copy":
        cmd_check(root, args, copy=True)
    elif act == "rebuild":
        cmd_check(root, args, rebuild=True)
    elif act == "rescan":
        cmd_rescan(root, args)
    elif act == "all":
        log("\n##### 1/3  ACTUALIZAR #####")
        failed = run_update(args, root)["failed"]
        log("\n##### 2/3  ORGANIZAR MRA POR CORE #####")
        cmd_organize(root, args)
        log("\n##### 3/3  REVISAR Y COPIAR ROMS #####")
        cmd_check(root, args, copy=True)
    elif act == "github":
        cmd_github(root, args)
    elif act == "github-lists":
        cmd_github(root, args, only_lists=True)
    elif act == "undo-organize":
        undo(root)
    elif act == "undo-rebuild":
        undo_rebuild(root)
    return failed


ACCIONES = ["update", "organize", "check", "copy", "rebuild", "rescan", "all", "github", "github-lists",
            "undo-organize", "undo-rebuild"]
MENU_ITEMS = [
    ("1", "update", "Actualizar               (descarga las DBs de jd_update.list)"),
    ("2", "organize", "Organizar MRA            (_CORE, _A-Z, _DEBUT, _UPDATED...)"),
    ("3", "check", "Revisar                  (solo lee; genera JD_Check.log)"),
    ("4", "copy", "Revisar y COPIAR ROMs que faltan desde la fuente"),
    ("5", "rebuild", "Revisar y reconstruir zips solucionables (con lo que ya tienes)"),
    ("6", "rescan", "Reescanear la fuente de ROMs (regenera jd_roms.db)"),
    ("7", "all", "TODO: 1 + 2 + 4"),
    ("8", "undo-organize", "Deshacer el organizador"),
    ("9", "undo-rebuild", "Deshacer los zips creados por la opcion 5"),
    ("g", "github", "Actualizar el script y las listas desde GitHub"),
    ("l", "github-lists", "Actualizar solo las listas (jd_update.list, jd_exclude.list) desde GitHub"),
]


def menu(root, args):
    keys = {k: act for k, act, _t in MENU_ITEMS}
    while True:
        print(f"\n===== JD Update =====   Raiz: {root}")
        for k, _a, t in MENU_ITEMS:
            print(f" {k}) {t}")
        print(f" d) Dry-run (simular, no cambia nada): {'SI' if args.dry_run else 'no'}")
        print(" 0) Salir")
        try:
            op = input("Opcion: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if op in ("0", "q", "s", "salir", ""):
            if op == "":
                continue
            return
        if op == "d":
            args.dry_run = not args.dry_run
            continue
        if op not in keys:
            print("Opcion no valida.")
            continue
        try:
            run_action(keys[op], root, args)
        except SystemExit as e:
            if e.code not in (None, 0):
                log(str(e.code))
        except KeyboardInterrupt:
            log("\nInterrumpido.")
        if _RESTART:
            print("\nEl script se actualizo: vuelve a ejecutarlo para usar la version nueva.")
            return
        try:
            input("\nEnter para volver al menu...")
        except (EOFError, KeyboardInterrupt):
            print()
            return


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
    ap.add_argument("--rescan", action="store_true", help="regenerar jd_roms.db antes de copiar")
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
