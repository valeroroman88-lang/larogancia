"""
goal_alert.py
=============
Analizador de probabilidad de gol inminente en partidos en vivo.
Integrado con el sistema de MatchStats del match_analyzer.py

Uso independiente:
    from goal_alert import calcular_alerta_gol, GoalMemory
    from match_analyzer import MatchStats

    memory = GoalMemory()   # instanciar UNA vez por partido, fuera del loop
    alerta = calcular_alerta_gol(ms, ms_prev, memory, url_flashscore="...")
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ==============================================================================
# MEMORIA ANTI-SPAM (reemplaza _goal_memory en match_data)
# ==============================================================================

@dataclass
class GoalMemory:
    """
    Evita re-enviar la misma alerta si el score no ha cambiado.
    Instanciar una sola vez por partido, fuera del loop de polling.
    """
    last_team:  str = ""
    last_score: int = 0          # score acumulativo que disparó la última alerta
    last_minute: int = 0

    def should_fire(self, team: str, score: int, minute: int) -> bool:
        """Devuelve True solo si es una situación nueva."""
        same = (self.last_team == team and self.last_score == score)
        # Aunque sea el mismo equipo, si han pasado >5 min la situación evolucionó
        if same and (minute - self.last_minute) < 5:
            return False
        return True

    def register(self, team: str, score: int, minute: int):
        self.last_team   = team
        self.last_score  = score
        self.last_minute = minute


# ==============================================================================
# MODELO EXTENDIDO DE STATS PARA GOL
# (campos que MatchStats no tiene todavía)
# ==============================================================================

@dataclass
class GoalStats:
    """
    Extiende MatchStats con los campos específicos para analizar peligro de gol.
    Se puede construir desde MatchStats + los campos extra del scraper.
    """
    # Identidad
    home_team:    str = "Local"
    away_team:    str = "Visitante"
    current_minute: int = 0
    score_home:   int = 0
    score_away:   int = 0
    score_diff:   int = 0         # home - away
    match_time:   str = ""        # texto "82'" para el mensaje

    # xG
    xg_home:      float = 0.0
    xg_away:      float = 0.0
    xgot_home:    float = 0.0     # xG on target
    xgot_away:    float = 0.0

    # Peligro ofensivo
    big_chances_home:     int   = 0
    big_chances_away:     int   = 0
    shots_box_home:       int   = 0    # tiros dentro del área
    shots_box_away:       int   = 0
    touches_box_home:     int   = 0    # toques en área rival
    touches_box_away:     int   = 0
    woodwork_home:        int   = 0    # palos
    woodwork_away:        int   = 0

    # Posesión y pases
    corners_home:         int   = 0
    corners_away:         int   = 0
    possession_home:      float = 50.0
    possession_away:      float = 50.0
    passes_ft_home:       float = 0.0  # % pases en último tercio
    passes_ft_away:       float = 0.0

    # Porteros
    saves_home:           int   = 0
    saves_away:           int   = 0
    xgot_faced_home:      float = 0.0  # xGOT que ha tenido que parar el portero local
    xgot_faced_away:      float = 0.0

    # Snapshot ~10 min atrás (para detectar tendencia)
    xg_home_prev:         float = 0.0
    xg_away_prev:         float = 0.0
    possession_home_prev: float = 50.0
    possession_away_prev: float = 50.0
    touches_box_home_prev: int  = 0
    touches_box_away_prev: int  = 0

    @property
    def xg_rate_home(self) -> float:
        """xG generado en los últimos ~10 min."""
        return max(0.0, self.xg_home - self.xg_home_prev)

    @property
    def xg_rate_away(self) -> float:
        return max(0.0, self.xg_away - self.xg_away_prev)

    @property
    def touches_box_rate_home(self) -> int:
        """Toques en área en los últimos ~10 min."""
        return max(0, self.touches_box_home - self.touches_box_home_prev)

    @property
    def touches_box_rate_away(self) -> int:
        return max(0, self.touches_box_away - self.touches_box_away_prev)


# ==============================================================================
# ANALIZADOR PRINCIPAL
# ==============================================================================

def _evaluate_team(
    gs: GoalStats,
    side: str,           # "home" o "away"
    phase_mult: float,
) -> tuple[float, list[str]]:
    """
    Evalúa el peligro ofensivo de UN equipo.
    Devuelve (score_ponderado, lista_de_razones).
    """
    score   = 0.0
    reasons = []

    if side == "home":
        team        = gs.home_team
        rival       = gs.away_team
        xg          = gs.xg_home
        xg_r        = gs.xg_away
        xgot        = gs.xgot_home
        xgot_r      = gs.xgot_away
        bc          = gs.big_chances_home
        bc_r        = gs.big_chances_away
        shots_box   = gs.shots_box_home
        shots_box_r = gs.shots_box_away
        touches     = gs.touches_box_home
        touches_r   = gs.touches_box_away
        corners     = gs.corners_home
        corners_r   = gs.corners_away
        woodwork    = gs.woodwork_home
        poss        = gs.possession_home
        poss_prev   = gs.possession_home_prev
        passes_ft   = gs.passes_ft_home
        passes_ft_r = gs.passes_ft_away
        saves_rival = gs.saves_away       # paradas del portero RIVAL
        xgot_rival  = gs.xgot_faced_away  # xGOT que ha enfrentado el portero rival
        xg_rate     = gs.xg_rate_home
        t_rate      = gs.touches_box_rate_home
    else:
        team        = gs.away_team
        rival       = gs.home_team
        xg          = gs.xg_away
        xg_r        = gs.xg_home
        xgot        = gs.xgot_away
        xgot_r      = gs.xgot_home
        bc          = gs.big_chances_away
        bc_r        = gs.big_chances_home
        shots_box   = gs.shots_box_away
        shots_box_r = gs.shots_box_home
        touches     = gs.touches_box_away
        touches_r   = gs.touches_box_home
        corners     = gs.corners_away
        corners_r   = gs.corners_home
        woodwork    = gs.woodwork_away
        poss        = gs.possession_away
        poss_prev   = gs.possession_away_prev
        passes_ft   = gs.passes_ft_away
        passes_ft_r = gs.passes_ft_home
        saves_rival = gs.saves_home
        xgot_rival  = gs.xgot_faced_home
        xg_rate     = gs.xg_rate_away
        t_rate      = gs.touches_box_rate_away

    xg_diff   = xg   - xg_r
    xgot_diff = xgot - xgot_r

    # ── Regla 1: Brecha de xG / xGOT ─────────────────────────────────────────
    if xg_diff >= 1.0:
        score += 25
        reasons.append(f"Ventaja crítica de xG ({xg:.2f} vs {xg_r:.2f}, +{xg_diff:.2f})")
    elif xg_diff >= 0.7:
        score += 15
        reasons.append(f"Brecha significativa de xG ({xg:.2f} vs {xg_r:.2f})")

    if xgot_diff >= 0.8:
        score += 20
        reasons.append(f"xGOT a favor muy alto ({xgot:.2f} vs {xgot_r:.2f}) → tiros peligrosos reales")
    elif xgot_diff >= 0.5:
        score += 10
        reasons.append(f"xGOT favorece a {team} ({xgot:.2f} vs {xgot_r:.2f})")

    # ── Regla 2: Ritmo xG en últimos ~10 min ─────────────────────────────────
    if xg_rate >= 0.4:
        score += 20
        reasons.append(f"Ritmo de xG muy alto en los últimos ~10 min (+{xg_rate:.2f})")
    elif xg_rate >= 0.2:
        score += 10
        reasons.append(f"Aceleración ofensiva reciente (+{xg_rate:.2f} xG en ~10 min)")

    # ── Regla 3: Big chances ──────────────────────────────────────────────────
    bc_diff = bc - bc_r
    if bc >= 3:
        score += 25
        reasons.append(f"{bc} ocasiones claras creadas (big chances)")
    elif bc_diff >= 2:
        score += 15
        reasons.append(f"Superioridad en big chances: {bc} vs {bc_r}")
    elif bc >= 1 and bc_diff >= 1:
        score += 8
        reasons.append(f"Ventaja en ocasiones claras ({bc} vs {bc_r})")

    # ── Regla 4: Dominio del área (tiros + toques) ────────────────────────────
    shots_diff  = shots_box  - shots_box_r
    touches_diff = touches   - touches_r

    if shots_diff >= 4 and touches_diff >= 10:
        score += 25   # Ambos indicadores = dominio total
        reasons.append(f"Dominio total del área: {touches} toques, {shots_box} tiros dentro")
    elif shots_diff >= 4:
        score += 15
        reasons.append(f"Superioridad en tiros al área: {shots_box} vs {shots_box_r}")
    elif touches_diff >= 10:
        score += 12
        reasons.append(f"Mucha presencia en el área rival: {touches} toques vs {touches_r}")

    # Toques recientes en el área (tendencia)
    if t_rate >= 8:
        score += 15
        reasons.append(f"Asedio reciente al área ({t_rate} toques en ~10 min)")
    elif t_rate >= 5:
        score += 8
        reasons.append(f"Incremento de presencia en área en ~10 min (+{t_rate} toques)")

    # ── Regla 5: Corners + palos (asedio a balón parado) ─────────────────────
    corner_diff = corners - corners_r
    if woodwork > 0 and corners >= 4:
        score += 20   # Palo + muchos corners = presión real
        reasons.append(f"Asedio total: {corners} corners y {woodwork} palo(s)/larguero(s)")
    elif woodwork > 0:
        score += 12
        reasons.append(f"{woodwork} palo(s)/larguero(s) → gol rozado")
    elif corner_diff >= 5:
        score += 10
        reasons.append(f"Dominio total en balón parado: {corners} corners vs {corners_r}")
    elif corners >= 5:
        score += 7
        reasons.append(f"Mucho peligro en corners: {corners} saques de esquina")

    # ── Regla 6: Portero rival muy exigido ────────────────────────────────────
    if saves_rival >= 5 or xgot_rival > 2.0:
        score += 20
        reasons.append(f"Portero rival al límite: {saves_rival} paradas, {xgot_rival:.2f} xGOT enfrentado")
    elif saves_rival >= 3 or xgot_rival > 1.2:
        score += 10
        reasons.append(f"Portero rival muy exigido: {saves_rival} paradas, {xgot_rival:.2f} xGOT")

    # ── Regla 7: Calidad en último tercio ────────────────────────────────────
    passes_diff = passes_ft - passes_ft_r
    if passes_ft >= 75 and passes_diff >= 15:
        score += 12
        reasons.append(f"Precisión quirúrgica en último tercio ({passes_ft:.0f}%, +{passes_diff:.0f}pp sobre rival)")
    elif passes_ft >= 70:
        score += 6
        reasons.append(f"Alta precisión de pases en zona de ataque ({passes_ft:.0f}%)")

    # ── Regla 8: Posesión sostenida/creciente ────────────────────────────────
    poss_growing = poss >= poss_prev
    if poss >= 65 and poss_growing:
        score += 10
        reasons.append(f"Dominio territorial creciente ({poss:.0f}% posesión, tendencia ↑)")
    elif poss >= 60 and poss_growing:
        score += 5
        reasons.append(f"Posesión alta sostenida ({poss:.0f}%)")

    # ── Bonus combinado: varios indicadores altos = sinergia ─────────────────
    # "Asedio perfecto": xGOT + big chances + toques área + corners
    siege_score = (
        (1 if xgot >= 1.0   else 0) +
        (1 if bc >= 2        else 0) +
        (1 if touches >= 15  else 0) +
        (1 if corners >= 4   else 0) +
        (1 if woodwork > 0   else 0)
    )
    if siege_score >= 4:
        score += 20
        reasons.append(f"Asedio perfecto: {siege_score}/5 indicadores de dominio ofensivo activos")
    elif siege_score >= 3:
        score += 10
        reasons.append(f"Múltiples indicadores de presión ofensiva activos ({siege_score}/5)")

    # ── Contexto: equipo perdiendo tiene mayor urgencia ofensiva ─────────────
    if side == "home" and gs.score_diff < 0 and gs.current_minute > 60:
        score *= 1.15
        reasons.append(f"{team} perdiendo en min {gs.current_minute}: urgencia ofensiva alta")
    elif side == "away" and gs.score_diff > 0 and gs.current_minute > 60:
        score *= 1.15
        reasons.append(f"{team} perdiendo en min {gs.current_minute}: urgencia ofensiva alta")

    # ── Aplicar multiplicador de fase ─────────────────────────────────────────
    score = round(score * phase_mult)

    return score, reasons


def calcular_alerta_gol(
    gs: GoalStats,
    memory: GoalMemory,
    url_flashscore: str = "",
    min_score: float = 55.0,    # umbral para disparar alerta
) -> str:
    """
    Analiza peligro de gol para ambos equipos.
    Devuelve el mensaje de alerta formateado, o cadena vacía si no aplica.
    """
    minute = gs.current_minute

    # Solo entre min 10 y 90+5
    if not (10 <= minute <= 95):
        return ""

    # ── Multiplicador de fase ──────────────────────────────────────────────────
    if minute < 30:
        phase_mult = 0.85
    elif minute < 60:
        phase_mult = 1.0
    elif minute < 75:
        phase_mult = 1.1
    elif minute < 85:
        phase_mult = 1.2
    else:
        phase_mult = 1.3   # últimos minutos: máxima urgencia

    score_h, reasons_h = _evaluate_team(gs, "home", phase_mult)
    score_a, reasons_a = _evaluate_team(gs, "away", phase_mult)

    # Determinar ganador
    if score_h >= min_score and score_h >= score_a:
        winner_side  = "home"
        winner_team  = gs.home_team
        winner_score = score_h
        winner_reasons = reasons_h
    elif score_a >= min_score and score_a > score_h:
        winner_side  = "away"
        winner_team  = gs.away_team
        winner_score = score_a
        winner_reasons = reasons_a
    else:
        return ""   # Ningún equipo supera el umbral

    # Anti-spam: no re-enviar si es la misma situación
    if not memory.should_fire(winner_team, int(winner_score), minute):
        return ""
    memory.register(winner_team, int(winner_score), minute)

    # ── Calcular probabilidad ─────────────────────────────────────────────────
    # Escala: score 55 → ~65%, score 150 → 95%
    prob = min(int(55 + (winner_score - min_score) * 0.35), 95)

    # Boost por "asedio perfecto" explícito
    if winner_side == "home":
        siege = (gs.woodwork_home > 0) + (gs.corners_home > 3) + (gs.big_chances_home > 1)
    else:
        siege = (gs.woodwork_away > 0) + (gs.corners_away > 3) + (gs.big_chances_away > 1)
    if siege == 3:
        prob = max(prob, 85)

    fire = "🔥 " if prob >= 85 else ""

    # ── Determinar motivo principal ───────────────────────────────────────────
    if winner_side == "home":
        xg_lead   = gs.xg_home - gs.xg_away
        xgot_val  = gs.xgot_home
        bc_val    = gs.big_chances_home
        ww        = gs.woodwork_home
    else:
        xg_lead   = gs.xg_away  - gs.xg_home
        xgot_val  = gs.xgot_away
        bc_val    = gs.big_chances_away
        ww        = gs.woodwork_away

    if ww > 0 and bc_val >= 2:
        motivo = "ASEDIO TOTAL"
    elif xg_lead >= 1.0:
        motivo = "SUPERIORIDAD xG CRÍTICA"
    elif bc_val >= 3:
        motivo = "LLUVIA DE BIG CHANCES"
    elif xgot_val > 1.5:
        motivo = "TIROS MORTALES"
    elif minute > 80:
        motivo = "PRESIÓN FINAL"
    else:
        motivo = "DOMINIO ACUMULADO"

    # ── Construir mensaje ─────────────────────────────────────────────────────
    score_str = f"{gs.score_home}-{gs.score_away}"
    lines = [
        f"{fire}⚽️ <b>GOL INMINENTE – {winner_team}</b>",
        f"📈 Probabilidad estimada: <b>{prob}%</b>",
        f"🎯 Motivo: <b>{motivo}</b>",
        f"⏱️ {gs.match_time or str(minute) + \"'\"}  |  "
        f"<b>{gs.home_team}</b> {score_str} <b>{gs.away_team}</b>",
        "📊 Razones:",
        *[f"• {r}" for r in winner_reasons[:4]],  # máximo 4 razones
    ]
    if url_flashscore:
        lines.append(f"<a href='{url_flashscore}'>Ver en Flashscore</a>")

    return "\n".join(lines)


# ==============================================================================
# INTEGRACIÓN CON MATCH_ANALYZER (helper de conversión)
# ==============================================================================

def goal_stats_from_match_stats(ms, ms_prev=None) -> GoalStats:
    """
    Convierte un MatchStats (de match_analyzer.py) en GoalStats.
    ms_prev: snapshot ~10 min antes, también MatchStats.
    """
    gs = GoalStats(
        home_team        = ms.home_team,
        away_team        = ms.away_team,
        current_minute   = ms.current_minute,
        score_home       = ms.score_home,
        score_away       = ms.score_away,
        score_diff       = ms.score_diff,
        match_time       = f"{ms.current_minute}'",
        xg_home          = ms.xg_home,
        xg_away          = ms.xg_away,
        corners_home     = ms.corners_home,
        corners_away     = ms.corners_away,
        possession_home  = ms.possession_home,
        possession_away  = ms.possession_away,
        saves_home       = ms.shots_on_target_away,  # proxy si no hay saves directo
        saves_away       = ms.shots_on_target_home,
        big_chances_home = ms.big_chances_home,
        big_chances_away = ms.big_chances_away,
    )
    if ms_prev:
        gs.xg_home_prev          = ms_prev.xg_home
        gs.xg_away_prev          = ms_prev.xg_away
        gs.possession_home_prev  = ms_prev.possession_home
        gs.possession_away_prev  = ms_prev.possession_away
    return gs



