import fitz
import httpx


async def extrair_texto_pdf_url(url_pdf: str | None, max_paginas: int = 25) -> str:
    if not url_pdf:
        return ""

    try:
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
            resp = await client.get(url_pdf)
            resp.raise_for_status()
            conteudo = resp.content

        if not conteudo.startswith(b"%PDF"):
            return ""

        partes: list[str] = []
        with fitz.open(stream=conteudo, filetype="pdf") as doc:
            total = min(len(doc), max_paginas)
            for idx in range(total):
                texto = doc[idx].get_text("text").strip()
                if texto:
                    partes.append(f"--- Página {idx + 1} ---\n{texto}")

        return "\n\n".join(partes)
    except Exception:
        return ""