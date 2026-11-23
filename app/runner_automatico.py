import asyncio
import os
import httpx
from app.main import processar_lote
from app.models import CuradoriaFinal, LoteEntrada
from scraper_playwright import coletar_lotes_playwright


async def enviar_alerta_diamante(item: CuradoriaFinal) -> None:
    webhook_url = os.getenv("ALERT_WEBHOOK_URL")
    if not webhook_url:
        return
    if item.semaforo != "VERDE" and item.nivel_oportunidade != "DIAMANTE":
        return

    texto = (
        f"💎 OPORTUNIDADE {item.nivel_oportunidade} ({item.semaforo}) — Score {item.score_qualidade}/100\n"
        f"Ativo: {item.triagem.categoria} | Distância: {item.viabilidade.distancia_vinhedo_km} km de Vinhedo/SP\n"
        f"Teto Diamante (≥35% ROI): R$ {item.viabilidade.lance_maximo_diamante:,.2f}\n"
        f"Teto Base Recomendado: R$ {item.viabilidade.lance_maximo_recomendado:,.2f}\n"
        f"Margem Líquida Est.: {item.viabilidade.margem_liquida_projetada_pct:.1f}% | "
        f"Lucro Est.: R$ {item.viabilidade.lucro_liquido_estimado:,.2f}\n"
        f"{item.resumo_executivo}"
    )
    async with httpx.AsyncClient(timeout=15.0) as client:
        await client.post(webhook_url, json={"text": texto})


async def executar_pipeline() -> None:
    url_alvo = os.getenv("SCRAPER_TARGET_URL")
    if not url_alvo:
        print("SCRAPER_TARGET_URL não configurada nas Secrets.")
        return

    leiloeiro = os.getenv("SCRAPER_AUCTIONEER", "Portal Leilão")
    comitente = os.getenv("SCRAPER_CREDITOR", "Banco / Financeira")

    brutos = await coletar_lotes_playwright(url_alvo, leiloeiro, comitente)
    print(f"Lotes coletados na página: {len(brutos)}")

    aprovados = 0
    for item_dict in brutos:
        try:
            lote = LoteEntrada(**item_dict)
            resultado = await processar_lote(lote)
            if resultado is not None:
                aprovados += 1
                await enviar_alerta_diamante(resultado)
        except Exception as exc:
            print(f"Erro ao processar lote {item_dict.get('titulo_lote')}: {exc}")

    print(f"Processamento concluído. Lotes auditados e salvos no painel: {aprovados}")


if __name__ == "__main__":
    asyncio.run(executar_pipeline())