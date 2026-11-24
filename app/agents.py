import asyncio
import json
import logging
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

logger = logging.getLogger("radar_agents")
client = genai.Client(api_key=(settings.gemini_api_key or "").strip())

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

TERMOS_BLOQUEIO_ESCOPO = (
    "sucata",
    "irrecuperável",
    "irrecuperavel",
    "moto ",
    "motocicleta",
    "4x2",
    "execução fiscal",
    "execucao fiscal",
    "trabalhista",
    "justiça do trabalho",
    "justica do trabalho",
    "penhora",
    "câmera",
    "camera",
    "alarme",
    "gabinete",
    "mesa",
    "cadeira",
    "aspirador",
    "mixer",
    "frigobar",
    "freezer",
    "expositora",
    "motogerador",
    "gerador",
    "ponte rolante",
    "torno",
    "solda",
    "peletização",
    "peletizacao",
)

INDICADORES_RETOMADA_FINANCEIRA = (
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
    "john deere",
    "cnh ",
    "safra",
    "consórcio",
    "consorcio",
)

MODELOS_SECUNDARIOS_BLOQUEADOS = (
    "whisper",
    "tts",
    "guard",
    "embed",
    "orpheus",
    "audio",
    "vision",
    "prompt-guard",
    "safety",
)

MODELOS_SECUNDARIOS_PREFERIDOS = (
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "llama-3.3-70b-versatile",
    "qwen/qwen3-32b",
    "qwen/qwen3.8-27b",
)

MODELOS_SECUNDARIOS_BAIXA_PRIORIDADE = (
    "allam",
    "8b",
    "7b",
)

ultima_fonte_ia: str = "nao_executado"
_ultimo_modelo_secundario_usado: str = ""


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


def _modelo_secundario_valido(modelo_id: str) -> bool:
    mid = (modelo_id or "").strip().lower()
    if not mid:
        return False
    return not any(b in mid for b in MODELOS_SECUNDARIOS_BLOQUEADOS)


def _prioridade_modelo_secundario(modelo_id: str) -> tuple[int, str]:
    mid = (modelo_id or "").strip().lower()
    if mid in MODELOS_SECUNDARIOS_PREFERIDOS:
        return (0, str(MODELOS_SECUNDARIOS_PREFERIDOS.index(mid)))
    if "gpt-oss" in mid and "120b" in mid:
        return (1, mid)
    if "gpt-oss" in mid:
        return (2, mid)
    if "llama-3.3" in mid or "70b" in mid:
        return (3, mid)
    if any(b in mid for b in MODELOS_SECUNDARIOS_BAIXA_PRIORIDADE):
        return (9, mid)
    return (5, mid)


def _normalizar_payload_schema(raw_content: str, schema_cls: type[SchemaT]) -> str:
    """Corrige saídas comuns de modelos menores (ex.: 'categoria': 'null' ou confiança nula)."""
    try:
        parsed = json.loads(raw_content)
    except Exception:
        return raw_content

    if not isinstance(parsed, dict):
        return raw_content

    if "categoria" in parsed and parsed["categoria"] in ("null", "None", "none", ""):
        parsed["categoria"] = None

    if schema_cls.__name__ == "TriagemResultado":
        conf = parsed.get("confianca_recuperado_financiamento")
        if conf is None or isinstance(conf, bool):
            parsed["confianca_recuperado_financiamento"] = 0
        elif isinstance(conf, str):
            try:
                parsed["confianca_recuperado_financiamento"] = max(0, min(100, int(float(conf))))
            except ValueError:
                parsed["confianca_recuperado_financiamento"] = 0

    return json.dumps(parsed, ensure_ascii=False)


