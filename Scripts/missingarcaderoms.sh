#!/bin/bash

ARCADE_DIR="/media/fat/_Arcade"
MAME_DIR="/media/fat/games/mame"
LOG_FILE="/media/fat/missingarcaderoms.txt"

RED='\033[0;31m'
NC='\033[0m' # Sin color

# Limpiar el log anterior si existe
> "$LOG_FILE"

echo "Escaneando MRA en $ARCADE_DIR en busca de ROMs faltantes..."

# Buscar MRAs en el directorio principal
find "$ARCADE_DIR" -maxdepth 1 -type f -name "*.mra" | while read -r mra_file; do
    mra_basename=$(basename "$mra_file")

    # Extraer el contenido de <setname>xxx</setname>, quitando espacios en blanco o saltos de línea (\r)
    setname=$(grep -io '<setname>[^<]*</setname>' "$mra_file" | head -n 1 | sed -e 's/<[^>]*>//g' | tr -d '\r' | xargs)
    
    # Saltar si no se encontró la etiqueta <setname> (algunos MRA homebrew no la usan)
    if [ -z "$setname" ]; then
        continue
    fi

    # Verificar si el archivo .zip existe en la carpeta de MAME
    if [ ! -f "$MAME_DIR/${setname}.zip" ]; then
        # Imprimir en pantalla en rojo
        echo -e "${RED}Falta ROM: ${setname}.zip (MRA: $mra_basename)${NC}"
        # Guardar en el log
        echo "${setname}.zip" >> "$LOG_FILE"
    fi
done

# Si el log tiene contenido, eliminar duplicados (varios MRA pueden pedir el mismo parent ROM)
if [ -s "$LOG_FILE" ]; then
    sort -u "$LOG_FILE" -o "$LOG_FILE"
    echo -e "\nLista única de ROMs faltantes guardada en: $LOG_FILE"
else
    echo -e "\nTodas las ROMs principales solicitadas están presentes."
    rm -f "$LOG_FILE"
fi
