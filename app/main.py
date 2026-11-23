import asyncio
from fastapi import FastAPI, HTTPException, Query
from app.agents import agente_2_triagem, agente_3_auditor_edital, agente_4_e_5_curador, resolver_distancia_km
from app.config import settings
from app.db import listar_oportunidades, salvar_lote_avaliado
from app.models import CuradoriaFinal, LoteEntrada

app = FastAPI(title="Motor Multiagente de Leilões - Recuperados de Financiamento", version="1.1.0")


async def processar_lote(lote: LoteEntrada) -> CuradoriaFinal | None:
    distancia_km = await resolver_distancia_km(lote)
    if distancia_km > settings.raio_maximo_km:
        return None

    triagem = await agente_2_triagem(lote, distancia_km)
    if not triagem.aprovado_triagem or not triagem.categoria:
        return None

    auditoria = await agente_3_auditor_edital(lote, triagem.categoria)
    resultado = await agente_4_e_5_curador(lote, triagem, auditoria, distancia_km)
    salvar_lote_avaliado(lote, resultado)
    return resultado


@app.post("/api/v1/lotes/avaliar", response_model=CuradoriaFinal | None)
async def avaliar_lote_endpoint(lote: LoteEntrada):
    try:
        return await processar_lote(lote)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/v1/lotes/lote-batch")
async def avaliar_lotes_em_paralelo(lotes: list[LoteEntrada]):
    resultados = await asyncio.gather(*(processar_lote(l) for l in lotes), return_exceptions=True)
    aprovados = [r for r in resultados if isinstance(r, CuradoriaFinal)]
    erros = [str(r) for r in resultados if isinstance(r, Exception)]
    return {"total_recebidos": len(lotes), "aprovados_salvos": len(aprovados), "erros": erros, "itens": aprovados}


@app.get("/api/v1/oportunidades")
async def get_oportunidades(
    semaforo: str | None = Query(default=None, pattern="^(VERDE|AMARELO|VERMELHO)$"),
    nivel: str | None = Query(default=None, pattern="^(DIAMANTE|PADRAO|ATENCAO|DESCARTADO)$"),
    limite: int = Query(default=50, ge=1, le=200),
):
    return listar_oportunidades(semaforo=semaforo, nivel_oportunidade=nivel, limite=limite)