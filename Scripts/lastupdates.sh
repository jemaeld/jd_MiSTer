#!/bin/bash

# Directorio base de Arcade en MiSTer FPGA
ARCADE_DIR="/media/fat/_Arcade"
LAST_UPDATES_DIR="$ARCADE_DIR/_@Lastupdates"

echo "Buscando archivos MRA agregados en los últimos 7 días..."

# Cambiar al directorio Arcade o salir si no existe
cd "$ARCADE_DIR" || { echo "Error: No se encuentra el directorio $ARCADE_DIR"; exit 1; }

# Preparar el directorio _@Lastupdates (crearlo y limpiar symlinks anteriores)
mkdir -p "$LAST_UPDATES_DIR"
rm -f "$LAST_UPDATES_DIR"/*.mra

# Buscar archivos físicos .mra (excluyendo subcarpetas) creados/modificados en los últimos 7 días
find . -maxdepth 1 -type f -name "*.mra" -mtime -7 | while read -r file; do
    # Eliminar el prefijo "./" que arroja el comando find
    filename="${file#./}"
    
    # Crear enlace simbólico relativo
    ln -s "../$filename" "$LAST_UPDATES_DIR/$filename"
done

# Verificar el total de accesos directos creados en la carpeta
COUNT=$(ls -1 "$LAST_UPDATES_DIR"/*.mra 2>/dev/null | wc -l)

echo "¡Listo! Se crearon $COUNT accesos directos en la carpeta _@Lastupdates."
