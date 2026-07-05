"""
vcf_to_csv.py
-------------
Convierte un archivo de contactos .vcf (vCard, exportado del celular/Gmail/
iCloud) al CSV (telefono,nombre) que usa monitor.py para cruzar el numero
del remitente de WhatsApp con su nombre real.

Uso:
    python vcf_to_csv.py contactos.vcf
    python vcf_to_csv.py contactos.vcf -o contactos.csv --pais 51

Si un contacto tiene varios telefonos, se genera una fila por cada uno
(mismo nombre), porque un mensaje de WhatsApp llega desde un numero
especifico y no sabemos de antemano cual de los telefonos del contacto es.
"""

import argparse
import csv
import quopri
import re
import sys


def unfold_lines(raw_text):
    """
    El formato vCard permite 'doblar' lineas largas: una linea que continua
    la anterior empieza con un espacio o tabulador. Aqui se vuelven a unir
    en una sola linea logica por propiedad.
    """
    raw_text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    unidas = []
    for linea in raw_text.split("\n"):
        if linea.startswith((" ", "\t")) and unidas:
            unidas[-1] += linea[1:]
        else:
            unidas.append(linea)
    return unidas


def decodificar_valor(valor, params):
    """Decodifica el valor si la propiedad viene en QUOTED-PRINTABLE (comun en vCard 2.1)."""
    if any(p.upper() == "ENCODING=QUOTED-PRINTABLE" for p in params):
        try:
            return quopri.decodestring(valor.encode("utf-8")).decode("utf-8", errors="replace")
        except Exception:
            return valor
    return valor


def limpiar_telefono(numero, pais_default=None):
    """Deja solo digitos y, si el numero parece local (sin codigo de pais), le antepone uno."""
    digitos = re.sub(r"\D", "", numero)
    if not digitos:
        return ""
    if pais_default and not digitos.startswith(pais_default):
        # celular local peruano tipico: 9 digitos empezando en 9
        if len(digitos) == 9 and digitos.startswith("9"):
            digitos = pais_default + digitos
    return digitos


def parsear_vcf(ruta, pais_default=None):
    with open(ruta, "r", encoding="utf-8", errors="replace") as f:
        contenido = f.read()

    contactos = []
    nombre = None
    telefonos = []

    for linea in unfold_lines(contenido):
        linea = linea.strip()
        if not linea:
            continue

        if linea.upper() == "BEGIN:VCARD":
            nombre = None
            telefonos = []
            continue

        if linea.upper() == "END:VCARD":
            if nombre or telefonos:
                for tel in telefonos or [""]:
                    if tel:
                        contactos.append({"telefono": tel, "nombre": nombre or ""})
            continue

        if ":" not in linea:
            continue

        clave, valor = linea.split(":", 1)
        partes_clave = clave.split(";")
        # Apple/iCloud antepone "item1.", "item2." etc a las propiedades
        propiedad = re.sub(r"^ITEM\d+\.", "", partes_clave[0].upper())
        params = partes_clave[1:]

        valor = decodificar_valor(valor, params)

        if propiedad == "FN" and not nombre:
            nombre = valor.strip()
        elif propiedad == "N" and not nombre:
            # N: Apellido;Nombre;SegundoNombre;Prefijo;Sufijo
            campos = (valor.split(";") + ["", "", "", "", ""])[:5]
            apellido, nombre_prop = campos[0].strip(), campos[1].strip()
            nombre = " ".join(p for p in (nombre_prop, apellido) if p)
        elif propiedad == "TEL":
            tel = limpiar_telefono(valor, pais_default)
            if tel:
                telefonos.append(tel)

    return contactos


def main():
    ap = argparse.ArgumentParser(description="Convierte un .vcf de contactos a CSV (telefono,nombre)")
    ap.add_argument("vcf", help="archivo .vcf de entrada")
    ap.add_argument("-o", "--output", default="contactos.csv", help="archivo CSV de salida (default: contactos.csv)")
    ap.add_argument(
        "--pais", default="51",
        help="codigo de pais a anteponer a celulares locales de 9 digitos (default: 51, Peru). Usa '' para desactivar.",
    )
    args = ap.parse_args()

    pais_default = args.pais or None

    contactos = parsear_vcf(args.vcf, pais_default)

    if not contactos:
        print("[WARN] No se encontraron contactos con telefono en el archivo.")
        sys.exit(1)

    # si el mismo telefono aparece repetido (contacto duplicado en el vcf), nos quedamos con el primero
    vistos = {}
    for c in contactos:
        vistos.setdefault(c["telefono"], c["nombre"])

    with open(args.output, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["telefono", "nombre"])
        for telefono, nombre in vistos.items():
            writer.writerow([telefono, nombre])

    print(f"[OK] {len(vistos)} contacto(s) escritos en {args.output}")


if __name__ == "__main__":
    main()
