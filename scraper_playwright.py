import argparse
import asyncio
import logging
import os
from typing import Any

from playwright.async_api import async_playwright

from app.agents import resolver_distancia_km
from app.config import settings
from app.main import avaliar_lote_detalhado
from app.models import LoteEntrada

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("radar_scraper")

PALAVRAS_BLOQUEIO = (
    "sucata",
    "irrecuperavel",
    "irrecuperável",
    "motocicleta",
    "moto ",
    "execucao fiscal",
    "execução fiscal",
    "trabalhista",
    "penhora",
    "justica do trabalho",
    "justiça do trabalho",
    "4x2",
)

INDICADORES_FIDUCIARIA = (
    "alienacao fiduciaria",
    "alienação fiduciária",
    "recuperado de financiamento",
    "retomada",
    "banco ",
    "financeira",
    "bv ",
    "santander",
    "bradesco",
    "itau",
    "itaú",
    "john deere",
    "cnh ",
    "safra",
)


def _normalizar_texto(texto: str | None) -> str:
    if not texto:
        return ""
    return " ".join(texto.lower().split())


def pre_filtrar_lote_bruto(bruto: dict[str, Any]) -> tuple[bool, str]:
    """Filtro rápido sem LLM para poupar cota diária do Gemini."""
    titulo = _normalizar_texto(bruto.get("titulo_lote"))
    descricao = _normalizar_texto(bruto.get("descricao_lote"))
    comitente = _normalizar_texto(bruto.get("comitente_credor"))
    combinado = f"{titulo} {descricao} {comitente}"

    if not titulo or not bruto.get("cidade"):
        return False, "Dados mínimos ausentes (título ou cidade)."

    for termo in PALAVRAS_BLOQUEIO:
        if termo in combinado:
            return False, f"Bloqueado no pré-filtro por termo: '{termo}'."

    tem_indicio_fiduciaria = any(ind in combinado for ind in INDICADORES_FIDUCIARIA)
    if not tem_indicio_fiduciaria:
        return False, "Sem indício claro de alienação fiduciária / retomada financeira."

    return True, "OK"


async def coletar_lotes_exemplo(max_itens: int = 10) -> list[dict[str, Any]]:
    """
    Coleta lotes via Playwright.
    Adapte URLs e seletores para os portais-alvo do seu repositório.
    """
    urls_alvo = [
        u.strip()
        for u in os.getenv("SCRAPER_TARGET_URLS", "").split(",")
        if u.strip()
    ]
    if not urls_alvo:
        logger.info("Nenhuma URL em SCRAPER_TARGET_URLS; nenhum lote novo raspado nesta rodada.")
        return []

    coletados: list[dict[str, Any]] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
        )
        page = await context.new_page()
        page.set_default_timeout(25000)

        for url in urls_alvo:
            if len(coletados) >= max_itens:
                break
            try:
                logger.info("Acessando portal: %s", url)
                await page.goto(url, wait_until="domcontentloaded")
                await page.wait_for_timeout(2500)

                # Exemplo genérico de cards; ajuste conforme o HTML real do portal
                cards = page.locator("article, .lot-card, .card-lote, [data-testid='lot-card']")
                total_cards = await cards.count()
                logger.info("Encontrados %d elementos candidatos em %s", total_cards, url)

                for idx in range(min(total_cards, max_itens - len(coletados))):
                    card = cards.nth(idx)
                    texto_card = (await card.inner_text() or "").strip()
                    linhas = [ln.strip() for ln in texto_card.splitlines() if ln.strip()]
                    if not linhas:
                        continue

                    titulo = linhas[0][:180]
                    link_el = card.locator("a").first
                    href = await link_el.get_attribute("href") if await link_el.count() else url
                    if href and href.startswith("/"):
                        base = url.split("/")[0] + "//" + url.split("/")[2]
                        href = f"{base}{href}"

                    coletados.append(
                        {
                            "leiloeiro": url.split("/")[2] if "://" in url else "Portal Leilão",
                            "comitente_credor": "Banco / Financeira (verificar edital)",
                            "numero_lote": str(idx + 1),
                            "url_lote": href or url,
                            "url_edital_pdf": None,
                            "titulo_lote": titulo,
                            "descricao_lote": texto_card[:1500],
                            "cidade": "Campinas",
                            "uf": "SP",
                            "lance_atual": 100000.0,
                            "valor_mercado_referencia": 160000.0,
                        }
                    )
            except Exception as exc:
                logger.warning("Falha ao raspar %s: %s", url, exc)

        await browser.close()

    return coletados


