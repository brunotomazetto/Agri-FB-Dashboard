#!/usr/bin/env python3
"""
Gera history.json (estático): série diária por ANO de cada ponto, para o gráfico
"spaghetti + envelope" (curvas dos últimos anos, banda min/máx, média).

Para cada coordenada única: baixa do Open-Meteo Archive (ERA5) a série diária
2010-2024 de temperatura média + precipitação, e armazena por ano alinhada ao
índice de dia-do-ano (0..365, ano bissexto de referência).

Resumível (cache _hist_cache.json), paceado e tolerante a rate-limit horário.
Roda 1x (reexecute ~1x/ano para incorporar o ano novo).
"""
import json, time, os, urllib.request, urllib.error, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
LOC = os.path.join(HERE, "locations.json")
OUT = os.path.join(HERE, "history_era5.json")          # ERA5 (Open-Meteo); NASA fica em history.json
CACHE = os.path.join(HERE, "_hist_era5_cache.json")

ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
Y0 = 2010
Y1 = datetime.date.today().year                      # inclui o ano corrente (parcial via ERA5)
START, END = f"{Y0}-01-01", datetime.date.today().isoformat()

def build_md_index():
    md, idx, days = {}, 0, [31,29,31,30,31,30,31,31,30,31,30,31]
    for m, n in enumerate(days, start=1):
        for d in range(1, n + 1):
            md[(m, d)] = idx; idx += 1
    return md
MD = build_md_index()  # 366 chaves

def secs_to_next_hour():
    now = datetime.datetime.utcnow()
    nxt = now.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)
    return max(30, int((nxt - now).total_seconds()) + 60)

def fetch(lat, lon, max_hour_waits=6, tries=5):
    url = (f"{ARCHIVE}?latitude={lat}&longitude={lon}"
           f"&start_date={START}&end_date={END}"
           f"&daily=temperature_2m_mean,precipitation_sum&timezone=auto")
    hour_waits, t = 0, 0
    while True:
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                return json.loads(r.read().decode())["daily"]
        except urllib.error.HTTPError as e:
            if e.code == 429:
                body = ""
                try: body = e.read().decode()
                except Exception: pass
                if "ourly" in body or "aily" in body:
                    if hour_waits >= max_hour_waits:
                        raise RuntimeError(f"falhou {lat},{lon} (limite persistente)")
                    hour_waits += 1; w = secs_to_next_hour()
                    print(f"    limite horário — aguardando {w}s ({hour_waits}/{max_hour_waits})", flush=True)
                    time.sleep(w); continue
                print("    429 por-minuto — 65s", flush=True); time.sleep(65); continue
            t += 1
            if t >= tries: raise RuntimeError(f"falhou {lat},{lon} (HTTP {e.code})")
            print(f"    retry {t}/{tries} ({e.code})", flush=True); time.sleep(8*t)
        except Exception as e:
            t += 1
            if t >= tries: raise RuntimeError(f"falhou {lat},{lon} ({str(e)[:40]})")
            print(f"    retry {t}/{tries} ({str(e)[:40]})", flush=True); time.sleep(10)

def to_year_matrix(dates, vals, nd):
    """dict ano -> array de 366 (índice dia-do-ano), arredondado."""
    years = {y: [None]*366 for y in range(Y0, Y1+1)}
    for ds, v in zip(dates, vals):
        if v is None: continue
        y = int(ds[:4]); m = int(ds[5:7]); d = int(ds[8:10])
        years[y][MD[(m, d)]] = round(v, nd)
    return years

def main():
    data = json.load(open(LOC, encoding="utf-8"))
    pts = list(data["capitais"]) + list(data["fazendas"])
    uniq = {}
    for p in pts:
        uniq.setdefault(f"{round(p['lat'],3)},{round(p['lon'],3)}", (p["lat"], p["lon"]))

    points = json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}
    todo = [(k, v) for k, v in sorted(uniq.items()) if k not in points]
    print(f"{len(uniq)} coords únicas | {len(points)} em cache | {len(todo)} a baixar", flush=True)

    for i, (key, (lat, lon)) in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {key}", flush=True)
        d = fetch(lat, lon)
        points[key] = {
            "t": to_year_matrix(d["time"], d["temperature_2m_mean"], 1),
            "p": to_year_matrix(d["time"], d["precipitation_sum"], 1),
        }
        json.dump(points, open(CACHE, "w", encoding="utf-8"))
        time.sleep(11)

    out = {
        "meta": {"fonte": "Open-Meteo Archive (ERA5)", "anos": f"{Y0}-{Y1}",
                 "doy_index": "0=1jan ... 59=29fev ... 365=31dez", "n_pontos": len(points)},
        "years": list(range(Y0, Y1+1)),
        "points": points,
    }
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"OK -> {OUT} ({os.path.getsize(OUT)/1024/1024:.1f} MB, {len(points)} pontos)", flush=True)

if __name__ == "__main__":
    main()
