#!/usr/bin/env python3
"""
extraer_factura.py — Extrae datos estructurados de facturas AFIP/ARCA en PDF
(Factura A/B/C/E/M, Notas de Crédito/Débito, Recibos) y los imprime como JSON.

Trabaja igual que la suite Escriba:
  * Modo API (por defecto): manda el PDF a Escriba (POST {ESCRIBA_URL}/api/convert),
    que aplica PyMuPDF con rotación, OCR opcional y anonimización PII.
  * Modo local (fallback automático, o forzado con --local): extrae el texto con
    PyMuPDF directamente, replicando app/pdf_extract.py de Escriba, y además arma
    renglones "visuales" con coordenadas (etiqueta→valor en la misma altura).

Sobre el texto obtenido parsea los campos con regex tolerantes (tildes,
mayúsculas, espacios) + asociación etiqueta→valor.

Salida: un único objeto JSON por STDOUT (UTF-8). Errores: JSON por STDERR.
Códigos de salida: 0 OK · 1 error inesperado · 2 entrada inválida ·
                   3 no se pudo extraer texto / API forzada no disponible.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
from datetime import date
from pathlib import Path

DEFAULT_URL = "http://localhost:8000"
DEFAULT_ENV_FILE = "/home/ubuntu/escriba/.env"
DEFAULT_TIMEOUT = 120
ANON_MODOS = ("typed", "anon", "seudo", "mask", "hash")


class ErrorEntrada(Exception):
    """PDF ausente/ilegible/no-PDF → exit 2."""


class ErrorExtraccion(Exception):
    """No se obtuvo texto o la API forzada falló → exit 3."""


# ═══════════════════════════════════════════════════════════════════════════
# 1. Obtención del texto
# ═══════════════════════════════════════════════════════════════════════════

def leer_api_key() -> str | None:
    """API key: env ESCRIBA_API_KEY o la línea API_TOKEN= del .env de Escriba.
    Nunca se imprime ni se loguea."""
    key = os.environ.get("ESCRIBA_API_KEY", "").strip()
    if key:
        return key
    env_file = os.environ.get("ESCRIBA_ENV_FILE", DEFAULT_ENV_FILE)
    try:
        for linea in Path(env_file).read_text(encoding="utf-8").splitlines():
            linea = linea.strip()
            if linea.startswith("API_TOKEN="):
                return linea.split("=", 1)[1].strip().strip('"').strip("'") or None
    except OSError:
        pass
    return None


def _multipart(campos: dict, archivo: tuple[str, bytes]) -> tuple[bytes, str]:
    """Arma un cuerpo multipart/form-data con la stdlib (sin requests)."""
    limite = "----escriba" + uuid.uuid4().hex
    partes = []
    for nombre, valor in campos.items():
        partes.append(
            f'--{limite}\r\nContent-Disposition: form-data; name="{nombre}"\r\n\r\n{valor}\r\n'
            .encode("utf-8"))
    fname, data = archivo
    fname = fname.replace('"', "")
    partes.append(
        (f'--{limite}\r\nContent-Disposition: form-data; name="file"; filename="{fname}"\r\n'
         "Content-Type: application/pdf\r\n\r\n").encode("utf-8") + data + b"\r\n")
    partes.append(f"--{limite}--\r\n".encode())
    return b"".join(partes), f"multipart/form-data; boundary={limite}"


def convertir_api(pdf: bytes, nombre: str, url: str, timeout: int, ocr: bool,
                  anonimizar: str | None, lang: str | None, pages: str | None) -> dict:
    """Llama a POST {url}/api/convert. Devuelve el JSON de Escriba.
    Lanza ConnectionError (red/timeout/5xx) o PermissionError (401/403)."""
    key = leer_api_key()
    campos = {}
    if ocr:
        campos["ocr"] = "true"
    if anonimizar:
        campos["anonymize"] = anonimizar
    if lang:
        campos["lang"] = lang
    if pages:
        campos["pages"] = pages
    body, ctype = _multipart(campos, (nombre, pdf))
    headers = {"Content-Type": ctype, "Accept": "application/json",
               "User-Agent": "escriba-n8n/1.0"}
    if key:
        headers["X-API-Key"] = key
    req = urllib.request.Request(url.rstrip("/") + "/api/convert", data=body,
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detalle = e.read()[:300].decode("utf-8", "replace")
        if e.code in (401, 403):
            raise PermissionError(f"API rechazó la autenticación (HTTP {e.code}). "
                                  "Revisá ESCRIBA_API_KEY.") from None
        raise ConnectionError(f"HTTP {e.code}: {detalle}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ConnectionError(f"Escriba no responde en {url}: {getattr(e, 'reason', e)}") from None
    except json.JSONDecodeError:
        raise ConnectionError("La API devolvió una respuesta que no es JSON") from None


_COL_GAP = 45.0   # gap de x (pt) que marca salto de columna (igual que Escriba)


def extraer_local(pdf: bytes) -> dict:
    """Modo local con PyMuPDF. Devuelve:
      texto  → igual que pdf_extract.extract_pdf_text (orden de bloques, rotación OK)
      layout → renglones visuales (clúster por y, palabras ordenadas por x) con TAB
               en cada salto de columna; permite unir etiqueta y valor de la misma
               altura, como invoice_field_values() de Escriba."""
    try:
        import pymupdf
    except ImportError:
        raise ErrorExtraccion("Modo local requiere pymupdf: pip install -r requirements.txt")
    try:
        doc = pymupdf.open(stream=pdf, filetype="pdf")
    except Exception as e:
        raise ErrorEntrada(f"No se pudo abrir el PDF: {e}")
    try:
        textos, layout = [], []
        for page in doc:
            txt = page.get_text("text").strip()   # respeta /Rotate y orientación
            if txt:
                textos.append(txt)
            words = page.get_text("words")       # (x0,y0,x1,y1,palabra,bloque,línea,n)
            renglones = []
            for w in sorted(words, key=lambda w: (w[1], w[0])):
                if renglones and abs(w[1] - renglones[-1]["y"]) <= 1.8:
                    renglones[-1]["ws"].append(w)
                else:
                    renglones.append({"y": w[1], "ws": [w]})
            for r in renglones:
                ws = sorted(r["ws"], key=lambda w: w[0])
                out, prev_x1 = "", None
                for w in ws:
                    if prev_x1 is not None:
                        out += "\t" if w[0] - prev_x1 > _COL_GAP else " "
                    out += w[4]
                    prev_x1 = w[2]
                layout.append(out)
            layout.append("")  # separador de página
        return {"texto": "\n\n".join(textos).strip(), "layout": "\n".join(layout),
                "paginas": doc.page_count}
    finally:
        doc.close()


# ═══════════════════════════════════════════════════════════════════════════
# 2. Normalización de texto
# ═══════════════════════════════════════════════════════════════════════════

def plegar(s: str) -> str:
    """Minúsculas y sin tildes, CONSERVANDO la longitud (índices 1:1 con el
    original, para recortar valores del texto original)."""
    out = []
    for ch in s:
        base = unicodedata.normalize("NFD", ch)[0]
        base = base.lower()
        out.append(base if len(base) == 1 else ch.lower()[:1] or ch)
    return "".join(out).replace("º", "°")


def limpiar_markdown(md: str) -> str:
    """Quita la sintaxis Markdown que pueda devolver Escriba/MarkItDown. Las
    celdas de tablas (|) pasan a TAB, que el parser trata como salto de columna."""
    md = re.sub(r"<!--.*?-->", "", md, flags=re.S)
    lineas = []
    for ln in md.splitlines():
        if re.fullmatch(r"\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*", ln):
            continue                                   # separador de tabla
        ln = re.sub(r"^\s{0,3}#{1,6}\s+", "", ln)      # encabezados
        ln = ln.replace("**", "").replace("__", "")
        ln = re.sub(r"^\s*[-*+]\s+", "", ln)           # viñetas
        if "|" in ln:
            ln = "\t".join(c.strip() for c in ln.strip().strip("|").split("|"))
        lineas.append(ln.rstrip())
    return "\n".join(lineas).strip()


# ═══════════════════════════════════════════════════════════════════════════
# 3. Etiquetas AFIP y asociación etiqueta → valor
# ═══════════════════════════════════════════════════════════════════════════

# (clave, regex sobre texto plegado). El orden importa: las más largas/específicas
# primero (en la alternación gana la primera que matchea en cada posición).
_C = r"c\.?\s?a\.?\s?e\.?a?"
ETIQUETAS = [
    ("receptor_nombre", r"apellido y nombre\s*/\s*razon social|apellido y nombre|denominacion"),
    ("razon_social", r"razon social"),
    ("domicilio", r"domicilio comercial|domicilio fiscal|domicilio"),
    ("cond_venta", r"condicion de venta|cond\.? de venta"),
    ("cond_iva", r"condicion (?:frente al|ante el|ante|de) i\.?v\.?a\.?|cond\.? (?:frente al )?i\.?v\.?a\.?|"
                 r"responsabilidad (?:frente al )?iva|i\.?v\.?a\.?\s+(?:responsable(?:\s+inscripto)?|"
                 r"monotributo|exento|consumidor final|no responsable)"),
    ("inicio_act", r"fecha de inicio de actividad(?:es)?|inicio de actividad(?:es)?|inicio de act(?:ividad(?:es)?)?\.?"),
    ("ingresos_brutos", r"ingresos brutos|ing\.? brutos|ii\.?\s?bb\.?|iibb"),
    ("fecha_vto_cae", rf"fecha de vto\.? de(?:l)? {_C}|fecha (?:de )?venc(?:imiento|\.)? (?:de(?:l)? )?{_C}|"
                      rf"vto\.? (?:de(?:l)? )?{_C}|vencimiento (?:de(?:l)? )?{_C}"),
    ("fecha_vto_pago", r"fecha de vto\.? para el pago|fecha de vencimiento para el pago|vto\.? (?:para el )?pago"),
    ("fecha_emision", r"fecha de emision|fecha emision|f\.? emision|fecha(?=\s*:)"),
    ("periodo_desde", r"periodo facturado desde"),
    ("periodo_hasta", r"hasta(?=\s*:)"),
    ("punto_venta", r"punto de venta|pto\.? (?:de )?vta\.?|p\.?\s?de venta"),
    ("comp_nro", r"comp\.?\s?nro\.?|comprobante n(?:ro|°|o)\.?|nro\.? (?:de )?comprobante|"
                 r"numero de comprobante|comp\.? n°"),
    ("cae", rf"{_C}\s*(?:n(?:ro|°|o)\.?|numero)?"),
    ("cuit", r"c\.?u\.?i\.?t\.?(?:\s*/\s*c\.?u\.?i\.?l\.?)?(?:\s*/\s*dni)?(?:\s*n(?:ro|°|o)\.?)?"),
    ("cuil", r"c\.?u\.?i\.?l\.?"),
    ("dni", r"d\.?n\.?i\.?(?:\s*n(?:ro|°|o)\.?)?|documento"),
    ("moneda", r"moneda"),
    ("neto_no_gravado", r"importe neto no gravado|neto no gravado"),
    ("neto", r"importe neto gravado|neto gravado|importe neto|sub[\s-]*total(?!\s*c/)"),
    ("iva_alic", r"i\.?v\.?a\.?\s*\d{1,2}(?:[.,]\d{1,2})?\s*%"),
    ("iva_total", r"(?:(?:importe(?:\s+de)?|total)\s+)?i\.?v\.?a\.?(?:\s+inscripto)?(?=\s*:|\s|$)"),
    ("otros_tributos", r"importe otros tributos|otros tributos"),
    ("exento", r"importe exento"),
    ("total", r"importe total|total(?:\s+de)? factura|total(?=\s*:)|total a pagar"),
]
_RE_ETIQ = re.compile(
    "|".join(f"(?P<{k}>(?<![a-z0-9«_]){p}(?![a-z_»]))" for k, p in ETIQUETAS))


def _limpiar_valor(v: str) -> str:
    v = v.strip().lstrip(":").strip()
    v = re.sub(r"^\$\s*", "", v)          # "$ 1.234,56" → "1.234,56"
    v = v.rstrip(":").strip()
    return "" if v in ("$", "-", "–") else v


def escanear_etiquetas(texto: str) -> list[dict]:
    """Recorre el texto línea a línea y asocia cada etiqueta con su valor:
      * mismo renglón: lo que sigue a la etiqueta hasta la próxima etiqueta o
        un TAB (salto de columna);
      * si queda vacío, el valor va en el/los renglón(es) siguientes. Se usa una
        cola FIFO de etiquetas pendientes, así también se resuelve el caso
        "Punto de Venta:\\nComp. Nro:\\n00001\\n00000123" (etiquetas apiladas)."""
    regs, pendientes = [], []
    for nl, linea in enumerate(texto.splitlines()):
        if not linea.strip():
            continue
        pl = plegar(linea)
        matches = list(_RE_ETIQ.finditer(pl))
        if not matches:
            if pendientes:
                # una línea sin etiquetas puede traer varios valores separados por TAB
                celdas = [c for c in linea.split("\t") if c.strip()]
                for celda in celdas:
                    if not pendientes:
                        break
                    r = pendientes.pop(0)
                    r["valor"] = _limpiar_valor(celda)
                    r["linea_valor"] = nl
            continue
        hubo_valor = False
        nuevos_pend = []
        for i, m in enumerate(matches):
            fin = matches[i + 1].start() if i + 1 < len(matches) else len(linea)
            crudo = linea[m.end():fin]
            if "\t" in crudo.strip(" :$"):     # cortar en salto de columna
                crudo = crudo.lstrip(" :$\t").split("\t")[0]
            valor = _limpiar_valor(crudo)
            r = {"clave": m.lastgroup, "etiqueta": linea[m.start():m.end()],
                 "valor": valor or None, "linea": nl, "linea_valor": nl}
            regs.append(r)
            if valor:
                hubo_valor = True
            else:
                nuevos_pend.append(r)
        if hubo_valor:
            pendientes = []
        pendientes.extend(nuevos_pend)
        pendientes = pendientes[-6:]           # no arrastrar etiquetas viejas
    return regs


# ═══════════════════════════════════════════════════════════════════════════
# 4. Normalizadores de valores
# ═══════════════════════════════════════════════════════════════════════════

_RE_CUIT = re.compile(r"(?<!\d)(20|23|24|25|26|27|30|33|34)[-\s.]?(\d{8})[-\s.]?(\d)(?!\d)")
_RE_FECHA = re.compile(r"(?<!\d)(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4}|\d{2})(?!\d)")
_RE_FECHA_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_RE_MONTO = re.compile(r"-?\d[\d.,]*\d|-?\d")
_RE_COND_IVA_TXT = re.compile(
    r"(?i)\b(responsable inscripto|responsable monotributo|monotributo|"
    r"exento|consumidor final|no responsable)\b"
)


def cuit_valido(digitos: str) -> bool:
    """Dígito verificador módulo 11 de AFIP."""
    if not re.fullmatch(r"\d{11}", digitos):
        return False
    pesos = [5, 4, 3, 2, 7, 6, 5, 4, 3, 2]
    dv = 11 - sum(int(a) * b for a, b in zip(digitos, pesos)) % 11
    dv = {11: 0, 10: 9}.get(dv, dv)
    return dv == int(digitos[10])


def extraer_cuits_relajados(texto: str) -> list[str]:
    """Extrae CUITs tolerando OCR con separadores/espacios intermedios."""
    out: list[str] = []
    for m in re.finditer(r"(?<!\d)(20|23|24|25|26|27|30|33|34)(?!\d)", texto):
        tramo = texto[m.start():m.start() + 26]
        dig = re.sub(r"\D", "", tramo)
        if len(dig) < 11:
            continue
        cand = dig[:11]
        if not cand.startswith(m.group(1)):
            continue
        if cand not in out and cuit_valido(cand):
            out.append(cand)
    return out


def _normalizar_condicion_iva(valor: str | None) -> str | None:
    if not valor:
        return None
    v = " ".join(valor.strip().split())
    if re.fullmatch(r"[0-9.\- ]{4,}", v):
        return None
    pv = plegar(v)
    if "inscripto" in pv:
        return "RESPONSABLE INSCRIPTO"
    if "monotrib" in pv:
        return "MONOTRIBUTO"
    if "consumidor final" in pv:
        return "CONSUMIDOR FINAL"
    if "exento" in pv:
        return "EXENTO"
    if "no responsable" in pv:
        return "NO RESPONSABLE"
    return v


def _parece_domicilio_linea(linea: str) -> bool:
    if not linea:
        return False
    l = " ".join(linea.strip().split())
    if not l:
        return False
    if re.search(r"(?i)\b(cae|comprobante|importe|total|neto|iva|ctg|transporte|vencimiento|ingresos|cuit|cbu|cuenta|correo|e-mail|asiento|emp\b)\b", l):
        return False
    return bool(
        re.search(r"(?i)\b(calle|av\.?|avenida|ruta|pasaje|pje\.?|barrio|piso|depto|dpto)\b", l)
        or re.search(r"\(\d{4}\)\s*[A-ZÁÉÍÓÚÑ ]{3,}", l)
        or re.search(r"^\d{4}\s+[A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ ]{2,}$", l)
        or (re.search(r"\d", l) and re.search(r"[A-ZÁÉÍÓÚÑ]{3,}", l) and len(l) <= 80)
    )


def norm_id(valor: str | None) -> dict | None:
    """Normaliza un CUIT/CUIL/DNI. Si el valor está anonimizado («ID_1», <...>)
    lo devuelve tal cual en 'original'."""
    if not valor:
        return None
    m = _RE_CUIT.search(valor)
    if m:
        dig = "".join(m.groups())
        return {"tipo": "CUIT", "numero": f"{dig[:2]}-{dig[2:10]}-{dig[10]}",
                "digitos": dig, "valido": cuit_valido(dig), "original": valor}
    m = re.search(r"(?<!\d)(\d{1,2}\.?\d{3}\.?\d{3})(?!\d)", valor)
    if m:
        dig = re.sub(r"\D", "", m.group(1))
        return {"tipo": "DNI", "numero": dig, "digitos": dig, "valido": 7 <= len(dig) <= 8,
                "original": valor}
    return {"tipo": None, "numero": None, "digitos": None, "valido": None, "original": valor}


def norm_fecha(valor: str | None) -> dict | None:
    """dd/mm/aaaa (o con - .) o aaaa-mm-dd → {'iso': 'AAAA-MM-DD', 'original': ...}."""
    if not valor:
        return None
    iso = None
    m = _RE_FECHA_ISO.search(valor)
    try:
        if m:
            iso = date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        else:
            m = _RE_FECHA.search(valor)
            if m:
                a = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
                iso = date(a, int(m[2]), int(m[1])).isoformat()
    except ValueError:
        iso = None
    orig = m.group(0) if m else valor.strip()
    return {"iso": iso, "original": orig}


def parse_monto(valor: str | None) -> float | None:
    """Parsea montos argentinos: '1.234,56' → 1234.56; también '1,234.56',
    '1234.56', '$ 121.000', '-50,00'."""
    if not valor:
        return None
    m = _RE_MONTO.search(valor.replace(" ", ""))
    if not m:
        return None
    s = m.group(0)
    neg = s.startswith("-")
    s = s.lstrip("-")
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        mil = "." if dec == "," else ","
        s = s.replace(mil, "").replace(dec, ".")
    elif "," in s:
        partes = s.split(",")
        if len(partes) == 2 and len(partes[1]) <= 2 or len(partes) == 2 and len(partes[1]) != 3:
            s = s.replace(",", ".")           # coma decimal (formato AR)
        else:
            s = s.replace(",", "")            # coma de miles
    elif "." in s:
        if re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
            s = s.replace(".", "")            # punto de miles
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def monto(valor: str | None) -> dict | None:
    if not valor:
        return None
    v = parse_monto(valor)
    return {"valor": v, "original": valor.strip()} if v is not None else {"valor": None, "original": valor.strip()}


# Códigos de comprobante AFIP (tabla de tipos de comprobante, los más usados).
CODIGOS_AFIP = {
    "001": ("Factura", "A"), "002": ("Nota de Débito", "A"), "003": ("Nota de Crédito", "A"),
    "004": ("Recibo", "A"), "006": ("Factura", "B"), "007": ("Nota de Débito", "B"),
    "008": ("Nota de Crédito", "B"), "009": ("Recibo", "B"), "011": ("Factura", "C"),
    "012": ("Nota de Débito", "C"), "013": ("Nota de Crédito", "C"), "015": ("Recibo", "C"),
    "019": ("Factura", "E"), "020": ("Nota de Débito", "E"), "021": ("Nota de Crédito", "E"),
    "051": ("Factura", "M"), "052": ("Nota de Débito", "M"), "053": ("Nota de Crédito", "M"),
    "201": ("Factura de Crédito Electrónica MiPyMEs", "A"),
    "202": ("Nota de Débito Electrónica MiPyMEs", "A"),
    "203": ("Nota de Crédito Electrónica MiPyMEs", "A"),
    "206": ("Factura de Crédito Electrónica MiPyMEs", "B"),
    "207": ("Nota de Débito Electrónica MiPyMEs", "B"),
    "208": ("Nota de Crédito Electrónica MiPyMEs", "B"),
    "211": ("Factura de Crédito Electrónica MiPyMEs", "C"),
    "212": ("Nota de Débito Electrónica MiPyMEs", "C"),
    "213": ("Nota de Crédito Electrónica MiPyMEs", "C"),
}

# Heurística is_invoice() de Escriba (idéntica: ≥2 marcadores presentes).
_INVOICE_HINTS = ("punto de venta", "comp. nro", "cae n", "comprobante",
                  "condición frente al iva", "condicion frente al iva", "factura")


def is_invoice(text: str) -> bool:
    low = (text or "").lower()
    return sum(h in low for h in _INVOICE_HINTS) >= 2


# ═══════════════════════════════════════════════════════════════════════════
# 5. Parseo de campos
# ═══════════════════════════════════════════════════════════════════════════

def detectar_tipo(texto: str) -> dict:
    pl = plegar(texto)
    cod_orig = codigo = None
    m = re.search(r"(?<![a-z])cod(?:igo)?\.?\s*(?:n[°o]\.?\s*)?:?\s*(\d{1,3})(?!\d)", pl)
    if m:
        cod_orig = texto[m.start():m.end()]
        codigo = m.group(1).zfill(3)
    clase = letra = None
    if codigo in CODIGOS_AFIP:
        clase, letra = CODIGOS_AFIP[codigo]
    if not clase:
        for patron, nombre in ((r"nota de credito", "Nota de Crédito"),
                               (r"nota de debito", "Nota de Débito"),
                               (r"factura de credito electronica", "Factura de Crédito Electrónica MiPyMEs"),
                               (r"(?<![a-z])recibo(?![a-z])", "Recibo"),
                               (r"(?<![a-z])factura(?![a-z])", "Factura")):
            if re.search(patron, pl):
                clase = nombre
                break
    if not letra:
        m = (
            re.search(r"(?:factura|nota de (?:credito|debito)|recibo)\s+(?:tipo\s+)?[\"'“]?([abcem])(?![a-z0-9])", pl)
            or re.search(r"\b([abcem])\s+\d{0,3}\s*(?:factura|nota de (?:credito|debito)|recibo)\b", pl)
            or re.search(r"^\s*([abcem])\s*$", pl, re.M)
        )
        if m:
            letra = m.group(1).upper()

    # Fallback: inferir código AFIP cuando hay clase+letra pero no vino explícito Cód.xxx
    if not codigo and clase and letra:
        for cod, (c, l) in CODIGOS_AFIP.items():
            if c == clase and l == letra:
                codigo = cod
                break

    tipo = f"{clase} {letra}" if clase and letra else clase
    return {"tipo_comprobante": tipo, "clase": clase, "letra": letra,
            "codigo_tipo": codigo, "codigo_tipo_original": cod_orig}


def _primero(regs, clave, cond=lambda r: True):
    for r in regs:
        if r["clave"] == clave and r["valor"] and cond(r):
            return r
    return None


def parsear_campos(texto: str) -> dict:
    """Parsea un texto (lineal de Escriba o renglones visuales del modo local)."""
    regs = escanear_etiquetas(texto)
    pl = plegar(texto)
    res: dict = detectar_tipo(texto)

    # --- División cabecera (emisor) / cuerpo (receptor) ---
    r_rec = next((r for r in regs if r["clave"] == "receptor_nombre"), None)
    ids = [r for r in regs if r["clave"] in ("cuit", "cuil", "dni") and r["valor"]]
    if r_rec is not None:
        corte = r_rec["linea"]
    else:  # sin etiqueta de receptor: el 2º identificador marca el bloque receptor
        corte = ids[1]["linea"] if len(ids) > 1 else 10**9
    cab = lambda r: r["linea"] < corte
    cuerpo = lambda r: r["linea"] >= corte

    # --- CUIT emisor / receptor por posición y contexto ---
    id_rec = None
    if r_rec is not None and ids:
        # el identificador más cercano a "Apellido y Nombre / Razón Social"
        # (misma línea primero, luego hasta 4 renglones antes o 8 después)
        cand = [r for r in ids if -4 <= r["linea"] - corte <= 8]
        if cand:
            id_rec = min(
                cand,
                key=lambda r: (
                    0 if r["clave"] in ("cuit", "cuil") else 1,
                    abs(r["linea"] - corte),
                    r["linea"] < corte,
                ),
            )
    elif len(ids) > 1:
        ids_cuit = [r for r in ids if r["clave"] in ("cuit", "cuil")]
        id_rec = ids_cuit[1] if len(ids_cuit) > 1 else ids[1]
    id_emi = next((r for r in ids if r is not id_rec), None)
    # Fallback por regex si faltan etiquetas: 1º CUIT = emisor, otro distinto = receptor
    cuits_txt = ["".join(m.groups()) for m in _RE_CUIT.finditer(texto)]
    for c in extraer_cuits_relajados(texto):
        if c not in cuits_txt:
            cuits_txt.append(c)
    emisor_id = norm_id(id_emi["valor"]) if id_emi else (norm_id(cuits_txt[0]) if cuits_txt else None)
    receptor_id = norm_id(id_rec["valor"]) if id_rec else None
    if receptor_id is None and emisor_id:
        otros = [c for c in cuits_txt if c != emisor_id.get("digitos")]
        receptor_id = norm_id(otros[0]) if otros else None

    val = lambda r: r["valor"] if r else None
    rs_emi = _primero(regs, "razon_social", cab)
    res["emisor"] = {
        "razon_social": val(rs_emi),
        "cuit": emisor_id["numero"] if emisor_id and emisor_id["numero"] else val(id_emi),
        "cuit_valido": emisor_id["valido"] if emisor_id else None,
        "domicilio": val(_primero(regs, "domicilio", cab)),
        "condicion_iva": _normalizar_condicion_iva(val(_primero(regs, "cond_iva", cab))),
        "ingresos_brutos": val(_primero(regs, "ingresos_brutos")),
        "inicio_actividades": norm_fecha(val(_primero(regs, "inicio_act"))),
    }
    rs_rec = r_rec if r_rec and r_rec["valor"] else _primero(regs, "razon_social", cuerpo)
    es_dni = bool(receptor_id and receptor_id["tipo"] == "DNI")
    res["receptor"] = {
        "razon_social": val(rs_rec),
        "cuit": None if es_dni else (receptor_id["numero"] or receptor_id["original"]) if receptor_id else None,
        "dni": receptor_id["numero"] if es_dni else None,
        "tipo_documento": receptor_id["tipo"] if receptor_id else None,
        "cuit_valido": receptor_id["valido"] if receptor_id and not es_dni else None,
        "domicilio": val(_primero(regs, "domicilio", cuerpo)),
        "condicion_iva": _normalizar_condicion_iva(val(_primero(regs, "cond_iva", cuerpo))),
    }
    res["condicion_venta"] = val(_primero(regs, "cond_venta"))

    # --- Punto de venta / número ---
    def _digitos(r, maxlen):
        if not r:
            return None
        m = re.search(rf"(?<!\d)\d{{1,{maxlen}}}(?!\d)", r["valor"])
        return m.group(0) if m else None
    pv = _digitos(_primero(regs, "punto_venta"), 5)
    nro = _digitos(_primero(regs, "comp_nro"), 8)
    if not (pv and nro):  # "0001-00000123" / "00001 - 00000123" en el texto
        m = re.search(r"(?<!\d)(\d{4,5})\s*-\s*(\d{8})(?!\d)", texto)
        if m:
            pv, nro = pv or m.group(1), nro or m.group(2)
    if not (pv and nro):  # etiquetas apiladas en una sola línea plegada
        m = re.search(r"punto de venta:?\s*comp\.?\s?nro:?\s*(\d{1,5})\s+(\d{1,8})", pl)
        if m:
            pv, nro = pv or m.group(1), nro or m.group(2)
    m = re.search(r"factura\s*n(?:ro|°|o)\.?\s*(\d{1,5})\s+(\d{1,8})", pl)
    if m:
        pv, nro = m.group(1), m.group(2)
    res["punto_venta"] = pv
    res["numero_comprobante"] = nro
    res["comprobante"] = (f"{int(pv):04d}-{int(nro):08d}" if pv and nro else None)

    # --- Fechas ---
    fe = val(_primero(regs, "fecha_emision", lambda r: bool(_RE_FECHA.search(r["valor"]) or _RE_FECHA_ISO.search(r["valor"]))))
    f_emi = norm_fecha(fe)
    res["fecha_emision"] = f_emi["iso"] if f_emi else None
    res["fecha_emision_original"] = f_emi["original"] if f_emi else None
    res["periodo_desde"] = (norm_fecha(val(_primero(regs, "periodo_desde"))) or {}).get("iso")
    res["periodo_hasta"] = (norm_fecha(val(_primero(regs, "periodo_hasta"))) or {}).get("iso")
    res["fecha_vto_pago"] = (norm_fecha(val(_primero(regs, "fecha_vto_pago"))) or {}).get("iso")
    if not res["fecha_emision"]:
        for ln in texto.splitlines()[:25]:
            if re.search(r"(?i)venc|cae|porte|transporte|ctg", ln):
                continue
            nf = norm_fecha(ln)
            if nf and nf.get("iso"):
                res["fecha_emision"] = nf["iso"]
                res["fecha_emision_original"] = nf["original"]
                break
    if not res["fecha_vto_pago"]:
        m = re.search(r"(?i)vencimiento(?:\s+el)?\s+(\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4})", texto)
        if m:
            res["fecha_vto_pago"] = (norm_fecha(m.group(1)) or {}).get("iso")

    # --- CAE ---
    r_cae = _primero(regs, "cae", lambda r: bool(re.search(r"\d{14}", r["valor"])))
    cae = re.search(r"\d{14}", r_cae["valor"]).group(0) if r_cae else None
    if not cae:
        m = re.search(r"c\.?a\.?e\.?a?[^0-9]{0,25}(\d{14})(?!\d)", pl)
        cae = m.group(1) if m else None
    res["cae"] = cae
    fv = norm_fecha(val(_primero(regs, "fecha_vto_cae")))
    res["fecha_vencimiento_cae"] = fv["iso"] if fv else None
    res["fecha_vencimiento_cae_original"] = fv["original"] if fv else None

    # --- Importes (sólo valores que parsean como número) ---
    es_num = lambda r: parse_monto(r["valor"]) is not None
    neto = monto(val(_primero(regs, "neto", es_num)))
    res["importe_neto"] = neto["valor"] if neto else None
    res["importe_neto_original"] = neto["original"] if neto else None
    detalle = []
    for r in regs:
        if r["clave"] == "iva_alic" and r["valor"]:
            alic = re.search(r"(\d{1,2}(?:[.,]\d{1,2})?)\s*%", r["etiqueta"])
            m_ = monto(r["valor"])
            detalle.append({"alicuota": parse_monto(alic.group(1)) if alic else None,
                            "monto": m_["valor"] if m_ else None,
                            "original": r["valor"]})
    # sólo la primera aparición de cada alícuota (copias ORIGINAL/DUPLICADO)
    vistos, iva_det = set(), []
    for d in detalle:
        if d["alicuota"] not in vistos:
            vistos.add(d["alicuota"]); iva_det.append(d)
    iva_cands = [r for r in regs if r["clave"] == "iva_total" and es_num(r)]
    iva_tot = monto(max(iva_cands, key=lambda r: parse_monto(r["valor"]) or 0)["valor"]) if iva_cands else None
    if iva_tot and iva_tot["valor"] is not None:
        res["iva"], res["iva_original"] = iva_tot["valor"], iva_tot["original"]
    elif iva_det:
        res["iva"] = round(sum(d["monto"] or 0 for d in iva_det), 2)
        res["iva_original"] = " + ".join(d["original"] for d in iva_det if d["monto"])  or iva_det[0]["original"]
    else:
        res["iva"] = res["iva_original"] = None
    res["iva_detalle"] = [d for d in iva_det if d["monto"]] or iva_det or None
    otros = monto(val(_primero(regs, "otros_tributos", es_num)))
    res["otros_tributos"] = otros["valor"] if otros else None
    total = monto(val(_primero(regs, "total", es_num)))
    res["importe_total"] = total["valor"] if total else None
    res["importe_total_original"] = total["original"] if total else None

    # --- Moneda ---
    mon = val(_primero(regs, "moneda"))
    cod_mon = None
    if mon:
        pm = plegar(mon)
        if re.search(r"(?<![a-z])(pes|ars|peso)", pm):
            cod_mon = "ARS"
        elif re.search(r"(?<![a-z])(dol|usd|u\$s|dolar)", pm):
            cod_mon = "USD"
        elif re.search(r"(?<![a-z])(eur|euro)", pm):
            cod_mon = "EUR"
    if not mon and re.search(r"(?i)\bson\s+pesos\b|\bpesos\b", texto):
        mon, cod_mon = "Pesos", "ARS"
    res["moneda"] = mon
    res["moneda_codigo"] = cod_mon

    if not res["emisor"].get("condicion_iva"):
        m_cond = _RE_COND_IVA_TXT.search(texto)
        if m_cond:
            res["emisor"]["condicion_iva"] = m_cond.group(1).upper()

    if not res["emisor"].get("razon_social"):
        m = re.search(r"(?im)^\s*de\s+([^\n]{4,80}?)\s+[A-Z]\s+\d+\s+FACTURA\b", texto)
        if m:
            res["emisor"]["razon_social"] = " ".join(m.group(1).split())

    if not res["receptor"].get("razon_social"):
        for ln in texto.splitlines():
            l = ln.strip()
            if not l:
                continue
            if re.search(r"(?i)^(señor\(es\):|condicion|cuenta|asiento|ctrl\.?|domicilio|localidad|cuit|ingresos|inicio|factura|comprobante|cae|vencimiento|importe|total|neto|iva|ctg|transporte|proced|destino|carga|km|kilos|tarifa|correo|e-mail)", l):
                continue
            m = re.search(r"\b([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ0-9&'.,\- ]{2,}?(?:S\.?\s*A\.?|S\.?\s*R\.?\s*L\.?|S\.?\s*A\.?\s*S\.?|S\.?\s*C\.?\s*A\.?))\b", l)
            if m:
                rs = " ".join(m.group(1).split())
                if not res["emisor"].get("razon_social") or plegar(rs) != plegar(res["emisor"].get("razon_social") or ""):
                    res["receptor"]["razon_social"] = rs
                    break

    lineas = [" ".join(ln.strip().split()) for ln in texto.splitlines() if ln.strip()]

    if not res["emisor"].get("domicilio"):
        for i, ln in enumerate(lineas[:14]):
            if re.search(r"(?i)\bfactura\b", ln) and i + 1 < len(lineas):
                cand = lineas[i + 1]
                if _parece_domicilio_linea(cand):
                    res["emisor"]["domicilio"] = cand
                    break
        if not res["emisor"].get("domicilio"):
            for ln in lineas[:20]:
                if _parece_domicilio_linea(ln):
                    res["emisor"]["domicilio"] = ln
                    break

    if not res["receptor"].get("domicilio"):
        rs_rec = res["receptor"].get("razon_social") or ""
        if rs_rec:
            prs = plegar(rs_rec)
            for i, ln in enumerate(lineas):
                if prs in plegar(ln):
                    # Caso OCR típico: "731235 Asiento..." + "7630 NECOCHEA ..." -> "73 1235 - 7630 NECOCHEA"
                    for j in range(i + 1, min(i + 6, len(lineas) - 1)):
                        m_calle = re.search(r"\b(\d{6})\b", lineas[j])
                        if not m_calle:
                            continue
                        prox = re.split(r"(?i)\bctrl\.?\s*n?°?", lineas[j + 1])[0].strip(" -—")
                        m_loc = re.search(r"\b(\d{4}\s+[A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ ]{2,})\b", prox)
                        if m_loc:
                            num = m_calle.group(1)
                            res["receptor"]["domicilio"] = f"{num[:2]} {num[2:]} - {m_loc.group(1)}"
                            break
                    if res["receptor"].get("domicilio"):
                        break

                    for cand in lineas[i + 1:i + 5]:
                        cand2 = re.split(r"(?i)\bctrl\.?\s*n?°?", cand)[0].strip(" -—")
                        if _parece_domicilio_linea(cand2):
                            res["receptor"]["domicilio"] = cand2
                            break
                    if res["receptor"].get("domicilio"):
                        break
        if not res["receptor"].get("domicilio"):
            for ln in lineas:
                cand = re.split(r"(?i)\bctrl\.?\s*n?°?", ln)[0].strip(" -—")
                if re.search(r"^\d{4}\s+[A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ ]{2,}$", cand):
                    res["receptor"]["domicilio"] = cand
                    break

    if not res["receptor"].get("condicion_iva"):
        m_rec_cond = re.search(r"(?im)^(?:\d{2}[-\s.]?\d{4,8}[-\s.]?\d)\s+([^\n]{0,40}\b(?:inscripto|monotributo|exento|consumidor\s+final|no\s+responsable)\b)", texto)
        if m_rec_cond:
            res["receptor"]["condicion_iva"] = _normalizar_condicion_iva(m_rec_cond.group(1))
        else:
            for ln in lineas:
                if re.search(r"(?i)\b(inscripto|monotributo|exento|consumidor\s+final|no\s+responsable)\b", ln):
                    if len(re.sub(r"\D", "", ln)) >= 10:
                        res["receptor"]["condicion_iva"] = _normalizar_condicion_iva(ln)
                        break

    if not res.get("condicion_venta"):
        m_cv = re.search(r"(?im)condicion\s+de\s+(?:venta|pago)\s*:\s*([^\n]+)", texto)
        if m_cv:
            res["condicion_venta"] = " ".join(m_cv.group(1).split())

    if not res.get("iva_detalle"):
        alics = []
        for m in re.finditer(r"(?i)importe\s+de\s+iva\s*(\d{1,2}(?:[.,]\d{1,2})?)", texto):
            a = parse_monto(m.group(1))
            if a is not None and 0 < a <= 100 and a not in alics:
                alics.append(a)
        if alics:
            det = None
            if res.get("importe_neto") is not None and res.get("iva") is not None:
                mejor = min(alics, key=lambda a: abs((res["importe_neto"] * a / 100.0) - res["iva"]))
                esperado = round(res["importe_neto"] * mejor / 100.0, 2)
                if abs(esperado - res["iva"]) <= max(2.0, abs(res["iva"]) * 0.03):
                    det = {"alicuota": mejor, "monto": res["iva"], "original": f"Importe de IVA {mejor:.2f}"}
            if not det:
                det = {"alicuota": alics[0], "monto": None, "original": f"Importe de IVA {alics[0]:.2f}"}
            res["iva_detalle"] = [det]

    if res.get("otros_tributos") is None:
        neto_v, iva_v, tot_v = res.get("importe_neto"), res.get("iva"), res.get("importe_total")
        if None not in (neto_v, iva_v, tot_v):
            dif = round(tot_v - neto_v - iva_v, 2)
            res["otros_tributos"] = 0.0 if abs(dif) < 0.01 else dif

    return res


def _completar(base: dict, extra: dict) -> dict:
    """Rellena los None de `base` con los valores de `extra` (recursivo)."""
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _completar(base[k], v)
        elif base.get(k) in (None, "", []) and v not in (None, "", []):
            base[k] = v
    return base


def _normalizar_receptor(campos: dict) -> dict:
    rec = campos.get("receptor")
    if not isinstance(rec, dict):
        return campos
    tipo = (rec.get("tipo_documento") or "").upper()
    if tipo != "DNI":
        rec["dni"] = None
    if tipo == "DNI":
        rec["cuit"] = None
        rec["cuit_valido"] = None
    return campos


# ═══════════════════════════════════════════════════════════════════════════
# 6. Orquestación
# ═══════════════════════════════════════════════════════════════════════════

def procesar(pdf: bytes, nombre: str, modo: str, url: str, timeout: int, ocr: bool,
             anonimizar: str | None, lang: str | None, pages: str | None) -> dict:
    t0 = time.monotonic()
    meta = {"modo_usado": None, "pdf_type": None, "ocr_applied": None, "elapsed_ms": None,
            "words": None, "chars": None, "escriba_url": url if modo != "local" else None,
            "advertencias": []}
    texto = layout = None
    api_resp = None

    if modo in ("api", "auto"):
        try:
            api_resp = convertir_api(pdf, nombre, url, timeout, ocr, anonimizar, lang, pages)
            texto = limpiar_markdown(api_resp.get("markdown") or "")
            meta.update(modo_usado="api", pdf_type=api_resp.get("pdf_type"),
                        ocr_applied=api_resp.get("ocr_applied"),
                        elapsed_ms=api_resp.get("elapsed_ms"),
                        words=api_resp.get("words"), chars=api_resp.get("chars"))
            if api_resp.get("note"):
                meta["advertencias"].append(f"Escriba: {api_resp['note']}")
            if not texto.strip():
                raise ErrorExtraccion("La API devolvió markdown vacío")
        except (ConnectionError, PermissionError, ErrorExtraccion) as e:
            if modo == "api":
                raise ErrorExtraccion(f"Modo API forzado y falló: {e}")
            meta["advertencias"].append(f"API no disponible, se usó modo local ({e})")
            api_resp, texto = None, None

    if texto is None:  # modo local (forzado o fallback)
        try:
            loc = extraer_local(pdf)
        except ErrorExtraccion as e:
            previo = "; ".join(meta["advertencias"])
            raise ErrorExtraccion(f"{e}{' | ' + previo if previo else ''}")
        texto, layout = loc["texto"], loc["layout"]
        meta.update(modo_usado="local", pdf_type="electrónico" if texto else "escaneado (sin texto)",
                    ocr_applied=False, words=len(texto.split()), chars=len(texto),
                    paginas=loc["paginas"])
        if anonimizar:
            meta["advertencias"].append("--anonimizar sólo aplica en modo API; se ignoró")
        if not texto.strip():
            raise ErrorExtraccion("El PDF no tiene capa de texto (¿escaneado?). "
                                  "Usá el modo API con --ocr.")

    # Con anonimización, Escriba a veces reemplaza ETIQUETAS ("Fecha de Emisión:")
    # o condiciones de IVA (que no son PII) por placeholders. Para parsear, se
    # restauran sólo esos casos usando el pseudonym_map; markdown_completo queda
    # tal cual lo devolvió Escriba.
    texto_parseo = texto
    if api_resp is not None and anonimizar:
        for ph, orig in (api_resp.get("pseudonym_map") or {}).items():
            o = str(orig).strip()
            if (_RE_ETIQ.fullmatch(plegar(o).rstrip(": ").strip()) or
                    re.fullmatch(r"(iva )?(responsable inscripto|responsable monotributo|monotributo|"
                                 r"exento|consumidor final|iva sujeto exento|no responsable)",
                                 plegar(o))):
                texto_parseo = texto_parseo.replace(ph, o)

    # Parseo: en modo local primero los renglones visuales (coordenadas) y se
    # completa con el texto lineal; en modo API sólo el texto de Escriba.
    if layout:
        campos = _completar(parsear_campos(layout), parsear_campos(texto))
    else:
        campos = parsear_campos(texto_parseo)
    campos = _normalizar_receptor(campos)

    salida = {"ok": True, "archivo": nombre}
    salida.update(campos)
    salida["es_factura"] = is_invoice(texto)
    if api_resp is not None and anonimizar:
        salida["anonimizado"] = api_resp.get("anonymized") or anonimizar
        salida["pii_count"] = api_resp.get("pii_count")
        salida["pseudonym_map"] = api_resp.get("pseudonym_map") or {}
    salida["markdown_completo"] = texto
    if meta["elapsed_ms"] is None:
        meta["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    meta["elapsed_total_ms"] = int((time.monotonic() - t0) * 1000)
    if not meta["advertencias"]:
        del meta["advertencias"]
    salida["meta"] = meta
    return salida


def leer_pdf(args) -> tuple[bytes, str]:
    if args.stdin or args.pdf == "-":
        data = sys.stdin.buffer.read()
        nombre = args.nombre or "stdin.pdf"
    else:
        if not args.pdf:
            raise ErrorEntrada("Falta la ruta al PDF (o usá --stdin)")
        p = Path(args.pdf)
        if not p.is_file():
            raise ErrorEntrada(f"No existe el archivo: {p}")
        data = p.read_bytes()
        nombre = args.nombre or p.name
    if not data:
        raise ErrorEntrada("El PDF está vacío")
    if b"%PDF" not in data[:1024]:
        raise ErrorEntrada("La entrada no parece un PDF (falta la firma %PDF)")
    return data, nombre


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="extraer_factura.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Extrae datos de facturas AFIP/ARCA en PDF y los imprime como JSON.",
        epilog="""\
