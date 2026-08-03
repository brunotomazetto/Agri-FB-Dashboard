#!/usr/bin/env python3
"""
Cliente do Open-Meteo Archive (ERA5) compartilhado pelos builders de clima.

Duas coisas que os builders originais não faziam e que mudam a ordem de
grandeza do custo:

1. LOTE. A API de arquivo aceita várias coordenadas por request (latitude e
   longitude como listas separadas por vírgula, resposta como array na mesma
   ordem). Baixar uma coordenada por request era o que tornava o rebuild um
   job de horas.

2. JANELA INCREMENTAL. O custo cobrado é ~proporcional a (coordenadas × dias),
   então rebaixar 2010→hoje toda vez é ~200x mais caro que buscar só os dias
   novos. Medido em 03/08/2026, com requests de 16 anos:

       10 coords × 6.059 dias -> 429 já no 2º request  (~10 coords/min)
       600 coords ×    45 dias -> 429 só no 7º request  (600 coords em 21s)

   O limite não é de requests, é de volume: ~60 mil coordenada-dias por
   minuto. BUDGET abaixo trabalha com folga sobre esse teto.

O ERA5 recente é preliminar (ERA5T) e sofre revisão, então quem chama deve
rebaixar uma janela de sobreposição em vez de só os dias inéditos — ver
OVERLAP_DAYS nos builders.
"""
import json, time, datetime, urllib.request, urllib.error

ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
BUDGET = 4500          # coordenada-dias por request (100 coords × 45 dias)
MAX_COORDS = 100       # teto por request, independente da janela
PAUSE = 1.0            # respiro entre requests bem-sucedidos


def _batch_size(days):
    return max(1, min(MAX_COORDS, BUDGET // max(1, days)))


def _secs_to_next_minute():
    return 62 - datetime.datetime.now().second


def _get(lats, lons, start, end, tries=6):
    url = (f"{ARCHIVE}?latitude={','.join(lats)}&longitude={','.join(lons)}"
           f"&start_date={start}&end_date={end}"
           "&daily=temperature_2m_mean,precipitation_sum&timezone=auto")
    for t in range(1, tries + 1):
        try:
            with urllib.request.urlopen(url, timeout=300) as r:
                d = json.loads(r.read().decode())
            # erro de cota volta como JSON 200/400 com {"error": true}, não como exceção
            if isinstance(d, dict) and d.get("error"):
                raise urllib.error.HTTPError(url, 429, d.get("reason", "erro"), None, None)
            return d if isinstance(d, list) else [d]
        except urllib.error.HTTPError as e:
            body = ""
            try: body = e.read().decode()
            except Exception: pass
            reason = (body or str(e)).lower()
            if e.code == 429 or "limit" in reason:
                # cota horária/diária não zera na virada do minuto
                w = 3600 if ("hourly" in reason or "daily" in reason) else _secs_to_next_minute()
                print(f"    cota atingida — aguardando {w}s ({t}/{tries})", flush=True)
                time.sleep(w)
                continue
            if t == tries:
                raise
            time.sleep(8 * t)
        except Exception as e:
            if t == tries:
                raise
            print(f"    retry {t}/{tries} ({str(e)[:60]})", flush=True)
            time.sleep(10)
    raise RuntimeError("Open-Meteo Archive: cota persistente")


def fetch(coords, start, end, rotulo=""):
    """coords: [(chave, lat, lon)] -> {chave: (datas, temps, precs)}

    Sobe exceção se a cota persistir; o cache do chamador é resumível, então
    relançar o job continua de onde parou.
    """
    days = (datetime.date.fromisoformat(end) - datetime.date.fromisoformat(start)).days + 1
    n = _batch_size(days)
    out = {}
    total = (len(coords) + n - 1) // n
    for i in range(0, len(coords), n):
        sl = coords[i:i + n]
        arr = _get([f"{c[1]}" for c in sl], [f"{c[2]}" for c in sl], start, end)
        for (k, _, _), loc in zip(sl, arr):
            d = loc.get("daily") or {}
            out[k] = (d.get("time", []),
                      d.get("temperature_2m_mean", []),
                      d.get("precipitation_sum", []))
        print(f"  {rotulo}[{i // n + 1}/{total}] {len(sl)} coords × {days}d", flush=True)
        time.sleep(PAUSE)
    return out
