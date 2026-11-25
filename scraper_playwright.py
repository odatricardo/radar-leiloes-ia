import argparse
import asyncio
import logging
import math
import os
import re
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

from playwright.async_api import Page, async_playwright

from app.agents import resolver_distancia_km
from app.config import settings
from app.main import avaliar_lote_detalhado
from app.models import LoteEntrada

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("radar_scraper")

UFS_BRASIL = {
    "AC",
    "AL",
    "AP",
    "AM",
    "BA",
    "CE",
    "DF",
    "ES",
    "GO",
    "MA",
    "MT",
    "MS",
    "MG",
    "PA",
    "PB",
    "PR",
    "PE",
    "PI",
    "RJ",
    "RN",
    "RS",
    "RO",
    "RR",
    "SC",
    "SP",
    "SE",
    "TO",
}

ESTADOS_POR_UF: dict[str, str] = {
    "AC": "Acre",
    "AL": "Alagoas",
    "AP": "Amapá",
    "AM": "Amazonas",
    "BA": "Bahia",
    "CE": "Ceará",
    "DF": "Distrito Federal",
    "ES": "Espírito Santo",
    "GO": "Goiás",
    "MA": "Maranhão",
    "MT": "Mato Grosso",
    "MS": "Mato Grosso do Sul",
    "MG": "Minas Gerais",
    "PA": "Pará",
    "PB": "Paraíba",
    "PR": "Paraná",
    "PE": "Pernambuco",
    "PI": "Piauí",
    "RJ": "Rio de Janeiro",
    "RN": "Rio Grande do Norte",
    "RS": "Rio Grande do Sul",
    "RO": "Rondônia",
    "RR": "Roraima",
    "SC": "Santa Catarina",
    "SP": "São Paulo",
    "SE": "Sergipe",
    "TO": "Tocantins",
}

TERMOS_ESCOPO_ATIVOS = (
    "caminhonete",
    "picape",
    "pickup",
    "4x4",
    "hilux",
    "ranger",
    "amarok",
    "s10",
    "sw4",
    "l200",
    "triton",
    "frontier",
    "toro",
    "caminhao",
    "caminhão",
    "cavalo mecanico",
    "cavalo mecânico",
    "carreta",
    "semirreboque",
    "scania",
    "volvo",
    "mercedes",
    "iveco",
    "daf",
    "man ",
    "vw ",
    "ford cargo",
    "atego",
    "axor",
    "actros",
    "constellation",
    "trator",
    "colhedora",
    "colheitadeira",
    "pulverizador",
    "plantadeira",
    "retroescavadeira",
    "escavadeira",
    "pa carregadeira",
    "pá carregadeira",
    "john deere",
    "massey ferguson",
    "valtra",
    "new holland",
    "case ih",
    "maquina agricola",
    "máquina agrícola",
    "implemento",
    "reboque",
    "veiculo",
    "veículo",
    "imovel",
    "imóvel",
    "apartamento",
    "apto",
    "casa",
    "terreno",
    "galpao",
    "galpão",
)

BLOQUEIO_DURO = (
    "sucata",
    "irrecuperavel",
    "irrecuperável",
    "motocicleta",
    "moto ",
    "biz ",
    "execucao fiscal",
    "execução fiscal",
    "trabalhista",
    "penhora",
    "justica do trabalho",
    "justiça do trabalho",
    "judicial online",
    "entrar ou abrir conta",
    "abrir conta",
    "ver todos os eventos",
    "ver todos os lotes",
    "minha conta",
    "habilite",
    "cadastre",
    "compartilhe",
    "dúvidas e contato",
    "duvidas e contato",
    "termos e condições",
    "termos e condicoes",
    "consultar documentos",
    "conheça nosso blog",
    "conheca nosso blog",
    "trabalhe conosco",
    "sobre a empresa",
    "newsletter",
)

RUIDO_SECUNDARIO = (
    "câmera",
    "camera",
    "alarme",
    "nvr",
    "gabinete",
    "mesa",
    "cadeira",
    "aspirador",
    "mixer",
    "frigobar",
    "freezer",
    "expositora",
    "ponte rolante",
    "empilhadeira",
    "torno",
    "solda",
    "gerador",
    "motogerador",
    "plataforma tesoura",
    "plataforma elevatória",
    "plataforma elevatoria",
    "porta pallets",
    "prateleiras",
    "túnel de encolhimento",
    "tunel de encolhimento",
    "poliamida",
    "instrumentos de percussão",
    "instrumentos de percussao",
    "cama elástica",
    "cama elastica",
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
    "consórcio",
    "consorcio",
)

