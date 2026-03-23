"""
flashscore_monitor.py
=====================
Sistema integrado de monitoreo en tiempo real de múltiples partidos de Flashscore.
Detecta y notifica por Telegram:
  - ⚽️ Gol inminente
  - ⚠️  Tarjeta amarilla probable

Uso:
    # Un solo partido
    python flashscore_monitor.py --urls "https://www.flashscore.mobi/match/ID/?s=2&t=stats"

    # Varios partidos simultáneos
    python flashscore_monitor.py --urls "URL1" "URL2" "URL3"

    # Con config JSON (recomendado para producción)
    python flashscore_monitor.py --config partidos.json

    # Sin Telegram (solo consola)
    python flashscore_monitor.py --urls "URL1" --no-telegram

Config JSON de ejemplo (partidos.json):
    [
      {
        "url": "https://www.flashscore.mobi/match/ID1/?s=2&t=stats",
        "arbitro": 4.8,
        "jugadores_amarilla": ["Casemiro", "Neymar"]
      },
      {
        "url": "https://www.flashscore.mobi/match/ID2/?s=2&t=stats",
        "arbitro": 3.1
      }
    ]
"""

from __future__ import annotations

import re
import json
import asyncio
import argparse
import logging
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional, Deque

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ── Telegram opcional ─────────────────────────────────────────────────────────
try:
    from telegram_notifier import send_telegram_message
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("monitor")


# ==============================================================================
# CONSTANTES
# ==============================================================================

POLL_INTERVAL   = 60          # segundos entre cada scrape
SNAPSHOT_WINDOW = 10          # minutos que representa el snapshot "previo"
SNAPSHOT_SLOTS  = SNAPSHOT_WINDOW  # guardamos 1 snapshot/min → deque de 10


# ==============================================================================
# MODELOS DE DATOS
# ==============================================================================

@dataclass
class MatchStats:
    home_team:  str   = "Local"
    away_team:  str   = "Visitante"
    current_minute: int = 0
    score_home: int   = 0
    score_away: int   = 0
    score_diff: int   = 0

    cards_home: int   = 0
    cards_away: int   = 0
    fouls_home: int   = 0
    fouls_away: int   = 0
    free_kicks_home: int = 0
    free_kicks_away: int = 0
    tackles_home_pct: float = 50.0
    tackles_away_pct: float = 50.0

    xg_home:  float = 0.0
    xg_away:  float = 0.0
    xgot_home: float = 0.0
    xgot_away: float = 0.0
    xgot_faced_home: float = 0.0
    xgot_faced_away: float = 0.0

    shots_home: int = 0
    shots_away: int = 0
    shots_on_target_home: int = 0
    shots_on_target_away: int = 0
    shots_box_home: int = 0
    shots_box_away: int = 0

    possession_home: float = 50.0
    possession_away: float = 50.0
    passes_ft_home:  float = 0.0
    passes_ft_away:  float = 0.0

    duels_won_home: int = 0
    duels_won_away: int = 0
    corners_home:   int = 0
    corners_away:   int = 0
    big_chances_home: int = 0
    big_chances_away: int = 0
    woodwork_home:  int = 0
    woodwork_away:  int = 0
    touches_box_home: int = 0
    touches_box_away: int = 0
    saves_home: int = 0
    saves_away: int = 0

    # Contexto externo
    arbitro_amarillas:   float = 3.5
    yellow_card_players: list  = field(default_factory=list)

    # Snapshot previo (poblado desde el historial)
    fouls_home_prev:         int   = 0
    fouls_away_prev:         int   = 0
    xg_home_prev:            float = 0.0
    xg_away_prev:            float = 0.0
    possession_home_prev:    float = 50.0
    possession_away_prev:    float = 50.0
    touches_box_home_prev:   int   = 0
    touches_box_away_prev:   int   = 0

    # ── Propiedades derivadas ─────────────────────────────────────────────────
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

    @property
    def xg_rate_home(self) -> float:
        return max(0.0, self.xg_home - self.xg_home_prev)

    @property
    def xg_rate_away(self) -> float:
        return max(0.0, self.xg_away - self.xg_away_prev)

    @property
    def touches_box_rate_home(self) -> int:
        return max(0, self.touches_box_home - self.touches_box_home_prev)

    @property
    def touches_box_rate_away(self) -> int:
        return max(0, self.touches_box_away - self.touches_box_away_prev)


