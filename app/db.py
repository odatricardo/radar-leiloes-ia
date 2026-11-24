import json
import psycopg
from app.config import settings
from app.models import CuradoriaFinal, LoteEntrada


def salvar_lote_avaliado(lote: LoteEntrada, resultado: CuradoriaFinal) -> None:
    sql = """
    INSERT INTO lotes_leilao (
        lote_id, leiloeiro, comitente_credor, url_lote, url_edital_pdf, modalidade,
        categoria, titulo, cidade, uf, distancia_vinhedo_km, lance_atual,
        valor_mercado_referencia, lance_maximo_recomendado, lance_maximo_diamante,
        custo_total_aquisicao, margem_liquida_projetada_pct, lucro_liquido_estimado,
        semaforo, nivel_oportunidade, score_qualidade, resumo_executivo,
        payload_completo, atualizado_em
    )
    VALUES (
        %(lote_id)s, %(leiloeiro)s, %(comitente_credor)s, %(url_lote)s, %(url_edital_pdf)s,
        %(modalidade)s, %(categoria)s, %(titulo)s, %(cidade)s, %(uf)s,
        %(distancia_vinhedo_km)s, %(lance_atual)s, %(valor_mercado_referencia)s,
        %(lance_maximo_recomendado)s, %(lance_maximo_diamante)s,
        %(custo_total_aquisicao)s, %(margem_liquida_projetada_pct)s, %(lucro_liquido_estimado)s,
        %(semaforo)s, %(nivel_oportunidade)s, %(score_qualidade)s, %(resumo_executivo)s,
        %(payload_completo)s::jsonb, NOW()
    )
    ON CONFLICT (lote_id) DO UPDATE SET
        leiloeiro = EXCLUDED.leiloeiro,
        comitente_credor = EXCLUDED.comitente_credor,
        url_lote = EXCLUDED.url_lote,
        url_edital_pdf = EXCLUDED.url_edital_pdf,
        modalidade = EXCLUDED.modalidade,
        categoria = EXCLUDED.categoria,
        titulo = EXCLUDED.titulo,
        cidade = EXCLUDED.cidade,
        uf = EXCLUDED.uf,
        distancia_vinhedo_km = EXCLUDED.distancia_vinhedo_km,
        lance_atual = EXCLUDED.lance_atual,
        valor_mercado_referencia = EXCLUDED.valor_mercado_referencia,
        lance_maximo_recomendado = EXCLUDED.lance_maximo_recomendado,
        lance_maximo_diamante = EXCLUDED.lance_maximo_diamante,
        custo_total_aquisicao = EXCLUDED.custo_total_aquisicao,
        margem_liquida_projetada_pct = EXCLUDED.margem_liquida_projetada_pct,
        lucro_liquido_estimado = EXCLUDED.lucro_liquido_estimado,
        semaforo = EXCLUDED.semaforo,
        nivel_oportunidade = EXCLUDED.nivel_oportunidade,
        score_qualidade = EXCLUDED.score_qualidade,
        resumo_executivo = EXCLUDED.resumo_executivo,
        payload_completo = EXCLUDED.payload_completo,
        atualizado_em = NOW();
    """
    params = {
        "lote_id": resultado.lote_id,
        "leiloeiro": lote.leiloeiro,
        "comitente_credor": lote.comitente_credor,
        "url_lote": lote.url_lote,
        "url_edital_pdf": lote.url_edital_pdf,
        "modalidade": resultado.triagem.modalidade_identificada,
        "categoria": resultado.triagem.categoria,
        "titulo": lote.titulo_lote,
        "cidade": lote.cidade,
        "uf": lote.uf.upper(),
        "distancia_vinhedo_km": resultado.viabilidade.distancia_vinhedo_km,
        "lance_atual": lote.lance_atual,
        "valor_mercado_referencia": lote.valor_mercado_referencia,
        "lance_maximo_recomendado": resultado.viabilidade.lance_maximo_recomendado,
        "lance_maximo_diamante": resultado.viabilidade.lance_maximo_diamante,
        "custo_total_aquisicao": resultado.viabilidade.custo_total_aquisicao,
        "margem_liquida_projetada_pct": resultado.viabilidade.margem_liquida_projetada_pct,
        "lucro_liquido_estimado": resultado.viabilidade.lucro_liquido_estimado,
        "semaforo": resultado.semaforo,
        "nivel_oportunidade": resultado.nivel_oportunidade,
        "score_qualidade": resultado.score_qualidade,
        "resumo_executivo": resultado.resumo_executivo,
        "payload_completo": json.dumps(resultado.model_dump(), ensure_ascii=False),
    }
    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()


def listar_oportunidades(
    semaforo: str | None = None,
    nivel_oportunidade: str | None = None,
    limite: int = 50,
) -> list[dict]:
    query = """
    SELECT lote_id, semaforo, nivel_oportunidade, score_qualidade, categoria, titulo,
           cidade, uf, distancia_vinhedo_km, lance_atual, valor_mercado_referencia,
           lance_maximo_recomendado, lance_maximo_diamante, margem_liquida_projetada_pct,
           lucro_liquido_estimado, resumo_executivo, url_lote, atualizado_em
    FROM lotes_leilao
    WHERE 1 = 1
    """
    params: list = []
    if semaforo:
        query += " AND semaforo = %s "
        params.append(semaforo.upper())
    if nivel_oportunidade:
        query += " AND nivel_oportunidade = %s "
        params.append(nivel_oportunidade.upper())

    query += """
    ORDER BY
        CASE nivel_oportunidade
            WHEN 'DIAMANTE' THEN 1
            WHEN 'PADRAO' THEN 2
            WHEN 'ATENCAO' THEN 3
            ELSE 4
        END ASC,
        score_qualidade DESC,
        margem_liquida_projetada_pct DESC
    LIMIT %s
    """
    params.append(limite)

    with psycopg.connect(settings.database_url, row_factory=psycopg.rows.dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return list(cur.fetchall())
