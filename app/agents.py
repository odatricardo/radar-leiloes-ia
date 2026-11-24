import asyncio
import json
import math
import random
from typing import TypeVar

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

SchemaT = TypeVar("SchemaT", bound=BaseModel)

client = genai.Client(api_key=settings.gemini_api_key)

VINHEDO_LAT = -23.0297
VINHEDO_LON = -46.9753
NON_RETRY_STATUS_CODES = (
    "400",
    "403",
    "404",
    "INVALID_ARGUMENT",
    "PERMISSION_DENIED",
    "NOT_FOUND",
)
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


def listar_modelos_disponiveis() -> list[str]:
    """Consulta na API do Gemini os modelos ativos para a chave atual."""
    modelos_ativos: list[str] = []
    try:
        for m in client.models.list():
            nome = (getattr(m, "name", "") or "").replace("models/", "").strip()
            acoes = (
                getattr(m, "supported_actions", None)
                or getattr(m, "supported_generation_methods", None)
                or []
            )
            if not nome or "gemini" not in nome.lower():
                continue
            if any(
                b in nome.lower()
                for b in ("embedding", "imagen", "veo", "aqa", "tts", "audio", "robotics")
            ):
                continue
            if acoes and "generateContent" not in acoes:
                continue
            if nome not in modelos_ativos:
                modelos_ativos.append(nome)
    except Exception:
        pass
    return modelos_ativos or settings.obter_cadeia_modelos()


async def _chamar_provedor_secundario_json(
    prompt: str,
    schema_cls: type[SchemaT],
) -> SchemaT | None:
    """Fallback opcional para provedor OpenAI-compatible quando o Gemini retorna 503/429."""
    if not settings.openai_api_key:
        return None

    base_url = (settings.openai_base_url or "https://api.openai.com/v1").rstrip("/")
    url = f"{base_url}/chat/completions"
    schema_json = json.dumps(schema_cls.model_json_schema(), ensure_ascii=False)

    payload = {
        "model": settings.openai_model,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "Responda exclusivamente com um objeto JSON válido aderente ao schema a seguir:\n"
                    f"{schema_json}"
                ),
            },
            {"role": "user", "content": prompt},
        ],
    }
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as http:
            resp = await http.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            conteudo = data["choices"][0]["message"]["content"]
            return schema_cls.model_validate_json(conteudo)
    except Exception:
        return None


async def _gerar_estruturado_resiliente(
    prompt: str,
    schema_cls: type[SchemaT],
    modelo_principal: str,
    modelo_fallback: str | None = None,
    temperature: float = 0.1,
    max_tentativas: int = 2,
) -> SchemaT | None:
    """
    1. Tenta Gemini com retry curto em erros transitórios (503/429).
    2. Se falhar, tenta provedor secundário configurado (se houver).
    3. Se todos falharem por indisponibilidade/cota, retorna None para acionar fallback por regras.
    """
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=schema_cls,
        temperature=temperature,
    )

    modelos = settings.obter_cadeia_modelos(modelo_principal)
    if modelo_fallback:
        nome_fb = modelo_fallback.replace("models/", "").strip()
        if nome_fb and nome_fb not in modelos:
            modelos.insert(1, nome_fb)

    ultimo_erro: Exception | None = None
    erro_transitorio: Exception | None = None

    for idx_modelo, modelo in enumerate(modelos):
        for tentativa in range(max_tentativas):
            try:
                resp = await client.aio.models.generate_content(
                    model=modelo,
                    contents=prompt,
                    config=config,
                )
                return schema_cls.model_validate_json(resp.text)
            except Exception as exc:
                ultimo_erro = exc
                msg = str(exc)
                eh_erro_fixo = any(cod in msg for cod in NON_RETRY_STATUS_CODES)
                eh_transitorio = (not eh_erro_fixo) and any(
                    marcador in msg for marcador in TRANSIENT_MARKERS
                )
                if eh_transitorio and erro_transitorio is None:
                    erro_transitorio = exc
                if eh_erro_fixo or not eh_transitorio or tentativa == max_tentativas - 1:
                    break
                espera = (0.8 * (tentativa + 1)) + random.uniform(0.1, 0.35)
                await asyncio.sleep(espera)

        if idx_modelo == 0 and erro_transitorio is not None:
            break

    # Tenta provedor secundário antes de desistir da LLM
    secundario = await _chamar_provedor_secundario_json(prompt, schema_cls)
    if secundario is not None:
        return secundario

    if settings.permitir_fallback_regras:
        return None

    raise erro_transitorio or ultimo_erro or RuntimeError("Falha ao consultar modelo de IA.")


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
            headers = {"User-Agent": "RadarLeiloesIA/1.3"}
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


