#!/bin/bash

ARCADE_DIR="/media/fat/_Arcade"
CORES_DIR="/media/fat/_Arcade/cores"
LOG_FILE="/media/fat/missingRFB.txt"

# Definición de colores
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # Sin color

# Modo simulación: ./missingRFB.sh --dry-run
# Avisa de los MRA que se ajustarían, pero NO los modifica.
DRY_RUN=0
if [ "$1" = "--dry-run" ] || [ "$1" = "-n" ]; then
    DRY_RUN=1
fi

# Limpiar el log anterior si existe
> "$LOG_FILE"

if [ "$DRY_RUN" -eq 1 ]; then
    echo "MODO SIMULACIÓN (--dry-run): no se modificará ningún MRA."
fi
echo "Escaneando archivos MRA en $ARCADE_DIR..."

# Buscar MRAs solo en el directorio principal
find "$ARCADE_DIR" -maxdepth 1 -type f -name "*.mra" | while read -r mra_file; do
    mra_basename=$(basename "$mra_file")

    # Extraer la etiqueta rbf EXACTA con su contenido (ej: <rbf>SKNS</rbf> o <rbf>SKNS.rbf</rbf>)
    exact_tag=$(grep -io '<rbf>[^<]*</rbf>' "$mra_file" | head -n 1)
    
    # Saltar si no se encontró la etiqueta
    if [ -z "$exact_tag" ]; then
        continue
    fi

    # Limpiar las etiquetas para obtener solo el nombre y quitar el .rbf si existe
    exact_rbf=$(echo "$exact_tag" | sed -e 's/<[^>]*>//g')
    clean_rbf=$(echo "$exact_rbf" | sed 's/\.[rR][bB][fF]$//')

    # Evaluar si el MRA ya está pidiendo el archivo con "Arcade-"
    case "$clean_rbf" in
        Arcade-*|arcade-*|ARCADE-*)
            # El MRA ya tiene el prefijo. Buscar si el core existe (exacto o con fecha)
            core_found=$(find "$CORES_DIR" -maxdepth 1 \( -iname "${clean_rbf}.rbf" -o -iname "${clean_rbf}_*.rbf" \) 2>/dev/null | head -n 1)
            if [ -z "$core_found" ]; then
                echo -e "${RED}Falta RBF: $clean_rbf (MRA: $mra_basename)${NC}"
                echo "$clean_rbf" >> "$LOG_FILE"
            fi
            ;;
        *)
            # El MRA NO tiene el prefijo. Verificamos si existe en la carpeta de cores CON o SIN el prefijo
            core_arcade=$(find "$CORES_DIR" -maxdepth 1 \( -iname "Arcade-${clean_rbf}.rbf" -o -iname "Arcade-${clean_rbf}_*.rbf" \) 2>/dev/null | head -n 1)
            core_normal=$(find "$CORES_DIR" -maxdepth 1 \( -iname "${clean_rbf}.rbf" -o -iname "${clean_rbf}_*.rbf" \) 2>/dev/null | head -n 1)

            if [ -n "$core_arcade" ]; then
                # CONDICIÓN CUMPLIDA: El core real tiene "Arcade-" pero el MRA no.
                # Reemplazamos la etiqueta en el MRA de forma segura
                if [ "$DRY_RUN" -eq 1 ]; then
                    echo -e "${YELLOW}[Simulación] Se modificaría: $mra_basename (RBF -> Arcade-${clean_rbf})${NC}"
                else
                    sed -i "s|${exact_tag}|<rbf>Arcade-${clean_rbf}</rbf>|g" "$mra_file"
                    # Mostrar en pantalla el aviso de modificación en color amarillo
                    echo -e "${YELLOW}MRA Modificado: $mra_basename (Se ajustó el RBF a Arcade-${clean_rbf})${NC}"
                fi
                
            elif [ -z "$core_normal" ]; then
                # No se encontró ni la versión normal ni la versión "Arcade-". Faltante real.
                echo -e "${RED}Falta RBF: $clean_rbf (MRA: $mra_basename)${NC}"
                echo "$clean_rbf" >> "$LOG_FILE"
            fi
            ;;
    esac
done

# Si el log tiene contenido, eliminar duplicados
if [ -s "$LOG_FILE" ]; then
    sort -u "$LOG_FILE" -o "$LOG_FILE"
    echo -e "\nLista única de cores faltantes guardada en: $LOG_FILE"
else
    echo "Todos los cores requeridos están presentes."
    rm -f "$LOG_FILE"
fi
