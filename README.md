# RetroLink

Servidor híbrido de arquivos para ambiente Windows, com foco em compatibilidade retro (Windows XP) e recursos modernos de gerenciamento de mídia.

## ✨ Principais recursos

- Interface **Clássica** (`/classico`) em HTML/Jinja2, estilo XP
- Interface **Moderna** (`/moderno`) com React + Tailwind
- Navegação e gerenciamento de arquivos
- Upload e download de arquivos
- Suporte a **múltiplas bibliotecas** (multi-pasta)
- Galeria de fotos (thumbnails + EXIF)
- Player de música e vídeo (streaming)
- Organização de fotos por data
- Detecção de duplicatas
- Conversão de arquivos/mídia
- Backup com histórico de versões
- Bloco de notas compartilhado no modo clássico

---

## 🧱 Stack técnica

- **Backend:** FastAPI
- **Servidor ASGI:** Uvicorn
- **Templates:** Jinja2
- **Frontend moderno:** React (via CDN) + Tailwind CSS
- **Mídia/Imagem:** Pillow, piexif, OpenCV, NumPy
- **Vídeo:** FFmpeg / FFprobe (recomendado no PATH)

---

## 📋 Requisitos

## Obrigatórios

- Python 3.10+ (recomendado 3.11+)
- Pip
- Windows (projeto otimizado para uso nesse cenário)

## Recomendados (recursos de mídia)

- FFmpeg e FFprobe instalados e disponíveis no `PATH`

---

## 📦 Dependências Python

Arquivo `requirements.txt`:

- fastapi==0.110.0
- uvicorn==0.29.0
- jinja2==3.1.3
- python-multipart==0.0.9
- Pillow
- piexif
- opencv-python
- numpy

---

## 🚀 Instalação (manual)

### 1) Clonar o repositório

```bash
git clone https://github.com/willianpn01/RetroLink.git
cd RetroLink
```

### 2) Criar e ativar ambiente virtual

**Windows (PowerShell):**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 3) Instalar dependências

```bash
pip install -r requirements.txt
```

### 4) (Opcional) Definir pasta compartilhada

Se não definir, o fallback será `./compartilhado`.

**PowerShell:**

```powershell
$env:RETROLINK_SHARED_DIR = "D:\RetroLinkCompartilhado"
```

### 5) Executar servidor

```bash
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

Acesse:

- Seleção de modo: `http://localhost:8000/`
- Clássico: `http://localhost:8000/classico`
- Moderno: `http://localhost:8000/moderno`

---

## ⚙️ Instalação rápida no Windows (script)

O projeto inclui `deploy_prod.bat` para setup e execução simplificados.

Uso:

```bat
deploy_prod.bat "D:\RetroLinkCompartilhado" 8000
```

O script:

1. Cria a pasta compartilhada (se necessário)
2. Cria/usa `.venv`
3. Atualiza `pip`
4. Instala dependências
5. Define `RETROLINK_SHARED_DIR`
6. Inicia o servidor

---

## 🗂️ Estrutura do projeto

```text
RetroLink/
├─ main.py
├─ requirements.txt
├─ deploy_prod.bat
├─ bibliotecas.json
├─ compartilhado/
│  └─ .cache/
│     ├─ thumbnails/
│     └─ versoes/
└─ templates/
   ├─ index.html
   ├─ moderno.html
   ├─ detalhes.html
   └─ notas.html
```

---

## 🧩 Multi-biblioteca

As bibliotecas são configuradas em `bibliotecas.json`.

Exemplo:

```json
{
  "bibliotecas": [
    {
      "id": "compartilhado",
      "nome": "Compartilhado XP",
      "caminho": "D:\\RetroLinkCompartilhado",
      "classico": true,
      "icone": "💾"
    }
  ]
}
```

### Regras implementadas

- Deve existir apenas **uma** biblioteca com `classico: true`
- Biblioteca clássica define a base usada em `/classico`
- APIs modernas aceitam `biblioteca_id` para atuar por biblioteca

---

## 🔌 APIs principais

## Bibliotecas

- `GET /api/bibliotecas`
- `POST /api/bibliotecas`
- `PUT /api/bibliotecas/{biblioteca_id}`
- `DELETE /api/bibliotecas/{biblioteca_id}`
- `POST /api/bibliotecas/{biblioteca_id}/definir-classico`

## Arquivos e mídia

- `GET /api/files`
- `GET /api/thumbnail`
- `GET /api/exif`
- `GET /api/video-info`
- `GET /api/stream`
- `GET /api/audio-stream`

## Clássico

- `GET /classico`
- `GET /classico/download/{path:path}`
- `POST /classico/upload`
- `GET /classico/detalhes`
- `GET/POST /classico/notas`

---

## 🔒 Segurança

O backend protege contra path traversal por meio de validação de caminho seguro (`get_safe_path`), garantindo que operações de arquivo não escapem da base permitida.

---

## 🛠️ Troubleshooting

## Erro em recursos de vídeo/thumbnail

- Verifique se `ffmpeg` e `ffprobe` estão no `PATH`

## Porta em uso

- Rode com outra porta:

```bash
python -m uvicorn main:app --host 0.0.0.0 --port 8010
```

## Pasta compartilhada inválida

- Defina `RETROLINK_SHARED_DIR` para um diretório existente com permissões de leitura/escrita

---

## 📝 Observações do repositório

- `DOCUMENTACAO_TECNICA.md` está ignorado pelo `.gitignore` (uso interno)
- `.venv/`, `__pycache__/` e `compartilhado/` também estão ignorados

---

## 📄 Licença

Defina aqui a licença do projeto (ex.: MIT, GPL, proprietária).
