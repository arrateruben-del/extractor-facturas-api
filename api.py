
import os
import sys
import json
import hmac
import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException

app = FastAPI(
    title="API Extractor de Facturas",
    version="1.0.0",
    docs_url=None,
    redoc_url=None
)

BASE_DIR = Path(__file__).resolve().parent
SCRIPT = BASE_DIR / "extraer_factura.py"
API_KEY = os.environ.get("EXTRACTOR_API_KEY", "")
MAX_BYTES = 15 * 1024 * 1024


@app.get("/health")
def health():
    return {"status": "ok"}


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