PADROES_URL_IGNORADA = (
    "/login",
    "/signin",
    "/authorization",
    "/cadastro",
    "/minha-conta",
    "/contato",
    "/fale-conosco",
    "/institucional",
    "/quem-somos",
    "/regulamento",
    "/termos",
    "/privacidade",
    "/ajuda",
    "/faq",
    "/categorias",
    "/categoria",
    "/todos-eventos",
    "accounts.superbid.net",
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
    "evento",
    "oferta",
    "detalhe",
    "asset",
    "lot",
    "pos-leilao",
)

URLS_ALTERNATIVAS_PORTAL: dict[str, list[str]] = {
    "superbid.net": [
        "https://exchange.superbid.net/pos-leilao/todos",
        "https://www.superbid.net/leiloes",
    ],
    "sold.com.br": [
        "https://www.sold.com.br/lotes",
    ],
    "sodresantoro.com.br": [
        "https://www.sodresantoro.com.br/lotes",
        "https://www.sodresantoro.com.br/",
    ],
    "vipleiloes.com.br": [
        "https://www.vipleiloes.com.br/lotes",
        "https://www.vipleiloes.com.br/",
    ],
    "portalzuk.com.br": [
        "https://www.portalzuk.com.br/leilao-de-veiculos",
        "https://www.portalzuk.com.br/leilao-de-imoveis",
        "https://www.portalzuk.com.br/",
    ],
    "megaleiloes.com.br": [
        "https://www.megaleiloes.com.br/veiculos/caminhoes",
        "https://www.megaleiloes.com.br/veiculos",
        "https://www.megaleiloes.com.br/imoveis",
        "https://www.megaleiloes.com.br/",
    ],
}


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


def _tem_termo_escopo(texto: str) -> bool:
    baixa = _normalizar_texto(texto)
    return any(t in baixa for t in TERMOS_ESCOPO_ATIVOS)


def _formatar_nome_cidade(bruto: str) -> str:
    """Normaliza 'BARRETOS' ou 'são paulo' para 'Barretos' ou 'São Paulo'."""
    limpo = " ".join(bruto.strip(" ,;-–/([)").split())
    minusculas_conectivos = {"de", "da", "do", "das", "dos", "e"}
    partes = limpo.lower().split(" ")
    formatadas: list[str] = []
    for idx, p in enumerate(partes):
        if idx > 0 and p in minusculas_conectivos:
            formatadas.append(p)
        else:
            formatadas.append(p.capitalize())
    return " ".join(formatadas)


def _parse_moeda_brl(bruto: str) -> float | None:
    limpo = bruto.replace("R$", "").replace("\xa0", " ").strip()
    if "," in limpo:
        limpo = limpo.replace(".", "").replace(",", ".")
    else:
        if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", limpo):
            limpo = limpo.replace(".", "")
    try:
        val = float(limpo)
        return val if val >= 500.0 else None
    except ValueError:
        return None


def _extrair_valores_brl(texto: str) -> list[float]:
    matches = re.findall(r"R\$\s*[\d\.]+(?:,\d{2})?", texto)
    valores: list[float] = []
    for bruto in matches:
        val = _parse_moeda_brl(bruto)
        if val is not None:
            valores.append(val)
    return valores


def _multiplicador_mercado_por_texto(texto: str) -> float:
    baixa = (texto or "").lower()
    if any(
        k in baixa
        for k in (
            "apto",
            "apartamento",
            "casa",
            "terreno",
            "galpão",
            "galpao",
            "imóvel",
            "imovel",
            "matrícula",
            "matricula",
        )
    ):
        return 1.65
    return 1.55


def _garantir_valores_coerentes(
    lance: float | None, avaliacao: float | None, contexto_texto: str = ""
) -> tuple[float, float]:
    """Garante que lance e valor de mercado fiquem positivos e coerentes (avaliação > lance)."""
    mult = _multiplicador_mercado_por_texto(contexto_texto)

    lance_ok = float(lance) if (lance is not None and lance >= 500.0) else None
    aval_ok = float(avaliacao) if (avaliacao is not None and avaliacao >= 500.0) else None

    if lance_ok is None and aval_ok is None:
        lance_ok = 100000.0
        aval_ok = round(lance_ok * mult, 2)
    elif lance_ok is not None and (aval_ok is None or aval_ok <= lance_ok):
        aval_ok = round(lance_ok * mult, 2)
    elif aval_ok is not None and lance_ok is None:
        lance_ok = round(aval_ok * 0.55, 2)

    # Trava distorções extremas caso um valor de outra seção/praça fuja muito do lance
    if lance_ok > 0 and aval_ok > lance_ok * 3.0:
        aval_ok = round(lance_ok * mult, 2)

    return round(lance_ok, 2), round(aval_ok, 2)


