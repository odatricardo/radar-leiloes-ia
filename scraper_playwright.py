import argparse
import asyncio
import logging
import os
import re
from typing import Any
from urllib.parse import urljoin, urlparse

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

PADROES_URL_IGNORADA = (
    "/login",
    "/cadastro",
    "/minha-conta",
    "/contato",
    "/institucional",
    "/quem-somos",
    "/termos",
    "/privacidade",
    "/ajuda",
    "/faq",
    "javascript:",
    "mailto:",
    "tel:",
)

PADROES_URL_LOTE = (
    "lote",
    "item",
    "bem",
    "veiculo",
    "imovel",
    "leilao",
    "oferta",
    "detalhe",
    "asset",
    "lot",
)


def _limpar_env(nome: str, padrao: str = "") -> str:
    valor = os.getenv(nome, padrao)
    if not isinstance(valor, str):
        return padrao
    return valor.strip().strip('"').strip("'")


def _sanitizar_variaveis_ambiente() -> None:
    """Remove espaços ou quebras de linha acidentais em variáveis sensíveis."""
    chaves_env = (
        "GEMINI_API_KEY",
        "GEMINI_MODEL_FAST",
        "GEMINI_MODEL_AUDIT",
        "GEMINI_FALLBACK_MODELS",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "OPENAI_BASE_URL",
        "DATABASE_URL",
        "GOOGLE_MAPS_API_KEY",
    )
    for chave in chaves_env:
        atual = os.environ.get(chave)
        if isinstance(atual, str) and atual != atual.strip():
            os.environ[chave] = atual.strip()

    for attr in ("gemini_api_key", "openai_api_key", "openai_base_url", "openai_model"):
        val = getattr(settings, attr, None)
        if isinstance(val, str):
            setattr(settings, attr, val.strip())


def _normalizar_texto(texto: str | None) -> str:
    if not texto:
        return ""
    return " ".join(texto.lower().split())


def _extrair_valores_brl(texto: str) -> list[float]:
    matches = re.findall(r"R\$\s*([\d\.]+,\d{2})", texto)
    valores: list[float] = []
    for bruto in matches:
        try:
            numero = float(bruto.replace(".", "").replace(",", "."))
            if numero >= 1000.0:
                valores.append(numero)
        except ValueError:
            continue
    return valores


def _escolher_titulo_card(linhas: list[str], href: str) -> str:
    candidatas = [
        ln
        for ln in linhas
        if len(ln) >= 6
        and not ln.lower().startswith(("r$", "lance", "ver lote", "detalhes", "saiba mais"))
        and not re.fullmatch(r"\d+\s*lotes?", ln.strip().lower())
    ]
    if candidatas:
        # Prioriza linha com nome de veículo, máquina, imóvel ou marca conhecida
        for ln in candidatas:
            if any(
                k in ln.lower()
                for k in (
                    "hilux",
                    "ranger",
                    "amarok",
                    "s10",
                    "sw4",
                    "caminhao",
                    "caminhão",
                    "scania",
                    "volvo",
                    "mercedes",
                    "iveco",
                    "trator",
                    "colheitadeira",
                    "apartamento",
                    "casa",
                    "terreno",
                    "galpao",
                    "galpão",
                    "imovel",
                    "imóvel",
                    "veiculo",
                    "veículo",
                )
            ):
                return ln[:180]
        return candidatas[0][:180]

    if linhas:
        return linhas[0][:180]

    parsed = urlparse(href)
    slug = parsed.path.rstrip("/").split("/")[-1].replace("-", " ").replace("_", " ").strip()
    return (slug[:180] if slug else "Lote em Leilão")


