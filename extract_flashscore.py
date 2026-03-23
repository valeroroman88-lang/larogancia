import requests
from bs4 import BeautifulSoup
import sys
import csv
import json
import re

def extract_match_data(url):
    """
    Extrae hora, equipos y URLs de los partidos de una página de flashscore.mobi
    y devuelve una lista de diccionarios.
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    
    try:
        print(f"Accediendo a: {url}...")
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Flashscore.mobi tiene una estructura donde los partidos están en el cuerpo principal
        # Los partidos suelen estar precedidos por un span con la hora y seguidos por el enlace del partido
        matches = []
        base_url = "https://www.flashscore.mobi"
        
        # Buscamos todos los enlaces de partidos
        match_links = soup.find_all('a', href=re.compile(r'/match/'))
        
        for link in match_links:
            href = link['href']
            # Obtener la URL limpia con ?t=stats
            clean_id = re.search(r'/match/([^/]+)/', href)
            if not clean_id:
                continue
            match_id = clean_id.group(1)
            stats_url = f"{base_url}/match/{match_id}/?t=stats"
            
            # Intentar encontrar la hora y los equipos
            # La estructura observada es: <span>HORA</span>EQUIPOS <a href=...>MARCADOR</a>
            # El texto de los equipos está justo antes del enlace
            
            # Buscamos el span previo que contiene la hora
            time_span = link.find_previous('span')
            match_time = time_span.get_text(strip=True) if time_span else "N/A"
            
            # El texto de los equipos está entre el span de la hora y el enlace del partido
            # Usamos next_sibling para obtener el texto después del span de la hora
            teams_text = ""
            if time_span:
                curr = time_span.next_sibling
                while curr and curr != link:
                    if isinstance(curr, str):
                        teams_text += curr
                    else:
                        teams_text += curr.get_text()
                    curr = curr.next_sibling
            
            teams_text = teams_text.strip().strip('-').strip()
            
            # Separar equipos si es posible (formato "Equipo A - Equipo B")
            if " - " in teams_text:
                team_home, team_away = teams_text.split(" - ", 1)
            else:
                team_home, team_away = teams_text, "N/A"

            matches.append({
                "hora": match_time,
                "equipo_local": team_home.strip(),
                "equipo_visitante": team_away.strip(),
                "url_stats": stats_url
            })
        
        return matches

    except Exception as e:
        print(f"Error al extraer los datos: {e}")
        return []

def save_to_csv(data, filename):
    if not data:
        return
    keys = data[0].keys()
    with open(filename, 'w', newline='', encoding='utf-8') as f:
        dict_writer = csv.DictWriter(f, fieldnames=keys)
        dict_writer.writeheader()
        dict_writer.writerows(data)
    print(f"Datos guardados en {filename}")

def save_to_json(data, filename):
    if not data:
        return
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    print(f"Datos guardados en {filename}")

if __name__ == "__main__":
    target_url = "https://www.flashscore.mobi/"
    if len(sys.argv) > 1:
        target_url = sys.argv[1]
        
    matches = extract_match_data(target_url)
    
    if matches:
        print(f"\nSe han encontrado {len(matches)} partidos.")
        save_to_csv(matches, "partidos.csv")
        save_to_json(matches, "partidos.json")
        
        # Mostrar los primeros 5 como ejemplo
        print("\nEjemplo de los primeros 5 partidos:")
        for m in matches[:5]:
            print(f"{m['hora']} | {m['equipo_local']} vs {m['equipo_visitante']} | {m['url_stats']}")
    else:
        print("No se encontraron datos de partidos.")