Ejemplos:
  python extraer_factura.py factura.pdf                  # API de Escriba (fallback local)
  python extraer_factura.py factura.pdf --local          # sólo PyMuPDF, sin red
  python extraer_factura.py scan.pdf --ocr               # PDF escaneado vía OCR de Escriba
  python extraer_factura.py factura.pdf --anonimizar seudo
  cat factura.pdf | python extraer_factura.py --stdin --nombre f.pdf

Variables de entorno:
  ESCRIBA_URL       URL base de Escriba (default http://localhost:8000)
  ESCRIBA_API_KEY   token X-API-Key (si falta, se lee API_TOKEN= de /home/ubuntu/escriba/.env)
  ESCRIBA_ENV_FILE  ruta alternativa al .env de Escriba
  ESCRIBA_MODE      auto | api | local (default auto)
  ESCRIBA_TIMEOUT   timeout de la API en segundos (default 120)

Códigos de salida: 0 OK · 1 error inesperado · 2 entrada inválida · 3 sin texto / API forzada caída
""")
    ap.add_argument("pdf", nargs="?", help="ruta al PDF ('-' = stdin)")
    ap.add_argument("--stdin", action="store_true", help="leer el PDF desde stdin")
    ap.add_argument("--nombre", help="nombre de archivo a informar (útil con --stdin)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--local", action="store_true", help="forzar modo local (PyMuPDF)")
    g.add_argument("--api", action="store_true", help="forzar modo API (sin fallback local)")
    ap.add_argument("--ocr", action="store_true", help="pedir OCR a Escriba (ocr=true)")
    ap.add_argument("--anonimizar", metavar="MODO", choices=ANON_MODOS,
                    help=f"anonimizar PII en la API: {', '.join(ANON_MODOS)}")
    ap.add_argument("--lang", help="idioma para OCR (se pasa tal cual a Escriba)")
    ap.add_argument("--pages", help="rango de páginas, ej '1-2' (se pasa a Escriba)")
    ap.add_argument("--url", default=os.environ.get("ESCRIBA_URL", DEFAULT_URL),
                    help="URL base de Escriba (env ESCRIBA_URL)")
    ap.add_argument("--timeout", type=int,
                    default=int(os.environ.get("ESCRIBA_TIMEOUT", DEFAULT_TIMEOUT)),
                    help="timeout de la API en segundos (env ESCRIBA_TIMEOUT)")
    ap.add_argument("--sin-markdown", action="store_true", help="omitir markdown_completo")
    ap.add_argument("--pretty", action="store_true", help="JSON indentado")
    args = ap.parse_args(argv)

    modo = "local" if args.local else "api" if args.api else \
        os.environ.get("ESCRIBA_MODE", "auto").strip().lower()
    if modo not in ("auto", "api", "local"):
        modo = "auto"

    def _error(msg, tipo, code):
        sys.stderr.write(json.dumps({"ok": False, "error": msg, "tipo": tipo},
                                    ensure_ascii=False) + "\n")
        return code

    try:
        pdf, nombre = leer_pdf(args)
        out = procesar(pdf, nombre, modo, args.url, args.timeout, args.ocr,
                       args.anonimizar, args.lang, args.pages)
        if args.sin_markdown:
            out.pop("markdown_completo", None)
    except ErrorEntrada as e:
        return _error(str(e), "entrada_invalida", 2)
    except ErrorExtraccion as e:
        return _error(str(e), "extraccion", 3)
    except Exception as e:  # noqa: BLE001
        return _error(f"{type(e).__name__}: {e}", "inesperado", 1)

    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2 if args.pretty else None) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