def _url_parece_lote(url_candidata: str, url_base: str) -> bool:
    if not url_candidata:
        return False
    baixa = url_candidata.lower().strip()
    if baixa.startswith(PADROES_URL_IGNORADA) or baixa == "#" or baixa.endswith("/#"):
        return False
    if baixa.rstrip("/") == url_base.rstrip("/").lower():
        return False
    parsed = urlparse(url_candidata)
    caminho = (parsed.path or "").lower()
    query = (parsed.query or "").lower()
    if any(p in caminho or p in query for p in PADROES_URL_LOTE):
        return True
    # Aceita caminhos profundos com ID numérico no final (ex.: /leilao/12345 ou /123456)
    if re.search(r"/\d{3,}/?$", caminho):
        return True
    return False


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
    Coleta lotes via Playwright com fallback de seletores de cards e links de lotes.
    """
    _sanitizar_variaveis_ambiente()
    urls_alvo = [
        u.strip()
        for u in _limpar_env("SCRAPER_TARGET_URLS").split(",")
        if u.strip().startswith(("http://", "https://"))
    ]
    if not urls_alvo:
        logger.info("Nenhuma URL em SCRAPER_TARGET_URLS; nenhum lote novo raspado nesta rodada.")
        return []

    coletados: list[dict[str, Any]] = []
    urls_vistas: set[str] = set()

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
                await page.wait_for_timeout(3000)

                seletor_cards = (
                    "article, .lot-card, .card-lote, [data-testid='lot-card'], "
                    ".card-auction, .auction-card, .card-item, [class*='lot-card'], [class*='card-lote']"
                )
                cards = page.locator(seletor_cards)
                total_cards = await cards.count()

                candidatos_extraidos = 0
                for idx in range(min(total_cards, max_itens * 2)):
                    if len(coletados) >= max_itens:
                        break
                    card = cards.nth(idx)
                    texto_card = (await card.inner_text() or "").strip()
                    linhas = [ln.strip() for ln in texto_card.splitlines() if ln.strip()]
                    if not linhas or len(texto_card) < 12:
                        continue

                    href_raw = ""
                    links = card.locator("a[href]")
                    total_links = await links.count()
                    for l_idx in range(total_links):
                        cand = await links.nth(l_idx).get_attribute("href")
                        if cand:
                            abs_cand = urljoin(url, cand.strip())
                            if _url_parece_lote(abs_cand, url):
                                href_raw = abs_cand
                                break
                    if not href_raw and total_links > 0:
                        cand0 = await links.first.get_attribute("href")
                        if cand0:
                            href_raw = urljoin(url, cand0.strip())

                    if not href_raw or href_raw in urls_vistas:
                        continue
                    urls_vistas.add(href_raw)

                    titulo = _escolher_titulo_card(linhas, href_raw)
                    valores = _extrair_valores_brl(texto_card)
                    lance_estimado = valores[0] if valores else 100000.0
                    mercado_estimado = (
                        max(valores) * 1.45 if valores and max(valores) > 0 else 160000.0
                    )

                    coletados.append(
                        {
                            "leiloeiro": urlparse(url).netloc or "Portal Leilão",
                            "comitente_credor": "Banco / Financeira (verificar edital)",
                            "numero_lote": str(idx + 1),
                            "url_lote": href_raw,
                            "url_edital_pdf": None,
                            "titulo_lote": titulo,
                            "descricao_lote": texto_card[:1500],
                            "cidade": "Campinas",
                            "uf": "SP",
                            "lance_atual": lance_estimado,
                            "valor_mercado_referencia": mercado_estimado,
                        }
                    )
                    candidatos_extraidos += 1

                # Fallback por links quando o portal não usa cards estruturados padrão
                if candidatos_extraidos == 0:
                    anchors = page.locator("a[href]")
                    total_anchors = await anchors.count()
                    for a_idx in range(min(total_anchors, 250)):
                        if len(coletados) >= max_itens:
                            break
                        anc = anchors.nth(a_idx)
                        href_raw = await anc.get_attribute("href")
                        if not href_raw:
                            continue
                        abs_href = urljoin(url, href_raw.strip())
                        if not _url_parece_lote(abs_href, url) or abs_href in urls_vistas:
                            continue

                        texto_anc = " ".join(((await anc.inner_text()) or "").split()).strip()
                        if len(texto_anc) < 8:
                            continue

                        urls_vistas.add(abs_href)
                        valores = _extrair_valores_brl(texto_anc)
                        lance_estimado = valores[0] if valores else 100000.0
                        mercado_estimado = (
                            max(valores) * 1.45 if valores and max(valores) > 0 else 160000.0
                        )

                        coletados.append(
                            {
                                "leiloeiro": urlparse(url).netloc or "Portal Leilão",
                                "comitente_credor": "Banco / Financeira (verificar edital)",
                                "numero_lote": str(candidatos_extraidos + 1),
                                "url_lote": abs_href,
                                "url_edital_pdf": None,
                                "titulo_lote": texto_anc[:180],
                                "descricao_lote": texto_anc[:1500],
                                "cidade": "Campinas",
                                "uf": "SP",
                                "lance_atual": lance_estimado,
                                "valor_mercado_referencia": mercado_estimado,
                            }
                        )
                        candidatos_extraidos += 1

                logger.info(
                    "Encontrados %d elementos candidatos em %s", candidatos_extraidos, url
                )
            except Exception as exc:
                logger.warning("Falha ao raspar %s: %s", url, exc)

        await browser.close()

    return coletados


async def executar_pipeline_coleta(max_lotes: int = 3, delay_entre_lotes_s: float = 2.5) -> None:
    _sanitizar_variaveis_ambiente()
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
            "[%d/%d] Avaliando lote: %s (%s/%s - %.1f km) -> %s",
            avaliados,
            max_lotes,
            lote_obj.titulo_lote,
            lote_obj.cidade,
            lote_obj.uf,
            dist_km,
            lote_obj.url_lote,
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
            msg = str(exc)
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg or "503" in msg or "UNAVAILABLE" in msg:
                logger.warning("Instabilidade ou cota atingida; encerrando rodada com segurança.")
                break

        await asyncio.sleep(delay_entre_lotes_s)

    logger.info("Resumo da execução: %d avaliados via IA, %d aprovados/salvos.", avaliados, aprovados)


def main() -> None:
    _sanitizar_variaveis_ambiente()
    parser = argparse.ArgumentParser(description="Coletor automático do Radar Leilões IA")
    parser.add_argument(
        "--max-lotes",
        type=int,
        default=int(_limpar_env("MAX_LOTES_POR_EXECUCAO", "3") or "3"),
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
