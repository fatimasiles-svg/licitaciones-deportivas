#!/usr/bin/env python3
"""
Monitor de licitaciones deportivas (bolsas del corredor, carreras, eventos deportivos)
sobre el buscador documental de la Plataforma de Contratación del Sector Público (PLACSP).

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
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

import requests
import yaml
from playwright.sync_api import sync_playwright

BUSCADOR_URL = "https://contrataciondelestado.es/wps/portal/plataforma/buscador/"
PAGINAS_POR_BUSQUEDA = 4  # 25 resultados por página -> hasta 100 documentos por frase

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("monitor_licitaciones")

EXTRAER_RESULTADOS_JS = """
() => {
  const links = Array.from(document.querySelectorAll('a')).filter(a => a.href.includes('idEvl'));
  const seen = new Set();
  const out = [];
  for (const a of links) {
    const bloque = a.closest('tr,li,div')?.innerText || '';
    const primeraLinea = bloque.split('\\n')[0] || '';
    const tipoMatch = primeraLinea.match(/\\(([^)]+)\\)/);
    const descMatch = bloque.match(/Descripión:\\s*([^\\n]+)/);
    const fechaMatch = bloque.match(/Detalle de la licitación\\s*([\\d\\/]+)/);
    if (!seen.has(a.href) && descMatch) {
      seen.add(a.href);
      out.push({
        href: a.href,
        tipo: tipoMatch ? tipoMatch[1] : '',
        desc: descMatch[1].trim(),
        fecha: fechaMatch ? fechaMatch[1] : '',
      });
    }
  }
  return out;
}
"""


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


def idevl_de(href: str) -> str:
    m = re.search(r"idEvl=([^&]+)", href)
    return m.group(1) if m else href


def buscar_frase(page, frase: str, max_paginas: int = PAGINAS_POR_BUSQUEDA) -> list[dict]:
    page.goto(BUSCADOR_URL, timeout=30000)
    page.get_by_label("Texto a buscar:").fill(f'"{frase}"')
    page.get_by_role("button", name="Buscar").click()
    page.wait_for_selector("text=Resultados para la consulta", timeout=20000)
    page.wait_for_timeout(1000)

    resultados = list(page.evaluate(EXTRAER_RESULTADOS_JS))
    for _ in range(max_paginas - 1):
        siguiente = page.locator('input[src*="NextButton"]')
        if siguiente.count() == 0:
            break  # no hay más páginas
        siguiente.first.click()
        page.wait_for_timeout(1200)
        resultados.extend(page.evaluate(EXTRAER_RESULTADOS_JS))
    return resultados


def extraer_ficha(page, url: str) -> dict:
    """Visita la ficha real de la licitación para sacar título completo, estado y fecha límite."""
    page.goto(url, timeout=30000)
    page.wait_for_selector("text=Objeto del contrato", timeout=20000)
    texto = page.inner_text("body")

    def buscar(patron: str) -> str:
        m = re.search(patron, texto)
        return m.group(1).strip() if m else ""

    return {
        "expediente": buscar(r"Expediente\n([^\n]+)"),
        "titulo": buscar(r"Objeto del contrato\n([\s\S]+?)\n\s*Enlace a la licitación"),
        "organo": buscar(r"Órgano de contratación\n([^\n]+)"),
        "estado": buscar(r"Estado de la Licitación\n([^\n]+)"),
        "cpv": buscar(r"Código CPV\n([^\n]+)"),
        "plazo": buscar(r"Fecha fin de presentación de oferta\n([^\n]+)"),
    }


def buscar_licitaciones(frases: list[str]) -> list[dict]:
    """Busca cada frase en el buscador documental de PLACSP, y para cada candidata
    única visita su ficha real para confirmar título completo, estado y plazo."""
    candidatas = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(locale="es-ES", extra_http_headers={"Accept-Language": "es-ES,es;q=0.9"})

        for frase in frases:
            try:
                resultados = buscar_frase(page, frase)
            except Exception as exc:
                log.warning("Fallo buscando %r: %s", frase, exc)
                continue
            log.info("  %r -> %d documentos", frase, len(resultados))
            for r in resultados:
                if r["tipo"] == "Anuncio de adjudicación":
                    continue  # ya adjudicada, no es una oportunidad para presentarse
                idevl = idevl_de(r["href"])
                candidatas.setdefault(idevl, r["href"])
            page.wait_for_timeout(500)  # no saturar el servidor

        log.info("Candidatas únicas a comprobar en su ficha real: %d", len(candidatas))
        encontradas = {}
        for i, (idevl, href) in enumerate(candidatas.items(), start=1):
            if i % 10 == 0 or i == len(candidatas):
                log.info("  ficha %d/%d...", i, len(candidatas))
            try:
                ficha = extraer_ficha(page, href)
            except Exception as exc:
                log.warning("Fallo consultando ficha %s: %s", href, exc)
                continue
            if ficha["estado"] != "Publicada":
                continue  # ya cerrada, adjudicada, resuelta, etc. -> no es una oportunidad
            if not ficha["titulo"]:
                continue
            encontradas[idevl] = {
                "id": f"doc:{idevl}",
                "titulo": f"{ficha['expediente']}. {ficha['titulo']}".strip(". "),
                "resumen": f"{ficha['organo']} {ficha['cpv']}",
                "link": href,
                "organo": ficha["organo"],
                "plazo": ficha["plazo"],
            }
            page.wait_for_timeout(400)
        browser.close()
    return list(encontradas.values())


def coincide(licitacion: dict, cfg: dict) -> bool:
    texto = f"{licitacion['titulo']} {licitacion['resumen']}".lower()
    excluir = [p.lower() for p in cfg.get("excluir_palabras_clave", [])]
    if any(patron in texto for patron in excluir):
        return False
    incluir = [p.lower() for p in cfg.get("incluir_palabras_clave", [])]
    return any(patron in texto for patron in incluir)


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


def clave_orden_plazo(licitacion: dict) -> datetime:
    try:
        return datetime.strptime(licitacion.get("plazo", ""), "%d/%m/%Y %H:%M")
    except ValueError:
        return datetime.max  # sin plazo reconocible -> al final


def generar_html(historial: list[dict], ruta_salida: Path, ultima_comprobacion: str, busqueda_ok: bool) -> None:
    filas = sorted(historial, key=clave_orden_plazo)

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
          <p class="meta">Plazo: {escapar(f.get('plazo') or 'n/d')} · Órgano: {escapar(f.get('organo') or 'n/d')} · Detectada: {escapar(f.get('encontrado_en', '')[:10])}</p>
        </article>"""
            for f in filas
        )
    else:
        tarjetas = '<p class="vacio">Todavía no se ha encontrado ninguna licitación que coincida. Esta página se actualiza automáticamente cada semana.</p>'

    estado = (
        '<span class="ok">● Búsqueda realizada correctamente</span>'
        if busqueda_ok
        else '<span class="error">● Hubo un problema al consultar PLACSP en la última ejecución</span>'
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
  <p class="subtitulo">Vigilancia automática del buscador documental de la Plataforma de Contratación del Sector Público (PLACSP)</p>
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
        f"{l['titulo']}\n{l['link']}\nPlazo: {l.get('plazo') or 'n/d'}"
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
        texto = f"📋 *{l['titulo']}*\n{l['link']}"
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

    frases = cfg.get("incluir_palabras_clave", [])
    log.info("Consultando el buscador documental de PLACSP con %d frases...", len(frases))
    try:
        licitaciones = buscar_licitaciones(frases)
    except Exception as exc:
        log.error("No se pudo consultar PLACSP: %s", exc)
        if not args.dry_run:
            generar_html(historial, Path(args.panel), ahora, busqueda_ok=False)
        return 1

    log.info("Licitaciones únicas encontradas: %d", len(licitaciones))

    coincidencias = [l for l in licitaciones if coincide(l, cfg)]
    nuevas = [l for l in coincidencias if l["id"] not in vistos]
    log.info("Nuevas (no notificadas antes): %d", len(nuevas))

    for l in nuevas:
        print(f"- {l['titulo']}\n  Plazo: {l.get('plazo') or 'n/d'}\n  {l['link']}\n")

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

    generar_html(historial, Path(args.panel), ahora, busqueda_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
