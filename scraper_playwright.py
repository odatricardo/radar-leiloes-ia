import re
from urllib.parse import urljoin
from playwright.async_api import async_playwright


def _parse_valor_brl(texto: str) -> float:
    numeros = re.findall(r"[\d\.]+,\d{2}", texto or "")
    if not numeros:
        return 0.0
    limpo = numeros[0].replace(".", "").replace(",", ".")
    try:
        return float(limpo)
    except ValueError:
        return 0.0


async def coletar_lotes_playwright(
    url_alvo: str,
    leiloeiro: str,
    comitente: str,
    limite_lotes: int = 25,
) -> list[dict]:
    """
    Coleta lotes genéricos de páginas de leilão via Playwright (headless Chromium).
    Ajuste os seletores conforme a estrutura específica de cada portal.
    """
    lotes_coletados: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        page = await context.new_page()
        await page.goto(url_alvo, wait_until="networkidle", timeout=45000)

        # Tenta localizar cartões ou linhas de lote em portais comuns
        cards = page.locator("article, .lot-card, .lote-item, .card-lote, tr.lote")
        total = await cards.count()

        for idx in range(min(total, limite_lotes)):
            card = cards.nth(idx)
            texto_bloco = (await card.inner_text()).strip()
            if len(texto_bloco) < 20:
                continue

            link_el = card.locator("a").first
            href = await link_el.get_attribute("href") if await link_el.count() > 0 else ""
            url_lote = urljoin(url_alvo, href) if href else url_alvo

            linhas = [ln.strip() for ln in texto_bloco.splitlines() if ln.strip()]
            titulo = linhas[0][:140] if linhas else f"Lote {idx + 1}"
            lance_atual = _parse_valor_brl(texto_bloco) or 50000.0
            valor_ref = round(lance_atual * 1.45, 2)

            lotes_coletados.append(
                {
                    "leiloeiro": leiloeiro,
                    "comitente_credor": comitente,
                    "numero_lote": str(idx + 1),
                    "url_lote": url_lote,
                    "titulo_lote": titulo,
                    "descricao_lote": texto_bloco[:2000],
                    "cidade": "Campinas",
                    "uf": "SP",
                    "lance_atual": lance_atual,
                    "valor_mercado_referencia": valor_ref,
                }
            )

        await browser.close()

    return lotes_coletados