async def _chamar_provedor_secundario_json(
    prompt: str,
    schema_cls: type[SchemaT],
) -> SchemaT | None:
    """Consulta provedor OpenAI-compatible quando o Gemini falha por indisponibilidade/cota."""
    global _ultimo_modelo_secundario_usado

    api_key = (settings.openai_api_key or "").strip()
    if not api_key:
        logger.info("Provedor secundário não configurado (OPENAI_API_KEY ausente).")
        return None

    base_url = (settings.openai_base_url or "https://api.groq.com/openai/v1").strip().rstrip("/")
    url = f"{base_url}/chat/completions"
    schema_json = json.dumps(schema_cls.model_json_schema(), ensure_ascii=False)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    mensagens = [
        {
            "role": "system",
            "content": (
                "Você é um assistente técnico preciso. Responda exclusivamente com um "
                "objeto JSON válido aderente ao schema a seguir, preenchendo TODOS os campos "
                "obrigatórios com tipos exatos (use JSON null real, nunca a string 'null'):\n"
                f"{schema_json}"
            ),
        },
        {"role": "user", "content": prompt},
    ]

    modelos_tentativa: list[str] = []
    modelo_cfg = (settings.openai_model or "").strip()
    if modelo_cfg and _modelo_secundario_valido(modelo_cfg):
        modelos_tentativa.append(modelo_cfg)
    for pref in MODELOS_SECUNDARIOS_PREFERIDOS:
        if pref not in modelos_tentativa and _modelo_secundario_valido(pref):
            modelos_tentativa.append(pref)

    buscou_lista = False
    async with httpx.AsyncClient(timeout=25.0) as http:
        idx = 0
        while idx < len(modelos_tentativa):
            modelo_atual = modelos_tentativa[idx]
            payload = {
                "model": modelo_atual,
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
                "messages": mensagens,
            }
            logger.info("Acionando provedor secundário (%s) em %s...", modelo_atual, base_url)
            try:
                resp = await http.post(url, json=payload, headers=headers)
                if resp.status_code in (400, 404) and not buscou_lista:
                    buscou_lista = True
                    logger.warning(
                        "Modelo '%s' retornou %s em %s; buscando modelos ativos...",
                        modelo_atual,
                        resp.status_code,
                        base_url,
                    )
                    try:
                        models_resp = await http.get(f"{base_url}/models", headers=headers)
                        models_resp.raise_for_status()
                        items = models_resp.json().get("data", [])
                        descobertos = [
                            (item.get("id") or "").strip()
                            for item in items
                            if _modelo_secundario_valido((item.get("id") or "").strip())
                        ]
                        descobertos.sort(key=_prioridade_modelo_secundario)
                        restante_atual = modelos_tentativa[idx + 1 :]
                        combinados: list[str] = []
                        for m_id in descobertos + restante_atual:
                            if m_id and m_id != modelo_atual and m_id not in combinados:
                                combinados.append(m_id)
                        modelos_tentativa = modelos_tentativa[: idx + 1] + combinados
                    except Exception as list_exc:
                        logger.warning("Falha ao listar modelos em %s: %s", base_url, list_exc)
                    idx += 1
                    continue

                resp.raise_for_status()
                data = resp.json()
                conteudo = data["choices"][0]["message"]["content"]
                conteudo_limpo = _normalizar_payload_schema(conteudo, schema_cls)
                resultado = schema_cls.model_validate_json(conteudo_limpo)
                _ultimo_modelo_secundario_usado = modelo_atual
                logger.info("Provedor secundário (%s) respondeu com sucesso.", modelo_atual)
                return resultado
            except Exception as exc:
                logger.warning("Falha no provedor secundário (%s): %s", modelo_atual, exc)
                idx += 1

    return None


async def _gerar_estruturado_resiliente(
    prompt: str,
    schema_cls: type[SchemaT],
    modelo_principal: str,
    modelo_fallback: str | None = None,
    temperature: float = 0.1,
    max_tentativas: int = 2,
) -> SchemaT | None:
    global ultima_fonte_ia

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
                ultima_fonte_ia = f"gemini ({modelo})"
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

    secundario = await _chamar_provedor_secundario_json(prompt, schema_cls)
    if secundario is not None:
        modelo_ok = _ultimo_modelo_secundario_usado or settings.openai_model
        ultima_fonte_ia = f"provedor_secundario ({modelo_ok})"
        return secundario

    if settings.permitir_fallback_regras:
        ultima_fonte_ia = "regras_contingencia"
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
    return (
        lote.texto_edital
        or "Edital completo não anexado ou PDF indisponível; analisar descrição do lote com rigor."
    )


def _detectar_categoria_permitida(texto: str) -> str | None:
    if any(
        p in texto
        for p in (
            "caminhão",
            "caminhao",
            "cavalo mecânico",
            "cavalo mecanico",
            "carreta",
            "semirreboque",
            "reboque",
        )
    ):
        return "caminhao"
    if any(
        p in texto
        for p in (
            "trator",
            "colheitadeira",
            "pulverizador",
            "máquina agrícola",
            "maquina agricola",
            "retroescavadeira",
            "pá carregadeira",
            "pa carregadeira",
        )
    ):
        return "maquina_agricola"
    if any(
        p in texto
        for p in (
            "apartamento",
            "casa ",
            "terreno",
            "galpão",
            "galpao",
            "imóvel",
            "imovel",
            "matrícula",
            "matricula",
            "lote urbano",
        )
    ):
        return "imovel"
    if any(
        p in texto
        for p in (
            "caminhonete",
            "picape",
            "pickup",
            "4x4",
            "hilux",
            "ranger",
            "amarok",
            "s10",
            "sw4",
            "frontier",
            "l200",
        )
    ):
        return "caminhonete_4x4"
    return None