async def executar_pipeline_coleta(max_lotes: int = 3, delay_entre_lotes_s: float = 2.5) -> None:
    brutos = await coletar_lotes_exemplo(max_itens=max_lotes * 4)
    if not brutos:
        logger.info("Pipeline encerrado sem novos lotes para avaliar.")
        return

    avaliados = 0
    aprovados = 0

    for bruto in brutos:
        if avaliados >= max_lotes:
            logger.info("Limite de %d lotes avaliados por execução atingido.", max_lotes)
            break

        ok_filtro, motivo_filtro = pre_filtrar_lote_bruto(bruto)
        if not ok_filtro:
            logger.info("Descartado no pré-filtro (%s): %s", motivo_filtro, bruto.get("titulo_lote"))
            continue

        try:
            lote_obj = LoteEntrada(**bruto)
        except Exception as exc:
            logger.warning("Lote inválido para schema LoteEntrada: %s", exc)
            continue

        dist_km = await resolver_distancia_km(lote_obj)
        if dist_km > settings.raio_maximo_km:
            logger.info(
                "Fora do raio de %.0f km (%.1f km em %s/%s): %s",
                settings.raio_maximo_km,
                dist_km,
                lote_obj.cidade,
                lote_obj.uf,
                lote_obj.titulo_lote,
            )
            continue

        lote_obj.distancia_vinhedo_km = dist_km
        avaliados += 1
        logger.info(
            "[%d/%d] Avaliando lote: %s (%s/%s - %.1f km)",
            avaliados,
            max_lotes,
            lote_obj.titulo_lote,
            lote_obj.cidade,
            lote_obj.uf,
            dist_km,
        )

        try:
            resultado = await avaliar_lote_detalhado(lote_obj)
            if resultado and resultado.aprovado:
                aprovados += 1
                logger.info("APROVADO e salvo: %s", lote_obj.titulo_lote)
            else:
                logger.info(
                    "Rejeitado na triagem/auditoria: %s",
                    getattr(resultado, "motivo_rejeicao", "sem motivo"),
                )
        except Exception as exc:
            logger.error("Erro ao avaliar lote '%s': %s", lote_obj.titulo_lote, exc)
            # Em caso de 503 ou 429, interrompe o lote atual para não queimar o restante da cota
            msg = str(exc)
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg or "503" in msg or "UNAVAILABLE" in msg:
                logger.warning("Instabilidade ou cota atingida; encerrando rodada com segurança.")
                break

        await asyncio.sleep(delay_entre_lotes_s)

    logger.info("Resumo da execução: %d avaliados via IA, %d aprovados/salvos.", avaliados, aprovados)


def main() -> None:
    parser = argparse.ArgumentParser(description="Coletor automático do Radar Leilões IA")
    parser.add_argument(
        "--max-lotes",
        type=int,
        default=int(os.getenv("MAX_LOTES_POR_EXECUCAO", "3")),
        help="Máximo de lotes enviados para a IA por execução",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=2.5,
        help="Pausa em segundos entre lotes para reduzir picos de API",
    )
    args = parser.parse_args()
    asyncio.run(executar_pipeline_coleta(max_lotes=args.max_lotes, delay_entre_lotes_s=args.delay))


if __name__ == "__main__":
    main()
