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
python3 jd_update.py          # menú numerado
python3 jd_update.py all      # actualizar + organizar + revisar y copiar ROMs
```

- **Actualizar:** descarga las DBs de `jd_update.list` (solo lo que falta o cambió, con MD5).
- **Organizar:** crea en `_@JD_Organizer` enlaces simbólicos: `_CORE` (por core), `_DEBUT`, `_PAST DEBUT`, `_UPDATED`, `_PAST UPDATED` y `_A-Z`. Una MRA cuenta como actualizada si cambió ella o el `.rbf` de su core.
- **Revisar:** replica el cargador del MiSTer, así que una MRA solo es problema si de verdad no cargaría (los sets merged no cuentan como faltantes). Escribe `JD_Check.log`.
- **Copiar:** copia desde la carpeta de ROMs MAME merged del NAS los zips que las MRA necesitan, usando un índice en texto plano (`jd_roms.db`, se regenera con `rescan`).

Ajusta `SOURCE_DIR` al inicio del script con la ruta de tus ROMs.

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
