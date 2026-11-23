import asyncio
import math
import httpx
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
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

VINHEDO_LAT = -23.0297
VINHEDO_LON = -46.9753
TRANSIENT_MARKERS = (
    "503",
    "UNAVAILABLE",
    "429",
    "RESOURCE_EXHAUSTED",
    "DEADLINE_EXCEEDED",
    "500",
    "502",
    "504",
)


class ResumoCuradoriaSchema(BaseModel):
    score_qualidade: int = Field(..., ge=0, le=100)
    resumo_executivo: str
    acao_recomendada: str


async def _gerar_com_retry(
    contents: str,
    config: types.GenerateContentConfig,
    modelo_principal: str,
    modelo_fallback: str | None = None,
    max_tentativas: int = 4,
):
    modelos = [modelo_principal]
    if modelo_fallback and modelo_fallback != modelo_principal:
        modelos.append(modelo_fallback)

    ultimo_erro: Exception | None = None
    for modelo in modelos:
        for tentativa in range(max_tentativas):
            try:
                return await client.aio.models.generate_content(
                    model=modelo,
                    contents=contents,
                    config=config,
                )
            except Exception as exc:
                ultimo_erro = exc
                msg = str(exc)
                eh_transitorio = any(marcador in msg for marcador in TRANSIENT_MARKERS)
                if not eh_transitorio or tentativa == max_tentativas - 1:
                    break
                await asyncio.sleep(2**tentativa)

    assert ultimo_erro is not None
    raise ultimo_erro


def _haversine_rodoviario_estimado(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    raio_terra_km = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    distancia_reta = 2 * raio_terra_km * math.asin(math.sqrt(a))
    return round(distancia_reta * 1.25, 1)


async def resolver_distancia_km(lote: LoteEntrada) -> float:
    if lote.distancia_vinhedo_km is not None:
        return lote.distancia_vinhedo_km

    cidade_norm = lote.cidade.strip().lower()
    uf_norm = lote.uf.strip().upper()
    if cidade_norm == "vinhedo" and uf_norm == "SP":
        return 10.0

    async with httpx.AsyncClient(timeout=10.0) as http:
        if settings.google_maps_api_key:
            origem = f"{settings.base_cidade}, {settings.base_uf}, Brasil"
            destino = f"{lote.cidade}, {uf_norm}, Brasil"
            url = "https://maps.googleapis.com/maps/api/distancematrix/json"
            params = {
                "origins": origem,
                "destinations": destino,
                "key": settings.google_maps_api_key,
                "language": "pt-BR",
            }
            try:
                resp = await http.get(url, params=params)
                data = resp.json()
                metros = data["rows"][0]["elements"][0]["distance"]["value"]
                return round(metros / 1000.0, 1)
            except Exception:
                pass

        try:
            nom_url = "https://nominatim.openstreetmap.org/search"
            nom_params = {
                "city": lote.cidade,
                "state": uf_norm,
                "country": "Brazil",
                "format": "json",
                "limit": 1,
            }
            headers = {"User-Agent": "RadarLeiloesIA/1.2"}
            nom_resp = await http.get(nom_url, params=nom_params, headers=headers)
            resultados = nom_resp.json()
            if resultados:
                lat = float(resultados[0]["lat"])
                lon = float(resultados[0]["lon"])
                return _haversine_rodoviario_estimado(VINHEDO_LAT, VINHEDO_LON, lat, lon)
        except Exception:
            pass

    return 250.0 if uf_norm == "SP" else 650.0


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
- Se rejeitar, preencha 'motivo_descarte' de forma clara e objetiva em 1 frase.

Dados do lote:
Leiloeiro: {lote.leiloeiro}
Comitente: {lote.comitente_credor}
Título: {lote.titulo_lote}
Descrição: {lote.descricao_lote}
Cidade/UF: {lote.cidade}/{lote.uf}
"""
    response = await _gerar_com_retry(
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=TriagemResultado,
            temperature=0.1,
        ),
        modelo_principal=settings.gemini_model_fast,
        modelo_fallback=settings.gemini_model_audit,
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
    response = await _gerar_com_retry(
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=AuditoriaEditalResultado,
            temperature=0.1,
        ),
        modelo_principal=settings.gemini_model_audit,
        modelo_fallback=settings.gemini_model_fast,
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

    is_diamante = (
        viabilidade.oportunidade_diamante
        or viabilidade.margem_liquida_projetada_pct >= MARGEM_DIAMANTE_PCT
    )

    bloqueio_grave = (
        not auditoria.recuperado_financiamento_confirmado
        or auditoria.sinistro_ou_avaria_grave
        or auditoria.restricao_documental_ou_judicial
        or divida_pct_mercado > 8.0
        or (not is_diamante and viabilidade.margem_liquida_projetada_pct < margem_meta)
    )

    posse_ok = auditoria.posse_ou_chave in ("com_chave", "desocupado")
    debitos_ok = auditoria.responsavel_debitos_anteriores == "credor_vendedor"

    if bloqueio_grave:
        semaforo: SemaforoStatus = "VERMELHO"
        nivel: NivelOportunidade = "DESCARTADO"
    elif is_diamante and posse_ok and debitos_ok:
        semaforo = "VERDE"
        nivel = "DIAMANTE"
    elif is_diamante:
        semaforo = "AMARELO"
        nivel = "DIAMANTE"
    elif viabilidade.margem_liquida_projetada_pct >= margem_meta and posse_ok:
        semaforo = "VERDE"
        nivel = "PADRAO"
    else:
        semaforo = "AMARELO"
        nivel = "ATENCAO"

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
    resumo_resp = await _gerar_com_retry(
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ResumoCuradoriaSchema,
            temperature=0.2,
        ),
        modelo_principal=settings.gemini_model_fast,
        modelo_fallback=settings.gemini_model_audit,
    )
    dados_curadoria = ResumoCuradoriaSchema.model_validate_json(resumo_resp.text)

    score_base = dados_curadoria.score_qualidade
    if nivel == "DIAMANTE":
        score_base = max(score_base, 90)
    elif semaforo == "VERMELHO":
        score_base = min(score_base, 35)

    return CuradoriaFinal(
        lote_id=lote.gerar_lote_id(),
        semaforo=semaforo,
        nivel_oportunidade=nivel,
        score_qualidade=score_base,
        resumo_executivo=dados_curadoria.resumo_executivo,
        acao_recomendada=dados_curadoria.acao_recomendada,
        triagem=triagem,
        auditoria=auditoria,
        viabilidade=viabilidade,
    )