def _extrair_valores_com_rotulo(texto: str) -> tuple[float, float]:
    """
    Retorna (lance_atual_ou_minimo, valor_avaliacao_ou_mercado) sempre com valores coerentes.
    Suporta rótulos como 'Lance mínimo', 'Último lance', 'Avaliação' e 'Inicial: R$ ...' (1ª/2ª praça),
    priorizando o menor valor de lance/praça quando múltiplos valores aparecem sem rótulo explícito.
    """
    lance: float | None = None
    avaliacao: float | None = None

    # Se houver "Último lance" ou "Lance mínimo" explícito, usa o menor encontrado entre os rótulos
    lances_explicitos: list[float] = []
    padroes_lance = (
        r"(?:[uú]ltimo\s*lance|lance\s*(?:m[ií]nimo(?:\s*atual)?|atual))[^\dR$]{0,30}(R\$\s*[\d\.]+(?:,\d{2})?)",
        r"(?:valor\s*(?:do\s*lance|m[ií]nimo))[^\dR$]{0,30}(R\$\s*[\d\.]+(?:,\d{2})?)",
    )
    for pat in padroes_lance:
        for m in re.finditer(pat, texto, flags=re.IGNORECASE):
            v = _parse_moeda_brl(m.group(1))
            if v is not None:
                lances_explicitos.append(v)

    padroes_avaliacao = (
        r"(?:avalia[cç][aã]o|valor\s*de\s*mercado|mercado|fipe)[^\dR$]{0,30}(R\$\s*[\d\.]+(?:,\d{2})?)",
    )
    for pat in padroes_avaliacao:
        m = re.search(pat, texto, flags=re.IGNORECASE)
        if m:
            avaliacao = _parse_moeda_brl(m.group(1))
            if avaliacao is not None:
                break

    iniciais_vals: list[float] = []
    for m_ini in re.finditer(
        r"\binicial\s*[:\-–]?\s*(R\$\s*[\d\.]+(?:,\d{2})?)",
        texto,
        flags=re.IGNORECASE,
    ):
        v = _parse_moeda_brl(m_ini.group(1))
        if v is not None:
            iniciais_vals.append(v)

    todos = _extrair_valores_brl(texto)

    if lances_explicitos:
        lance = min(lances_explicitos)
    elif iniciais_vals:
        lance = min(iniciais_vals)
        maior_ini = max(iniciais_vals)
        if avaliacao is None and maior_ini > lance:
            avaliacao = maior_ini
    elif todos:
        # Quando há múltiplos valores sem rótulo claro (ex.: 1ª e 2ª praça), prioriza o menor valor
        # como lance atual para não confundir lance de 1ª praça com lance atual.
        lance = min(todos)

    if avaliacao is None:
        candidatos_aval = [v for v in (iniciais_vals + todos) if lance is not None and v > lance]
        if candidatos_aval:
            avaliacao = max(candidatos_aval)

    return _garantir_valores_coerentes(lance, avaliacao, texto)


def _cidade_parece_ruido(cidade_bruta: str) -> bool:
    baixa = " ".join(cidade_bruta.lower().strip().split())
    if len(baixa) < 3 or len(baixa.split()) > 5:
        return True
    # Evita capturar áreas/metragens ou propriedades como nome de cidade (ex.: "72 ha - sítio...")
    if re.search(r"\b\d+(?:[.,]\d+)?\s*(?:ha|hectares?|m2|m²|km2|km²|alqueires?)\b", baixa):
        return True
    if any(
        ruido in baixa
        for ruido in (
            "http",
            "www",
            "lote",
            "edital",
            "processo",
            "banco",
            "leilão",
            "leilao",
            "lance",
            "avaliação",
            "industrial",
            "máquinas",
            "maquinas",
            "equipamentos",
            "eventos",
            "localização",
            "localizacao",
            "retirada",
            "pátio",
            "patio",
            "comarca",
            "estado",
            "sítio",
            "sitio",
            "fazenda",
            "chácara",
            "chacara",
            "gleba",
            "matrícula",
            "matricula",
            "habilite",
            "cadastre",
            "compartilhe",
            "dúvidas",
            "duvidas",
            "contato",
            "termos",
            "condições",
            "condicoes",
            "documentos",
            "newsletter",
            "institucional",
            "blog",
            "sobre a empresa",
            "trabalhe conosco",
            "veículos",
            "veiculos",
            "caminhões",
            "caminhoes",
            "aeronaves",
            "colhedoras",
            "tratores",
            "implementos",
        )
    ):
        return True
    return False


