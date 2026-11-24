import asyncio
import math
import re
from typing import Any

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from app import agents as ag
from app.agents import listar_modelos_disponiveis
from app.config import settings
from app.db import listar_oportunidades, salvar_lote_avaliado
from app.finance import MARGEM_DIAMANTE_PCT
from app.main import avaliar_lote_detalhado
from app.models import LoteEntrada

st.set_page_config(
    page_title="Radar de Leilões IA — Recuperados de Financiamento",
    page_icon="💎",
    layout="wide",
)

TERMOS_FORA_EXIBICAO = (
    "moto ",
    "motocicleta",
    "biz ",
    "honda/biz",
    "sucata",
    "irrecuperável",
    "irrecuperavel",
    "judicial online",
    "leiloesjudiciais.com.br",
    "execução fiscal",
    "execucao fiscal",
    "trabalhista",
    "sem título",
    "sem titulo",
)

PESO_NIVEL = {
    "DIAMANTE": 0,
    "PADRAO": 1,
    "PADRÃO": 1,
    "ATENCAO": 2,
    "ATENÇÃO": 2,
    "DESCARTADO": 3,
}

PESO_SEMAFORO = {
    "VERDE": 0,
    "AMARELO": 1,
    "VERMELHO": 2,
}


def _correr_async(coro: Any) -> Any:
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, coro).result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


def _normalizar_dataframe(raw_itens: Any) -> pd.DataFrame:
    if not raw_itens:
        return pd.DataFrame()

    registros: list[dict[str, Any]] = []
    for item in raw_itens:
        if isinstance(item, dict):
            base = dict(item)
            if "titulo" in base and not base.get("titulo_lote"):
                base["titulo_lote"] = base["titulo"]
            for aninhado in ("curadoria", "triagem", "auditoria", "viabilidade", "payload_completo"):
                sub = base.get(aninhado)
                if isinstance(sub, dict):
                    for k, v in sub.items():
                        base.setdefault(k, v)
            registros.append(base)
        elif hasattr(item, "model_dump"):
            dumped = item.model_dump()
            for aninhado in ("triagem", "auditoria", "viabilidade"):
                sub = dumped.get(aninhado)
                if isinstance(sub, dict):
                    for k, v in sub.items():
                        dumped.setdefault(k, v)
            registros.append(dumped)

    if not registros:
        return pd.DataFrame()

    df = pd.DataFrame(registros)

    for col_num, padrao in (
        ("score_qualidade", 0.0),
        ("distancia_vinhedo_km", math.nan),
        ("lance_atual", 0.0),
        ("valor_mercado_referencia", 0.0),
        ("margem_liquida_projetada_pct", 0.0),
        ("lucro_liquido_estimado", 0.0),
        ("lance_maximo_recomendado", 0.0),
        ("lance_maximo_diamante", 0.0),
    ):
        if col_num in df.columns:
            df[col_num] = pd.to_numeric(df[col_num], errors="coerce")
            if not math.isnan(padrao):
                df[col_num] = df[col_num].fillna(padrao)
        else:
            df[col_num] = padrao

    if df["margem_liquida_projetada_pct"].eq(0.0).all():
        mascara_val = (df["valor_mercado_referencia"] > 0) & (df["lance_atual"] > 0)
        df.loc[mascara_val, "margem_liquida_projetada_pct"] = (
            (
                (df.loc[mascara_val, "valor_mercado_referencia"] - df.loc[mascara_val, "lance_atual"])
                / df.loc[mascara_val, "valor_mercado_referencia"]
            )
            * 100.0
        ).round(1)

    for col_str, padrao in (
        ("nivel_oportunidade", "PADRAO"),
        ("semaforo", "AMARELO"),
        ("categoria", "nao_informado"),
        ("titulo_lote", ""),
        ("descricao_lote", ""),
        ("cidade", ""),
        ("uf", ""),
        ("leiloeiro", ""),
        ("url_lote", ""),
    ):
        if col_str in df.columns:
            df[col_str] = df[col_str].fillna(padrao).astype(str).str.strip()
        else:
            df[col_str] = padrao

    df["nivel_oportunidade"] = df["nivel_oportunidade"].replace("", "PADRAO").str.upper()
    df["semaforo"] = df["semaforo"].replace("", "AMARELO").str.upper()
    df["categoria"] = df["categoria"].replace("", "nao_informado").str.lower()

    mascara_incoerente = (df["nivel_oportunidade"] == "DIAMANTE") & (df["semaforo"] == "VERMELHO")
    df.loc[mascara_incoerente, "nivel_oportunidade"] = "DESCARTADO"

    return df


