"""
scrape_slugs.py — Scraper genérico de slugs de equipos en Flashscore.com
=========================================================================
Uso:
    python scrape_slugs.py --url "https://www.flashscore.com/football/spain/laliga/" --liga "LaLiga"
    python scrape_slugs.py --url "https://www.flashscore.com/football/england/premier-league/" --liga "Premier League"
    python scrape_slugs.py --file ligas.txt   # procesar varias ligas de un fichero

El resultado se guarda/actualiza en slugs.json (merge con datos previos).

Formato de ligas.txt (una liga por línea, separado por |):
    https://www.flashscore.com/football/spain/laliga/ | LaLiga
    https://www.flashscore.com/football/england/premier-league/ | Premier League
"""

import asyncio
import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

SLUGS_FILE = "slugs.json"

# User-agent de escritorio para flashscore.com (no mobi)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def load_existing_slugs() -> dict:
    """Carga el fichero slugs.json existente o devuelve dict vacío."""
    if Path(SLUGS_FILE).exists():
        try:
            with open(SLUGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                logging.info(f"slugs.json cargado: {len(data)} equipos existentes.")
                return data
        except Exception as e:
            logging.warning(f"Error leyendo slugs.json: {e}. Se empieza de cero.")
    return {}


def save_slugs(slugs: dict):
    """Guarda el diccionario de slugs en slugs.json."""
    with open(SLUGS_FILE, "w", encoding="utf-8") as f:
        json.dump(slugs, f, ensure_ascii=False, indent=2)
    logging.info(f"slugs.json guardado: {len(slugs)} equipos en total.")


def extract_slug_from_url(url: str) -> str:
    """
    Extrae el slug completo (nombre-ID) de una URL de equipo de flashscore.com.
    Ejemplo:
      https://www.flashscore.com/team/manchester-city/Wtn9Stg0/
      → "manchester-city-Wtn9Stg0"
    """
    # Patron: /team/<nombre>/<id>/
    match = re.search(r"/team/([^/]+)/([^/]+)/?", url)
    if match:
        name_part = match.group(1)   # manchester-city
        id_part   = match.group(2)   # Wtn9Stg0
        return f"{name_part}-{id_part}"
    return ""


def normalize_team_name(name: str) -> str:
    """Normaliza el nombre del equipo para usarlo como clave del diccionario."""
    return name.strip()


async def scrape_league_slugs(league_url: str, league_name: str) -> dict:
    """
    Scrapea una liga de flashscore.com y devuelve un dict {nombre_equipo: slug}.
    Navega a la página de clasificación/equipos de la liga para obtener los links
    de cada equipo, de donde se extrae el slug.
    """
    slugs = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()

        logging.info(f"[{league_name}] Navegando a {league_url} ...")
        await page.goto(league_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)  # espera extra para JS dinámico

        # Aceptar cookies si aparece el banner
        try:
            accept_btn = page.locator("#onetrust-accept-btn-handler")
            if await accept_btn.is_visible(timeout=4000):
                await accept_btn.click()
                await page.wait_for_timeout(1000)
                logging.info(f"[{league_name}] Cookies aceptadas.")
        except Exception:
            pass

        await page.wait_for_timeout(2000)

        # Intentar navegar a la sección de participantes/equipos si existe
        # Flashscore tiene una pestaña "Participantes" o "Equipos" en las ligas
        participants_found = False
        for label in ["Participants", "Participantes", "Teams", "Equipos", "Clubs"]:
            try:
                tab = page.locator(f"a:has-text('{label}'), span:has-text('{label}')")
                if await tab.first.is_visible(timeout=2000):
                    await tab.first.click()
                    await page.wait_for_timeout(2000)
                    logging.info(f"[{league_name}] Pestaña '{label}' encontrada y clickada.")
                    participants_found = True
                    break
            except Exception:
                pass

        if not participants_found:
            logging.info(f"[{league_name}] No se encontró pestaña de equipos, extrayendo desde vista actual...")

        # Recopilar todos los hrefs de la página que apunten a /team/
        await page.wait_for_timeout(1500)
        html_content = await page.content()

        # Extraer todos los links de equipos del HTML
        team_links = re.findall(r'href="(/team/[^"]+)"', html_content)
        team_links = list(set(team_links))  # deduplicar

        logging.info(f"[{league_name}] {len(team_links)} links de equipos encontrados en HTML.")

        # Para cada link, obtener el nombre visible del equipo
        # Usamos Playwright para iterar los elementos <a> que contienen /team/
        anchors = await page.locator("a[href*='/team/']").all()
        logging.info(f"[{league_name}] {len(anchors)} elementos <a> con /team/ encontrados.")

        for anchor in anchors:
            try:
                href = await anchor.get_attribute("href")
                if not href or "/team/" not in href:
                    continue

                slug = extract_slug_from_url(href)
                if not slug:
                    continue

                # Intentar obtener el texto visible del enlace o su hijo
                name = (await anchor.inner_text()).strip()

                # Limpiar nombre: quitar saltos de línea y espacios extra
                name = re.sub(r"\s+", " ", name).strip()

                # Ignorar si el nombre está vacío o es solo un número/símbolo
                if not name or len(name) < 2 or name.isdigit():
                    continue

                team_name = normalize_team_name(name)
                if team_name and slug:
                    slugs[team_name] = slug
                    logging.debug(f"  → {team_name}: {slug}")

            except Exception as e:
                logging.debug(f"Error procesando anchor: {e}")
                continue

        # Fallback: si no se extrajeron nombres, usar solo los hrefs
        if not slugs and team_links:
            logging.warning(f"[{league_name}] Fallback: extrayendo slugs solo desde hrefs (sin nombres).")
            for href in team_links:
                slug = extract_slug_from_url(href)
                if slug:
                    # Usar la parte del nombre del slug como clave temporal
                    name_part = slug.rsplit("-", 1)[0].replace("-", " ").title()
                    slugs[name_part] = slug

        await browser.close()

    logging.info(f"[{league_name}] Extraídos {len(slugs)} equipos.")
    return slugs


async def process_leagues(leagues: list[tuple[str, str]]):
    """
    Procesa una lista de (url, nombre_liga), hace merge con slugs.json existente
    y guarda el resultado.
    """
    all_slugs = load_existing_slugs()
    total_new = 0

    for url, name in leagues:
        try:
            league_slugs = await scrape_league_slugs(url, name)
            new_in_league = 0
            for team, slug in league_slugs.items():
                if team not in all_slugs:
                    new_in_league += 1
                all_slugs[team] = slug  # siempre actualiza por si el slug cambió
            total_new += new_in_league
            logging.info(f"[{name}] {new_in_league} equipos nuevos añadidos.")
        except Exception as e:
            logging.error(f"Error scrapeando {name} ({url}): {e}")

    save_slugs(all_slugs)
    logging.info(f"✅ Proceso completado. {total_new} equipos nuevos. Total: {len(all_slugs)}.")
    return all_slugs


def parse_leagues_file(filepath: str) -> list[tuple[str, str]]:
    """Lee un fichero de ligas con formato: URL | Nombre"""
    leagues = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|", 1)
            if len(parts) == 2:
                url  = parts[0].strip()
                name = parts[1].strip()
                leagues.append((url, name))
            else:
                logging.warning(f"Línea ignorada (formato incorrecto): {line}")
    return leagues


def main():
    parser = argparse.ArgumentParser(
        description="Scraper genérico de slugs de equipos en Flashscore.com"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url",  help="URL de la liga en flashscore.com")
    group.add_argument("--file", help="Fichero .txt con varias ligas (URL | Nombre)")
    parser.add_argument("--liga", help="Nombre de la liga (requerido con --url)", default="Liga")
    parser.add_argument("--output", help="Fichero de salida (default: slugs.json)", default="slugs.json")
    parser.add_argument("--debug", action="store_true", help="Activar logs de debug")

    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    global SLUGS_FILE
    SLUGS_FILE = args.output

    if args.url:
        leagues = [(args.url, args.liga)]
    else:
        leagues = parse_leagues_file(args.file)
        if not leagues:
            logging.error("No se encontraron ligas válidas en el fichero.")
            sys.exit(1)

    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    asyncio.run(process_leagues(leagues))


if __name__ == "__main__":
    main()