def _extrair_cidade_uf(
    texto: str, padrao_cidade: str = "Não informada", padrao_uf: str = "XX"
) -> tuple[str, str]:
    """
    Extrai (cidade, uf) priorizando o último segmento de 'Bairro/Local - Cidade - UF'
    (ex.: 'Aparecidinha - Sorocaba - SP' -> 'Sorocaba', 'SP') e evitando capturar
    metragens/propriedades como '72 ha - Sítio das Primaveras'.
    """
    janelas: list[str] = []
    for kw in (
        "localização",
        "localizacao",
        "local",
        "retirada",
        "pátio",
        "patio",
        "avaliação",
        "lance mínimo",
        "depositário",
        "comarca",
        "cidade",
        "município",
        "municipio",
        "endereço",
        "estado",
        "uf",
    ):
        for match in re.finditer(r"\b" + re.escape(kw) + r"\b", texto, flags=re.IGNORECASE):
            inicio = max(0, match.start() - 60)
            fim = min(len(texto), match.end() + 220)
            janelas.append(texto[inicio:fim])
    janelas.append(texto[:6000])

    # Captura sequências como 'Aparecidinha - Sorocaba - SP' ou 'Brotas - SP'
    regex_cadeia = re.compile(
        r"(?:^|[\s:;(\[|])([A-Za-zÀ-ÿ0-9\s'\-\.]{2,90}?)\s*(?:/|-|–)\s*([A-Za-z]{2})(?=$|[\s.,;)\]|])",
        flags=re.MULTILINE,
    )

    for trecho in janelas:
        for m in regex_cadeia.finditer(trecho):
            cadeia_bruta = m.group(1).strip(" ,;-–/([|)")
            uf_cand = m.group(2).strip().upper()
            if uf_cand not in UFS_BRASIL:
                continue

            # Divide cadeias encadeadas (ex.: "Área Industrial - Aparecidinha - Sorocaba")
            # e testa do segmento mais próximo da UF para trás.
            partes_cadeia = [p.strip(" ,;-–/([|)") for p in re.split(r"\s*(?:/|-|–)\s*", cadeia_bruta) if p.strip()]
            for parte in reversed(partes_cadeia):
                if _cidade_parece_ruido(parte):
                    continue
                cidade_fmt = _formatar_nome_cidade(parte)
                if len(cidade_fmt) >= 3:
                    return cidade_fmt[:80], uf_cand

    # Fallback para 'Cidade (SP)'
    regex_paren = re.compile(
        r"(?:^|[\s:;(\[|])([A-Za-zÀ-ÿ\s'\-\.]{2,45}?)\s*\(\s*([A-Za-z]{2})\s*\)",
        flags=re.MULTILINE,
    )
    for trecho in janelas:
        for m in regex_paren.finditer(trecho):
            cidade_bruta = m.group(1).strip(" ,;-–/([|)")
            uf_cand = m.group(2).strip().upper()
            if uf_cand not in UFS_BRASIL or _cidade_parece_ruido(cidade_bruta):
                continue
            cidade_fmt = _formatar_nome_cidade(cidade_bruta)
            if len(cidade_fmt) >= 3:
                return cidade_fmt[:80], uf_cand

    uf_detectada = padrao_uf if padrao_uf in UFS_BRASIL else None
    if not uf_detectada:
        m_uf = re.search(
            r"(?:\buf\b|\bestado\b)\s*[:\-–]?\s*([A-Za-z]{2})\b", texto, flags=re.IGNORECASE
        )
        if m_uf and m_uf.group(1).upper() in UFS_BRASIL:
            uf_detectada = m_uf.group(1).upper()

    if uf_detectada:
        m_cid = re.search(
            r"(?:cidade|munic[ií]pio|local(?:iza[cç][aã]o)?|retirada)\s*[:\-–]\s*([A-Za-zÀ-ÿ\s'\-\.]{3,45})",
            texto,
            flags=re.IGNORECASE,
        )
        if m_cid:
            cid_cand = re.split(r"\s*(?:/|-|–|\(|\n|,|;)", m_cid.group(1))[0].strip()
            if not _cidade_parece_ruido(cid_cand):
                return _formatar_nome_cidade(cid_cand)[:80], uf_detectada

    return padrao_cidade, uf_detectada or padrao_uf


def _escolher_titulo_card(linhas: list[str], href: str) -> str:
    candidatas = [
        ln
        for ln in linhas
        if len(ln) >= 6
        and not ln.lower().startswith(
            ("r$", "lance", "ver lote", "detalhes", "saiba mais", "entrar", "ver todos")
        )
        and not re.fullmatch(r"\d+\s*lotes?", ln.strip().lower())
    ]
    if candidatas:
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
                    "colhedora",
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
    return slug[:180] if slug else "Lote em Leilão"


