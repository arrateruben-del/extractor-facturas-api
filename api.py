
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


@app.post("/pdf-a-imagen")
async def pdf_a_imagen(
    file: UploadFile = File(...),
    x_api_key: str | None = Header(default=None),
):
    """Devuelve la primera pagina del PDF como PNG para lectura visual en n8n.

    Autenticacion y limite de tamano compatibles con /extraer-factura.
    """
    if not API_KEY or not x_api_key or not hmac.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="API Key invalida")

    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Solo se aceptan archivos PDF")

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

                pagina = documento[0]
                ancho = float(pagina.rect.width)
                alto = float(pagina.rect.height)
                if ancho <= 0 or alto <= 0:
                    raise HTTPException(status_code=422, detail="Pagina PDF sin dimensiones validas")

                # Se busca ~2x de resolucion sin superar dimensiones ni 8 MP.
                escala = min(2.0, 2400 / ancho, 3000 / alto,
                             (8_000_000 / (ancho * alto)) ** 0.5)
                imagen = pagina.get_pixmap(
                    matrix=pymupdf.Matrix(escala, escala),
                    colorspace=pymupdf.csRGB,
                    alpha=False,
                )
                png = imagen.tobytes("png")
        except HTTPException:
            raise
        except Exception as exc:
            # No exponer detalles del PDF ni rutas internas al cliente.
            raise HTTPException(status_code=422, detail="No se pudo convertir el PDF a imagen") from exc

        return Response(
            content=png,
            media_type="image/png",
            headers={"Cache-Control": "no-store"},
        )
    finally:
        await file.close()
