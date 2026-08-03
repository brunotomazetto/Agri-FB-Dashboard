#!/usr/bin/env python3
"""
Gera history_era5.json (estático): série diária por ANO de cada ponto, para o
gráfico "spaghetti + envelope" (curvas dos últimos anos, banda min/máx, média).

Para cada coordenada única baixa do Open-Meteo Archive (ERA5) a série diária de
temperatura média + precipitação desde Y0, e armazena por ano alinhada ao índice
de dia-do-ano (0..365, ano bissexto de referência).

Incremental e em lote (ver ../om_archive.py): ponto inédito puxa Y0→hoje, ponto
já em cache puxa só os últimos OVERLAP_DAYS dias. Isso importa porque o custo
cobrado pela API é ~proporcional a (coordenadas × dias) — rebaixar o histórico
inteiro toda vez é o que tornava este script um job de horas, incapaz de rodar
em cron.

A janela de sobreposição não é opcional: o ERA5 recente é preliminar (ERA5T) e
os últimos dias são revisados depois. Buscar só os dias inéditos congelaria o
valor preliminar para sempre.

Uso:  python3 build_history.py [--full]     (--full ignora o cache e rebaixa tudo)
"""
import json, os, sys, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))          # Climate Monitor/
import om_archive

LOC = os.path.join(HERE, "locations.json")
OUT = os.path.join(HERE, "history_era5.json")          # ERA5; NASA fica em history.json
CACHE = os.path.join(HERE, "_hist_era5_cache.json")

Y0 = 2010
Y1 = datetime.date.today().year                      # inclui o ano corrente (parcial via ERA5)
OVERLAP_DAYS = 45

# pontos do módulo — única linha que difere entre Weather e Sugar Monitor
def module_points(data):
    return list(data["capitais"]) + list(data["fazendas"])   # regiões: ver build_regions.py


def build_md_index():
    md, i, days = {}, 0, [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    for m, n in enumerate(days, 1):
        for d in range(1, n + 1):
            md[(m, d)] = i; i += 1
    return md
MD = build_md_index()


def to_year_matrix(dates, vals, nd):
    years = {y: [None] * 366 for y in range(Y0, Y1 + 1)}
    merge_years(years, dates, vals, nd)
    return years


def merge_years(years, dates, vals, nd):
    """Grava os dias baixados por cima da matriz existente — os revisados
    sobrescrevem o preliminar e o resto do histórico fica intocado.
    Aceita chave int (matriz nova) ou str (matriz vinda do cache JSON)."""
    for ds, v in zip(dates, vals):
        if v is None:
            continue
        y, m, d = int(ds[:4]), int(ds[5:7]), int(ds[8:10])
        if not (Y0 <= y <= Y1):
            continue
        key = y if y in years else str(y)
        if key not in years:                       # ano novo (virada de ano)
            key = str(y) if any(isinstance(k, str) for k in years) else y
            years[key] = [None] * 366
        years[key][MD[(m, d)]] = round(v, nd)


def load_cache():
    """Cache do build anterior; na falta dele, o próprio history_era5.json serve
    de semente — o "points" do output tem exatamente o mesmo formato.

    Isso evita que uma execução sem cache (CI limpo, cache expirado, máquina
    nova) vire um bootstrap de 16 anos, que é caro em cota e leva horas. Com a
    semente, o pior caso é uma janela incremental como qualquer outra."""
    if os.path.exists(CACHE):
        return json.load(open(CACHE, encoding="utf-8"))
    if os.path.exists(OUT):
        pts = json.load(open(OUT, encoding="utf-8")).get("points", {})
        if pts:
            print(f"sem cache — semeando com {OUT.rsplit(os.sep, 1)[-1]} "
                  f"({len(pts)} pontos)", flush=True)
        return pts
    return {}


def main():
    full = "--full" in sys.argv
    data = json.load(open(LOC, encoding="utf-8"))
    uniq = {}
    for p in module_points(data):
        uniq.setdefault(f"{round(p['lat'],3)},{round(p['lon'],3)}", (p["lat"], p["lon"]))

    points = {} if full else load_cache()
    hoje = datetime.date.today()
    novas = [(k, v[0], v[1]) for k, v in sorted(uniq.items()) if k not in points]
    velhas = [(k, v[0], v[1]) for k, v in sorted(uniq.items()) if k in points]
    print(f"{len(uniq)} coords únicas | {len(novas)} completas | "
          f"{len(velhas)} incrementais ({OVERLAP_DAYS}d)", flush=True)

    if novas:
        got = om_archive.fetch(novas, f"{Y0}-01-01", hoje.isoformat(), "completa ")
        for k, (dates, t, p) in got.items():
            points[k] = {"t": to_year_matrix(dates, t, 1),
                         "p": to_year_matrix(dates, p, 1)}
        json.dump(points, open(CACHE, "w", encoding="utf-8"))

    if velhas:
        ini = (hoje - datetime.timedelta(days=OVERLAP_DAYS)).isoformat()
        got = om_archive.fetch(velhas, ini, hoje.isoformat(), "janela ")
        for k, (dates, t, p) in got.items():
            merge_years(points[k]["t"], dates, t, 1)
            merge_years(points[k]["p"], dates, p, 1)
        json.dump(points, open(CACHE, "w", encoding="utf-8"))

    out = {
        "meta": {"fonte": "Open-Meteo Archive (ERA5)", "anos": f"{Y0}-{Y1}",
                 "doy_index": "0=1jan ... 59=29fev ... 365=31dez", "n_pontos": len(points)},
        "years": list(range(Y0, Y1 + 1)),
        "points": points,
    }
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"OK -> {OUT} ({os.path.getsize(OUT)/1024/1024:.1f} MB, {len(points)} pontos)", flush=True)


if __name__ == "__main__":
    main()
