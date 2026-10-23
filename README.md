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

## Estructura del repositorio

```
Scripts/              Scripts para MiSTer (se instalan en /media/fat/Scripts)
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
