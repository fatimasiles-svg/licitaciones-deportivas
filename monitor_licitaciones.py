#!/usr/bin/env python3
"""
Monitor de licitaciones deportivas (bolsas del corredor, carreras, eventos deportivos)
sobre el feed ATOM abierto de la Plataforma de Contratación del Sector Público (PLACSP).

Uso:
    python monitor_licitaciones.py --config config.yaml
"""

import argparse
import json
import logging
import os
import re
import smtplib
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

import requests
import yaml

FEED_URL = "https://contrataciondelestado.es/sindicacion/sindicacion_643/licitacionesPerfilesContratanteCompleto3.atom"

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
# El feed de PLACSP incrusta CPVs y otros metadatos vía extensiones CODICE.
CODICE_NS = {
    "cac-place-ext": "urn:dgpe:names:draft:codice:schema:xsd:CommonAggregateComponents-2",
    "cbc-place-ext": "urn:dgpe:names:draft:codice:schema:xsd:CommonBasicComponents-2",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("monitor_licitaciones")


def cargar_config(ruta: str) -> dict:
    with open(ruta, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # Permite pasar secretos por variable de entorno (útil en GitHub Actions)
    # sin tener que escribirlos en el yaml del repo.
    cfg.setdefault("email", {})
    cfg.setdefault("telegram", {})
    cfg["email"]["usuario"] = os.environ.get("EMAIL_USER", cfg["email"].get("usuario"))
    cfg["email"]["password"] = os.environ.get("EMAIL_PASSWORD", cfg["email"].get("password"))
    cfg["email"]["destinatario"] = os.environ.get("EMAIL_TO", cfg["email"].get("destinatario"))
    cfg["telegram"]["bot_token"] = os.environ.get("TELEGRAM_BOT_TOKEN", cfg["telegram"].get("bot_token"))
    cfg["telegram"]["chat_id"] = os.environ.get("TELEGRAM_CHAT_ID", cfg["telegram"].get("chat_id"))
    return cfg


def descargar_feed(url: str, timeout: int = 30) -> bytes:
    headers = {"User-Agent": "monitor-licitaciones-deportivas/1.0"}
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def extraer_cpvs(entry: ET.Element) -> list[str]:
    """Extrae códigos CPV de la extensión CODICE si están presentes en la entry."""
    cpvs = []
    for el in entry.iter():
        tag = el.tag.split("}")[-1]
        if tag in ("ItemClassificationCode",) and el.text:
            texto = el.text.strip()
            if texto.isdigit():
                cpvs.append(texto)
    return sorted(set(cpvs))


def parsear_entries(contenido_atom: bytes) -> list[dict]:
    root = ET.fromstring(contenido_atom)
    licitaciones = []
    for entry in root.findall("atom:entry", ATOM_NS):
        entry_id = entry.findtext("atom:id", default="", namespaces=ATOM_NS)
        titulo = (entry.findtext("atom:title", default="", namespaces=ATOM_NS) or "").strip()
        resumen = (entry.findtext("atom:summary", default="", namespaces=ATOM_NS) or "").strip()
        actualizado = entry.findtext("atom:updated", default="", namespaces=ATOM_NS)
        link_el = entry.find("atom:link", ATOM_NS)
        link = link_el.get("href") if link_el is not None else ""

        licitaciones.append(
            {
                "id": entry_id,
                "titulo": titulo,
                "resumen": resumen,
                "actualizado": actualizado,
                "link": link,
                "cpv": extraer_cpvs(entry),
            }
        )
    return licitaciones


def coincide(licitacion: dict, cfg: dict) -> bool:
    texto = f"{licitacion['titulo']} {licitacion['resumen']}".lower()

    excluir = [p.lower() for p in cfg.get("excluir_palabras_clave", [])]
    if any(patron in texto for patron in excluir):
        return False

    incluir = [p.lower() for p in cfg.get("incluir_palabras_clave", [])]
    if any(patron in texto for patron in incluir):
        return True

    cpv_objetivo = set(cfg.get("cpv_codigos", []))
    if cpv_objetivo and any(cpv.startswith(tuple(cpv_objetivo)) for cpv in licitacion["cpv"]):
        return True

    return False


def cargar_vistos(ruta: Path) -> dict:
    if not ruta.exists():
        return {}
    try:
        with open(ruta, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        log.warning("No se pudo leer %s, empezando de cero", ruta)
        return {}


def guardar_vistos(ruta: Path, vistos: dict, dias_retencion: int = 60) -> None:
    limite = datetime.now(timezone.utc) - timedelta(days=dias_retencion)
    vistos_podados = {}
    for entry_id, guardado_en in vistos.items():
        try:
            fecha = datetime.fromisoformat(guardado_en)
        except ValueError:
            continue
        if fecha >= limite:
            vistos_podados[entry_id] = guardado_en

    ruta.parent.mkdir(parents=True, exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(vistos_podados, f, ensure_ascii=False, indent=2)


def cargar_historial(ruta: Path) -> list[dict]:
    if not ruta.exists():
        return []
    try:
        with open(ruta, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        log.warning("No se pudo leer %s, empezando de cero", ruta)
        return []


def guardar_historial(ruta: Path, historial: list[dict], dias_retencion: int = 180) -> None:
    limite = datetime.now(timezone.utc) - timedelta(days=dias_retencion)
    podado = []
    for item in historial:
        try:
            fecha = datetime.fromisoformat(item["encontrado_en"])
        except (KeyError, ValueError):
            continue
        if fecha >= limite:
            podado.append(item)

    ruta.parent.mkdir(parents=True, exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(podado, f, ensure_ascii=False, indent=2)


def formatear_fecha(iso_texto: str) -> str:
    try:
        fecha = datetime.fromisoformat(iso_texto)
    except ValueError:
        return iso_texto
    return fecha.strftime("%d/%m/%Y %H:%M UTC")


def generar_html(historial: list[dict], ruta_salida: Path, ultima_comprobacion: str, feed_ok: bool) -> None:
    filas = sorted(historial, key=lambda x: x.get("encontrado_en", ""), reverse=True)

    def escapar(texto: str) -> str:
        return (
            texto.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    if filas:
        tarjetas = "\n".join(
            f"""
        <article class="tarjeta">
          <h2><a href="{escapar(f['link'])}" target="_blank" rel="noopener">{escapar(f['titulo'])}</a></h2>
          <p class="meta">Publicada: {escapar(f.get('actualizado', 'n/d')[:10])} · Detectada: {escapar(f.get('encontrado_en', '')[:10])}{' · CPV: ' + escapar(', '.join(f['cpv'])) if f.get('cpv') else ''}</p>
        </article>"""
            for f in filas
        )
    else:
        tarjetas = '<p class="vacio">Todavía no se ha encontrado ninguna licitación que coincida. Esta página se actualiza automáticamente cada semana.</p>'

    estado = (
        '<span class="ok">● Feed consultado correctamente</span>'
        if feed_ok
        else '<span class="error">● Hubo un problema al consultar el feed en la última ejecución</span>'
    )

    html = f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Licitaciones deportivas</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; max-width: 780px; margin: 0 auto; padding: 24px 16px 64px; line-height: 1.5; }}
  h1 {{ font-size: 1.4rem; margin-bottom: 4px; }}
  .subtitulo {{ color: #666; font-size: 0.9rem; margin-top: 0; }}
  .estado {{ font-size: 0.85rem; margin: 12px 0 28px; }}
  .ok {{ color: #1a7f37; }}
  .error {{ color: #c0392b; }}
  .tarjeta {{ border: 1px solid rgba(127,127,127,0.3); border-radius: 10px; padding: 14px 16px; margin-bottom: 12px; }}
  .tarjeta h2 {{ font-size: 1.05rem; margin: 0 0 6px; }}
  .tarjeta a {{ text-decoration: none; }}
  .tarjeta a:hover {{ text-decoration: underline; }}
  .meta {{ font-size: 0.82rem; color: #777; margin: 0; }}
  .vacio {{ color: #777; font-style: italic; }}
  footer {{ margin-top: 40px; font-size: 0.78rem; color: #999; }}
</style>
</head>
<body>
  <h1>Licitaciones deportivas — bolsas del corredor, carreras y eventos</h1>
  <p class="subtitulo">Vigilancia automática del feed de la Plataforma de Contratación del Sector Público (PLACSP)</p>
  <p class="estado">{estado} · Última comprobación: {escapar(formatear_fecha(ultima_comprobacion))}</p>
  {tarjetas}
  <footer>Se actualiza automáticamente cada lunes mediante GitHub Actions. {len(filas)} licitación(es) en el histórico (últimos 180 días).</footer>
</body>
</html>
"""
    ruta_salida.parent.mkdir(parents=True, exist_ok=True)
    with open(ruta_salida, "w", encoding="utf-8") as f:
        f.write(html)
    log.info("Panel HTML generado en %s", ruta_salida)


def enviar_email(cfg_email: dict, nuevas: list[dict]) -> None:
    if not all([cfg_email.get("usuario"), cfg_email.get("password"), cfg_email.get("destinatario")]):
        log.info("Email no configurado del todo, se omite el envío por correo")
        return

    cuerpo = "\n\n".join(
        f"{l['titulo']}\n{l['link']}\nActualizado: {l['actualizado']}"
        for l in nuevas
    )
    asunto = f"[Licitaciones deportivas] {len(nuevas)} nueva(s) coincidencia(s)"
    msg = MIMEText(cuerpo, "plain", "utf-8")
    msg["Subject"] = asunto
    msg["From"] = cfg_email["usuario"]
    msg["To"] = cfg_email["destinatario"]

    servidor = cfg_email.get("smtp_servidor", "smtp.gmail.com")
    puerto = int(cfg_email.get("smtp_puerto", 587))

    with smtplib.SMTP(servidor, puerto, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(cfg_email["usuario"], cfg_email["password"])
        smtp.send_message(msg)
    log.info("Email enviado a %s", cfg_email["destinatario"])


def enviar_telegram(cfg_telegram: dict, nuevas: list[dict]) -> None:
    token = cfg_telegram.get("bot_token")
    chat_id = cfg_telegram.get("chat_id")
    if not token or not chat_id:
        log.info("Telegram no configurado, se omite el envío")
        return

    for l in nuevas:
        texto = f"📋 *{l['titulo']}*\n{l['link']}\nActualizado: {l['actualizado']}"
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            resp = requests.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": texto,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": False,
                },
                timeout=15,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.error("Fallo enviando a Telegram: %s", exc)
    log.info("Notificaciones de Telegram enviadas (%d)", len(nuevas))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml", help="Ruta al fichero de configuración")
    parser.add_argument("--vistos", default="data/vistos.json", help="Fichero de estado de licitaciones ya notificadas")
    parser.add_argument("--historial", default="data/historial.json", help="Fichero con el histórico completo para el panel web")
    parser.add_argument("--panel", default="docs/index.html", help="Ruta de salida del panel HTML (GitHub Pages)")
    parser.add_argument("--dry-run", action="store_true", help="No envía alertas, solo muestra por consola")
    args = parser.parse_args()

    if not Path(args.config).exists():
        log.error("No existe el fichero de configuración: %s (copia config.example.yaml)", args.config)
        return 1

    cfg = cargar_config(args.config)
    ruta_vistos = Path(args.vistos)
    ruta_historial = Path(args.historial)
    vistos = cargar_vistos(ruta_vistos)
    historial = cargar_historial(ruta_historial)
    ahora = datetime.now(timezone.utc).isoformat()

    log.info("Descargando feed de PLACSP...")
    try:
        contenido = descargar_feed(cfg.get("feed_url", FEED_URL))
    except requests.RequestException as exc:
        log.error("No se pudo descargar el feed: %s", exc)
        if not args.dry_run:
            generar_html(historial, Path(args.panel), ahora, feed_ok=False)
        return 1

    licitaciones = parsear_entries(contenido)
    log.info("Entradas en el feed: %d", len(licitaciones))

    coincidencias = [l for l in licitaciones if coincide(l, cfg)]
    log.info("Coincidencias por palabra clave/CPV: %d", len(coincidencias))

    nuevas = [l for l in coincidencias if l["id"] not in vistos]
    log.info("Nuevas (no notificadas antes): %d", len(nuevas))

    for l in nuevas:
        print(f"- {l['titulo']}\n  {l['link']}\n  CPV: {', '.join(l['cpv']) or 'n/d'}\n")

    if args.dry_run:
        log.info("Modo --dry-run: no se han enviado alertas ni guardado estado")
        return 0

    if nuevas:
        enviar_email(cfg.get("email", {}), nuevas)
        enviar_telegram(cfg.get("telegram", {}), nuevas)

        for l in nuevas:
            vistos[l["id"]] = ahora
            historial.append({**l, "encontrado_en": ahora})
        guardar_vistos(ruta_vistos, vistos)
        guardar_historial(ruta_historial, historial)
    else:
        log.info("Nada nuevo que avisar.")

    generar_html(historial, Path(args.panel), ahora, feed_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
