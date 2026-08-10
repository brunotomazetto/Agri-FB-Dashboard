#!/usr/bin/env python3
"""
Carrega a grade climática do Brasil inteiro no Supabase (tabela climate_cell).

Por que isto existe
-------------------
O history.json cobre 66 pontos fixos. Para o cliente apontar QUALQUER
coordenada, a série tem que estar pronta antes — e está, porque a grade do
NASA POWER é 0,5° lat × 0,625° lon: o Brasil são ~2.450 células, não
infinitos pontos. Duas coordenadas na mesma célula devolvem a mesma série,
então o universo de downloads possíveis é finito e pequeno.

Dois modos
----------
  --full   (padrão)  baixa 2010→hoje. Roda UMA vez, no seed inicial.
  --tail             baixa só o ano corrente. É o que roda toda semana.

O --tail não é uma otimização preguiçosa: o POWER REVISA os ~30 dias mais
recentes (medido em 03/08/2026: 28 dias de junho mudaram entre dois builds,
|Δ| médio 0,42 °C, máx 1,30 °C). Um refresh que só acrescentasse os dias
novos congelaria o valor preliminar para sempre. Rebaixar o ano corrente
inteiro absorve a revisão e ainda custa 1/17 do payload — os anos fechados
não mudam mais e são baixados uma vez só.

Uso
---
  export SUPABASE_URL=https://xxxx.supabase.co
  export SUPABASE_SERVICE_ROLE_KEY=...        # service role: ignora RLS
  python seed_grid_supabase.py --full         # ~15 min, uma vez
  python seed_grid_supabase.py --tail         # ~10 min, semanal

  python seed_grid_supabase.py --cells-only   # só gera grid_br.json e sai
  python seed_grid_supabase.py --dry-run --limit 5   # não escreve no banco

Resumível: o progresso vai para _grid_done.json, então uma queda de rede no
minuto 12 não recomeça do zero.
"""
import argparse, base64, datetime, json, os, struct, sys, time
import urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
GRID = os.path.join(HERE, "grid_br.json")
DONE = os.path.join(HERE, "_grid_done.json")
MESH = os.path.join(HERE, "_br_mesh.json")

POWER = "https://power.larc.nasa.gov/api/temporal/daily/point"
IBGE = ("https://servicodados.ibge.gov.br/api/v3/malhas/paises/BR"
        "?formato=application/vnd.geo+json&qualidade=maxima")

DLAT, DLON = 0.5, 0.625          # grade do NASA POWER
Y0 = 2010
Y1 = datetime.date.today().year
MISSING = -32768                 # sentinela do int16
SCALE = 10                       # 0,1 °C e 0,1 mm
PAR = 8                          # conexões simultâneas (testado até 30 sem 429)


# ── grade ──────────────────────────────────────────────────────────────
def cell_of(lat, lon):
    """Centro da célula que contém o ponto.

    floor(x/d + 0.5), não round(): o round() do Python arredonda
    meio-para-o-par e o do Postgres/JS meio-para-cima, então um ponto
    exatamente na fronteira (lat −14,75) cairia em células diferentes
    conforme quem calculou. Tem que bater com climate_cell_id() no
    migration_climate_grid.sql e com cellOf() no weather_dashboard.html.
    """
    import math
    return (round(math.floor(lat / DLAT + 0.5) * DLAT, 4),
            round(math.floor(lon / DLON + 0.5) * DLON, 4))


def cell_key(lat, lon):
    la, lo = cell_of(lat, lon)
    return f"{la:.4f},{lo:.4f}"


def _get(url, timeout=120):
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    if raw[:2] == b"\x1f\x8b":
        import gzip
        raw = gzip.decompress(raw)
    return raw


def _inside(x, y, ring):
    c, n = False, len(ring)
    for i in range(n):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            c = not c
    return c