def _titulo_parece_generico(titulo: str) -> bool:
    t = (titulo or "").strip().lower()
    if not t or len(t) < 6:
        return True
    if re.fullmatch(r"\d+\s*lotes?", t):
        return True
    if t in {
        "lote em leilão",
        "lote em leilao",
        "ver lote",
        "detalhes",
        "saiba mais",
        "entrar ou abrir conta",
        "ver todos os eventos",
    }:
        return True
    return False


def _limpar_url_lote(url: str) -> str:
    """Remove parâmetros de rastreamento/paginação (?...) para evitar lotes duplicados."""
    parsed = urlparse(url.strip())
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))


def _url_parece_lote(url_candidata: str, url_base: str) -> bool:
    if not url_candidata:
        return False
    baixa = url_candidata.lower().strip()
    if any(p in baixa for p in PADROES_URL_IGNORADA) or baixa == "#" or baixa.endswith("/#"):
        return False
    if baixa.rstrip("/") == url_base.rstrip("/").lower():
        return False
    parsed = urlparse(url_candidata)
    caminho = (parsed.path or "").lower()
    query = (parsed.query or "").lower()
    if any(p in caminho or p in query for p in PADROES_URL_LOTE):
        return True
    if re.search(r"/\d{3,}/?$", caminho):
        return True
    return False


def _expandir_urls_com_fallback(urls_base: list[str]) -> list[tuple[str, str | None]]:
    """
    Retorna pares (url_para_acessar, categoria_hint) garantindo que todas as rotas
    de categoria do portal (veículos, caminhões, imóveis) sejam percorridas, e não
    puladas após a primeira página retornar candidatos.
    """
    plano: list[tuple[str, str | None]] = []
    vistas: set[str] = set()

    for url in urls_base:
        u_limpa = url.strip()
        if not u_limpa:
            continue
        if u_limpa not in vistas:
            vistas.add(u_limpa)
            plano.append((u_limpa, None))

        host = (urlparse(u_limpa).netloc or "").lower().removeprefix("www.")
        for dominio_chave, alternativas in URLS_ALTERNATIVAS_PORTAL.items():
            if host == dominio_chave or host.endswith(f".{dominio_chave}"):
                for alt in alternativas:
                    if alt not in vistas:
                        vistas.add(alt)
                        # Marca apenas como categoria complementar (nunca bloqueia outras categorias do mesmo domínio)
                        plano.append((alt, None))
                break

    return plano


async def _rolar_pagina_lazy(page: Page, passos: int = 5, espera_s: float = 0.75) -> None:
    """Rola a página em etapas para disparar carregamento lazy-load dos cards."""
    try:
        for _ in range(passos):
            await page.evaluate("window.scrollBy(0, Math.floor(window.innerHeight * 0.85))")
            await asyncio.sleep(espera_s)
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(0.35)
    except Exception:
        pass


def _extrair_sinais_operacionais(texto: str) -> str:
    """Extrai trechos curtos sobre chave, posse, desocupação ou retomada para apoiar a auditoria."""
    baixa = texto.lower()
    termos = (
        "chave",
        "chaves",
        "desocupado",
        "ocupado",
        "posse",
        "alienação fiduciária",
        "alienacao fiduciaria",
        "recuperado de financiamento",
        "retomada",
        "débito",
        "debito",
        "ipva",
        "condomínio",
        "condominio",
    )
    trechos: list[str] = []
    for termo in termos:
        idx = baixa.find(termo)
        if idx != -1:
            ini = max(0, idx - 60)
            fim = min(len(texto), idx + 120)
            trecho = " ".join(texto[ini:fim].split())
            if trecho not in trechos:
                trechos.append(trecho)
        if len(trechos) >= 6:
            break
    return " | ".join(trechos)