def filtrar_e_ordenar_oportunidades(
    df: pd.DataFrame,
    raio_max_km: float = 500.0,
    exibir_descartados: bool = False,
) -> pd.DataFrame:
    """Remove registros inválidos/fora do escopo e ordena por prioridade."""
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    if not exibir_descartados:
        out = out[
            (out["nivel_oportunidade"] != "DESCARTADO") & (out["semaforo"] != "VERMELHO")
        ]

    out = out[out["titulo_lote"].str.len() >= 5]

    dist_num = pd.to_numeric(out["distancia_vinhedo_km"], errors="coerce")
    out = out[dist_num.isna() | (dist_num <= raio_max_km)]

    cols_texto = [
        c
        for c in ("titulo_lote", "titulo", "descricao_lote", "url_lote", "leiloeiro")
        if c in out.columns
    ]
    if cols_texto and not out.empty:
        serie_texto = pd.Series("", index=out.index, dtype="object")
        for c in cols_texto:
            serie_texto = serie_texto + " " + out[c].fillna("").astype(str)
        serie_texto = serie_texto.str.lower()
        mascara_bloqueio = serie_texto.apply(
            lambda txt: any(termo in txt for termo in TERMOS_FORA_EXIBICAO)
        )
        out = out[~mascara_bloqueio]

    if not out.empty:
        mascara_generico = out["titulo_lote"].str.lower().apply(
            lambda t: bool(re.fullmatch(r"\d+\s*lotes?", t.strip()))
        )
        out = out[~mascara_generico]

    if out.empty:
        return out

    out["_chave_dedup"] = out.apply(
        lambda r: (
            r["url_lote"].strip().lower()
            if r["url_lote"].strip()
            else f"{r['titulo_lote'].lower()}|{r['cidade'].lower()}|{r['uf'].lower()}"
        ),
        axis=1,
    )
    out = out.drop_duplicates(subset=["_chave_dedup"], keep="first").drop(columns=["_chave_dedup"])

    if out.empty:
        return out

    out["_ord_nivel"] = out["nivel_oportunidade"].map(PESO_NIVEL).fillna(9)
    out["_ord_sem"] = out["semaforo"].map(PESO_SEMAFORO).fillna(9)
    out["_ord_score"] = pd.to_numeric(out["score_qualidade"], errors="coerce").fillna(0)
    out["_ord_margem"] = pd.to_numeric(out["margem_liquida_projetada_pct"], errors="coerce").fillna(0)

    out = out.sort_values(
        by=["_ord_nivel", "_ord_sem", "_ord_score", "_ord_margem"],
        ascending=[True, True, False, False],
    ).drop(columns=["_ord_nivel", "_ord_sem", "_ord_score", "_ord_margem"])

    return out.reset_index(drop=True)


def _formatar_moeda(valor: Any) -> str:
    try:
        num = float(valor)
        if math.isnan(num):
            return "—"
        return f"R$ {num:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return "—"


def _formatar_dist(valor: Any) -> str:
    try:
        num = float(valor)
        if math.isnan(num):
            return "—"
        return f"{num:.1f} km"
    except Exception:
        return "—"


