import json
import httpx
from google import genai
from google.genai import types
from app.config import settings
from app.finance import MARGEM_DIAMANTE_PCT, calcular_viabilidade, obter_margem_minima
from app.models import (
    AuditoriaEditalResultado,
    CuradoriaFinal,
    LoteEntrada,
    NivelOportunidade,
    SemaforoStatus,
    TriagemResultado,
)
from app.pdf_reader import extrair_texto_pdf_url

client = genai.Client(api_key=settings.gemini_api_key)


async def resolver_distancia_km(lote: LoteEntrada) -> float:
    if lote.distancia_vinhedo_km is not None:
        return lote.distancia_vinhedo_km

    if settings.google_maps_api_key:
        origem = f"{settings.base_cidade}, {settings.base_uf}, Brasil"
        destino = f"{lote.cidade}, {lote.uf}, Brasil"
        url = "https://maps.googleapis.com/maps/api/distancematrix/json"
        params = {
            "origins": origem,
            "destinations": destino,
            "key": settings.google_maps_api_key,
            "language": "pt-BR",
        }
        async with httpx.AsyncClient(timeout=10.0) as http:
            resp = await http.get(url, params=params)
            data = resp.json()
            try:
                metros = data["rows"][0]["elements"][0]["distance"]["value"]
                return metros / 1000.0
            except (KeyError, IndexError):
                pass

    return 120.0 if lote.uf.upper() == "SP" else 380.0


async def preparar_texto_edital(lote: LoteEntrada) -> str:
    texto_pdf = await extrair_texto_pdf_url(lote.url_edital_pdf)
    if texto_pdf and lote.texto_edital:
        return f"{lote.texto_edital}\n\n[CONTEUDO EXTRAIDO DO PDF DO EDITAL]\n{texto_pdf}"
    if texto_pdf:
        return texto_pdf
    return lote.texto_edital or "Edital completo não anexado ou PDF indisponível; analisar descrição do lote com rigor."


async def agente_2_triagem(lote: LoteEntrada, distancia_km: float) -> TriagemResultado:
    prompt = f"""
Você é um especialista sênior em leilões brasileiros. Avalie se o lote atende aos critérios estritos:
- Base: {settings.base_cidade}/{settings.base_uf}, raio máximo de {settings.raio_maximo_km} km (distância calculada: {distancia_km:.1f} km).
- Categorias permitidas: caminhonete_4x4, caminhao, maquina_agricola, imovel.
- Origem obrigatória: RECUPERADO DE FINANCIAMENTO / ALIENAÇÃO FIDUCIÁRIA (retomada bancária, financeira ou banco de montadora).
- Rejeite: leilões judiciais trabalhistas/fiscais, sucatas, carros 4x2 comuns, motos ou distância acima de {settings.raio_maximo_km} km.

Dados do lote:
Leiloeiro: {lote.leiloeiro}
Comitente: {lote.comitente_credor}
Título: {lote.titulo_lote}
Descrição: {lote.descricao_lote}
Cidade/UF: {lote.cidade}/{lote.uf}
"""
    response = await client.aio.models.generate_content(
        model=settings.gemini_model_fast,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=TriagemResultado,
            temperature=0.1,
        ),
    )
    return TriagemResultado.model_validate_json(response.text)


async def agente_3_auditor_edital(lote: LoteEntrada, categoria: str) -> AuditoriaEditalResultado:
    conteudo_edital = await preparar_texto_edital(lote)
    prompt = f"""
Você é um auditor jurídico e operacional de leilões extrajudiciais (Lei 9.514/97 e Decreto-Lei 911/69).
Categoria do bem: {categoria}
Comitente/Credor: {lote.comitente_credor}
Descrição do lote: {lote.descricao_lote}
Trechos/Texto do edital:
{conteudo_edital[:32000]}

Extraia com rigor:
1. Se é recuperado de financiamento / alienação fiduciária confirmado.
2. Situação de posse/chave (com_chave, sem_chave, desocupado, ocupado, nao_informado).
3. Quem paga débitos anteriores (credor_vendedor, arrematante, misto_ou_incerto) e valor estimado se houver repasse ao arrematante.
4. Comissão do leiloeiro (%) e taxa administrativa/pátio (R$).
5. Sinistro/avaria grave ou restrição documental/judicial e alertas críticos.
"""
    response = await client.aio.models.generate_content(
        model=settings.gemini_model_audit,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=AuditoriaEditalResultado,
            temperature=0.1,
        ),
    )
    return AuditoriaEditalResultado.model_validate_json(response.text)


