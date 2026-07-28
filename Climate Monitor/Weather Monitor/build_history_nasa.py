#!/usr/bin/env python3
"""
Gera history.json a partir do NASA POWER (sem rate limit), série diária por ANO
de cada ponto: temperatura média (T2M) + precipitação (PRECTOTCORR), 2010→hoje.
Inclui o ano corrente (NASA POWER tem ~2-3 dias de latência).

Resumível (cache _hist_cache.json). Roda 1x (reexecute ~1x/ano).
"""
import json, time, os, urllib.request, urllib.error, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
LOC = os.path.join(HERE, "locations.json")
OUT = os.path.join(HERE, "history.json")
CACHE = os.path.join(HERE, "_hist_cache.json")

API = "https://power.larc.nasa.gov/api/temporal/daily/point"
Y0 = 2010
Y1 = datetime.date.today().year
START = f"{Y0}0101"
END = datetime.date.today().strftime("%Y%m%d")

def build_md_index():
    md, idx, days = {}, 0, [31,29,31,30,31,30,31,31,30,31,30,31]
    for m, n in enumerate(days, start=1):
        for d in range(1, n + 1):
            md[(m, d)] = idx; idx += 1
    return md
MD = build_md_index()

def fetch(lat, lon, tries=5):
    url = (f"{API}?parameters=T2M,PRECTOTCORR&community=AG"
           f"&longitude={lon}&latitude={lat}&start={START}&end={END}&format=JSON")
    for t in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                j = json.loads(r.read().decode())
            return j["properties"]["parameter"]
        except Exception as e:
            w = 6 * (t + 1)
            print(f"    retry {t+1}/{tries} em {w}s ({str(e)[:50]})", flush=True)
            time.sleep(w)
    raise RuntimeError(f"falhou {lat},{lon}")

def to_year_matrix(param, nd):
    years = {y: [None]*366 for y in range(Y0, Y1+1)}
    for ymd, v in param.items():
        if v is None or v <= -900:
            continue
        y, m, d = int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8])
        if y < Y0 or y > Y1:
            continue
        years[y][MD[(m, d)]] = round(v, nd)
    return years

def main():
    data = json.load(open(LOC, encoding="utf-8"))
    pts = list(data["capitais"]) + list(data["fazendas"])   # regiões: ver build_regions.py
    uniq = {}
    for p in pts:
        uniq.setdefault(f"{round(p['lat'],3)},{round(p['lon'],3)}", (p["lat"], p["lon"]))

    points = json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}
    todo = [(k, v) for k, v in sorted(uniq.items()) if k not in points]
    print(f"{len(uniq)} coords | {len(points)} em cache | {len(todo)} a baixar (NASA POWER)", flush=True)

    for i, (key, (lat, lon)) in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {key}", flush=True)
        par = fetch(lat, lon)
        points[key] = {
            "t": to_year_matrix(par["T2M"], 1),
            "p": to_year_matrix(par["PRECTOTCORR"], 1),
        }
        json.dump(points, open(CACHE, "w", encoding="utf-8"))
        time.sleep(0.6)

    out = {
        "meta": {"fonte": "NASA POWER (T2M, PRECTOTCORR)", "anos": f"{Y0}-{Y1}",
                 "doy_index": "0=1jan ... 59=29fev ... 365=31dez", "n_pontos": len(points)},
        "years": list(range(Y0, Y1+1)),
        "points": points,
    }
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"OK -> {OUT} ({os.path.getsize(OUT)/1024/1024:.1f} MB, {len(points)} pontos)", flush=True)

if __name__ == "__main__":
    main()
