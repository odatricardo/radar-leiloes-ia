from app.config import settings
from app.models import AuditoriaEditalResultado, CategoriaAtivo, LoteEntrada, ViabilidadeFinanceira

MARGEM_DIAMANTE_PCT = 35.0


def obter_margem_minima(categoria: CategoriaAtivo) -> float:
    mapa = {
        "caminhonete_4x4": settings.margem_min_4x4,
        "caminhao": settings.margem_min_caminhao,
        "maquina_agricola": settings.margem_min_maquina,
        "imovel": settings.margem_min_imovel,
    }
    return mapa[categoria]


def estimar_frete(categoria: CategoriaAtivo, distancia_km: float) -> float:
    km = max(distancia_km, 0.0)
    if categoria == "caminhonete_4x4":
        return round(350.0 + km * 3.2, 2)
    if categoria == "caminhao":
        return round(900.0 + km * 7.5, 2)
    if categoria == "maquina_agricola":
        return round(1500.0 + km * 11.0, 2)
    return 0.0


def calcular_viabilidade(
    lote: LoteEntrada,
    categoria: CategoriaAtivo,
    auditoria: AuditoriaEditalResultado,
    distancia_km: float,
) -> ViabilidadeFinanceira:
    margem_min_pct = obter_margem_minima(categoria)
    margem_min_frac = margem_min_pct / 100.0
    margem_diamante_frac = MARGEM_DIAMANTE_PCT / 100.0
    comissao_frac = auditoria.comissao_leiloeiro_pct / 100.0

    frete = estimar_frete(categoria, distancia_km)
    taxa_patio = auditoria.taxa_administrativa_patio or lote.taxa_admin_padrao

    debitos_repassados = (
        auditoria.valor_debitos_repassados_estimado
        if auditoria.responsavel_debitos_anteriores != "credor_vendedor"
        else 0.0
    )

    if categoria == "imovel":
        impostos_e_cartorio = lote.valor_mercado_referencia * 0.045
        reparos_e_condominio = 8000.0
        corretagem_saida = lote.valor_mercado_referencia * 0.05 if lote.incluir_corretagem_imovel else 0.0
        custos_operacionais_categoria = impostos_e_cartorio + reparos_e_condominio + corretagem_saida
        contingencia_pct = 0.05
    elif categoria in ("caminhao", "maquina_agricola"):
        custos_operacionais_categoria = 2800.0 + 8500.0
        contingencia_pct = 0.03
    else:
        custos_operacionais_categoria = 2800.0
        contingencia_pct = 0.03

    custos_nao_proporcionais = frete + taxa_patio + debitos_repassados + custos_operacionais_categoria
    contingencia = lote.valor_mercado_referencia * contingencia_pct

    alvo_custo_base = lote.valor_mercado_referencia / (1.0 + margem_min_frac)
    lance_max_base = max(
        0.0,
        (alvo_custo_base - custos_nao_proporcionais - contingencia) / (1.0 + comissao_frac),
    )

    alvo_custo_diamante = lote.valor_mercado_referencia / (1.0 + margem_diamante_frac)
    lance_max_diamante = max(
        0.0,
        (alvo_custo_diamante - custos_nao_proporcionais - contingencia) / (1.0 + comissao_frac),
    )

    comissao_atual = lote.lance_atual * comissao_frac
    custo_total_atual = lote.lance_atual + comissao_atual + custos_nao_proporcionais + contingencia
    lucro_liquido = lote.valor_mercado_referencia - custo_total_atual
    margem_pct = (lucro_liquido / custo_total_atual * 100.0) if custo_total_atual > 0 else 0.0

    eh_diamante = margem_pct >= MARGEM_DIAMANTE_PCT

    return ViabilidadeFinanceira(
        distancia_vinhedo_km=round(distancia_km, 1),
        frete_estimado=round(frete, 2),
        custos_fixos_e_impostos=round(custos_nao_proporcionais, 2),
        reserva_contingencia=round(contingencia, 2),
        custo_total_aquisicao=round(custo_total_atual, 2),
        lance_maximo_recomendado=round(lance_max_base, 2),
        lance_maximo_diamante=round(lance_max_diamante, 2),
        margem_liquida_projetada_pct=round(margem_pct, 2),
        lucro_liquido_estimado=round(lucro_liquido, 2),
        oportunidade_diamante=eh_diamante,
    )