async def _enriquecer_lote_na_pagina(page: Page, item: dict[str, Any]) -> dict[str, Any]:
    """Abre a url_lote para extrair descrição, cidade/UF e valores reais quando disponível."""
    if item.get("_enriquecido"):
        return item

    url_lote = item.get("url_lote")
    if not url_lote or not str(url_lote).startswith(("http://", "https://")):
        return item

    item["_enriquecido"] = True
    try:
        await page.goto(url_lote, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(1800)

        texto_pagina = ""
        seletores_desc = (
            "[class*='descricao'], [class*='description'], [id*='descricao'], "
            "[class*='detalhe-lote'], [class*='lot-detail'], main"
        )
        loc_desc = page.locator(seletores_desc).first
        if await loc_desc.count():
            texto_pagina = ((await loc_desc.inner_text()) or "").strip()

        if len(texto_pagina) < 40:
            texto_pagina = ((await page.locator("body").inner_text()) or "").strip()

        if not texto_pagina:
            return item

        linhas_pag = [ln.strip() for ln in texto_pagina.splitlines() if len(ln.strip()) >= 4]
        if _titulo_parece_generico(item.get("titulo_lote", "")) and linhas_pag:
            for ln in linhas_pag:
                if not _titulo_parece_generico(ln) and not ln.lower().startswith(
                    ("home", "leilões", "leiloes", "entrar", "cadastrar", "r$")
                ):
                    item["titulo_lote"] = ln[:180]
                    break

        cidade_atual = item.get("cidade")
        uf_atual = item.get("uf")
        cidade_real, uf_real = _extrair_cidade_uf(
            texto_pagina,
            padrao_cidade=cidade_atual if cidade_atual and cidade_atual != "Não informada" else "Não informada",
            padrao_uf=uf_atual if uf_atual and uf_atual != "XX" else "XX",
        )
        item["cidade"] = cidade_real
        item["uf"] = uf_real

        lance_real, avaliacao_real = _extrair_valores_com_rotulo(texto_pagina)
        contexto = f"{item.get('titulo_lote', '')} {texto_pagina[:1200]}"
        lance_ok, aval_ok = _garantir_valores_coerentes(lance_real, avaliacao_real, contexto)
        item["lance_atual"] = lance_ok
        item["valor_mercado_referencia"] = aval_ok

        pdf_link = page.locator("a[href*='.pdf'], a[href*='edital']").first
        if await pdf_link.count():
            href_pdf = await pdf_link.get_attribute("href")
            if href_pdf:
                item["url_edital_pdf"] = urljoin(url_lote, href_pdf.strip())

        descricao_antiga = item.get("descricao_lote", "")
        texto_limpo = " ".join(texto_pagina.split())
        sinais = _extrair_sinais_operacionais(texto_limpo)
        bloco_sinais = f"\n[SINAIS POSSE/EDITAL]: {sinais}" if sinais else ""
        item["descricao_lote"] = (
            f"{descricao_antiga}{bloco_sinais}\n\n[DETALHES DA PÁGINA DO LOTE]\n{texto_limpo[:2400]}".strip()[:3500]
        )
    except Exception as exc:
        logger.debug("Não foi possível enriquecer página do lote %s: %s", url_lote, exc)

    return item


def pre_filtrar_lote_bruto(bruto: dict[str, Any]) -> tuple[bool, str]:
    """Filtro rápido sem LLM para poupar cota diária de IA e evitar chamadas ao geocoder."""
    titulo = _normalizar_texto(bruto.get("titulo_lote"))
    descricao = _normalizar_texto(bruto.get("descricao_lote"))
    comitente = _normalizar_texto(bruto.get("comitente_credor"))
    url_lote = _normalizar_texto(bruto.get("url_lote"))
    combinado = f"{titulo} {descricao} {comitente} {url_lote}"

    if _titulo_parece_generico(bruto.get("titulo_lote", "")):
        return False, "Título genérico ou de navegação/login/categoria."

    for termo in BLOQUEIO_DURO:
        if termo in combinado:
            return False, f"Bloqueado no pré-filtro por termo: '{termo}'."

    tem_ativo_escopo = _tem_termo_escopo(f"{titulo} {combinado}")
    if not tem_ativo_escopo:
        for termo in RUIDO_SECUNDARIO:
            if termo in combinado:
                return False, f"Bloqueado no pré-filtro por termo: '{termo}'."

    cidade = (bruto.get("cidade") or "").strip()
    if not titulo or not cidade or cidade.lower() in {"não informada", "nao informada"}:
        return False, "Dados mínimos ausentes (título ou localização)."

    tem_indicio_fiduciaria = any(ind in combinado for ind in INDICADORES_FIDUCIARIA)
    if not tem_indicio_fiduciaria and not (bruto.get("_enriquecido") and tem_ativo_escopo):
        return False, "Sem indício claro de alienação fiduciária / retomada financeira."

    return True, "OK"


async def _coletar_brutos_sem_enriquecer(max_itens: int) -> list[dict[str, Any]]:
    _sanitizar_variaveis_ambiente()
    urls_base = [
        u.strip()
        for u in _limpar_env("SCRAPER_TARGET_URLS").split(",")
        if u.strip().startswith(("http://", "https://"))
    ]
    if not urls_base:
        logger.info("Nenhuma URL em SCRAPER_TARGET_URLS; nenhum lote novo raspado nesta rodada.")
        return []

    plano_urls = _expandir_urls_com_fallback(urls_base)
    coletados: list[dict[str, Any]] = []
    urls_vistas: set[str] = set()
    portais_com_candidatos: set[str] = set()

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

        cota_por_url = max(8, math.ceil(max_itens / max(len(plano_urls), 1)))
        for url, origem_fallback in plano_urls:
            if len(coletados) >= max_itens:
                break
            if origem_fallback and origem_fallback in portais_com_candidatos:
                continue

            try:
                logger.info("Acessando portal: %s", url)
                await page.goto(url, wait_until="domcontentloaded")
                await page.wait_for_timeout(2500)
                await _rolar_pagina_lazy(page)

                seletor_cards = (
                    "article, .lot-card, .card-lote, [data-testid='lot-card'], "
                    ".card-auction, .auction-card, .card-item, .offer-card, "
                    "[class*='lot-card'], [class*='card-lote'], [class*='LotCard'], [class*='CardLote']"
                )
                cards = page.locator(seletor_cards)
                total_cards = await cards.count()

                candidatos_extraidos = 0
                for idx in range(min(total_cards, max(max_itens * 3, 90))):
                    if len(coletados) >= max_itens or candidatos_extraidos >= cota_por_url:
                        break
                    card = cards.nth(idx)
                    texto_card = ((await card.inner_text()) or "").strip()
                    linhas = [ln.strip() for ln in texto_card.splitlines() if ln.strip()]
                    if not linhas or len(texto_card) < 12:
                        continue

                    href_raw = ""
                    links = card.locator("a[href]")
                    total_links = await links.count()
                    for l_idx in range(total_links):
                        cand = await links.nth(l_idx).get_attribute("href")
                        if cand:
                            abs_cand = _limpar_url_lote(urljoin(url, cand.strip()))
                            if _url_parece_lote(abs_cand, url):
                                href_raw = abs_cand
                                break
                    if not href_raw and total_links > 0:
                        cand0 = await links.first.get_attribute("href")
                        if cand0:
                            abs0 = _limpar_url_lote(urljoin(url, cand0.strip()))
                            if _url_parece_lote(abs0, url):
                                href_raw = abs0

                    if not href_raw or href_raw in urls_vistas:
                        continue
                    urls_vistas.add(href_raw)

                    titulo = _escolher_titulo_card(linhas, href_raw)
                    lance_est, mercado_est = _extrair_valores_com_rotulo(texto_card)
                    lance_ok, aval_ok = _garantir_valores_coerentes(
                        lance_est, mercado_est, f"{titulo} {texto_card}"
                    )
                    cidade_card, uf_card = _extrair_cidade_uf(texto_card)

                    coletados.append(
                        {
                            "leiloeiro": urlparse(url).netloc or "Portal Leilão",
                            "comitente_credor": "Banco / Financeira (verificar edital)",
                            "numero_lote": str(idx + 1),
                            "url_lote": href_raw,
                            "url_edital_pdf": None,
                            "titulo_lote": titulo,
                            "descricao_lote": texto_card[:1500],
                            "cidade": cidade_card,
                            "uf": uf_card,
                            "lance_atual": lance_ok,
                            "valor_mercado_referencia": aval_ok,
                        }
                    )
                    candidatos_extraidos += 1

                if candidatos_extraidos == 0:
                    anchors = page.locator("a[href]")
                    total_anchors = await anchors.count()
                    for a_idx in range(min(total_anchors, 450)):
                        if len(coletados) >= max_itens or candidatos_extraidos >= cota_por_url:
                            break
                        anc = anchors.nth(a_idx)
                        href_raw = await anc.get_attribute("href")
                        if not href_raw:
                            continue
                        abs_href = _limpar_url_lote(urljoin(url, href_raw.strip()))
                        if not _url_parece_lote(abs_href, url) or abs_href in urls_vistas:
                            continue

                        texto_anc = " ".join(((await anc.inner_text()) or "").split()).strip()
                        if len(texto_anc) < 8:
                            continue

                        urls_vistas.add(abs_href)
                        lance_est, mercado_est = _extrair_valores_com_rotulo(texto_anc)
                        lance_ok, aval_ok = _garantir_valores_coerentes(
                            lance_est, mercado_est, texto_anc
                        )
                        cidade_anc, uf_anc = _extrair_cidade_uf(texto_anc)

                        coletados.append(
                            {
                                "leiloeiro": urlparse(url).netloc or "Portal Leilão",
                                "comitente_credor": "Banco / Financeira (verificar edital)",
                                "numero_lote": str(candidatos_extraidos + 1),
                                "url_lote": abs_href,
                                "url_edital_pdf": None,
                                "titulo_lote": texto_anc[:180],
                                "descricao_lote": texto_anc[:1500],
                                "cidade": cidade_anc,
                                "uf": uf_anc,
                                "lance_atual": lance_ok,
                                "valor_mercado_referencia": aval_ok,
                            }
                        )
                        candidatos_extraidos += 1

                if candidatos_extraidos > 0:
                    host_atual = (urlparse(url).netloc or "").lower().removeprefix("www.")
                    for dom in URLS_ALTERNATIVAS_PORTAL:
                        if host_atual == dom or host_atual.endswith(f".{dom}"):
                            portais_com_candidatos.add(dom)

                logger.info(
                    "Encontrados %d elementos candidatos em %s", candidatos_extraidos, url
                )
            except Exception as exc:
                logger.warning("Falha ao raspar %s: %s", url, exc)

        await browser.close()

    return coletados


async def coletar_lotes_exemplo(max_itens: int = 10) -> list[dict[str, Any]]:
    """Coleta e enriquece até min(max_itens, 8) lotes nas páginas de detalhe."""
    coletados = await _coletar_brutos_sem_enriquecer(max_itens=max_itens)
    if not coletados:
        return []

    max_paginas_detalhe = min(len(coletados), max_itens, 8)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
        )
        page_detalhe = await context.new_page()
        page_detalhe.set_default_timeout(20000)
        for idx_item in range(max_paginas_detalhe):
            coletados[idx_item] = await _enriquecer_lote_na_pagina(page_detalhe, coletados[idx_item])
        await browser.close()

    return coletados


