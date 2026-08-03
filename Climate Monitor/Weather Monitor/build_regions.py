#!/usr/bin/env python3
"""
Séries climáticas por REGIÃO produtora (RS + MT), a partir de TODOS os municípios.

Composição (locations.json -> "regioes"):
  MT: 7 macrorregiões Imea (lista municipal oficial da nota técnica de nov/2017).
  RS: 7 mesorregiões IBGE.
  638 municípios no total, cada um com milho_t e soja_t (produção média 22-24)
  usados como peso alternativo à média simples.

Como funciona
-------------
Os 638 municípios caem em ~189 células da grade do NASA POWER (0,5° lat ×
0,625° lon). Baixar por célula em vez de por município corta o download em ~70%
sem perder nada: municípios da mesma célula recebem exatamente o mesmo dado da
API de qualquer forma. Os dois modelos usam a MESMA grade de células, de modo
que alternar NASA/ERA5 no dashboard isola a diferença de reanálise, não a de
amostragem espacial.

A série de cada região é a média das células ponderada pela soma dos pesos dos
municípios que caem em cada uma — ou seja, idêntica à média sobre todos os
municípios. Com peso=1 para todos, é a média simples municipal.

Duas etapas independentes:
  download  — baixa as células que faltam (cache resumível, por modelo)
  aggregate — recalcula as séries regionais a partir do cache

Reatribuir pesos exige apenas `aggregate`; nada é baixado de novo.

Uso:  python3 build_regions.py [nasa|era5|all] [--only-aggregate]
"""
import json, os, sys, time, datetime, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))          # Climate Monitor/
import om_archive

LOC = os.path.join(HERE, "locations.json")

# Janela rebaixada a cada execução incremental do ERA5. O ERA5T dos últimos
# dias é preliminar e revisado; 45 dias cobrem a revisão com folga e ainda
# deixam o job na casa de minutos.
OVERLAP_DAYS = 45

# Grade do NASA POWER; compartilhada pelos dois modelos (ver docstring).
DLAT, DLON = 0.5, 0.625

Y0 = 2010
Y1 = datetime.date.today().year

MODELS = {
    "nasa": {"out": "history.json", "cache": "_cells_nasa.json", "fonte": "NASA POWER (T2M, PRECTOTCORR)"},
    "era5": {"out": "history_era5.json", "cache": "_cells_era5.json", "fonte": "Open-Meteo Archive (ERA5)"},
}


