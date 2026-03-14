import os
import shutil
import re
import json
import asyncio
import mimetypes
import hashlib
import uuid
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, quote
from PIL import Image
import piexif
import cv2
import numpy as np

from fastapi import FastAPI, File, UploadFile, Request, HTTPException, Query, Body, Form
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

# Configuração de diretórios
BASE_DIR = Path(__file__).resolve().parent
SHARED_DIR = Path(os.environ.get("RETROLINK_SHARED_DIR", "./compartilhado")).resolve()
SHARED_DIR.mkdir(parents=True, exist_ok=True)
BIBLIOTECAS_FILE = BASE_DIR / "bibliotecas.json"

CACHE_DIR = SHARED_DIR / ".cache"
THUMBNAILS_DIR = CACHE_DIR / "thumbnails"
THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)

BACKUPS_DIR = SHARED_DIR / "backups"
BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
VERSIONS_DIR = CACHE_DIR / "versoes"
VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_CONFIG_FILE = CACHE_DIR / "backup_config.json"
NOTES_FILE = CACHE_DIR / "notas.txt"

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.heic', '.heif', '.tif', '.tiff'}
VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.mpg', '.mpeg'}
AUDIO_EXTENSIONS = {'.mp3', '.flac', '.ogg', '.wav', '.m4a', '.aac'}

organize_status = {
    "status": "idle",
    "total": 0,
    "processados": 0,
    "mensagem": "",
}
organize_status_lock = asyncio.Lock()

conversion_jobs: dict[str, dict[str, Any]] = {}
conversion_processes: dict[str, asyncio.subprocess.Process] = {}
conversion_jobs_lock = asyncio.Lock()

backup_jobs: dict[str, dict[str, Any]] = {}
backup_jobs_lock = threading.Lock()
backup_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="retrolink-backup")

async def shutdown_cleanup_conversion_processes():
    """Finaliza processos ffmpeg pendentes para evitar ruído no encerramento do servidor."""
    async with conversion_jobs_lock:
        running = list(conversion_processes.items())

    for job_id, process in running:
        try:
            if process.returncode is None:
                process.kill()
                await process.wait()
        except Exception:
            pass

        async with conversion_jobs_lock:
            conversion_processes.pop(job_id, None)
            if job_id in conversion_jobs and conversion_jobs[job_id]["status"] == "em_andamento":
                conversion_jobs[job_id]["status"] = "cancelado"
                conversion_jobs[job_id]["error"] = "Cancelado no encerramento do servidor"

    try:
        backup_executor.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await shutdown_cleanup_conversion_processes()

app = FastAPI(
    title="RetroLink",
    description="Servidor de arquivos híbrido para Windows XP",
    lifespan=lifespan,
)

def _normalize_bibliotecas_payload(payload: Any) -> dict[str, Any]:
    default_entry = {
        "id": "compartilhado",
        "nome": "Compartilhado XP",
        "caminho": str(SHARED_DIR),
        "classico": True,
        "icone": "💾",
    }

    if not isinstance(payload, dict):
        return {"bibliotecas": [default_entry]}

    items = payload.get("bibliotecas", [])
    if not isinstance(items, list):
        items = []

    normalized: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        item_id = str(raw.get("id", "")).strip()
        nome = str(raw.get("nome", "")).strip()
        caminho = str(raw.get("caminho", "")).strip()
        icone = str(raw.get("icone", "📁")).strip() or "📁"
        if not item_id or not nome:
            continue
        normalized.append({
            "id": item_id,
            "nome": nome,
            "caminho": caminho,
            "classico": bool(raw.get("classico", False)),
            "icone": icone,
        })

    if not normalized:
        normalized = [default_entry]

    classico_indexes = [idx for idx, b in enumerate(normalized) if b.get("classico")]
    if not classico_indexes:
        normalized[0]["classico"] = True
        classico_indexes = [0]

    # Garante apenas uma biblioteca clássica
    first_classico = classico_indexes[0]
    for idx, _ in enumerate(normalized):
        normalized[idx]["classico"] = idx == first_classico

    # Preenche caminho da clássica se vier vazio
    if not normalized[first_classico].get("caminho"):
        normalized[first_classico]["caminho"] = str(SHARED_DIR)

    # Se houver caminho vazio em qualquer outra, usa fallback para evitar estado inválido
    for item in normalized:
        if not item.get("caminho"):
            item["caminho"] = str(SHARED_DIR)

    return {"bibliotecas": normalized}

def save_bibliotecas_config(data: dict[str, Any]):
    normalized = _normalize_bibliotecas_payload(data)
    BIBLIOTECAS_FILE.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")

def load_bibliotecas_config() -> dict[str, Any]:
    if not BIBLIOTECAS_FILE.exists():
        initial = _normalize_bibliotecas_payload({"bibliotecas": [{
            "id": "compartilhado",
            "nome": "Compartilhado XP",
            "caminho": "",
            "classico": True,
            "icone": "💾",
        }]})
        save_bibliotecas_config(initial)
        return initial

    try:
        raw = json.loads(BIBLIOTECAS_FILE.read_text(encoding="utf-8"))
    except Exception:
        raw = {}

    normalized = _normalize_bibliotecas_payload(raw)
    save_bibliotecas_config(normalized)
    return normalized

def get_bibliotecas() -> list[dict[str, Any]]:
    config = load_bibliotecas_config()
    return list(config.get("bibliotecas", []))

def get_biblioteca_by_id(biblioteca_id: str) -> dict[str, Any]:
    for biblioteca in get_bibliotecas():
        if biblioteca.get("id") == biblioteca_id:
            return biblioteca
    raise HTTPException(status_code=404, detail="Biblioteca não encontrada")

def get_library_base_dir(biblioteca_id: str | None = None) -> Path:
    if not biblioteca_id:
        return SHARED_DIR
    biblioteca = get_biblioteca_by_id(biblioteca_id)
    base_dir = Path(str(biblioteca.get("caminho", ""))).resolve()
    if not base_dir.exists() or not base_dir.is_dir():
        raise HTTPException(status_code=400, detail="Caminho da biblioteca não existe ou não é diretório")
    return base_dir

def get_classico_base_dir() -> Path:
    for biblioteca in get_bibliotecas():
        if biblioteca.get("classico"):
            base_dir = Path(str(biblioteca.get("caminho", ""))).resolve()
            if base_dir.exists() and base_dir.is_dir():
                return base_dir
    return SHARED_DIR

def get_safe_path(requested_path: str, base_dir: Path | None = None) -> Path:
    """Resolve e valida se o caminho solicitado está dentro do diretório base permitido (Path Traversal protection)."""
    if base_dir is None:
        base_dir = SHARED_DIR
    # Remove barras iniciais para evitar que sejam tratados como caminhos absolutos
    clean_path = requested_path.lstrip("/\\")
    try:
        resolved_path = (base_dir / clean_path).resolve()
        # Verifica se o caminho resolvido começa com o caminho base resolvido
        if not str(resolved_path).startswith(str(base_dir.resolve())):
            raise HTTPException(status_code=400, detail="Tentativa de Path Traversal detectada.")
        return resolved_path
    except Exception:
        raise HTTPException(status_code=400, detail="Caminho inválido.")

# Garante criação/normalização do arquivo de bibliotecas no startup do processo.
load_bibliotecas_config()

# Configuração do Jinja2
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