# ==============================================================================
# ESTADO POR PARTIDO (anti-spam + historial de snapshots)
# ==============================================================================

@dataclass
class MatchState:
    """Todo el estado mutable asociado a un partido en seguimiento."""
    url:    str
    arbitro_amarillas:   float = 3.5
    yellow_card_players: list  = field(default_factory=list)

    # Historial de snapshots (deque con los últimos SNAPSHOT_SLOTS)
    history: Deque[MatchStats] = field(default_factory=lambda: deque(maxlen=SNAPSHOT_SLOTS))

    # Anti-spam para goles
    goal_last_team:    str = ""
    goal_last_minute:  int = 0   # minuto en que se disparó la última alerta
    goal_last_sh:      int = -1  # marcador en el momento de la última alerta
    goal_last_sa:      int = -1

    # Anti-spam para tarjetas
    card_last_focus:   str = ""
    card_last_minute:  int = 0
    card_last_score:   int = 0

    # Control de fin de partido
    finished: bool = False

    def prev_snapshot(self) -> Optional[MatchStats]:
        """Snapshot ~10 min atrás (el más antiguo del historial)."""
        return self.history[0] if len(self.history) == self.history.maxlen else None

    def push(self, ms: MatchStats):
        self.history.append(ms)

    # ── Anti-spam gol ─────────────────────────────────────────────────────────
    def goal_should_fire(self, team: str, sh: int, sa: int, minute: int) -> bool:
        """
        Dispara si:
          - Es la primera alerta del partido, O
          - El equipo protagonista cambió, O
          - El marcador cambió desde la última alerta (gol real entre medias), O
          - Han pasado >= 12 min sin cambio (situación muy prolongada)
        """
        if self.goal_last_minute == 0:
            return True
        score_changed = (sh != self.goal_last_sh or sa != self.goal_last_sa)
        team_changed  = (team != self.goal_last_team)
        time_elapsed  = (minute - self.goal_last_minute) >= 12
        return score_changed or team_changed or time_elapsed

    def goal_register(self, team: str, sh: int, sa: int, minute: int):
        self.goal_last_team   = team
        self.goal_last_sh     = sh
        self.goal_last_sa     = sa
        self.goal_last_minute = minute

    # ── Anti-spam tarjeta ─────────────────────────────────────────────────────
    def card_should_fire(self, focus: str, score: int, minute: int) -> bool:
        same = (self.card_last_focus == focus and self.card_last_score == score)
        if same and (minute - self.card_last_minute) < 8:
            return False
        return True

    def card_register(self, focus: str, score: int, minute: int):
        self.card_last_focus  = focus
        self.card_last_score  = score
        self.card_last_minute = minute


# ==============================================================================
# PARSERS DE VALORES
# ==============================================================================

def parse_int(value: str, default: int = 0) -> int:
    if not value:
        return default
    m = re.match(r"(\d+)\s*/\s*\d+", value.strip())
    if m:
        return int(m.group(1))
    m = re.match(r"(\d+)\s*%", value.strip())
    if m:
        return int(m.group(1))
    m = re.match(r"(\d+)", value.strip())
    if m:
        return int(m.group(1))
    return default


def parse_float(value: str, default: float = 0.0) -> float:
    if not value:
        return default
    m = re.search(r"(\d+(?:\.\d+)?)", value.strip())
    if m:
        return float(m.group(1))
    return default


def parse_percentage(value: str, default: float = 50.0) -> float:
    if not value:
        return default
    m = re.match(r"(\d+(?:\.\d+)?)\s*%", value.strip())
    if m:
        return float(m.group(1))
    return default


# ==============================================================================
# MAPA STAT → CAMPO
# ==============================================================================