def build_grid():
    """Células cujo centro OU algum canto cai em território brasileiro.

    Sem a máscara, o bounding box do Brasil tem 5.893 células e mais da
    metade é oceano — 2,4× de download jogado fora. Testar também os
    cantos é o que segura a costa e a faixa de fronteira: uma célula que
    só encosta no litoral tem centro no mar mas contém fazenda.
    """
    if os.path.exists(GRID):
        cells = json.load(open(GRID, encoding="utf-8"))
        print(f"grade: {len(cells)} células (de {GRID})", flush=True)
        return cells

    if os.path.exists(MESH):
        mesh = json.load(open(MESH, encoding="utf-8"))
    else:
        print("baixando malha do Brasil (IBGE)…", flush=True)
        mesh = json.loads(_get(IBGE))
        json.dump(mesh, open(MESH, "w"))

    geom = mesh["features"][0]["geometry"]
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    rings = [p[0] for p in polys]
    land = lambda x, y: any(_inside(x, y, r) for r in rings)

    lats = [round(i * DLAT, 4) for i in range(int(-34 / DLAT) - 1, int(6 / DLAT) + 2)]
    lons = [round(i * DLON, 4) for i in range(int(-75 / DLON) - 1, int(-33 / DLON) + 2)]

    cells = []
    for la in lats:
        for lo in lons:
            hit = land(lo, la) or any(land(lo + dx * DLON / 2, la + dy * DLAT / 2)
                                      for dx in (-1, 1) for dy in (-1, 1))
            if hit:
                cells.append({"cell_id": f"{la:.4f},{lo:.4f}", "lat": la, "lon": lo})

    json.dump(cells, open(GRID, "w"), separators=(",", ":"))
    print(f"grade: {len(cells)} células de {len(lats)*len(lons)} no bounding box "
          f"-> {GRID}", flush=True)
    return cells


# ── download + codificação ─────────────────────────────────────────────
def md_index():
    md, i, days = {}, 0, [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    for m, n in enumerate(days, 1):
        for d in range(1, n + 1):
            md[(m, d)] = i
            i += 1
    return md
MD = md_index()


def fetch(lat, lon, y0, tries=8):
    """Baixa de y0 até hoje. Backoff longo: um blip de DNS não pode matar
    um job de 15 min."""
    end = datetime.date.today().strftime("%Y%m%d")
    url = (f"{POWER}?parameters=T2M,PRECTOTCORR&community=AG"
           f"&longitude={lon}&latitude={lat}"
           f"&start={y0}0101&end={end}&format=JSON")
    last = None
    for t in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=180) as r:
                return json.loads(r.read().decode())["properties"]["parameter"]
        except Exception as e:
            last = e
            time.sleep(min(60, 5 * (t + 1)))
    raise RuntimeError(f"POWER falhou em {lat},{lon}: {last}")


def encode_years(param):
    """{'20250131': 24.3, ...} -> {ano: base64(int16[366])}"""
    years = {}
    for ymd, v in param.items():
        y, m, d = int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8])
        if y < Y0 or y > Y1:
            continue
        arr = years.get(y)
        if arr is None:
            arr = years[y] = [MISSING] * 366
        if v is None or v <= -900:      # POWER usa -999 para ausente
            continue
        arr[MD[(m, d)]] = max(-32767, min(32767, int(round(v * SCALE))))
    return {y: base64.b64encode(struct.pack("<366h", *a)).decode() for y, a in years.items()}


def rows_for(cell, y0):
    par = fetch(cell["lat"], cell["lon"], y0)
    t = encode_years(par["T2M"])
    p = encode_years(par["PRECTOTCORR"])
    return [{"cell_id": cell["cell_id"], "model": "nasa", "year": y,
             "t": t.get(y), "p": p.get(y)}
            for y in sorted(set(t) | set(p))]