def get_file_icon(filename: str) -> str:
    """Retorna um emoji/ícone baseado na extensão do arquivo para categorização visual."""
    ext = filename.lower().split('.')[-1] if '.' in filename else ''
    
    icons = {
        # Jogos e Imagens de Disco
        'iso': '💿', 'bin': '💿', 'cue': '💿', 'img': '💿', 'mdf': '💿', 'mds': '💿',
        # Arquivos Compactados
        'zip': '📦', 'rar': '📦', '7z': '📦', 'tar': '📦', 'gz': '📦',
        # Executáveis e Instaladores
        'exe': '💾', 'msi': '💾', 'bat': '💾', 'cmd': '💾',
        # ROMs de Emuladores
        'rom': '🕹️', 'smc': '🕹️', 'sfc': '🕹️', 'nes': '🕹️', 'gba': '🕹️', 'gbc': '🕹️', 'z64': '🕹️', 'n64': '🕹️',
        # Multimídia
        'mp3': '🎵', 'wav': '🎵', 'mid': '🎵', 'avi': '🎬', 'mpg': '🎬', 'mpeg': '🎬', 'wmv': '🎬',
        # Imagens
        'bmp': '🖼️', 'jpg': '🖼️', 'jpeg': '🖼️', 'gif': '🖼️', 'png': '🖼️',
        # Documentos
        'txt': '📝', 'doc': '📝', 'pdf': '📕',
    }
    
    return icons.get(ext, '📄')