def main() -> None:
    st.title("💎 Radar de Leilões IA — Recuperados de Financiamento")
    st.caption(
        f"Base operacional: {settings.base_cidade}/{settings.base_uf} • "
        f"Raio máximo: {settings.raio_maximo_km:.0f} km • "
        f"Corte Diamante: {MARGEM_DIAMANTE_PCT:.0f}%+"
    )

    if "oportunidades_raw" not in st.session_state:
        try:
            st.session_state["oportunidades_raw"] = listar_oportunidades(limite=200)
        except Exception as exc:
            st.session_state["oportunidades_raw"] = []
            st.warning(f"Não foi possível carregar oportunidades iniciais do banco: {exc}")

    with st.sidebar:
        st.header("Controles & Filtros")
        if st.button("🔄 Atualizar Dados do Banco", use_container_width=True):
            try:
                st.session_state["oportunidades_raw"] = listar_oportunidades(limite=200)
                st.success("Dados atualizados!")
            except Exception as exc:
                st.error(f"Erro ao atualizar dados: {exc}")

        exibir_descartados = st.checkbox("Exibir lotes DESCARTADOS / VERMELHOS", value=False)
        raio_filtro = st.slider(
            "Raio máximo de exibição (km)",
            min_value=50,
            max_value=int(settings.raio_maximo_km),
            value=int(settings.raio_maximo_km),
            step=25,
        )

        categorias_opcoes = ["todas", "caminhonete_4x4", "caminhao", "maquina_agricola", "imovel"]
        categoria_filtro = st.selectbox("Categoria de ativo", categorias_opcoes, index=0)

        with st.expander("Modelos IA Ativos"):
            try:
                modelos_gem = listar_modelos_disponiveis()
                st.caption("Gemini: " + (", ".join(modelos_gem[:4]) if modelos_gem else "padrão"))
            except Exception:
                st.caption("Gemini: cadeia configurada")
            st.caption(f"Secundário: {settings.openai_model or 'auto'}")
            st.caption(f"Última fonte: {ag.ultima_fonte_ia}")

    df_base = _normalizar_dataframe(st.session_state.get("oportunidades_raw", []))
    df_filtrado = filtrar_e_ordenar_oportunidades(
        df_base,
        raio_max_km=float(raio_filtro),
        exibir_descartados=exibir_descartados,
    )
    if categoria_filtro != "todas" and not df_filtrado.empty:
        df_filtrado = df_filtrado[df_filtrado["categoria"] == categoria_filtro].reset_index(drop=True)

    col1, col2, col3, col4 = st.columns(4)
    total_exibidos = len(df_filtrado)
    total_diamante = (
        int((df_filtrado["nivel_oportunidade"] == "DIAMANTE").sum())
        if not df_filtrado.empty
        else 0
    )
    total_verdes = (
        int((df_filtrado["semaforo"] == "VERDE").sum()) if not df_filtrado.empty else 0
    )
    margem_media = (
        float(df_filtrado["margem_liquida_projetada_pct"].mean())
        if not df_filtrado.empty
        else 0.0
    )

    col1.metric("Oportunidades Válidas", total_exibidos)
    col2.metric("Nível DIAMANTE", total_diamante)
    col3.metric("Semáforo VERDE", total_verdes)
    col4.metric("Margem Líquida Média", f"{margem_media:.1f}%")

    st.divider()

    g_col1, g_col2 = st.columns(2)
    with g_col1:
        st.subheader("Margem Líquida (%) vs. Distância")
        if df_filtrado.empty:
            st.info("Sem oportunidades válidas para plotar no gráfico de dispersão.")
        else:
            df_plot = df_filtrado.dropna(subset=["distancia_vinhedo_km"]).copy()
            if df_plot.empty:
                st.info("Nenhuma oportunidade com distância informada para o gráfico.")
            else:
                fig_disp = px.scatter(
                    df_plot,
                    x="distancia_vinhedo_km",
                    y="margem_liquida_projetada_pct",
                    color="nivel_oportunidade",
                    hover_name="titulo_lote",
                    labels={
                        "distancia_vinhedo_km": "Distância de Vinhedo/SP (km)",
                        "margem_liquida_projetada_pct": "Margem Líquida Real (%)",
                        "nivel_oportunidade": "Classificação",
                    },
                )
                fig_disp.add_hline(
                    y=MARGEM_DIAMANTE_PCT,
                    line_dash="dash",
                    annotation_text=f"Linha Diamante ({MARGEM_DIAMANTE_PCT:.0f}%)",
                    annotation_position="top right",
                )
                st.plotly_chart(fig_disp, use_container_width=True)

    with g_col2:
        st.subheader("ROI Líquido Médio por Classe de Ativo")
        if df_filtrado.empty:
            st.info("Sem oportunidades válidas para calcular ROI por categoria.")
        else:
            df_cat = (
                df_filtrado.groupby("categoria", as_index=False)["margem_liquida_projetada_pct"]
                .mean()
                .sort_values("margem_liquida_projetada_pct", ascending=False)
            )
            fig_bar = go.Figure(
                data=[
                    go.Bar(
                        x=df_cat["categoria"],
                        y=df_cat["margem_liquida_projetada_pct"].round(1),
                        text=[f"{v:.1f}%" for v in df_cat["margem_liquida_projetada_pct"]],
                        textposition="auto",
                    )
                ]
            )
            fig_bar.update_layout(
                xaxis_title="Classe de Ativo",
                yaxis_title="ROI Líquido Médio (%)",
            )
            st.plotly_chart(fig_bar, use_container_width=True)

    st.divider()

    st.subheader("Ranking de Lotes Aprovados (Ordenado por Prioridade e Score)")
    if df_filtrado.empty:
        st.info(
            "Nenhum lote aprovado dentro dos critérios (alienação fiduciária/retomada, "
            f"categorias permitidas e raio de até {raio_filtro} km) encontrado no momento."
        )
    else:
        tabela = pd.DataFrame(
            {
                "Nível": df_filtrado["nivel_oportunidade"],
                "Status": df_filtrado["semaforo"],
                "Score": df_filtrado["score_qualidade"].round(0).astype(int),
                "Ativo": df_filtrado["categoria"],
                "Lote": df_filtrado["titulo_lote"],
                "Cidade": df_filtrado["cidade"],
                "UF": df_filtrado["uf"],
                "Dist. (km)": df_filtrado["distancia_vinhedo_km"].apply(_formatar_dist),
                "Lance Atual": df_filtrado["lance_atual"].apply(_formatar_moeda),
                "Avaliação/Mercado": df_filtrado["valor_mercado_referencia"].apply(_formatar_moeda),
                "Margem (%)": df_filtrado["margem_liquida_projetada_pct"].apply(
                    lambda v: f"{float(v):.1f}%" if pd.notna(v) else "—"
                ),
                "Abrir Anúncio": df_filtrado["url_lote"],
            }
        )
        st.dataframe(
            tabela,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Abrir Anúncio": st.column_config.LinkColumn(
                    "Abrir Anúncio",
                    display_text="Abrir Anúncio",
                )
            },
        )

    with st.expander("Avaliar Lote Manualmente"):
        with st.form("form_avaliar_lote"):
            c1, c2, c3 = st.columns(3)
            leiloeiro = c1.text_input("Leiloeiro", value="Portal Leilão")
            comitente = c2.text_input("Comitente / Credor", value="Banco / Alienação Fiduciária")
            num_lote = c3.text_input("Número do Lote", value="01")

            c4, c5, c6 = st.columns(3)
            cidade = c4.text_input("Cidade", value="Campinas")
            uf = c5.text_input("UF", value="SP", max_chars=2)
            url_lote = c6.text_input("URL do Anúncio", value="https://exemplo.com/lote/01")

            titulo = st.text_input(
                "Título do Lote",
                value="Toyota Hilux CD 4x4 Diesel — Recuperado de Financiamento",
            )
            descricao = st.text_area(
                "Descrição / Edital",
                value="Veículo retomado por alienação fiduciária, com chave, sem débitos para o arrematante.",
            )

            c7, c8 = st.columns(2)
            lance_atual = c7.number_input("Lance Atual (R$)", min_value=100.0, value=110000.0, step=1000.0)
            valor_mercado = c8.number_input(
                "Valor de Mercado / Avaliação (R$)", min_value=100.0, value=185000.0, step=1000.0
            )

            submitted = st.form_submit_button("Avaliar e Salvar")
            if submitted:
                try:
                    lote_in = LoteEntrada(
                        leiloeiro=leiloeiro,
                        comitente_credor=comitente,
                        numero_lote=num_lote,
                        url_lote=url_lote,
                        titulo_lote=titulo,
                        descricao_lote=descricao,
                        cidade=cidade,
                        uf=uf,
                        lance_atual=lance_atual,
                        valor_mercado_referencia=valor_mercado,
                    )
                    resp = _correr_async(avaliar_lote_detalhado(lote_in))
                    if resp.aprovado and resp.curadoria:
                        salvar_lote_avaliado(lote_in, resp.curadoria)
                        st.session_state["oportunidades_raw"] = listar_oportunidades(limite=200)
                        st.success(
                            f"Aprovado! Nível: {resp.curadoria.nivel_oportunidade} | "
                            f"Semáforo: {resp.curadoria.semaforo} | Score: {resp.curadoria.score_qualidade}"
                        )
                    else:
                        st.warning(f"Reprovado: {resp.motivo_rejeicao or 'Fora dos critérios.'}")
                except Exception as exc:
                    st.error(f"Erro ao avaliar lote: {exc}")


if __name__ == "__main__":
    main()