def _triagem_por_regras(lote: LoteEntrada, distancia_km: float) -> TriagemResultado:
    global ultima_fonte_ia
    ultima_fonte_ia = "regras_contingencia"

    texto = f"{lote.titulo_lote} {lote.descricao_lote} {lote.comitente_credor}".lower()
    if distancia_km > settings.raio_maximo_km:
        return TriagemResultado(
            aprovado_triagem=False,
            confianca_recuperado_financiamento=0,
            categoria=None,
            modalidade_identificada="",
            motivo_descarte=(
                f"Distância ({distancia_km:.1f} km) acima do limite de "
                f"{settings.raio_maximo_km:.0f} km."
            ),
        )

    if any(p in texto for p in TERMOS_BLOQUEIO_ESCOPO):
        return TriagemResultado(
            aprovado_triagem=False,
            confianca_recuperado_financiamento=0,
            categoria=None,
            modalidade_identificada="",
            motivo_descarte="Item fora do escopo operacional, condição restrita ou processo judicial bloqueado.",
        )

    categoria = _detectar_categoria_permitida(texto)
    if not categoria:
        return TriagemResultado(
            aprovado_triagem=False,
            confianca_recuperado_financiamento=0,
            categoria=None,
            modalidade_identificada="",
            motivo_descarte="Categoria não identificada entre caminhonete_4x4, caminhao, maquina_agricola ou imovel.",
        )

    fiduciaria_ok = any(k in texto for k in INDICADORES_RETOMADA_FINANCEIRA)
    if not fiduciaria_ok:
        return TriagemResultado(
            aprovado_triagem=False,
            confianca_recuperado_financiamento=15,
            categoria=None,
            modalidade_identificada="",
            motivo_descarte="Sem indício claro de alienação fiduciária ou recuperado de financiamento.",
        )

    return TriagemResultado(
        aprovado_triagem=True,
        confianca_recuperado_financiamento=80,
        categoria=categoria,
        modalidade_identificada="Alienação Fiduciária / Retomada (fallback regras)",
        motivo_descarte=None,
    )


def _auditoria_por_regras(lote: LoteEntrada, texto_edital: str) -> AuditoriaEditalResultado:
    global ultima_fonte_ia
    ultima_fonte_ia = "regras_contingencia"

    base_txt = (
        f"{lote.titulo_lote} {lote.descricao_lote} {lote.comitente_credor} {texto_edital}".lower()
    )
    fiduciaria_ok = any(k in base_txt for k in INDICADORES_RETOMADA_FINANCEIRA)
    sinistro_grave = any(
        k in base_txt
        for k in ("sinistro grande monta", "sinistro grave", "enchente", "incêndio", "incendio")
    )
    restricao = any(
        k in base_txt
        for k in ("bloqueio judicial", "indisponibilidade", "ação judicial", "acao judicial")
    )

    posse = (
        "com_chave"
        if "com chave" in base_txt
        else ("desocupado" if "desocupado" in base_txt else "nao_informado")
    )
    debitos = (
        "credor_vendedor"
        if any(
            k in base_txt
            for k in ("débitos quitados", "debitos quitados", "por conta do comitente")
        )
        else "misto_ou_incerto"
    )

    return AuditoriaEditalResultado(
        recuperado_financiamento_confirmado=fiduciaria_ok,
        posse_ou_chave=posse,
        responsavel_debitos_anteriores=debitos,
        valor_debitos_repassados_estimado=0.0,
        comissao_leiloeiro_pct=5.0,
        taxa_administrativa_patio=1500.0,
        sinistro_ou_avaria_grave=sinistro_grave,
        restricao_documental_ou_judicial=restricao,
        resumo_clausulas=(
            "Avaliação preliminar gerada por regras devido a instabilidade temporária da API de IA."
        ),
        alertas_criticos=[
            "API de IA indisponível no momento: validar cláusulas do edital manualmente antes do lance."
        ],
    )


async def agente_2_triagem(lote: LoteEntrada, distancia_km: float) -> TriagemResultado:
    prompt = f"""
Você é um especialista sênior em leilões brasileiros. Avalie se o lote atende aos critérios estritos:
- Base: {settings.base_cidade}/{settings.base_uf}, raio máximo de {settings.raio_maximo_km} km (distância calculada: {distancia_km:.1f} km).
- Categorias permitidas: caminhonete_4x4, caminhao, maquina_agricola, imovel.
- Origem obrigatória: RECUPERADO DE FINANCIAMENTO / ALIENAÇÃO FIDUCIÁRIA (retomada bancária, financeira ou banco de montadora).
- Rejeite: leilões judiciais trabalhistas/fiscais, sucatas, carros 4x2 comuns, motos, equipamentos comerciais/eletrônicos/móveis fora das categorias permitidas ou distância acima de {settings.raio_maximo_km} km.
- Se não houver indício de alienação fiduciária/retomada financeira ou se o bem não for uma das 4 categorias permitidas, reprove (aprovado_triagem=false).
- Preencha sempre 'confianca_recuperado_financiamento' (0 a 100) e, se rejeitar, 'motivo_descarte' em 1 frase objetiva.

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
    elif is_diamante and (posse_ok or debitos_ok):
        semaforo = "AMARELO"
        nivel = "DIAMANTE"
    elif is_diamante:
        semaforo = "AMARELO"
        nivel = "ATENCAO"
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
        score_base = (
            85
            if (nivel == "DIAMANTE" and semaforo == "VERDE")
            else (
                70
                if nivel == "DIAMANTE"
                else (60 if semaforo == "VERDE" else (45 if semaforo == "AMARELO" else 25))
            )
        )
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
        if nivel == "DIAMANTE" and semaforo == "VERDE":
            score_base = max(score_base, 90)
        elif nivel == "DIAMANTE":
            score_base = min(max(score_base, 70), 84)
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