def get_file_size_formatted(size_bytes: int) -> str:
    """Formata o tamanho do arquivo para uma leitura mais amigável."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"

def get_unique_destination_path(target_dir: Path, filename: str, reserved: set[str]) -> Path:
    """Gera caminho único sem sobrescrever arquivos existentes (nome, nome_2, nome_3...)."""
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    candidate = target_dir / f"{stem}{suffix}"
    counter = 2

    while candidate.exists() or str(candidate.resolve()) in reserved:
        candidate = target_dir / f"{stem}_{counter}{suffix}"
        counter += 1

    reserved.add(str(candidate.resolve()))
    return candidate

def extract_photo_datetime(file_path: Path) -> datetime:
    """Extrai data da foto via EXIF (DateTimeOriginal). Fallback: mtime do arquivo."""
    try:
        with Image.open(file_path) as img:
            exif_bytes = img.info.get("exif", b"")
            if exif_bytes:
                exif_dict = piexif.load(exif_bytes)
                date_raw = exif_dict.get("Exif", {}).get(piexif.ExifIFD.DateTimeOriginal)
                if date_raw:
                    date_str = date_raw.decode("utf-8", errors="ignore")
                    return datetime.strptime(date_str, "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass

    return datetime.fromtimestamp(file_path.stat().st_mtime)

def build_photo_organization_plan() -> dict[str, Any]:
    """Monta plano de organização de fotos para Fotos Organizadas/Ano/Mês."""
    plan = []
    found = 0
    already_organized = 0
    reserved_targets: set[str] = set()

    for item in SHARED_DIR.rglob("*"):
        if not item.is_file():
            continue

        rel = item.relative_to(SHARED_DIR).as_posix()
        if rel.startswith(".cache/"):
            continue

        if item.suffix.lower() not in IMAGE_EXTENSIONS:
            continue

        found += 1

        if rel.startswith("Fotos Organizadas/"):
            already_organized += 1
            continue

        dt = extract_photo_datetime(item)
        year = dt.strftime("%Y")
        month = dt.strftime("%m")
        destination_dir = SHARED_DIR / "Fotos Organizadas" / year / month
        destination = get_unique_destination_path(destination_dir, item.name, reserved_targets)

        plan.append({
            "source": rel,
            "destination": destination.relative_to(SHARED_DIR).as_posix(),
            "size": item.stat().st_size,
        })

    return {
        "summary": {
            "found": found,
            "already_organized": already_organized,
            "to_move": len(plan),
        },
        "moves": plan,
    }

async def set_organize_status(status: str, total: int = 0, processados: int = 0, mensagem: str = ""):
    async with organize_status_lock:
        organize_status["status"] = status
        organize_status["total"] = total
        organize_status["processados"] = processados
        organize_status["mensagem"] = mensagem

def file_md5(file_path: Path) -> str:
    hasher = hashlib.md5()
    with open(file_path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()

def image_phash(file_path: Path) -> int | None:
    try:
        with Image.open(file_path) as img:
            gray = img.convert("L").resize((32, 32), Image.Resampling.LANCZOS)
            matrix = np.asarray(gray, dtype=np.float32)
            dct = cv2.dct(matrix)
            low = dct[:8, :8]
            med = np.median(low[1:, 1:])
            bits = (low > med).flatten()

            value = 0
            for bit in bits:
                value = (value << 1) | int(bit)
            return int(value)
    except Exception:
        return None

def hamming_distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()

def load_backup_config() -> list[dict[str, Any]]:
    if not BACKUP_CONFIG_FILE.exists():
        return []

    try:
        data = json.loads(BACKUP_CONFIG_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []

        normalized = []
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("source_path"), str):
                normalized.append({
                    "source_path": item["source_path"],
                    "last_backup": item.get("last_backup"),
                })
            elif isinstance(item, str):
                normalized.append({"source_path": item, "last_backup": None})
        return normalized
    except Exception:
        return []

def save_backup_config(config: list[dict[str, Any]]):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

def backup_folder_name_from_source(source_path: Path) -> str:
    drive = source_path.drive.replace(":", "")
    base_name = source_path.name.strip() or "raiz"
    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", base_name)
    if drive:
        return f"{drive}_{safe_name}"
    return safe_name

def preserve_current_version(destination_file: Path):
    if not destination_file.exists() or not destination_file.is_file():
        return

    rel_dest = destination_file.relative_to(SHARED_DIR)
    version_dir = VERSIONS_DIR / rel_dest.parent
    version_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    version_name = f"{timestamp}_{destination_file.name}"
    shutil.copy2(destination_file, version_dir / version_name)

def set_backup_job_fields(job_id: str, **fields):
    with backup_jobs_lock:
        if job_id in backup_jobs:
            backup_jobs[job_id].update(fields)

def run_backup_job_sync(job_id: str):
    config = load_backup_config()
    copied = 0
    skipped = 0
    errors = 0
    total = 0
    last_errors: list[str] = []
    now_str = datetime.now().isoformat(timespec="seconds")

    def push_error(message: str):
        nonlocal errors
        errors += 1
        last_errors.append(message)
        if len(last_errors) > 10:
            last_errors.pop(0)

    try:
        set_backup_job_fields(
            job_id,
            status="em_andamento",
            total=0,
            copiados=0,
            ignorados=0,
            erros=0,
            last_errors=[],
            mensagem="Sincronizando pastas...",
        )

        for item in config:
            source_text = item["source_path"]
            source_dir = Path(source_text)
            if not source_dir.exists() or not source_dir.is_dir():
                push_error(f"Pasta inválida/inacessível: {source_text}")
                continue

            backup_root = BACKUPS_DIR / backup_folder_name_from_source(source_dir)

            for src in source_dir.rglob("*"):
                if not src.is_file():
                    continue

                total += 1
                try:
                    rel_inside_source = src.relative_to(source_dir)
                    dst = backup_root / rel_inside_source
                    dst.parent.mkdir(parents=True, exist_ok=True)

                    if dst.exists() and dst.is_file():
                        src_md5 = file_md5(src)
                        dst_md5 = file_md5(dst)
                        if src_md5 == dst_md5:
                            skipped += 1
                        else:
                            preserve_current_version(dst)
                            shutil.copy2(src, dst)
                            copied += 1
                    else:
                        shutil.copy2(src, dst)
                        copied += 1
                except Exception as e:
                    push_error(f"{src}: {e}")

                set_backup_job_fields(
                    job_id,
                    total=total,
                    copiados=copied,
                    ignorados=skipped,
                    erros=errors,
                    last_errors=last_errors,
                )

            item["last_backup"] = now_str

        save_backup_config(config)

        final_status = "erro" if errors > 0 and copied == 0 and skipped == 0 else "concluido"
        set_backup_job_fields(
            job_id,
            status=final_status,
            total=total,
            copiados=copied,
            ignorados=skipped,
            erros=errors,
            last_errors=last_errors,
            mensagem="Backup finalizado" if final_status == "concluido" else "Backup concluído com erros",
        )
    except Exception as e:
        push_error(str(e))
        set_backup_job_fields(
            job_id,
            status="erro",
            total=total,
            copiados=copied,
            ignorados=skipped,
            erros=errors,
            last_errors=last_errors,
            mensagem="Falha na execução do backup",
        )

def list_versions_for_backup_path(backup_relative_path: str) -> list[dict[str, Any]]:
    dest = get_safe_path(backup_relative_path, SHARED_DIR)
    rel_dest = dest.relative_to(SHARED_DIR)

    if not str(rel_dest).replace("\\", "/").startswith("backups/"):
        raise HTTPException(status_code=400, detail="O caminho informado deve estar dentro de backups/")

    version_dir = VERSIONS_DIR / rel_dest.parent
    if not version_dir.exists() or not version_dir.is_dir():
        return []

    versions = []
    prefix = f"_{rel_dest.name}"
    for item in version_dir.iterdir():
        if not item.is_file():
            continue

        if not item.name.endswith(prefix):
            continue

        timestamp_str = item.name.split("_", 2)[:2]
        date_text = ""
        try:
            stamp = "_".join(timestamp_str)
            dt = datetime.strptime(stamp, "%Y%m%d_%H%M%S")
            date_text = dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            date_text = "Desconhecida"

        versions.append({
            "date": date_text,
            "size": item.stat().st_size,
            "size_formatted": get_file_size_formatted(item.stat().st_size),
            "version_path": item.relative_to(SHARED_DIR).as_posix(),
        })

    versions.sort(key=lambda x: x["version_path"], reverse=True)
    return versions

def extract_version_datetime(version_file: Path) -> datetime | None:
    parts = version_file.name.split("_", 2)
    if len(parts) < 3:
        return None
    stamp = f"{parts[0]}_{parts[1]}"
    try:
        return datetime.strptime(stamp, "%Y%m%d_%H%M%S")
    except Exception:
        return None

def get_versions_cache_stats() -> dict[str, Any]:
    total_files = 0
    total_size = 0
    oldest: datetime | None = None
    newest: datetime | None = None

    for item in VERSIONS_DIR.rglob("*"):
        if not item.is_file():
            continue
        total_files += 1
        size = item.stat().st_size
        total_size += size

        dt = extract_version_datetime(item)
        if dt is None:
            dt = datetime.fromtimestamp(item.stat().st_mtime)

        if oldest is None or dt < oldest:
            oldest = dt
        if newest is None or dt > newest:
            newest = dt

    return {
        "total_files": total_files,
        "total_size": total_size,
        "total_size_formatted": get_file_size_formatted(total_size),
        "oldest": oldest.isoformat(timespec="seconds") if oldest else None,
        "newest": newest.isoformat(timespec="seconds") if newest else None,
    }

def parse_versions_cleanup_cutoff(until: str) -> datetime | None:
    value = (until or "").strip().lower()
    if value == "all":
        return None

    if value.endswith("d") and value[:-1].isdigit():
        days = int(value[:-1])
        return datetime.now() - timedelta(days=days)

    # formato: YYYY-MM-DD
    try:
        date_value = datetime.strptime(value, "%Y-%m-%d")
        return date_value.replace(hour=23, minute=59, second=59)
    except Exception:
        raise HTTPException(status_code=400, detail="Parâmetro 'until' inválido. Use all, Nd (ex: 30d) ou YYYY-MM-DD")

def cleanup_versions_cache(until: str) -> dict[str, Any]:
    cutoff = parse_versions_cleanup_cutoff(until)
    removed_files = 0
    removed_bytes = 0
    errors: list[str] = []

    for item in VERSIONS_DIR.rglob("*"):
        if not item.is_file():
            continue

        try:
            file_dt = extract_version_datetime(item)
            if file_dt is None:
                file_dt = datetime.fromtimestamp(item.stat().st_mtime)

            if cutoff is not None and file_dt > cutoff:
                continue

            size = item.stat().st_size
            item.unlink()
            removed_files += 1
            removed_bytes += size
        except Exception as e:
            errors.append(f"{item}: {e}")
            if len(errors) >= 10:
                break

    # limpeza de diretórios vazios
    for directory in sorted(VERSIONS_DIR.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if directory.is_dir():
            try:
                if not any(directory.iterdir()):
                    directory.rmdir()
            except Exception:
                pass

    return {
        "removed_files": removed_files,
        "removed_bytes": removed_bytes,
        "removed_size_formatted": get_file_size_formatted(removed_bytes),
        "errors": errors,
        "stats": get_versions_cache_stats(),
    }

def build_duplicates_report() -> dict[str, Any]:
    md5_groups: dict[str, list[dict[str, Any]]] = {}
    image_hashes: list[dict[str, Any]] = []

    for item in SHARED_DIR.rglob("*"):
        if not item.is_file():
            continue

        rel = item.relative_to(SHARED_DIR).as_posix()
        if rel.startswith(".cache/"):
            continue

        try:
            size = item.stat().st_size
            md5 = file_md5(item)
            payload = {
                "path": rel,
                "name": item.name,
                "size": size,
            }
            md5_groups.setdefault(md5, []).append(payload)

            if item.suffix.lower() in IMAGE_EXTENSIONS:
                ph = image_phash(item)
                if ph is not None:
                    image_hashes.append({**payload, "phash": ph})
        except Exception:
            continue

    identical = []
    for md5, files in md5_groups.items():
        if len(files) > 1:
            identical.append({
                "hash": md5,
                "files": files,
                "total_size": sum(f["size"] for f in files),
            })

    similar = []
    consumed: set[str] = set()
    for i, base in enumerate(image_hashes):
        if base["path"] in consumed:
            continue

        group = [base]
        consumed.add(base["path"])
        for j in range(i + 1, len(image_hashes)):
            candidate = image_hashes[j]
            if candidate["path"] in consumed:
                continue
            if hamming_distance(base["phash"], candidate["phash"]) <= 10:
                group.append(candidate)
                consumed.add(candidate["path"])

        if len(group) > 1:
            similar.append({
                "files": [{"path": g["path"], "name": g["name"], "size": g["size"]} for g in group],
                "total_size": sum(g["size"] for g in group),
            })

    return {"identicos": identical, "similares": similar}

async def ffprobe_duration_seconds(file_path: Path) -> float:
    process = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(file_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await process.communicate()
    try:
        return float(stdout.decode().strip())
    except Exception:
        return 0.0

def build_conversion_command(input_path: Path, output_path: Path, target_format: str, quality: str) -> list[str]:
    quality = quality.lower()
    target_format = target_format.lower()
    image_target_formats = {"jpg", "jpeg", "png", "webp", "bmp", "tiff", "gif"}

    video_quality_map = {
        "alta": ["-preset", "medium", "-crf", "20"],
        "media": ["-preset", "veryfast", "-crf", "24"],
        "baixa": ["-preset", "ultrafast", "-crf", "30"],
    }
    audio_quality_map = {
        "alta": "320k",
        "media": "192k",
        "baixa": "128k",
    }

    cmd = ["ffmpeg", "-y", "-i", str(input_path)]

    if target_format in image_target_formats:
        image_quality_map = {
            "alta": ("2", "95", "2"),
            "media": ("5", "80", "5"),
            "baixa": ("10", "65", "8"),
        }
        jpg_q, webp_q, png_level = image_quality_map.get(quality, image_quality_map["media"])
        cmd += ["-frames:v", "1"]

        if target_format in {"jpg", "jpeg"}:
            cmd += ["-q:v", jpg_q]
        elif target_format == "webp":
            cmd += ["-c:v", "libwebp", "-quality", webp_q]
        elif target_format == "png":
            cmd += ["-compression_level", png_level]
    elif target_format == "avi":
        # Perfil de compatibilidade com Windows XP
        cmd += ["-c:v", "mpeg4", "-b:v", "800k", "-c:a", "libmp3lame", "-b:a", "96k"]
    elif target_format in {"mp3", "flac", "ogg"}:
        cmd += ["-vn"]
        if target_format == "mp3":
            cmd += ["-c:a", "libmp3lame", "-b:a", audio_quality_map.get(quality, "192k")]
        elif target_format == "flac":
            cmd += ["-c:a", "flac"]
        else:
            cmd += ["-c:a", "libopus", "-b:a", audio_quality_map.get(quality, "192k")]
    elif target_format == "webm":
        cmd += ["-c:v", "libvpx-vp9", "-c:a", "libopus"] + video_quality_map.get(quality, video_quality_map["media"])
    else:
        cmd += ["-c:v", "libx264", "-c:a", "aac"] + video_quality_map.get(quality, video_quality_map["media"])

    cmd.append(str(output_path))
    return cmd

async def run_conversion_job(job_id: str, input_path: Path, output_path: Path, target_format: str, quality: str):
    total_duration = await ffprobe_duration_seconds(input_path)
    command = build_conversion_command(input_path, output_path, target_format, quality)
    stderr_tail = []

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        async with conversion_jobs_lock:
            conversion_processes[job_id] = process

        while True:
            line = await process.stderr.readline()
            if not line:
                break

            text = line.decode("utf-8", errors="ignore").strip()
            if text:
                stderr_tail.append(text)
                if len(stderr_tail) > 20:
                    stderr_tail.pop(0)

            match = re.search(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", text)
            if match and total_duration > 0:
                hours = int(match.group(1))
                minutes = int(match.group(2))
                seconds = float(match.group(3))
                elapsed = hours * 3600 + minutes * 60 + seconds
                progress = min(99, int((elapsed / total_duration) * 100))
                async with conversion_jobs_lock:
                    if job_id in conversion_jobs and conversion_jobs[job_id]["status"] == "em_andamento":
                        conversion_jobs[job_id]["progress"] = progress

        rc = await process.wait()

        async with conversion_jobs_lock:
            conversion_processes.pop(job_id, None)
            if job_id not in conversion_jobs:
                return

            # Se job já foi cancelado
            if conversion_jobs[job_id]["status"] == "cancelado":
                return

            if rc == 0 and output_path.exists():
                conversion_jobs[job_id]["status"] = "concluido"
                conversion_jobs[job_id]["progress"] = 100
                conversion_jobs[job_id]["output_path"] = output_path.relative_to(SHARED_DIR).as_posix()
            else:
                conversion_jobs[job_id]["status"] = "erro"
                conversion_jobs[job_id]["error"] = "\n".join(stderr_tail[-5:]) or "Falha na conversão"
    except Exception as e:
        async with conversion_jobs_lock:
            conversion_processes.pop(job_id, None)
            if job_id in conversion_jobs and conversion_jobs[job_id]["status"] != "cancelado":
                conversion_jobs[job_id]["status"] = "erro"
                conversion_jobs[job_id]["error"] = str(e)

@app.get("/", response_class=HTMLResponse)
async def root():
    """Rota raiz com links para os modos de interface."""
    html_content = """
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
        <meta charset="UTF-8">
        <title>RetroLink - Servidor Híbrido</title>
        <style>
            body {
                font-family: Tahoma, Arial, sans-serif;
                background-color: #004e98;
                color: white;
                text-align: center;
                margin-top: 100px;
            }
            .container {
                background-color: #c0c0c0;
                color: black;
                border: 2px outset #ffffff;
                width: 500px;
                margin: 0 auto;
                padding: 20px;
                box-shadow: 5px 5px 10px rgba(0,0,0,0.5);
            }
            h1 {
                margin-top: 0;
                color: #000080;
                border-bottom: 2px solid #808080;
                padding-bottom: 10px;
            }
            .btn {
                display: block;
                width: 90%;
                margin: 20px auto;
                padding: 15px;
                font-size: 18px;
                font-weight: bold;
                text-decoration: none;
                color: black;
                background-color: #e0e0e0;
                border: 3px outset #ffffff;
                cursor: pointer;
            }
            .btn:active {
                border-style: inset;
                background-color: #d0d0d0;
            }
            .btn-classic {
                background-color: #fffacd;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>RetroLink - Seleção de Modo</h1>
            <p>Escolha a interface adequada para o seu sistema:</p>
            
            <a href="/classico" class="btn btn-classic">
                🖥️ Acessar Modo Clássico (Windows XP)
            </a>
            
            <a href="/moderno" class="btn">
                🚀 Acessar Modo Moderno (React)
            </a>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@app.get("/api/files")
async def api_get_files(
    path: str = Query("", description="Caminho relativo da subpasta"),
    biblioteca_id: str | None = Query(None, description="Biblioteca ativa"),
):
    """Endpoint da API (Fase 2) que retorna a listagem de arquivos em JSON."""
    files_info = []
    
    try:
        base_dir = get_library_base_dir(biblioteca_id)
        current_dir = get_safe_path(path, base_dir)
        if not current_dir.is_dir():
            raise HTTPException(status_code=404, detail="Diretório não encontrado")
            
        # Calcula caminho relativo
        rel_path = current_dir.relative_to(base_dir)
        rel_path_str = str(rel_path).replace('\\', '/') if str(rel_path) != '.' else ""
        
        # Link para diretório pai se não estiver na raiz
        parent_path = ""
        if rel_path_str:
            parent_rel = rel_path.parent
            parent_path = str(parent_rel).replace('\\', '/') if str(parent_rel) != '.' else ""
            files_info.append({
                "name": ".. (Voltar)",
                "icon": '📁',
                "size": "-",
                "is_dir": True,
                "path": parent_path
            })

        for item in current_dir.iterdir():
            item_rel_path = str(item.relative_to(base_dir)).replace('\\', '/')
            if item.is_file():
                stat = item.stat()
                files_info.append({
                    "name": item.name,
                    "icon": get_file_icon(item.name),
                    "size": get_file_size_formatted(stat.st_size),
                    "is_dir": False,
                    "path": item_rel_path
                })
            elif item.is_dir():
                files_info.append({
                    "name": item.name,
                    "icon": '📁',
                    "size": "-",
                    "is_dir": True,
                    "path": item_rel_path
                })
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
        
    # Ordena: pastas primeiro, depois arquivos
    files_info.sort(key=lambda x: (x["name"] != ".. (Voltar)", not x["is_dir"], x["name"].lower()))
    
    return {
        "current_path": rel_path_str,
        "files": files_info,
        "biblioteca_id": biblioteca_id,
    }

@app.get("/api/bibliotecas")
async def api_get_bibliotecas():
    return {"bibliotecas": get_bibliotecas()}

@app.post("/api/bibliotecas")
async def api_create_biblioteca(payload: dict[str, Any] = Body(default={})):
    item_id = str(payload.get("id", "")).strip()
    nome = str(payload.get("nome", "")).strip()
    caminho_raw = str(payload.get("caminho", "")).strip()
    icone = str(payload.get("icone", "📁")).strip() or "📁"

    if not item_id or not nome or not caminho_raw:
        raise HTTPException(status_code=400, detail="Campos obrigatórios: id, nome, caminho")

    config = load_bibliotecas_config()
    bibliotecas = config.get("bibliotecas", [])
    if any(b.get("id") == item_id for b in bibliotecas):
        raise HTTPException(status_code=409, detail="Já existe uma biblioteca com este id")

    caminho = Path(caminho_raw).resolve()
    if not caminho.exists() or not caminho.is_dir():
        raise HTTPException(status_code=400, detail="O caminho informado não existe ou não é diretório")

    created = {
        "id": item_id,
        "nome": nome,
        "caminho": str(caminho),
        "classico": False,
        "icone": icone,
    }
    bibliotecas.append(created)
    save_bibliotecas_config({"bibliotecas": bibliotecas})
    return created

@app.put("/api/bibliotecas/{biblioteca_id}")
async def api_update_biblioteca(biblioteca_id: str, payload: dict[str, Any] = Body(default={})):
    nome = str(payload.get("nome", "")).strip()
    caminho_raw = str(payload.get("caminho", "")).strip()
    icone = str(payload.get("icone", "📁")).strip() or "📁"
    if not nome or not caminho_raw:
        raise HTTPException(status_code=400, detail="Campos obrigatórios: nome, caminho")

    caminho = Path(caminho_raw).resolve()
    if not caminho.exists() or not caminho.is_dir():
        raise HTTPException(status_code=400, detail="O caminho informado não existe ou não é diretório")

    config = load_bibliotecas_config()
    bibliotecas = config.get("bibliotecas", [])
    for item in bibliotecas:
        if item.get("id") == biblioteca_id:
            item["nome"] = nome
            item["caminho"] = str(caminho)
            item["icone"] = icone
            save_bibliotecas_config({"bibliotecas": bibliotecas})
            return item

    raise HTTPException(status_code=404, detail="Biblioteca não encontrada")

@app.delete("/api/bibliotecas/{biblioteca_id}")
async def api_delete_biblioteca(biblioteca_id: str):
    config = load_bibliotecas_config()
    bibliotecas = config.get("bibliotecas", [])

    target = None
    for item in bibliotecas:
        if item.get("id") == biblioteca_id:
            target = item
            break

    if target is None:
        raise HTTPException(status_code=404, detail="Biblioteca não encontrada")
    if target.get("classico"):
        raise HTTPException(status_code=400, detail="A biblioteca clássica não pode ser removida")

    bibliotecas = [b for b in bibliotecas if b.get("id") != biblioteca_id]
    save_bibliotecas_config({"bibliotecas": bibliotecas})
    return {"ok": True}

@app.post("/api/bibliotecas/{biblioteca_id}/definir-classico")
async def api_definir_biblioteca_classica(biblioteca_id: str):
    config = load_bibliotecas_config()
    bibliotecas = config.get("bibliotecas", [])

    found = False
    for item in bibliotecas:
        is_target = item.get("id") == biblioteca_id
        item["classico"] = is_target
        if is_target:
            found = True

    if not found:
        raise HTTPException(status_code=404, detail="Biblioteca não encontrada")

    save_bibliotecas_config({"bibliotecas": bibliotecas})
    return {"ok": True, "bibliotecas": bibliotecas}

@app.post("/api/organizar-fotos/preview")
async def api_organizar_fotos_preview():
    plan = await asyncio.to_thread(build_photo_organization_plan)
    return plan

@app.post("/api/organizar-fotos")
async def api_organizar_fotos():
    async with organize_status_lock:
        if organize_status["status"] == "em_andamento":
            raise HTTPException(status_code=409, detail="Já existe uma organização em andamento")

    async def runner():
        try:
            await set_organize_status("planejando", 0, 0, "Montando plano de organização...")
            plan = await asyncio.to_thread(build_photo_organization_plan)
            moves = plan["moves"]
            total = len(moves)
            await set_organize_status("em_andamento", total, 0, "Movendo fotos...")

            for idx, move in enumerate(moves, start=1):
                src = get_safe_path(move["source"], SHARED_DIR)
                dst = get_safe_path(move["destination"], SHARED_DIR)
                dst.parent.mkdir(parents=True, exist_ok=True)

                if src.exists() and src.is_file():
                    await asyncio.to_thread(shutil.move, str(src), str(dst))

                await set_organize_status("em_andamento", total, idx, f"{idx}/{total} processados")

            await set_organize_status("concluido", total, total, "Organização finalizada")
        except Exception as e:
            await set_organize_status("erro", 0, 0, str(e))

    asyncio.create_task(runner())
    return {"started": True}

@app.get("/api/organizar-fotos/status")
async def api_organizar_fotos_status():
    async with organize_status_lock:
        total = organize_status["total"]
        processados = organize_status["processados"]
        progress = int((processados / total) * 100) if total else 0
        return {
            **organize_status,
            "progress": progress,
        }

@app.get("/api/duplicatas")
async def api_duplicatas():
    report = await asyncio.to_thread(build_duplicates_report)
    return report

@app.delete("/api/duplicatas")
async def api_delete_duplicatas(payload: dict[str, Any] = Body(default={})): 
    paths = payload.get("paths", [])
    if not isinstance(paths, list):
        raise HTTPException(status_code=400, detail="Campo 'paths' deve ser uma lista")

    deleted = 0
    freed_bytes = 0
    for rel_path in paths:
        if not isinstance(rel_path, str):
            continue
        try:
            file_path = get_safe_path(rel_path, SHARED_DIR)
            if file_path.exists() and file_path.is_file():
                size = file_path.stat().st_size
                file_path.unlink()
                deleted += 1
                freed_bytes += size
        except Exception:
            continue

    return {"deleted": deleted, "freed_bytes": freed_bytes}

@app.post("/api/converter")
async def api_converter(payload: dict[str, Any] = Body(default={})):
    input_rel = payload.get("path", "")
    target_format = str(payload.get("formato_destino", "")).lower()
    quality = str(payload.get("qualidade", "media")).lower()

    if not input_rel or not target_format:
        raise HTTPException(status_code=400, detail="Campos 'path' e 'formato_destino' são obrigatórios")

    if target_format not in {"mp4", "mkv", "webm", "mp3", "flac", "ogg", "avi", "jpg", "jpeg", "png", "webp", "bmp", "tiff", "gif"}:
        raise HTTPException(status_code=400, detail="Formato de destino inválido")

    if quality not in {"alta", "media", "baixa"}:
        quality = "media"

    input_path = get_safe_path(input_rel, SHARED_DIR)
    if not input_path.exists() or not input_path.is_file():
        raise HTTPException(status_code=404, detail="Arquivo de origem não encontrado")

    image_target_formats = {"jpg", "jpeg", "png", "webp", "bmp", "tiff", "gif"}
    if target_format in image_target_formats and input_path.suffix.lower() not in IMAGE_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Conversão para imagem requer arquivo de origem de imagem")

    output_dir = input_path.parent
    output_name = f"{input_path.stem}_convertido.{target_format}"
    output_path = get_unique_destination_path(output_dir, output_name, set())

    job_id = uuid.uuid4().hex
    async with conversion_jobs_lock:
        conversion_jobs[job_id] = {
            "job_id": job_id,
            "status": "em_andamento",
            "progress": 0,
            "input_path": input_rel,
            "formato_destino": target_format,
            "qualidade": quality,
            "output_path": None,
            "error": None,
        }

    asyncio.create_task(run_conversion_job(job_id, input_path, output_path, target_format, quality))
    return {"job_id": job_id}

@app.get("/api/converter/status/{job_id}")
async def api_converter_status(job_id: str):
    async with conversion_jobs_lock:
        job = conversion_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job não encontrado")
        return job

@app.post("/api/converter/cancel/{job_id}")
async def api_converter_cancel(job_id: str):
    async with conversion_jobs_lock:
        job = conversion_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job não encontrado")

        process = conversion_processes.get(job_id)
        if process and process.returncode is None:
            process.kill()

        job["status"] = "cancelado"
    return {"ok": True}

@app.get("/api/backup/config")
async def api_backup_config_get():
    return {"folders": load_backup_config()}

@app.post("/api/backup/config")
async def api_backup_config_post(payload: Any = Body(default=[])):
    if isinstance(payload, list):
        paths = payload
    elif isinstance(payload, dict):
        paths = payload.get("paths", [])
    else:
        paths = []

    if not isinstance(paths, list):
        raise HTTPException(status_code=400, detail="Payload inválido, use uma lista de caminhos")

    existing = load_backup_config()
    existing_map = {item["source_path"].lower(): item for item in existing if isinstance(item.get("source_path"), str)}
    normalized = []
    for raw in paths:
        if not isinstance(raw, str):
            continue
        text = raw.strip()
        if not text:
            continue

        source = Path(text)
        if not source.exists() or not source.is_dir():
            raise HTTPException(status_code=400, detail=f"Pasta inválida/inexistente: {text}")

        source_text = str(source)
        prev = existing_map.get(source_text.lower())
        normalized.append({"source_path": source_text, "last_backup": prev.get("last_backup") if prev else None})

    save_backup_config(normalized)
    return {"folders": normalized}

@app.delete("/api/backup/config/{index}")
async def api_backup_config_delete(index: int):
    config = load_backup_config()
    if index < 0 or index >= len(config):
        raise HTTPException(status_code=404, detail="Índice de configuração não encontrado")

    config.pop(index)
    save_backup_config(config)
    return {"folders": config}

@app.post("/api/backup/executar")
async def api_backup_executar():
    job_id = uuid.uuid4().hex
    with backup_jobs_lock:
        backup_jobs[job_id] = {
            "job_id": job_id,
            "status": "em_andamento",
            "total": 0,
            "copiados": 0,
            "ignorados": 0,
            "erros": 0,
            "last_errors": [],
            "mensagem": "Iniciando backup...",
        }

    backup_executor.submit(run_backup_job_sync, job_id)
    return {"job_id": job_id}

@app.get("/api/backup/status/{job_id}")
async def api_backup_status(job_id: str):
    with backup_jobs_lock:
        job = backup_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job de backup não encontrado")

        total = int(job.get("total", 0) or 0)
        processed = int(job.get("copiados", 0) or 0) + int(job.get("ignorados", 0) or 0) + int(job.get("erros", 0) or 0)
        progress = int((processed / total) * 100) if total else 0
        return {
            **job,
            "progress": min(100, progress),
        }

@app.get("/api/versoes")
async def api_versoes(path: str = Query(..., description="Caminho relativo em backups/")):
    versions = list_versions_for_backup_path(path)
    return {
        "path": path,
        "versions": versions,
    }

@app.post("/api/versoes/restaurar")
async def api_versoes_restaurar(payload: dict[str, Any] = Body(default={})):
    version_rel = str(payload.get("version_path", ""))
    destination_rel = str(payload.get("destination_path", ""))

    if not version_rel or not destination_rel:
        raise HTTPException(status_code=400, detail="Campos 'version_path' e 'destination_path' são obrigatórios")

    version_file = get_safe_path(version_rel, SHARED_DIR)
    destination_file = get_safe_path(destination_rel, SHARED_DIR)

    rel_version = version_file.relative_to(SHARED_DIR).as_posix()
    rel_dest = destination_file.relative_to(SHARED_DIR).as_posix()
    if not rel_version.startswith('.cache/versoes/'):
        raise HTTPException(status_code=400, detail="A versão informada deve estar no cache de versões")
    if not rel_dest.startswith('backups/'):
        raise HTTPException(status_code=400, detail="O destino deve estar dentro de backups/")

    if not version_file.exists() or not version_file.is_file():
        raise HTTPException(status_code=404, detail="Arquivo de versão não encontrado")

    destination_file.parent.mkdir(parents=True, exist_ok=True)
    if destination_file.exists() and destination_file.is_file():
        preserve_current_version(destination_file)

    shutil.copy2(version_file, destination_file)
    return {"ok": True, "restored_to": rel_dest}

@app.get("/api/versoes/cache/stats")
async def api_versoes_cache_stats():
    return get_versions_cache_stats()

@app.post("/api/versoes/cache/limpar")
async def api_versoes_cache_limpar(payload: dict[str, Any] = Body(default={})):
    until = str(payload.get("until", "")).strip()
    if not until:
        raise HTTPException(status_code=400, detail="Campo 'until' é obrigatório")
    return cleanup_versions_cache(until)

@app.get("/api/thumbnail")
async def api_get_thumbnail(
    path: str = Query(..., description="Caminho relativo do arquivo de imagem/vídeo"),
    biblioteca_id: str | None = Query(None, description="Biblioteca ativa"),
):
    """Gera e retorna um thumbnail de 300px usando cache. Usa FFmpeg para vídeos e Pillow para fotos."""
    try:
        base_dir = get_library_base_dir(biblioteca_id)
        file_path = get_safe_path(path, base_dir)
        if not file_path.exists() or not file_path.is_file():
            raise HTTPException(status_code=404, detail="Arquivo não encontrado")
            
        rel_path = file_path.relative_to(base_dir)
        cache_base = THUMBNAILS_DIR if not biblioteca_id else (THUMBNAILS_DIR / f"lib_{biblioteca_id}")
        cache_path = cache_base / rel_path.with_suffix('.jpg')
        
        # Cria as subpastas no cache se não existirem
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Verifica se o cache existe e é mais novo que o original
        if cache_path.exists() and cache_path.stat().st_mtime > file_path.stat().st_mtime:
            return FileResponse(cache_path)
            
        ext = file_path.suffix.lower()
        # Se for vídeo, extrair o primeiro frame
        if ext in ['.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv']:
            try:
                # Tenta usar o ffmpeg primeiro
                process = await asyncio.create_subprocess_exec(
                    'ffmpeg', '-y', '-i', str(file_path), '-vframes', '1', 
                    '-vf', 'scale=300:-1', str(cache_path),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL
                )
                await process.communicate()
            except FileNotFoundError:
                # ffmpeg não está no PATH, usa OpenCV (cv2) como fallback
                process = None
                
            # Se o ffmpeg falhou ou não existe, usa cv2
            if not cache_path.exists():
                try:
                    cap = cv2.VideoCapture(str(file_path))
                    success, frame = cap.read()
                    if success:
                        # Redimensiona mantendo a proporção (largura 300px)
                        h, w = frame.shape[:2]
                        new_h = int(300 * h / w)
                        frame = cv2.resize(frame, (300, new_h))
                        cv2.imwrite(str(cache_path), frame)
                    cap.release()
                except Exception as e:
                    print(f"Erro cv2 thumbnail: {e}")
                    
            if not cache_path.exists():
                raise HTTPException(status_code=500, detail="Erro ao gerar thumbnail do vídeo")
        else:
            # Se for imagem, usar Pillow
            with Image.open(file_path) as img:
                # Remove EXIF orientation metadata that might rotate the thumbnail incorrectly
                try:
                    exif = img.getexif()
                    if exif and piexif.ImageIFD.Orientation in exif:
                        orientation = exif[piexif.ImageIFD.Orientation]
                        if orientation == 3: img = img.rotate(180, expand=True)
                        elif orientation == 6: img = img.rotate(270, expand=True)
                        elif orientation == 8: img = img.rotate(90, expand=True)
                except Exception:
                    pass # Ignore EXIF parsing errors for thumbnails
                    
                img.thumbnail((300, 300))
                # Converte para RGB para salvar como JPG
                if img.mode in ('RGBA', 'P'):
                    img = img.convert('RGB')
                img.save(cache_path, "JPEG", quality=85)
                
        return FileResponse(cache_path)
    except HTTPException:
        raise
    except Exception as e:
        print(f"Erro em /api/thumbnail: {e}")
        raise HTTPException(status_code=500, detail="Erro interno ao processar thumbnail")

@app.get("/api/exif")
async def api_get_exif(
    path: str = Query(..., description="Caminho relativo da imagem"),
    biblioteca_id: str | None = Query(None, description="Biblioteca ativa"),
):
    """Retorna os metadados EXIF da imagem (data, GPS, modelo)."""
    try:
        file_path = get_safe_path(path, get_library_base_dir(biblioteca_id))
        if not file_path.exists() or not file_path.is_file():
            raise HTTPException(status_code=404, detail="Arquivo não encontrado")
            
        try:
            with Image.open(file_path) as img:
                exif_dict = piexif.load(img.info.get("exif", b""))
                
                # Extrai informações úteis
                metadata = {}
                
                # Câmera / Modelo
                if piexif.ImageIFD.Make in exif_dict["0th"]:
                    metadata["make"] = exif_dict["0th"][piexif.ImageIFD.Make].decode('utf-8').strip('\x00')
                if piexif.ImageIFD.Model in exif_dict["0th"]:
                    metadata["model"] = exif_dict["0th"][piexif.ImageIFD.Model].decode('utf-8').strip('\x00')
                    
                # Data de Captura
                if piexif.ExifIFD.DateTimeOriginal in exif_dict["Exif"]:
                    date_str = exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal].decode('utf-8')
                    # Converter "YYYY:MM:DD HH:MM:SS" para legível
                    try:
                        parts = date_str.split(" ")
                        metadata["date"] = f"{parts[0].replace(':', '/')} {parts[1]}"
                    except:
                        metadata["date"] = date_str
                
                # Resolução original
                metadata["resolution"] = f"{img.width} x {img.height}"
                
                return metadata
        except Exception as e:
            # Se não tiver EXIF ou não for JPEG/TIFF suportado, retornar objeto vazio
            return {}
            
    except HTTPException:
        raise
    except Exception as e:
        return {}

