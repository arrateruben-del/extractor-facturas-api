
import os
import sys
import json
import hmac
import uuid
import subprocess
import tempfile
import urllib.request
import urllib.error
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException
from fastapi.responses import Response

app = FastAPI(
    title="API Extractor de Facturas",
    version="1.1.0",
    docs_url=None,
    redoc_url=None
)

BASE_DIR = Path(__file__).resolve().parent
SCRIPT = BASE_DIR / "extraer_factura.py"
API_KEY = os.environ.get("EXTRACTOR_API_KEY", "")
MAX_BYTES = 15 * 1024 * 1024


ESCRIBA_URL = os.environ.get("ESCRIBA_URL", "").strip()
ESCRIBA_API_KEY = os.environ.get("ESCRIBA_API_KEY", "").strip()


def _validar_url(valor: str) -> dict:
    """Valida que la URL tenga esquema http(s) y host. No es un ping."""
    if not valor:
        return {"ok": False, "detalle": "ESCRIBA_URL vacía"}
    p = urlparse(valor)
    if p.scheme not in ("http", "https") or not p.hostname:
        return {"ok": False, "host": None,
                "detalle": f"No es una URL con servidor (valor='{valor}'). "
                           "Formato esperado: http://host:puerto"}
    return {"ok": True, "host": p.hostname, "port": p.port, "scheme": p.scheme}


def _probar_escriba_auth(timeout: int = 8) -> dict:
    """Prueba REAL de conectividad + autenticación contra Escriba.
    Nunca devuelve ni loguea el valor del token."""
    val = _validar_url(ESCRIBA_URL)
    if not val["ok"]:
        return {"url_valida": False, **val, "auth": None}
    if not ESCRIBA_API_KEY:
        return {"url_valida": True, "host": val["host"],
                "auth": "sin_api_key", "detalle": "ESCRIBA_API_KEY vacía"}
    boundary = "----diag" + uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; "
            "name=\"file\"; filename=\"diag.pdf\"\r\n"
            "Content-Type: application/pdf\r\n\r\n%PDF-1.4\r\n"
            f"--{boundary}--\r\n").encode()
    req = urllib.request.Request(
        ESCRIBA_URL.rstrip("/") + "/api/convert", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "X-API-Key": ESCRIBA_API_KEY, "Accept": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {"url_valida": True, "host": val["host"],
                    "auth": "ok", "http": r.status}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"url_valida": True, "host": val["host"], "auth": "rechazada",
                    "http": e.code,
                    "detalle": "El token ESCRIBA_API_KEY no es válido (HTTP %d)" % e.code}
        # Cualquier otro código (p.ej. 422 por el PDF dummy) = auth aceptada
        return {"url_valida": True, "host": val["host"], "auth": "ok", "http": e.code}
    except Exception as e:
        return {"url_valida": True, "host": val["host"], "auth": "sin_conexion",
                "detalle": f"Escriba no responde: {e}"}


@app.get("/health")
def health(deep: bool = False):
    """Básico por defecto. Con ?deep=1 valida ESCRIBA_URL y prueba el token real."""
    if not deep:
        return {"status": "ok"}
    escriba = _probar_escriba_auth()
    sano = escriba.get("url_valida") and escriba.get("auth") == "ok"
    return {"status": "ok" if sano else "degradado",
            "extractor_api_key_configurada": bool(API_KEY),
            "escriba": escriba}


@app.post("/extraer-factura")
async def extraer_factura(
    file: UploadFile = File(...),
    ocr: bool = Form(default=False),
    x_api_key: str | None = Header(default=None)
):
    if not API_KEY or not x_api_key or not hmac.compare_digest(
        x_api_key, API_KEY
    ):
        raise HTTPException(
            status_code=401,
            detail="API Key invalida"
        )

    nombre = file.filename or ""

    if not nombre.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=400,
            detail="Solo se aceptan archivos PDF"
        )

    ruta = None

    try:
        with tempfile.NamedTemporaryFile(
            suffix=".pdf",
            delete=False
        ) as temporal:
            ruta = temporal.name
            total = 0
            encabezado = b""

            while bloque := await file.read(1024 * 1024):
                if not encabezado:
                    encabezado = bloque[:5]

                total += len(bloque)

                if total > MAX_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="PDF demasiado grande"
                    )

                temporal.write(bloque)

        if encabezado != b"%PDF-":
            raise HTTPException(
                status_code=400,
                detail="El archivo no parece un PDF valido"
            )

        comando = [sys.executable, str(SCRIPT), ruta]
        if ocr:
            # Requiere ESCRIBA_URL y una API key válida para el servicio OCR.
            comando.extend(["--api", "--ocr"])
        else:
            # Mantiene el modo local actual cuando no se solicita OCR.
            comando.append("--local")

        resultado = subprocess.run(
            comando,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=120
        )

        try:
            datos = json.loads(resultado.stdout)
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=500,
                detail="El extractor no devolvio JSON valido"
            )

        if resultado.returncode != 0 or not datos.get("ok"):
            raise HTTPException(
                status_code=422,
                detail={
                    "mensaje": "No se pudo extraer la factura",
                    "tipo": datos.get("tipo", "error")
                }
            )

        return datos

    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504,
            detail="Tiempo de procesamiento agotado"
        )

    finally:
        await file.close()

        if ruta and os.path.exists(ruta):
            os.remove(ruta)


