"""
match_analyzer.py
=================
Scraper mejorado de Flashscore Mobi + analizador de probabilidad de tarjeta amarilla.

Uso:
    python match_analyzer.py --url "https://www.flashscore.mobi/match/29v3xm7l/?s=2&t=stats"
    python match_analyzer.py --url "..." --arbitro 4.2 --jugadores "Neymar,Casemiro"
    python match_analyzer.py --url "..." --output result.json --no-telegram
"""

import re
import json
import asyncio
import argparse
from dataclasses import dataclass, field, asdict
from typing import Optional

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ── Importación opcional de Telegram ──────────────────────────────────────────
try:
    from telegram_notifier import send_telegram_message
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False


# ==============================================================================
# MODELOS DE DATOS
# ==============================================================================

@dataclass
class MatchStats:
    """Todos los campos que Flashscore expone en la pestaña Stats."""
    home_team: str = "Local"
    away_team: str = "Visitante"
    current_minute: int = 0
    score_home: int = 0
    score_away: int = 0
    score_diff: int = 0          # home - away

    # Tarjetas
    cards_home: int = 0
    cards_away: int = 0

    # Faltas brutas
    fouls_home: int = 0
    fouls_away: int = 0

    # Free kicks (faltas RECIBIDAS = proxy de agresividad del rival)
    free_kicks_home: int = 0
    free_kicks_away: int = 0

    # Entradas
    tackles_home_pct: float = 50.0
    tackles_away_pct: float = 50.0

    # xG
    xg_home: float = 0.0
    xg_away: float = 0.0

    # Tiros
    shots_home: int = 0
    shots_away: int = 0
    shots_on_target_home: int = 0
    shots_on_target_away: int = 0

    # Posesión
    possession_home: float = 50.0
    possession_away: float = 50.0

    # Duelos
    duels_won_home: int = 0
    duels_won_away: int = 0

    # Córners
    corners_home: int = 0
    corners_away: int = 0

    # Big chances
    big_chances_home: int = 0
    big_chances_away: int = 0

    # Contexto externo (no en Flashscore)
    arbitro_amarillas: float = 3.5
    yellow_card_players: list = field(default_factory=list)

    # Histórico de snapshots para calcular diffs (~10 min)
    fouls_home_prev: int = 0
    fouls_away_prev: int = 0

    @property
    def diff_fouls_home(self) -> int:
        return max(0, self.fouls_home - self.fouls_home_prev)

    @property
    def diff_fouls_away(self) -> int:
        return max(0, self.fouls_away - self.fouls_away_prev)

    @property
    def total_fouls(self) -> int:
        return self.fouls_home + self.fouls_away

    @property
    def total_cards(self) -> int:
        return self.cards_home + self.cards_away


# ==============================================================================
# PARSERS DE VALORES
# ==============================================================================

def parse_int(value: str, default: int = 0) -> int:
    """'207/253' → 207  |  '82%' → 82  |  '4' → 4"""
    if not value:
        return default
    # Fracción → tomar numerador
    m = re.match(r"(\d+)\s*/\s*\d+", value.strip())
    if m:
        return int(m.group(1))
    # Porcentaje → parte entera
    m = re.match(r"(\d+)\s*%", value.strip())
    if m:
        return int(m.group(1))
    # Número directo
    m = re.match(r"(\d+)", value.strip())
    if m:
        return int(m.group(1))
    return default


def parse_float(value: str, default: float = 0.0) -> float:
    """'0.47' → 0.47  |  '33% (5/15)' → 33.0"""
    if not value:
        return default
    m = re.search(r"(\d+(?:\.\d+)?)", value.strip())
    if m:
        return float(m.group(1))
    return default


def parse_percentage(value: str, default: float = 50.0) -> float:
    """'33% (5/15)' → 33.0"""
    if not value:
        return default
    m = re.match(r"(\d+(?:\.\d+)?)\s*%", value.strip())
    if m:
        return float(m.group(1))
    return default