STAT_MAP = {
    "yellow cards":                    ("cards_home",           "cards_away",           parse_int),
    "fouls":                           ("fouls_home",           "fouls_away",           parse_int),
    "free kicks":                      ("free_kicks_home",      "free_kicks_away",      parse_int),
    "tackles":                         ("tackles_home_pct",     "tackles_away_pct",     parse_percentage),
    "expected goals (xg)":             ("xg_home",              "xg_away",              parse_float),
    "xg on target (xgot)":             ("xgot_home",            "xgot_away",            parse_float),
    "expected goals on target (xgot)": ("xgot_home",            "xgot_away",            parse_float),
    "xgot faced":                      ("xgot_faced_home",      "xgot_faced_away",      parse_float),
    "total shots":                     ("shots_home",           "shots_away",           parse_int),
    "shots on target":                 ("shots_on_target_home", "shots_on_target_away", parse_int),
    "shots inside the box":            ("shots_box_home",       "shots_box_away",       parse_int),
    "shots inside box":                ("shots_box_home",       "shots_box_away",       parse_int),
    "ball possession":                 ("possession_home",      "possession_away",      parse_percentage),
    "passes in final third":           ("passes_ft_home",       "passes_ft_away",       parse_percentage),
    "duels won":                       ("duels_won_home",       "duels_won_away",       parse_int),
    "corner kicks":                    ("corners_home",         "corners_away",         parse_int),
    "big chances":                     ("big_chances_home",     "big_chances_away",     parse_int),
    "hit woodwork":                    ("woodwork_home",        "woodwork_away",        parse_int),
    "touches in opposition box":       ("touches_box_home",     "touches_box_away",     parse_int),
    "goalkeeper saves":                ("saves_home",           "saves_away",           parse_int),
}


# ==============================================================================
# EXTRACCIÓN HTML
# ==============================================================================