def md_index():
    md, i, days = {}, 0, [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    for m, n in enumerate(days, 1):
        for d in range(1, n + 1):
            md[(m, d)] = i; i += 1
    return md
MD = md_index()


def cell_of(lat, lon):
    """Centro da célula da grade que contém o ponto."""
    return (round(round(lat / DLAT) * DLAT, 4), round(round(lon / DLON) * DLON, 4))


# Ponderações oferecidas no dashboard. A chave entra no sufixo do ponto
# ("UF|Região|n"). Produção média de 2022-2024 em toneladas, SIDRA 1612 —
# média de 3 anos para diluir quebra de safra de um ano isolado.
PESOS = {
    "n": ("simples (1 por município)", lambda m: 1),
    "m": ("produção de milho, média 2022-24 (t)", lambda m: m.get("milho_t", 0)),
    "s": ("produção de soja, média 2022-24 (t)", lambda m: m.get("soja_t", 0)),
}


def load_cells():
    """municípios -> {cellkey: {lat, lon, regioes: {regiao: {peso: soma}}}}"""
    regs = json.load(open(LOC, encoding="utf-8"))["regioes"]
    cells = {}
    for m in regs:
        la, lo = cell_of(m["lat"], m["lon"])
        k = f"{la},{lo}"
        c = cells.setdefault(k, {"lat": la, "lon": lo, "regioes": {}})
        rk = m["uf"] + "|" + m["regiao"]
        acc = c["regioes"].setdefault(rk, {p: 0 for p in PESOS})
        for p, (_, fn) in PESOS.items():
            acc[p] += fn(m)
    return regs, cells


# ---------------------------------------------------------------- download
def fetch_nasa(lat, lon, tries=12):
    """Backoff longo: uma queda de rede/DNS não pode matar um job de 20 min —
    o cache é resumível, mas reiniciar à mão a cada blip é pior que esperar."""
    url = ("https://power.larc.nasa.gov/api/temporal/daily/point"
           f"?parameters=T2M,PRECTOTCORR&community=AG&longitude={lon}&latitude={lat}"
           f"&start={Y0}0101&end={datetime.date.today():%Y%m%d}&format=JSON")
    for t in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                p = json.loads(r.read().decode())["properties"]["parameter"]
            return ([f"{k[:4]}-{k[4:6]}-{k[6:]}" for k in p["T2M"]],
                    list(p["T2M"].values()), list(p["PRECTOTCORR"].values()))
        except Exception as e:
            w = min(120, 5 * 2 ** min(t, 5))          # 5,10,20,40,80,120,120…
            print(f"    retry {t+1}/{tries} em {w}s ({str(e)[:60]})", flush=True)
            time.sleep(w)
    raise RuntimeError(f"falhou NASA {lat},{lon}")


def to_years(dates, vals, nd):
    years = {y: [None] * 366 for y in range(Y0, Y1 + 1)}
    merge_years(years, dates, vals, nd)
    return years


def merge_years(years, dates, vals, nd):
    """Grava os dias baixados por cima da matriz existente. É o que permite
    rebaixar só uma janela recente: os dias revisados sobrescrevem o valor
    preliminar e o resto do histórico fica intocado.

    Aceita matriz com chave int (recém-criada) ou str (vinda do cache JSON)."""
    for ds, v in zip(dates, vals):
        if v is None or v <= -900:
            continue
        y, m, d = int(ds[:4]), int(ds[5:7]), int(ds[8:10])
        if not (Y0 <= y <= Y1):
            continue
        key = y if y in years else str(y)
        if key not in years:                       # ano novo (virada de ano)
            key = str(y) if any(isinstance(k, str) for k in years) else y
            years[key] = [None] * 366
        years[key][MD[(m, d)]] = round(v, nd)


def download(model, cells):
    return download_era5(cells) if model == "era5" else download_nasa(cells)


def download_nasa(cells):
    """NASA POWER não tem cota, mas só atende uma coordenada por request: ~726
    requests, ~50 min. O rebuild COMPLETO é de propósito — o POWER revisa os
    ~30 dias mais recentes (medido em 03/08/2026: 28 dias de junho mudaram
    entre o build de 02/07 e o de 02/08, |Δ| médio 0,42 °C e máx 1,30 °C), e
    rebaixar tudo é o que faz o histórico convergir para o valor final.

    O cache existe só para retomar um job interrompido: em CI ele não é
    restaurado entre execuções, senão célula já baixada nunca seria revisitada
    e o valor preliminar ficaria congelado para sempre."""
    cfg = MODELS["nasa"]
    cache_path = os.path.join(HERE, cfg["cache"])
    cache = json.load(open(cache_path, encoding="utf-8")) if os.path.exists(cache_path) else {}
    todo = [k for k in sorted(cells) if k not in cache]
    print(f"[nasa] {len(cells)} células | {len(cache)} em cache | {len(todo)} a baixar", flush=True)
    for i, k in enumerate(todo, 1):
        c = cells[k]
        print(f"  [{i}/{len(todo)}] {k}", flush=True)
        dates, t, p = fetch_nasa(c["lat"], c["lon"])
        cache[k] = {"t": to_years(dates, t, 1), "p": to_years(dates, p, 1)}
        json.dump(cache, open(cache_path, "w", encoding="utf-8"))
        time.sleep(0.6)
    return cache


def download_era5(cells):
    """Incremental e em lote (ver om_archive). Célula inédita puxa 2010→hoje;
    célula já em cache puxa só a janela de sobreposição, porque o ERA5 recente
    é preliminar (ERA5T) e os últimos dias são revisados depois."""
    cfg = MODELS["era5"]
    cache_path = os.path.join(HERE, cfg["cache"])
    cache = json.load(open(cache_path, encoding="utf-8")) if os.path.exists(cache_path) else {}
    hoje = datetime.date.today()
    novas = [(k, cells[k]["lat"], cells[k]["lon"]) for k in sorted(cells) if k not in cache]
    velhas = [(k, cells[k]["lat"], cells[k]["lon"]) for k in sorted(cells) if k in cache]
    print(f"[era5] {len(cells)} células | {len(novas)} completas | "
          f"{len(velhas)} incrementais ({OVERLAP_DAYS}d)", flush=True)

    if novas:
        got = om_archive.fetch(novas, f"{Y0}-01-01", hoje.isoformat(), "completa ")
        for k, (dates, t, p) in got.items():
            cache[k] = {"t": to_years(dates, t, 1), "p": to_years(dates, p, 1)}
        json.dump(cache, open(cache_path, "w", encoding="utf-8"))

    if velhas:
        ini = (hoje - datetime.timedelta(days=OVERLAP_DAYS)).isoformat()
        got = om_archive.fetch(velhas, ini, hoje.isoformat(), "janela ")
        for k, (dates, t, p) in got.items():
            merge_years(cache[k]["t"], dates, t, 1)
            merge_years(cache[k]["p"], dates, p, 1)
        json.dump(cache, open(cache_path, "w", encoding="utf-8"))
    return cache


# --------------------------------------------------------------- aggregate
def aggregate(model, cells, cache):
    """Média ponderada das células de cada região, dia a dia, para cada ponderação."""
    regioes = {}
    for k, c in cells.items():
        for rk, ws in c["regioes"].items():
            regioes.setdefault(rk, []).append((k, ws))

    points, vazias = {}, []
    for rk, members in sorted(regioes.items()):
        linha = f"  {rk:42s} {len(members):3d} células"
        for peso in PESOS:
            tot = sum(ws[peso] for _, ws in members)
            if tot <= 0:
                # região sem produção da cultura: não gera série (evita divisão
                # por zero e uma linha falsa de zeros no dashboard)
                vazias.append(f"{rk}|{peso}")
                linha += f"   {peso}:—"
                continue
            out = {"t": {}, "p": {}}
            for var in ("t", "p"):
                for y in range(Y0, Y1 + 1):
                    ys = str(y)
                    acc = [0.0] * 366
                    wsum = [0.0] * 366
                    for ck, ws in members:
                        w = ws[peso]
                        if w <= 0:
                            continue
                        arr = cache[ck][var].get(ys) or cache[ck][var].get(y)
                        if not arr:
                            continue
                        for i, v in enumerate(arr):
                            if v is not None:
                                acc[i] += v * w; wsum[i] += w
                    out[var][ys] = [round(acc[i] / wsum[i], 1) if wsum[i] else None
                                    for i in range(366)]
            points[rk + "|" + peso] = out
            linha += f"   {peso}:ok"
        print(linha, flush=True)
    if vazias:
        print("  sem produção (série omitida):", ", ".join(vazias), flush=True)
    return points


def write(model, cells, cache, regs):
    points = aggregate(model, cells, cache)
    cfg = MODELS[model]
    out_path = os.path.join(HERE, cfg["out"])

    # Preserva capitais e fazendas; descarta pontos órfãos de versões anteriores
    # (municípios avulsos) e regrava todas as regiões.
    keep = {f"{round(p['lat'],3)},{round(p['lon'],3)}"
            for grp in ("capitais", "fazendas")
            for p in json.load(open(LOC, encoding="utf-8"))[grp]}
    base = {}
    if os.path.exists(out_path):
        old = json.load(open(out_path, encoding="utf-8")).get("points", {})
        base = {k: v for k, v in old.items() if "|" not in k and k in keep}
        orfaos = sum(1 for k in old if "|" not in k and k not in keep)
        if orfaos:
            print(f"[{model}] {orfaos} pontos órfãos descartados", flush=True)
    base.update(points)

    info = {}
    for m in regs:
        rk = m["uf"] + "|" + m["regiao"]
        d = info.setdefault(rk, {"municipios": 0, "milho_t": 0, "soja_t": 0})
        d["municipios"] += 1
        d["milho_t"] += m.get("milho_t", 0)
        d["soja_t"] += m.get("soja_t", 0)
    out = {
        "meta": {"fonte": cfg["fonte"], "anos": f"{Y0}-{Y1}",
                 "doy_index": "0=1jan ... 59=29fev ... 365=31dez",
                 "n_pontos": len(base),
                 "pesos": {k: v[0] for k, v in PESOS.items()},
                 "regioes": dict(sorted(info.items())),
                 "grade_regioes": f"{DLAT}x{DLON} (NASA POWER); regiões = média das células ponderada pelos pesos municipais"},
        "years": list(range(Y0, Y1 + 1)),
        "points": base,
    }
    json.dump(out, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"[{model}] OK -> {cfg['out']} ({os.path.getsize(out_path)/1024/1024:.1f} MB, "
          f"{len(base)} pontos, {len(points)} regiões)", flush=True)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    only_agg = "--only-aggregate" in sys.argv
    which = (args[0] if args else "all")
    models = list(MODELS) if which == "all" else [which]

    regs, cells = load_cells()
    print(f"{len(regs)} municípios -> {len(cells)} células da grade "
          f"({DLAT}°x{DLON}°)\n", flush=True)

    for m in models:
        cache_path = os.path.join(HERE, MODELS[m]["cache"])
        if only_agg:
            cache = json.load(open(cache_path, encoding="utf-8"))
            falta = [k for k in cells if k not in cache]
            if falta:
                sys.exit(f"[{m}] cache incompleto: faltam {len(falta)} células — rode sem --only-aggregate")
        else:
            cache = download(m, cells)
        write(m, cells, cache, regs)


if __name__ == "__main__":
    main()