# ==============================================================================
# EXTRACCIÓN DE HTML
# ==============================================================================

# Mapa de nombres de stat en Flashscore → campo en MatchStats
STAT_MAP = {
    # Tarjetas
    "yellow cards":         ("cards_home",              "cards_away",              parse_int),
    "red cards":            (None,                       None,                      None),

    # Faltas
    "fouls":                ("fouls_home",              "fouls_away",              parse_int),
    "free kicks":           ("free_kicks_home",         "free_kicks_away",         parse_int),

    # Entradas
    "tackles":              ("tackles_home_pct",        "tackles_away_pct",        parse_percentage),

    # xG
    "expected goals (xg)":  ("xg_home",                "xg_away",                 parse_float),

    # Tiros
    "total shots":          ("shots_home",              "shots_away",              parse_int),
    "shots on target":      ("shots_on_target_home",    "shots_on_target_away",    parse_int),

    # Posesión
    "ball possession":      ("possession_home",         "possession_away",         parse_percentage),

    # Duelos
    "duels won":            ("duels_won_home",          "duels_won_away",          parse_int),

    # Córners
    "corner kicks":         ("corners_home",            "corners_away",            parse_int),

    # Big chances
    "big chances":          ("big_chances_home",        "big_chances_away",        parse_int),
}