def extract_stats_from_html(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    raw = []
    rows = soup.find_all("div", class_=lambda c: c and "wcl-row" in c)
    for row in rows:
        texts = [d.text.strip() for d in row.find_all("div") if d.text.strip()]
        if len(texts) < 3:
            continue
        for i in range(len(texts) - 2):
            home_val, stat_name, away_val = texts[i], texts[i+1], texts[i+2]
            if not re.search(r"[a-zA-Z]{3,}", stat_name):
                continue
            if re.match(r"^\d", stat_name):
                continue
            raw.append({"category": stat_name, "home": home_val, "away": away_val})
            break
    return raw


def extract_teams(soup: BeautifulSoup) -> tuple[str, str]:
    for cls in ["participant__participantName", "team", "home-team", "away-team"]:
        els = soup.find_all(class_=lambda c: c and cls in c)
        if len(els) >= 2:
            return els[0].text.strip(), els[1].text.strip()
    title_el = soup.find("title")
    if title_el:
        part = title_el.text.split("|")[-1].strip() if "|" in title_el.text else title_el.text.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            return a.strip(), b.strip()
    return "Local", "Visitante"


def extract_score_and_minute(soup: BeautifulSoup) -> tuple[int, int, int]:
    sh = sa = minute = 0
    for cls in ["score", "detailScore", "event__score"]:
        el = soup.find(class_=lambda c: c and cls in c)
        if el:
            m = re.search(r"(\d+)\s*[:\-]\s*(\d+)", el.text)
            if m:
                sh, sa = int(m.group(1)), int(m.group(2))
                break
    if sh == 0 and sa == 0:
        for div in soup.find_all("div"):
            m = re.match(r"^(\d+)\s*[:\-]\s*(\d+)$", div.text.strip())
            if m:
                sh, sa = int(m.group(1)), int(m.group(2))
                break
    for cls in ["minute", "matchMinute", "event__time", "detail"]:
        for el in soup.find_all(class_=lambda c: c and cls in c):
            m = re.search(r"(\d{1,3})['\+]", el.text)
            if m:
                minute = int(m.group(1))
                break
        if minute:
            break
    if not minute:
        m = re.search(r"\b(\d{1,3})['′]\s", soup.get_text())
        if m:
            minute = int(m.group(1))
    return sh, sa, minute


def build_match_stats(html: str, state: MatchState) -> MatchStats:
    soup  = BeautifulSoup(html, "html.parser")
    home, away = extract_teams(soup)
    sh, sa, minute = extract_score_and_minute(soup)

    ms = MatchStats(
        home_team=home, away_team=away,
        current_minute=minute,
        score_home=sh, score_away=sa,
        score_diff=sh - sa,
        arbitro_amarillas=state.arbitro_amarillas,
        yellow_card_players=state.yellow_card_players,
    )

    # Rellenar desde HTML
    for entry in extract_stats_from_html(html):
        key = entry["category"].strip().lower()
        if key not in STAT_MAP:
            continue
        fh, fa, parser = STAT_MAP[key]
        if fh and parser:
            setattr(ms, fh, parser(entry["home"], getattr(ms, fh)))
        if fa and parser:
            setattr(ms, fa, parser(entry["away"], getattr(ms, fa)))

    # Rellenar desde snapshot previo
    prev = state.prev_snapshot()
    if prev:
        ms.fouls_home_prev          = prev.fouls_home
        ms.fouls_away_prev          = prev.fouls_away
        ms.xg_home_prev             = prev.xg_home
        ms.xg_away_prev             = prev.xg_away
        ms.possession_home_prev     = prev.possession_home
        ms.possession_away_prev     = prev.possession_away
        ms.touches_box_home_prev    = prev.touches_box_home
        ms.touches_box_away_prev    = prev.touches_box_away

    return ms


# ==============================================================================
# ANALIZADOR DE TARJETA AMARILLA
# ==============================================================================

def calcular_alerta_tarjeta(ms: MatchStats) -> tuple[str, str, int]:
    """
    Devuelve (mensaje, focus, score_total).
    """
    minute     = ms.current_minute
    score_diff = ms.score_diff
    arb_avg    = ms.arbitro_amarillas
    yp         = ms.yellow_card_players

    arbitro_st   = arb_avg >= 4.5
    arbitro_perm = arb_avg < 3.0

    if minute < 30:       phase_mult = 0.8
    elif minute < 60:     phase_mult = 1.0
    elif minute < 75:     phase_mult = 1.2
    elif minute < 85:     phase_mult = 1.5
    else:                 phase_mult = 1.8

    score  = {"home": 0.0, "away": 0.0, "both": 0.0}
    reason = {"home": [],  "away": [],  "both": []}

    def add(team, pts, text):
        score[team] += pts
        reason[team].append(text)

    if ms.total_fouls > 25:
        add("both", 25, f"Partido muy sucio: {ms.total_fouls} faltas")
    elif ms.total_fouls > 18:
        add("both", 12, f"Alto volumen de faltas: {ms.total_fouls}")

    diff_total = ms.diff_fouls_home + ms.diff_fouls_away
    if diff_total > 10:
        add("both", 35, f"Ráfaga: {diff_total} faltas en ~10 min")
    if ms.diff_fouls_home > 5:
        add("home", 30, f"{ms.home_team}: {ms.diff_fouls_home} faltas en ~10 min")
    if ms.diff_fouls_away > 5:
        add("away", 30, f"{ms.away_team}: {ms.diff_fouls_away} faltas en ~10 min")

    if arbitro_st:
        add("both", 25, f"Árbitro estricto: media {arb_avg:.1f} AM/partido")
    elif arbitro_perm and ms.total_fouls > 18:
        add("both", 20, f"Árbitro permisivo ({arb_avg:.1f}) con {ms.total_fouls} faltas → compensación")

    if ms.total_cards >= 6:
        add("both", 30, f"Partido en llamas: {ms.total_cards} amarillas")
    elif ms.total_cards >= 4:
        add("both", 15, f"{ms.total_cards} tarjetas en el partido")
    if ms.total_cards >= 2 and minute < 20:
        add("both", 25, f"Pistolero: {ms.total_cards} tarjetas antes del min 20")

    if minute > 70 and abs(score_diff) <= 1:
        if score_diff < 0:
            add("home", 35, f"{ms.home_team} perdiendo en min {minute}: desesperación")
        elif score_diff > 0:
            add("away", 35, f"{ms.away_team} perdiendo en min {minute}: desesperación")
        else:
            add("both", 20, f"Empate en min {minute}: presión máxima")

    fk_diff = ms.free_kicks_away - ms.free_kicks_home
    if abs(fk_diff) > 6:
        if fk_diff > 0:
            add("home", 20, f"{ms.home_team} muy agresivo: {ms.free_kicks_away} faltas al rival")
        else:
            add("away", 20, f"{ms.away_team} muy agresivo: {ms.free_kicks_home} faltas al rival")

    if ms.tackles_home_pct < 40:
        add("home", 15, f"{ms.home_team}: {ms.tackles_home_pct:.0f}% entradas exitosas → entradas sucias")
    if ms.tackles_away_pct < 40:
        add("away", 15, f"{ms.away_team}: {ms.tackles_away_pct:.0f}% entradas exitosas → entradas sucias")

    xg_diff = ms.xg_away - ms.xg_home
    if xg_diff > 0.6:
        add("home", 15, f"{ms.home_team} bajo presión ofensiva (xG rival: {ms.xg_away:.2f})")
    elif xg_diff < -0.6:
        add("away", 15, f"{ms.away_team} bajo presión ofensiva (xG rival: {ms.xg_home:.2f})")

    if yp:
        add("both", 15 * len(yp), f"{len(yp)} jugador(es) con amarilla: {', '.join(yp)}")

    total_duels = ms.duels_won_home + ms.duels_won_away
    if total_duels > 0:
        pct = ms.duels_won_home / total_duels * 100
        if pct < 40:
            add("home", 10, f"{ms.home_team} pierde el 60%+ de los duelos")
        elif pct > 60:
            add("away", 10, f"{ms.away_team} pierde el 60%+ de los duelos")

    for k in score:
        score[k] = round(score[k] * phase_mult)

    total_home = score["home"] + round(score["both"] * 0.6)
    total_away = score["away"] + round(score["both"] * 0.6)
    total_both = score["both"]
    best = max(total_home, total_away, total_both)

    if best < 30:
        return "", "", 0

    prob = min(int(30 + best * 0.45), 95)

    if best == total_both:
        focus      = "AMBOS EQUIPOS"
        top_reason = reason["both"][:3]
    elif total_home >= total_away:
        focus      = ms.home_team
        top_reason = (reason["home"] + reason["both"])[:3]
    else:
        focus      = ms.away_team
        top_reason = (reason["away"] + reason["both"])[:3]

    if ms.total_cards >= 2 and minute < 20:      motivo = "ÁRBITRO PISTOLERO"
    elif ms.total_cards >= 6:                    motivo = "PARTIDO EN LLAMAS"
    elif minute > 70 and abs(score_diff) <= 1:   motivo = "CONTEXTO CRÍTICO"
    elif arbitro_perm and ms.total_fouls > 18:   motivo = "OLLA A PRESIÓN"
    elif ms.diff_fouls_home > 5 or ms.diff_fouls_away > 5: motivo = "ACUMULACIÓN ESPECÍFICA"
    else:                                        motivo = "FACTORES COMBINADOS"

    msg = "\n".join([
        f"⚠️ <b>POSIBLE TARJETA – {focus}</b>",
        f"Probabilidad: <b>{prob}%</b>  |  Motivo: <b>{motivo}</b>",
        f"⏱️ {minute}'  |  {ms.home_team} {ms.score_home}-{ms.score_away} {ms.away_team}",
        "Razones:",
        *[f"• {r}" for r in top_reason],
    ])
    return msg, focus, int(best)


# ==============================================================================
# ANALIZADOR DE GOL INMINENTE
# ==============================================================================

def _eval_side(ms: MatchStats, side: str, phase_mult: float) -> tuple[float, list[str]]:
    sc = 0.0
    rs = []

    if side == "home":
        xg, xg_r     = ms.xg_home, ms.xg_away
        xgot, xgot_r = ms.xgot_home, ms.xgot_away
        bc, bc_r      = ms.big_chances_home, ms.big_chances_away
        sb, sb_r      = ms.shots_box_home, ms.shots_box_away
        tb, tb_r      = ms.touches_box_home, ms.touches_box_away
        co, co_r      = ms.corners_home, ms.corners_away
        ww            = ms.woodwork_home
        poss, pp      = ms.possession_home, ms.possession_home_prev
        pft, pft_r    = ms.passes_ft_home, ms.passes_ft_away
        sv_r          = ms.saves_away
        xgf_r         = ms.xgot_faced_away
        xg_rate       = ms.xg_rate_home
        tb_rate        = ms.touches_box_rate_home
        team          = ms.home_team
        losing        = ms.score_diff < 0
    else:
        xg, xg_r     = ms.xg_away, ms.xg_home
        xgot, xgot_r = ms.xgot_away, ms.xgot_home
        bc, bc_r      = ms.big_chances_away, ms.big_chances_home
        sb, sb_r      = ms.shots_box_away, ms.shots_box_home
        tb, tb_r      = ms.touches_box_away, ms.touches_box_home
        co, co_r      = ms.corners_away, ms.corners_home
        ww            = ms.woodwork_away
        poss, pp      = ms.possession_away, ms.possession_away_prev
        pft, pft_r    = ms.passes_ft_away, ms.passes_ft_home
        sv_r          = ms.saves_home
        xgf_r         = ms.xgot_faced_home
        xg_rate       = ms.xg_rate_away
        tb_rate        = ms.touches_box_rate_away
        team          = ms.away_team
        losing        = ms.score_diff > 0

    xg_d   = xg   - xg_r
    xgot_d = xgot - xgot_r

    # xG
    if xg_d >= 1.0:
        sc += 25; rs.append(f"Ventaja crítica de xG ({xg:.2f} vs {xg_r:.2f})")
    elif xg_d >= 0.7:
        sc += 15; rs.append(f"Brecha de xG ({xg:.2f} vs {xg_r:.2f})")

    if xgot_d >= 0.8:
        sc += 20; rs.append(f"xGOT muy alto ({xgot:.2f} vs {xgot_r:.2f})")
    elif xgot_d >= 0.5:
        sc += 10; rs.append(f"xGOT favorece a {team} ({xgot:.2f})")

    # Ritmo xG reciente
    if xg_rate >= 0.4:
        sc += 20; rs.append(f"Aceleración ofensiva alta (+{xg_rate:.2f} xG en ~10 min)")
    elif xg_rate >= 0.2:
        sc += 10; rs.append(f"Ritmo ofensivo creciente (+{xg_rate:.2f} xG en ~10 min)")

    # Big chances
    bc_d = bc - bc_r
    if bc >= 3:
        sc += 25; rs.append(f"{bc} big chances creadas")
    elif bc_d >= 2:
        sc += 15; rs.append(f"Superioridad en big chances: {bc} vs {bc_r}")
    elif bc >= 1 and bc_d >= 1:
        sc += 8;  rs.append(f"Ventaja en big chances ({bc} vs {bc_r})")

    # Dominio del área
    if (sb - sb_r) >= 4 and (tb - tb_r) >= 10:
        sc += 25; rs.append(f"Dominio total del área: {tb} toques, {sb} tiros")
    elif (sb - sb_r) >= 4:
        sc += 15; rs.append(f"Superioridad en tiros al área: {sb} vs {sb_r}")
    elif (tb - tb_r) >= 10:
        sc += 12; rs.append(f"Alta presencia en área rival: {tb} toques")

    if tb_rate >= 8:
        sc += 15; rs.append(f"Asedio reciente al área ({tb_rate} toques en ~10 min)")
    elif tb_rate >= 5:
        sc += 8;  rs.append(f"Más presencia en área en ~10 min (+{tb_rate} toques)")

    # Corners + palos
    if ww > 0 and co >= 4:
        sc += 20; rs.append(f"Asedio: {co} corners y {ww} palo(s)/larguero(s)")
    elif ww > 0:
        sc += 12; rs.append(f"{ww} palo(s) tocados → gol rozado")
    elif (co - co_r) >= 5:
        sc += 10; rs.append(f"Dominio en balón parado: {co} corners vs {co_r}")
    elif co >= 5:
        sc += 7;  rs.append(f"{co} corners a favor")

    # Portero rival exigido
    if sv_r >= 5 or xgf_r > 2.0:
        sc += 20; rs.append(f"Portero rival al límite: {sv_r} paradas, {xgf_r:.2f} xGOT")
    elif sv_r >= 3 or xgf_r > 1.2:
        sc += 10; rs.append(f"Portero rival muy exigido: {sv_r} paradas")

    # Pases último tercio
    if pft >= 75 and (pft - pft_r) >= 15:
        sc += 12; rs.append(f"Precisión en último tercio: {pft:.0f}%")
    elif pft >= 70:
        sc += 6;  rs.append(f"Alta precisión en zona de ataque ({pft:.0f}%)")

    # Posesión: solo suma si hay al menos UN indicador ofensivo real
    has_offensive_indicator = (xg_d >= 0.5 or xgot >= 0.8 or bc >= 1 or sb >= 3 or co >= 4)
    if has_offensive_indicator:
        if poss >= 65 and poss >= pp:
            sc += 10; rs.append(f"Dominio territorial creciente ({poss:.0f}% posesión)")
        elif poss >= 60 and poss >= pp:
            sc += 5;  rs.append(f"Posesión alta sostenida ({poss:.0f}%)")

    # Bonus sinergia
    siege = sum([xgot >= 1.0, bc >= 2, tb >= 15, co >= 4, ww > 0])
    if siege >= 4:
        sc += 20; rs.append(f"Asedio perfecto: {siege}/5 indicadores activos")
    elif siege >= 3:
        sc += 10; rs.append(f"Múltiples indicadores ofensivos activos ({siege}/5)")

    # Urgencia (equipo perdiendo)
    if losing and ms.current_minute > 60:
        sc *= 1.15
        rs.append(f"{team} perdiendo en min {ms.current_minute}: urgencia ofensiva")

    return round(sc * phase_mult), rs


def calcular_alerta_gol(ms: MatchStats) -> tuple[str, str, int]:
    """
    Devuelve (mensaje, winner_team, score_ganador).
    """
    minute = ms.current_minute
    if not (10 <= minute <= 95):
        return "", "", 0

    if minute < 30:       pm = 0.85
    elif minute < 60:     pm = 1.0
    elif minute < 75:     pm = 1.1
    elif minute < 85:     pm = 1.2
    else:                 pm = 1.3

    MIN_SCORE = 80.0   # subido de 55 → exige combinación real de factores

    sh, rh = _eval_side(ms, "home", pm)
    sa, ra = _eval_side(ms, "away", pm)

    if sh >= MIN_SCORE and sh >= sa:
        wteam, wscore, wreasons, wside = ms.home_team, sh, rh, "home"
    elif sa >= MIN_SCORE and sa > sh:
        wteam, wscore, wreasons, wside = ms.away_team, sa, ra, "away"
    else:
        return "", "", 0

    prob = min(int(60 + (wscore - MIN_SCORE) * 0.30), 95)

    siege = sum([
        (ms.woodwork_home if wside=="home" else ms.woodwork_away) > 0,
        (ms.corners_home  if wside=="home" else ms.corners_away)  > 3,
        (ms.big_chances_home if wside=="home" else ms.big_chances_away) > 1,
    ])
    if siege == 3:
        prob = max(prob, 85)

    fire = "🔥 " if prob >= 85 else ""

    xg_lead = (ms.xg_home - ms.xg_away) if wside=="home" else (ms.xg_away - ms.xg_home)
    xgot_v  = ms.xgot_home if wside=="home" else ms.xgot_away
    bc_v    = ms.big_chances_home if wside=="home" else ms.big_chances_away
    ww_v    = ms.woodwork_home if wside=="home" else ms.woodwork_away

    if ww_v > 0 and bc_v >= 2:       motivo = "ASEDIO TOTAL"
    elif xg_lead >= 1.0:             motivo = "SUPERIORIDAD xG CRÍTICA"
    elif bc_v >= 3:                  motivo = "LLUVIA DE BIG CHANCES"
    elif xgot_v > 1.5:               motivo = "TIROS MORTALES"
    elif minute > 80:                motivo = "PRESIÓN FINAL"
    else:                            motivo = "DOMINIO ACUMULADO"

    msg = "\n".join([
        f"{fire}⚽️ <b>GOL INMINENTE – {wteam}</b>",
        f"📈 Probabilidad: <b>{prob}%</b>  |  Motivo: <b>{motivo}</b>",
        f"⏱️ {minute}'  |  <b>{ms.home_team}</b> {ms.score_home}-{ms.score_away} <b>{ms.away_team}</b>",
        "📊 Razones:",
        *[f"• {r}" for r in wreasons[:4]],
    ])
    return msg, wteam, int(wscore)


# ==============================================================================
# SCRAPER
# ==============================================================================

async def scrape(url: str, headless: bool = True, retries: int = 3) -> Optional[str]:
    """Devuelve el HTML de la página o None si falla."""
    for attempt in range(1, retries + 1):
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=headless)
                ctx = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
                        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                        "Version/16.6 Mobile/15E148 Safari/604.1"
                    ),
                    viewport={"width": 390, "height": 844},
                )
                page = await ctx.new_page()
                await page.route("**/*.{png,jpg,jpeg,gif,svg,woff,woff2,ttf}", lambda r: r.abort())
                await page.goto(url, wait_until="networkidle", timeout=30_000)
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
                return html
        except PlaywrightTimeout:
            log.warning(f"Timeout intento {attempt} → {url}")
        except Exception as e:
            log.warning(f"Error intento {attempt} → {url}: {e}")
        await asyncio.sleep(2 * attempt)
    return None


