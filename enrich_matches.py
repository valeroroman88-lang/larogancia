"""
enrich_matches.py — Enriquece el JSON diario con slugs de equipos y MATCH_ID
=============================================================================
Se llama desde radar_final_v1.py justo después de descargar el JSON de GitHub.

Uso:
    from enrich_matches import enrich_match_list
    data = await fetch_json(JSON_URL)
    data = enrich_match_list(data)
"""

import json
import logging
import os
import re
from pathlib import Path

# Ruta al fichero de slugs (en el mismo directorio que este script)
_SLUGS_PATH = os.path.join(os.path.dirname(__file__), "slugs.json")

# Cache en memoria para no releer el fichero en cada llamada
_slugs_cache: dict | None = None


def _load_slugs() -> dict:
    global _slugs_cache
    if _slugs_cache is not None:
        return _slugs_cache
    if not Path(_SLUGS_PATH).exists():
        logging.warning(f"[Enrich] slugs.json no encontrado en {_SLUGS_PATH}. Sin enriquecimiento.")
        _slugs_cache = {}
        return _slugs_cache
    try:
        with open(_SLUGS_PATH, "r", encoding="utf-8") as f:
            _slugs_cache = json.load(f)
        logging.info(f"[Enrich] slugs.json cargado: {len(_slugs_cache)} equipos.")
    except Exception as e:
        logging.error(f"[Enrich] Error leyendo slugs.json: {e}")
        _slugs_cache = {}
    return _slugs_cache


def _extract_match_id(mobi_url: str) -> str:
    """
    Extrae el MATCH_ID de una URL de flashscore.mobi.
    Ejemplo:
      https://www.flashscore.mobi/match/Sx4Kqs0J/ → "Sx4Kqs0J"
    """
    match = re.search(r"/match/([^/?]+)", mobi_url)
    return match.group(1) if match else ""


def _find_slug(team_name: str, slugs: dict) -> str:
    """
    Busca el slug de un equipo con tolerancia a variaciones de nombre.
    Estrategias en orden:
      1. Match exacto
      2. Match case-insensitive
      3. Match parcial (el nombre del JSON contiene la clave del dict o viceversa)
    """
    if not team_name:
        return ""

    # 1. Exacto
    if team_name in slugs:
        return slugs[team_name]

    # 2. Case-insensitive
    name_lower = team_name.lower()
    for key, slug in slugs.items():
        if key.lower() == name_lower:
            return slug

    # 3. Partial match — el más corto contenido en el más largo
    for key, slug in slugs.items():
        k_lower = key.lower()
        if name_lower in k_lower or k_lower in name_lower:
            return slug

    return ""


def build_second_half_url(match_data: dict) -> str:
    """
    Construye la URL de estadísticas de 2ª parte de flashscore.com.
    Requiere HOME_SLUG, AWAY_SLUG y MATCH_ID en match_data.

    Ejemplo resultado:
      https://www.flashscore.com/match/football/manchester-city-Wtn9Stg0/
      real-madrid-W8mj7MDD/summary/stats/2nd-half/?mid=Sx4Kqs0J
    """
    home_slug = match_data.get("HOME_SLUG", "")
    away_slug = match_data.get("AWAY_SLUG", "")
    mid       = match_data.get("MATCH_ID", "")

    if not all([home_slug, away_slug, mid]):
        return ""

    return (
        f"https://www.flashscore.com/match/football/"
        f"{home_slug}/{away_slug}/"
        f"summary/stats/2nd-half/?mid={mid}"
    )


def enrich_match(match: dict) -> dict:
    """
    Añade HOME_SLUG, AWAY_SLUG, MATCH_ID y URL_2H a un partido individual.
    No modifica los campos existentes.
    """
    slugs = _load_slugs()

    home = match.get("HOME", "")
    away = match.get("AWAY", "")
    url  = match.get("URL flashscore", "")

    home_slug = _find_slug(home, slugs)
    away_slug = _find_slug(away, slugs)
    match_id  = _extract_match_id(url)

    match["HOME_SLUG"] = home_slug
    match["AWAY_SLUG"] = away_slug
    match["MATCH_ID"]  = match_id

    # URL de 2ª parte (vacía si faltan datos)
    url_2h = ""
    if home_slug and away_slug and match_id:
        url_2h = build_second_half_url(match)
    match["URL_2H"] = url_2h

    # Log de advertencia si no se encontró slug
    if not home_slug:
        logging.warning(f"[Enrich] Slug no encontrado para equipo local: '{home}'")
    if not away_slug:
        logging.warning(f"[Enrich] Slug no encontrado para equipo visitante: '{away}'")
    if not match_id:
        logging.warning(f"[Enrich] MATCH_ID no extraído de URL: '{url}'")

    return match


def enrich_match_list(matches: list) -> list:
    """
    Enriquece toda la lista de partidos del JSON diario.
    Devuelve la misma lista modificada in-place.
    """
    found    = 0
    missing  = 0
    no_url2h = 0

    for match in matches:
        enrich_match(match)
        if match.get("HOME_SLUG") and match.get("AWAY_SLUG"):
            found += 1
        else:
            missing += 1
        if not match.get("URL_2H"):
            no_url2h += 1

    total = len(matches)
    logging.info(
        f"[Enrich] {total} partidos procesados: "
        f"{found} con slugs completos, "
        f"{missing} sin slugs, "
        f"{no_url2h} sin URL_2H."
    )
    return matches


def reload_slugs():
    """Fuerza la recarga del fichero slugs.json (útil si se actualiza en caliente)."""
    global _slugs_cache
    _slugs_cache = None
    _load_slugs()
    logging.info("[Enrich] slugs.json recargado.")