def extract_stats_from_html(html_content: str) -> dict:
    """
    Extrae stats del HTML de Flashscore Mobi.
    Devuelve un dict plano {stat_name: {home, away}} y también los campos
    ya mapeados para MatchStats.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    raw_stats = []

    # Flashscore usa filas con clase que contiene 'wcl-row'
    rows = soup.find_all("div", class_=lambda c: c and "wcl-row" in c)

    for row in rows:
        divs = [d for d in row.find_all("div") if d.text.strip()]
        texts = [d.text.strip() for d in divs]

        if len(texts) >= 3:
            # Intentar detectar patrón: home | stat_name | away
            # Flashscore pone: [home_val, stat_label, away_val]
            # A veces hay un div wrapper adicional al inicio
            for i in range(len(texts) - 2):
                home_val  = texts[i]
                stat_name = texts[i + 1]
                away_val  = texts[i + 2]

                # La stat_name suele tener texto mixto (no numérico)
                if not re.search(r"[a-zA-Z]{3,}", stat_name):
                    continue
                # El stat_name no debe parecer un valor numérico
                if re.match(r"^\d", stat_name):
                    continue

                raw_stats.append({
                    "category": stat_name,
                    "home":     home_val,
                    "away":     away_val,
                })
                break  # Un resultado por fila

    return raw_stats


def map_stats_to_dataclass(raw_stats: list, ms: MatchStats) -> MatchStats:
    """Rellena MatchStats a partir de la lista de stats crudas."""
    for entry in raw_stats:
        key = entry["category"].strip().lower()
        if key not in STAT_MAP:
            continue
        field_home, field_away, parser = STAT_MAP[key]
        if field_home and parser:
            setattr(ms, field_home, parser(entry["home"], getattr(ms, field_home)))
        if field_away and parser:
            setattr(ms, field_away, parser(entry["away"], getattr(ms, field_away)))
    return ms


def extract_teams_from_html(soup: BeautifulSoup) -> tuple[str, str]:
    """Extrae nombres de equipos directamente del DOM (más fiable que el título)."""
    # Flashscore mobi tiene elementos con clase 'participant' o similares
    for cls in ["participant__participantName", "team", "home-team", "away-team"]:
        els = soup.find_all(class_=lambda c: c and cls in c)
        if len(els) >= 2:
            return els[0].text.strip(), els[1].text.strip()

    # Fallback: buscar el patrón "Equipo A - Equipo B" en el texto visible
    title_el = soup.find("title")
    if title_el:
        title = title_el.text
        if "|" in title:
            part = title.split("|")[-1].strip()
        else:
            part = title.strip()
        if "-" in part:
            parts = part.split("-", 1)
            return parts[0].strip(), parts[1].strip()

    return "Local", "Visitante"


def extract_score_and_minute(soup: BeautifulSoup) -> tuple[int, int, int]:
    """
    Devuelve (score_home, score_away, current_minute).
    Flashscore mobi: busca el marcador en varios selectores posibles.
    """
    score_home = score_away = minute = 0

    # Buscar marcador
    for cls in ["score", "detailScore", "event__score"]:
        el = soup.find(class_=lambda c: c and cls in c)
        if el:
            text = el.text.strip()
            m = re.search(r"(\d+)\s*[:\-]\s*(\d+)", text)
            if m:
                score_home = int(m.group(1))
                score_away = int(m.group(2))
                break

    # También buscar en divs genéricos con patrón "N:N" o "N-N"
    if score_home == 0 and score_away == 0:
        for div in soup.find_all("div"):
            m = re.match(r"^(\d+)\s*[:\-]\s*(\d+)$", div.text.strip())
            if m:
                score_home = int(m.group(1))
                score_away = int(m.group(2))
                break

    # Buscar minuto
    for cls in ["minute", "matchMinute", "event__time", "detail"]:
        els = soup.find_all(class_=lambda c: c and cls in c)
        for el in els:
            m = re.search(r"(\d{1,3})['\+]", el.text)
            if m:
                minute = int(m.group(1))
                break
        if minute:
            break

    # Fallback: buscar patrón "82'" en todo el texto
    if not minute:
        full_text = soup.get_text()
        m = re.search(r"\b(\d{1,3})['′]\s", full_text)
        if m:
            minute = int(m.group(1))

    return score_home, score_away, minute


# ==============================================================================
# SCRAPER PRINCIPAL
# ==============================================================================

async def scrape_match_async(
    url: str,
    output_file: Optional[str] = None,
    headless: bool = True,
    prev_snapshot: Optional[dict] = None,
    arbitro_amarillas: float = 3.5,
    yellow_card_players: Optional[list] = None,
    retries: int = 3,
) -> MatchStats:
    """
    Scraper robusto con reintentos.
    prev_snapshot: dict con fouls_home y fouls_away de hace ~10 min para calcular diffs.
    """
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=headless)
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
                        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                        "Version/16.6 Mobile/15E148 Safari/604.1"
                    ),
                    viewport={"width": 390, "height": 844},
                )
                page = await context.new_page()

                # Bloquear recursos innecesarios para acelerar la carga
                await page.route(
                    "**/*.{png,jpg,jpeg,gif,svg,woff,woff2,ttf}",
                    lambda r: r.abort()
                )

                await page.goto(url, wait_until="networkidle", timeout=30_000)

                # Aceptar cookies si aparece el banner
                try:
                    btn = page.locator("#onetrust-accept-btn-handler")
                    if await btn.is_visible(timeout=3000):
                        await btn.click()
                        await page.wait_for_timeout(500)
                except Exception:
                    pass

                await page.wait_for_timeout(1500)

                html = await page.content()
                await browser.close()

            # ── Parsear HTML ──────────────────────────────────────
            soup = BeautifulSoup(html, "html.parser")
            home_team, away_team = extract_teams_from_html(soup)
            score_home, score_away, minute = extract_score_and_minute(soup)
            raw_stats = extract_stats_from_html(html)

            # ── Construir MatchStats ──────────────────────────────
            ms = MatchStats(
                home_team=home_team,
                away_team=away_team,
                current_minute=minute,
                score_home=score_home,
                score_away=score_away,
                score_diff=score_home - score_away,
                arbitro_amarillas=arbitro_amarillas,
                yellow_card_players=yellow_card_players or [],
            )

            # Histórico para diffs
            if prev_snapshot:
                ms.fouls_home_prev = prev_snapshot.get("fouls_home", 0)
                ms.fouls_away_prev = prev_snapshot.get("fouls_away", 0)

            ms = map_stats_to_dataclass(raw_stats, ms)

            # ── Guardar JSON si se pide ───────────────────────────
            if output_file:
                with open(output_file, "w", encoding="utf-8") as f:
                    json.dump(
                        {"stats": raw_stats, "parsed": asdict(ms)},
                        f, ensure_ascii=False, indent=2
                    )
                print(f"✅ Datos guardados en {output_file}")

            return ms

        except PlaywrightTimeout:
            last_error = f"Timeout en intento {attempt}"
            print(f"⚠️  {last_error}. Reintentando...")
            await asyncio.sleep(2 * attempt)
        except Exception as e:
            last_error = str(e)
            print(f"⚠️  Error en intento {attempt}: {e}. Reintentando...")
            await asyncio.sleep(2 * attempt)

    raise RuntimeError(f"No se pudo scrapear tras {retries} intentos. Último error: {last_error}")


# ==============================================================================
# ANALIZADOR DE TARJETA AMARILLA
# ==============================================================================

def calcular_alerta_tarjeta(ms: MatchStats) -> str:
    """
    Sistema de scoring acumulativo ponderado.
    Devuelve el mensaje de alerta formateado o cadena vacía si no hay alerta.
    """
    minute      = ms.current_minute
    score_diff  = ms.score_diff
    arb_avg     = ms.arbitro_amarillas
    yp          = ms.yellow_card_players

    UMBRAL_ESTRICTO  = 4.5
    UMBRAL_PERMISIVO = 3.0
    arbitro_st   = arb_avg >= UMBRAL_ESTRICTO
    arbitro_perm = arb_avg < UMBRAL_PERMISIVO

    # ── Multiplicador de fase del partido ─────────────────
    if minute < 30:
        phase_mult = 0.8
    elif minute < 60:
        phase_mult = 1.0
    elif minute < 75:
        phase_mult = 1.2
    elif minute < 85:
        phase_mult = 1.5
    else:
        phase_mult = 1.8  # descuento: máxima tensión

    score  = {"home": 0.0, "away": 0.0, "both": 0.0}
    reason = {"home": [],  "away": [],  "both": []}

    def add(team: str, pts: float, text: str):
        score[team]  += pts
        reason[team].append(text)

    # ── Factor 1: Volumen total de faltas ─────────────────
    if ms.total_fouls > 25:
        add("both", 25, f"Partido muy sucio: {ms.total_fouls} faltas acumuladas")
    elif ms.total_fouls > 18:
        add("both", 12, f"Alto volumen de faltas: {ms.total_fouls}")

    # ── Factor 2: Ráfaga reciente (~10 min) ───────────────
    diff_total = ms.diff_fouls_home + ms.diff_fouls_away
    if diff_total > 10:
        add("both", 35, f"Ráfaga: {diff_total} faltas en los últimos ~10 min")
    if ms.diff_fouls_home > 5:
        add("home", 30, f"{ms.home_team}: {ms.diff_fouls_home} faltas en últimos ~10 min")
    if ms.diff_fouls_away > 5:
        add("away", 30, f"{ms.away_team}: {ms.diff_fouls_away} faltas en últimos ~10 min")

    # ── Factor 3: Árbitro ─────────────────────────────────
    if arbitro_st:
        add("both", 25, f"Árbitro estricto: media {arb_avg:.1f} AM/partido")
    elif arbitro_perm and ms.total_fouls > 18:
        add("both", 20,
            f"Árbitro permisivo ({arb_avg:.1f} AM/p) con {ms.total_fouls} faltas → compensación inminente")

    # ── Factor 4: Partido ya muy tarjeteado ───────────────
    if ms.total_cards >= 6:
        add("both", 30, f"Partido en llamas: {ms.total_cards} amarillas ya mostradas")
    elif ms.total_cards >= 4:
        add("both", 15, f"{ms.total_cards} tarjetas ya en el partido")

    # ── Factor 5: Árbitro pistolero temprano ─────────────
    if ms.total_cards >= 2 and minute < 20:
        add("both", 25, f"Pistolero: {ms.total_cards} tarjetas antes del min 20")

    # ── Factor 6: Contexto crítico de marcador ────────────
    if minute > 70 and abs(score_diff) <= 1:
        if score_diff < 0:
            add("home", 35, f"{ms.home_team} perdiendo en min {minute}: desesperación táctica")
        elif score_diff > 0:
            add("away", 35, f"{ms.away_team} perdiendo en min {minute}: desesperación táctica")
        else:
            add("both", 20, f"Empate en min {minute}: presión máxima en ambos")

    # ── Factor 7: Free kicks como proxy de agresividad ────
    # Flashscore = faltas RECIBIDAS por cada equipo
    # Si away recibe más → home está faltando más
    fk_diff = ms.free_kicks_away - ms.free_kicks_home
    if abs(fk_diff) > 6:
        if fk_diff > 0:
            add("home", 20, f"{ms.home_team} provocó {ms.free_kicks_away} faltas al rival (muy agresivo)")
        else:
            add("away", 20, f"{ms.away_team} provocó {ms.free_kicks_home} faltas al rival (muy agresivo)")

    # ── Factor 8: Porcentaje bajo de entradas exitosas ────
    if ms.tackles_home_pct < 40:
        add("home", 15,
            f"{ms.home_team}: solo {ms.tackles_home_pct:.0f}% entradas exitosas → entradas sucias/desesperadas")
    if ms.tackles_away_pct < 40:
        add("away", 15,
            f"{ms.away_team}: solo {ms.tackles_away_pct:.0f}% entradas exitosas → entradas sucias/desesperadas")

    # ── Factor 9: Presión ofensiva del rival ──────────────
    xg_diff = ms.xg_away - ms.xg_home
    if xg_diff > 0.6:
        add("home", 15, f"{ms.home_team} bajo presión intensa (xG rival: {ms.xg_away:.2f})")
    elif xg_diff < -0.6:
        add("away", 15, f"{ms.away_team} bajo presión intensa (xG rival: {ms.xg_home:.2f})")

    if ms.big_chances_away > 0 and score_diff < 0:
        add("home", 10, f"Rival crea {ms.big_chances_away} ocasión(es) clara(s): {ms.home_team} puede desesperarse")
    if ms.big_chances_home > 0 and score_diff > 0:
        add("away", 10, f"Rival crea {ms.big_chances_home} ocasión(es) clara(s): {ms.away_team} puede desesperarse")

    # ── Factor 10: Jugadores en riesgo ────────────────────
    if yp:
        add("both", 15 * len(yp), f"{len(yp)} jugador(es) con 1 amarilla en campo: {', '.join(yp)}")

    # ── Factor 11: Asimetría de duelos ────────────────────
    total_duels = ms.duels_won_home + ms.duels_won_away
    if total_duels > 0:
        duel_pct_home = ms.duels_won_home / total_duels * 100
        if duel_pct_home < 40:
            add("home", 10, f"{ms.home_team} perdiendo el 60%+ de los duelos: puede escalar la agresividad")
        elif duel_pct_home > 60:
            add("away", 10, f"{ms.away_team} perdiendo el 60%+ de los duelos: puede escalar la agresividad")

    # ── Aplicar multiplicador de fase ─────────────────────
    for k in score:
        score[k] = round(score[k] * phase_mult)

    total_home = score["home"] + round(score["both"] * 0.6)
    total_away = score["away"] + round(score["both"] * 0.6)
    total_both = score["both"]
    best = max(total_home, total_away, total_both)

    if best < 30:
        return ""  # Sin alerta significativa

    # Probabilidad: escala no lineal, tope en 95%
    prob = min(int(30 + best * 0.45), 95)

    # ── Foco y razones principales ────────────────────────
    if best == total_both:
        focus      = "AMBOS EQUIPOS"
        top_reason = reason["both"][:3]
    elif total_home >= total_away:
        focus      = ms.home_team
        top_reason = (reason["home"] + reason["both"])[:3]
    else:
        focus      = ms.away_team
        top_reason = (reason["away"] + reason["both"])[:3]

    # ── Motivo principal ──────────────────────────────────
    if ms.total_cards >= 2 and minute < 20:
        motivo = "ÁRBITRO PISTOLERO"
    elif ms.total_cards >= 6:
        motivo = "PARTIDO EN LLAMAS"
    elif minute > 70 and abs(score_diff) <= 1:
        motivo = "CONTEXTO CRÍTICO"
    elif arbitro_perm and ms.total_fouls > 18:
        motivo = "OLLA A PRESIÓN"
    elif ms.diff_fouls_home > 5 or ms.diff_fouls_away > 5:
        motivo = "ACUMULACIÓN ESPECÍFICA"
    else:
        motivo = "FACTORES COMBINADOS"

    # ── Construir mensaje ─────────────────────────────────
    lines = [
        f"⚠️ <b>POSIBLE TARJETA – {focus}</b>",
        f"Probabilidad estimada: <b>{prob}%</b>",
        f"Motivo: <b>{motivo}</b>",
        f"⏱️ Minuto: {minute}'  |  Marcador: {ms.home_team} {ms.score_home}-{ms.score_away} {ms.away_team}",
        "Razones:",
        *[f"• {r}" for r in top_reason],
    ]
    return "\n".join(lines)


# ==============================================================================
# PUNTO DE ENTRADA
# ==============================================================================

async def main():
    parser = argparse.ArgumentParser(description="Scraper + analizador de tarjetas Flashscore")
    parser.add_argument("--url",        required=True,  help="URL de Flashscore mobi (stats)")
    parser.add_argument("--output",     default=None,   help="Archivo JSON de salida (opcional)")
    parser.add_argument("--no-headless",action="store_true", help="Mostrar navegador")
    parser.add_argument("--no-telegram",action="store_true", help="No enviar a Telegram")
    parser.add_argument("--arbitro",    type=float, default=3.5, help="Promedio AM del árbitro")
    parser.add_argument("--jugadores",  default="",
                        help="Jugadores con 1 amarilla, separados por coma")
    parser.add_argument("--prev-fouls", default=None,
                        help='JSON con snapshot previo: \'{"fouls_home":10,"fouls_away":5}\'')
    args = parser.parse_args()

    prev_snapshot = None
    if args.prev_fouls:
        prev_snapshot = json.loads(args.prev_fouls)

    yellow_players = [p.strip() for p in args.jugadores.split(",") if p.strip()]

    print(f"🔍 Scrapeando {args.url} ...")
    ms = await scrape_match_async(
        url=args.url,
        output_file=args.output,
        headless=not args.no_headless,
        prev_snapshot=prev_snapshot,
        arbitro_amarillas=args.arbitro,
        yellow_card_players=yellow_players,
    )

    print(f"\n📊 {ms.home_team} {ms.score_home}-{ms.score_away} {ms.away_team}  ({ms.current_minute}')")
    print(f"   Faltas: {ms.fouls_home} - {ms.fouls_away}")
    print(f"   Tarjetas: {ms.cards_home} - {ms.cards_away}")
    print(f"   xG: {ms.xg_home:.2f} - {ms.xg_away:.2f}")
    print(f"   Entradas: {ms.tackles_home_pct:.0f}% - {ms.tackles_away_pct:.0f}%")

    alerta = calcular_alerta_tarjeta(ms)

    if alerta:
        print(f"\n{alerta}")
        if TELEGRAM_AVAILABLE and not args.no_telegram:
            send_telegram_message(alerta)
            print("📨 Alerta enviada a Telegram.")
    else:
        print("\n✅ Sin alerta de tarjeta en este momento.")


if __name__ == "__main__":
    asyncio.run(main())