# ── Supabase ───────────────────────────────────────────────────────────
class Supa:
    def __init__(self, url, key):
        self.url = url.rstrip("/") + "/rest/v1/climate_cell"
        self.h = {"apikey": key, "Authorization": "Bearer " + key,
                  "Content-Type": "application/json",
                  "Prefer": "resolution=merge-duplicates,return=minimal"}

    def upsert(self, rows, tries=5):
        body = json.dumps(rows).encode()
        for t in range(tries):
            try:
                req = urllib.request.Request(self.url + "?on_conflict=cell_id,model,year",
                                             data=body, headers=self.h, method="POST")
                with urllib.request.urlopen(req, timeout=180) as r:
                    if r.status < 300:
                        return
            except urllib.error.HTTPError as e:
                detail = e.read()[:300].decode(errors="replace")
                if e.code < 500 or t == tries - 1:
                    raise RuntimeError(f"upsert {e.code}: {detail}")
            except Exception as e:
                if t == tries - 1:
                    raise
            time.sleep(4 * (t + 1))


# ── main ───────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--full", action="store_true", help=f"baixa {Y0}→hoje (seed inicial)")
    g.add_argument("--tail", action="store_true", help=f"baixa só {Y1} (refresh semanal)")
    ap.add_argument("--cells-only", action="store_true", help="só gera grid_br.json")
    ap.add_argument("--dry-run", action="store_true", help="não escreve no Supabase")
    ap.add_argument("--limit", type=int, default=0, help="processa só N células (teste)")
    ap.add_argument("--par", type=int, default=PAR, help=f"conexões simultâneas (padrão {PAR})")
    a = ap.parse_args()

    cells = build_grid()
    if a.cells_only:
        return

    tail = a.tail and not a.full
    y0 = Y1 if tail else Y0
    mode = f"tail ({Y1})" if tail else f"full ({Y0}-{Y1})"

    # O cache de progresso só vale para o --full: no --tail toda célula
    # precisa voltar, senão o ano corrente congela na data do cache.
    done = set()
    if not tail and os.path.exists(DONE):
        done = set(json.load(open(DONE, encoding="utf-8")))

    todo = [c for c in cells if c["cell_id"] not in done]
    if a.limit:
        todo = todo[:a.limit]

    supa = None
    if not a.dry_run:
        url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            sys.exit("SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY são obrigatórios "
                     "(ou use --dry-run)")
        supa = Supa(url, key)

    print(f"modo {mode} | {len(cells)} células | {len(done)} já feitas | "
          f"{len(todo)} a baixar | {a.par} paralelas", flush=True)

    t0 = time.time()
    ok, fail = 0, []
    buf = []

    def flush():
        nonlocal buf
        if buf and supa:
            supa.upsert(buf)
        buf = []

    with ThreadPoolExecutor(a.par) as ex:
        for cell, res in zip(todo, ex.map(lambda c: _safe(c, y0), todo)):
            if isinstance(res, Exception):
                fail.append((cell["cell_id"], str(res)[:80]))
                continue
            buf.extend(res)
            ok += 1
            done.add(cell["cell_id"])
            if len(buf) >= 400:
                flush()
                if not tail:
                    json.dump(sorted(done), open(DONE, "w"))
            if ok % 100 == 0:
                el = time.time() - t0
                eta = el / ok * (len(todo) - ok)
                print(f"  {ok}/{len(todo)} células · {el/60:.1f} min · "
                      f"ETA {eta/60:.1f} min", flush=True)
    flush()
    if not tail:
        json.dump(sorted(done), open(DONE, "w"))

    print(f"OK: {ok} células em {(time.time()-t0)/60:.1f} min"
          + (f" | {len(fail)} falhas" if fail else ""), flush=True)
    for cid, msg in fail[:10]:
        print(f"  FALHA {cid}: {msg}", flush=True)
    if fail:
        # Sai != 0 para o workflow não commitar/silenciar um refresh parcial,
        # mas o que deu certo já está gravado — rodar de novo retoma o resto.
        sys.exit(f"{len(fail)} células falharam; rode de novo para completar")


def _safe(cell, y0):
    try:
        return rows_for(cell, y0)
    except Exception as e:
        return e


if __name__ == "__main__":
    main()
