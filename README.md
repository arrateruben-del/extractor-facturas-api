# Extractor de Facturas API

API REST desarrollada con Python y FastAPI para extraer información fiscal de facturas PDF.

## Tecnologías

- Python 3.12
- FastAPI
- PyMuPDF
- Uvicorn
- HTTPS mediante Caddy
- Servicio permanente mediante systemd

## Instalación

1. Crear un entorno virtual con Python 3.
2. Instalar las dependencias de requirements.txt.
3. Crear un archivo .env a partir de .env.example.
4. Configurar una API Key segura.
5. Ejecutar FastAPI con Uvicorn.

## Endpoints

GET /health: comprueba el estado de la API.

POST /extraer-factura: recibe un PDF mediante multipart/form-data, campo file.

El endpoint de extracción requiere el encabezado X-API-Key.

## Seguridad

No publicar archivos .env, claves privadas ni documentos fiscales.

## Integración

La API puede recibir solicitudes HTTP desde n8n Cloud y devolver los datos extraídos en JSON.