def _render_pixmap(pymupdf, pagina, dpi: int):
    """Renderiza una pagina a pixmap RGB a los DPI pedidos, acotando el tamano
    a <= 8 MP y a 2400x3000 px para no agotar memoria con PDFs grandes."""
    ancho = float(pagina.rect.width)
    alto = float(pagina.rect.height)
    if ancho <= 0 or alto <= 0:
        raise HTTPException(status_code=422, detail="Pagina PDF sin dimensiones validas")
    escala = dpi / 72.0
    escala = min(escala, 2400 / ancho, 3000 / alto, (8_000_000 / (ancho * alto)) ** 0.5)
    escala = max(escala, 0.1)
    return pagina.get_pixmap(
        matrix=pymupdf.Matrix(escala, escala),
        colorspace=pymupdf.csRGB,
        alpha=False,
    )


def _pixmap_a_bytes(pixmap, formato: str, calidad: int) -> bytes:
    if formato == "png":
        return pixmap.tobytes("png")
    return pixmap.tobytes("jpg", jpg_quality=calidad)


@app.post("/pdf-a-imagen")
async def pdf_a_imagen(
    file: UploadFile = File(...),
    formato: str = Form(default="png"),
    paginas: str = Form(default="1"),
    dpi: int = Form(default=150),
    calidad: int = Form(default=90),
    x_api_key: str | None = Header(default=None),
):
    """Convierte un PDF en imagenes JPG o PNG.

    Parametros (form-data):
      - file: PDF a convertir (obligatorio).
      - formato: 'png' (por defecto) o 'jpg'.
      - paginas: '1' (por defecto) o cualquier numero de pagina (1-based); o
        'todas'/'all' para convertir todas las paginas.
      - dpi: resolucion de salida (72-300, por defecto 150).
      - calidad: calidad JPG 1-100 (por defecto 90; se ignora en PNG).

    Devuelve una sola imagen cuando se pide una pagina, o un ZIP
    (una imagen por pagina) cuando se piden todas. Autenticacion y limite de
    tamano compatibles con /extraer-factura.
    """
    if not API_KEY or not x_api_key or not hmac.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="API Key invalida")

    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Solo se aceptan archivos PDF")

    formato = (formato or "png").strip().lower()
    if formato in ("jpeg",):
        formato = "jpg"
    if formato not in ("png", "jpg"):
        raise HTTPException(status_code=400, detail="formato debe ser 'png' o 'jpg'")

    dpi = max(72, min(int(dpi), 300))
    calidad = max(1, min(int(calidad), 100))
    sel = (paginas or "1").strip().lower()
    todas = sel in ("todas", "all", "*")
    if not todas:
        if not sel.isdigit() or int(sel) < 1:
            raise HTTPException(
                status_code=400,
                detail="paginas debe ser un numero >= 1 o 'todas'",
            )
        n_pagina = int(sel)

    ext = "png" if formato == "png" else "jpg"
    media = "image/png" if formato == "png" else "image/jpeg"
    base = Path(file.filename or "pdf").stem or "pagina"

    try:
        contenido = bytearray()
        while bloque := await file.read(1024 * 1024):
            contenido.extend(bloque)
            if len(contenido) > MAX_BYTES:
                raise HTTPException(status_code=413, detail="PDF demasiado grande")

        if not contenido.startswith(b"%PDF-"):
            raise HTTPException(status_code=400, detail="El archivo no parece un PDF valido")

        try:
            import pymupdf
        except ImportError:
            raise HTTPException(status_code=500, detail="PyMuPDF no esta instalado")

        try:
            with pymupdf.open(stream=bytes(contenido), filetype="pdf") as documento:
                if documento.page_count < 1:
                    raise HTTPException(status_code=422, detail="El PDF no contiene paginas")
                if documento.needs_pass:
                    raise HTTPException(status_code=422, detail="El PDF tiene contrasena")

                if not todas:
                    if n_pagina > documento.page_count:
                        raise HTTPException(
                            status_code=422,
                            detail=f"El PDF tiene {documento.page_count} pagina(s); "
                                   f"se pidio la {n_pagina}",
                        )
                    pixmap = _render_pixmap(pymupdf, documento[n_pagina - 1], dpi)
                    datos = _pixmap_a_bytes(pixmap, formato, calidad)
                    return Response(
                        content=datos,
                        media_type=media,
                        headers={
                            "Cache-Control": "no-store",
                            "Content-Disposition":
                                f'inline; filename="{base}_p{n_pagina}.{ext}"',
                        },
                    )

                import io
                import zipfile
                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                    ancho_idx = len(str(documento.page_count))
                    for i in range(documento.page_count):
                        pixmap = _render_pixmap(pymupdf, documento[i], dpi)
                        datos = _pixmap_a_bytes(pixmap, formato, calidad)
                        nombre = f"{base}_p{str(i + 1).zfill(ancho_idx)}.{ext}"
                        zf.writestr(nombre, datos)
                buffer.seek(0)
                return Response(
                    content=buffer.getvalue(),
                    media_type="application/zip",
                    headers={
                        "Cache-Control": "no-store",
                        "Content-Disposition": f'attachment; filename="{base}_{ext}.zip"',
                    },
                )
        except HTTPException:
            raise
        except Exception as exc:
            # No exponer detalles del PDF ni rutas internas al cliente.
            raise HTTPException(status_code=422, detail="No se pudo convertir el PDF a imagen") from exc
    finally:
        await file.close()
