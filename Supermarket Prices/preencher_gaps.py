"""
Preenche gaps do dia atual com o último preço disponível.
Cobre produtos não coletados, indisponíveis e duplos erros.
Chamado UMA VEZ após todas as categorias serem coletadas.
"""
import sqlite3
from pathlib import Path
from datetime import date

_ROOT   = Path(__file__).resolve().parent
DB_PATH = _ROOT / "precos.db"

def preencher_gaps(con, hoje):
    inseridos = 0

    # Remove todas as cópias de hoje (recalcula do zero)
    con.execute("DELETE FROM precos WHERE data_coleta=? AND erro='copiado_dia_anterior'", (hoje,))
    con.commit()

    # Produtos com dado real hoje
    tem_hoje = set()
    for r in con.execute("""
        SELECT supermercado, nome_produto, embalagem FROM precos
        WHERE data_coleta=? AND preco_atual IS NOT NULL
          AND (erro IS NULL OR erro='input_manual')
    """, (hoje,)).fetchall():
        tem_hoje.add((r[0], r[1], r[2]))

    # Candidatos: últimos 7 dias + indisponíveis hoje + preco_nao_encontrado hoje
    candidatos = con.execute("""
        SELECT DISTINCT supermercado, categoria, grupo, marca, nome_produto,
               embalagem, cidade, uf, regiao, url
        FROM precos
        WHERE preco_atual IS NOT NULL AND erro IS NULL
          AND data_coleta >= date(?, '-7 days') AND data_coleta < ?
        UNION
        SELECT DISTINCT supermercado, categoria, grupo, marca, nome_produto,
               embalagem, cidade, uf, regiao, url
        FROM precos
        WHERE data_coleta=? AND erro IN ('produto_indisponivel','preco_nao_encontrado_todas_rotas')
    """, (hoje, hoje, hoje)).fetchall()

    for p in candidatos:
        sm, cat, grp, marca, nome, emb, cidade, uf, reg, url = p

        if (sm, nome, emb) in tem_hoje:
            continue

        ultimo = con.execute("""
            SELECT preco_atual, preco_original, em_promocao FROM precos
            WHERE supermercado=? AND nome_produto=? AND embalagem=?
              AND preco_atual IS NOT NULL AND erro IS NULL
            ORDER BY data_coleta DESC LIMIT 1
        """, (sm, nome, emb)).fetchone()

        if not ultimo:
            continue

        con.execute("""
            INSERT INTO precos
            (data_coleta, horario_coleta, supermercado, categoria, grupo, marca,
             nome_produto, embalagem, cidade, uf, regiao, preco_atual, preco_original,
             em_promocao, disponivel, url, erro, rota_css, tentativas)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,'copiado_dia_anterior',99,1)
        """, (hoje, "00:00:00", sm, cat, grp, marca, nome, emb,
              cidade, uf, reg, ultimo[0], ultimo[1], ultimo[2], url))
        inseridos += 1
        tem_hoje.add((sm, nome, emb))

    # Remove entradas de erro quando já existe copiado
    con.execute("""
        DELETE FROM precos WHERE data_coleta=?
          AND erro IN ('produto_indisponivel','preco_nao_encontrado_todas_rotas')
          AND EXISTS (
            SELECT 1 FROM precos r
            WHERE r.supermercado=precos.supermercado AND r.nome_produto=precos.nome_produto
            AND r.embalagem=precos.embalagem AND r.data_coleta=precos.data_coleta
            AND r.erro='copiado_dia_anterior'
          )
    """, (hoje,))

    con.commit()
    print(f"  → {inseridos} preços preenchidos por cópia do dia anterior")
    return inseridos

if __name__ == "__main__":
    hoje = date.today().isoformat()
    print(f"Preenchendo gaps para {hoje}...")
    con = sqlite3.connect(DB_PATH)
    preencher_gaps(con, hoje)
    con.close()
    print("✓ Concluído")
