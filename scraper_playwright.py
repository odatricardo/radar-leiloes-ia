from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import re
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

from playwright.async_api import Page, async_playwright

from app.agents import resolver_distancia_km
from app.config import settings
from app.main import avaliar_lote_detalhado
from app.models import LoteEntrada

logger = logging.getLogger("radar_leiloes.scraper")
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

UFS_BRASIL = {
    "AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA",
    "MT", "MS", "MG", "PA", "PB", "PR", "PE", "PI", "RJ", "RN",
    "RS", "RO", "RR", "SC", "SP", "SE", "TO",
}

TERMOS_ESCOPO_ATIVOS = (
    # Caminhonetes e picapes 4x4 / diesel
    "caminhonete",
    "camionete",
    "picape",
    "pickup",
    "4x4",
    "hilux",
    "sw4",
    "ranger",
    "amarok",
    "s10",
    "trailblazer",
    "l200",
    "triton",
    "pajero",
    "frontier",
    "toro",
    "oroch",
    "saveiro",
    "strada",
    "montana",
    "ram 1500",
    "ram 2500",
    "ram 3500",
    "f-250",
    "f250",
    "f-350",
    "f350",
    "d-20",
    "d20",
    # Caminhões e implementos rodoviários pesados
    "caminhao",
    "caminhão",
    "cavalo mecanico",
    "cavalo mecânico",
    "trator rodoviario",
    "trator rodoviário",
    "carreta",
    "semirreboque",
    "semi-reboque",
    "bitrem",
    "rodotrem",
    "cacamba",
    "caçamba",
    "bau",
    "baú",
    "sider",
    "graneleiro",
    "prancha",
    "munck",
    "guindaste",
    "scania",
    "volvo fh",
    "volvo fm",
    "volvo vm",
    "mercedes-benz",
    "mercedes benz",
    "accelo",
    "atego",
    "axor",
    "actros",
    "constellation",
    "delivery",
    "worker",
    "vw 24.",
    "vw 17.",
    "vw 13.",
    "vw 9.",
    "vw 8.",
    "iveco",
    "stralis",
    "tector",
    "daily",
    "daf xf",
    "daf cf",
    "man tgx",
    "ford cargo",
    # Máquinas agrícolas e linha amarela
    "trator",
    "colheitadeira",
    "colhedora",
    "pulverizador",
    "plantadeira",
    "semeadeira",
    "retroescavadeira",
    "escavadeira",
    "pa carregadeira",
    "pá carregadeira",
    "motoniveladora",
    "rolo compactador",
    "minicarregadeira",
    "bobcat",
    "maquina agricola",
    "máquina agrícola",
    "implemento agricola",
    "implemento agrícola",
    "john deere",
    "massey ferguson",
    "valtra",
    "new holland",
    "case ih",
    "jacto",
    "stara",
    "yanmar",
    "agrale",
    "caterpillar",
    "komatsu",
    "hyundai",
    "jcb",
    # Imóveis urbanos e rurais
    "imovel",
    "imóvel",
    "apartamento",
    "apto",
    "casa",
    "sobrado",
    "terreno",
    "lote urbano",
    "gleba",
    "fazenda",
    "sitio",
    "sítio",
    "chacara",
    "chácara",
    "area rural",
    "área rural",
    "galpao",
    "galpão",
    "barracão",
    "barracao",
    "sala comercial",
    "predio comercial",
    "prédio comercial",
    "matricula",
    "matrícula",
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
    "massa falida",
    "falencia",
    "falência",
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

BLOQUEIO_QUALIDADE_CRITICA = (
    "sinistrado",
    "sinistro",
    "media monta",
    "média monta",
    "grande monta",
    "pequena monta",
    "colisao",
    "colisão",
    "capotado",
    "capotamento",
    "incendiado",
    "incendio",
    "incêndio",
    "alagamento",
    "enchente",
    "chassi recortado",
    "chassi remarcado",
    "sem motor",
    "motor fundido",
    "motor desmontado",
    "sem cambio",
    "sem câmbio",
    "desmanche",
    "somente pecas",
    "somente peças",
    "fracao ideal",
    "fração ideal",
    "parte ideal",
    "direitos possessorios",
    "direitos possessórios",
    "leilao de direitos",
    "leilão de direitos",
    "ocupado por terceiros com acao",
    "ocupado por terceiros com ação",
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
    "consolidacao da propriedade",
    "consolidação da propriedade",
    "lei 9.514",
    "9.514/97",
    "devedor fiduciante",
    "credor fiduciario",
    "credor fiduciário",
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


def _classificar_categoria_bruta(texto: str) -> str | None:
    """Identifica se o texto pertence a uma das 4 categorias-alvo do Radar."""
    baixa = _normalizar_texto(texto)
    if any(
        p in baixa
        for p in (
            "caminhão",
            "caminhao",
            "cavalo mecânico",
            "cavalo mecanico",
            "carreta",
            "semirreboque",
            "scania",
            "volvo",
            "iveco",
            "daf",
            "atego",
            "axor",
            "actros",
            "constellation",
            "ford cargo",
            "mercedes-benz 1",
            "mercedes benz 1",
            "mercedes-benz 2",
            "mercedes benz 2",
            "vw 24.",
            "vw 17.",
            "vw 13.",
            "vw 8.",
            "vw 9.",
            "vw 11.",
        )
    ):
        return "caminhao"

    if any(
        p in baixa
        for p in (
            "trator",
            "colhedora",
            "colheitadeira",
            "pulverizador",
            "plantadeira",
            "retroescavadeira",
            "escavadeira",
            "pá carregadeira",
            "pa carregadeira",
            "máquina agrícola",
            "maquina agricola",
            "john deere",
            "massey ferguson",
            "valtra",
            "new holland",
            "case ih",
        )
    ):
        return "maquina_agricola"

    if any(
        p in baixa
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
            "l200",
            "triton",
            "frontier",
            "toro",
        )
    ):
        return "caminhonete_4x4"

    if any(
        p in baixa
        for p in (
            "imovel",
            "imóvel",
            "apartamento",
            "apto",
            "casa",
            "terreno",
            "galpao",
            "galpão",
            "rural",
            "fazenda",
            "sítio",
            "sitio",
            "matrícula",
            "matricula",
            "lote urbano",
        )
    ):
        return "imovel"

    return None


def _tem_termo_escopo(texto: str) -> bool:
    if _classificar_categoria_bruta(texto) is not None:
        return True
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
    matches = re.findall(r"(?:R\$\s*)?(\d{1,3}(?:\.\d{3})+,\d{2}|R\$\s*\d{1,3}(?:\.\d{3})+)", texto)
    valores: list[float] = []
    for bruto in matches:
        val = _parse_moeda_brl(bruto)
        if val is not None:
            valores.append(val)
    return valores


def _parse_numero_estruturado(valor: Any) -> float | None:
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        num = float(valor)
        return round(num, 2) if num >= 500.0 else None
    if isinstance(valor, str):
        limpo = valor.strip()
        if not limpo:
            return None
        if "," in limpo:
            return _parse_moeda_brl(limpo)
        limpo_num = re.sub(r"[^\d.]", "", limpo)
        if limpo_num.count(".") > 1:
            limpo_num = limpo_num.replace(".", "")
        try:
            num = float(limpo_num)
            return round(num, 2) if num >= 500.0 else None
        except ValueError:
            return None
    return None


def _extrair_dados_estruturados_html(html: str) -> dict[str, Any]:
    """Extrai lance, avaliação, cidade e UF de JSON-LD, __NEXT_DATA__ e atributos data-*."""
    resultado: dict[str, Any] = {
        "lance": None,
        "avaliacao": None,
        "cidade": None,
        "uf": None,
    }
    if not html:
        return resultado

    lances_cand: list[float] = []
    aval_cand: list[float] = []

    def _varrer_obj(obj: Any, chave_contexto: str = "") -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                k_low = str(k).lower()
                ctx = f"{chave_contexto}.{k_low}" if chave_contexto else k_low

                if any(
                    t in k_low
                    for t in ("lance", "bid", "minimumbid", "currentbid", "price", "valor_inicial", "inicial")
                ):
                    num = _parse_numero_estruturado(v)
                    if num is not None:
                        lances_cand.append(num)

                if any(
                    t in k_low
                    for t in ("avaliacao", "avaliação", "appraisal", "marketvalue", "fipe", "mercado")
                ):
                    num = _parse_numero_estruturado(v)
                    if num is not None:
                        aval_cand.append(num)

                if any(t in k_low for t in ("city", "cidade", "municipio", "município", "addresslocality")):
                    if isinstance(v, str) and not _cidade_parece_ruido(v) and not resultado["cidade"]:
                        resultado["cidade"] = _formatar_nome_cidade(v)[:80]

                if any(t in k_low for t in ("uf", "state", "estado", "addressregion")):
                    if isinstance(v, str):
                        uf_up = v.strip().upper()
                        if len(uf_up) == 2 and uf_up in UFS_BRASIL and not resultado["uf"]:
                            resultado["uf"] = uf_up

                _varrer_obj(v, ctx)
        elif isinstance(obj, list):
            for elem in obj[:80]:
                _varrer_obj(elem, chave_contexto)

    for raw_ld in re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        try:
            _varrer_obj(json.loads(raw_ld.strip()), "json_ld")
        except Exception:
            continue

    m_next = re.search(
        r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if m_next:
        try:
            _varrer_obj(json.loads(m_next.group(1).strip()), "next_data")
        except Exception:
            pass

    for m_attr in re.finditer(
        r'\bdata-(price|lance|bid|avaliacao|avaliação|valor|city|cidade|uf|state)=["\']([^"\']{1,100})["\']',
        html,
        flags=re.IGNORECASE,
    ):
        attr_nome = m_attr.group(1).lower()
        attr_val = m_attr.group(2).strip()
        if attr_nome in ("price", "lance", "bid", "valor"):
            num = _parse_numero_estruturado(attr_val)
            if num is not None:
                lances_cand.append(num)
        elif attr_nome in ("avaliacao", "avaliação"):
            num = _parse_numero_estruturado(attr_val)
            if num is not None:
                aval_cand.append(num)
        elif attr_nome in ("city", "cidade") and not resultado["cidade"] and not _cidade_parece_ruido(attr_val):
            resultado["cidade"] = _formatar_nome_cidade(attr_val)[:80]
        elif attr_nome in ("uf", "state") and not resultado["uf"]:
            uf_up = attr_val.upper()
            if uf_up in UFS_BRASIL:
                resultado["uf"] = uf_up

    if lances_cand:
        resultado["lance"] = min(lances_cand)
    if aval_cand:
        lance_ref = resultado["lance"] or 0.0
        maiores = [v for v in aval_cand if v > lance_ref]
        resultado["avaliacao"] = max(maiores) if maiores else max(aval_cand)

    return resultado


def _garantir_valores_coerentes(
    lance: float | None, avaliacao: float | None, contexto_texto: str = ""
) -> tuple[float, float, bool]:
    """
    Retorna (lance_ok, aval_ok, valor_real_encontrado).
    Nunca inventa R$ 100.000 / R$ 165.000 quando o portal não traz valor real.
    """
    lance_ok = float(lance) if (lance is not None and lance >= 500.0) else None
    aval_ok = float(avaliacao) if (avaliacao is not None and avaliacao >= 500.0) else None

    if lance_ok is None and aval_ok is None:
        return 1000.0, 1050.0, False

    if lance_ok is not None and (aval_ok is None or aval_ok <= lance_ok):
        aval_ok = round(lance_ok * 1.35, 2)
    elif aval_ok is not None and lance_ok is None:
        lance_ok = round(aval_ok * 0.65, 2)

    if lance_ok > 0 and aval_ok > lance_ok * 3.5:
        aval_ok = round(lance_ok * 1.45, 2)

    return round(lance_ok, 2), round(aval_ok, 2), True


def _extrair_valores_com_rotulo(texto: str) -> tuple[float, float, bool]:
    lance: float | None = None
    avaliacao: float | None = None

    lances_explicitos: list[float] = []
    padroes_lance = (
        r"(?:[uú]ltimo\s*lance|lance\s*(?:m[ií]nimo(?:\s*atual)?|atual)|valor\s*(?:do\s*lance|m[ií]nimo))[^\dR$]{0,35}((?:R\$\s*)?\d{1,3}(?:\.\d{3})+(?:,\d{2})?)",
    )
    for pat in padroes_lance:
        for m in re.finditer(pat, texto, flags=re.IGNORECASE):
            v = _parse_moeda_brl(m.group(1))
            if v is not None:
                lances_explicitos.append(v)

    padroes_avaliacao = (
        r"(?:avalia[cç][aã]o|valor\s*de\s*mercado|mercado|fipe)[^\dR$]{0,35}((?:R\$\s*)?\d{1,3}(?:\.\d{3})+(?:,\d{2})?)",
    )
    for pat in padroes_avaliacao:
        m = re.search(pat, texto, flags=re.IGNORECASE)
        if m:
            avaliacao = _parse_moeda_brl(m.group(1))
            if avaliacao is not None:
                break

    iniciais_vals: list[float] = []
    for m_ini in re.finditer(
        r"\binicial\s*[:\-–]?\s*((?:R\$\s*)?\d{1,3}(?:\.\d{3})+(?:,\d{2})?)",
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

            partes_cadeia = [
                p.strip(" ,;-–/([|)")
                for p in re.split(r"\s*(?:/|-|–)\s*", cadeia_bruta)
                if p.strip()
            ]
            for parte in reversed(partes_cadeia):
                if _cidade_parece_ruido(parte):
                    continue
                cidade_fmt = _formatar_nome_cidade(parte)
                if len(cidade_fmt) >= 3:
                    return cidade_fmt[:80], uf_cand

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
                        plano.append((alt, None))
                break

    return plano


async def _rolar_pagina_lazy(page: Page, passos: int = 5, espera_s: float = 0.75) -> None:
    try:
        for _ in range(passos):
            await page.evaluate("window.scrollBy(0, Math.floor(window.innerHeight * 0.85))")
            await asyncio.sleep(espera_s)
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(0.35)
    except Exception:
        pass


def _extrair_sinais_operacionais(texto: str) -> str:
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
        "doc",
        "ipva pago",
        "sinistro",
        "monta",
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


def _extrair_texto_edital_pdf_sync(url_pdf: str, max_paginas: int = 6) -> str:
    """Baixa e lê as primeiras páginas de um edital em PDF (quando PyMuPDF estiver instalado)."""
    if not url_pdf or not url_pdf.lower().startswith(("http://", "https://")):
        return ""
    try:
        import fitz  # PyMuPDF
    except Exception:
        return ""

    try:
        req = Request(
            url_pdf,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                )
            },
        )
        with urlopen(req, timeout=12) as resp:
            conteudo = resp.read(4_500_000)
        if not conteudo or not conteudo.startswith(b"%PDF"):
            return ""
        doc = fitz.open(stream=conteudo, filetype="pdf")
        partes: list[str] = []
        for idx in range(min(len(doc), max_paginas)):
            txt = (doc[idx].get_text("text") or "").strip()
            if txt:
                partes.append(txt)
        doc.close()
        return " ".join(" ".join(partes).split())[:6000]
    except Exception:
        return ""


async def _extrair_texto_edital_pdf(url_pdf: str) -> str:
    return await asyncio.to_thread(_extrair_texto_edital_pdf_sync, url_pdf)


async def _enriquecer_lote_na_pagina(page: Page, item: dict[str, Any]) -> dict[str, Any]:
    if item.get("_enriquecido"):
        return item

    url_lote = item.get("url_lote")
    if not url_lote or not str(url_lote).startswith(("http://", "https://")):
        return item

    item["_enriquecido"] = True
    try:
        await page.goto(url_lote, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(1800)

        html_pagina = ""
        try:
            html_pagina = await page.content()
        except Exception:
            html_pagina = ""
        dados_estruturados = _extrair_dados_estruturados_html(html_pagina)

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

        pdf_link = page.locator("a[href*='.pdf'], a[href*='edital']").first
        if await pdf_link.count():
            href_pdf = await pdf_link.get_attribute("href")
            if href_pdf:
                item["url_edital_pdf"] = urljoin(url_lote, href_pdf.strip())

        texto_pdf = ""
        if item.get("url_edital_pdf"):
            texto_pdf = await _extrair_texto_edital_pdf(item["url_edital_pdf"])

        texto_combinado_fonte = f"{texto_pagina}\n{texto_pdf}".strip()

        if (
            not texto_combinado_fonte
            and not dados_estruturados.get("lance")
            and not dados_estruturados.get("avaliacao")
        ):
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
            texto_combinado_fonte,
            padrao_cidade=cidade_atual if cidade_atual and cidade_atual != "Não informada" else "Não informada",
            padrao_uf=uf_atual if uf_atual and uf_atual != "XX" else "XX",
        )
        if (not cidade_real or cidade_real == "Não informada") and dados_estruturados.get("cidade"):
            cidade_real = dados_estruturados["cidade"]
        if (not uf_real or uf_real == "XX") and dados_estruturados.get("uf"):
            uf_real = dados_estruturados["uf"]
        item["cidade"] = cidade_real
        item["uf"] = uf_real

        lance_real, avaliacao_real, tem_valor_real = _extrair_valores_com_rotulo(texto_combinado_fonte)
        if not tem_valor_real and (
            dados_estruturados.get("lance") is not None or dados_estruturados.get("avaliacao") is not None
        ):
            lance_real, avaliacao_real, tem_valor_real = _garantir_valores_coerentes(
                dados_estruturados.get("lance"),
                dados_estruturados.get("avaliacao"),
                f"{item.get('titulo_lote', '')} {texto_combinado_fonte[:1200]}",
            )

        if tem_valor_real:
            item["lance_atual"] = lance_real
            item["valor_mercado_referencia"] = avaliacao_real
            item["_valor_real"] = True
        elif not item.get("_valor_real"):
            item["lance_atual"] = lance_real
            item["valor_mercado_referencia"] = avaliacao_real
            item["_valor_real"] = False

        cat_detectada = _classificar_categoria_bruta(
            f"{item.get('titulo_lote', '')} {texto_combinado_fonte[:1500]}"
        )
        if cat_detectada:
            item["_categoria_detectada"] = cat_detectada

        descricao_antiga = item.get("descricao_lote", "")
        texto_limpo = " ".join(texto_combinado_fonte.split())
        sinais = _extrair_sinais_operacionais(texto_limpo)
        bloco_sinais = f"\n[SINAIS POSSE/EDITAL]: {sinais}" if sinais else ""
        aviso_valor = (
            ""
            if item.get("_valor_real")
            else "\n[AVISO_VALOR]: Valor não explícito na página; verificar edital/card."
        )
        item["descricao_lote"] = (
            f"{descricao_antiga}{bloco_sinais}{aviso_valor}\n\n[DETALHES DA PÁGINA DO LOTE]\n{texto_limpo[:2400]}".strip()[:3500]
        )
    except Exception as exc:
        logger.debug("Não foi possível enriquecer página do lote %s: %s", url_lote, exc)

    return item


def _avaliar_qualidade_e_margem_bruta(bruto: dict[str, Any]) -> tuple[bool, str, float]:
    """
    Filtro crítico de qualidade do bem e atratividade financeira antes da IA.
    Retorna (aprovado_pre_qualidade, motivo, desconto_bruto_pct).
    """
    combinado = _normalizar_texto(
        f"{bruto.get('titulo_lote', '')} {bruto.get('descricao_lote', '')} {bruto.get('url_lote', '')}"
    )

    for termo_critico in BLOQUEIO_QUALIDADE_CRITICA:
        if termo_critico in combinado:
            return False, f"Qualidade/risco reprovado no filtro crítico ('{termo_critico}').", 0.0

    lance = float(bruto.get("lance_atual") or 0.0)
    mercado = float(bruto.get("valor_mercado_referencia") or 0.0)
    tem_valor_real = bool(bruto.get("_valor_real"))

    if not tem_valor_real or lance <= 0 or mercado <= 0:
        return True, "OK (aguardando confirmação de valor na auditoria)", 0.0

    desconto_bruto_pct = ((mercado - lance) / mercado) * 100.0
    categoria = bruto.get("_categoria_detectada") or _classificar_categoria_bruta(combinado)

    desconto_minimo_exigido = 22.0 if categoria == "imovel" else 18.0
    if desconto_bruto_pct < desconto_minimo_exigido:
        return (
            False,
            f"Desconto bruto insuficiente ({desconto_bruto_pct:.1f}% < {desconto_minimo_exigido:.0f}% mínimo para margem líquida).",
            round(desconto_bruto_pct, 2),
        )

    return True, "OK", round(desconto_bruto_pct, 2)


def pre_filtrar_lote_bruto(bruto: dict[str, Any]) -> tuple[bool, str]:
    """Filtro rápido e crítico antes de gastar cota de geocoder/IA."""
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

    categoria = bruto.get("_categoria_detectada") or _classificar_categoria_bruta(combinado)
    tem_ativo_escopo = categoria is not None or _tem_termo_escopo(f"{titulo} {combinado}")
    if not tem_ativo_escopo:
        for termo in RUIDO_SECUNDARIO:
            if termo in combinado:
                return False, f"Bloqueado no pré-filtro por termo: '{termo}'."
        return False, "Fora das 4 categorias-alvo (caminhonete 4x4, caminhão, máquina agrícola, imóvel)."

    cidade = (bruto.get("cidade") or "").strip()
    if not titulo or not cidade or cidade.lower() in {"não informada", "nao informada"}:
        return False, "Dados mínimos ausentes (título ou localização)."

    ok_qualidade, motivo_qualidade, _ = _avaliar_qualidade_e_margem_bruta(bruto)
    if not ok_qualidade:
        return False, motivo_qualidade

    tem_indicio_fiduciaria = any(ind in combinado for ind in INDICADORES_FIDUCIARIA)
    if not tem_indicio_fiduciaria and not (bruto.get("_enriquecido") and categoria is not None):
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

        cota_por_url = max(10, math.ceil(max_itens / max(len(plano_urls), 1)))
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
                for idx in range(min(total_cards, max(max_itens * 3, 100))):
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
                    lance_est, mercado_est, tem_val = _extrair_valores_com_rotulo(texto_card)
                    cidade_card, uf_card = _extrair_cidade_uf(texto_card)
                    cat_card = _classificar_categoria_bruta(f"{titulo} {texto_card} {href_raw}")

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
                            "lance_atual": lance_est,
                            "valor_mercado_referencia": mercado_est,
                            "_valor_real": tem_val,
                            "_categoria_detectada": cat_card,
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
                        lance_est, mercado_est, tem_val = _extrair_valores_com_rotulo(texto_anc)
                        cidade_anc, uf_anc = _extrair_cidade_uf(texto_anc)
                        cat_anc = _classificar_categoria_bruta(f"{texto_anc} {abs_href}")

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
                                "lance_atual": lance_est,
                                "valor_mercado_referencia": mercado_est,
                                "_valor_real": tem_val,
                                "_categoria_detectada": cat_anc,
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
    brutos = await _coletar_brutos_sem_enriquecer(max_itens=max(max_lotes * 10, 80))
    if not brutos:
        logger.info("Pipeline encerrado sem novos lotes para avaliar.")
        return

    buckets: dict[str, list[dict[str, Any]]] = {
        "caminhonete_4x4": [],
        "caminhao": [],
        "maquina_agricola": [],
        "imovel": [],
    }

    for bruto in brutos:
        combinado = f"{bruto.get('titulo_lote', '')} {bruto.get('descricao_lote', '')} {bruto.get('url_lote', '')}"
        norm = _normalizar_texto(combinado)
        if _titulo_parece_generico(bruto.get("titulo_lote", "")):
            continue
        if any(termo in norm for termo in BLOQUEIO_DURO):
            continue
        if any(termo in norm for termo in BLOQUEIO_QUALIDADE_CRITICA):
            continue

        cat = bruto.get("_categoria_detectada") or _classificar_categoria_bruta(combinado)
        if cat in buckets:
            buckets[cat].append(bruto)

    ordem_rotacao = [
        "caminhonete_4x4",
        "caminhao",
        "maquina_agricola",
        "imovel",
        "caminhonete_4x4",
        "caminhao",
        "maquina_agricola",
    ]
    limite_enriquecimento = min(max(max_lotes * 3, 24), 32)
    candidatos_enriquecer: list[dict[str, Any]] = []
    indices_bucket = {k: 0 for k in buckets}

    while len(candidatos_enriquecer) < limite_enriquecimento:
        adicionou = False
        for cat_key in ordem_rotacao:
            if len(candidatos_enriquecer) >= limite_enriquecimento:
                break
            idx_b = indices_bucket[cat_key]
            if idx_b < len(buckets[cat_key]):
                candidatos_enriquecer.append(buckets[cat_key][idx_b])
                indices_bucket[cat_key] = idx_b + 1
                adicionou = True
        if not adicionou:
            break

    if candidatos_enriquecer:
        logger.info(
            "Enriquecendo %d candidatos prioritários (4x4=%d, caminhão=%d, agrícola=%d, imóvel=%d)...",
            len(candidatos_enriquecer),
            indices_bucket["caminhonete_4x4"],
            indices_bucket["caminhao"],
            indices_bucket["maquina_agricola"],
            indices_bucket["imovel"],
        )
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

    ids_prioritarios = {id(x) for x in candidatos_enriquecer}
    fila_completa = candidatos_enriquecer + [b for b in brutos if id(b) not in ids_prioritarios]

    def _chave_prioridade(item_ord: dict[str, Any]) -> tuple[int, int, float]:
        tem_val = 1 if item_ord.get("_valor_real") else 0
        cat_o = item_ord.get("_categoria_detectada") or ""
        peso_cat = (
            2
            if cat_o in ("caminhonete_4x4", "caminhao", "maquina_agricola")
            else (1 if cat_o == "imovel" else 0)
        )
        lance_o = float(item_ord.get("lance_atual") or 0.0)
        merc_o = float(item_ord.get("valor_mercado_referencia") or 0.0)
        desc_o = ((merc_o - lance_o) / merc_o) if (tem_val and merc_o > 0) else 0.0
        return (tem_val, peso_cat, desc_o)

    fila_completa.sort(key=_chave_prioridade, reverse=True)

    avaliados = 0
    aprovados = 0
    descartados_pre = 0
    fora_raio = 0

    for bruto in fila_completa:
        if avaliados >= max_lotes:
            logger.info("Limite de %d lotes avaliados por execução atingido.", max_lotes)
            break

        ok_filtro, motivo_filtro = pre_filtrar_lote_bruto(bruto)
        if not ok_filtro:
            descartados_pre += 1
            logger.info("Descartado no pré-filtro (%s): %s", motivo_filtro, bruto.get("titulo_lote"))
            continue

        try:
            lote_obj = LoteEntrada(**bruto)
        except Exception as exc:
            logger.warning("Lote inválido para schema LoteEntrada: %s", exc)
            continue

        dist_km = await resolver_distancia_km(lote_obj)
        if dist_km > settings.raio_maximo_km:
            fora_raio += 1
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
            "[%d/%d] Avaliando lote [%s | valor_real=%s]: %s (%s/%s - %.1f km) -> %s",
            avaliados,
            max_lotes,
            bruto.get("_categoria_detectada") or "indefinida",
            bruto.get("_valor_real"),
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

    logger.info(
        "Funil da execução: brutos=%d | enriquecidos=%d | descartados_pre=%d | fora_raio=%d | avaliados_ia=%d | aprovados=%d",
        len(brutos),
        len(candidatos_enriquecer),
        descartados_pre,
        fora_raio,
        avaliados,
        aprovados,
    )


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