@app.get("/api/video-info")
async def api_get_video_info(
    path: str = Query(..., description="Caminho relativo do vídeo"),
    biblioteca_id: str | None = Query(None, description="Biblioteca ativa"),
):
    """Retorna duração, resolução e codec usando ffprobe."""
    try:
        file_path = get_safe_path(path, get_library_base_dir(biblioteca_id))
        if not file_path.exists() or not file_path.is_file():
            raise HTTPException(status_code=404, detail="Arquivo não encontrado")
            
        process = await asyncio.create_subprocess_exec(
            'ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', '-show_streams', str(file_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await process.communicate()
        
        if process.returncode != 0:
            return {"error": "Falha ao ler metadados"}
            
        data = json.loads(stdout)
        info = {"duration": "00:00", "resolution": "Desconhecida", "codec": "Desconhecido"}
        
        # Parse duração
        if "format" in data and "duration" in data["format"]:
            try:
                seconds = float(data["format"]["duration"])
                mins, secs = divmod(seconds, 60)
                hours, mins = divmod(mins, 60)
                if hours > 0:
                    info["duration"] = f"{int(hours):02d}:{int(mins):02d}:{int(secs):02d}"
                else:
                    info["duration"] = f"{int(mins):02d}:{int(secs):02d}"
            except: pass
            
        # Parse streams de vídeo para resolução e codec
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                info["codec"] = stream.get("codec_name", "Desconhecido").upper()
                width = stream.get("width")
                height = stream.get("height")
                if width and height:
                    info["resolution"] = f"{width}x{height}"
                break
                
        return info
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/stream")
async def api_stream_video(
    request: Request,
    path: str = Query(..., description="Caminho relativo do vídeo"),
    biblioteca_id: str | None = Query(None, description="Biblioteca ativa"),
):
    """
    Streaming de vídeo. Se for MP4/WebM nativo, suporta Range Requests (seek).
    Se for MKV/AVI/MOV, faz transcode on-the-fly usando ffmpeg.
    """
    file_path = get_safe_path(path, get_library_base_dir(biblioteca_id))
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")
        
    ext = file_path.suffix.lower()
    
    # Formatos suportados nativamente (Range Requests)
    if ext in ['.mp4', '.webm', '.ogg']:
        file_size = file_path.stat().st_size
        range_header = request.headers.get("Range")
        
        if range_header:
            match = re.match(r"bytes=(\d+)-(\d*)", range_header)
            if not match:
                raise HTTPException(status_code=400, detail="Range header inválido")
            
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else file_size - 1
            length = end - start + 1
            
            def file_chunk_generator(start_byte, chunk_size):
                with open(file_path, "rb") as f:
                    f.seek(start_byte)
                    remaining = chunk_size
                    while remaining > 0:
                        chunk = f.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        yield chunk

            return StreamingResponse(
                file_chunk_generator(start, length),
                status_code=206,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(length),
                    "Content-Type": f"video/{ext.lstrip('.')}"
                }
            )
        else:
            return FileResponse(
                path=file_path,
                headers={"Accept-Ranges": "bytes"}
            )
            
    # Formatos que exigem transcode em tempo real (MKV, AVI, etc)
    else:
        async def ffmpeg_stream():
            process = await asyncio.create_subprocess_exec(
                'ffmpeg', '-i', str(file_path),
                '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28',
                '-c:a', 'aac', '-b:a', '128k',
                '-f', 'mp4', '-movflags', 'frag_keyframe+empty_moov', 'pipe:1',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            try:
                while True:
                    chunk = await process.stdout.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
            except Exception:
                pass
            finally:
                if process.returncode is None:
                    process.kill()
                    
        return StreamingResponse(
            ffmpeg_stream(),
            media_type="video/mp4"
        )

@app.get("/moderno", response_class=HTMLResponse)
async def modo_moderno(request: Request):
    """Renderiza a interface moderna em React (SPA)."""
    return templates.TemplateResponse("moderno.html", {"request": request})

@app.get("/classico", response_class=HTMLResponse)
async def modo_classico(
    request: Request,
    path: str = Query("", description="Caminho relativo da subpasta"),
    busca: str = Query("", description="Busca por nome (case-insensitive)"),
    ordenar: str = Query("nome", description="Ordenação: nome|tamanho|data"),
    ordem: str = Query("asc", description="Ordem: asc|desc"),
):
    """Renderiza a interface clássica listando os arquivos disponíveis."""
    files_info: list[dict[str, Any]] = []
    parent_entry = None
    rel_path_str = ""

    busca = (busca or "").strip()
    ordenar = (ordenar or "nome").strip().lower()
    ordem = (ordem or "asc").strip().lower()
    if ordenar not in {"nome", "tamanho", "data"}:
        ordenar = "nome"
    if ordem not in {"asc", "desc"}:
        ordem = "asc"
    is_desc = ordem == "desc"

    def build_classico_url(target_path: str, target_ordenar: str, target_ordem: str, target_busca: str | None = None) -> str:
        params = {
            "path": target_path,
            "ordenar": target_ordenar,
            "ordem": target_ordem,
        }
        if target_busca is None:
            target_busca = busca
        if target_busca:
            params["busca"] = target_busca
        return "/classico?" + urlencode(params)

    try:
        classico_base = get_classico_base_dir()
        current_dir = get_safe_path(path, classico_base)
        if not current_dir.is_dir():
            raise HTTPException(status_code=404, detail="Diretório não encontrado")
            
        # Calcula caminho relativo para exibição e links
        rel_path = current_dir.relative_to(classico_base)
        rel_path_str = str(rel_path).replace('\\', '/') if str(rel_path) != '.' else ""
        
        # Link para diretório pai se não estiver na raiz
        parent_path = ""
        if rel_path_str:
            parent_rel = rel_path.parent
            parent_path = str(parent_rel).replace('\\', '/') if str(parent_rel) != '.' else ""
            parent_entry = {
                "name": ".. (Voltar)",
                "icon": '📁',
                "size": "-",
                "modified": "-",
                "is_dir": True,
                "path": parent_path,
                "open_url": build_classico_url(parent_path, ordenar, ordem),
            }

        # Lê os arquivos do diretório atual
        for item in current_dir.iterdir():
            item_rel_path = str(item.relative_to(classico_base)).replace('\\', '/')
            stat = item.stat()
            modified_text = datetime.fromtimestamp(stat.st_mtime).strftime("%d/%m/%Y %H:%M")

            entry = {
                "name": item.name,
                "size": "-",
                "size_bytes": 0,
                "modified": modified_text,
                "modified_ts": stat.st_mtime,
                "is_dir": item.is_dir(),
                "path": item_rel_path,
                "open_url": build_classico_url(item_rel_path, ordenar, ordem),
                "download_url": f"/classico/download/{quote(item_rel_path, safe='/')}",
                "details_url": "/classico/detalhes?" + urlencode({"path": item_rel_path}),
            }

            if item.is_file():
                entry["icon"] = get_file_icon(item.name)
                entry["size"] = get_file_size_formatted(stat.st_size)
                entry["size_bytes"] = stat.st_size
            elif item.is_dir():
                entry["icon"] = '📁'

            if busca and busca.lower() not in item.name.lower():
                continue

            files_info.append(entry)
    except HTTPException:
        raise
    except Exception as e:
        print(f"Erro ao ler diretório: {e}")

    # Ordena por coluna selecionada e mantém pastas acima de arquivos.
    if ordenar == "tamanho":
        files_info.sort(key=lambda x: x.get("size_bytes", 0), reverse=is_desc)
    elif ordenar == "data":
        files_info.sort(key=lambda x: x.get("modified_ts", 0), reverse=is_desc)
    else:
        files_info.sort(key=lambda x: x["name"].lower(), reverse=is_desc)
    files_info.sort(key=lambda x: not x["is_dir"])

    if parent_entry and not busca:
        files_info.insert(0, parent_entry)

    def sort_header_url(column: str) -> str:
        next_ordem = "asc"
        if ordenar == column:
            next_ordem = "desc" if ordem == "asc" else "asc"
        return build_classico_url(rel_path_str, column, next_ordem)

    sort_symbols = {
        "nome": "▲" if ordenar == "nome" and ordem == "asc" else "▼" if ordenar == "nome" else "",
        "tamanho": "▲" if ordenar == "tamanho" and ordem == "asc" else "▼" if ordenar == "tamanho" else "",
        "data": "▲" if ordenar == "data" and ordem == "asc" else "▼" if ordenar == "data" else "",
    }

    return templates.TemplateResponse("index.html", {
        "request": request,
        "files": files_info,
        "current_path": rel_path_str,
        "busca": busca,
        "ordenar": ordenar,
        "ordem": ordem,
        "clear_search_url": "/classico?" + urlencode({"path": rel_path_str, "ordenar": ordenar, "ordem": ordem}),
        "sort_nome_url": sort_header_url("nome"),
        "sort_tamanho_url": sort_header_url("tamanho"),
        "sort_data_url": sort_header_url("data"),
        "sort_symbols": sort_symbols,
    })

@app.get("/classico/notas", response_class=HTMLResponse)
async def classico_notas(request: Request, salvo: int = Query(0, description="Mostra confirmação de salvamento")):
    conteudo = ""
    ultima_modificacao = "Nunca salvo"

    if NOTES_FILE.exists() and NOTES_FILE.is_file():
        try:
            conteudo = NOTES_FILE.read_text(encoding="utf-8")
        except Exception:
            conteudo = ""

        try:
            ultima_modificacao = datetime.fromtimestamp(NOTES_FILE.stat().st_mtime).strftime("%d/%m/%Y %H:%M:%S")
        except Exception:
            ultima_modificacao = "Nunca salvo"

    return templates.TemplateResponse("notas.html", {
        "request": request,
        "conteudo": conteudo,
        "salvo": bool(salvo),
        "ultima_modificacao": ultima_modificacao,
    })

@app.post("/classico/notas")
async def classico_notas_save(conteudo: str = Form("")):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    NOTES_FILE.write_text(conteudo, encoding="utf-8")
    return RedirectResponse(url="/classico/notas?salvo=1", status_code=303)

@app.get("/classico/detalhes", response_class=HTMLResponse)
async def classico_detalhes(request: Request, path: str = Query(..., description="Caminho relativo do arquivo")):
    classico_base = get_classico_base_dir()
    file_path = get_safe_path(path, classico_base)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")

    rel_path = file_path.relative_to(classico_base).as_posix()
    stat = file_path.stat()
    parent_rel = file_path.parent.relative_to(classico_base)
    parent_path = "" if str(parent_rel) == "." else parent_rel.as_posix()

    return templates.TemplateResponse("detalhes.html", {
        "request": request,
        "icon": get_file_icon(file_path.name),
        "name": file_path.name,
        "relative_path": rel_path,
        "size": get_file_size_formatted(stat.st_size),
        "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%d/%m/%Y %H:%M:%S"),
        "extension": file_path.suffix.lower().lstrip(".") or "(sem extensão)",
        "download_url": f"/classico/download/{quote(rel_path, safe='/')}",
        "back_url": "/classico?" + urlencode({"path": parent_path}),
    })

def iterfile(file_path: Path):
    """Gerador seguro para streaming de arquivos em chunks."""
    file_like = open(file_path, mode="rb")
    try:
        # Lê em pedaços de 1MB para evitar sobrecarga de memória no Windows 10
        while True:
            chunk = file_like.read(1024 * 1024)
            if not chunk:
                break
            yield chunk
    finally:
        # Garante que o arquivo seja fechado caso a conexão caia no meio do download
        file_like.close()

@app.get("/api/audio-stream")
async def audio_stream(
    path: str = Query(..., description="Caminho relativo do áudio"),
    biblioteca_id: str | None = Query(None, description="Biblioteca ativa"),
):
    """Stream de áudio para o player moderno com suporte nativo a seek via Range requests."""
    file_path = get_safe_path(path, get_library_base_dir(biblioteca_id))

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Arquivo de áudio não encontrado")

    content_type, _ = mimetypes.guess_type(str(file_path))
    if not content_type:
        content_type = "audio/mpeg"

    return FileResponse(path=file_path, media_type=content_type)

@app.get("/classico/download/{path:path}")
async def download_file(path: str, biblioteca_id: str | None = Query(None, description="Biblioteca ativa")):
    """Endpoint otimizado para download de arquivos."""
    base_dir = get_library_base_dir(biblioteca_id) if biblioteca_id else get_classico_base_dir()
    file_path = get_safe_path(path, base_dir)
        
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")
        
    # StreamingResponse com gerador seguro para garantir o fechamento do arquivo
    return StreamingResponse(
        iterfile(file_path),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{file_path.name}"'}
    )

@app.post("/classico/upload")
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    path: str = Query("", description="Pasta de destino"),
    biblioteca_id: str | None = Query(None, description="Biblioteca ativa"),
):
    """Recebe upload de arquivo via formulário tradicional e redireciona de volta."""
    if not file.filename:
        return HTMLResponse(content="<h1>Erro: Nenhum arquivo selecionado</h1><a href='/classico'>Voltar</a>", status_code=400)
        
    # Verifica o caminho de destino do upload
    base_dir = get_library_base_dir(biblioteca_id) if biblioteca_id else get_classico_base_dir()
    dest_dir = get_safe_path(path, base_dir)
    if not dest_dir.is_dir():
        return HTMLResponse(content="<h1>Erro: Diretório de destino inválido</h1><a href='/classico'>Voltar</a>", status_code=400)
        
    # Concatena o nome do arquivo enviado ao diretório de destino validado
    file_path = dest_dir / os.path.basename(file.filename)
    
    try:
        # Lê e escreve em chunks (pedaços) para não carregar arquivos gigantes (ex: ISOs) inteiros na RAM
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception as e:
        return HTMLResponse(content=f"<h1>Erro no upload: {e}</h1><a href='/classico'>Voltar</a>", status_code=500)
    finally:
        file.file.close()
        
    # Retorna um snippet de HTML que, rodando dentro do iframe, chama uma função no parent para atualizar a tela
    success_html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Upload Concluído</title>
        <script>
            // Tenta recarregar a página pai se houver suporte a JS (mesmo antigo)
            try {
                window.parent.location.reload();
            } catch(e) {
                // Fallback para navegadores MUITO restritos
                document.write('Upload concluído! Atualize a página principal manualmente.');
            }
        </script>
    </head>
    <body style="font-family: Tahoma, Arial; font-size: 12px; background-color: #c0c0c0;">
        Upload de "%s" concluído! <a href="/classico?path=%s" target="_parent">Voltar</a>
    </body>
    </html>
    """ % (file.filename, path)
    
    return HTMLResponse(content=success_html)

if __name__ == "__main__":
    import uvicorn
    # Inicia o servidor escutando em todas as interfaces de rede (0.0.0.0)
    # na porta 8000. Isso permite acesso pelo Windows XP através do IP do Windows 10.
    try:
        uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
    except (KeyboardInterrupt, SystemExit):
        print("Servidor encerrado.")
