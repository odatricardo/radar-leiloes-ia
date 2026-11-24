import asyncio
import time
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from app import agents as ag
from app.agents import listar_modelos_disponiveis
from app.config import settings
from app.db import listar_oportunidades
from app.main import avaliar_lote_detalhado
from app.models import LoteEntrada

st.set_page_config(
    page_title="Radar Leilões IA — Recuperados de Financiamento",
    page_icon="💎",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    header[data-testid="stHeader"] {
        background: transparent;
    }
    .block-container {
        padding-top: 4rem;
        padding-bottom: 2.5rem;
    }
    div[data-testid="stMetric"] {
        background-color: #F8FAFC;
        border: 1px solid #CBD5E1;
        padding: 14px 16px;
        border-radius: 12px;
        overflow: hidden;
    }
    div[data-testid="stMetric"] label,
    div[data-testid="stMetric"] [data-testid="stMetricLabel"] {
        color: #334155 !important;
        font-size: 0.85rem !important;
        font-weight: 600 !important;
    }
    div[data-testid="stMetric"] [data-testid="stMetricValue"] {
        color: #0F172A !important;
        font-size: 1.35rem !important;
        font-weight: 700 !important;
        white-space: nowrap !important;
        overflow: visible !important;
        text-overflow: clip !important;
    }
    button[data-baseweb="tab"] {
        padding-top: 8px;
        padding-bottom: 8px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data(ttl=3600, show_spinner=False)
def carregar_modelos_cached() -> list[str]:
    return listar_modelos_disponiveis()


def formatar_brl(valor: float) -> str:
    valor_seguro = float(valor or 0.0)
    return f"R$ {valor_seguro:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def formatar_origem_ia(fonte: str) -> str:
    if fonte.startswith("gemini"):
        return f"Gemini ({fonte.replace('gemini', '').strip(' ()')})"
    if fonte.startswith("provedor_secundario"):
        return f"Provedor Secundário / Groq ({fonte.replace('provedor_secundario', '').strip(' ()')})"
    if fonte == "regras_contingencia":
        return "Contingência por Regras (fallback local)"
    return fonte


def sanitizar_url_anuncio(url: object) -> str | None:
    if not isinstance(url, str):
        return None
    limpa = url.strip()
    if not limpa.startswith(("http://", "https://")):
        return None
    if "nao-informado.local" in limpa.lower():
        return None
    return limpa


CATEGORIAS_LABELS = {
    "caminhonete_4x4": "🛻 Caminhonete 4x4",
    "caminhao": "🚛 Caminhão",
    "maquina_agricola": "🚜 Máquina Agrícola",
    "imovel": "🏢 Imóvel",
}


with st.sidebar:
    st.title("💎 Radar Leilões IA")
    st.caption("Base: Vinhedo/SP • Raio até 500 km • Alienação Fiduciária")
    st.divider()

    st.subheader("1. Filtros do Painel")
    filtro_semaforo = st.multiselect(
        "Semáforo de Risco",
        options=["VERDE", "AMARELO", "VERMELHO"],
        default=["VERDE", "AMARELO"],
    )
    filtro_nivel = st.multiselect(
        "Nível de Oportunidade",
        options=["DIAMANTE", "PADRAO", "ATENCAO", "DESCARTADO"],
        default=["DIAMANTE", "PADRAO", "ATENCAO"],
    )
    filtro_categorias = st.multiselect(
        "Classes de Ativos",
        options=list(CATEGORIAS_LABELS.keys()),
        default=list(CATEGORIAS_LABELS.keys()),
        format_func=lambda x: CATEGORIAS_LABELS.get(x, str(x)),
    )
    raio_max = st.slider(
        "Distância máxima de Vinhedo (km)", min_value=50, max_value=500, value=500, step=25
    )
    roi_min_filtro = st.slider(
        "Margem líquida mínima exibida (%)", min_value=15, max_value=60, value=20, step=1
    )

    st.divider()
    st.subheader("2. Motor Gemini Ativo")
    modelos_disponiveis = carregar_modelos_cached()
    indice_padrao = (
        modelos_disponiveis.index(settings.gemini_model_fast)
        if settings.gemini_model_fast in modelos_disponiveis
        else 0
    )
    modelo_selecionado = st.selectbox(
        "Modelo Principal (troque se der 503)",
        options=modelos_disponiveis,
        index=indice_padrao,
    )
    settings.gemini_model_fast = modelo_selecionado
    settings.gemini_model_audit = modelo_selecionado

    if settings.openai_api_key:
        st.caption(f"Fallback secundário ativo: `{settings.openai_model}`")
    else:
        st.caption("Fallback secundário: não configurado (usa regras se IA falhar)")

    st.divider()
    if st.button("🔄 Atualizar Dados do Banco", width="stretch"):
        st.rerun()


tab_dash, tab_avaliar, tab_simulador = st.tabs(
    ["📊 Dashboard Executivo", "⚡ Analisar Novo Lote / Edital (1 Clique)", "🧮 Simulador Visual de Lance"]
)

with tab_dash:
    st.subheader("Central de Oportunidades — Recuperados de Financiamento")

    try:
        registros = listar_oportunidades(limite=200)
    except Exception as e:
        st.error(f"Não foi possível conectar ao banco de dados. Verifique DATABASE_URL. Detalhe: {e}")
        registros = []

    if not registros:
        st.info(
            "Nenhum lote cadastrado ainda. Use a aba **'Analisar Novo Lote / Edital'** para avaliar o primeiro lote com 1 clique."
        )
    else:
        df = pd.DataFrame(registros)
        col_contagem = "lote_id" if "lote_id" in df.columns else df.columns[0]

        df_filtrado = df[
            (df["semaforo"].isin(filtro_semaforo))
            & (df["nivel_oportunidade"].isin(filtro_nivel))
            & (df["categoria"].isin(filtro_categorias))
            & (df["distancia_vinhedo_km"] <= raio_max)
            & (df["margem_liquida_projetada_pct"] >= roi_min_filtro)
        ].copy()

        c1, c2, c3, c4 = st.columns(4)
        total_lotes = len(df_filtrado)
        total_diamantes = int((df_filtrado["nivel_oportunidade"] == "DIAMANTE").sum())
        roi_medio = float(df_filtrado["margem_liquida_projetada_pct"].mean()) if total_lotes else 0.0
        lucro_potencial_top5 = (
            float(df_filtrado.nlargest(5, "lucro_liquido_estimado")["lucro_liquido_estimado"].sum())
            if total_lotes
            else 0.0
        )

        c1.metric("Oportunidades Filtradas", f"{total_lotes} lotes")
        c2.metric("💎 Diamante (≥ 35%)", f"{total_diamantes} lotes")
        c3.metric("Margem Líquida Média (ROI)", f"{roi_medio:.1f}%")
        c4.metric("Lucro Potencial (Top 5 Lotes)", formatar_brl(lucro_potencial_top5))

        st.divider()

        if df_filtrado.empty:
            st.warning("Nenhum lote corresponde aos filtros selecionados na barra lateral.")
        else:
            g1, g2 = st.columns(2)

            with g1:
                fig_scatter = px.scatter(
                    df_filtrado,
                    x="distancia_vinhedo_km",
                    y="margem_liquida_projetada_pct",
                    size="lucro_liquido_estimado",
                    color="nivel_oportunidade",
                    hover_name="titulo",
                    hover_data={
                        "cidade": True,
                        "uf": True,
                        "lance_atual": ":,.2f",
                        "lance_maximo_recomendado": ":,.2f",
                    },
                    color_discrete_map={
                        "DIAMANTE": "#0284C7",
                        "PADRAO": "#16A34A",
                        "ATENCAO": "#D97706",
                        "DESCARTADO": "#DC2626",
                    },
                    title="Mapa de Decisão: Distância (km) vs. Margem Líquida (%)",
                    labels={
                        "distancia_vinhedo_km": "Distância de Vinhedo/SP (km)",
                        "margem_liquida_projetada_pct": "Margem Líquida Real (%)",
                        "nivel_oportunidade": "Classificação",
                    },
                )
                fig_scatter.add_hline(
                    y=35,
                    line_dash="dash",
                    line_color="#0284C7",
                    annotation_text="Linha Diamante (35%)",
                )
                fig_scatter.update_layout(template="plotly_white", height=390)
                st.plotly_chart(fig_scatter, width="stretch")

            with g2:
                resumo_cat = df_filtrado.groupby("categoria", as_index=False).agg(
                    roi_medio=("margem_liquida_projetada_pct", "mean"),
                    lucro_medio=("lucro_liquido_estimado", "mean"),
                    qtd=(col_contagem, "count"),
                )
                resumo_cat["categoria_label"] = resumo_cat["categoria"].map(
                    lambda x: CATEGORIAS_LABELS.get(x, str(x))
                )
                fig_bar = px.bar(
                    resumo_cat,
                    x="categoria_label",
                    y="roi_medio",
                    text=resumo_cat["roi_medio"].apply(lambda v: f"{v:.1f}%"),
                    color="categoria_label",
                    title="ROI Líquido Médio por Classe de Ativo (%)",
                    labels={
                        "categoria_label": "Classe de Ativo",
                        "roi_medio": "ROI Líquido Médio (%)",
                    },
                )
                fig_bar.update_layout(template="plotly_white", showlegend=False, height=390)
                st.plotly_chart(fig_bar, width="stretch")

            st.subheader("Ranking de Lotes Aprovados (Ordenado por Prioridade e Score)")
            df_filtrado["url_lote_segura"] = df_filtrado["url_lote"].apply(sanitizar_url_anuncio)

            df_exibir = df_filtrado[
                [
                    "nivel_oportunidade",
                    "semaforo",
                    "score_qualidade",
                    "categoria",
                    "titulo",
                    "cidade",
                    "uf",
                    "distancia_vinhedo_km",
                    "lance_atual",
                    "lance_maximo_diamante",
                    "lance_maximo_recomendado",
                    "margem_liquida_projetada_pct",
                    "lucro_liquido_estimado",
                    "url_lote_segura",
                ]
            ].rename(
                columns={
                    "nivel_oportunidade": "Nível",
                    "semaforo": "Status",
                    "score_qualidade": "Score",
                    "categoria": "Ativo",
                    "titulo": "Lote",
                    "cidade": "Cidade",
                    "uf": "UF",
                    "distancia_vinhedo_km": "Dist. (km)",
                    "lance_atual": "Lance Atual (R$)",
                    "lance_maximo_diamante": "Teto Diamante (R$)",
                    "lance_maximo_recomendado": "Teto Base (R$)",
                    "margem_liquida_projetada_pct": "ROI Líq. (%)",
                    "lucro_liquido_estimado": "Lucro Est. (R$)",
                    "url_lote_segura": "Link",
                }
            )
            st.dataframe(
                df_exibir,
                width="stretch",
                hide_index=True,
                column_config={
                    "Link": st.column_config.LinkColumn("Abrir Anúncio", display_text="Abrir anúncio"),
                    "Lance Atual (R$)": st.column_config.NumberColumn(format="R$ %.2f"),
                    "Teto Diamante (R$)": st.column_config.NumberColumn(format="R$ %.2f"),
                    "Teto Base (R$)": st.column_config.NumberColumn(format="R$ %.2f"),
                    "Lucro Est. (R$)": st.column_config.NumberColumn(format="R$ %.2f"),
                    "ROI Líq. (%)": st.column_config.NumberColumn(format="%.1f%%"),
                },
            )


with tab_avaliar:
    st.subheader("Auditoria Instantânea com Agentes de IA")
    st.caption(
        f"Modelo ativo: **{modelo_selecionado}**. Cole os dados ou o link do PDF do edital abaixo para auditar e salvar direto no painel."
    )

    with st.form("form_novo_lote"):
        col1, col2, col3 = st.columns(3)
        leiloeiro = col1.text_input("Leiloeiro / Portal", placeholder="Ex: Superbid / Sodré Santoro")
        comitente = col2.text_input(
            "Banco / Financeira Credora", placeholder="Ex: Banco Bradesco / Banco John Deere"
        )
        numero_lote = col3.text_input("Número do Lote", placeholder="Ex: 042")

        titulo_lote = st.text_input(
            "Título do Lote",
            placeholder="Ex: Toyota Hilux SRX 4x4 2022/2023 Retomada Financiamento",
        )
        col_c1, col_c2, col_c3, col_c4 = st.columns(4)
        cidade = col_c1.text_input("Cidade do Pátio/Imóvel", placeholder="Ex: Ribeirão Preto")
        uf = col_c2.text_input("UF", value="SP", max_chars=2)
        lance_atual = col_c3.number_input(
            "Lance Atual / Mínimo (R$)", min_value=1000.0, value=145000.0, step=1000.0
        )
        valor_mercado = col_c4.number_input(
            "Valor de Revenda Conservador (R$)", min_value=1000.0, value=220000.0, step=1000.0
        )

        url_lote = st.text_input(
            "URL do Anúncio do Bem no Portal do Leiloeiro",
            placeholder="https://www.portaldeleilao.com.br/lote/...",
        )
        url_edital_pdf = st.text_input(
            "URL Direta do PDF do Edital (Opcional - leitura automática)",
            placeholder="https://.../edital.pdf",
        )
        descricao_lote = st.text_area("Descrição do Lote / Observações do Edital", height=130)

        enviado = st.form_submit_button(
            "🚀 Auditar Lote com IA e Calcular Teto de Lance", width="stretch"
        )

    if enviado:
        if not leiloeiro or not comitente or not titulo_lote or not cidade:
            st.error("Preencha pelo menos Leiloeiro, Banco Credor, Título e Cidade.")
        else:
            ag.ultima_fonte_ia = "nao_executado"
            inicio = time.perf_counter()
            url_anuncio_limpa = sanitizar_url_anuncio(url_lote)
            with st.spinner(
                f"Auditando lote com {modelo_selecionado} e calculando distância de Vinhedo/SP..."
            ):
                lote_obj = LoteEntrada(
                    leiloeiro=leiloeiro,
                    comitente_credor=comitente,
                    numero_lote=numero_lote or "1",
                    url_lote=url_anuncio_limpa or "https://nao-informado.local",
                    url_edital_pdf=url_edital_pdf or None,
                    titulo_lote=titulo_lote,
                    descricao_lote=descricao_lote or titulo_lote,
                    cidade=cidade,
                    uf=uf.upper(),
                    lance_atual=lance_atual,
                    valor_mercado_referencia=valor_mercado,
                )
                try:
                    resposta = asyncio.run(avaliar_lote_detalhado(lote_obj))
                    erro_execucao = None
                except Exception as exc:
                    resposta = None
                    erro_execucao = str(exc)

            duracao = time.perf_counter() - inicio
            origem_rotulo = formatar_origem_ia(ag.ultima_fonte_ia)

            if erro_execucao:
                st.error(f"Falha na análise após {duracao:.1f}s ({origem_rotulo}): {erro_execucao}")
            elif resposta is None or not resposta.aprovado or resposta.curadoria is None:
                motivo = (
                    resposta.motivo_rejeicao
                    if resposta
                    else "Lote descartado automaticamente na triagem."
                )
                st.warning(f"{motivo} *(Tempo: {duracao:.1f}s • Motor: {origem_rotulo})*")
            else:
                resultado = resposta.curadoria
                cor_banner = {"VERDE": "success", "AMARELO": "warning", "VERMELHO": "error"}.get(
                    resultado.semaforo, "info"
                )
                getattr(st, cor_banner)(
                    f"**Resultado: {resultado.semaforo} | Nível {resultado.nivel_oportunidade} "
                    f"(Score {resultado.score_qualidade}/100) — {duracao:.1f}s**\n\n"
                    f"**Motor utilizado:** {origem_rotulo}\n\n"
                    f"{resultado.resumo_executivo}\n\n**Ação:** {resultado.acao_recomendada}"
                )
                m1, m2, m3, m4 = st.columns(4)
                m1.metric(
                    "Teto Diamante (≥35%)",
                    formatar_brl(resultado.viabilidade.lance_maximo_diamante),
                )
                m2.metric(
                    "Teto Meta Base", formatar_brl(resultado.viabilidade.lance_maximo_recomendado)
                )
                m3.metric(
                    "Margem Líquida",
                    f"{resultado.viabilidade.margem_liquida_projetada_pct:.1f}%",
                )
                m4.metric(
                    "Lucro Estimado",
                    formatar_brl(resultado.viabilidade.lucro_liquido_estimado),
                )

                with st.expander("Detalhes Jurídicos e Operacionais da Auditoria"):
                    st.write(f"**Motor utilizado:** {origem_rotulo}")
                    if url_anuncio_limpa:
                        st.write(f"**Anúncio do Bem:** [{url_anuncio_limpa}]({url_anuncio_limpa})")
                    else:
                        st.write("**Anúncio do Bem:** URL não informada")
                    st.write(
                        f"**Modalidade:** {resultado.triagem.modalidade_identificada or 'Alienação Fiduciária'}"
                    )
                    st.write(
                        f"**Distância de Vinhedo/SP:** {resultado.viabilidade.distancia_vinhedo_km:.1f} km"
                    )
                    st.write(f"**Posse / Chave:** {resultado.auditoria.posse_ou_chave}")
                    st.write(
                        f"**Responsável por Débitos:** {resultado.auditoria.responsavel_debitos_anteriores}"
                    )
                    st.write(f"**Resumo das Cláusulas:** {resultado.auditoria.resumo_clausulas}")
                    if resultado.auditoria.alertas_criticos:
                        st.warning("Alertas: " + " | ".join(resultado.auditoria.alertas_criticos))


with tab_simulador:
    st.subheader("Simulador Rápido de Viabilidade Antes do Pregão")
    s1, s2, s3 = st.columns(3)
    sim_revenda = s1.number_input(
        "Preço de Revenda Conservador (R$)", min_value=10000.0, value=250000.0, step=5000.0
    )
    sim_lance = s2.number_input(
        "Seu Lance Pretendido (R$)", min_value=5000.0, value=155000.0, step=2500.0
    )
    sim_custos = s3.number_input(
        "Custos Totais (Frete + Pátio + Doc + Contingência) (R$)",
        min_value=0.0,
        value=18500.0,
        step=500.0,
    )

    comissao = sim_lance * 0.05
    custo_total = sim_lance + comissao + sim_custos
    lucro = sim_revenda - custo_total
    roi = (lucro / custo_total * 100.0) if custo_total > 0 else 0.0

    fig_waterfall = go.Figure(
        go.Waterfall(
            orientation="v",
            measure=["relative", "relative", "relative", "total", "relative", "total"],
            x=[
                "Lance",
                "Comissão (5%)",
                "Custos & Contingência",
                "Custo Total",
                "Margem Bruta",
                "Preço Revenda",
            ],
            y=[sim_lance, comissao, sim_custos, 0, lucro, 0],
            connector={"line": {"color": "#94A3B8"}},
        )
    )
    fig_waterfall.update_layout(
        title=f"Composição Financeira da Operação — ROI Líquido: {roi:.1f}% | Lucro: {formatar_brl(lucro)}",
        template="plotly_white",
        height=400,
    )

    st.plotly_chart(fig_waterfall, width="stretch")
