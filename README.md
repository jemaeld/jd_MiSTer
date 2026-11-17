# jd_MiSTer

Base de datos personal para MiSTer FPGA, compatible con *Downloader* y *Update All*. Aquí van mis scripts y archivos personales, además de cores y archivos que no aparecen en las distribuciones oficiales.

## Instalación

Agrega esto a `downloader.ini` (o a la configuración de Update All) en tu MiSTer:

```ini
[jemaeld/jd_MiSTer]
db_url = https://raw.githubusercontent.com/jemaeld/jd_MiSTer/db/db.json.zip
```

Solo hace falta hacerlo una vez. Después, cada vez que corras *downloader* o *update_all* se instalarán los archivos actualizados de este repositorio.

## Scripts

Se instalan en `/media/fat/Scripts/` y aparecen en el menú **Scripts** de MiSTer.

| Script | Qué hace |
|---|---|
| `missingRFB.sh` | Revisa los `.mra` de `_Arcade` y comprueba que exista el `.rbf` que piden en `_Arcade/cores`. Si el core existe como `Arcade-X` pero el MRA pide `X`, corrige la etiqueta. Lo que falta se guarda en `/media/fat/missingRFB.txt`. Con `--dry-run` solo avisa, sin modificar nada. |
| `missingarcaderoms.sh` | Lee el `<setname>` de cada MRA y comprueba que exista su `.zip` en `games/mame`. Lo que falta se guarda en `/media/fat/missingarcaderoms.txt`. |
| `lastupdates.sh` | Crea en `_Arcade/_@Lastupdates` enlaces simbólicos a los MRA agregados o modificados en los últimos 7 días. |

> `missingRFB.sh` modifica los archivos `.mra` en el modo normal. Si quieres ver qué cambiaría antes de aplicarlo, ejecútalo por SSH con `./missingRFB.sh --dry-run`.

## MRAs extras

La carpeta `_Arcade/@JD_Extras/` se instala en `/media/fat/_Arcade/@JD_Extras/` y contiene MRAs de hacks, betas y variantes que no vienen en las distribuciones oficiales:

- Street Fighter II: `sf2mix`, `sf2prime`, Champion Re-Edit y Special Champion Edition Arcade Sr Street
- Mortal Kombat: Ultimate Mortal Kombat 3 PLUS (Beta 2) y Mortal Kombat II PLUS (Beta 2)
- Namco System 12: Soul Calibur (World, US ALT y Japan JP ALT), Fighting Layer (Asia y Japan ALT) y Ehrgeiz

Estos MRAs necesitan que tengas en `games/mame` los `.zip` de ROM correspondientes y el core indicado en cada MRA. Los scripts solo revisan `_Arcade` (no sus subcarpetas), así que `missingRFB.sh` y `missingarcaderoms.sh` no analizan esta carpeta.

## NAS: jd_update

`NAS/jd_update/` tiene mi gestor de ROMs y MRA, pensado para correr por SSH en el NAS (Python 3.8+, sin dependencias). No es un script del MiSTer: vive en la carpeta `jd_update/` dentro de la raíz de MiSTer (donde están `_Arcade` y `games`).

```
python3 jd_update.py          # menú numerado con submenús: 4 ROMs, 5 Torrents y discos, 6 Logs, 7 Mantenimiento
python3 jd_update.py all      # actualizar + organizar + revisar y copiar ROMs
```

- **Actualizar:** descarga las DBs de `jd_update.list` (solo lo que falta o cambió, con MD5).
- **Organizar:** crea en `_@JD_Organizer` enlaces simbólicos: `_CORE` (por core), `_DEBUT`, `_PAST DEBUT`, `_UPDATED`, `_PAST UPDATED` y `_A-Z`. Una MRA cuenta como actualizada si cambió ella o el `.rbf` de su core.
- **Revisar:** replica el cargador del MiSTer, así que una MRA solo es problema si de verdad no cargaría (los sets merged no cuentan como faltantes). Escribe `JD_Check.log`.
- **Copiar:** copia desde la carpeta de ROMs MAME merged del NAS los zips que las MRA necesitan, usando un índice en texto plano (`jd_roms.db`, se regenera con `rescan`).

Ajusta `SOURCE_DIR` al inicio del script con la ruta de tus ROMs.