# ==============================================================================
# LOOP DE UN PARTIDO
# ==============================================================================

async def monitor_match(state: MatchState, headless: bool, telegram: bool):
    """
    Corre indefinidamente hasta que el partido termina o falla demasiadas veces.
    """
    label  = state.url.split("/match/")[-1].split("/")[0]  # ID corto para logs
    errors = 0

    log.info(f"[{label}] Iniciando monitoreo → {state.url}")

    while not state.finished:
        html = await scrape(state.url, headless=headless)

        if html is None:
            errors += 1
            log.error(f"[{label}] Fallo de scrape #{errors}")
            if errors >= 5:
                log.error(f"[{label}] Demasiados errores. Deteniendo.")
                break
            await asyncio.sleep(POLL_INTERVAL)
            continue

        errors = 0
        ms = build_match_stats(html, state)
        state.push(ms)

        minute = ms.current_minute
        label_match = f"{ms.home_team} {ms.score_home}-{ms.score_away} {ms.away_team}"
        log.info(f"[{label}] {label_match} ({minute}')")

        # Detectar partido terminado (min 0 después de haber arrancado = FT)
        if len(state.history) > 5 and minute == 0:
            log.info(f"[{label}] Partido finalizado. Deteniendo monitoreo.")
            state.finished = True
            break

        alerts = []

        # ── Alerta de gol ──────────────────────────────────────────────────
        msg_gol, wteam, wscore = calcular_alerta_gol(ms)
        if msg_gol:
            if state.goal_should_fire(wteam, ms.score_home, ms.score_away, minute):
                state.goal_register(wteam, ms.score_home, ms.score_away, minute)
                msg_gol += f"\n<a href='{state.url}'>Ver en Flashscore</a>"
                alerts.append(msg_gol)
                log.info(f"[{label}] ⚽️ ALERTA GOL → {wteam}")

        # ── Alerta de tarjeta ──────────────────────────────────────────────
        msg_card, focus, cscore = calcular_alerta_tarjeta(ms)
        if msg_card:
            if state.card_should_fire(focus, cscore, minute):
                state.card_register(focus, cscore, minute)
                msg_card += f"\n<a href='{state.url}'>Ver en Flashscore</a>"
                alerts.append(msg_card)
                log.info(f"[{label}] ⚠️  ALERTA TARJETA → {focus}")

        # ── Enviar ────────────────────────────────────────────────────────
        if alerts:
            full_msg = "\n\n".join(alerts)
            print(f"\n{'='*60}\n{full_msg}\n{'='*60}\n")
            if telegram and TELEGRAM_AVAILABLE:
                try:
                    send_telegram_message(full_msg)
                except Exception as e:
                    log.error(f"[{label}] Error Telegram: {e}")

        await asyncio.sleep(POLL_INTERVAL)


