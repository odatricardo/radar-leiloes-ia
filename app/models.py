import hashlib
from typing import Literal
from pydantic import BaseModel, Field


CategoriaAtivo = Literal["caminhonete_4x4", "caminhao", "maquina_agricola", "imovel"]
SemaforoStatus = Literal["VERDE", "AMARELO", "VERMELHO"]
NivelOportunidade = Literal["DIAMANTE", "PADRAO", "ATENCAO", "DESCARTADO"]
PosseStatus = Literal["com_chave", "sem_chave", "desocupado", "ocupado", "nao_informado"]
ResponsavelDebito = Literal["credor_vendedor", "arrematante", "misto_ou_incerto"]


class LoteEntrada(BaseModel):
    leiloeiro: str = Field(..., min_length=2)
    comitente_credor: str = Field(..., min_length=2)
    numero_edital: str | None = None
    numero_lote: str = Field(..., min_length=1)
    url_lote: str
    url_edital_pdf: str | None = None
    titulo_lote: str
    descricao_lote: str
    texto_edital: str | None = None
    cidade: str
    uf: str = Field(..., min_length=2, max_length=2)
    distancia_vinhedo_km: float | None = Field(default=None, ge=0)
    lance_atual: float = Field(..., gt=0)
    valor_mercado_referencia: float = Field(..., gt=0)
    taxa_admin_padrao: float = Field(default=1500.0, ge=0)
    incluir_corretagem_imovel: bool = True

    def gerar_lote_id(self) -> str:
        chave = f"{self.leiloeiro.strip().lower()}|{self.numero_edital or ''}|{self.numero_lote.strip().lower()}"
        return hashlib.sha256(chave.encode("utf-8")).hexdigest()


class TriagemResultado(BaseModel):
    aprovado_triagem: bool
    categoria: CategoriaAtivo | None = None
    modalidade_identificada: str = ""
    confianca_recuperado_financiamento: int = Field(..., ge=0, le=100)
    motivo_descarte: str | None = None


class AuditoriaEditalResultado(BaseModel):
    recuperado_financiamento_confirmado: bool
    posse_ou_chave: PosseStatus = "nao_informado"
    responsavel_debitos_anteriores: ResponsavelDebito = "misto_ou_incerto"
    valor_debitos_repassados_estimado: float = Field(default=0.0, ge=0)
    comissao_leiloeiro_pct: float = Field(default=5.0, ge=0, le=20)
    taxa_administrativa_patio: float = Field(default=1500.0, ge=0)
    sinistro_ou_avaria_grave: bool = False
    restricao_documental_ou_judicial: bool = False
    alertas_criticos: list[str] = Field(default_factory=list)
    resumo_clausulas: str


class ViabilidadeFinanceira(BaseModel):
    distancia_vinhedo_km: float
    frete_estimado: float
    custos_fixos_e_impostos: float
    reserva_contingencia: float
    custo_total_aquisicao: float
    lance_maximo_recomendado: float
    lance_maximo_diamante: float
    margem_liquida_projetada_pct: float
    lucro_liquido_estimado: float
    oportunidade_diamante: bool


class CuradoriaFinal(BaseModel):
    lote_id: str
    semaforo: SemaforoStatus
    nivel_oportunidade: NivelOportunidade
    score_qualidade: int = Field(..., ge=0, le=100)
    resumo_executivo: str
    acao_recomendada: str
    triagem: TriagemResultado
    auditoria: AuditoriaEditalResultado
    viabilidade: ViabilidadeFinanceira