**Torrents (solo planea, todavía no descarga):** pon los `.torrent` de Pleasuredome (MAME / HBMAME ROMs *merged*) en `jd_update/torrents/` y, opcionalmente, sus datfiles en `jd_update/dats/`. `python3 jd_update.py torrents` (menú 5 → 3) calcula qué zips faltan y cuántos MB serían, sin bajar el torrent completo; con los datfiles resuelve el zip del padre cuando la MRA pide el de un clon. `torrents --find texto` busca cualquier archivo dentro de los torrents (útil para CHD). Resultado en `JD_Torrents.log`.

**Discos (HDD / IMG / CHD), solo revisa:** `python3 jd_update.py disks` (menú 5 → 2) detecta los discos de tres formas: **FIJO** (la MRA trae `<file ... .hdd/.img/.chd>` con destino exacto en `games/`), **OSD** (la MRA solo nombra el `.chd` en un comentario, p. ej. Pop'n Music 8, y el core lo pide por las ranuras del OSD) y **POSIBLE** (el set de MAME tiene CHD en el torrent pero la MRA no lo menciona; para revisar a mano). Indica cuáles ya existen, cuáles salen de los `.torrent` (con tamaño) y cuáles no tienen fuente. Resultado en `JD_Discos.log` y `JD_Discos.json`. Todavía no descarga ni convierte.

Para que `Discos` ignore lo que ya tienes resuelto, el script crea `jd_update/jd_disks_ignore.list` (solo del NAS, no se baja de GitHub): `core:NOMBRE` ignora un core entero y `NOMBRE` ignora un set o disco (admite `*`). Para los discos de OSD sugiere `games/<set de MAME>/<archivo>` como destino. Además, si tu Deluge tiene el plugin Label, `Magnets` etiqueta con `mister` (configurable en `jd_deluge.ini`, `label =`) los torrents de la lista que ya estén en tu Deluge.

**Magnets → `.torrent` (solo metadatos):** pon un magnet por línea en `jd_update/magnets.list` y corre `python3 jd_update.py magnets` (menú 5 → 1). Le pide a tu Deluge (Web UI) la lista de archivos de cada magnet con `core.prefetch_magnet_metadata` —sin agregarlo ni bajar datos—, verifica el hash y guarda el `.torrent` en `jd_update/torrents/`. La URL y contraseña del Web UI se piden una vez y quedan en `jd_update/jd_deluge.ini` (permisos 600, solo en el NAS; no se sube a GitHub).

Para leer `JD_Check.log` sin salir de la terminal: menú 6 (Ver logs), o `python3 jd_update.py log` (`--section N` muestra una sección, `--grep texto` busca en todo el log).

Para traer de aquí el script y las listas (`jd_update.list`, `jd_exclude.list`) a tu NAS: menú 7 → 1, o `python3 jd_update.py github` (`github-lists` solo trae las listas). Muestra qué cambia, pide confirmación y guarda copia `.bak`.

## Configuración

`Backups/MiSTer.ini` es mi `MiSTer.ini` (salida HDMI a 2560x1440, HDR, integer scale y el `main` de Zaparoo). Se guarda aquí como respaldo y no en la raíz a propósito: así Downloader no sobrescribe el `MiSTer.ini` activo en `/media/fat/`. Para usarlo, cópialo a `/media/fat/MiSTer.ini`.

## Estructura del repositorio

```
Scripts/              Scripts para MiSTer (se instalan en /media/fat/Scripts)
_Arcade/@JD_Extras/   MRAs extras (se instalan en /media/fat/_Arcade/@JD_Extras)
Backups/              Respaldo de configuración (MiSTer.ini)
external_files.csv    Archivos externos que se descargan desde otras URLs
.github/workflows/    Workflow que genera db.json.zip en la rama "db"
```

## Archivos externos

`external_files.csv` permite incluir archivos alojados en otros repositorios. Formato:

```
Path in MiSTer, URL, Size in bytes (optional but recommended), MD5 Hash (optional but recommended), Filter Terms (optional)
```

## Cómo se genera la base de datos

Cada push a `main` ejecuta el workflow `Build Custom Database`, que genera `db.json.zip` en la rama `db`. Basado en la plantilla [Downloader_DB-Template_MiSTer](https://github.com/theypsilon/Downloader_DB-Template_MiSTer).