async def agente_4_e_5_curador(
    lote: LoteEntrada,
    triagem: TriagemResultado,
    auditoria: AuditoriaEditalResultado,
    distancia_km: float,
) -> CuradoriaFinal:
    assert triagem.categoria is not None
    viabilidade = calcular_viabilidade(lote, triagem.categoria, auditoria, distancia_km)
    margem_meta = obter_margem_minima(triagem.categoria)

    divida_pct_mercado = (
        (auditoria.valor_debitos_repassados_estimado / lote.valor_mercado_referencia) * 100.0
        if lote.valor_mercado_referencia > 0
        else 0.0
    )

    bloqueio_grave = (
        not auditoria.recuperado_financiamento_confirmado
        or auditoria.sinistro_ou_avaria_grave
        or auditoria.restricao_documental_ou_judicial
        or divida_pct_mercado > 8.0
        or viabilidade.margem_liquida_projetada_pct < margem_meta
    )

    posse_ok = auditoria.posse_ou_chave in ("com_chave", "desocupado")
    debitos_ok = auditoria.responsavel_debitos_anteriores == "credor_vendedor"

    if bloqueio_grave:
        semaforo: SemaforoStatus = "VERMELHO"
        nivel: NivelOportunidade = "DESCARTADO"
    elif viabilidade.margem_liquida_projetada_pct >= margem_meta and posse_ok:
        semaforo = "VERDE"
        nivel = "DIAMANTE" if (viabilidade.oportunidade_diamante and debitos_ok) else "PADRAO"
    else:
        semaforo = "AMARELO"
        nivel = "DIAMANTE" if viabilidade.oportunidade_diamante else "ATENCAO"

    prompt = f"""
Você é um gestor de ativos em {settings.base_cidade}/{settings.base_uf}.
Gere um score de qualidade (0 a 100), um resumo executivo direto (até 3 linhas) e a ação recomendada para o arrematante.

Lote: {lote.titulo_lote} ({lote.cidade}/{lote.uf}, {viabilidade.distancia_vinhedo_km} km)
Semáforo: {semaforo} | Nível de Oportunidade: {nivel} (Meta Diamante >= {MARGEM_DIAMANTE_PCT:.0f}%)
Lance atual: R$ {lote.lance_atual:,.2f} | Valor mercado: R$ {lote.valor_mercado_referencia:,.2f}
Lance máximo recomendado (Meta Base {margem_meta:.0f}%): R$ {viabilidade.lance_maximo_recomendado:,.2f}
Lance máximo Diamante (>= {MARGEM_DIAMANTE_PCT:.0f}%): R$ {viabilidade.lance_maximo_diamante:,.2f}
Margem líquida projetada: {viabilidade.margem_liquida_projetada_pct:.1f}%
Lucro líquido estimado: R$ {viabilidade.lucro_liquido_estimado:,.2f}
Posse/Chave: {auditoria.posse_ou_chave} | Débitos: {auditoria.responsavel_debitos_anteriores}
Alertas: {", ".join(auditoria.alertas_criticos) or "Nenhum"}
"""
    resumo_resp = await client.aio.models.generate_content(
        model=settings.gemini_model_fast,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema={
                "type": "OBJECT",
                "properties": {
                    "score_qualidade": {"type": "INTEGER"},
                    "resumo_executivo": {"type": "STRING"},
                    "acao_recomendada": {"type": "STRING"},
                },
                "required": ["score_qualidade", "resumo_executivo", "acao_recomendada"],
            },
            temperature=0.2,
        ),
    )
    dados_curadoria = json.loads(resumo_resp.text)

    score_base = max(0, min(100, int(dados_curadoria["score_qualidade"])))
    if nivel == "DIAMANTE":
        score_base = max(score_base, 90)

    return CuradoriaFinal(
        lote_id=lote.gerar_lote_id(),
        semaforo=semaforo,
        nivel_oportunidade=nivel,
        score_qualidade=score_base,
        resumo_executivo=dados_curadoria["resumo_executivo"],
        acao_recomendada=dados_curadoria["acao_recomendada"],
        triagem=triagem,
        auditoria=auditoria,
        viabilidade=viabilidade,
    )