def _triagem_por_regras(lote: LoteEntrada, distancia_km: float) -> TriagemResultado:
    texto = f"{lote.titulo_lote} {lote.descricao_lote} {lote.comitente_credor}".lower()
    if distancia_km > settings.raio_maximo_km:
        return TriagemResultado(
            aprovado=False,
            categoria=None,
            modalidade_identificada=None,
            motivo_descarte=f"Distância ({distancia_km:.1f} km) acima do limite de {settings.raio_maximo_km:.0f} km.",
        )
    if any(p in texto for p in ("sucata", "irrecuperável", "irrecuperavel", "moto ", "motocicleta")):
        return TriagemResultado(
            aprovado=False,
            categoria=None,
            modalidade_identificada=None,
            motivo_descarte="Categoria ou condição fora do escopo (sucata ou motocicleta).",
        )

    categoria = "caminhonete_4x4"
    if any(p in texto for p in ("caminhão", "caminhao", "cavalo mecânico", "carreta")):
        categoria = "caminhao"
    elif any(p in texto for p in ("trator", "colheitadeira", "pulverizador", "máquina agrícola", "maquina agricola")):
        categoria = "maquina_agricola"
    elif any(p in texto for p in ("apartamento", "casa ", "terreno", "galpão", "galpao", "imóvel", "imovel", "matrícula")):
        categoria = "imovel"

    return TriagemResultado(
        aprovado=True,
        categoria=categoria,
        modalidade_identificada="Alienação Fiduciária / Retomada (fallback regras)",
        motivo_descarte=None,
    )


def _auditoria_por_regras(lote: LoteEntrada, texto_edital: str) -> AuditoriaEditalResultado:
    base_txt = f"{lote.titulo_lote} {lote.descricao_lote} {lote.comitente_credor} {texto_edital}".lower()
    fiduciaria_ok = any(
        k in base_txt
        for k in (
            "alienação fiduciária",
            "alienacao fiduciaria",
            "recuperado de financiamento",
            "retomada",
            "banco ",
            "financeira",
            "bv ",
            "santander",
            "bradesco",
            "itaú",
            "itau",
        )
    )
    sinistro_grave = any(k in base_txt for k in ("sinistro grande monta", "sinistro grave", "enchente", "incêndio", "incendio"))
    restricao = any(k in base_txt for k in ("bloqueio judicial", "indisponibilidade", "ação judicial", "acao judicial"))

    posse = "com_chave" if "com chave" in base_txt else ("desocupado" if "desocupado" in base_txt else "nao_informado")
    debitos = "credor_vendedor" if any(k in base_txt for k in ("débitos quitados", "debitos quitados", "por conta do comitente")) else "misto_ou_incerto"

    return AuditoriaEditalResultado(
        recuperado_financiamento_confirmado=fiduciaria_ok,
        posse_ou_chave=posse,
        responsavel_debitos_anteriores=debitos,
        valor_debitos_repassados_estimado=0.0,
        comissao_leiloeiro_pct=5.0,
        taxa_administrativa_estimada=1500.0,
        sinistro_ou_avaria_grave=sinistro_grave,
        restricao_documental_ou_judicial=restricao,
        resumo_clausulas="Avaliação preliminar gerada por regras devido a instabilidade temporária da API de IA.",
        alertas_criticos=["API de IA indisponível no momento: validar cláusulas do edital manualmente antes do lance."],
    )


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
    res = await _gerar_estruturado_resiliente(
        prompt=prompt,
        schema_cls=TriagemResultado,
        modelo_principal=settings.gemini_model_fast,
        modelo_fallback=settings.gemini_model_audit,
        temperature=0.1,
    )
    return res if res is not None else _triagem_por_regras(lote, distancia_km)


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
    res = await _gerar_estruturado_resiliente(
        prompt=prompt,
        schema_cls=AuditoriaEditalResultado,
        modelo_principal=settings.gemini_model_audit,
        modelo_fallback=settings.gemini_model_fast,
        temperature=0.1,
    )
    return res if res is not None else _auditoria_por_regras(lote, conteudo_edital)


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
    dados_curadoria = await _gerar_estruturado_resiliente(
        prompt=prompt,
        schema_cls=ResumoCuradoriaSchema,
        modelo_principal=settings.gemini_model_fast,
        modelo_fallback=settings.gemini_model_audit,
        temperature=0.2,
    )

    if dados_curadoria is None:
        score_base = 75 if nivel == "DIAMANTE" else (60 if semaforo == "VERDE" else (45 if semaforo == "AMARELO" else 25))
        resumo_exec = (
            f"Análise por regras (API em instabilidade): ROI líquido estimado de "
            f"{viabilidade.margem_liquida_projetada_pct:.1f}% a {viabilidade.distancia_vinhedo_km:.1f} km de Vinhedo/SP."
        )
        acao_rec = (
            f"Conferir edital manualmente e respeitar teto de lance de R$ {viabilidade.lance_maximo_recomendado:,.2f} "
            f"(Diamante até R$ {viabilidade.lance_maximo_diamante:,.2f})."
        )
    else:
        score_base = dados_curadoria.score_qualidade
        if nivel == "DIAMANTE":
            score_base = max(score_base, 90)
        elif semaforo == "VERMELHO":
            score_base = min(score_base, 35)
        resumo_exec = dados_curadoria.resumo_executivo
        acao_rec = dados_curadoria.acao_recomendada

    return CuradoriaFinal(
        lote_id=lote.gerar_lote_id(),
        semaforo=semaforo,
        nivel_oportunidade=nivel,
        score_qualidade=score_base,
        resumo_executivo=resumo_exec,
        acao_recomendada=acao_rec,
        triagem=triagem,
        auditoria=auditoria,
        viabilidade=viabilidade,
    )