# ==============================================================================
# PUNTO DE ENTRADA
# ==============================================================================

async def main():
    parser = argparse.ArgumentParser(description="Monitor multi-partido Flashscore")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--urls",   nargs="+", help="URLs de partidos a monitorear")
    group.add_argument("--config", help="JSON con lista de partidos y configuración")

    parser.add_argument("--no-headless", action="store_true")
    parser.add_argument("--no-telegram", action="store_true")
    parser.add_argument("--interval",    type=int, default=POLL_INTERVAL,
                        help=f"Segundos entre scrapes (default: {POLL_INTERVAL})")
    args = parser.parse_args()

    global POLL_INTERVAL
    POLL_INTERVAL = args.interval

    # ── Construir lista de MatchState ──────────────────────────────────────
    states: list[MatchState] = []

    if args.config:
        with open(args.config, encoding="utf-8") as f:
            configs = json.load(f)
        for c in configs:
            states.append(MatchState(
                url=c["url"],
                arbitro_amarillas=c.get("arbitro", 3.5),
                yellow_card_players=c.get("jugadores_amarilla", []),
            ))
    else:
        for url in args.urls:
            states.append(MatchState(url=url))

    headless = not args.no_headless
    telegram  = not args.no_telegram

    if not TELEGRAM_AVAILABLE and telegram:
        log.warning("telegram_notifier no disponible. Las alertas solo se mostrarán en consola.")

    log.info(f"Monitoreando {len(states)} partido(s). Intervalo: {POLL_INTERVAL}s")

    # ── Lanzar todos los partidos en paralelo ──────────────────────────────
    tasks = [monitor_match(s, headless, telegram) for s in states]
    await asyncio.gather(*tasks)

    log.info("Todos los partidos han finalizado.")


if __name__ == "__main__":
    asyncio.run(main())
