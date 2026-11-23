import asyncio
from fastapi import FastAPI, HTTPException, Query
from app.agents import (
    agente_2_triagem,
    agente_3_auditor_edital,
    agente_4_e_5_curador,
    resolver_distancia_km,
)
from app.config import settings
from app.db import listar_oportunidades, salvar_lote_avaliado
from app.models import CuradoriaFinal, LoteEntrada, ProcessamentoLoteResponse

app = FastAPI(title="Motor Multiagente de Leilões - Recuperados de Financiamento", version="1.2.0")


async def avaliar_lote_detalhado(lote: LoteEntrada) -> ProcessamentoLoteResponse:
    distancia_km = await resolver_distancia_km(lote)
    if distancia_km > settings.raio_maximo_km:
        return ProcessamentoLoteResponse(
            aprovado=False,
            motivo_rejeicao=(
                f"Distância estimada de {distancia_km:.1f} km até {settings.base_cidade}/{settings.base_uf} "
                f"excede o limite máximo de {settings.raio_maximo_km} km."
            ),
        )

    triagem = await agente_2_triagem(lote, distancia_km)
    if not triagem.aprovado_triagem or not triagem.categoria:
        motivo = triagem.motivo_descarte or "Categoria fora do escopo ou origem não confirmada como alienação fiduciária."
        return ProcessamentoLoteResponse(
            aprovado=False,
            motivo_rejeicao=f"Reprovado na Triagem: {motivo}",
        )

    auditoria = await agente_3_auditor_edital(lote, triagem.categoria)
    resultado = await agente_4_e_5_curador(lote, triagem, auditoria, distancia_km)
    await asyncio.to_thread(salvar_lote_avaliado, lote, resultado)
    return ProcessamentoLoteResponse(aprovado=True, curadoria=resultado)


async def processar_lote(lote: LoteEntrada) -> CuradoriaFinal | None:
    resposta = await avaliar_lote_detalhado(lote)
    return resposta.curadoria


@app.post("/api/v1/lotes/avaliar", response_model=ProcessamentoLoteResponse)
async def avaliar_lote_endpoint(lote: LoteEntrada):
    try:
        return await avaliar_lote_detalhado(lote)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/v1/lotes/lote-batch")
async def avaliar_lotes_em_paralelo(lotes: list[LoteEntrada]):
    resultados = await asyncio.gather(*(avaliar_lote_detalhado(l) for l in lotes), return_exceptions=True)
    aprovados = [r.curadoria for r in resultados if isinstance(r, ProcessamentoLoteResponse) and r.aprovado and r.curadoria]
    descartados = [
        r.motivo_rejeicao
        for r in resultados
        if isinstance(r, ProcessamentoLoteResponse) and not r.aprovado
    ]
    erros = [str(r) for r in resultados if isinstance(r, Exception)]
    return {
        "total_recebidos": len(lotes),
        "aprovados_salvos": len(aprovados),
        "descartados_triagem": len(descartados),
        "motivos_descarte": descartados,
        "erros": erros,
        "itens": aprovados,
    }


@app.get("/api/v1/oportunidades")
async def get_oportunidades(
    semaforo: str | None = Query(default=None, pattern="^(VERDE|AMARELO|VERMELHO)$"),
    nivel: str | None = Query(default=None, pattern="^(DIAMANTE|PADRAO|ATENCAO|DESCARTADO)$"),
    limite: int = Query(default=50, ge=1, le=200),
):
    return listar_oportunidades(semaforo=semaforo, nivel_oportunidade=nivel, limite=limite)
