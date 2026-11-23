import fitz
import httpx

HEADERS_PDF = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,*/*;q=0.8",
}


async def extrair_texto_pdf_url(url_pdf: str | None, max_paginas: int = 15) -> str:
    if not url_pdf or not url_pdf.strip().lower().startswith(("http://", "https://")):
        return ""

    try:
        timeout = httpx.Timeout(connect=6.0, read=12.0, write=6.0, pool=6.0)
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers=HEADERS_PDF,
        ) as client:
            resp = await client.get(url_pdf.strip())
            resp.raise_for_status()
            conteudo = resp.content

        if not conteudo.startswith(b"%PDF"):
            return ""

        partes: list[str] = []
        caracteres_acumulados = 0
        limite_caracteres = 35000

        with fitz.open(stream=conteudo, filetype="pdf") as doc:
            total = min(len(doc), max_paginas)
            for idx in range(total):
                texto = doc[idx].get_text("text").strip()
                if texto:
                    bloco = f"--- Página {idx + 1} ---\n{texto}"
                    partes.append(bloco)
                    caracteres_acumulados += len(bloco)
                    if caracteres_acumulados >= limite_caracteres:
                        break

        return "\n\n".join(partes)
    except Exception:
        return ""