async def executar_pipeline_coleta(max_lotes: int = 12, delay_entre_lotes_s: float = 1.5) -> None:
    _sanitizar_variaveis_ambiente()
    brutos = await _coletar_brutos_sem_enriquecer(max_itens=max(max_lotes * 8, 60))
    if not brutos:
        logger.info("Pipeline encerrado sem novos lotes para avaliar.")
        return

    def _eh_imovel_bruto(item_b: dict[str, Any]) -> bool:
        txt_b = _normalizar_texto(
            f"{item_b.get('titulo_lote', '')} {item_b.get('descricao_lote', '')} {item_b.get('url_lote', '')}"
        )
        return any(
            k in txt_b
            for k in ("imovel", "imóvel", "apartamento", "apto", "casa", "terreno", "galpao", "galpão", "rural", "fazenda", "sítio", "sitio")
        )

    candidatos_veiculos_maq: list[dict[str, Any]] = []
    candidatos_imoveis: list[dict[str, Any]] = []
    limite_enriquecimento = min(max(max_lotes * 2, 16), 24)

    for bruto in brutos:
        combinado = f"{bruto.get('titulo_lote', '')} {bruto.get('descricao_lote', '')} {bruto.get('url_lote', '')}"
        bloqueado_direto = any(termo in _normalizar_texto(combinado) for termo in BLOQUEIO_DURO)
        if (
            not _titulo_parece_generico(bruto.get("titulo_lote", ""))
            and not bloqueado_direto
            and _tem_termo_escopo(combinado)
        ):
            if _eh_imovel_bruto(bruto):
                candidatos_imoveis.append(bruto)
            else:
                candidatos_veiculos_maq.append(bruto)

    # Intercala veículos/caminhões/máquinas com imóveis para garantir diversidade real na avaliação
    candidatos_enriquecer: list[dict[str, Any]] = []
    idx_v = 0
    idx_i = 0
    while len(candidatos_enriquecer) < limite_enriquecimento and (
        idx_v < len(candidatos_veiculos_maq) or idx_i < len(candidatos_imoveis)
    ):
        if idx_v < len(candidatos_veiculos_maq):
            candidatos_enriquecer.append(candidatos_veiculos_maq[idx_v])
            idx_v += 1
        if len(candidatos_enriquecer) < limite_enriquecimento and idx_i < len(candidatos_imoveis):
            candidatos_enriquecer.append(candidatos_imoveis[idx_i])
            idx_i += 1

    # Reordena `brutos` colocando os candidatos enriquecidos intercalados no início da fila de avaliação
    ids_prioritarios = {id(x) for x in candidatos_enriquecer}
    brutos = candidatos_enriquecer + [b for b in brutos if id(b) not in ids_prioritarios]

    if candidatos_enriquecer:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1366, "height": 768},
            )
            page_loc = await context.new_page()
            page_loc.set_default_timeout(20000)
            for item in candidatos_enriquecer:
                await _enriquecer_lote_na_pagina(page_loc, item)
            await browser.close()

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
        default=int(_limpar_env("MAX_LOTES_POR_EXECUCAO", "12") or "12"),
        help="Máximo de lotes enviados para a IA por execução",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.5,
        help="Pausa em segundos entre lotes para reduzir picos de API",
    )
    args = parser.parse_args()
    asyncio.run(executar_pipeline_coleta(max_lotes=args.max_lotes, delay_entre_lotes_s=args.delay))


if __name__ == "__main__":
    main()
